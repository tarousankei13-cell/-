"""不正検知。

紹介制度は放っておくと自作自演で残高を刷れてしまうので、
報酬の付与は「招待された人が初注文して実績まで承認された時点」に
遅らせたうえで、ここの判定を通す。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .cards import find_similar
from .store import JST, Store, now_jst

log = logging.getLogger("bot.fraud")


@dataclass
class Finding:
    kind: str
    score: int
    detail: str
    block: bool = False


@dataclass
class Verdict:
    findings: list[Finding] = field(default_factory=list)

    @property
    def score(self) -> int:
        return min(100, sum(f.score for f in self.findings))

    @property
    def blocked(self) -> bool:
        return any(f.block for f in self.findings)

    @property
    def summary(self) -> str:
        if not self.findings:
            return "問題なし"
        return " / ".join(f"{f.kind}({f.score})" for f in self.findings)

    def reasons(self) -> str:
        return "\n".join(f"- {f.detail}" for f in self.findings) or "- なし"


@dataclass
class FraudConfig:
    referral_min_account_age_days: int = 30
    referral_min_guild_age_hours: int = 24
    referral_max_per_user: int = 20
    referral_review_score: int = 40
    multi_account_sender_threshold: int = 2
    image_hash_threshold: int = 6
    order_burst_window_minutes: int = 10
    order_burst_max: int = 3
    order_amount_alert: int = 5000


class FraudEngine:
    def __init__(self, store: Store, config: FraudConfig):
        self.store = store
        self.config = config

    # ------------------------------------------------------------ 紹介

    def _referral_chain(self, user_id: int, depth: int = 10) -> list[int]:
        chain: list[int] = []
        current = user_id
        for _ in range(depth):
            row = self.store.get_user(current)
            if row is None or row["referred_by"] is None:
                break
            current = int(row["referred_by"])
            if current in chain:
                break
            chain.append(current)
        return chain

    def check_referral_claim(
        self,
        inviter_id: int,
        invitee_id: int,
        invitee_account_created: Optional[datetime] = None,
        invitee_joined_at: Optional[datetime] = None,
    ) -> Verdict:
        """紹介コードを使おうとした時点のチェック。block が立ったら拒否する。"""
        verdict = Verdict()
        cfg = self.config

        if inviter_id == invitee_id:
            verdict.findings.append(
                Finding("self_referral", 100, "自分の紹介コードは使用できません", block=True)
            )
            return verdict

        invitee = self.store.get_user(invitee_id)
        if invitee is not None and invitee["referred_by"] is not None:
            verdict.findings.append(
                Finding("already_referred", 100, "既に紹介元が登録されています", block=True)
            )
            return verdict

        # A -> B -> A の循環
        if invitee_id in self._referral_chain(inviter_id):
            verdict.findings.append(
                Finding("referral_cycle", 100, "紹介関係が循環しています", block=True)
            )
            return verdict

        # 招待者が既に注文していないと紹介できない（捨て垢からの量産を防ぐ）
        inviter = self.store.get_user(inviter_id)
        if inviter is None or int(inviter["total_orders"]) < 1:
            verdict.findings.append(
                Finding(
                    "inviter_no_history",
                    100,
                    "紹介元がまだ一度も注文していません",
                    block=True,
                )
            )
            return verdict

        invited = self.store.count_referred(inviter_id)
        if invited >= cfg.referral_max_per_user:
            verdict.findings.append(
                Finding(
                    "referral_limit",
                    100,
                    f"紹介の上限 {cfg.referral_max_per_user} 人に達しています",
                    block=True,
                )
            )
            return verdict

        now = now_jst()
        if invitee_account_created is not None:
            age_days = (now - invitee_account_created).days
            if age_days < cfg.referral_min_account_age_days:
                verdict.findings.append(
                    Finding(
                        "young_account",
                        50,
                        f"Discord アカウントの作成から {age_days} 日"
                        f"（基準 {cfg.referral_min_account_age_days} 日）",
                    )
                )

        if invitee_joined_at is not None:
            age_hours = (now - invitee_joined_at).total_seconds() / 3600
            if age_hours < cfg.referral_min_guild_age_hours:
                verdict.findings.append(
                    Finding(
                        "fresh_join",
                        30,
                        f"サーバー参加から {age_hours:.1f} 時間"
                        f"（基準 {cfg.referral_min_guild_age_hours} 時間）",
                    )
                )

        shared = self._shared_senders(inviter_id, invitee_id)
        if shared:
            verdict.findings.append(
                Finding(
                    "shared_kyash",
                    60,
                    f"紹介元と同じ Kyash アカウントからチャージしています ({len(shared)} 件一致)",
                )
            )

        return verdict

    def check_referral_payout(self, inviter_id: int, invitee_id: int) -> Verdict:
        """報酬を払う直前の再チェック。"""
        verdict = Verdict()
        invitee = self.store.get_user(invitee_id)
        inviter = self.store.get_user(inviter_id)

        if invitee is None or inviter is None:
            verdict.findings.append(Finding("missing_user", 100, "利用者が見つかりません", block=True))
            return verdict
        if int(invitee["referral_reward_paid"]):
            verdict.findings.append(Finding("already_paid", 100, "報酬は支払い済みです", block=True))
            return verdict
        if int(invitee["banned"]) or int(inviter["banned"]):
            verdict.findings.append(Finding("banned", 100, "利用停止中の利用者が含まれます", block=True))
            return verdict

        shared = self._shared_senders(inviter_id, invitee_id)
        if shared:
            verdict.findings.append(
                Finding("shared_kyash", 60, f"同一 Kyash アカウントからのチャージ ({len(shared)} 件)")
            )

        open_flags = self.store.open_fraud_flags(invitee_id)
        if open_flags:
            verdict.findings.append(
                Finding("open_flags", 40, f"未解決の不正フラグが {len(open_flags)} 件あります")
            )

        return verdict

    def _shared_senders(self, a: int, b: int) -> list[str]:
        return sorted(set(self.store.senders_of_user(a)) & set(self.store.senders_of_user(b)))

    # ------------------------------------------------ 多重アカウント（機能17）

    def check_charge(self, user_id: int, sender_public_id: str, sender_name: str) -> Verdict:
        """チャージ元の Kyash アカウントが複数の Discord ユーザーで共有されていないか。"""
        verdict = Verdict()
        if not sender_public_id:
            return verdict

        others = [u for u in self.store.users_sharing_sender(sender_public_id) if u != user_id]
        if len(others) + 1 > self.config.multi_account_sender_threshold:
            verdict.findings.append(
                Finding(
                    "shared_kyash_sender",
                    70,
                    f"Kyash「{sender_name}」が {len(others) + 1} 人の Discord "
                    f"アカウントにチャージしています: "
                    + ", ".join(f"<@{u}>" for u in ([user_id] + others)[:6]),
                )
            )
        elif others:
            verdict.findings.append(
                Finding(
                    "shared_kyash_sender_minor",
                    25,
                    f"Kyash「{sender_name}」は <@{others[0]}> でも使われています",
                )
            )
        return verdict

    # ------------------------------------------------------- 画像の使い回し

    def check_image(self, user_id: int, phash: str) -> Verdict:
        verdict = Verdict()
        known = self.store.all_image_hashes()
        hit = find_similar(phash, known, threshold=self.config.image_hash_threshold)
        if hit is None:
            return verdict
        _matched, owner_id, distance = hit
        if owner_id == user_id:
            verdict.findings.append(
                Finding(
                    "image_reuse_self",
                    80,
                    f"過去に自分が投稿した画像と一致します (距離 {distance})",
                    block=True,
                )
            )
        else:
            verdict.findings.append(
                Finding(
                    "image_reuse_other",
                    90,
                    f"<@{owner_id}> が投稿した画像と一致します (距離 {distance})",
                    block=True,
                )
            )
        return verdict

    # ------------------------------------------------------------- 注文

    def check_order(self, user_id: int, face_amount: int) -> Verdict:
        verdict = Verdict()
        cfg = self.config

        since = now_jst() - timedelta(minutes=cfg.order_burst_window_minutes)
        recent = self.store.count_orders_since(user_id, since)
        if recent >= cfg.order_burst_max:
            verdict.findings.append(
                Finding(
                    "order_burst",
                    50,
                    f"{cfg.order_burst_window_minutes} 分で {recent} 件の注文",
                )
            )

        if face_amount >= cfg.order_amount_alert:
            verdict.findings.append(
                Finding("large_order", 30, f"定価 {face_amount:,} 円の注文")
            )

        return verdict

    # -------------------------------------------------------------- 記録

    def record(self, user_id: int, verdict: Verdict, context: str = "") -> list[int]:
        ids = []
        for finding in verdict.findings:
            if finding.score < 25:
                continue
            ids.append(
                self.store.add_fraud_flag(
                    user_id, finding.kind, finding.score,
                    f"[{context}] {finding.detail}" if context else finding.detail,
                )
            )
        return ids
