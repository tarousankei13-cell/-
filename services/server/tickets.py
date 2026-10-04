"""
問い合わせチケット

利用者がボタンを押すと、その人と担当者だけが見える場所ができる。

⚠️ **チャンネル方式とスレッド方式で、担当者の見え方が違う。**
     チャンネル … ロールに閲覧権限を与えるだけで担当者全員に見える
     スレッド   … ロールを @メンション してもスレッドには入らない。
                  親チャンネルで「スレッドの管理」権限が必要

⚠️ 書き起こし（transcript）は channel.history() で取る。
   これは REST の取得なので、**MESSAGE CONTENT INTENT が無くても
   本文が読める**（インテントが止めているのはゲートウェイの通知だけ）。
   消されたメッセージの内容だけは、あとから取れない。
"""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import func, select

import config
from core import settings
from db.models import Ticket, as_utc
from db.session import session_scope

log = logging.getLogger("bot.server.tickets")


class TicketError(Exception):
    """利用者にそのまま見せてよい、断る理由。"""


# 開いているチケットのチャンネルID。
#   ⚠️ **毎メッセージDBを引かないため**に持っている。
#      ここが古いと「放置の自動クローズ」がずれるだけなので、
#      取りこぼしても害は小さい。起動時と作成・終了のたびに直す。
_open_channels: set[int] = set()


def is_ticket_channel(channel_id: int | None) -> bool:
    """開いているチケットの場所か（DBを引かない）。"""
    return channel_id is not None and channel_id in _open_channels


async def reload_cache() -> int:
    """起動時に呼ぶ。開いているチケットを覚え直す。"""
    async with session_scope() as s:
        rows = (await s.execute(
            select(Ticket.channel_id).where(
                Ticket.status != Ticket.STATUS_CLOSED
            )
        )).scalars().all()
    _open_channels.clear()
    _open_channels.update(int(c) for c in rows)
    return len(_open_channels)


# ------------------------------------------------------------
#  設定の読み出し
# ------------------------------------------------------------

def enabled() -> bool:
    return bool(settings.get("ticket_enabled", False))


def mode() -> str:
    m = str(settings.get("ticket_mode", config.TICKET_MODE) or "channel")
    return m if m in ("channel", "thread") else "channel"


def staff_role_ids() -> list[int]:
    return [int(r) for r in (settings.get("ticket_staff_roles") or [])]


def kinds() -> list[dict]:
    """問い合わせの種別。設定が壊れていても既定へ落とす。"""
    raw = settings.get("ticket_kinds") or []
    out = [k for k in raw if isinstance(k, dict) and k.get("key") and k.get("label")]
    return out or list(config.TICKET_KINDS_DEFAULT)


def kind_of(key: str) -> dict:
    for k in kinds():
        if k.get("key") == key:
            return k
    return {"key": key, "label": key, "emoji": "💬", "desc": ""}


def is_staff(member: discord.Member | None) -> bool:
    """担当者か。サーバーを管理できる人も担当者として扱う。"""
    if member is None:
        return False
    perms = getattr(member, "guild_permissions", None)
    if perms is not None and (perms.administrator or perms.manage_guild):
        return True
    wanted = set(staff_role_ids())
    if not wanted:
        return False
    return any(r.id in wanted for r in getattr(member, "roles", []))


# ------------------------------------------------------------
#  いまの状態
# ------------------------------------------------------------

async def open_tickets(guild_id: int, opener_id: int | None = None) -> list[Ticket]:
    """開いているチケット。"""
    async with session_scope() as s:
        q = select(Ticket).where(
            Ticket.guild_id == guild_id,
            Ticket.status != Ticket.STATUS_CLOSED,
        )
        if opener_id is not None:
            q = q.where(Ticket.opener_id == opener_id)
        return list((await s.execute(q.order_by(Ticket.id))).scalars().all())


async def by_channel(channel_id: int) -> Ticket | None:
    async with session_scope() as s:
        return (await s.execute(
            select(Ticket).where(Ticket.channel_id == channel_id)
        )).scalar_one_or_none()


async def counts(guild_id: int) -> dict[str, int]:
    """状態ごとの件数。"""
    async with session_scope() as s:
        rows = (await s.execute(
            select(Ticket.status, func.count())
            .where(Ticket.guild_id == guild_id)
            .group_by(Ticket.status)
        )).all()
    return {str(st): int(n) for st, n in rows}


# ------------------------------------------------------------
#  作る
# ------------------------------------------------------------

