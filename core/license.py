"""
BOTの貸し出し（サーバーごとのライセンス）

このBOTを他のサーバーへ招いて使わせるための仕組み。

  ・**先払い**。期限までは使え、過ぎたら自動で止まる。
  ・止まるのは**そのサーバーだけ**。他のサーバーには影響しない。
  ・持ち主自身のサーバー（ホーム）は期限を持たず、止まらない。

⚠️ **行が無いサーバーでは何も動かない。**
   「知らないサーバーでは既定で使える」にしてはいけない。
   勝手に招待されたサーバーで注文されると、マクドナルドのアカウントと
   カードはこちら持ちなので、そのまま損害になる。

⚠️ 期限の判定は**必ずこの場所で**行う。あちこちで
   `expires_at < now` と書くと、片方だけ直し忘れて穴が開く。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db.models import GuildLicense, LicenseLog, as_utc, utcnow
from db.session import session_scope

log = logging.getLogger("bot.license")

# 期限の何日前に予告するか（多い順）。0 は期限切れの知らせ。
NOTICE_DAYS = (7, 3, 1)

# 止まっている理由
OK = "ok"
NONE = "none"              # そもそも貸していない
EXPIRED = "expired"        # 期限切れ
SUSPENDED = "suspended"    # 手で止めている

MAX_DAYS = 3650            # 1回に延ばせる上限（約10年）


@dataclass(frozen=True)
class Status:
    """そのサーバーが使える状態か。"""
    allowed: bool
    reason: str
    expires_at: datetime | None = None
    is_home: bool = False
    suspend_reason: str = ""

    @property
    def days_left(self) -> int | None:
        """残り日数。期限なしなら None。切れていれば 0 以下。"""
        if self.expires_at is None:
            return None
        delta = self.expires_at - utcnow()
        # ⚠️ 切り上げる。あと数時間でも「残り1日」と見せる。
        #    切り捨てると、期限日の朝に「残り0日」と出て驚かせる。
        return -(-delta.total_seconds() // 86400).__int__()

    def message(self) -> str:
        """利用者に見せる、止まっている理由。"""
        if self.allowed:
            return ""
        if self.reason == NONE:
            return (
                "このサーバーでは、このBOTはまだ使えません。\n"
                "ご利用には貸し出しの手続きが必要です。"
            )
        if self.reason == SUSPENDED:
            base = "このサーバーでの利用は、現在停止されています。"
            return f"{base}\n理由: {self.suspend_reason}" if self.suspend_reason else base
        when = ""
        if self.expires_at:
            when = f"（期限: {self.expires_at.astimezone(config.JST):%Y/%m/%d %H:%M}）"
        return (
            f"このサーバーでの利用期限が切れています。{when}\n"
            "引き続きご利用になる場合は、期限の延長をご依頼ください。"
        )


def _status_of(row: GuildLicense | None) -> Status:
    """行から状態を判定する。**ここだけが判定の正解。**"""
    if row is None:
        return Status(allowed=False, reason=NONE)
    if row.suspended:
        return Status(
            allowed=False, reason=SUSPENDED, expires_at=as_utc(row.expires_at),
            is_home=row.is_home, suspend_reason=row.suspend_reason,
        )
    if row.is_home:
        # ⚠️ ホームは期限を見ない。持ち主のサーバーが止まると、
        #    延長する手段ごと失われる。
        return Status(allowed=True, reason=OK, expires_at=None, is_home=True)
    exp = as_utc(row.expires_at)
    if exp is None:
        # ホーム以外の「期限なし」は、付け方を誤った行。止めずに通すと
        # 永久に無料で使われるので、貸していない扱いにする。
        log.warning("ホームでないのに期限がありません: guild=%s", row.guild_id)
        return Status(allowed=False, reason=NONE)
    if exp <= utcnow():
        return Status(allowed=False, reason=EXPIRED, expires_at=exp)
    return Status(allowed=True, reason=OK, expires_at=exp)


async def status(guild_id: int | None) -> Status:
    """そのサーバーが使える状態か。

    ⚠️ guild_id が None（DM）は使えない扱いにする。どのサーバーの
       ものとして扱うか決められないため。残高も設定もサーバーごとに
       分かれているので、DMでは誰の残高か定まらない。
    """
    if not guild_id:
        return Status(allowed=False, reason=NONE)
    async with session_scope() as s:
        row = await s.get(GuildLicense, int(guild_id))
        return _status_of(row)


async def allowed(guild_id: int | None) -> bool:
    return (await status(guild_id)).allowed


async def _log(
    s: AsyncSession, guild_id: int, action: str, *,
    days: int = 0, expires_at: datetime | None = None,
    actor_id: int | None = None, detail: str = "",
) -> None:
    s.add(LicenseLog(
        guild_id=guild_id, action=action, days=days, expires_at=expires_at,
        actor_id=actor_id, detail=detail[:2000],
    ))


class LicenseError(Exception):
    pass


async def grant(
    guild_id: int, days: int, *, guild_name: str = "",
    actor_id: int | None = None, contact_id: int | None = None,
    note: str = "",
) -> Status:
    """貸す（新規）。すでにあれば期限を**置き換える**。

    ⚠️ 延長は extend() を使うこと。grant は置き換えなので、
       うっかり使うと残りの期間が消える。
    """
    if days <= 0:
        raise LicenseError("日数は1以上にしてください。")
    if days > MAX_DAYS:
        raise LicenseError(f"一度に指定できるのは {MAX_DAYS} 日までです。")

    async with session_scope() as s:
        row = await s.get(GuildLicense, int(guild_id))
        exp = utcnow() + timedelta(days=days)
        if row is None:
            row = GuildLicense(guild_id=int(guild_id))
            s.add(row)
        row.guild_name = (guild_name or row.guild_name or "")[:128]
        row.expires_at = exp
        row.granted_at = utcnow()
        row.granted_by = actor_id
        row.suspended = False
        row.suspend_reason = ""
        row.is_home = False
        row.contact_id = contact_id if contact_id is not None else row.contact_id
        if note:
            row.note = note[:2000]
        row.total_days = (row.total_days or 0) + days
        # ⚠️ 予告の印を消す。消さないと、次の期間で予告が一度も出ない。
        row.notified = ""
        await _log(s, int(guild_id), "grant", days=days, expires_at=exp,
                   actor_id=actor_id, detail=note)
        st = _status_of(row)
    log.info("貸し出しました: guild=%s %d日 期限=%s", guild_id, days, exp)
    return st


async def extend(
    guild_id: int, days: int, *, actor_id: int | None = None, note: str = "",
) -> Status:
    """期限を延ばす。

    ⚠️ **残りが無い（切れている）場合は、今から数える。**
       過去の期限に足すと、延ばしたのに切れたままになる。
    """
    if days <= 0:
        raise LicenseError("日数は1以上にしてください。")
    if days > MAX_DAYS:
        raise LicenseError(f"一度に指定できるのは {MAX_DAYS} 日までです。")

    async with session_scope() as s:
        row = await s.get(GuildLicense, int(guild_id))
        if row is None:
            raise LicenseError(
                "このサーバーにはまだ貸し出していません。`/lend grant` をお使いください。"
            )
        if row.is_home:
            raise LicenseError("ホームサーバーには期限がありません。")
        now = utcnow()
        base = as_utc(row.expires_at)
        if base is None or base < now:
            base = now           # 切れていたら今から
        row.expires_at = base + timedelta(days=days)
        row.extend_count = (row.extend_count or 0) + 1
        row.total_days = (row.total_days or 0) + days
        row.notified = ""        # 予告をやり直す
        await _log(s, int(guild_id), "extend", days=days,
                   expires_at=row.expires_at, actor_id=actor_id, detail=note)
        st = _status_of(row)
    log.info("期限を延ばしました: guild=%s +%d日 → %s", guild_id, days, st.expires_at)
    return st


async def revoke(
    guild_id: int, *, actor_id: int | None = None, reason: str = "",
) -> bool:
    """貸し出しを止める（記録は残す）。"""
    async with session_scope() as s:
        row = await s.get(GuildLicense, int(guild_id))
        if row is None:
            return False
        if row.is_home:
            raise LicenseError("ホームサーバーは止められません。")
        row.suspended = True
        row.suspend_reason = reason[:200]
        await _log(s, int(guild_id), "revoke", actor_id=actor_id, detail=reason)
    log.info("貸し出しを止めました: guild=%s（%s）", guild_id, reason or "理由なし")
    return True


async def resume(guild_id: int, *, actor_id: int | None = None) -> bool:
    """手で止めたものを再開する。期限は変えない。"""
    async with session_scope() as s:
        row = await s.get(GuildLicense, int(guild_id))
        if row is None or not row.suspended:
            return False
        row.suspended = False
        row.suspend_reason = ""
        await _log(s, int(guild_id), "resume", actor_id=actor_id)
    return True


async def set_home(guild_id: int, *, actor_id: int | None = None) -> None:
    """ホームサーバー（持ち主自身のサーバー）を決める。

    ⚠️ ホームは**1つだけ**。付け替えると前のホームはただの貸し先に戻る。
       戻った側に期限が無いと即座に止まるので、期限を入れておく。
    """
    async with session_scope() as s:
        rows = (await s.execute(
            select(GuildLicense).where(GuildLicense.is_home.is_(True))
        )).scalars().all()
        for r in rows:
            if r.guild_id == int(guild_id):
                continue
            r.is_home = False
            if r.expires_at is None:
                # 期限なしのまま降格させると「付け方を誤った行」になり止まる。
                # 猶予として30日入れる。黙って止めない。
                r.expires_at = utcnow() + timedelta(days=30)
            await _log(s, r.guild_id, "unhome", actor_id=actor_id,
                       expires_at=as_utc(r.expires_at))

        row = await s.get(GuildLicense, int(guild_id))
        if row is None:
            row = GuildLicense(guild_id=int(guild_id))
            s.add(row)
        row.is_home = True
        row.expires_at = None
        row.suspended = False
        row.suspend_reason = ""
        await _log(s, int(guild_id), "home", actor_id=actor_id)
    log.info("ホームサーバーを設定しました: guild=%s", guild_id)


async def home_guild_id() -> int | None:
    async with session_scope() as s:
        row = (await s.execute(
            select(GuildLicense).where(GuildLicense.is_home.is_(True))
        )).scalars().first()
        return row.guild_id if row else None


async def ensure_home(guild_id: int) -> bool:
    """ホームがまだ無ければ、このサーバーをホームにする。

    ⚠️ 初回起動のための仕掛け。これが無いと、持ち主自身のサーバーでも
       「貸していない」と判定されて `/lend` すら打てず、詰む。
    """
    if await home_guild_id() is not None:
        return False
    await set_home(guild_id)
    log.info("最初のサーバーをホームにしました: guild=%s", guild_id)
    return True


async def all_licenses() -> list[GuildLicense]:
    async with session_scope() as s:
        rows = (await s.execute(
            select(GuildLicense).order_by(
                GuildLicense.is_home.desc(), GuildLicense.expires_at.asc(),
            )
        )).scalars().all()
        # ⚠️ セッションの外で使うので、必要な値を読み出しておく。
        #    遅延読み込みのまま返すと、使う側で DetachedInstanceError になる。
        for r in rows:
            _ = (r.guild_id, r.guild_name, r.is_home, r.expires_at,
                 r.suspended, r.suspend_reason, r.contact_id, r.note,
                 r.extend_count, r.total_days, r.notified)
        s.expunge_all()
        return list(rows)


async def mark_notified(guild_id: int, tag: str) -> bool:
    """予告を送った印を付ける。すでに付いていれば False。

    ⚠️ 「送る前に印を付ける」こと。送ってから付けると、送信の途中で
       落ちたときに同じ予告を何度も送る。
    """
    async with session_scope() as s:
        row = await s.get(GuildLicense, int(guild_id))
        if row is None:
            return False
        seen = {t for t in (row.notified or "").split(",") if t}
        if tag in seen:
            return False
        seen.add(tag)
        row.notified = ",".join(sorted(seen))
        return True


def due_notice(st: Status) -> str | None:
    """いま送るべき予告の印。無ければ None。"""
    if st.is_home or st.expires_at is None:
        return None
    left = st.days_left
    if left is None:
        return None
    if left <= 0:
        return "0"
    # ⚠️ **小さいほうから**探す。大きいほうから探すと、残り2日のときに
    #    「7日前」を返してしまう。7日前はすでに送信済みなので飛ばされ、
    #    **3日前の予告が一度も出ない**。いちばん差し迫った予告を返す。
    for d in sorted(NOTICE_DAYS):
        if left <= d:
            return str(d)
    return None
