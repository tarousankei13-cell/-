"""
入金の照合

Kyash 側の入金履歴と、こちらの記録（kyash_receipts）を突き合わせる。

⚠️ お金を扱う以上、**どちらかにしか無い** 状態を放置してはいけない。

    Kyash にあって こちらに無い … 受け取ったのに記帳していない
                                  → 利用者の残高が足りない
    こちらにあって Kyash に無い … 記帳したのに入金が確認できない
                                  → 二重記帳か、相手側の遅れ

⚠️ ここは **読むだけ**。見つけても自動では直さない。
   お金の帳尻を機械が勝手に合わせるのが一番危ない。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from db.models import KyashAccount, KyashReceipt
from db.session import session_scope

log = logging.getLogger("bot.reconcile")

CREDITED = "CREDITED"
# 記帳まで進んでいない（調べる価値のある）状態
IN_FLIGHT = ("RESERVED", "RECEIVING", "RECEIVED", "MANUAL_REVIEW")


@dataclass
class Report:
    checked: int = 0                       # 突き合わせた件数
    missing_here: list[dict] = field(default_factory=list)   # Kyashのみ
    missing_there: list[dict] = field(default_factory=list)  # こちらのみ
    stuck: list[dict] = field(default_factory=list)          # 途中で止まっている
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.missing_here or self.missing_there or self.stuck)

    def summary(self) -> str:
        if self.errors and not self.checked:
            return "照合できませんでした（" + " / ".join(self.errors[:2]) + "）"
        if self.clean:
            return f"{self.checked} 件を照合し、食い違いはありませんでした。"
        parts = []
        if self.missing_here:
            parts.append(f"記帳もれ **{len(self.missing_here)}** 件")
        if self.missing_there:
            parts.append(f"入金が見つからない **{len(self.missing_there)}** 件")
        if self.stuck:
            parts.append(f"途中で止まっている **{len(self.stuck)}** 件")
        return f"{self.checked} 件を照合し、" + "・".join(parts) + " を見つけました。"


def _amount_of(row: dict) -> int:
    """履歴1件の金額。取り出せなければ0。"""
    for key in ("amount", "price", "value"):
        v = row.get(key)
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, dict):
            for k2 in ("amount", "value"):
                if isinstance(v.get(k2), (int, float)):
                    return int(v[k2])
    return 0


def _is_incoming(row: dict) -> bool:
    """受け取り（入金）か。判断できないものは入金とみなさない。"""
    for key in ("type", "kind", "direction", "timelineType"):
        v = str(row.get(key) or "").upper()
        if not v:
            continue
        if any(w in v for w in ("RECEIV", "INCOMING", "PLUS", "DEPOSIT")):
            return True
        if any(w in v for w in ("SEND", "OUTGOING", "MINUS", "PAY", "WITHDRAW")):
            return False
    return _amount_of(row) > 0


async def check(days: int = 1, limit: int = 100) -> Report:
    """
    直近 days 日ぶんを照合する。

    ⚠️ 履歴は口座ごとに取る。口座が複数あるときは全部見る。
    """
    rep = Report()
    since = datetime.now(timezone.utc) - timedelta(days=days)

    async with session_scope() as s:
        accounts = [
            (a.id, a.label)
            for a in (await s.execute(select(KyashAccount))).scalars().all()
            if a.access_token_enc
        ]
        rows = (
            await s.execute(
                select(KyashReceipt).where(KyashReceipt.created_at >= since)
            )
        ).scalars().all()
        ours = [
            {
                "id": r.id, "amount": int(r.amount or 0), "status": r.status,
                "account_id": r.kyash_account_id, "discord_id": r.discord_id,
                "created_at": r.created_at,
            }
            for r in rows
        ]

    # ① 途中で止まっているもの（相手に聞くまでもない）
    for r in ours:
        if r["status"] in IN_FLIGHT:
            rep.stuck.append(r)

    credited = [r for r in ours if r["status"] == CREDITED]
    rep.checked = len(credited)

    # ② Kyash 側の入金履歴を取る
    from services.kyash import accounts as kyash_accounts

    theirs: list[dict] = []
    for account_id, label in accounts:
        handle = None
        try:
            async with session_scope() as s:
                acc = await s.get(KyashAccount, account_id)
                handle = kyash_accounts.build_client(acc)
            got = await handle.get_history(limit=limit)
            for row in got:
                if _is_incoming(row):
                    theirs.append({"account_id": account_id, "amount": _amount_of(row),
                                   "raw": row})
        except Exception as e:
            rep.errors.append(f"#{account_id} {label}: {str(e)[:60]}")
            log.info("口座 %s の履歴を取れませんでした: %s", account_id, e)
        finally:
            if handle:
                await handle.aclose()

    if rep.errors and not theirs:
        return rep

    # ③ 金額で突き合わせる。
    # ⚠️ 履歴に受取IDは入っていないので、金額と口座でしか照らせない。
    #    同じ額が複数あっても取り違えないよう、1件ずつ消し込む。
    pool: dict[tuple[int, int], int] = {}
    for t in theirs:
        key = (t["account_id"], t["amount"])
        pool[key] = pool.get(key, 0) + 1

    for r in credited:
        key = (r["account_id"], r["amount"])
        if pool.get(key, 0) > 0:
            pool[key] -= 1
        else:
            rep.missing_there.append(r)

    for (account_id, amount), left in pool.items():
        for _ in range(left):
            rep.missing_here.append({"account_id": account_id, "amount": amount})

    return rep