def _overwrites(
    guild: discord.Guild, opener: discord.Member, me: discord.Member,
) -> dict:
    """
    専用チャンネルの見え方。

    ⚠️ @everyone を閉じるのを忘れないこと。忘れると全員に見える。
    ⚠️ BOT自身にも明示的に許可を与える。カテゴリの設定で
       BOTが閉め出されていると、作った直後に書き込めなくなる。
    """
    ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        opener: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            read_message_history=True, attach_files=True,
            embed_links=True,
        ),
        me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True,
            read_message_history=True, manage_channels=True,
            manage_messages=True, embed_links=True, attach_files=True,
        ),
    }
    for rid in staff_role_ids():
        role = guild.get_role(rid)
        if role is not None:
            ow[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, manage_messages=True,
                embed_links=True, attach_files=True,
            )
    return ow


async def _check_room(guild: discord.Guild) -> str | None:
    """
    作れる余地があるか。危ないときは注意書きを返す。

    ⚠️ 上限に当たってから気付くと、利用者が問い合わせられない。
       先に知らせる。
    """
    cid = settings.get("ticket_category")
    if not cid:
        return None
    cat = guild.get_channel(int(cid))
    if not isinstance(cat, discord.CategoryChannel):
        return None
    left = config.TICKET_CATEGORY_LIMIT - len(cat.channels)
    if left <= 0:
        raise TicketError(
            "チケットの置き場所がいっぱいです（1カテゴリ50個まで）。"
            "管理者に、終わったチケットの整理をお願いしてください。"
        )
    if left <= config.TICKET_CATEGORY_WARN:
        return f"置き場所の残りが {left} 個です"
    return None


async def create(
    guild: discord.Guild,
    opener: discord.Member,
    *,
    kind: str = "other",
    subject: str | None = None,
) -> tuple[Ticket, discord.abc.Messageable, str | None]:
    """
    チケットを作る。

    返り値は（記録・話す場所・管理者への注意書き）。

    ⚠️ 先に場所を作り、あとで記録を書く。
       逆にすると、場所を作れなかったときに幽霊の記録が残る。
    """
    if not enabled():
        raise TicketError("いま問い合わせを受け付けていません。")

    limit = int(settings.get("ticket_max_open", 2) or 2)
    if limit > 0:
        mine = await open_tickets(guild.id, opener.id)
        if len(mine) >= limit:
            where = "、".join(f"<#{t.channel_id}>" for t in mine)
            raise TicketError(
                f"すでに {len(mine)} 件の問い合わせが開いています（{where}）。\n"
                "そちらで続けてお書きください。"
            )

    me = guild.me
    if me is None:
        raise TicketError("BOTの状態を取得できませんでした。少し待ってお試しください。")

    note = await _check_room(guild)
    info = kind_of(kind)
    # 次の番号を先に見ておく（名前に入れるため）
    async with session_scope() as s:
        last = (await s.execute(
            select(func.max(Ticket.id)).where(Ticket.guild_id == guild.id)
        )).scalar()
    number = int(last or 0) + 1
    name = f"ticket-{number:04d}-{opener.name}"[:95]

    use_thread = mode() == "thread"
    place: discord.abc.Messageable

    if use_thread:
        parent_id = settings.get("ticket_channel")
        parent = guild.get_channel(int(parent_id)) if parent_id else None
        if not isinstance(parent, discord.TextChannel):
            raise TicketError(
                "問い合わせの置き場所が設定されていません。"
                "管理者に `/ticket setup` をお願いしてください。"
            )
        try:
            place = await parent.create_thread(
                name=name,
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=10080,     # 7日
                reason=f"チケット {number:04d}（{opener}）",
            )
            await place.add_user(opener)
        except discord.Forbidden as e:
            raise TicketError(
                "問い合わせの場所を作れませんでした。"
                "BOTに「プライベートスレッドの作成」権限が必要です。"
            ) from e
    else:
        cat_id = settings.get("ticket_category")
        cat = guild.get_channel(int(cat_id)) if cat_id else None
        if cat is not None and not isinstance(cat, discord.CategoryChannel):
            cat = None
        try:
            place = await guild.create_text_channel(
                name=name,
                category=cat,
                overwrites=_overwrites(guild, opener, me),
                topic=f"{info['label']}　{opener}（{opener.id}）",
                reason=f"チケット {number:04d}（{opener}）",
            )
        except discord.Forbidden as e:
            raise TicketError(
                "問い合わせの場所を作れませんでした。"
                "BOTに「チャンネルの管理」権限が必要です。"
            ) from e

    async with session_scope() as s:
        row = Ticket(
            guild_id=guild.id,
            channel_id=place.id,
            is_thread=use_thread,
            opener_id=opener.id,
            kind=kind,
            subject=(subject or "")[:200] or None,
        )
        s.add(row)
        await s.flush()
        ticket = Ticket(
            id=row.id, guild_id=row.guild_id, channel_id=row.channel_id,
            is_thread=row.is_thread, opener_id=row.opener_id, kind=row.kind,
            subject=row.subject, status=row.status,
        )

    _open_channels.add(place.id)
    log.info("チケット %s を作りました（%s / %s）",
             ticket.number, opener, "スレッド" if use_thread else "チャンネル")
    return ticket, place, note


