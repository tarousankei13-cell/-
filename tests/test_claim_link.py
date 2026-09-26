#!/usr/bin/env python3
"""Kyash 請求リンクによるチャージの統合テスト。

Bot が請求リンクを発行し、利用者が支払い、Bot が履歴と残高で確認して
残高を付与するまでの流れを検証する。Kyash の HTTP は完全にモックし、
実通信は行わない。

対象: 請求リンクの発行と検証 (送金リンクが返った場合の拒否・金額不一致の拒否) /
支払い前後の状態 / 自動確認 (履歴1回の取得で一括突合) / 手動確認ボタン /
二重付与の防止 / 支払い済みリンクの無効化 / 期限切れ / 利用者による取消 /
同時進行の禁止 / レート適用 / 招待報酬の確定。

実行:
    python3 tests/test_claim_link.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_claim_test"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "test_claim.db"
config.SECRET_KEY_PATH = SCRATCH / "secret.key"
config.BACKUP_DIR = SCRATCH / "backups"
config.RANKING_DEBOUNCE_SECONDS = 0.05

import discord  # noqa: E402
import requests  # noqa: E402

import kyash_service  # noqa: E402
import utils  # noqa: E402
from charge_service import ChargeError  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(f"{'  ok  ' if condition else ' FAIL '} {label}")


# ---------------------------------------------------------------------------
# Kyash HTTP モック (請求リンク)
# ---------------------------------------------------------------------------
STATE: dict[str, Any] = {
    "wallet": 10_000,
    "claims": {},        # link_id -> {"uuid","amount","paid","cancelled"}
    "history": [],
    "seq": 0,
    "create_mode": "claim",   # claim / send / wrong_amount / error
    "cancelled": [],
    "history_calls": 0,
}


class FakeResponse:
    def __init__(self, payload: Any = None, text: str = "", status: int = 200) -> None:
        self._payload = payload
        self.text = text
        self.status_code = status

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def pay_claim(link_id: str) -> None:
    """利用者が請求リンクを支払った状態にする。"""
    claim = STATE["claims"][link_id]
    claim["paid"] = True
    STATE["wallet"] += claim["amount"]
    STATE["history"].insert(0, {"linkUuid": claim["uuid"], "amount": claim["amount"]})


def fake_get(url: str, **kwargs: Any) -> FakeResponse:
    if "kyash.me/payments/" in url:
        link_id = url.rsplit("/", 1)[-1]
        claim = STATE["claims"].get(link_id)
        if claim is None or claim["cancelled"]:
            return FakeResponse(text="<html>処理済み</html>")
        if claim["kind"] == "send":
            return FakeResponse(text=(
                f'<div class="amountText text_send">&yen;{claim["amount"]:,}</div>'
                f'<a class="btn_send" data-href-app="kyash://claim/{claim["uuid"]}">受け取る</a>'
            ))
        return FakeResponse(text=(
            f'<div class="amountText text_request">&yen;{claim["amount"]:,}</div>'
            f'<a class="btn_request" data-href-app="kyash://request/u/{claim["uuid"]}">支払う</a>'
        ))
    if url.endswith("/primary_wallet"):
        return FakeResponse({"code": 200, "result": {"data": {
            "uuid": "W", "balance": {"amount": STATE["wallet"],
                                     "amountBreakdown": {"kyashMoney": STATE["wallet"],
                                                         "kyashValue": 0}},
            "pointBalance": {"availableAmount": 0}}}})
    if "/timeline" in url:
        STATE["history_calls"] += 1
        return FakeResponse({"code": 200, "result": {"data": {"timelines": STATE["history"]}}})
    if "/v1/links/" in url:
        return FakeResponse({"code": 200, "result": {"data": {
            "target": {"publicId": "p", "userName": "payer"}}}})
    if url.endswith("/v1/me"):
        return FakeResponse({"code": 200, "result": {"data": {
            "userName": "bot", "imageUrl": "", "lastNameReal": "x", "firstNameReal": "y",
            "phoneNumber": "0", "kyc": True}}})
    raise AssertionError(f"unexpected GET {url}")


def fake_post(url: str, **kwargs: Any) -> FakeResponse:
    if url.endswith("/v1/me/links"):
        if STATE["create_mode"] == "error":
            return FakeResponse({"code": 400, "error": {"message": "発行できません"}})
        payload = kwargs.get("json") or {}
        amount = int(payload.get("amount", 0))
        STATE["seq"] += 1
        link_id = f"CLAIM{STATE['seq']:04d}"
        stored = amount + 1 if STATE["create_mode"] == "wrong_amount" else amount
        STATE["claims"][link_id] = {
            "uuid": f"claim-uuid-{STATE['seq']}",
            "amount": stored,
            "paid": False,
            "cancelled": False,
            "kind": "send" if STATE["create_mode"] == "send" else "claim",
        }
        return FakeResponse({"code": 200, "result": {"data": {
            "link": f"https://kyash.me/payments/{link_id}"}}})
    raise AssertionError(f"unexpected POST {url}")


def fake_delete(url: str, **kwargs: Any) -> FakeResponse:
    link_uuid = url.rsplit("/", 1)[-1]
    STATE["cancelled"].append(link_uuid)
    for claim in STATE["claims"].values():
        if claim["uuid"] == link_uuid:
            claim["cancelled"] = True
    return FakeResponse({"code": 200, "result": {"data": {"ok": True}}})


def fake_put(url: str, **kwargs: Any) -> FakeResponse:
    raise AssertionError("請求リンク方式では受取API(PUT)を呼ばない")


requests.get = fake_get          # type: ignore[assignment]
requests.post = fake_post        # type: ignore[assignment]
requests.delete = fake_delete    # type: ignore[assignment]
requests.put = fake_put          # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Discord スタブ
# ---------------------------------------------------------------------------
class StubRole:
    def __init__(self, role_id: int, name: str, position: int = 5) -> None:
        self.id = role_id
        self.name = name
        self.position = position
        self.managed = False
        self.mention = f"<@&{role_id}>"

    def is_default(self) -> bool:
        return self.position == 0

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StubRole) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)


class StubPermissions:
    def __getattr__(self, name: str) -> bool:
        return True


class StubMember:
    def __init__(self, user_id: int, guild: "StubGuild",
                 roles: list[StubRole] | None = None) -> None:
        self.id = user_id
        self.guild = guild
        self.bot = False
        self.name = f"user{user_id}"
        self.display_name = self.name
        self.mention = f"<@{user_id}>"
        self.created_at = datetime.now(timezone.utc) - timedelta(days=200)
        self.joined_at = datetime.now(timezone.utc)
        self.roles = roles or []


class StubChannel:
    def __init__(self, channel_id: int, guild: "StubGuild") -> None:
        self.id = channel_id
        self.guild = guild
        self.mention = f"<#{channel_id}>"
        self.sent: list[discord.Embed] = []

    def permissions_for(self, member: object) -> StubPermissions:
        return StubPermissions()

    async def send(self, *, embed: discord.Embed | None = None, **kw: Any) -> Any:
        if embed is not None:
            self.sent.append(embed)
        return type("M", (), {"id": 1, "channel": self})()


class StubGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.name = "テストサーバー"
        self.icon = None
        self.owner_id = 1
        self.members: dict[int, StubMember] = {}
        self.roles: dict[int, StubRole] = {}
        self.me = StubMember(999_999, self)
        self.me.guild_permissions = StubPermissions()  # type: ignore[attr-defined]
        self.channel = StubChannel(500_001, self)
        self.text_channels = [self.channel]

    def get_member(self, user_id: int) -> StubMember | None:
        return self.members.get(user_id)

    def get_role(self, role_id: int) -> StubRole | None:
        return self.roles.get(role_id)

    def get_channel(self, channel_id: int) -> StubChannel | None:
        return self.channel if channel_id == self.channel.id else None

    def add_member(self, m: StubMember) -> StubMember:
        self.members[m.id] = m
        return m

    def add_role(self, r: StubRole) -> StubRole:
        self.roles[r.id] = r
        return r


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    import main as main_module

    guild = StubGuild(31_000)

    class TestBot(main_module.ChargeBot):
        def __init__(self) -> None:
            super().__init__()
            self.owner_alerts: list[str] = []
            self.dms: list[tuple[int, str]] = []

        def get_guild(self, guild_id: int):  # type: ignore[override]
            return guild if guild_id == guild.id else None

        def get_user(self, user_id: int):  # type: ignore[override]
            return guild.get_member(user_id)

        @property
        def guilds(self):  # type: ignore[override]
            return [guild]

        async def alert_owner(self, message: str) -> None:
            self.owner_alerts.append(message)

    bot = TestBot()

    async def fake_send_dm(user_id, embed, *, tx_id=None, queue_on_failure=True):
        bot.dms.append((user_id, embed.title or ""))
        return True

    bot.charge._send_dm = fake_send_dm  # type: ignore[assignment]

    async def fake_resolve_channel(guild_id, channel_id, setting_name):
        return guild.channel

    bot.charge._resolve_channel = fake_resolve_channel  # type: ignore[assignment]

    await bot.db.connect()
    G = guild.id
    await bot.db.set_guild_permission(G, "ALLOWED", 1)
    await bot.db.update_settings(
        G, charge_rate="130", minimum_charge=100, maximum_charge=50_000, daily_limit=0,
        achievement_channel_id=guild.channel.id, log_channel_id=guild.channel.id,
    )
    client = kyash_service.Kyash(access_token="t")
    bot.kyash._client = client
    bot.kyash._status = config.KyashAccountStatus.ACTIVE

    member = guild.add_member(StubMember(41_001, guild))
    await bot.db.ensure_user(G, member.id)

    print("\n=== 1. 請求リンクの発行 ===")
    quote = await bot.charge.start_claim_charge(G, member.id, "1000")
    tx_id = str(quote["tx_id"])
    link_id = quote["url"].rsplit("/", 1)[-1]
    check(quote["amount"] == 1000 and quote["credited"] == 1300,
          f"金額とレートを確定 ({quote['amount']}円 → {quote['credited']})")
    check(quote["url"].startswith("https://kyash.me/payments/"), "リンク URL を発行した")
    row = await bot.db.get_transaction(tx_id)
    check(row["status"] == config.TxStatus.WAITING_PAYMENT, "支払い待ちの取引を作成")
    check(row["source"] == config.TxSource.KYASH_CLAIM
          and row["provider"] == config.ChargeProvider.KYASH_CLAIM,
          f"source/provider が請求リンク ({row['source']})")
    check(row["link_uuid"] and row["claim_link_id"] == link_id,
          "履歴突合用のUUIDと再表示用のIDを保存")
    check(int(row["wallet_before"]) == 10_000, "発行前の受取残高を記録")

    print("\n=== 2. 支払い前は付与しない ===")
    result = await bot.charge.check_claim_payment(tx_id, user_id=member.id)
    check(result == "PENDING", f"未払いでは PENDING ({result})")
    check(await bot.db.get_balance(G, member.id) == 0, "未払いで残高は増えない")
    try:
        await bot.charge.check_claim_payment(tx_id, user_id=99_999)
        check(False, "他人が確認できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.NOT_ALLOWED, f"他人の確認を拒否 ({exc.code})")

    print("\n=== 3. 同時進行の禁止 ===")
    try:
        await bot.charge.start_claim_charge(G, member.id, "2000")
        check(False, "同時に2件発行できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.ACTIVE_TRANSACTION_EXISTS,
              f"進行中の取引があるため拒否 ({exc.code})")

    print("\n=== 4. 支払い → 手動確認で付与 ===")
    pay_claim(link_id)
    result = await bot.charge.check_claim_payment(tx_id, user_id=member.id)
    check(result == "CREDITED", f"支払いを確認して付与 ({result})")
    check(await bot.db.get_balance(G, member.id) == 1300,
          f"残高に反映 ({await bot.db.get_balance(G, member.id)})")
    row = await bot.db.get_transaction(tx_id)
    check(row["status"] == config.TxStatus.COMPLETED, "取引が COMPLETED")
    check(int(row["received_amount"]) == 1000, "受取額を記録")
    check(str(row["link_uuid"]) in STATE["cancelled"],
          "支払い済みのリンクを無効化した (再利用防止)")
    check(any("チャージ実績" in (e.title or "") for e in guild.channel.sent),
          "実績チャンネルへ投稿された")
    check(any(uid == member.id for uid, _ in bot.dms), "利用者へ DM を送った")

    print("\n=== 5. 他人の支払いを自分のものと誤認しない ===")
    # 2人が同時に請求リンクを持ち、片方だけが支払った状態を作る
    a = guild.add_member(StubMember(41_010, guild))
    b = guild.add_member(StubMember(41_011, guild))
    await bot.db.ensure_user(G, a.id)
    await bot.db.ensure_user(G, b.id)
    qa = await bot.charge.start_claim_charge(G, a.id, "1000")
    qb = await bot.charge.start_claim_charge(G, b.id, "1000")
    pay_claim(qa["url"].rsplit("/", 1)[-1])          # A だけが支払う
    res_b = await bot.charge.check_claim_payment(str(qb["tx_id"]), user_id=b.id)
    check(res_b == "PENDING",
          f"未払いのBは、Aの支払いで残高が増えても PENDING ({res_b})")
    check(await bot.db.get_balance(G, b.id) == 0,
          "他人の支払いで残高が発行されない (誤検知なし)")
    res_a = await bot.charge.check_claim_payment(str(qa["tx_id"]), user_id=a.id)
    check(res_a == "CREDITED" and await bot.db.get_balance(G, a.id) == 1300,
          f"支払ったAには正しく付与 ({res_a})")
    await bot.charge.cancel_claim_transaction(str(qb["tx_id"]), b.id)

    print("\n=== 6. 二重付与の防止 ===")
    before = await bot.db.get_balance(G, member.id)
    again = await bot.charge.check_claim_payment(tx_id, user_id=member.id)
    check(again == "DONE", f"処理済みは DONE ({again})")
    check(await bot.db.get_balance(G, member.id) == before, "二重に付与されない")
    # 履歴に同じ支払いが残っていても、自動確認で再付与しない
    credited = await bot.charge.check_waiting_payments()
    check(credited == 0 and await bot.db.get_balance(G, member.id) == before,
          "自動確認でも再付与しない")

    print("\n=== 7. 同時実行でも1回しか付与しない ===")
    conc = guild.add_member(StubMember(41_020, guild))
    await bot.db.ensure_user(G, conc.id)
    qc = await bot.charge.start_claim_charge(G, conc.id, "1000")
    pay_claim(qc["url"].rsplit("/", 1)[-1])
    # 手動確認と自動確認が同時に走る状況を再現する
    results = await asyncio.gather(
        bot.charge.check_claim_payment(str(qc["tx_id"]), user_id=conc.id),
        bot.charge.check_claim_payment(str(qc["tx_id"]), user_id=conc.id),
        bot.charge.check_waiting_payments(),
        bot.charge.check_claim_payment(str(qc["tx_id"]), user_id=conc.id),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, Exception)]
    check(not errors, f"同時実行で例外が出ない ({errors or 'なし'})")
    bal = await bot.db.get_balance(G, conc.id)
    check(bal == 1300, f"同時実行でも付与は1回分だけ ({bal})")
    audit = await bot.db.audit_balance(G, conc.id)
    check(audit["diff"] == 0, f"残高と履歴が一致 (差分 {audit['diff']})")

    print("\n=== 8. 自動確認 (履歴の取得は1回だけ) ===")
    users = [guild.add_member(StubMember(41_100 + i, guild)) for i in range(3)]
    links = []
    for u in users:
        await bot.db.ensure_user(G, u.id)
        q = await bot.charge.start_claim_charge(G, u.id, "1000")
        links.append((str(q["tx_id"]), q["url"].rsplit("/", 1)[-1], u))
    for _tx, lid, _u in links[:2]:
        pay_claim(lid)
    STATE["history_calls"] = 0
    credited = await bot.charge.check_waiting_payments()
    check(credited == 2, f"支払い済みの2件だけ付与 ({credited}件)")
    check(STATE["history_calls"] == 1,
          f"履歴の取得は1回だけ ({STATE['history_calls']}回)")
    for idx, (tx, _lid, u) in enumerate(links):
        bal = await bot.db.get_balance(G, u.id)
        expect = 1300 if idx < 2 else 0
        check(bal == expect, f"利用者{idx}の残高 {bal} (期待 {expect})")

    print("\n=== 9. 利用者による取り消し ===")
    pending_tx, pending_link, pending_user = links[2]
    await bot.charge.cancel_claim_transaction(pending_tx, pending_user.id)
    row = await bot.db.get_transaction(pending_tx)
    check(row["status"] == config.TxStatus.CANCELLED, "取引がキャンセルされた")
    check(STATE["claims"][pending_link]["cancelled"], "リンクも無効化された")
    try:
        await bot.charge.cancel_claim_transaction(pending_tx, pending_user.id)
        check(False, "二重取消ができてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.REQUEST_ALREADY_HANDLED,
              f"二重取消を拒否 ({exc.code})")

    print("\n=== 10. 期限切れ ===")
    expired_user = guild.add_member(StubMember(41_200, guild))
    await bot.db.ensure_user(G, expired_user.id)
    q = await bot.charge.start_claim_charge(G, expired_user.id, "1000")
    expired_tx = str(q["tx_id"])
    expired_link_uuid = str((await bot.db.get_transaction(expired_tx))["link_uuid"])
    await bot.db.execute(
        "UPDATE charge_transactions SET expires_at=? WHERE id=?",
        (utils.now_ts() - 10, expired_tx),
    )
    dms_before = len(bot.dms)
    count = await bot.charge.expire_claim_transactions()
    row = await bot.db.get_transaction(expired_tx)
    check(count == 1 and row["status"] == config.TxStatus.EXPIRED,
          f"期限切れを処理 ({count}件 / {row['status']})")
    check(expired_link_uuid in STATE["cancelled"], "期限切れのリンクを無効化")
    check(len(bot.dms) > dms_before, "期限切れを DM で通知")
    check(await bot.db.get_balance(G, expired_user.id) == 0, "期限切れで残高は増えない")

    print("\n=== 11. 期限切れ直前の支払いは取りこぼさない ===")
    late_user = guild.add_member(StubMember(41_300, guild))
    await bot.db.ensure_user(G, late_user.id)
    q = await bot.charge.start_claim_charge(G, late_user.id, "1000")
    late_tx = str(q["tx_id"])
    pay_claim(q["url"].rsplit("/", 1)[-1])
    await bot.db.execute(
        "UPDATE charge_transactions SET expires_at=? WHERE id=?",
        (utils.now_ts() - 10, late_tx),
    )
    await bot.charge.expire_claim_transactions()
    row = await bot.db.get_transaction(late_tx)
    check(row["status"] == config.TxStatus.COMPLETED,
          f"期限切れ処理でも支払い済みなら付与する ({row['status']})")
    check(await bot.db.get_balance(G, late_user.id) == 1300, "期限直前の支払いを反映")

    print("\n=== 12. 発行時の異常を拒否する ===")
    safe_user = guild.add_member(StubMember(41_400, guild))
    await bot.db.ensure_user(G, safe_user.id)
    for mode, label in (("send", "送金リンクが返ってきた"),
                        ("wrong_amount", "金額が違うリンクが返ってきた"),
                        ("error", "API がエラーを返した")):
        STATE["create_mode"] = mode
        try:
            await bot.charge.start_claim_charge(G, safe_user.id, "1000")
            check(False, f"{label} のに発行が成功してしまう")
        except ChargeError as exc:
            check(exc.code == config.ErrorCode.CLAIM_LINK_FAILED,
                  f"{label} → 拒否 ({exc.code})")
        active = await bot.db.count_active_transactions(G, safe_user.id)
        check(active == 0, f"{label}: 取引を残さない (進行中 {active}件)")
    STATE["create_mode"] = "claim"

    print("\n=== 13. ロール別レートの適用 ===")
    vip = guild.add_role(StubRole(8_001, "VIP", position=10))
    await bot.db.set_role_rate(G, vip.id, Decimal("150"), 10)
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH_CLAIM, charge_rate=Decimal("120"), updated_by=1
    )
    vip_user = guild.add_member(StubMember(41_500, guild, roles=[vip]))
    await bot.db.ensure_user(G, vip_user.id)
    q = await bot.charge.start_claim_charge(G, vip_user.id, "1000")
    check(q["charge_rate"] == Decimal("150") and q["credited"] == 1500,
          f"方式レート120%よりロールの150%を採用 ({q['charge_rate']})")
    plain_user = guild.add_member(StubMember(41_600, guild))
    await bot.db.ensure_user(G, plain_user.id)
    q2 = await bot.charge.start_claim_charge(G, plain_user.id, "1000")
    check(q2["charge_rate"] == Decimal("120"),
          f"ロールなしは方式レート120% ({q2['charge_rate']})")

    print("\n=== 14. 方式の停止 ===")
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH_CLAIM, enabled=False, updated_by=1
    )
    off_user = guild.add_member(StubMember(41_700, guild))
    await bot.db.ensure_user(G, off_user.id)
    try:
        await bot.charge.start_claim_charge(G, off_user.id, "1000")
        check(False, "停止中でも発行できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.PROVIDER_DISABLED, f"停止中を拒否 ({exc.code})")
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH_CLAIM, enabled=True, updated_by=1
    )

    print("\n=== 15. 招待報酬の確定 ===")
    campaign_id = await bot.db.create_campaign(
        guild_id=G, name="招待テスト", inviter_reward=500, invited_reward=300,
        min_account_age_days=0, daily_limit=0, total_limit=0, require_charge=True,
        require_days=0, require_review=False, starts_at=None, ends_at=None, created_by=1,
    )
    inviter = guild.add_member(StubMember(41_800, guild))
    invited = guild.add_member(StubMember(41_801, guild))
    await bot.db.ensure_user(G, inviter.id)
    await bot.db.ensure_user(G, invited.id)
    await bot.db.record_invite(
        guild_id=G, campaign_id=campaign_id, inviter_id=inviter.id, invited_id=invited.id,
        code="c", status=config.InviteStatus.PENDING, reason=None,
    )
    q = await bot.charge.start_claim_charge(G, invited.id, "1000")
    pay_claim(q["url"].rsplit("/", 1)[-1])
    await bot.charge.check_claim_payment(str(q["tx_id"]), user_id=invited.id)
    record = await bot.db.get_invite_record_for_invited(G, invited.id)
    check(record["status"] == config.InviteStatus.CONFIRMED,
          "請求リンクの支払いでも招待が確定する")
    check(await bot.db.get_balance(G, inviter.id) == 500, "招待者へ報酬が入った")

    print("\n=== 16. 整合性 / 秘密情報 ===")
    integrity = await bot.db.integrity_check()
    check(integrity["pragma"] == "ok", "PRAGMA quick_check OK")
    check(not integrity["balance_mismatch"], "残高と履歴合計が一致")
    check(not integrity["negative_balance"], "負の残高がない")
    dump = "\n".join(await bot.db.run(lambda c: list(c.iterdump())))
    check("https://kyash.me" not in dump, "完全なURLは保存されていない")
    check("CLAIM0001" in dump,
          "Bot 自身の請求リンクIDのみ保存 (再表示・無効化のため)")

    await bot.charge.shutdown()
    await bot.price.shutdown()
    await bot.kyash.shutdown()
    await bot.db.close()

    print("\n" + "=" * 70)
    print(f"結果: {len(PASS)} 件成功 / {len(FAIL)} 件失敗")
    for label in FAIL:
        print(f"  ✗ {label}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if FAIL else 0)
