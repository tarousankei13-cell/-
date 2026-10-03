"""
招待キャンペーン

招待した人に残高を渡す。**実際にお金が出ていく**ので、
配りすぎないための歯止めをいくつも重ねてある。

  1. 既定で無効（/campaign enable で管理者が明示的に開ける）
  2. 1人あたりの上限（invite_max_per_user）
  3. キャンペーン全体の上限（invite_budget）
  4. 1人は一度しか招待されない（invites の主キー）
  5. 自分で自分を招待できない
  6. 条件を満たすまで渡さない（既定は「招待された人の初回注文」）
  7. 渡すのは元帳（core/ledger）を通す。残高の出どころが必ず残る

⚠️ 「招待された」判定は、あとから取り消せない。
   先に条件を満たしてから渡すこと。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

import config
from core import ledger as L
from core import settings
from db.models import Invite, User, utcnow
from db.session import session_scope, user_scope

log = logging.getLogger("bot.invite")

# 招待コードに使う文字。
# 読み上げたり書き写したりするので、見間違えるものは入れない。
#   0 と O、1 と I と L は、フォントによってほぼ同じに見える。
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 6


class InviteError(Exception):
    """利用者にそのまま見せてよい、招待の失敗理由。"""


def code_for(discord_id: int) -> str:
    """
    その人の招待コード。discord_id から導き、いつでも同じ値になる。

    ⚠️ discord_id を復元できてはいけないので、ハッシュから作る。
    """
    digest = hashlib.sha256(f"invite:{discord_id}".encode()).digest()
    n = int.from_bytes(digest[:8], "big")
    out = []
    for _ in range(CODE_LENGTH):
        n, r = divmod(n, len(ALPHABET))
        out.append(ALPHABET[r])
    return "".join(out)


# 書き写すときに間違えやすい文字を、正しい文字へ寄せる。
# （コード側は 0 1 I O L を使っていないので、入力されたら読み違い）
_CONFUSABLE = str.maketrans({"0": "O", "O": "Q", "1": "7", "I": "J", "L": "J"})


def normalize(code: str) -> str:
    """
    入力ゆれを吸収する。

    小文字・空白・ハイフンを直したうえで、見間違えやすい文字を寄せる。
    ⚠️ 寄せ先は ALPHABET に入っている文字でなければ意味がない。
    """
    t = (code or "").strip().upper().replace("-", "").replace(" ", "")
    t = "".join(ch for ch in t if ch.isalnum())
    return t.translate(_CONFUSABLE)


def enabled() -> bool:
    return bool(settings.get("invite_enabled", config.INVITE_ENABLED))


def reward_amount() -> int:
    return max(0, int(settings.get("invite_reward", config.INVITE_REWARD)))


def invitee_amount() -> int:
    return max(0, int(settings.get("invite_reward_invitee", config.INVITE_REWARD_INVITEE)))


def condition() -> str:
    return str(settings.get("invite_condition", config.INVITE_CONDITION))


# ============================================================
#  集計
# ============================================================

@dataclass
class Stats:
    total: int = 0            # 紐づいた招待の数
    rewarded: int = 0         # 特典を渡した数
    spent: int = 0            # 渡した合計額
    pending: int = 0          # 条件待ち

    @property
    def budget_left(self) -> int:
        budget = int(settings.get("invite_budget", config.INVITE_BUDGET))
        return max(0, budget - self.spent) if budget > 0 else -1   # -1 は無制限


async def stats() -> Stats:
    async with session_scope() as s:
        total = (await s.execute(select(func.count()).select_from(Invite))).scalar() or 0
        rewarded = (
            await s.execute(
                select(func.count()).select_from(Invite).where(Invite.rewarded.is_(True))
            )
        ).scalar() or 0
        spent = (
            await s.execute(select(func.coalesce(func.sum(Invite.reward_amount), 0)))
        ).scalar() or 0
    return Stats(
        total=int(total), rewarded=int(rewarded), spent=int(spent),
        pending=int(total) - int(rewarded),
    )


async def count_for(inviter_id: int, *, rewarded_only: bool = False) -> int:
    async with session_scope() as s:
        q = select(func.count()).select_from(Invite).where(
            Invite.inviter_id == inviter_id
        )
        if rewarded_only:
            q = q.where(Invite.rewarded.is_(True))
        return int((await s.execute(q)).scalar() or 0)


async def ranking(limit: int = 10) -> list[tuple[int, int]]:
    """(招待した人のID, 成立した数) を多い順に。"""
    async with session_scope() as s:
        rows = (
            await s.execute(
                select(Invite.inviter_id, func.count().label("n"))
                .where(Invite.rewarded.is_(True))
                .group_by(Invite.inviter_id)
                .order_by(func.count().desc())
                .limit(limit)
            )
        ).all()
    return [(int(r[0]), int(r[1])) for r in rows]


# ============================================================
#  紐づけ
# ============================================================

async def ensure_code(discord_id: int) -> str:
    """
    その人の招待コードを用意して返す。

    code_for() は discord_id から決まるので毎回同じだが、
    **コードから人を引く**ために列にも入れておく。
    全員ぶんハッシュを計算して突き合わせるのは、人が増えると重い。
    """
    code = code_for(discord_id)
    async with session_scope() as s:
        row = await s.get(User, discord_id)
        if row is not None and row.invite_code != code:
            row.invite_code = code
    return code


async def find_inviter(code: str) -> int | None:
    """招待コードから、招待した人を探す。"""
    wanted = normalize(code)
    if len(wanted) != CODE_LENGTH:
        return None
    async with session_scope() as s:
        uid = (
            await s.execute(
                select(User.discord_id).where(User.invite_code == wanted)
            )
        ).scalars().first()
    return int(uid) if uid is not None else None


async def link(discord_id: int, inviter_id: int, *, source: str = "code") -> Invite:
    """
    招待された人を、招待した人に紐づける。

    特典はここでは渡さない。条件を満たしたときに `grant_if_ready` が渡す。
    """
    if not enabled():
        raise InviteError("いまは招待キャンペーンを行っていません。")
    if discord_id == inviter_id:
        raise InviteError("ご自身の招待コードは使えません。")

    async with session_scope() as s:
        if await s.get(Invite, discord_id) is not None:
            raise InviteError("すでに招待コードを登録済みです。")

        inviter = await s.get(User, inviter_id)
        if inviter is None:
            raise InviteError("その招待コードは見つかりませんでした。")
        if inviter.is_banned:
            raise InviteError("その招待コードは使えません。")

        # 招待した人の上限
        limit = int(settings.get("invite_max_per_user", config.INVITE_MAX_PER_USER))
        if limit > 0:
            n = int(
                (
                    await s.execute(
                        select(func.count()).select_from(Invite)
                        .where(Invite.inviter_id == inviter_id)
                    )
                ).scalar() or 0
            )
            if n >= limit:
                raise InviteError(
                    "その方の招待はすでに上限に達しています。"
                )

        row = Invite(
            discord_id=discord_id, inviter_id=inviter_id, source=source,
            rewarded=False, reward_amount=0,
        )
        s.add(row)

        me = await s.get(User, discord_id)
        if me is not None:
            me.invited_by = inviter_id
        return row


async def grant_if_ready(discord_id: int) -> list[tuple[int, int]]:
    """
    条件を満たしていれば特典を渡す。渡した (相手のID, 金額) の一覧を返す。

    注文が成立したあとと、紐づけた直後（条件が join のとき）に呼ぶ。
    何度呼んでも二重には渡さない。
    """
    if not enabled():
        return []

    async with session_scope() as s:
        row = await s.get(Invite, discord_id)
        if row is None or row.rewarded:
            return []
        inviter_id = row.inviter_id

        if condition() == "first_order":
            me = await s.get(User, discord_id)
            if me is None or (me.total_orders or 0) < 1:
                return []   # まだ注文していない

    inviter_pay = reward_amount()
    invitee_pay = invitee_amount()

    # ---- 全体の予算 ----
    st = await stats()
    budget = int(settings.get("invite_budget", config.INVITE_BUDGET))
    if budget > 0 and st.spent + inviter_pay + invitee_pay > budget:
        log.info("招待キャンペーンの予算に達したため、特典を渡しませんでした")
        return []

    paid: list[tuple[int, int]] = []
    try:
        if inviter_pay > 0:
            async with user_scope(inviter_id) as s:
                await L.adjust(s, inviter_id, inviter_pay, memo="招待キャンペーンの特典")
            paid.append((inviter_id, inviter_pay))
        if invitee_pay > 0:
            async with user_scope(discord_id) as s:
                await L.adjust(s, discord_id, invitee_pay, memo="招待キャンペーンの特典")
            paid.append((discord_id, invitee_pay))
    except L.LedgerError:
        log.exception("招待の特典を渡せませんでした")
        return []

    # ⚠️ 渡し終わってから記録する。先に記録して失敗すると、
    #    渡していないのに渡したことになる。
    async with session_scope() as s:
        row = await s.get(Invite, discord_id)
        if row is not None:
            row.rewarded = True
            row.reward_amount = inviter_pay + invitee_pay
            row.rewarded_at = utcnow()
    return paid