# ------------------------------------------------------------
#  担当する
# ------------------------------------------------------------

async def claim(channel_id: int, staff_id: int) -> Ticket:
    """担当者を決める。"""
    async with session_scope() as s:
        row = (await s.execute(
            select(Ticket).where(Ticket.channel_id == channel_id)
        )).scalar_one_or_none()
        if row is None:
            raise TicketError("ここはチケットではありません。")
        if row.status == Ticket.STATUS_CLOSED:
            raise TicketError("このチケットは終了しています。")
        if row.claimed_by and row.claimed_by != staff_id:
            raise TicketError(f"すでに <@{row.claimed_by}> が担当しています。")
        row.claimed_by = staff_id
        row.status = Ticket.STATUS_CLAIMED
        row.last_activity_at = datetime.now(timezone.utc)
        out = Ticket(id=row.id, channel_id=row.channel_id,
                     opener_id=row.opener_id, kind=row.kind,
                     subject=row.subject, status=row.status,
                     claimed_by=row.claimed_by)
    return out


async def touch(channel_id: int) -> None:
    """
    発言があったことを記録する（放置の自動クローズ用）。

    ⚠️ ここは毎メッセージ呼ばれる。重い処理を入れないこと。
       チケットでない場所なら、DBを引かずにすぐ戻る。
    """
    if not is_ticket_channel(channel_id):
        return
    async with session_scope() as s:
        row = (await s.execute(
            select(Ticket).where(
                Ticket.channel_id == channel_id,
                Ticket.status != Ticket.STATUS_CLOSED,
            )
        )).scalar_one_or_none()
        if row is not None:
            row.last_activity_at = datetime.now(timezone.utc)


# ------------------------------------------------------------
#  書き起こし
# ------------------------------------------------------------

async def transcript(
    channel: discord.abc.Messageable, ticket: Ticket, *, limit: int = 2000,
) -> discord.File | None:
    """
    やり取りを1つのテキストにまとめる。

    ⚠️ REST での取得なので MESSAGE CONTENT INTENT は要らない。
       「メッセージ履歴を読む」権限だけ要る。
    """
    lines = [
        f"チケット {ticket.number}",
        f"種別　　{kind_of(ticket.kind)['label']}",
        f"件名　　{ticket.subject or '（なし）'}",
        f"開いた人 {ticket.opener_id}",
        "=" * 60,
        "",
    ]
    try:
        async for m in channel.history(limit=limit, oldest_first=True):
            when = m.created_at.astimezone(config.JST).strftime("%Y-%m-%d %H:%M")
            body = m.content or ""
            if m.attachments:
                body += "".join(f"\n    [添付] {a.filename} {a.url}"
                                for a in m.attachments)
            if m.embeds and not body.strip():
                body = "（埋め込み）"
            lines.append(f"[{when}] {m.author}:")
            for row in (body or "（本文なし）").splitlines() or ["（本文なし）"]:
                lines.append(f"    {row}")
            lines.append("")
    except discord.Forbidden:
        lines.append("※ BOTに「メッセージ履歴を読む」権限が無いため、"
                     "やり取りを書き出せませんでした。")
    except discord.HTTPException as e:
        lines.append(f"※ 書き出しに失敗しました: {e}")

    data = "\n".join(lines).encode("utf-8")
    return discord.File(io.BytesIO(data), filename=f"ticket-{ticket.id:04d}.txt")


# ------------------------------------------------------------
#  閉じる
# ------------------------------------------------------------

