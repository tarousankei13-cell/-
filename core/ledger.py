"""
複式元帳

残高カラムを直接 UPDATE する設計は、同時実行のもとで必ず壊れる。
そこで残高は「元帳の合計」として導出する。

不変条件（これを破る記帳は API レベルで不可能にする）:
    同じ tx_id の amount の合計は必ず 0

勘定科目:
    issuance                系の外から入ってきた金（チャージの相手勘定）
    user:<discord_id>       利用者の利用可能残高
    user:<discord_id>:hold  注文処理中のホールド
    subsidy_pool            管理者負担のプール
    settlement              マクドナルドへ支払った累計

取引パターン（docs/04 §3.2）:
    チャージ ¥1000   issuance −1000 / user:X +1000
    ホールド ¥354    user:X −354    / user:X:hold +354
    解放             user:X:hold −354 / user:X +354
    確定             user:X:hold −354 / settlement +354
    負担 ¥236        subsidy_pool −236 / settlement +236
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.locks import assert_locked
from db.models import Ledger

log = logging.getLogger("bot.ledger")

ISSUANCE = "issuance"
SUBSIDY_POOL = "subsidy_pool"
SETTLEMENT = "settlement"


class LedgerError(Exception):
    pass


class InsufficientBalance(LedgerError):
    def __init__(self, required: int, available: int) -> None:
        self.required = required
        self.available = available
        super().__init__(f"残高不足: 必要 {required}円 / 現在 {available}円")


def user_account(discord_id: int) -> str:
    return f"user:{discord_id}"


def hold_account(discord_id: int) -> str:
    return f"user:{discord_id}:hold"


@dataclass(frozen=True)
class Entry:
    """1件の記帳。account と amount の組。"""
    account: str
    amount: int


async def post(
    session: AsyncSession,
    entries: list[Entry],
    *,
    kind: str,
    order_id: str | None = None,
    receipt_id: str | None = None,
    memo: str | None = None,
    tx_id: str | None = None,
) -> str:
    """
    取引を記帳する。合計が 0 でなければ記帳しない。

    Returns: tx_id
    """
    if not entries:
        raise LedgerError("記帳する項目がありません")

    total = sum(e.amount for e in entries)
    if total != 0:
        raise LedgerError(
            f"貸借が一致しません（合計 {total}円）。"
            f"内訳: {[(e.account, e.amount) for e in entries]}"
        )
    if any(e.amount == 0 for e in entries):
        raise LedgerError("金額0の記帳は受け付けません")

    tx = tx_id or str(uuid.uuid4())
    for e in entries:
        session.add(
            Ledger(
                tx_id=tx, account=e.account, amount=e.amount, kind=kind,
                order_id=order_id, receipt_id=receipt_id, memo=memo,
            )
        )
    await session.flush()
    log.info(
        "記帳 %s tx=%s %s", kind, tx,
        " / ".join(f"{e.account} {e.amount:+d}" for e in entries),
    )
    return tx


async def balance(session: AsyncSession, account: str) -> int:
    """勘定の残高を元帳の合計から求める。"""
    result = await session.execute(
        select(func.coalesce(func.sum(Ledger.amount), 0)).where(Ledger.account == account)
    )
    return int(result.scalar_one())


async def user_balance(session: AsyncSession, discord_id: int) -> int:
    """利用者が今すぐ使える残高（ホールド分は含まない）。"""
    return await balance(session, user_account(discord_id))


async def held_balance(session: AsyncSession, discord_id: int) -> int:
    """注文処理中でホールドされている額。"""
    return await balance(session, hold_account(discord_id))


# ------------------------------------------------------------
#  取引
# ------------------------------------------------------------

async def charge(
    session: AsyncSession, discord_id: int, amount: int, *, receipt_id: str, memo: str | None = None
) -> str:
    """
    チャージ。

    ⚠️ 呼ぶ前に、Kyash 側の受け取りが成功していることを必ず確認すること
    （docs/03 §3.1 の「受取成功 → 残高検証 → 記帳」の順序）。
    """
    assert_locked(discord_id)
    if amount <= 0:
        raise LedgerError("チャージ額は1円以上である必要があります")
    return await post(
        session,
        [Entry(ISSUANCE, -amount), Entry(user_account(discord_id), amount)],
        kind="charge", receipt_id=receipt_id, memo=memo,
    )


async def hold(
    session: AsyncSession, discord_id: int, amount: int, *, order_id: str
) -> str:
    """
    注文のために残高を確保する。

    残高が足りなければ InsufficientBalance を送出し、何も記帳しない。
    """
    assert_locked(discord_id)
    if amount <= 0:
        raise LedgerError("ホールド額は1円以上である必要があります")

    available = await user_balance(session, discord_id)
    if available < amount:
        raise InsufficientBalance(amount, available)

    return await post(
        session,
        [Entry(user_account(discord_id), -amount), Entry(hold_account(discord_id), amount)],
        kind="hold", order_id=order_id,
    )


async def release(
    session: AsyncSession, discord_id: int, amount: int, *, order_id: str, memo: str | None = None
) -> str:
    """注文が失敗したのでホールドを解放して残高に戻す。"""
    assert_locked(discord_id)
    return await post(
        session,
        [Entry(hold_account(discord_id), -amount), Entry(user_account(discord_id), amount)],
        kind="release", order_id=order_id, memo=memo,
    )


async def capture(
    session: AsyncSession, discord_id: int, user_amount: int, subsidy_amount: int, *, order_id: str
) -> str:
    """
    決済が成立したのでホールドを確定する。

    利用者の支払いと管理者の負担を、同じ取引として1つの tx_id で記帳する。
    こうしておくと「定価 = 利用者 + 負担」が元帳の上で常に検証できる。
    """
    assert_locked(discord_id)
    entries = [
        Entry(hold_account(discord_id), -user_amount),
        Entry(SETTLEMENT, user_amount),
    ]
    if subsidy_amount > 0:
        entries += [Entry(SUBSIDY_POOL, -subsidy_amount), Entry(SETTLEMENT, subsidy_amount)]
    return await post(session, entries, kind="capture", order_id=order_id)


async def adjust(
    session: AsyncSession, discord_id: int, amount: int, *, memo: str
) -> str:
    """
    管理者による手動の残高付与・減算。

    amount が正なら付与、負なら減算。減算で残高がマイナスになる場合は拒否する。
    """
    assert_locked(discord_id)
    if amount == 0:
        raise LedgerError("増減額が0です")
    if amount < 0:
        available = await user_balance(session, discord_id)
        if available + amount < 0:
            raise InsufficientBalance(-amount, available)
    return await post(
        session,
        [Entry(ISSUANCE, -amount), Entry(user_account(discord_id), amount)],
        kind="adjust", memo=memo,
    )


# ------------------------------------------------------------
#  検査
# ------------------------------------------------------------

@dataclass
class IntegrityReport:
    checked: int
    broken: list[tuple[str, int]]      # (tx_id, 差額)
    negative: list[tuple[str, int]]    # (勘定, 残高)

    @property
    def ok(self) -> bool:
        return not self.broken and not self.negative


async def verify_integrity(session: AsyncSession) -> IntegrityReport:
    """
    元帳の整合性を検査する。定期実行して、破れていれば管理者へ通知する。

    1. すべての tx_id で合計が 0 か
    2. 利用者の残高がマイナスになっていないか
    """
    rows = await session.execute(
        select(Ledger.tx_id, func.sum(Ledger.amount)).group_by(Ledger.tx_id)
    )
    broken = [(tx, int(total)) for tx, total in rows.all() if int(total) != 0]

    rows = await session.execute(
        select(Ledger.account, func.sum(Ledger.amount))
        .where(Ledger.account.like("user:%"))
        .group_by(Ledger.account)
    )
    negative = [(acc, int(total)) for acc, total in rows.all() if int(total) < 0]

    checked = await session.scalar(select(func.count(func.distinct(Ledger.tx_id)))) or 0
    report = IntegrityReport(checked=int(checked), broken=broken, negative=negative)
    if not report.ok:
        log.error(
            "元帳の整合性が破れています: 不一致 %d件 / マイナス残高 %d件",
            len(broken), len(negative),
        )
    return report


async def outstanding_user_balance(session: AsyncSession) -> int:
    """
    利用者が保有する未使用残高の総額。

    前払式支払手段の届出基準（基準日に1,000万円超）の監視に使う。
    """
    result = await session.execute(
        select(func.coalesce(func.sum(Ledger.amount), 0)).where(Ledger.account.like("user:%"))
    )
    return int(result.scalar_one())
