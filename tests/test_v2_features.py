"""v2 で追加した機能の統合テスト。

対象: VIPショップ (残高でロール購入) / ロール別チャージ率 / 招待キャンペーンの不正対策 /
残高の詳細操作 (付け替え・取消・突合・修復) / チャージ取消 / 期間別ランキング /
トークン期限警告 / 受取残高しきい値 / 連続失敗クールダウン / 残高操作ログ /
MANUAL_REVIEW の緩和 / 日次サマリ。

実行:
    python3 tests/test_v2_features.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_v2_test"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "test_v2.db"
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
# Discord オブジェクトのスタブ
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

    def __ge__(self, other: "StubRole") -> bool:
        return self.position >= other.position

    def __lt__(self, other: "StubRole") -> bool:
        return self.position < other.position

    def __eq__(self, other: object) -> bool:
        return isinstance(other, StubRole) and other.id == self.id

    def __hash__(self) -> int:
        return hash(self.id)


class StubPermissions:
    def __init__(self, **kwargs: bool) -> None:
        self._values = {
            "manage_roles": True, "manage_guild": True, "create_instant_invite": True,
            "send_messages": True, "embed_links": True, "view_channel": True,
        }
        self._values.update(kwargs)

    def __getattr__(self, name: str) -> bool:
        return self._values.get(name, False)


class StubMember:
    def __init__(self, user_id: int, guild: "StubGuild", *, bot: bool = False,
                 created_days_ago: int = 100, roles: list[StubRole] | None = None) -> None:
        self.id = user_id
        self.guild = guild
        self.bot = bot
        self.name = f"user{user_id}"
        self.display_name = self.name
        self.mention = f"<@{user_id}>"
        self.created_at = datetime.now(timezone.utc) - timedelta(days=created_days_ago)
        self.joined_at = datetime.now(timezone.utc)
        self.roles = roles or []
        self.role_events: list[tuple[str, int]] = []
        self.fail_add_roles = False

    async def add_roles(self, *roles: StubRole, reason: str | None = None) -> None:
        if self.fail_add_roles:
            raise discord.Forbidden(_FakeResponse(), "no permission")
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)
            self.role_events.append(("add", role.id))

    async def remove_roles(self, *roles: StubRole, reason: str | None = None) -> None:
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)
            self.role_events.append(("remove", role.id))


class _FakeResponse:
    status = 403
    reason = "Forbidden"


class StubInvite:
    def __init__(self, code: str, uses: int = 0) -> None:
        self.code = code
        self.uses = uses
        self.url = f"https://discord.gg/{code}"


class StubChannel:
    def __init__(self, channel_id: int, guild: "StubGuild") -> None:
        self.id = channel_id
        self.guild = guild
        self.mention = f"<#{channel_id}>"
        self.sent: list[discord.Embed] = []

    def permissions_for(self, member: object) -> StubPermissions:
        return StubPermissions()

    async def create_invite(self, **kwargs: object) -> StubInvite:
        code = f"code{len(self.guild.invite_objects) + 1}"
        invite = StubInvite(code, 0)
        self.guild.invite_objects.append(invite)
        return invite

    async def send(self, *, embed: discord.Embed | None = None, **kwargs: object) -> object:
        if embed is not None:
            self.sent.append(embed)
        return object()


class StubGuild:
    def __init__(self, guild_id: int, name: str = "テストサーバー") -> None:
        self.id = guild_id
        self.name = name
        self.icon = None
        self.owner_id = 1
        self.members: dict[int, StubMember] = {}
        self.roles: dict[int, StubRole] = {}
        self.invite_objects: list[StubInvite] = []
        self.me = StubMember(999_999, self, created_days_ago=500)
        self.me.top_role = StubRole(9999, "Bot", position=100)  # type: ignore[attr-defined]
        self.me.guild_permissions = StubPermissions()  # type: ignore[attr-defined]
        self.channel = StubChannel(500_001, self)
        self.text_channels = [self.channel]
        self.rules_channel = None
        self.system_channel = None
        self.default_role = StubRole(guild_id, "@everyone", position=0)

    def get_member(self, user_id: int) -> StubMember | None:
        return self.members.get(user_id)

    def get_role(self, role_id: int) -> StubRole | None:
        return self.roles.get(role_id)

    def get_channel(self, channel_id: int) -> StubChannel | None:
        return self.channel if channel_id == self.channel.id else None

    async def invites(self) -> list[StubInvite]:  # type: ignore[override]
        return list(self.invite_objects)

    def add_member(self, member: StubMember) -> StubMember:
        self.members[member.id] = member
        return member

    def add_role(self, role: StubRole) -> StubRole:
        self.roles[role.id] = role
        return role


# ---------------------------------------------------------------------------
# Kyash HTTP モック (正常受取のみ。詳細は test_charge_flow.py で検証)
# ---------------------------------------------------------------------------
STATE: dict = {"wallet": 10_000, "links": {}, "received": set(), "history": []}


class FakeResponse:
    def __init__(self, payload=None, text: str = "", status: int = 200) -> None:
        self._payload = payload
        self.text = text
        self.status_code = status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def add_link(link_id: str, amount: int, uuid: str) -> str:
    STATE["links"][link_id] = {"amount": amount, "uuid": uuid}
    return f"https://kyash.me/payments/{link_id}"


def fake_get(url: str, **kwargs):
    if "kyash.me/payments/" in url:
        link = STATE["links"].get(url.rsplit("/", 1)[-1])
        if link is None or link["uuid"] in STATE["received"]:
            return FakeResponse(text="<html>処理済み</html>")
        return FakeResponse(text=(
            f'<div class="amountText text_send">&yen;{link["amount"]:,}</div>'
            f'<a class="btn_send" data-href-app="kyash://claim/{link["uuid"]}">受け取る</a>'
        ))
    if url.endswith("/primary_wallet"):
        return FakeResponse({"code": 200, "result": {"data": {
            "uuid": "W", "balance": {"amount": STATE["wallet"],
                                     "amountBreakdown": {"kyashMoney": STATE["wallet"],
                                                         "kyashValue": 0}},
            "pointBalance": {"availableAmount": 0}}}})
    if "/timeline" in url:
        return FakeResponse({"code": 200, "result": {"data": {"timelines": STATE["history"]}}})
    if "/v1/links/" in url:
        return FakeResponse({"code": 200, "result": {"data": {
            "target": {"publicId": "p", "userName": "sender"}}}})
    if url.endswith("/v1/me"):
        return FakeResponse({"code": 200, "result": {"data": {
            "userName": "bot", "imageUrl": "", "lastNameReal": "x", "firstNameReal": "y",
            "phoneNumber": "0", "kyc": True}}})
    raise AssertionError(f"unexpected GET {url}")


def fake_put(url: str, **kwargs):
    link_uuid = url.split("/v1/links/")[1].rsplit("/receive", 1)[0]
    amount = next((v["amount"] for v in STATE["links"].values() if v["uuid"] == link_uuid), 0)
    STATE["received"].add(link_uuid)
    STATE["wallet"] += amount
    STATE["history"].insert(0, {"linkUuid": link_uuid, "amount": amount})
    return FakeResponse({"code": 200, "result": {"data": {"ok": True}}})


requests.get = fake_get
requests.put = fake_put


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    import main as main_module

    guild = StubGuild(7000)
    balance_log_channel = StubChannel(500_002, guild)
    guild.text_channels.append(balance_log_channel)

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
        if channel_id == balance_log_channel.id:
            return balance_log_channel
        return guild.channel if guild_id == guild.id else None

    bot.charge._resolve_channel = fake_resolve_channel  # type: ignore[assignment]
    await bot.db.connect()

    G = guild.id
    await bot.db.set_guild_permission(G, "ALLOWED", 1)
    await bot.db.update_settings(
        G, charge_rate="130", minimum_charge=100, maximum_charge=50_000,
        daily_limit=1_000_000, balance_log_channel_id=balance_log_channel.id,
        balance_log_scope="ALL",
    )
    client = kyash_service.Kyash(access_token="t")
    bot.kyash._client = client
    bot.kyash._status = config.KyashAccountStatus.ACTIVE

    vip_role = guild.add_role(StubRole(8001, "VIP", position=10))
    member = guild.add_member(StubMember(9001, guild))

    print("\n=== 1. ロール別チャージ率 (VIP) ===")
    settings = await bot.db.get_settings(G)
    rate, role_id = await bot.charge.resolve_charge_rate(G, member.id, settings)
    check(rate == Decimal("130") and role_id is None, f"既定レートを適用 ({rate})")
    await bot.db.set_role_rate(G, vip_role.id, Decimal("150"), 10)
    rate, role_id = await bot.charge.resolve_charge_rate(G, member.id, settings)
    check(rate == Decimal("130"), "ロール未所持なら既定レート")
    member.roles.append(vip_role)
    rate, role_id = await bot.charge.resolve_charge_rate(G, member.id, settings)
    check(rate == Decimal("150") and role_id == vip_role.id, f"VIPロールで {rate}% を適用")
    premium = guild.add_role(StubRole(8002, "PREMIUM", position=11))
    await bot.db.set_role_rate(G, premium.id, Decimal("140"), 20)
    member.roles.append(premium)
    rate, role_id = await bot.charge.resolve_charge_rate(G, member.id, settings)
    check(rate == Decimal("140") and role_id == premium.id, "優先度が高いレートを採用")
    member.roles.remove(premium)

    print("\n=== 2. VIPレートでのチャージ ===")
    tx, amount, _ = await bot.charge.start_charge(G, member.id, "1000")
    row = await bot.db.get_transaction(tx)
    check(str(row["charge_rate"]) == "150", f"Transactionに適用レートを保存 ({row['charge_rate']}%)")
    await bot.charge.submit_link(tx, add_link("LINK001", 1000, "UUID001"), member.id)
    await bot.charge.process_queue_once()
    row = await bot.db.get_transaction(tx)
    check(row["credited_amount"] == 1500, f"VIPレートで付与 (credited={row['credited_amount']})")
    check(await bot.db.get_balance(G, member.id) == 1500, "残高 1500")
    check(
        any("残高変更" in (e.title or "") for e in balance_log_channel.sent),
        f"残高操作ログがチャンネルへ送信された ({len(balance_log_channel.sent)}件)",
    )

    print("\n=== 3. ショップ (残高でVIPロールを購入) ===")
    item_id = await bot.db.add_shop_item(
        guild_id=G, role_id=vip_role.id, name="VIPロール", price=1000,
        duration_days=30, stock=2, purchase_limit=1, created_by=1,
    )
    buyer = guild.add_member(StubMember(9002, guild))
    await bot.charge.admin_adjust_balance(
        guild_id=G, user_id=buyer.id, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=2500, operator_id=1, reason="テスト",
    )
    result = await bot.charge.purchase_shop_item(buyer, item_id)
    check(result["balance_after"] == 1500, "残高から支払い (2500 → 1500)")
    check(vip_role in buyer.roles, "ロールが付与された")
    check(result["expires_at"] is not None, "有効期限が設定された")
    purchase_id = int(result["purchase_id"])
    purchase = await bot.db.get_purchase(purchase_id)
    check(purchase["status"] == config.PurchaseStatus.ACTIVE, "購入が有効化された")
    rate, role_id = await bot.charge.resolve_charge_rate(G, buyer.id, settings)
    check(rate == Decimal("150"), "購入したロールでチャージ率が上がる")
    try:
        await bot.charge.purchase_shop_item(buyer, item_id)
        check(False, "購入上限が効いていない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.SHOP_LIMIT_REACHED, f"購入上限を拒否 ({exc.code})")
    poor = guild.add_member(StubMember(9003, guild))
    try:
        await bot.charge.purchase_shop_item(poor, item_id)
        check(False, "残高不足が検知されない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.INSUFFICIENT_BALANCE, f"残高不足を拒否 ({exc.code})")

    print("\n=== 4. ロール付与失敗時の自動返金 ===")
    failing = guild.add_member(StubMember(9004, guild))
    failing.fail_add_roles = True
    await bot.charge.admin_adjust_balance(
        guild_id=G, user_id=failing.id, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=1000, operator_id=1, reason="テスト",
    )
    try:
        await bot.charge.purchase_shop_item(failing, item_id)
        check(False, "ロール付与失敗が検知されない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.ROLE_ASSIGN_FAILED, f"付与失敗を検知 ({exc.code})")
    check(await bot.db.get_balance(G, failing.id) == 1000, "付与失敗時に自動返金された")

    print("\n=== 5. 期限切れロールの剥奪 / 返金 ===")
    await bot.db.execute(
        "UPDATE shop_purchases SET expires_at=? WHERE id=?", (utils.now_ts() - 10, purchase_id)
    )
    expired = await bot.charge.expire_shop_purchases()
    check(expired == 1, "期限切れの購入を処理")
    check(vip_role not in buyer.roles, "期限切れでロールを剥奪")
    check(
        (await bot.db.get_purchase(purchase_id))["status"] == config.PurchaseStatus.EXPIRED,
        "購入状態が EXPIRED",
    )
    balance_before_refund = await bot.db.get_balance(G, buyer.id)
    await bot.charge.refund_shop_purchase(
        purchase_id, operator_id=1, reason="テスト返金"
    )
    check(
        await bot.db.get_balance(G, buyer.id) == balance_before_refund + 1000,
        "返金で残高が戻る",
    )
    item = await bot.db.get_shop_item(item_id)
    check(int(item["stock"]) == 2, f"在庫が復元された ({item['stock']})")

    print("\n=== 6. 招待キャンペーン: 正常フロー ===")
    preset = config.CAMPAIGN_PRESETS["STANDARD"]
    campaign_id = await bot.db.create_campaign(
        guild_id=G, name="テストキャンペーン", inviter_reward=500, invited_reward=300,
        min_account_age_days=preset["min_account_age_days"],
        daily_limit=preset["daily_limit"], total_limit=preset["total_limit"],
        require_charge=True, require_days=0, require_review=False,
        starts_at=None, ends_at=None, created_by=1,
    )
    inviter = guild.add_member(StubMember(9100, guild, created_days_ago=200))
    await bot.charge.sync_invite_cache(guild)
    link = await bot.charge.issue_invite_code(inviter)
    check(link["created"] and link["code"], f"個人専用リンクを発行 ({link['code']})")
    again = await bot.charge.issue_invite_code(inviter)
    check(not again["created"] and again["code"] == link["code"], "2回目は同じリンクを再利用")

    invited = guild.add_member(StubMember(9101, guild, created_days_ago=60))
    guild.invite_objects[0].uses = 1          # 招待が1回使われた状態にする
    await bot.charge.handle_member_join(invited)
    record = await bot.db.get_invite_record_for_invited(G, invited.id)
    check(record is not None and record["status"] == config.InviteStatus.PENDING,
          "参加を PENDING で記録 (報酬は未確定)")
    check(int(record["inviter_id"]) == inviter.id, "招待者を正しく特定")
    check(await bot.db.get_balance(G, inviter.id) == 0, "確定前は報酬を付与しない")

    tx2, _, _ = await bot.charge.start_charge(G, invited.id, "1000")
    await bot.charge.submit_link(tx2, add_link("LINK002", 1000, "UUID002"), invited.id)
    await bot.charge.process_queue_once()
    record = await bot.db.get_invite_record_for_invited(G, invited.id)
    check(record["status"] == config.InviteStatus.CONFIRMED, "チャージ完了で招待が確定")
    check(await bot.db.get_balance(G, inviter.id) == 500, "招待者へ報酬 500")
    invited_balance = await bot.db.get_balance(G, invited.id)
    check(invited_balance == 1300 + 300, f"参加者はチャージ1300+報酬300 ({invited_balance})")
    result = await bot.db.confirm_invite_and_reward(int(record["id"]))
    check(result["already_confirmed"] and await bot.db.get_balance(G, inviter.id) == 500,
          "報酬の二重付与を防止")

    print("\n=== 7. 招待キャンペーン: 不正対策 ===")
    async def join_with(code_index: int, member_obj: StubMember) -> dict:
        guild.invite_objects[code_index].uses += 1
        await bot.charge.handle_member_join(member_obj)
        row = await bot.db.get_invite_record_for_invited(G, member_obj.id)
        return {"status": row["status"], "reason": row["reason"]} if row else {}

    # 自己招待 (判定ロジックを直接検証する。実運用では再入場判定が先に効く)
    campaign_row = await bot.db.get_active_campaign(G)
    self_member = guild.add_member(StubMember(9150, guild, created_days_ago=200))
    status, reason = await bot.charge._judge_invite(
        self_member, campaign_row, {"rejoin": False}, self_member.id, False
    )
    check(status == config.InviteStatus.REJECTED
          and reason == config.InviteRejectReason.SELF_INVITE,
          f"自己招待を無効化 ({reason})")
    # 招待者・被招待者のアカウント作成日が近接している場合は保留にする
    close_inviter = guild.add_member(StubMember(9151, guild, created_days_ago=90))
    close_invited = guild.add_member(StubMember(9152, guild, created_days_ago=90))
    status, reason = await bot.charge._judge_invite(
        close_invited, campaign_row, {"rejoin": False}, close_inviter.id, False
    )
    check(status == config.InviteStatus.HOLD
          and reason == config.InviteRejectReason.SUSPICIOUS_AGE,
          f"作成日が近接するアカウントを保留 ({reason})")

    # アカウントが新しすぎる
    newbie = guild.add_member(StubMember(9102, guild, created_days_ago=1))
    r = await join_with(0, newbie)
    check(r["status"] == config.InviteStatus.REJECTED
          and r["reason"] == config.InviteRejectReason.ACCOUNT_TOO_NEW,
          f"アカウント作成が新しすぎる招待を無効化 ({r['reason']})")

    # Bot アカウント
    bot_account = guild.add_member(StubMember(9103, guild, bot=True, created_days_ago=100))
    r = await join_with(0, bot_account)
    check(r["status"] == config.InviteStatus.REJECTED
          and r["reason"] == config.InviteRejectReason.BOT_ACCOUNT,
          "Botアカウントを無効化")

    # 退出 → 再入場
    rejoiner = guild.add_member(StubMember(9104, guild, created_days_ago=90))
    await bot.db.record_member_join(G, rejoiner.id)
    await bot.charge.handle_member_leave(G, rejoiner.id)
    r = await join_with(0, rejoiner)
    check(r["status"] == config.InviteStatus.REJECTED
          and r["reason"] == config.InviteRejectReason.REJOIN,
          "再入場による周回を無効化")

    # 招待者不明 (使用回数が増えない = バニティURL等)
    unknown = guild.add_member(StubMember(9105, guild, created_days_ago=90))
    await bot.charge.handle_member_join(unknown)
    row = await bot.db.get_invite_record_for_invited(G, unknown.id)
    check(row["status"] == config.InviteStatus.REJECTED
          and row["reason"] == config.InviteRejectReason.UNKNOWN_INVITER,
          "招待者を特定できない参加を無効化")

    # 既に被招待者として記録済み (二重記録の防止)
    before_count = (await bot.db.list_invite_records(G, limit=1))[1]
    await bot.charge.handle_member_join(invited)
    after_count = (await bot.db.list_invite_records(G, limit=1))[1]
    check(before_count == after_count, "同一ユーザーの再記録を防止")

    # ブラックリスト
    await bot.db.add_invite_blacklist(G, inviter.id, "テスト", 1)
    blacklisted_join = guild.add_member(StubMember(9106, guild, created_days_ago=90))
    r = await join_with(0, blacklisted_join)
    check(r["status"] == config.InviteStatus.REJECTED
          and r["reason"] == config.InviteRejectReason.BLACKLISTED,
          "ブラックリスト対象の招待を無効化")
    await bot.db.remove_invite_blacklist(G, inviter.id)

    # 短時間の大量参加 → HOLD (却下ではなく保留)
    inviter2 = guild.add_member(StubMember(9200, guild, created_days_ago=300))
    await bot.charge.issue_invite_code(inviter2)
    burst_index = len(guild.invite_objects) - 1
    holds = 0
    for i in range(config.INVITE_BURST_COUNT + 1):
        m = guild.add_member(StubMember(9300 + i, guild, created_days_ago=90))
        r = await join_with(burst_index, m)
        if r.get("status") == config.InviteStatus.HOLD:
            holds += 1
    check(holds >= 1, f"短時間の大量参加を保留 (HOLD {holds}件)")
    hold_rows, hold_total = await bot.db.list_invite_records(
        G, status=config.InviteStatus.HOLD, limit=5
    )
    check(hold_total >= 1, f"管理者レビュー待ちが記録された ({hold_total}件)")

    # 保留の承認 → チャージ条件が残るため PENDING へ
    await bot.charge.review_invite(
        int(hold_rows[0]["id"]), approve=True, operator_id=1, reason="確認済み"
    )
    reviewed = await bot.db.get_invite_record(int(hold_rows[0]["id"]))
    check(reviewed["status"] == config.InviteStatus.PENDING,
          "承認後もチャージ条件の判定を継続")
    # 保留の却下
    if hold_total >= 2:
        await bot.charge.review_invite(
            int(hold_rows[1]["id"]), approve=False, operator_id=1, reason="不正"
        )
        rejected = await bot.db.get_invite_record(int(hold_rows[1]["id"]))
        check(rejected["status"] == config.InviteStatus.REJECTED, "却下できる")

    # 日次上限
    limited_inviter = guild.add_member(StubMember(9400, guild, created_days_ago=300))
    await bot.charge.issue_invite_code(limited_inviter)
    idx = len(guild.invite_objects) - 1
    await bot.db.update_campaign(campaign_id, daily_limit=1)
    m1 = guild.add_member(StubMember(9401, guild, created_days_ago=90))
    await join_with(idx, m1)
    m2 = guild.add_member(StubMember(9402, guild, created_days_ago=90))
    r = await join_with(idx, m2)
    check(r["status"] == config.InviteStatus.REJECTED
          and r["reason"] == config.InviteRejectReason.DAILY_LIMIT,
          "日次上限を超えた招待を無効化")
    await bot.db.update_campaign(campaign_id, daily_limit=preset["daily_limit"])

    print("\n=== 8. 残高の詳細操作 ===")
    a = guild.add_member(StubMember(9500, guild))
    b = guild.add_member(StubMember(9501, guild))
    await bot.charge.admin_adjust_balance(
        guild_id=G, user_id=a.id, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=5000, operator_id=1, reason="初期",
    )
    await bot.charge.move_balance(
        guild_id=G, from_user_id=a.id, to_user_id=b.id, amount=2000,
        operator_id=1, reason="誤付与の是正",
    )
    check(await bot.db.get_balance(G, a.id) == 3000
          and await bot.db.get_balance(G, b.id) == 2000, "付け替えが両者へ反映")
    rows, _ = await bot.db.list_balance_history_filtered(G, user_id=a.id, limit=1)
    await bot.charge.undo_balance_change(
        guild_id=G, history_id=int(rows[0]["id"]), operator_id=1, reason="取消テスト"
    )
    check(await bot.db.get_balance(G, a.id) == 5000, "取消で残高が戻る")
    try:
        await bot.charge.undo_balance_change(
            guild_id=G, history_id=int(rows[0]["id"]), operator_id=1, reason="二重"
        )
        check(False, "二重取消できてしまう")
    except Exception as exc:
        check("既に取り消され" in str(exc), "二重取消を拒否")
    audit = await bot.db.audit_balance(G, a.id)
    check(audit["diff"] == 0, "突合が一致")
    await bot.db.execute(
        "UPDATE balances SET balance=balance+999 WHERE guild_id=? AND user_id=?", (G, a.id)
    )
    audit = await bot.db.audit_balance(G, a.id)
    check(audit["diff"] == 999, f"不整合を検出 (差分 {audit['diff']})")
    await bot.charge.repair_balance(
        guild_id=G, user_id=a.id, operator_id=1, reason="修復テスト", mode="balance"
    )
    audit = await bot.db.audit_balance(G, a.id)
    check(audit["diff"] == 0, "修復で一致した")

    print("\n=== 9. チャージの取消 (返金) ===")
    refund_target = guild.add_member(StubMember(9600, guild))
    tx3, _, _ = await bot.charge.start_charge(G, refund_target.id, "2000")
    await bot.charge.submit_link(tx3, add_link("LINK003", 2000, "UUID003"), refund_target.id)
    await bot.charge.process_queue_once()
    credited = (await bot.db.get_transaction(tx3))["credited_amount"]
    check(await bot.db.get_balance(G, refund_target.id) == credited, f"チャージ完了 ({credited})")
    await bot.charge.refund_charge_transaction(
        tx3, operator_id=1, reason="誤チャージ"
    )
    check(await bot.db.get_balance(G, refund_target.id) == 0, "取消で残高を回収")
    check((await bot.db.get_transaction(tx3))["refunded_at"] is not None, "取消日時を記録")
    try:
        await bot.charge.refund_charge_transaction(tx3, operator_id=1, reason="二重")
        check(False, "二重取消できてしまう")
    except Exception:
        check(True, "二重取消を拒否")
    check(any("取り消され" in title for _, title in bot.dms), "利用者へ取消を通知")

    print("\n=== 10. 期間別ランキング ===")
    settings = await bot.db.get_settings(G)
    weekly = await bot.charge.build_ranking_entries(G, settings, config.RankingType.WEEKLY)
    monthly = await bot.charge.build_ranking_entries(G, settings, config.RankingType.MONTHLY)
    invite_rank = await bot.charge.build_ranking_entries(G, settings, config.RankingType.INVITE)
    balance_rank = await bot.charge.build_ranking_entries(G, settings, config.RankingType.BALANCE)
    check(bool(weekly), f"週間ランキングを集計 ({len(weekly)}件)")
    check(bool(monthly), f"月間ランキングを集計 ({len(monthly)}件)")
    check(bool(invite_rank), f"招待ランキングを集計 ({len(invite_rank)}件)")
    check(bool(balance_rank), f"残高ランキングを集計 ({len(balance_rank)}件)")
    check(not any(uid == refund_target.id for uid, _, _ in weekly),
          "取消済みチャージは期間ランキングから除外")
    sig_a = bot.charge.ranking_signature(weekly)
    sig_b = bot.charge.ranking_signature(
        await bot.charge.build_ranking_entries(G, settings, config.RankingType.WEEKLY)
    )
    check(sig_a == sig_b, "同内容なら署名が一致 (無駄な編集をしない)")

    print("\n=== 11. 連続失敗クールダウン ===")
    spammer = guild.add_member(StubMember(9700, guild))
    for i in range(config.FAILURE_COOLDOWN_THRESHOLD):
        bot.charge.record_failure(G, spammer.id)
    remaining = bot.charge.cooldown_remaining(G, spammer.id)
    check(remaining > 0, f"連続失敗でクールダウン ({remaining}秒)")
    try:
        await bot.charge.start_charge(G, spammer.id, "1000")
        check(False, "クールダウン中にチャージできてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.COOLDOWN, f"クールダウン中を拒否 ({exc.code})")
    bot.charge.clear_cooldown(G, spammer.id)
    check(bot.charge.cooldown_remaining(G, spammer.id) == 0, "管理者が解除できる")

    print("\n=== 12. 残高上限 / 受取アカウントのしきい値 ===")
    await bot.db.update_settings(G, max_balance=1000)
    capped = guild.add_member(StubMember(9800, guild))
    await bot.charge.admin_adjust_balance(
        guild_id=G, user_id=capped.id, change_type=config.BalanceChangeType.ADMIN_ADD,
        amount=900, operator_id=1, reason="テスト",
    )
    try:
        await bot.charge.start_charge(G, capped.id, "1000")
        check(False, "残高上限が効いていない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.MAX_BALANCE_EXCEEDED, f"残高上限で拒否 ({exc.code})")
    await bot.db.update_settings(G, max_balance=0)

    await bot.kyash.set_wallet_threshold(STATE["wallet"] + 500)
    await bot.kyash.get_wallet()
    normal = guild.add_member(StubMember(9801, guild))
    try:
        await bot.charge.start_charge(G, normal.id, "1000")
        check(False, "受取しきい値が効いていない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.WALLET_LIMIT, f"受取しきい値で拒否 ({exc.code})")
    check("UUID-NEW" not in STATE["received"], "しきい値超過時は受取しない")
    await bot.kyash.set_wallet_threshold(0)

    print("\n=== 13. MANUAL_REVIEW の緩和とエスカレーション ===")
    stuck_user = guild.add_member(StubMember(9900, guild))
    tx4, _, _ = await bot.charge.start_charge(G, stuck_user.id, "1000")
    await bot.charge.submit_link(tx4, add_link("LINK004", 1000, "UUID004"), stuck_user.id)
    await bot.db.transition_status(tx4, config.TxStatus.PROCESSING,
                                  expected=(config.TxStatus.QUEUED,))
    await bot.charge._to_manual_review(tx4, config.ErrorCode.MANUAL_REVIEW, "テスト")
    try:
        await bot.charge.start_charge(G, stuck_user.id, "1000")
        check(False, "確認中でも新規チャージできてしまう (既定では不可のはず)")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.ACTIVE_TRANSACTION_EXISTS,
              f"既定では確認中の利用者をブロック ({exc.code})")
    await bot.db.update_settings(G, manual_review_allow_new=1)
    tx5, _, _ = await bot.charge.start_charge(G, stuck_user.id, "1000")
    check(bool(tx5), "設定で確認中でも新規チャージを許可できる")
    await bot.charge.cancel_transaction(tx5, stuck_user.id)
    await bot.db.execute(
        "UPDATE charge_transactions SET updated_at=? WHERE id=?",
        (utils.now_ts() - config.MANUAL_REVIEW_ESCALATION_SECONDS - 60, tx4),
    )
    before_alerts = len(bot.owner_alerts)
    await bot.charge.escalate_manual_reviews()
    check(len(bot.owner_alerts) > before_alerts, "未解決の手動確認を再通知")

    print("\n=== 14. 日次サマリ ===")
    await bot.db.update_settings(G, summary_channel_id=guild.channel.id, summary_enabled=1)
    posted = await bot.charge.post_daily_summary(
        G, start=utils.jst_day_start(), end=utils.now_ts() + 1
    )
    check(posted, "日次サマリを投稿")
    check(any("デイリーサマリ" in (e.title or "") for e in guild.channel.sent),
          "サマリ Embed が送信された")

    print("\n=== 15. 管理ダッシュボード / トークン期限 ===")
    embed = await bot.charge.build_admin_panel_embed(G)
    check("管理ダッシュボード" in (embed.title or ""), "管理ダッシュボードを生成")
    bot.kyash._token_issued_at = utils.now_ts() - 28 * 86400
    check(bot.kyash.token_expiring_soon, f"トークン失効が近い ({bot.kyash.token_days_left:.1f}日)")
    snapshot = bot.kyash.status_snapshot()
    check("token_days_left" in snapshot and "wallet_headroom" in snapshot,
          "状態スナップショットに期限・しきい値情報を含む")

    print("\n=== 16. 整合性 ===")
    integrity = await bot.db.integrity_check()
    check(integrity["pragma"] == "ok", "PRAGMA quick_check OK")
    check(not integrity["completed_without_history"], "完了取引に履歴がある")
    check(not integrity["balance_mismatch"], "残高と履歴合計が一致")
    check(not integrity["negative_balance"], "負の残高がない")
    dump = "\n".join(await bot.db.run(lambda c: list(c.iterdump())))
    check("payments/" not in dump, "DBに送金リンクURLが保存されていない")

    await bot.charge.shutdown()
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