async def close(
    bot: discord.Client,
    channel: discord.abc.Messageable,
    *,
    closed_by: int,
    reason: str = "",
) -> Ticket:
    """
    チケットを閉じる。

    書き起こしを記録先へ送ってから、場所を片づける。

    ⚠️ 記録を送るのに失敗しても、閉じる処理は進める。
       閉じられないままだと、同時オープン数の上限に引っかかって
       次の問い合わせができなくなる。
    """
    async with session_scope() as s:
        row = (await s.execute(
            select(Ticket).where(Ticket.channel_id == channel.id)
        )).scalar_one_or_none()
        if row is None:
            raise TicketError("ここはチケットではありません。")
        if row.status == Ticket.STATUS_CLOSED:
            raise TicketError("このチケットはすでに終了しています。")
        row.status = Ticket.STATUS_CLOSED
        row.closed_at = datetime.now(timezone.utc)
        row.closed_by = closed_by
        row.close_reason = (reason or "")[:400] or None
        ticket = Ticket(
            id=row.id, guild_id=row.guild_id, channel_id=row.channel_id,
            is_thread=row.is_thread, opener_id=row.opener_id, kind=row.kind,
            subject=row.subject, status=row.status, claimed_by=row.claimed_by,
            opened_at=row.opened_at, closed_at=row.closed_at,
            closed_by=row.closed_by, close_reason=row.close_reason,
        )

    _open_channels.discard(channel.id)
    await _archive(bot, channel, ticket)
    return ticket


async def _archive(
    bot: discord.Client, channel: discord.abc.Messageable, ticket: Ticket,
) -> None:
    """書き起こしを送って、場所を片づける。"""
    from ui import embeds

    from services.server import logs

    cid = settings.get("ticket_log_channel") or logs.log_channel_id()
    if cid:
        try:
            file = await transcript(channel, ticket)
            target = bot.get_channel(int(cid)) or await bot.fetch_channel(int(cid))
            await target.send(embed=embeds.ticket_closed(ticket), file=file)
        except Exception as e:      # 記録に失敗しても閉じる処理は止めない
            log.warning("チケット %s の記録を残せませんでした: %s",
                        ticket.number, e)

    try:
        if ticket.is_thread:
            # スレッドは消さずに閉じる（あとから読み返せるように）
            await channel.edit(archived=True, locked=True,
                               reason="チケット終了")
        else:
            await channel.delete(reason="チケット終了")
    except discord.Forbidden:
        log.warning("チケット %s の場所を片づけられませんでした（権限不足）",
                    ticket.number)
    except discord.HTTPException as e:
        log.warning("チケット %s の片づけに失敗: %s", ticket.number, e)


# ------------------------------------------------------------
#  放置の自動クローズ
# ------------------------------------------------------------

async def stale(guild_id: int | None = None) -> list[Ticket]:
    """放置されているチケット。"""
    hours = int(settings.get("ticket_auto_close_hours", 0) or 0)
    if hours <= 0:
        return []
    cut = datetime.now(timezone.utc) - timedelta(hours=hours)
    async with session_scope() as s:
        q = select(Ticket).where(Ticket.status != Ticket.STATUS_CLOSED)
        if guild_id is not None:
            q = q.where(Ticket.guild_id == guild_id)
        rows = list((await s.execute(q)).scalars().all())
    return [r for r in rows if (as_utc(r.last_activity_at) or cut) < cut]


async def close_stale(bot: discord.Client) -> int:
    """
    放置されたチケットを閉じる。閉じた件数を返す。

    ⚠️ 定期処理から呼ぶ。1件失敗しても残りを続けること。
    """
    done = 0
    for t in await stale():
        channel = bot.get_channel(t.channel_id)
        if channel is None:
            # 場所が消えている。記録だけ閉じる。
            async with session_scope() as s:
                row = await s.get(Ticket, t.id)
                if row and row.status != Ticket.STATUS_CLOSED:
                    row.status = Ticket.STATUS_CLOSED
                    row.closed_at = datetime.now(timezone.utc)
                    row.close_reason = "場所が見つからないため終了"
            _open_channels.discard(t.channel_id)
            done += 1
            continue
        try:
            hours = int(settings.get("ticket_auto_close_hours", 0) or 0)
            await channel.send(
                f"⏰ {hours}時間お返事がなかったため、この問い合わせを"
                "終了します。続きがあれば、またパネルからお知らせください。"
            )
        except Exception as e:
            # 知らせられなくても閉じる処理は止めない。
            # ⚠️ ただし黙って消さない。ずっと失敗していることに
            #    気付けなくなる。
            log.warning("チケット %s に終了のお知らせを出せませんでした: %s",
                        t.number, e)
        try:
            await close(bot, channel, closed_by=0,
                        reason="お返事がないため自動で終了")
            done += 1
        except Exception as e:
            log.warning("チケット %s の自動終了に失敗: %s", t.number, e)
    if done:
        log.info("放置されたチケットを %d 件終了しました", done)
    return done
