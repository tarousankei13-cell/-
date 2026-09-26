#!/usr/bin/env python3
"""PayPay / LTC チャージ (申請 → 管理者承認) の統合テスト。

対象: 方式ごとの有効/無効・レート・金額上下限 / 方式別レートとロール別レートの
優先関係 / LTC の価格取得と確定 (外部 API はモック) / 申請の作成・提出・承認・
却下・取消・期限切れ / 二重申請と同時承認の防止 / 審査カードの投稿と更新 /
承認時の残高付与・実績投稿・DM・招待報酬の確定 / 承認権限。

外部 HTTP (CoinGecko) は必ずモックする。実通信は行わない。

実行:
    python3 tests/test_providers.py
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

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_provider_test"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "test_providers.db"
config.SECRET_KEY_PATH = SCRATCH / "secret.key"
config.BACKUP_DIR = SCRATCH / "backups"
config.RANKING_DEBOUNCE_SECONDS = 0.05

import discord  # noqa: E402

import price_service  # noqa: E402
import utils  # noqa: E402
from charge_service import ChargeError  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(f"{'  ok  ' if condition else ' FAIL '} {label}")


# ---------------------------------------------------------------------------
# Discord スタブ
# ---------------------------------------------------------------------------
class _FakeResponse:
    status = 404
    reason = "Not Found"


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


class StubMessage:
    def __init__(self, message_id: int, channel: "StubChannel",
                 embed: discord.Embed | None) -> None:
        self.id = message_id
        self.channel = channel
        self.embeds = [embed] if embed else []
        self.edits: list[discord.Embed] = []
        self.view_cleared = False

    async def edit(self, *, embed: discord.Embed | None = None,
                   view: Any = None, **kwargs: object) -> "StubMessage":
        if embed is not None:
            self.embeds = [embed]
            self.edits.append(embed)
        if view is None:
            self.view_cleared = True
        return self


class StubChannel:
    def __init__(self, channel_id: int, guild: "StubGuild") -> None:
        self.id = channel_id
        self.guild = guild
        self.mention = f"<#{channel_id}>"
        self.sent: list[discord.Embed] = []
        self.messages: dict[int, StubMessage] = {}

    def permissions_for(self, member: object) -> StubPermissions:
        return StubPermissions()

    async def send(self, *, embed: discord.Embed | None = None,
                   view: Any = None, **kwargs: object) -> StubMessage:
        if embed is not None:
            self.sent.append(embed)
        message = StubMessage(900_000 + len(self.messages) + 1, self, embed)
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> StubMessage:
        message = self.messages.get(int(message_id))
        if message is None:
            raise discord.NotFound(_FakeResponse(), "not found")  # type: ignore[arg-type]
        return message


class StubGuild:
    def __init__(self, guild_id: int, name: str = "テストサーバー") -> None:
        self.id = guild_id
        self.name = name
        self.icon = None
        self.owner_id = 1
        self.members: dict[int, StubMember] = {}
        self.roles: dict[int, StubRole] = {}
        self.me = StubMember(999_999, self)
        self.me.guild_permissions = StubPermissions()  # type: ignore[attr-defined]
        self.channel = StubChannel(500_001, self)
        self.review = StubChannel(500_002, self)
        self.text_channels = [self.channel, self.review]

    def get_member(self, user_id: int) -> StubMember | None:
        return self.members.get(user_id)

    def get_role(self, role_id: int) -> StubRole | None:
        return self.roles.get(role_id)

    def get_channel(self, channel_id: int) -> StubChannel | None:
        for ch in self.text_channels:
            if ch.id == channel_id:
                return ch
        return None

    def add_member(self, member: StubMember) -> StubMember:
        self.members[member.id] = member
        return member

    def add_role(self, role: StubRole) -> StubRole:
        self.roles[role.id] = role
        return role


class StubUser:
    """Bot Owner / 一般管理者の判定用。"""

    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.mention = f"<@{user_id}>"


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    import main as main_module

    guild = StubGuild(21_000)
    other_guild = StubGuild(22_000, "別のサーバー")

    class TestBot(main_module.ChargeBot):
        def __init__(self) -> None:
            super().__init__()
            self.owner_alerts: list[str] = []
            self.dms: list[tuple[int, str]] = []

        def get_guild(self, guild_id: int):  # type: ignore[override]
            return {guild.id: guild, other_guild.id: other_guild}.get(guild_id)

        def get_user(self, user_id: int):  # type: ignore[override]
            return guild.get_member(user_id)

        def get_channel(self, channel_id: int):  # type: ignore[override]
            return guild.get_channel(channel_id) or other_guild.get_channel(channel_id)

        @property
        def guilds(self):  # type: ignore[override]
            return [guild, other_guild]

        def is_bot_owner(self, user: object) -> bool:  # type: ignore[override]
            return getattr(user, "id", None) == OWNER_ID

        async def alert_owner(self, message: str) -> None:
            self.owner_alerts.append(message)

    OWNER_ID = 4242
    bot = TestBot()

    async def fake_send_dm(user_id, embed, *, tx_id=None, queue_on_failure=True):
        bot.dms.append((user_id, embed.title or ""))
        return True

    bot.charge._send_dm = fake_send_dm  # type: ignore[assignment]

    async def fake_resolve_channel(guild_id, channel_id, setting_name):
        return guild.get_channel(channel_id) or guild.channel

    bot.charge._resolve_channel = fake_resolve_channel  # type: ignore[assignment]

    async def fake_resolve_global(channel_id):
        return guild.get_channel(int(channel_id))

    bot.charge._resolve_global_channel = fake_resolve_global  # type: ignore[assignment]

    # --- 価格 API のモック (実通信はしない) ---
    price_state: dict[str, Any] = {"payload": {"litecoin": {"jpy": 12_000}}, "fail": None,
                                   "calls": 0}

    def fake_request(self=None):  # noqa: ANN001
        price_state["calls"] += 1
        if price_state["fail"]:
            raise price_service.PriceError(str(price_state["fail"]))
        return price_state["payload"]

    bot.price._request_coingecko = fake_request  # type: ignore[assignment]

    await bot.db.connect()
    G = guild.id
    await bot.db.set_guild_permission(G, "ALLOWED", OWNER_ID)
    await bot.db.set_guild_permission(other_guild.id, "ALLOWED", OWNER_ID)
    await bot.db.update_settings(
        G, charge_rate="130", minimum_charge=100, maximum_charge=50_000,
        daily_limit=0, achievement_channel_id=guild.channel.id,
        log_channel_id=guild.channel.id,
    )
    settings = await bot.db.get_settings(G)
    member = guild.add_member(StubMember(31_001, guild))
    await bot.db.ensure_user(G, member.id)

    print("\n=== 1. 価格取得 (CoinGecko / モック) ===")
    quote = await bot.price.get_price(force=True)
    check(quote.price == Decimal("12000") and quote.source == config.PRICE_SOURCE_COINGECKO,
          f"価格を取得できた ({quote.price} / {quote.source})")
    cached = await bot.price.get_price()
    check(price_state["calls"] == 1 and cached.price == quote.price,
          "キャッシュ内は API を再呼び出ししない")
    check(utils.asset_amount_for(1000, quote.price) == Decimal("0.08333334"),
          "円 → LTC 換算は切り上げ (不足を出さない)")

    # 異常値ガード
    price_state["payload"] = {"litecoin": {"jpy": 100_000}}
    try:
        await bot.price.get_price(force=True)
        check(False, "異常値ガードが働かない")
    except price_service.PriceError as exc:
        check("変動" in exc.detail, f"直近値から乖離した価格を拒否 ({exc.detail[:40]}…)")
    check(any("価格の異常" in m for m in bot.owner_alerts), "価格異常を Owner へ通知した")

    # 手動フォールバック
    price_state["payload"] = {"litecoin": {"jpy": 12_000}}
    price_state["fail"] = "通信エラー (テスト)"
    await bot.price.set_manual_price(Decimal("11000"))
    fallback = await bot.price.get_price(force=True)
    check(fallback.price == Decimal("11000") and fallback.source == config.PRICE_SOURCE_MANUAL
          and fallback.stale,
          f"API 失敗時に手動価格へフォールバック ({fallback.price})")
    await bot.db.set_system_value(price_service.KEY_MANUAL_PRICE, "")
    price_state["fail"] = None
    await bot.price.get_price(force=True)

    print("\n=== 2. 方式の利用可否 ===")
    entries = {e["provider"]: e for e in await bot.charge.provider_availability(G, settings)}
    check(not entries[config.ChargeProvider.PAYPAY]["available"]
          and "入金先" in entries[config.ChargeProvider.PAYPAY]["reason"],
          "入金先が無い方式は使えない")
    try:
        await bot.charge.start_manual_charge(G, member.id, config.ChargeProvider.PAYPAY, "1000")
        check(False, "未設定でも申請できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.PROVIDER_NOT_CONFIGURED,
              f"未設定の方式を拒否 ({exc.code})")

    await bot.db.set_destination(
        config.ChargeProvider.PAYPAY, address="paypay-recv-001", label="受取用",
        note="メモ欄には何も書かないでください", updated_by=OWNER_ID,
    )
    await bot.db.set_destination(
        config.ChargeProvider.LTC, address="ltc1qtestaddress0000000000000000",
        label="受取用", note=None, updated_by=OWNER_ID,
    )
    try:
        await bot.charge.start_manual_charge(G, member.id, config.ChargeProvider.PAYPAY, "1000")
        check(False, "審査チャンネル未設定でも申請できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.REVIEW_CHANNEL_NOT_SET,
              f"審査チャンネル未設定を拒否 ({exc.code})")
    await bot.charge.set_review_channel_id(guild.review.id)
    entries = {e["provider"]: e for e in await bot.charge.provider_availability(G, settings)}
    check(all(entries[p]["available"] for p in config.MANUAL_PROVIDERS),
          "入金先と審査チャンネルが揃うと使える")

    await bot.db.set_provider_settings(G, config.ChargeProvider.PAYPAY, enabled=False,
                                       updated_by=OWNER_ID)
    try:
        await bot.charge.start_manual_charge(G, member.id, config.ChargeProvider.PAYPAY, "1000")
        check(False, "停止中でも申請できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.PROVIDER_DISABLED, f"停止中を拒否 ({exc.code})")
    await bot.db.set_provider_settings(G, config.ChargeProvider.PAYPAY, enabled=True,
                                       updated_by=OWNER_ID)

    print("\n=== 3. 方式別レートとロール別レートの優先関係 ===")
    await bot.db.set_provider_settings(G, config.ChargeProvider.PAYPAY,
                                       charge_rate=Decimal("120"), updated_by=OWNER_ID)
    await bot.db.set_provider_settings(G, config.ChargeProvider.LTC,
                                       charge_rate=Decimal("200"), updated_by=OWNER_ID)
    rate, role = await bot.charge.resolve_provider_rate(
        G, member.id, config.ChargeProvider.PAYPAY, settings)
    check(rate == Decimal("120") and role is None, f"方式別レートが既定を置き換える ({rate})")
    rate, _ = await bot.charge.resolve_provider_rate(
        G, member.id, config.ChargeProvider.KYASH, settings)
    check(rate == Decimal("130"), f"未設定の方式はサーバー既定 ({rate})")

    vip = guild.add_role(StubRole(8_001, "VIP", position=10))
    member.roles.append(vip)
    await bot.db.set_role_rate(G, vip.id, Decimal("150"), 10)
    rate, role = await bot.charge.resolve_provider_rate(
        G, member.id, config.ChargeProvider.PAYPAY, settings)
    check(rate == Decimal("150") and role == vip.id,
          f"方式レートより高いロールレートを採用 ({rate})")
    rate, role = await bot.charge.resolve_provider_rate(
        G, member.id, config.ChargeProvider.LTC, settings)
    check(rate == Decimal("200") and role is None,
          f"ロールレートより高い方式レートを採用 ({rate})")

    print("\n=== 4. 金額の上下限 (方式ごと) ===")
    await bot.db.set_provider_settings(G, config.ChargeProvider.PAYPAY,
                                       minimum_charge=1_000, maximum_charge=5_000,
                                       updated_by=OWNER_ID)
    low, high = await bot.charge.provider_limits(G, config.ChargeProvider.PAYPAY, settings)
    check((low, high) == (1_000, 5_000), f"方式別の上下限が効く ({low}〜{high})")
    for amount, expect in (("500", config.ErrorCode.AMOUNT_BELOW_MIN),
                           ("9000", config.ErrorCode.AMOUNT_ABOVE_MAX)):
        try:
            await bot.charge.start_manual_charge(
                G, member.id, config.ChargeProvider.PAYPAY, amount)
            check(False, f"{amount} 円が通ってしまう")
        except ChargeError as exc:
            check(exc.code == expect, f"{amount} 円を拒否 ({exc.code})")
    await bot.db.set_provider_settings(G, config.ChargeProvider.PAYPAY, clear_limits=True,
                                       updated_by=OWNER_ID)

    print("\n=== 5. レート制限 (外部 API を守る) ===")
    limited = False
    spammer = guild.add_member(StubMember(31_900, guild))
    await bot.db.ensure_user(G, spammer.id)
    for _ in range(config.RATE_LIMIT_CHARGE_COUNT + 3):
        try:
            await bot.charge.start_manual_charge(
                G, spammer.id, config.ChargeProvider.PAYPAY, "1000")
        except ChargeError as exc:
            if exc.code == config.ErrorCode.RATE_LIMITED:
                limited = True
                break
    check(limited, "連続申請をレート制限で止める")
    check(price_state["calls"] <= 6,
          f"レート制限により価格 API の呼び出しが抑えられている ({price_state['calls']}回)")
    # 以降のテストでは制限を緩める (制限そのものは上で確認済み)
    bot.charge._charge_rate_limiter = utils.RateLimiter(10_000, 1)
    for row, _t in [(r, 0) for r in (await bot.db.list_requests(
            guild_id=G, user_id=spammer.id, limit=50))[0]]:
        await bot.db.cancel_request(int(row["id"]))

    print("\n=== 6. PayPay: 申請 → 承認 ===")
    quote = await bot.charge.start_manual_charge(
        G, member.id, config.ChargeProvider.PAYPAY, "1000")
    request_id = int(quote["request_id"])
    check(quote["charge_rate"] == Decimal("150") and quote["estimated_credit"] == 1500,
          f"申請時にレートを確定 ({quote['charge_rate']} → {quote['estimated_credit']})")
    check(quote["destination"]["address"] == "paypay-recv-001", "入金先を案内する")
    row = await bot.db.get_request(request_id)
    check(row["status"] == config.RequestStatus.QUOTED, "作成直後は送金待ち")

    try:
        await bot.charge.submit_request(request_id, member.id, "!!!不正な形式!!!")
        check(False, "不正な取引IDが通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.INVALID_PROOF, f"不正な取引IDを拒否 ({exc.code})")

    result = await bot.charge.submit_request(request_id, member.id, "pp-tx-0001")
    check(result["proof_ref"] == "PP-TX-0001", f"取引IDを正規化 ({result['proof_ref']})")
    row = await bot.db.get_request(request_id)
    check(row["status"] == config.RequestStatus.PENDING, "申請後は承認待ち")
    cards = [e for e in guild.review.sent if "チャージ申請" in (e.title or "")]
    check(len(cards) == 1, f"審査チャンネルへカードを投稿 ({len(cards)}件)")
    check(row["review_message_id"] is not None, "カードのメッセージIDを保存")

    balance_before = await bot.db.get_balance(G, member.id)
    approved = await bot.charge.approve_request(request_id, operator_id=OWNER_ID)
    check(approved["credited_amount"] == 1500, f"付与額 ({approved['credited_amount']})")
    check(await bot.db.get_balance(G, member.id) == balance_before + 1500,
          "残高へ反映された")
    tx = await bot.db.get_transaction(str(approved["transaction_id"]))
    check(tx["status"] == config.TxStatus.COMPLETED
          and tx["source"] == config.TxSource.MANUAL_PAYPAY,
          f"取引が COMPLETED / source={tx['source']}")
    check(any("チャージ実績" in (e.title or "") for e in guild.channel.sent),
          "実績チャンネルへ投稿された")
    check(any(uid == member.id for uid, _ in bot.dms), "利用者へ DM を送った")
    card = guild.review.messages[int(row["review_message_id"])]
    check(len(card.edits) >= 1 and card.view_cleared,
          f"審査カードを更新しボタンを外した (編集{len(card.edits)}回)")

    try:
        await bot.charge.approve_request(request_id, operator_id=OWNER_ID)
        check(False, "二重承認が通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.REQUEST_ALREADY_HANDLED,
              f"二重承認を拒否 ({exc.code})")
    check(await bot.db.get_balance(G, member.id) == balance_before + 1500,
          "二重承認でも残高は増えない")

    print("\n=== 7. 二重申請の防止 ===")
    dup = await bot.charge.start_manual_charge(
        G, member.id, config.ChargeProvider.PAYPAY, "1000")
    try:
        await bot.charge.submit_request(int(dup["request_id"]), member.id, "PP-TX-0001")
        check(False, "同じ取引IDが再利用できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.DUPLICATE_PROOF,
              f"承認済みの取引IDの再利用を拒否 ({exc.code})")
    await bot.charge.cancel_own_request(int(dup["request_id"]), member.id)
    check((await bot.db.get_request(int(dup["request_id"])))["status"]
          == config.RequestStatus.CANCELLED, "利用者が申請を取り消せる")

    print("\n=== 8. LTC: 価格確定 → 申請 → 却下 ===")
    ltc_quote = await bot.charge.start_manual_charge(
        G, member.id, config.ChargeProvider.LTC, "1000")
    check(ltc_quote["asset_amount"] == Decimal("0.08333334"),
          f"送る数量を提示 ({utils.fmt_asset(ltc_quote['asset_amount'])})")
    check(ltc_quote["asset_price"] == Decimal("12000"), "単価を申請に固定した")
    check(ltc_quote["charge_rate"] == Decimal("200")
          and ltc_quote["estimated_credit"] == 2000,
          f"LTC の方式レートを適用 ({ltc_quote['charge_rate']})")

    # 申請後に相場が動いても、確定済みの単価は変わらない
    price_state["payload"] = {"litecoin": {"jpy": 14_000}}
    await bot.price.get_price(force=True)
    stored = await bot.db.get_request(int(ltc_quote["request_id"]))
    check(utils.to_decimal(stored["asset_price"]) == Decimal("12000"),
          "相場が動いても申請の単価は変わらない")

    txid = "ab" * 32
    try:
        await bot.charge.submit_request(int(ltc_quote["request_id"]), member.id, "not-a-txid")
        check(False, "不正な txid が通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.INVALID_PROOF, f"不正な txid を拒否 ({exc.code})")
    submitted = await bot.charge.submit_request(
        int(ltc_quote["request_id"]), member.id,
        f"https://blockchair.com/litecoin/transaction/{txid}", "0.0834",
    )
    check(submitted["proof_ref"] == txid, "エクスプローラ URL から txid を取り出す")
    stored = await bot.db.get_request(int(ltc_quote["request_id"]))
    check(utils.to_decimal(stored["asset_amount"]) == Decimal("0.0834"),
          f"実際に送った数量を記録 ({stored['asset_amount']})")

    before = await bot.db.get_balance(G, member.id)
    rejected = await bot.charge.reject_request(
        int(ltc_quote["request_id"]), operator_id=OWNER_ID, reason="着金が確認できませんでした",
    )
    check(rejected["reason"].startswith("着金"), "却下理由を記録")
    check(await bot.db.get_balance(G, member.id) == before, "却下で残高は動かない")
    check(any("却下" in title for _uid, title in bot.dms), "却下を DM で通知した")

    print("\n=== 9. 金額を直して承認 ===")
    fix = await bot.charge.start_manual_charge(G, member.id, config.ChargeProvider.LTC, "1000")
    await bot.charge.submit_request(int(fix["request_id"]), member.id, "cd" * 32, "0.05")
    before = await bot.db.get_balance(G, member.id)
    fixed = await bot.charge.approve_request(
        int(fix["request_id"]), operator_id=OWNER_ID, credited_amount=1200,
        note="実際の入金が 0.05 LTC だったため",
    )
    check(fixed["credited_amount"] == 1200, "指定した額で承認できる")
    check(await bot.db.get_balance(G, member.id) == before + 1200, "指定額が付与された")
    logs, _ = await bot.db.list_audit_logs(action="REQUEST_APPROVE", limit=5)
    override = [
        row for row in logs
        if utils.json_contains_text(row["detail"], "実際の入金が 0.05 LTC")
    ]
    check(bool(override), "修正の理由が監査ログに残る")

    print("\n=== 10. 承認の権限 ===")
    check(await bot.charge.can_review(G, StubUser(OWNER_ID)), "Bot Owner は承認できる")
    check(not await bot.charge.can_review(G, StubUser(31_002)),
          "既定ではサーバー管理者は承認できない")
    await bot.charge.set_delegated(G, True)
    check(await bot.charge.can_review(G, StubUser(31_002)),
          "委任したサーバーでは承認できる")
    check(not await bot.charge.can_review(other_guild.id, StubUser(31_002)),
          "委任していないサーバーでは承認できない")
    await bot.charge.set_delegated(G, False)
    check(not await bot.charge.can_review(G, StubUser(31_002)), "委任を解除できる")

    print("\n=== 11. 申請の上限と期限切れ ===")
    opened = []
    for _ in range(config.MAX_OPEN_REQUESTS_PER_USER):
        opened.append(await bot.charge.start_manual_charge(
            G, member.id, config.ChargeProvider.PAYPAY, "1000"))
    try:
        await bot.charge.start_manual_charge(G, member.id, config.ChargeProvider.PAYPAY, "1000")
        check(False, "同時申請の上限を超えられる")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.OPEN_REQUEST_LIMIT,
              f"同時申請の上限を拒否 ({exc.code})")

    # 送金待ちのまま期限切れ
    await bot.db.execute(
        "UPDATE charge_requests SET quote_expires_at=? WHERE status=?",
        (utils.now_ts() - 10, config.RequestStatus.QUOTED),
    )
    expired = await bot.charge.expire_stale_requests()
    check(expired >= len(opened), f"送金待ちの期限切れを処理 ({expired}件)")
    statuses = [
        (await bot.db.get_request(int(q["request_id"])))["status"] for q in opened
    ]
    check(all(st == config.RequestStatus.EXPIRED for st in statuses),
          f"期限切れの状態になった ({set(statuses)})")

    # 申請済みのまま放置 → 期限切れ + DM
    stale = await bot.charge.start_manual_charge(
        G, member.id, config.ChargeProvider.PAYPAY, "1000")
    await bot.charge.submit_request(int(stale["request_id"]), member.id, "PP-STALE-01")
    dms_before = len(bot.dms)
    await bot.db.execute(
        "UPDATE charge_requests SET expires_at=? WHERE id=?",
        (utils.now_ts() - 10, int(stale["request_id"])),
    )
    await bot.charge.expire_stale_requests()
    row = await bot.db.get_request(int(stale["request_id"]))
    check(row["status"] == config.RequestStatus.EXPIRED, "承認待ちの期限切れを処理")
    check(len(bot.dms) > dms_before, "期限切れを DM で通知した")
    try:
        await bot.charge.approve_request(int(stale["request_id"]), operator_id=OWNER_ID)
        check(False, "期限切れの申請が承認できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.REQUEST_ALREADY_HANDLED,
              f"期限切れの承認を拒否 ({exc.code})")

    print("\n=== 12. 未処理申請の催促 ===")
    remind = await bot.charge.start_manual_charge(
        G, member.id, config.ChargeProvider.PAYPAY, "1000")
    await bot.charge.submit_request(int(remind["request_id"]), member.id, "PP-REMIND-1")
    alerts_before = len(bot.owner_alerts)
    check(await bot.charge.remind_pending_requests() == 0, "新しい申請では催促しない")
    await bot.db.execute(
        "UPDATE charge_requests SET submitted_at=? WHERE id=?",
        (utils.now_ts() - config.REVIEW_REMIND_SECONDS - 60, int(remind["request_id"])),
    )
    check(await bot.charge.remind_pending_requests() == 1, "滞留した申請を催促する")
    check(len(bot.owner_alerts) > alerts_before, "催促を Owner へ通知した")

    print("\n=== 13. 招待報酬の確定 (手動承認でも確定する) ===")
    campaign_id = await bot.db.create_campaign(
        guild_id=G, name="招待テスト", inviter_reward=500, invited_reward=300,
        min_account_age_days=0, daily_limit=0, total_limit=0,
        require_charge=True, require_days=0, require_review=False,
        starts_at=None, ends_at=None, created_by=OWNER_ID,
    )
    inviter = guild.add_member(StubMember(31_100, guild))
    invited = guild.add_member(StubMember(31_101, guild))
    await bot.db.ensure_user(G, inviter.id)
    await bot.db.ensure_user(G, invited.id)
    record_id, _created = await bot.db.record_invite(
        guild_id=G, campaign_id=campaign_id, inviter_id=inviter.id,
        invited_id=invited.id, code="testcode", status=config.InviteStatus.PENDING,
        reason=None,
    )
    check(await bot.db.get_balance(G, inviter.id) == 0, "確定前は報酬なし")
    inv_request = await bot.charge.start_manual_charge(
        G, invited.id, config.ChargeProvider.PAYPAY, "1000")
    await bot.charge.submit_request(int(inv_request["request_id"]), invited.id, "PP-INVITE-1")
    await bot.charge.approve_request(int(inv_request["request_id"]), operator_id=OWNER_ID)
    record = await bot.db.get_invite_record_for_invited(G, invited.id)
    check(record["status"] == config.InviteStatus.CONFIRMED,
          "PayPay の承認でも招待が確定する")
    check(await bot.db.get_balance(G, inviter.id) == 500, "招待者へ報酬が入った")

    print("\n=== 14. 価格が使えないときは LTC を受け付けない ===")
    price_state["fail"] = "通信エラー (テスト)"
    await bot.db.set_system_value(price_service.KEY_MANUAL_PRICE, "")
    await bot.db.set_system_value(price_service.KEY_LAST_GOOD_AT, "0")
    bot.price._cache = None
    try:
        await bot.charge.start_manual_charge(G, member.id, config.ChargeProvider.LTC, "1000")
        check(False, "価格が無いのに LTC 申請が通る")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.PRICE_UNAVAILABLE,
              f"価格が使えないときは拒否 ({exc.code})")
    paypay_ok = await bot.charge.start_manual_charge(
        G, member.id, config.ChargeProvider.PAYPAY, "1000")
    check(int(paypay_ok["request_id"]) > 0, "LTC が不可でも PayPay は使える")
    price_state["fail"] = None

    print("\n=== 15. 整合性 ===")
    integrity = await bot.db.integrity_check()
    check(integrity["pragma"] == "ok", "PRAGMA quick_check OK")
    check(not integrity["balance_mismatch"], "残高と履歴合計が一致")
    check(not integrity["negative_balance"], "負の残高がない")
    dump = "\n".join(await bot.db.run(lambda c: list(c.iterdump())))
    check("paypay-recv-001" in dump, "入金先は設定として保存される (秘密情報ではない)")
    check("payments/" not in dump, "DBに送金リンクURLが保存されていない")

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
