"""利用者負担率の決定。

率は一貫して「利用者が定価の何%を払うか」で扱う。
60 なら利用者が 60% を払い、オーナーが 40% を負担する。
値が小さいほど利用者が得をする向き。

優先順位:
  1. ユーザー個別設定があればそれ（明示指定なので最優先）
  2. なければ min(ロール別の最小, 適用中のキャンペーン, 既定値)
     ＝ 利用者にとって一番有利な率を採用する
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from .store import JST


@dataclass(frozen=True)
class Campaign:
    name: str
    rate: int
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # 月=0 ... 日=6
    start_hour: int = 0
    end_hour: int = 24

    def active_at(self, when: datetime) -> bool:
        if when.weekday() not in self.weekdays:
            return False
        return self.start_hour <= when.hour < self.end_hour


@dataclass
class RateDecision:
    rate: int
    source: str

    @property
    def subsidy(self) -> int:
        return 100 - self.rate


def resolve_rate(
    default_rate: int,
    role_rates: dict[int, int],
    user_rates: dict[int, int],
    campaigns: Iterable[Campaign],
    user_id: int,
    role_ids: Iterable[int],
    when: Optional[datetime] = None,
) -> RateDecision:
    when = when or datetime.now(JST)

    if user_id in user_rates:
        return RateDecision(int(user_rates[user_id]), "ユーザー個別")

    best = RateDecision(int(default_rate), "既定")

    role_ids = set(role_ids)
    matched = [int(v) for k, v in role_rates.items() if int(k) in role_ids]
    if matched:
        lowest = min(matched)
        if lowest < best.rate:
            best = RateDecision(lowest, "ロール別")

    for campaign in campaigns:
        if campaign.active_at(when) and campaign.rate < best.rate:
            best = RateDecision(int(campaign.rate), f"キャンペーン: {campaign.name}")

    return best


def user_pays(face_amount: int, rate: int) -> int:
    """利用者負担額。端数は利用者に有利な側（切り捨て）へ寄せる。"""
    return max(0, (face_amount * int(rate)) // 100)


def active_campaigns(campaigns: Iterable[Campaign], when: Optional[datetime] = None) -> list[Campaign]:
    when = when or datetime.now(JST)
    return [c for c in campaigns if c.active_at(when)]
