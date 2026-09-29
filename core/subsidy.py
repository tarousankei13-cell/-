"""
負担率の解決

管理者が負担する割合(%)を決め、利用者の支払額を算出する。

  利用者の支払額 = 定価 × (100 − 負担率) / 100   ※切り上げ
  管理者の負担額 = 定価 − 利用者の支払額

検算: 定価590円・負担率40% → 590 × 60 / 100 = 354 → 利用者 354円 / 負担 236円
      （添付画像の「¥354（定価 ¥590 の 60%）」と一致する）

優先順位:
  1. 利用者ごとの個別設定
  2. ロールごとの設定（複数該当なら priority 最小 → 同値なら負担率が高い方）
  3. 全体の既定値
  ※ 月間の負担上限に達している場合は全体の既定値へ戻す
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import settings
from db.models import Ledger, SubsidyRule

log = logging.getLogger("bot.subsidy")


@dataclass
class Quote:
    """見積もり。"""
    list_price: int        # 定価
    subsidy_rate: float    # 管理者負担率(%)
    user_amount: int       # 利用者の支払額
    subsidy_amount: int    # 管理者の負担額
    source: str            # どのルールが適用されたか
    capped: bool = False   # 月間上限に達して既定値へ戻したか

    @property
    def user_rate(self) -> float:
        """利用者の支払い率(%)。パネルにはこちらを表示する。"""
        return round(100.0 - self.subsidy_rate, 2)

    def describe(self) -> str:
        r = int(self.user_rate) if self.user_rate == int(self.user_rate) else self.user_rate
        return f"定価 ¥{self.list_price:,} の {r}%"


def calculate(list_price: int, subsidy_rate: float) -> tuple[int, int]:
    """
    (利用者の支払額, 管理者の負担額) を返す。

    端数は切り上げ（運営側が損をしないように）。
    """
    if list_price < 0:
        raise ValueError("定価が負の数です")
    rate = max(0.0, min(100.0, float(subsidy_rate)))
    user_amount = math.ceil(list_price * (100.0 - rate) / 100.0)
    user_amount = max(0, min(list_price, user_amount))
    return user_amount, list_price - user_amount


async def monthly_subsidy_used(session: AsyncSession, discord_id: int) -> int:
    """今月その利用者に使った負担額の合計。"""
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    # subsidy_pool からの支出のうち、この利用者の注文に紐づくもの
    from db.models import Order

    result = await session.execute(
        select(func.coalesce(func.sum(-Ledger.amount), 0))
        .select_from(Ledger)
        .join(Order, Order.id == Ledger.order_id)
        .where(
            Ledger.account == "subsidy_pool",
            Ledger.amount < 0,
            Ledger.created_at >= start,
            Order.discord_id == discord_id,
        )
    )
    return int(result.scalar_one() or 0)


async def resolve(
    session: AsyncSession,
    discord_id: int,
    role_ids: list[int],
    list_price: int,
) -> Quote:
    """この利用者・この金額に対する見積もりを出す。"""
    rules = (
        await session.execute(select(SubsidyRule).where(SubsidyRule.enabled.is_(True)))
    ).scalars().all()

    global_rate = float(settings.get("subsidy_rate", 40.0))
    global_cap = settings.get("monthly_subsidy_cap")

    chosen_rate = global_rate
    chosen_cap = global_cap
    source = "全体設定"

    # ① ロール
    role_set = set(role_ids)
    role_rules = [r for r in rules if r.scope == "role" and r.target_id in role_set]
    if role_rules:
        role_rules.sort(key=lambda r: (r.priority, -float(r.subsidy_rate)))
        best = role_rules[0]
        chosen_rate = float(best.subsidy_rate)
        chosen_cap = best.monthly_cap if best.monthly_cap is not None else global_cap
        source = f"ロール設定 (ID {best.target_id})"

    # ② 利用者個別（最優先）
    user_rule = next(
        (r for r in rules if r.scope == "user" and r.target_id == discord_id), None
    )
    if user_rule:
        chosen_rate = float(user_rule.subsidy_rate)
        chosen_cap = user_rule.monthly_cap if user_rule.monthly_cap is not None else global_cap
        source = "個別設定"

    # ③ 月間上限
    capped = False
    if chosen_cap is not None:
        used = await monthly_subsidy_used(session, discord_id)
        _, would_subsidize = calculate(list_price, chosen_rate)
        if used + would_subsidize > int(chosen_cap):
            capped = True
            chosen_rate = global_rate
            source = f"{source} → 月間上限(¥{int(chosen_cap):,})に達したため全体設定"
            # 全体設定でも超える場合は負担なし
            _, g = calculate(list_price, chosen_rate)
            if used + g > int(chosen_cap):
                chosen_rate = 0.0
                source = f"月間上限(¥{int(chosen_cap):,})に達したため負担なし"

    user_amount, subsidy_amount = calculate(list_price, chosen_rate)
    return Quote(
        list_price=list_price,
        subsidy_rate=chosen_rate,
        user_amount=user_amount,
        subsidy_amount=subsidy_amount,
        source=source,
        capped=capped,
    )
