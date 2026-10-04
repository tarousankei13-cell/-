"""
警告と処分

⚠️ **処分は必ず記録に残す。** 誰が・誰を・なぜ・いつ。
   記録が無いと、あとから「不当だ」と言われたときに何も示せない。

⚠️ **本人に理由を伝える。** 黙って消されるのが一番の不満になる。
   キックやBANはDMが届かなくなるため、**処分の前に**送る。

⚠️ Discord の制限
     ・タイムアウトは28日まで
     ・一括削除は14日より古いメッセージを消せない
     ・自分より上のロールの人は処分できない（BOTも同じ）
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from sqlalchemy import select

import config
from core import audit, settings
from db.models import ModWarning
from db.session import session_scope

log = logging.getLogger("bot.server.mod")


class ModError(Exception):
    """管理者にそのまま見せてよい、断る理由。"""


# ------------------------------------------------------------
#  できるかどうかの確認
# ------------------------------------------------------------

def can_act(actor: discord.Member, target: discord.Member) -> str | None:
    """
    処分してよい相手か。駄目な理由があれば返す。

    ⚠️ ここを飛ばすと Discord から 403 が返るだけで、
       管理者には何が起きたか分からない。先に日本語で説明する。
    """
    if actor.id == target.id:
        return "ご自身は処分できません。"
    if target.bot:
        return "BOTは処分できません。"
    guild = target.guild
    if target.id == guild.owner_id:
        return "サーバーの所有者は処分できません。"

    me = guild.me
    if me is not None and target.top_role >= me.top_role:
        return (
            f"{target.display_name} さんのロールがBOTより上にあるため操作できません。\n"
            "サーバー設定で、BOTのロールを相手より**上**に移動してください。"
        )
    perms = getattr(actor, "guild_permissions", None)
    if perms is not None and not perms.administrator:
        if target.top_role >= actor.top_role:
            return "ご自身と同じか、上のロールの方は処分できません。"
    return None


# ------------------------------------------------------------
#  本人への連絡
# ------------------------------------------------------------

async def notify(
    user: discord.abc.User, *, guild_name: str, action: str,
    reason: str, extra: str = "",
) -> bool:
    """
    処分の理由をDMで伝える。届いたら True。

    ⚠️ キック・BANの**前に**呼ぶこと。あとだと共通のサーバーが無くなり、
       DMを送れなくなる。
    ⚠️ DMを閉じている人には届かない。届かなくても処分は進める。
    """
    if not settings.get("mod_dm_on_action", True):
        return False
    from ui import embeds

    body = (
        f"**{guild_name}** での対応をお知らせします。\n\n"
        f"**内容**　{action}\n"
        f"**理由**　{reason or '（記載なし）'}"
    )
    if extra:
        body += f"\n\n{extra}"
    try:
        await user.send(embed=embeds.warn(body, title="サーバーからのお知らせ"))
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False


# ------------------------------------------------------------
#  警告
# ------------------------------------------------------------

async def active_warnings(guild_id: int, user_id: int) -> list[ModWarning]:
    """取り消されていない警告。"""
    async with session_scope() as s:
        return list((await s.execute(
            select(ModWarning).where(
                ModWarning.guild_id == guild_id,
                ModWarning.user_id == user_id,
                ModWarning.cleared_at.is_(None),
            ).order_by(ModWarning.id)
        )).scalars().all())


async def all_warnings(guild_id: int, user_id: int) -> list[ModWarning]:
    async with session_scope() as s:
        return list((await s.execute(
            select(ModWarning).where(
                ModWarning.guild_id == guild_id,
                ModWarning.user_id == user_id,
            ).order_by(ModWarning.id)
        )).scalars().all())


async def add_warning(
    guild_id: int, user_id: int, moderator_id: int, reason: str,
) -> tuple[int, int]:
    """警告を1件足す。（記録ID, たまっている件数）を返す。"""
    async with session_scope() as s:
        row = ModWarning(
            guild_id=guild_id, user_id=user_id,
            moderator_id=moderator_id, reason=(reason or "")[:400],
        )
        s.add(row)
        await s.flush()
        wid = row.id
    count = len(await active_warnings(guild_id, user_id))
    await audit.record(
        actor_id=moderator_id, action="mod.warn", target=str(user_id),
        reason=reason, detail={"warning_id": wid, "count": count},
    )
    return wid, count


async def clear_warning(warning_id: int, by: int) -> bool:
    async with session_scope() as s:
        row = await s.get(ModWarning, warning_id)
        if row is None or row.cleared_at is not None:
            return False
        row.cleared_at = datetime.now(timezone.utc)
        row.cleared_by = by
        uid = row.user_id
    await audit.record(
        actor_id=by, action="mod.warn_clear", target=str(uid),
        detail={"warning_id": warning_id},
    )
    return True


async def clear_all_warnings(guild_id: int, user_id: int, by: int) -> int:
    rows = await active_warnings(guild_id, user_id)
    async with session_scope() as s:
        for r in rows:
            row = await s.get(ModWarning, r.id)
            if row is not None:
                row.cleared_at = datetime.now(timezone.utc)
                row.cleared_by = by
    if rows:
        await audit.record(
            actor_id=by, action="mod.warn_clear_all", target=str(user_id),
            detail={"count": len(rows)},
        )
    return len(rows)


def escalation(count: int) -> tuple[str, str] | None:
    """
    警告がたまったときの自動処分。(種類, 説明) を返す。

    ⚠️ 強い順に見る。「5回でBAN」と「3回で停止」が両方あるとき、
       5回目でBANになるようにする。
    """
    ban_at = int(settings.get("mod_warn_ban_at", 0) or 0)
    kick_at = int(settings.get("mod_warn_kick_at", 0) or 0)
    to_at = int(settings.get("mod_warn_timeout_at", 0) or 0)
    if ban_at > 0 and count >= ban_at:
        return "ban", f"警告が {count} 回たまったため"
    if kick_at > 0 and count >= kick_at:
        return "kick", f"警告が {count} 回たまったため"
    if to_at > 0 and count >= to_at:
        mins = int(settings.get("mod_warn_timeout_minutes", 60) or 60)
        return "timeout", f"警告が {count} 回たまったため（{mins}分）"
    return None


# ------------------------------------------------------------
#  処分
# ------------------------------------------------------------

async def timeout(
    member: discord.Member, minutes: int, *, reason: str, by: int,
) -> None:
    """発言を止める。"""
    cap = config.MOD_TIMEOUT_MAX_DAYS * 24 * 60
    if minutes <= 0:
        raise ModError("時間は1分以上でご指定ください。")
    if minutes > cap:
        raise ModError(
            f"発言停止は {config.MOD_TIMEOUT_MAX_DAYS} 日（{cap}分）までです"
            "（Discordの制限）。それより長く止めたい場合はキックかBANをお使いください。"
        )
    try:
        await member.timeout(timedelta(minutes=minutes), reason=reason[:400])
    except discord.Forbidden as e:
        raise ModError(
            "発言を止められませんでした。"
            "BOTに「メンバーをタイムアウト」権限が必要です。"
        ) from e
    await audit.record(
        actor_id=by, action="mod.timeout", target=str(member.id),
        reason=reason, detail={"minutes": minutes},
    )


async def untimeout(member: discord.Member, *, by: int) -> None:
    try:
        await member.timeout(None, reason="発言停止の解除")
    except discord.Forbidden as e:
        raise ModError("解除できませんでした（権限不足）。") from e
    await audit.record(
        actor_id=by, action="mod.untimeout", target=str(member.id),
    )


async def kick(member: discord.Member, *, reason: str, by: int) -> None:
    try:
        await member.kick(reason=reason[:400])
    except discord.Forbidden as e:
        raise ModError(
            "キックできませんでした。BOTに「メンバーをキック」権限が必要です。"
        ) from e
    await audit.record(
        actor_id=by, action="mod.kick", target=str(member.id), reason=reason,
    )


async def ban(
    guild: discord.Guild, user: discord.abc.User, *,
    reason: str, by: int, delete_days: int = 0,
) -> None:
    try:
        await guild.ban(
            user, reason=reason[:400],
            delete_message_seconds=max(0, min(7, delete_days)) * 86400,
        )
    except discord.Forbidden as e:
        raise ModError(
            "BANできませんでした。BOTに「メンバーをBAN」権限が必要です。"
        ) from e
    await audit.record(
        actor_id=by, action="mod.ban", target=str(user.id), reason=reason,
    )


async def unban(guild: discord.Guild, user_id: int, *, by: int) -> bool:
    try:
        user = await guild.fetch_ban(discord.Object(id=user_id))
    except discord.NotFound:
        return False
    except discord.Forbidden as e:
        raise ModError("BANの一覧を見られませんでした（権限不足）。") from e
    await guild.unban(user.user, reason=f"解除（{by}）")
    await audit.record(actor_id=by, action="mod.unban", target=str(user_id))
    return True


async def purge(
    channel: discord.TextChannel, count: int, *,
    by: int, user: discord.abc.User | None = None,
) -> int:
    """
    メッセージをまとめて消す。消した件数を返す。

    ⚠️ Discord は **14日より古いメッセージを一括では消せない**。
       古いものが混ざっていると、そこで止まる。件数で正直に返す。
    """
    cap = config.MOD_PURGE_MAX
    if count < 1:
        raise ModError("1件以上でご指定ください。")
    if count > cap:
        raise ModError(f"一度に消せるのは {cap} 件までです。")

    def pick(m: discord.Message) -> bool:
        return user is None or m.author.id == user.id

    cut = datetime.now(timezone.utc) - timedelta(days=14)
    try:
        deleted = await channel.purge(
            limit=count if user is None else max(count * 5, count),
            check=pick, after=cut, reason=f"一括削除（{by}）",
        )
    except discord.Forbidden as e:
        raise ModError(
            "消せませんでした。BOTに「メッセージの管理」権限が必要です。"
        ) from e
    except discord.HTTPException as e:
        raise ModError(f"消せませんでした: {e}") from e

    n = len(deleted)
    await audit.record(
        actor_id=by, action="mod.purge", target=str(channel.id),
        detail={"count": n, "user": user.id if user else None},
    )
    return n


async def slowmode(
    channel: discord.TextChannel, seconds: int, *, by: int,
) -> None:
    """低速モード。0で解除。"""
    if not 0 <= seconds <= 21600:
        raise ModError("0〜21600秒（6時間）の範囲でご指定ください。")
    try:
        await channel.edit(slowmode_delay=seconds, reason=f"低速モード（{by}）")
    except discord.Forbidden as e:
        raise ModError("変えられませんでした（チャンネルの管理権限が必要です）。") from e
    await audit.record(
        actor_id=by, action="mod.slowmode", target=str(channel.id),
        detail={"seconds": seconds},
    )


async def lock(
    channel: discord.TextChannel, *, locked: bool, by: int,
) -> None:
    """
    チャンネルを閉じる／開ける。

    ⚠️ @everyone の「メッセージを送信」だけを変える。
       他の権限は触らない（戻すときに元の設定を壊さないため）。
    """
    ow = channel.overwrites_for(channel.guild.default_role)
    ow.send_messages = False if locked else None
    try:
        await channel.set_permissions(
            channel.guild.default_role, overwrite=ow,
            reason=("封鎖" if locked else "封鎖の解除") + f"（{by}）",
        )
    except discord.Forbidden as e:
        raise ModError("変えられませんでした（チャンネルの管理権限が必要です）。") from e
    await audit.record(
        actor_id=by, action="mod.lock" if locked else "mod.unlock",
        target=str(channel.id),
    )
