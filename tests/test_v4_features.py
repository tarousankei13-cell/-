#!/usr/bin/env python3
"""v4 で追加した機能の統合テスト。

対象: 累計チャージによる段位 / ランキング報酬の自動配布 /
受取用 Kyash アカウントの複数登録 / サブスク商品と商品タイプの拡張 /
オークション / チャージ目標 / 不正検知 / 返金申請 / レシート / グラフ画像。

外部 HTTP は使わない (Kyash は必要な部分だけモックする)。

実行:
    python3 tests/test_v4_features.py
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

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_v4_test"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "test_v4.db"
config.SECRET_KEY_PATH = SCRATCH / "secret.key"
config.BACKUP_DIR = SCRATCH / "backups"
config.RANKING_DEBOUNCE_SECONDS = 0.05

import discord  # noqa: E402

import utils  # noqa: E402
from charge_service import ChargeError  # noqa: E402

#: 後続のセクションで例外を検証するために使う
_CHARGE_ERROR = ChargeError

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
        self.colour = None
        self.deleted = False

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

    async def delete(self, *, reason: str | None = None) -> None:
        self.deleted = True
        self.guild.roles.pop(self.id, None)  # type: ignore[attr-defined]


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
        self.nick: str | None = None
        self.created_at = datetime.now(timezone.utc) - timedelta(days=300)
        self.joined_at = datetime.now(timezone.utc)
        self.roles = roles or []
        self.fail_add_roles = False

    async def add_roles(self, *roles: StubRole, reason: str | None = None) -> None:
        if self.fail_add_roles:
            raise discord.Forbidden(_FakeResponse(), "no permission")  # type: ignore[arg-type]
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles: StubRole, reason: str | None = None) -> None:
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)

    async def edit(self, *, nick: str | None = None, reason: str | None = None) -> None:
        self.nick = nick


class StubChannel:
    def __init__(self, channel_id: int, guild: "StubGuild", name: str = "ch") -> None:
        self.id = channel_id
        self.guild = guild
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.sent: list[discord.Embed] = []
        self.messages: dict[int, Any] = {}
        self.deleted = False
        self.overwrites: dict[Any, Any] = {}

    def permissions_for(self, member: object) -> StubPermissions:
        return StubPermissions()

    async def send(self, *, embed: discord.Embed | None = None,
                   view: Any = None, **kw: Any) -> Any:
        if embed is not None:
            self.sent.append(embed)
        msg = StubMessage(900_000 + len(self.messages) + 1, self, embed)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, message_id: int) -> Any:
        msg = self.messages.get(int(message_id))
        if msg is None:
            raise discord.NotFound(_FakeResponse(), "not found")  # type: ignore[arg-type]
        return msg

    async def delete(self, *, reason: str | None = None) -> None:
        self.deleted = True


class StubMessage:
    def __init__(self, message_id: int, channel: StubChannel,
                 embed: discord.Embed | None) -> None:
        self.id = message_id
        self.channel = channel
        self.embeds = [embed] if embed else []
        self.edits: list[discord.Embed] = []

    async def edit(self, *, embed: discord.Embed | None = None,
                   view: Any = None, **kw: Any) -> "StubMessage":
        if embed is not None:
            self.embeds = [embed]
            self.edits.append(embed)
        return self


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
        self.me.top_role = StubRole(9_999, "Bot", position=100)  # type: ignore[attr-defined]
        self.channel = StubChannel(500_001, self, "general")
        self.review = StubChannel(500_002, self, "review")
        self.text_channels = [self.channel, self.review]
        self.default_role = StubRole(guild_id, "@everyone", position=0)
        self.created_roles: list[StubRole] = []
        self.created_channels: list[StubChannel] = []
        self._next_id = 70_000

    def get_member(self, user_id: int) -> StubMember | None:
        return self.members.get(user_id)

    def get_role(self, role_id: int) -> StubRole | None:
        return self.roles.get(role_id)

    def get_channel(self, channel_id: int) -> StubChannel | None:
        for ch in self.text_channels:
            if ch.id == channel_id:
                return ch
        return None

    def add_member(self, m: StubMember) -> StubMember:
        self.members[m.id] = m
        return m

    def add_role(self, r: StubRole) -> StubRole:
        self.roles[r.id] = r
        r.guild = self  # type: ignore[attr-defined]
        return r

    async def create_role(self, *, name: str, colour: Any = None,
                          reason: str | None = None, **kw: Any) -> StubRole:
        self._next_id += 1
        role = StubRole(self._next_id, name, position=3)
        role.colour = colour
        self.add_role(role)
        self.created_roles.append(role)
        return role

    async def create_text_channel(self, name: str, *, overwrites: Any = None,
                                  reason: str | None = None, **kw: Any) -> StubChannel:
        self._next_id += 1
        ch = StubChannel(self._next_id, self, name)
        ch.overwrites = overwrites or {}  # 公開範囲の検証に使う
        self.text_channels.append(ch)
        self.created_channels.append(ch)
        return ch


OWNER_ID = 5252


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    import main as main_module

    guild = StubGuild(51_000)

    class TestBot(main_module.ChargeBot):
        def __init__(self) -> None:
            super().__init__()
            self.owner_alerts: list[str] = []
            self.dms: list[tuple[int, str]] = []

        def get_guild(self, guild_id: int):  # type: ignore[override]
            return guild if guild_id == guild.id else None

        def get_user(self, user_id: int):  # type: ignore[override]
            return guild.get_member(user_id)

        def get_channel(self, channel_id: int):  # type: ignore[override]
            return guild.get_channel(channel_id)

        @property
        def guilds(self):  # type: ignore[override]
            return [guild]

        def is_bot_owner(self, user: object) -> bool:  # type: ignore[override]
            return getattr(user, "id", None) == OWNER_ID

        async def alert_owner(self, message: str) -> None:
            self.owner_alerts.append(message)

    bot = TestBot()

    async def fake_send_dm(user_id, embed, *, tx_id=None, queue_on_failure=True):
        bot.dms.append((user_id, embed.title or ""))
        return True

    bot.charge._send_dm = fake_send_dm  # type: ignore[assignment]

    async def fake_resolve_channel(guild_id, channel_id, setting_name):
        return guild.get_channel(channel_id) or guild.channel

    bot.charge._resolve_channel = fake_resolve_channel  # type: ignore[assignment]
    bot.charge._resolve_message_channel = (  # type: ignore[assignment]
        lambda gid, cid: fake_resolve_channel(gid, cid, "")
    )

    async def fake_resolve_global(channel_id):
        return guild.get_channel(int(channel_id))

    bot.charge._resolve_global_channel = fake_resolve_global  # type: ignore[assignment]

    await bot.db.connect()
    G = guild.id
    await bot.db.set_guild_permission(G, "ALLOWED", OWNER_ID)
    await bot.db.update_settings(
        G, charge_rate="130", minimum_charge=100, maximum_charge=1_000_000,
        daily_limit=0, achievement_channel_id=guild.channel.id,
        log_channel_id=guild.channel.id,
    )

    async def give_charge(user_id: int, amount: int, *, when: int | None = None) -> str:
        """完了済みチャージを直接作る (受取経路は他のテストで検証済み)。"""
        await bot.db.ensure_user(G, user_id)
        tx = await bot.db.create_proxy_transaction(
            guild_id=G, user_id=user_id, requested_amount=amount,
            received_amount=amount, charge_rate=Decimal("130"),
        )
        await bot.db.credit_transaction(
            tx, utils.calc_credited_amount(amount, Decimal("130"))
        )
        if when is not None:
            await bot.db.execute(
                "UPDATE charge_transactions SET created_at=?, completed_at=? WHERE id=?",
                (when, when, tx),
            )
        return tx

    print("\n=== 1. 累計チャージによる段位 ===")
    bronze = guild.add_role(StubRole(61_001, "ブロンズ", position=6))
    silver = guild.add_role(StubRole(61_002, "シルバー", position=7))
    gold = guild.add_role(StubRole(61_003, "ゴールド", position=8))
    t1 = await bot.db.add_tier(guild_id=G, name="ブロンズ", threshold=10_000,
                               role_id=bronze.id, description="レート+5%",
                               created_by=OWNER_ID)
    t2 = await bot.db.add_tier(guild_id=G, name="シルバー", threshold=50_000,
                               role_id=silver.id, description=None, created_by=OWNER_ID)
    await bot.db.add_tier(guild_id=G, name="ゴールド", threshold=100_000,
                          role_id=gold.id, description=None, created_by=OWNER_ID)
    tiers = await bot.db.list_tiers(G)
    check([int(t["threshold"]) for t in tiers] == [10_000, 50_000, 100_000],
          "段位はしきい値の昇順で返る")

    member = guild.add_member(StubMember(62_001, guild))
    await give_charge(member.id, 5_000)
    promoted = await bot.charge.check_tiers(G, member.id)
    check(not promoted and bronze not in member.roles, "しきい値未満では付与しない")

    await give_charge(member.id, 6_000)   # 累計 11,000
    promoted = await bot.charge.check_tiers(G, member.id)
    check(len(promoted) == 1 and bronze in member.roles,
          f"しきい値到達で付与 ({[p['name'] for p in promoted]})")
    check(any("昇格" in t for _u, t in bot.dms), "段位到達を DM で通知")
    check(any("段位に到達" in (e.title or "") for e in guild.channel.sent),
          "実績チャンネルへ投稿")

    promoted = await bot.charge.check_tiers(G, member.id)
    check(not promoted, "同じ段位を二重に付与しない")

    # 一気に2段飛ばし → 途中の段位も付与される
    await give_charge(member.id, 95_000)  # 累計 106,000
    promoted = await bot.charge.check_tiers(G, member.id)
    names = sorted(str(p["name"]) for p in promoted)
    check(names == ["ゴールド", "シルバー"] and silver in member.roles and gold in member.roles,
          f"飛び越えても途中の段位を付与 ({names})")

    # 返金された取引は累計に数えない
    refund_user = guild.add_member(StubMember(62_002, guild))
    tx = await give_charge(refund_user.id, 20_000)
    await bot.db.execute(
        "UPDATE charge_transactions SET refunded_at=? WHERE id=?", (utils.now_ts(), tx)
    )
    total = await bot.db.get_total_charged(G, refund_user.id)
    check(total == 0, f"取消された取引は累計に数えない ({total})")
    check(not await bot.charge.check_tiers(G, refund_user.id), "取消後は昇格しない")

    # Bot より上位のロールは付与できない (安全に失敗する)
    high = guild.add_role(StubRole(61_010, "管理者より上", position=200))
    await bot.db.add_tier(guild_id=G, name="無理な段位", threshold=1,
                          role_id=high.id, description=None, created_by=OWNER_ID)
    alerts_before = len(bot.owner_alerts)
    safe_user = guild.add_member(StubMember(62_003, guild))
    await give_charge(safe_user.id, 1_000)
    await bot.charge.check_tiers(G, safe_user.id)
    check(high not in safe_user.roles, "付与できないロールは付与しない")
    check(len(bot.owner_alerts) > alerts_before, "付与できないことを Owner へ通知")

    # 段位の削除で記録も消え、作り直すと再付与される
    await bot.db.remove_tier(G, t1)
    granted = await bot.db.list_granted_tier_ids(G, member.id)
    check(t1 not in granted and t2 in granted, "削除した段位の記録だけが消える")

    print("\n=== 2. ランキング報酬の自動配布 ===")
    # 締めた週に3人がチャージした状態を作る
    start, end, period_key = utils.period_bounds(config.RankingPeriod.WEEKLY)
    mid = start + 3600
    winners = [guild.add_member(StubMember(63_000 + i, guild)) for i in range(4)]
    for i, w in enumerate(winners):
        await give_charge(w.id, (4 - i) * 10_000, when=mid)   # 1位が最多
    champ_role = guild.add_role(StubRole(61_020, "週間王者", position=9))
    await bot.db.set_ranking_reward(
        guild_id=G, ranking_type=config.RankingPeriod.WEEKLY, rank_from=1, rank_to=1,
        amount=5_000, role_id=champ_role.id, created_by=OWNER_ID)
    await bot.db.set_ranking_reward(
        guild_id=G, ranking_type=config.RankingPeriod.WEEKLY, rank_from=2, rank_to=3,
        amount=2_000, role_id=None, created_by=OWNER_ID)

    ranking = await bot.db.get_period_ranking(G, start=start, end=end, limit=10)
    check([int(r["user_id"]) for r in ranking][:4] == [w.id for w in winners],
          "締めた期間のランキングが獲得額の降順になる")

    before = {w.id: await bot.db.get_balance(G, w.id) for w in winners}
    result = await bot.charge.distribute_ranking_rewards(
        G, config.RankingPeriod.WEEKLY, operator_id=OWNER_ID)
    check(result["granted"] == 3, f"上位3人へ配布 ({result['granted']}人)")
    check(result["period_key"] == period_key, f"期間キー ({result['period_key']})")
    check(await bot.db.get_balance(G, winners[0].id) == before[winners[0].id] + 5_000,
          "1位へ 5,000")
    check(await bot.db.get_balance(G, winners[1].id) == before[winners[1].id] + 2_000,
          "2位へ 2,000")
    check(await bot.db.get_balance(G, winners[3].id) == before[winners[3].id],
          "4位へは配布しない")
    check(champ_role in winners[0].roles, "1位へロールを付与")
    check(any("報酬配布" in (e.title or "") for e in guild.channel.sent),
          "配布結果を実績チャンネルへ投稿")

    before2 = await bot.db.get_balance(G, winners[0].id)
    again = await bot.charge.distribute_ranking_rewards(G, config.RankingPeriod.WEEKLY)
    check(again.get("already") is True, "同じ期間は再配布しない")
    check(await bot.db.get_balance(G, winners[0].id) == before2, "二重配布されない")

    # 凍結された利用者は対象外
    frozen = guild.add_member(StubMember(63_100, guild))
    await give_charge(frozen.id, 500_000, when=mid)
    await bot.db.set_frozen(G, frozen.id, True, OWNER_ID, "テスト")
    ranking = await bot.db.get_period_ranking(G, start=start, end=end, limit=10)
    check(frozen.id not in [int(r["user_id"]) for r in ranking],
          "凍結された利用者はランキングに載らない")

    print("\n=== 3. 受取用 Kyash アカウントの複数登録 ===")
    import kyash_service

    # 3件の枠を用意する (優先度 0,1,2)
    ids = []
    for i, (label, threshold) in enumerate(
        (("main", 10_000), ("sub1", 20_000), ("sub2", 0))
    ):
        account_id = await bot.kyash.add_account(label, threshold=threshold, priority=i)
        ids.append(account_id)
        slot = bot.kyash.get_slot(account_id)
        assert slot is not None
        slot.client = object()  # type: ignore[assignment]
        slot.status = config.KyashAccountStatus.ACTIVE
        slot.wallet_balance = 0
    check([s.label for s in bot.kyash.slots()] == ["main", "sub1", "sub2"],
          "優先度順に並ぶ")
    check(len(bot.kyash.usable_slots()) == 3, "3件すべて利用可")
    check(bot.kyash.is_usable, "1件でも使えれば受付可")

    picked = bot.kyash.pick_slot(5_000)
    check(picked.label == "main", f"優先度の高い順に選ぶ ({picked.label})")

    # main がしきい値に達したら sub1 が選ばれる
    bot.kyash.get_slot(ids[0]).wallet_balance = 9_000  # type: ignore[union-attr]
    picked = bot.kyash.pick_slot(5_000)
    check(picked.label == "sub1", f"余裕が無ければ次のアカウントへ ({picked.label})")
    check(bot.kyash.is_usable and not bot.kyash.wallet_limit_reached,
          "1件が上限でもチャージは止まらない")

    # main/sub1 が上限 → しきい値なしの sub2 が選ばれる
    bot.kyash.get_slot(ids[1]).wallet_balance = 19_500  # type: ignore[union-attr]
    picked = bot.kyash.pick_slot(5_000)
    check(picked.label == "sub2", f"しきい値なしのアカウントが受ける ({picked.label})")

    # sub2 を無効化すると受け取れるアカウントが無くなる
    await bot.kyash.set_account_options(ids[2], enabled=False)
    try:
        bot.kyash.pick_slot(5_000)
        check(False, "全て上限なのに選べてしまう")
    except kyash_service.NoCapacityError as exc:
        check("しきい値" in str(exc) or "ありません" in str(exc),
              f"受け取れるアカウントが無いことを検出 ({exc})")
    # 「その額が入らない」と「もう何も受け取れない」は別の状態として扱う
    check(not bot.kyash.wallet_limit_reached,
          "しきい値に未達なら wallet_limit_reached は False (少額はまだ受けられる)")
    try:
        bot.kyash.check_wallet_capacity(5_000)
        check(False, "容量チェックが通ってしまう")
    except kyash_service.WalletLimitError:
        check(True, "その額が入らないことは容量チェックで拒否される")
    picked = bot.kyash.pick_slot(500)
    check(picked.label == "main", f"少額なら余裕のあるアカウントで受ける ({picked.label})")
    # しきい値に完全に到達させると「もう受け取れない」状態になる
    bot.kyash.get_slot(ids[0]).wallet_balance = 10_000   # type: ignore[union-attr]
    bot.kyash.get_slot(ids[1]).wallet_balance = 20_000   # type: ignore[union-attr]
    check(bot.kyash.wallet_limit_reached,
          "全アカウントがしきい値に到達すると wallet_limit_reached が True")
    bot.kyash.get_slot(ids[0]).wallet_balance = 9_000    # type: ignore[union-attr]
    bot.kyash.get_slot(ids[1]).wallet_balance = 19_500   # type: ignore[union-attr]

    await bot.kyash.set_account_options(ids[2], enabled=True)
    check(bot.kyash.pick_slot(5_000).label == "sub2", "再有効化で再び使える")

    # 集約された状態
    snapshot = bot.kyash.status_snapshot()
    check(snapshot["account_count"] == 3 and snapshot["usable_count"] == 3,
          f"スナップショットに件数が入る ({snapshot['account_count']}/{snapshot['usable_count']})")
    check(len(snapshot["accounts"]) == 3, "アカウント明細が入る")
    check(snapshot["wallet_balance"] == 28_500,
          f"残高は合計で表示 ({snapshot['wallet_balance']})")
    check(all("access_token" not in str(a) for a in snapshot["accounts"]),
          "スナップショットにトークンを含まない")

    # 永続化されているか (再読込しても優先度としきい値が残る)
    rows = await bot.db.list_kyash_accounts()
    check([r["label"] for r in rows] == ["main", "sub1", "sub2"], "DB にも優先度順で保存")
    check([int(r["threshold"]) for r in rows] == [10_000, 20_000, 0],
          "しきい値が保存されている")

    # 識別名で引ける / 未知の ID は最古へフォールバックする
    check(bot.kyash.find_slot_by_label("sub1") is not None, "識別名で引ける")
    fallback = bot.kyash.get_slot(999_999)
    check(fallback is not None and fallback.id == ids[0],
          "未知のIDは最古のアカウントへフォールバック (v3以前の取引用)")

    # --- 監視タスクがアカウント単位で通知するか ---
    import tasks as tasks_module

    monitor = tasks_module.BackgroundTasks(bot, bot.db, bot.charge, bot.kyash)
    health_results: dict[int, str] = {
        ids[0]: config.KyashAccountStatus.ACTIVE,
        ids[1]: config.KyashAccountStatus.AUTH_REQUIRED,
        ids[2]: config.KyashAccountStatus.ACTIVE,
    }

    async def fake_health(account_id=None):
        assert account_id is not None, "監視はアカウントを指定して確認する"
        status = health_results[account_id]
        slot = bot.kyash.get_slot(account_id)
        assert slot is not None
        slot.status = status
        if status != config.KyashAccountStatus.ACTIVE:
            slot.last_error = "Authorization: Bearer eyJhbGciOi.SECRET.SIG"
        return status

    real_health = bot.kyash.health_check
    bot.kyash.health_check = fake_health  # type: ignore[assignment]
    bot.owner_alerts.clear()
    await monitor.kyash_health()
    alerts = "\n".join(bot.owner_alerts)
    check(len(bot.owner_alerts) >= 1, f"異常なアカウントを通知する ({len(bot.owner_alerts)}件)")
    check("sub1" in alerts, "通知に対象アカウント名が入る")
    check("継続" in alerts, "他が生きていれば継続と伝える")
    check("Bearer eyJ" not in alerts and "SECRET" not in alerts,
          "通知にトークンを含まない (マスクされる)")
    before = len(bot.owner_alerts)
    await monitor.kyash_health()
    check(len(bot.owner_alerts) == before, "同じ異常は繰り返し通知しない")
    health_results[ids[1]] = config.KyashAccountStatus.ACTIVE
    bot.owner_alerts.clear()
    await monitor.kyash_health()
    check(any("復帰" in a and "sub1" in a for a in bot.owner_alerts),
          "復帰も通知する")

    # トークン期限はアカウントごとに1日1回
    slot1 = bot.kyash.get_slot(ids[1])
    assert slot1 is not None
    slot1.token_issued_at = utils.now_ts() - int(29.5 * 86400)
    bot.owner_alerts.clear()
    await monitor._check_token_expiry()
    check(any("sub1" in a and "アクセストークン" in a for a in bot.owner_alerts),
          "失効が近いアカウントを名指しで通知")
    before = len(bot.owner_alerts)
    await monitor._check_token_expiry()
    check(len(bot.owner_alerts) == before, "トークン警告は1日1回")

    # 残高しきい値: 1台だけなら継続、全台なら停止として通知
    for account_id in ids:
        slot = bot.kyash.get_slot(account_id)
        assert slot is not None
        slot.status = config.KyashAccountStatus.ACTIVE
    bot.kyash.get_slot(ids[0]).wallet_balance = 10_000   # type: ignore[union-attr]
    bot.kyash.get_slot(ids[1]).wallet_balance = 0        # type: ignore[union-attr]
    bot.owner_alerts.clear()
    await monitor._check_wallet_threshold()
    check(any("main" in a for a in bot.owner_alerts), "上限到達を個別に通知")
    check(all("停止しています" not in a for a in bot.owner_alerts),
          "他に余裕があれば停止とは言わない")
    bot.kyash.get_slot(ids[1]).wallet_balance = 20_000   # type: ignore[union-attr]
    # sub2 はしきい値なし (無制限) なので、これを塞がないと「全台上限」にはならない
    bot.owner_alerts.clear()
    await monitor._check_wallet_threshold()
    check(all("すべて" not in a for a in bot.owner_alerts),
          "しきい値なしのアカウントが残っていれば全台上限にはしない")
    await bot.kyash.set_account_options(ids[2], threshold=1_000)
    bot.kyash.get_slot(ids[2]).wallet_balance = 1_000    # type: ignore[union-attr]
    bot.owner_alerts.clear()
    await monitor._check_wallet_threshold()
    check(any("すべて" in a for a in bot.owner_alerts), "全台上限なら停止を通知")
    before = len(bot.owner_alerts)
    await monitor._check_wallet_threshold()
    check(len(bot.owner_alerts) == before, "全台上限の通知も繰り返さない")
    bot.kyash.health_check = real_health  # type: ignore[assignment]
    for account_id in ids:
        bot.kyash.get_slot(account_id).wallet_balance = 0  # type: ignore[union-attr]
    await bot.kyash.set_account_options(ids[2], threshold=0)

    # 削除
    await bot.kyash.remove_account(ids[2])
    check(len(bot.kyash.slots()) == 2, "削除できる")
    check(await bot.db.get_kyash_account_row(ids[2]) is None, "DB からも消える")

    print("\n=== 4. 商品タイプの拡張 ===")
    shopper = guild.add_member(StubMember(63_101, guild))
    await bot.db.update_settings(G, shop_enabled=1)
    await bot.db.adjust_balance(
        guild_id=G, user_id=shopper.id, amount=500_000,
        change_type=config.BalanceChangeType.ADMIN_ADD,
        operator_id=OWNER_ID, reason="テスト用",
    )

    async def buy(item_id: int, *, value: str | None = None, color: str | None = None):
        return await bot.charge.purchase_shop_item(
            shopper, item_id, item_input=value, item_color=color
        )

    # --- カスタムロール: 名前と色を指定して新規作成 ---
    custom_id = await bot.db.add_shop_item(
        guild_id=G, role_id=0, name="好きな名前のロール", price=1_000,
        duration_days=0, item_type=config.ShopItemType.CUSTOM_ROLE,
        created_by=OWNER_ID,
    )
    before_roles = len(guild.roles)
    result = await buy(custom_id, value="常連さん", color="#FF66AA")
    created = guild.created_roles[-1]
    check(len(guild.roles) == before_roles + 1 and created.name == "常連さん",
          f"カスタムロールが作られる ({created.name})")
    check(created in shopper.roles, "作ったロールが付与される")
    check(getattr(created.colour, "value", None) == 0xFF66AA,
          "指定した色が反映される")
    assets = await bot.db.list_purchase_assets(int(result["purchase_id"]))
    check(len(assets) == 1 and int(assets[0]["asset_id"]) == created.id,
          "後片付け用に作成物が記録される")
    # 危険な文字は落とす (メンションやコードブロックを名前にできない)
    await buy(custom_id, value="@everyone `x`")
    check(guild.created_roles[-1].name == "everyone x",
          f"入力から危険な記号が除かれる ({guild.created_roles[-1].name})")
    # 色の形式が違えば購入を拒否し、代金は引かれない
    bal_before = await bot.db.get_balance(G, shopper.id)
    try:
        await buy(custom_id, value="色テスト", color="あか")
        check(False, "不正な色が通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.ITEM_INPUT_INVALID,
              f"不正な色は拒否する ({exc.code})")
    check(await bot.db.get_balance(G, shopper.id) == bal_before,
          "拒否された購入では残高が減らない (自動返金)")
    # 返金するとロールも消える
    await bot.charge.refund_shop_purchase(
        int(result["purchase_id"]), operator_id=OWNER_ID, reason="テスト返金"
    )
    check(created.deleted and guild.get_role(created.id) is None,
          "返金で作成したロールが削除される")
    left = await bot.db.list_purchase_assets(int(result["purchase_id"]))
    check(not left, "片付け済みの作成物は未処理として残らない")

    # --- ニックネーム: 変更と復元 ---
    shopper.nick = "もとの名前"
    nick_id = await bot.db.add_shop_item(
        guild_id=G, role_id=0, name="改名権", price=500, duration_days=1,
        item_type=config.ShopItemType.NICKNAME, created_by=OWNER_ID,
        purchase_limit=0,
    )
    nick_result = await buy(nick_id, value="あたらしい名前")
    check(shopper.nick == "あたらしい名前", f"ニックネームが変わる ({shopper.nick})")
    nick_assets = await bot.db.list_purchase_assets(int(nick_result["purchase_id"]))
    check(nick_assets and str(nick_assets[0]["detail"]) == "もとの名前",
          "変更前のニックネームが保存される")
    await bot.db.execute(
        "UPDATE shop_purchases SET expires_at=? WHERE id=?",
        (utils.now_ts() - 10, int(nick_result["purchase_id"])),
    )
    await bot.charge.expire_shop_purchases()
    check(shopper.nick == "もとの名前", f"期限切れで元の名前に戻る ({shopper.nick})")

    # --- チャージ率ブースト ---
    boost_id = await bot.db.add_shop_item(
        guild_id=G, role_id=0, name="率ブースト(+20/24h)", price=2_000,
        duration_days=1, item_type=config.ShopItemType.RATE_BOOST,
        payload={"bonus_rate": "20", "hours": 24}, created_by=OWNER_ID,
        purchase_limit=0,
    )
    settings = await bot.db.get_settings(G)
    base_rate, _ = await bot.charge.resolve_charge_rate(G, shopper.id, settings)
    boost_result = await buy(boost_id)
    boosted, bonus = await bot.charge.apply_rate_boost(G, shopper.id, base_rate)
    check(boosted == base_rate + Decimal("20") and bonus == Decimal("20"),
          f"ブースト分だけ率が上がる ({base_rate} → {boosted})")
    # 2つ持っても合算しない (最大の1つだけ)
    big_id = await bot.db.add_shop_item(
        guild_id=G, role_id=0, name="率ブースト(+30/24h)", price=3_000,
        duration_days=1, item_type=config.ShopItemType.RATE_BOOST,
        payload={"bonus_rate": "30", "hours": 24}, created_by=OWNER_ID,
        purchase_limit=0,
    )
    await buy(big_id)
    boosted2, bonus2 = await bot.charge.apply_rate_boost(G, shopper.id, base_rate)
    check(boosted2 == base_rate + Decimal("30") and bonus2 == Decimal("30"),
          f"複数のブーストは合算せず最大を使う ({boosted2})")
    # 実際のチャージ開始でも反映され、取引に保存される
    tx_id, _amount, _s = await bot.charge.start_charge(G, shopper.id, "1000")
    tx_row = await bot.db.get_transaction(tx_id)
    check(utils.to_decimal(tx_row["charge_rate"]) == base_rate + Decimal("30"),
          f"取引にブースト後の率が保存される ({tx_row['charge_rate']})")
    await bot.charge.cancel_transaction(tx_id, shopper.id)
    # 返金でブーストが無効になる
    await bot.charge.refund_shop_purchase(
        int(boost_result["purchase_id"]), operator_id=OWNER_ID, reason="テスト返金"
    )
    remaining = await bot.db.list_active_rate_boosts(G, shopper.id)
    check(all(int(r["purchase_id"]) != int(boost_result["purchase_id"])
              for r in remaining),
          "返金したブーストは無効になる")

    # --- 専用チャンネル ---
    channel_item = await bot.db.add_shop_item(
        guild_id=G, role_id=0, name="専用チャンネル", price=5_000,
        duration_days=1, item_type=config.ShopItemType.PRIVATE_CHANNEL,
        created_by=OWNER_ID,
    )
    ch_result = await buy(channel_item, value="わたしの部屋")
    made = guild.created_channels[-1]
    check(made.id == int(ch_result["channel_id"]), "専用チャンネルが作られる")
    check("わたしの部屋" in made.name or "-" in made.name,
          f"入力がチャンネル名に反映される ({made.name})")
    check(guild.default_role in made.overwrites
          and made.overwrites[guild.default_role].view_channel is False,
          "@everyone からは見えない設定になる")
    check(shopper in made.overwrites
          and made.overwrites[shopper].view_channel is True,
          "購入者だけが見られる")
    await bot.db.execute(
        "UPDATE shop_purchases SET expires_at=? WHERE id=?",
        (utils.now_ts() - 10, int(ch_result["purchase_id"])),
    )
    await bot.charge.expire_shop_purchases()
    check(made.deleted, "期限切れでチャンネルが削除される")

    # --- 設定不備の商品は購入前に拒否する ---
    broken = await bot.db.add_shop_item(
        guild_id=G, role_id=0, name="設定不備ブースト", price=100,
        duration_days=1, item_type=config.ShopItemType.RATE_BOOST,
        payload=None, created_by=OWNER_ID,
    )
    bal_before = await bot.db.get_balance(G, shopper.id)
    try:
        await buy(broken)
        check(False, "設定不備の商品が購入できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.SHOP_ITEM_UNAVAILABLE,
              f"設定不備の商品は購入前に拒否 ({exc.code})")
    check(await bot.db.get_balance(G, shopper.id) == bal_before,
          "拒否された時点で残高は動かない")
    missing_role = await bot.db.add_shop_item(
        guild_id=G, role_id=999_111, name="消えたロール", price=100,
        item_type=config.ShopItemType.ROLE, created_by=OWNER_ID,
    )
    row = await bot.db.get_shop_item(missing_role, G)
    check(bot.charge.shop_item_problem(guild, row) is not None,
          "存在しないロールの商品は問題として検出される")

    print("\n=== 5. サブスク (自動更新) ===")
    sub_item = await bot.db.add_shop_item(
        guild_id=G, role_id=0, name="月額ブースト", price=1_000,
        duration_days=30, item_type=config.ShopItemType.RATE_BOOST,
        payload={"bonus_rate": "5", "hours": 720}, subscription=True,
        created_by=OWNER_ID, purchase_limit=0,
    )
    sub = await buy(sub_item)
    purchase_id = int(sub["purchase_id"])
    row = await bot.db.get_purchase(purchase_id)
    check(bool(row["subscription"]) and row["next_charge_at"] == row["expires_at"],
          "購入時に次回課金日が期限と同じになる")

    # 予告 DM は更新日の1日前から、1回だけ
    await bot.db.execute(
        "UPDATE shop_purchases SET next_charge_at=?, expires_at=? WHERE id=?",
        (utils.now_ts() + 3600, utils.now_ts() + 3600, purchase_id),
    )
    bot.dms.clear()
    await bot.charge.run_subscriptions()
    notices = [d for d in bot.dms if "自動更新" in d[1]]
    check(len(notices) == 1, f"更新予告の DM が届く ({len(notices)}件)")
    bot.dms.clear()
    await bot.charge.run_subscriptions()
    check(not [d for d in bot.dms if "お知らせ" in d[1]], "予告は1回だけ送る")

    # 更新日を過ぎたら自動で引き落とす
    await bot.db.execute(
        "UPDATE shop_purchases SET next_charge_at=?, expires_at=? WHERE id=?",
        (utils.now_ts() - 5, utils.now_ts() - 5, purchase_id),
    )
    bal_before = await bot.db.get_balance(G, shopper.id)
    bot.dms.clear()
    stats = await bot.charge.run_subscriptions()
    row = await bot.db.get_purchase(purchase_id)
    check(stats["renewed"] == 1, f"自動更新が1件行われる ({stats})")
    check(await bot.db.get_balance(G, shopper.id) == bal_before - 1_000,
          "更新料が残高から引かれる")
    check(int(row["renewal_count"]) == 1, f"更新回数が増える ({row['renewal_count']})")
    check(int(row["expires_at"]) > utils.now_ts(), "期限が先に延びる")
    check(int(row["next_charge_at"]) == int(row["expires_at"]),
          "次回課金日も一緒に延びる")

    # 同じ更新を2回処理しない
    bal_before = await bot.db.get_balance(G, shopper.id)
    again = await bot.charge.run_subscriptions()
    check(again["renewed"] == 0 and
          await bot.db.get_balance(G, shopper.id) == bal_before,
          "更新日が来ていなければ二重課金しない")

    # 同時に走っても1回だけ課金する
    await bot.db.execute(
        "UPDATE shop_purchases SET next_charge_at=?, expires_at=? WHERE id=?",
        (utils.now_ts() - 5, utils.now_ts() - 5, purchase_id),
    )
    bal_before = await bot.db.get_balance(G, shopper.id)
    outcomes = await asyncio.gather(*[
        bot.db.renew_subscription(purchase_id) for _ in range(8)
    ])
    ok_count = sum(1 for o in outcomes if o.get("renewed"))
    check(ok_count == 1, f"同時8件でも更新は1回だけ ({ok_count}件)")
    check(await bot.db.get_balance(G, shopper.id) == bal_before - 1_000,
          "同時実行でも引き落としは1回")

    # 残高不足なら終了し、特典も取り消す
    await bot.db.execute(
        "UPDATE shop_purchases SET next_charge_at=?, expires_at=? WHERE id=?",
        (utils.now_ts() - 5, utils.now_ts() - 5, purchase_id),
    )
    current = await bot.db.get_balance(G, shopper.id)
    await bot.db.adjust_balance(
        guild_id=G, user_id=shopper.id, amount=current,
        change_type=config.BalanceChangeType.ADMIN_REMOVE,
        operator_id=OWNER_ID, reason="残高不足を作る",
    )
    bot.dms.clear()
    stats = await bot.charge.run_subscriptions()
    row = await bot.db.get_purchase(purchase_id)
    check(stats["failed"] == 1, f"残高不足で更新が止まる ({stats})")
    check(not bool(row["subscription"]) and row["next_charge_at"] is None,
          "自動更新が解除される")
    check(str(row["status"]) == config.PurchaseStatus.EXPIRED,
          f"購入が終了状態になる ({row['status']})")
    check(await bot.db.get_balance(G, shopper.id) == 0,
          "残高不足のときはマイナスにしない")
    check(any("終了" in d[1] for d in bot.dms), "終了を DM で知らせる")
    boosts = await bot.db.list_active_rate_boosts(G, shopper.id)
    check(all(int(b["purchase_id"] or 0) != purchase_id for b in boosts),
          "終了と同時に特典 (ブースト) も取り消す")

    # 自動更新の解約: 期限までは有効なまま
    await bot.db.adjust_balance(
        guild_id=G, user_id=shopper.id, amount=100_000,
        change_type=config.BalanceChangeType.ADMIN_ADD,
        operator_id=OWNER_ID, reason="テスト用",
    )
    sub2 = await buy(sub_item)
    pid2 = int(sub2["purchase_id"])
    cancelled = await bot.charge.cancel_subscription(
        G, pid2, user_id=shopper.id, operator_id=shopper.id
    )
    row = await bot.db.get_purchase(pid2)
    check(cancelled["purchase_id"] == pid2 and not bool(row["subscription"]),
          "解約で自動更新が止まる")
    check(str(row["status"]) == config.PurchaseStatus.ACTIVE and row["expires_at"],
          "解約しても期限までは有効")
    # 他人の購入は解約できない
    try:
        await bot.charge.cancel_subscription(
            G, pid2, user_id=63_999, operator_id=63_999
        )
        check(False, "他人の自動更新を解約できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.SUBSCRIPTION_NOT_FOUND,
              f"他人の購入は解約できない ({exc.code})")
    # 商品が販売停止になったら更新しない
    sub3 = await buy(sub_item)
    pid3 = int(sub3["purchase_id"])
    await bot.db.update_shop_item(sub_item, G, active=0)
    await bot.db.execute(
        "UPDATE shop_purchases SET next_charge_at=?, expires_at=? WHERE id=?",
        (utils.now_ts() - 5, utils.now_ts() - 5, pid3),
    )
    outcome = await bot.db.renew_subscription(pid3)
    check(not outcome["renewed"] and outcome["reason"] == "ITEM_UNAVAILABLE",
          f"販売停止中の商品は更新しない ({outcome['reason']})")

    print("\n=== 6. オークション ===")
    prize = guild.add_role(StubRole(64_001, "レア称号", position=6))
    bidder_a = guild.add_member(StubMember(64_101, guild))
    bidder_b = guild.add_member(StubMember(64_102, guild))
    bidder_c = guild.add_member(StubMember(64_103, guild))
    for who, amount in ((bidder_a, 50_000), (bidder_b, 50_000), (bidder_c, 1_000)):
        await bot.db.adjust_balance(
            guild_id=G, user_id=who.id, amount=amount,
            change_type=config.BalanceChangeType.ADMIN_ADD,
            operator_id=OWNER_ID, reason="オークション用",
        )
    async def total_balance() -> int:
        """3人の残高合計 (お金が増減していないかの確認に使う)。"""
        members = [bidder_a, bidder_b, bidder_c]
        members += [m for m in (guild.get_member(64_104),) if m is not None]
        values = [await bot.db.get_balance(G, w.id) for w in members]
        return sum(values)

    # 投入した残高の合計。以降「残高 + 預かり額」がこの値と一致することを見る。
    injected_total = await total_balance()

    created = await bot.charge.create_auction(
        guild, name="レア称号オークション", role=prize, start_price=1_000,
        min_increment=500, hours=1.0, duration_days=0, description="テスト",
        created_by=OWNER_ID,
    )
    auction_id = int(created["auction_id"])
    message = await guild.channel.send(embed=discord.Embed(title="panel"))
    await bot.db.set_auction_message(
        auction_id, channel_id=guild.channel.id, message_id=message.id
    )

    # 開始価格未満は入札できない
    try:
        await bot.charge.place_bid(bidder_a, auction_id, 500)
        check(False, "開始価格未満で入札できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.BID_TOO_LOW,
              f"開始価格未満は拒否 ({exc.code})")

    # 最初の入札: その場で残高を預かる
    bal_a = await bot.db.get_balance(G, bidder_a.id)
    first = await bot.charge.place_bid(bidder_a, auction_id, 1_000)
    check(await bot.db.get_balance(G, bidder_a.id) == bal_a - 1_000,
          "入札した分が残高から引かれる (預かり)")
    check(first["refunded"] is None, "最初の入札では誰にも返金しない")

    # 同じ人が最高額のまま重ねて入札できない
    try:
        await bot.charge.place_bid(bidder_a, auction_id, 2_000)
        check(False, "最高額の本人が続けて入札できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.ALREADY_HIGHEST,
              f"最高額の本人は入札できない ({exc.code})")

    # 最低更新額を満たさない入札は拒否
    try:
        await bot.charge.place_bid(bidder_b, auction_id, 1_200)
        check(False, "最低更新額未満で入札できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.BID_TOO_LOW,
              f"最低更新額 (現在額+500) 未満は拒否 ({exc.code})")

    # 上回る入札: 前の人へその場で全額返金
    bal_a_held = await bot.db.get_balance(G, bidder_a.id)
    bal_b = await bot.db.get_balance(G, bidder_b.id)
    bot.dms.clear()
    second = await bot.charge.place_bid(bidder_b, auction_id, 1_500)
    check(await bot.db.get_balance(G, bidder_a.id) == bal_a_held + 1_000,
          "上回られた人へ全額返金される")
    check(await bot.db.get_balance(G, bidder_b.id) == bal_b - 1_500,
          "新しい入札者から預かる")
    check(second["refunded"] and int(second["refunded"]["user_id"]) == bidder_a.id,
          "返金先が正しい")
    check(any("上回られました" in d[1] for d in bot.dms),
          "上回られたことを DM で知らせる")

    # 残高不足では入札できない (預かり方式なので事前に弾ける)
    try:
        await bot.charge.place_bid(bidder_c, auction_id, 40_000)
        check(False, "残高不足で入札できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.INSUFFICIENT_BALANCE,
              f"残高不足は拒否 ({exc.code})")

    # 同時入札 (別人が同額) でも預かり額の合計が壊れない
    bidder_d = guild.add_member(StubMember(64_104, guild))
    await bot.db.adjust_balance(
        guild_id=G, user_id=bidder_d.id, amount=50_000,
        change_type=config.BalanceChangeType.ADMIN_ADD,
        operator_id=OWNER_ID, reason="オークション用",
    )
    injected_total += 50_000
    results = await asyncio.gather(
        bot.charge.place_bid(bidder_a, auction_id, 2_000),
        bot.charge.place_bid(bidder_d, auction_id, 2_000),
        bot.charge.place_bid(bidder_a, auction_id, 2_000),
        return_exceptions=True,
    )
    accepted = [r for r in results if not isinstance(r, BaseException)]
    check(len(accepted) == 1,
          f"同額の同時入札は1件しか通らない ({len(accepted)}件)")
    codes = {r.code for r in results if isinstance(r, ChargeError)}
    check(codes <= {config.ErrorCode.BID_TOO_LOW, config.ErrorCode.ALREADY_HIGHEST},
          f"通らなかった入札は理由が説明される ({codes})")
    # 通らなかった入札では残高が動いていない
    loser = bidder_d if int(accepted[0]["bid_id"]) and (
        await bot.db.get_balance(G, bidder_d.id)) == 50_000 else bidder_a
    check(await bot.db.get_balance(G, loser.id) in (50_000, 48_000, 47_000, 46_000),
          "拒否された入札で残高が中途半端に減らない")
    auction_row = await bot.db.get_auction(auction_id, G)
    held = int(auction_row["current_bid"])
    check(int(auction_row["current_bidder"]) == bidder_a.id,
          "最高額の入札者が記録されている")
    # 預かり中の入札は「最高額の1件」だけ
    bids = await bot.db.list_auction_bids(auction_id, limit=50)
    unrefunded = [b for b in bids if not int(b["refunded"])]
    check(len(unrefunded) == 1 and int(unrefunded[0]["amount"]) == held,
          f"預かり中の入札は最高額の1件だけ ({len(unrefunded)}件)")
    # 残高 + 預かり額 = 最初の総額 (お金が増減していない)
    total_now = await total_balance()
    check(total_now + held == injected_total,
          f"残高と預かり額の合計が保たれる ({total_now} + {held} = {injected_total})")

    # 締切前の入札で締切が延長される
    await bot.db.execute(
        "UPDATE auctions SET ends_at=? WHERE id=?",
        (utils.now_ts() + 30, auction_id),
    )
    ext = await bot.charge.place_bid(bidder_b, auction_id, held + 500)
    check(ext["extended"] and int(ext["ends_at"]) > utils.now_ts() + 30,
          "締切直前の入札で締切が延長される")

    # 締切 → 落札
    await bot.db.execute(
        "UPDATE auctions SET ends_at=? WHERE id=?", (utils.now_ts() - 1, auction_id)
    )
    bal_winner = await bot.db.get_balance(G, bidder_b.id)
    bot.dms.clear()
    closed = await bot.charge.close_due_auctions()
    auction_row = await bot.db.get_auction(auction_id, G)
    check(closed == 1 and str(auction_row["status"]) == config.AuctionStatus.CLOSED,
          f"締切で落札が確定する ({auction_row['status']})")
    check(int(auction_row["winner_id"]) == bidder_b.id, "落札者が記録される")
    check(prize in bidder_b.roles, "落札者に景品のロールが付く")
    check(await bot.db.get_balance(G, bidder_b.id) == bal_winner,
          "落札時に追加の引き落としは無い (入札時に預かっている)")
    check(any("落札しました" in d[1] for d in bot.dms), "落札を DM で知らせる")

    # 締切済みのオークションには入札できない
    try:
        await bot.charge.place_bid(bidder_a, auction_id, 999_999)
        check(False, "終了後に入札できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.AUCTION_NOT_OPEN,
              f"終了後の入札は拒否 ({exc.code})")

    # 二重に締め切っても何も起きない
    again = await bot.db.close_auction(auction_id, force=True)
    check(not again["closed"] and again["reason"] == "NOT_DUE",
          "同じオークションを二重に締め切らない")

    # --- 入札なしで終了 ---
    empty = await bot.charge.create_auction(
        guild, name="入札なし", role=prize, start_price=100, min_increment=100,
        hours=1.0, created_by=OWNER_ID,
    )
    empty_id = int(empty["auction_id"])
    await bot.db.execute(
        "UPDATE auctions SET ends_at=? WHERE id=?", (utils.now_ts() - 1, empty_id)
    )
    await bot.charge.close_due_auctions()
    row = await bot.db.get_auction(empty_id, G)
    check(str(row["status"]) == config.AuctionStatus.FAILED,
          f"入札が無ければ FAILED になる ({row['status']})")

    # --- 中止すると預かり分を返す ---
    cancel_target = await bot.charge.create_auction(
        guild, name="中止テスト", role=prize, start_price=1_000, min_increment=100,
        hours=1.0, created_by=OWNER_ID,
    )
    cancel_id = int(cancel_target["auction_id"])
    await bot.charge.place_bid(bidder_a, cancel_id, 1_000)
    bal_before_cancel = await bot.db.get_balance(G, bidder_a.id)
    bot.dms.clear()
    cancel_result = await bot.charge.cancel_auction(
        G, cancel_id, operator_id=OWNER_ID, reason="テストのため中止"
    )
    check(await bot.db.get_balance(G, bidder_a.id) == bal_before_cancel + 1_000,
          "中止で預かり分が返る")
    check(cancel_result["refunded"], "返金の記録が返る")
    check(any("中止" in d[1] for d in bot.dms), "中止を DM で知らせる")
    row = await bot.db.get_auction(cancel_id, G)
    check(str(row["status"]) == config.AuctionStatus.CANCELLED, "状態が中止になる")
    # 二重に中止しても二重返金しない
    bal_after_cancel = await bot.db.get_balance(G, bidder_a.id)
    try:
        await bot.charge.cancel_auction(
            G, cancel_id, operator_id=OWNER_ID, reason="二重中止"
        )
        check(False, "二重に中止できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.AUCTION_NOT_OPEN,
              f"終了済みは中止できない ({exc.code})")
    check(await bot.db.get_balance(G, bidder_a.id) == bal_after_cancel,
          "二重返金は起きない")

    # --- 景品を渡せなければ落札額を返して中止にする ---
    undeliverable = await bot.charge.create_auction(
        guild, name="渡せない景品", role=prize, start_price=1_000,
        min_increment=100, hours=1.0, created_by=OWNER_ID,
    )
    bad_id = int(undeliverable["auction_id"])
    await bot.charge.place_bid(bidder_a, bad_id, 1_000)
    await bot.db.execute(
        "UPDATE auctions SET ends_at=? WHERE id=?", (utils.now_ts() - 1, bad_id)
    )
    bidder_a.roles = [r for r in bidder_a.roles if r.id != prize.id]
    bidder_a.fail_add_roles = True
    bal_before_fail = await bot.db.get_balance(G, bidder_a.id)
    bot.owner_alerts.clear()
    bot.dms.clear()
    outcome = await bot.charge.close_auction(bad_id)
    bidder_a.fail_add_roles = False
    row = await bot.db.get_auction(bad_id, G)
    check(outcome.get("delivered") is False, "景品を渡せなかったことが分かる")
    check(await bot.db.get_balance(G, bidder_a.id) == bal_before_fail + 1_000,
          "渡せなければ落札額を返金する")
    check(str(row["status"]) == config.AuctionStatus.CANCELLED,
          f"渡せなかったオークションは中止扱い ({row['status']})")
    check(bot.owner_alerts or bot.dms, "管理者か落札者へ知らせる")

    # --- 期限つきの落札ロールは期限で剥がれる ---
    timed_auction = await bot.charge.create_auction(
        guild, name="期間つき称号", role=prize, start_price=1_000, min_increment=100,
        hours=1.0, duration_days=7, created_by=OWNER_ID,
    )
    timed_id = int(timed_auction["auction_id"])
    await bot.charge.place_bid(bidder_b, timed_id, 1_000)
    await bot.db.execute(
        "UPDATE auctions SET ends_at=? WHERE id=?", (utils.now_ts() - 1, timed_id)
    )
    await bot.charge.close_auction(timed_id)
    row = await bot.db.get_auction(timed_id, G)
    check(row["role_expires_at"] and int(row["role_expires_at"]) > utils.now_ts(),
          "期間つきの落札ではロールの期限が入る")
    check(prize in bidder_b.roles, "落札直後はロールを持っている")
    await bot.db.execute(
        "UPDATE auctions SET role_expires_at=? WHERE id=?",
        (utils.now_ts() - 1, timed_id),
    )
    handled = await bot.charge.expire_auction_roles()
    check(handled == 1 and prize not in bidder_b.roles,
          f"期限が切れたらロールを剥がす ({handled}件)")
    check(await bot.charge.expire_auction_roles() == 0,
          "同じ期限切れを繰り返し処理しない")

    # --- 同時開催数の上限 ---
    made = []
    while await bot.db.count_open_auctions(G) < config.MAX_OPEN_AUCTIONS:
        extra = await bot.charge.create_auction(
            guild, name=f"枠テスト{len(made)}", role=prize, start_price=100,
            min_increment=100, hours=1.0, created_by=OWNER_ID,
        )
        made.append(int(extra["auction_id"]))
    try:
        await bot.charge.create_auction(
            guild, name="あふれる", role=prize, start_price=100, min_increment=100,
            hours=1.0, created_by=OWNER_ID,
        )
        check(False, "同時開催数の上限を超えられてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.AUCTION_LIMIT_REACHED,
              f"同時開催数の上限を守る ({exc.code})")
    for extra_id in made:
        await bot.charge.cancel_auction(
            G, extra_id, operator_id=OWNER_ID, reason="後片付け"
        )

    # --- 開催時間が短すぎる / 渡せないロール ---
    try:
        await bot.charge.create_auction(
            guild, name="短すぎる", role=prize, start_price=100, min_increment=100,
            hours=0.01, created_by=OWNER_ID,
        )
        check(False, "短すぎる開催時間が通ってしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.INVALID_AMOUNT,
              f"短すぎる開催時間は拒否 ({exc.code})")
    high_role = guild.add_role(StubRole(64_900, "管理者より上", position=200))
    try:
        await bot.charge.create_auction(
            guild, name="渡せないロール", role=high_role, start_price=100,
            min_increment=100, hours=1.0, created_by=OWNER_ID,
        )
        check(False, "付与できないロールで開催できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.ROLE_ASSIGN_FAILED,
              f"付与できないロールでは開催できない ({exc.code})")

    # --- すべて終わった後、預かり金が残っていないこと ---
    open_left = await bot.db.count_open_auctions(G)
    leftover = await bot.db.fetchall(
        "SELECT * FROM auction_bids WHERE guild_id=? AND refunded=0", (G,)
    )
    closed_winners = await bot.db.fetchall(
        "SELECT winning_bid FROM auctions WHERE guild_id=? AND status=? "
        "AND winning_bid IS NOT NULL",
        (G, config.AuctionStatus.CLOSED),
    )
    won_total = sum(int(r["winning_bid"]) for r in closed_winners)
    held_total = sum(int(b["amount"]) for b in leftover)
    check(open_left == 0, f"開催中のオークションが残っていない ({open_left}件)")
    check(held_total == won_total,
          f"預かり中の残りは落札分だけ (預かり {held_total} / 落札 {won_total})")
    final_total = await total_balance()
    check(final_total + won_total == injected_total,
          f"最後まで残高と落札額の合計が保たれる "
          f"({final_total} + {won_total} = {injected_total})")

    print("\n=== 7. サーバー全体のチャージ目標 ===")
    goal_role = guild.add_role(StubRole(65_001, "目標達成者", position=6))
    g1 = guild.add_member(StubMember(65_101, guild))
    g2 = guild.add_member(StubMember(65_102, guild))
    g3 = guild.add_member(StubMember(65_103, guild))

    # 他のテストで作ったチャージと混ざらないよう、集計期間を明確に分ける。
    # (集計開始をこの時刻にそろえ、参加分だけをこの時刻より後に作る)
    goal_window = utils.now_ts() + 3_600

    async def open_goal(**kwargs: Any) -> int:
        """目標を作り、集計開始を goal_window にそろえる。"""
        created = await bot.charge.create_goal(guild, **kwargs)
        new_id = int(created["goal_id"])
        await bot.db.execute(
            "UPDATE charge_goals SET starts_at=? WHERE id=?", (goal_window, new_id)
        )
        return new_id

    # 目標より前のチャージは数えない (期間の境界)
    await give_charge(g1.id, 10_000, when=goal_window - 60)

    goal_id = await open_goal(
        name="みんなで10万円", target_amount=100_000, reward_amount=500,
        reward_role=goal_role, days=7.0, created_by=OWNER_ID,
    )
    goal_row = await bot.db.get_goal(goal_id, G)
    assert goal_row is not None
    progress = await bot.db.goal_progress(goal_row)
    check(progress["total"] == 0,
          f"開始前のチャージは進捗に入らない ({progress['total']})")

    # 同時に2つは作れない
    try:
        await bot.charge.create_goal(
            guild, name="二重目標", target_amount=1_000, reward_amount=100,
            reward_role=None, days=1.0, created_by=OWNER_ID,
        )
        check(False, "目標を同時に2つ作れてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.GOAL_ALREADY_OPEN,
              f"集計中の目標は1つだけ ({exc.code})")

    # 報酬が無い目標は作れない
    await bot.db.close_goal(goal_id, status=config.GoalStatus.CANCELLED)
    try:
        await bot.charge.create_goal(
            guild, name="報酬なし", target_amount=1_000, reward_amount=0,
            reward_role=None, days=1.0, created_by=OWNER_ID,
        )
        check(False, "報酬なしの目標が作れてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.INVALID_AMOUNT,
              f"報酬のない目標は作れない ({exc.code})")

    # 本番の目標を作り直す
    goal_id = await open_goal(
        name="みんなで3万円", target_amount=30_000, reward_amount=500,
        reward_role=goal_role, days=7.0, created_by=OWNER_ID,
    )

    # 進捗が積み上がる (未達のうちは報酬を配らない)
    await give_charge(g1.id, 10_000, when=goal_window + 60)
    await give_charge(g2.id, 5_000, when=goal_window + 120)
    goal_row = await bot.db.get_goal(goal_id, G)
    progress = await bot.db.goal_progress(goal_row)  # type: ignore[arg-type]
    check(progress["total"] == 15_000 and progress["users"] == 2,
          f"期間内のチャージが合計される ({progress['total']} / {progress['users']}人)")
    outcome = await bot.charge.check_guild_goal(G)
    goal_row = await bot.db.get_goal(goal_id, G)
    check(str(goal_row["status"]) == config.GoalStatus.OPEN,  # type: ignore[index]
          "未達のうちは集計中のまま")
    check(not await bot.db.list_goal_grants(goal_id),
          "未達では報酬を配らない")
    check(outcome.get("refreshed", 0) >= 0, "進捗の確認で例外が出ない")

    # 目標に到達 → 期間内にチャージした全員へ配布
    await give_charge(g3.id, 20_000, when=goal_window + 180)
    bal_before = {
        m.id: await bot.db.get_balance(G, m.id) for m in (g1, g2, g3)
    }
    bot.dms.clear()
    result = await bot.charge.check_goals()
    goal_row = await bot.db.get_goal(goal_id, G)
    assert goal_row is not None
    check(result["achieved"] == 1
          and str(goal_row["status"]) == config.GoalStatus.ACHIEVED,
          f"目標額に届いたら達成になる ({goal_row['status']})")
    check(int(goal_row["achieved_total"]) == 35_000,
          f"達成時の到達額が記録される ({goal_row['achieved_total']})")
    grants = await bot.db.list_goal_grants(goal_id)
    check(len(grants) == 3, f"期間内にチャージした全員へ配布 ({len(grants)}人)")
    for m in (g1, g2, g3):
        got = await bot.db.get_balance(G, m.id) - bal_before[m.id]
        check(got == 500, f"{m.id} に報酬が入る ({got})")
        check(goal_role in m.roles, f"{m.id} に報酬ロールが付く")
    check(len([d for d in bot.dms if "達成報酬" in d[1]]) == 3,
          "達成を全員へ DM で知らせる")

    # 二重配布しない
    bal_after = {m.id: await bot.db.get_balance(G, m.id) for m in (g1, g2, g3)}
    again = await bot.charge.distribute_goal_rewards(goal_id)
    check(again["granted"] == 0, f"同じ目標で二重配布しない ({again['granted']}人)")
    for m in (g1, g2, g3):
        check(await bot.db.get_balance(G, m.id) == bal_after[m.id],
              f"{m.id} の残高が二重に増えない")
    # 達成済みの目標は再達成できない
    check(not await bot.db.mark_goal_achieved(goal_id, total=999_999),
          "達成済みの目標を二重に達成できない")
    # 達成後は新しい目標を作れる
    next_id = await open_goal(
        name="次の目標", target_amount=1_000_000, reward_amount=100,
        reward_role=None, days=1.0, created_by=OWNER_ID,
    )
    check(next_id != goal_id, "達成後は次の目標を開始できる")

    # 凍結された利用者は配布対象から外れる
    await bot.db.set_frozen(G, g2.id, True, OWNER_ID, "テスト凍結")
    frozen_goal_row = await bot.db.get_goal(next_id, G)
    participants = await bot.db.list_goal_participants(frozen_goal_row)  # type: ignore[arg-type]
    check(all(int(p["user_id"]) != g2.id for p in participants),
          "凍結された利用者は配布対象に入らない")
    await bot.db.set_frozen(G, g2.id, False, OWNER_ID, "解除")

    # 期限切れ (未達) は報酬なしで終了する
    await bot.db.execute(
        "UPDATE charge_goals SET ends_at=? WHERE id=?",
        (utils.now_ts() - 5, next_id),
    )
    closed = await bot.charge.check_goals()
    row = await bot.db.get_goal(next_id, G)
    check(closed["closed"] == 1 and str(row["status"]) == config.GoalStatus.CLOSED,  # type: ignore[index]
          f"期限切れで未達なら終了する ({row['status']})")  # type: ignore[index]
    check(not await bot.db.list_goal_grants(next_id), "未達では報酬を配らない (期限切れ)")

    # 手動で締める: 達成していれば報酬を配る
    manual_id = await open_goal(
        name="手動締め", target_amount=1_000, reward_amount=200,
        reward_role=None, days=7.0, created_by=OWNER_ID,
    )
    await give_charge(g1.id, 2_000, when=goal_window + 240)
    bal_before_manual = await bot.db.get_balance(G, g1.id)
    manual_result = await bot.charge.close_goal(
        G, manual_id, operator_id=OWNER_ID, cancel=False
    )
    check(manual_result["status"] == config.GoalStatus.ACHIEVED,
          f"目標額に届いていれば手動でも達成扱い ({manual_result['status']})")
    check(await bot.db.get_balance(G, g1.id) > bal_before_manual,
          "手動達成でも報酬が入る")

    # 中止では報酬を配らない
    cancel_goal_id = await open_goal(
        name="中止する目標", target_amount=1_000, reward_amount=300,
        reward_role=None, days=7.0, created_by=OWNER_ID,
    )
    await give_charge(g1.id, 5_000, when=goal_window + 300)
    bal_before_cancel_goal = await bot.db.get_balance(G, g1.id)
    cancel_result = await bot.charge.close_goal(
        G, cancel_goal_id, operator_id=OWNER_ID, cancel=True
    )
    check(cancel_result["status"] == config.GoalStatus.CANCELLED,
          f"中止は達成にしない ({cancel_result['status']})")
    check(await bot.db.get_balance(G, g1.id) == bal_before_cancel_goal,
          "中止では報酬を配らない")
    check(not await bot.db.list_goal_grants(cancel_goal_id),
          "中止した目標に配布記録は残らない")
    # 終了済みの目標は締められない
    try:
        await bot.charge.close_goal(
            G, cancel_goal_id, operator_id=OWNER_ID, cancel=True
        )
        check(False, "終了済みの目標を二重に締められてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.GOAL_NOT_FOUND,
              f"終了済みの目標は締められない ({exc.code})")

    # 期限なしの目標は「いまの時点まで」を数える
    endless_id = await bot.charge.create_goal(
        guild, name="期限なし", target_amount=1_000_000, reward_amount=100,
        reward_role=None, days=None, created_by=OWNER_ID,
    )
    endless_row = await bot.db.get_goal(int(endless_id["goal_id"]), G)
    assert endless_row is not None
    check(endless_row["ends_at"] is None, "期限なしの目標は締切を持たない")
    # 既存のテストデータが入るため、増分で確かめる
    before_endless = (await bot.db.goal_progress(endless_row))["total"]
    await give_charge(g1.id, 3_000)
    after_endless = (await bot.db.goal_progress(endless_row))["total"]
    check(after_endless - before_endless == 3_000,
          f"期限なしでも現在までのチャージを数える "
          f"({before_endless} → {after_endless})")
    await bot.charge.close_goal(
        G, int(endless_row["id"]), operator_id=OWNER_ID, cancel=True  # type: ignore[index]
    )

    # 付与できないロールを報酬にはできない
    try:
        await bot.charge.create_goal(
            guild, name="渡せないロール", target_amount=1_000, reward_amount=0,
            reward_role=high_role, days=None, created_by=OWNER_ID,
        )
        check(False, "付与できないロールを報酬にできてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.ROLE_ASSIGN_FAILED,
              f"付与できないロールは報酬にできない ({exc.code})")

    print("\n=== 8. 不正検知 ===")
    await bot.charge.set_review_channel_id(guild.review.id)
    guild.review.sent.clear()

    # --- 短時間の大量チャージ ---
    burst_user = guild.add_member(StubMember(66_101, guild))
    for _ in range(config.FRAUD_BURST_COUNT):
        await give_charge(burst_user.id, 1_000)
    found = await bot.db.find_burst_chargers(
        G, window=config.FRAUD_BURST_WINDOW, minimum=config.FRAUD_BURST_COUNT
    )
    check(any(int(r["user_id"]) == burst_user.id for r in found),
          "短時間の大量チャージを見つける")
    outcome = await bot.charge.scan_guild_fraud(G)
    flags, total = await bot.db.list_fraud_flags(G, user_id=burst_user.id)
    check(outcome["flagged"] >= 1 and total >= 1,
          f"検知フラグが立つ ({outcome})")
    burst_flag = next(
        f for f in flags if str(f["kind"]) == config.FraudKind.BURST_CHARGE
    )
    check(str(burst_flag["status"]) == config.FraudStatus.OPEN, "初期状態は未処理")
    check(str(burst_flag["severity"]) in config.FRAUD_SEVERITY_LABELS,
          f"重要度が入る ({burst_flag['severity']})")
    check(any("検知" in (e.title or "") for e in guild.review.sent),
          "検知カードが審査チャンネルへ投稿される")
    check(burst_flag["message_id"] is not None, "カードのメッセージIDを保存する")

    # 同じ兆候を二重にフラグしない (内容だけ更新する)
    before_total = (await bot.db.list_fraud_flags(G, user_id=burst_user.id))[1]
    again = await bot.charge.scan_guild_fraud(G)
    after_total = (await bot.db.list_fraud_flags(G, user_id=burst_user.id))[1]
    check(after_total == before_total and again["updated"] >= 1,
          f"同じ兆候は重ねて立てず更新する (新規 {again['flagged']} / 更新 {again['updated']})")

    # 自動処分はしない
    user_row = await bot.db.get_user(G, burst_user.id)
    check(not (user_row and int(user_row["frozen"] or 0)),
          "検知しただけでは凍結しない (判断は人が行う)")

    # 処理すると状態が変わり、再検知で新しいフラグが立つ
    flag_id = int(burst_flag["id"])
    result = await bot.charge.review_fraud_flag(
        flag_id, status=config.FraudStatus.IGNORED, reviewed_by=OWNER_ID,
        note="テストのため問題なし",
    )
    reviewed = await bot.db.get_fraud_flag(flag_id, G)
    check(result["status"] == config.FraudStatus.IGNORED
          and str(reviewed["status"]) == config.FraudStatus.IGNORED,  # type: ignore[index]
          "問題なしにできる")
    check(int(reviewed["reviewed_by"]) == OWNER_ID and reviewed["reviewed_at"],  # type: ignore[index]
          "担当者と日時が残る")
    try:
        await bot.charge.review_fraud_flag(
            flag_id, status=config.FraudStatus.RESOLVED, reviewed_by=OWNER_ID
        )
        check(False, "処理済みの検知を二重に処理できてしまう")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.FRAUD_FLAG_NOT_FOUND,
              f"処理済みの検知は再処理できない ({exc.code})")
    rescan = await bot.charge.scan_guild_fraud(G)
    check(rescan["flagged"] >= 1,
          "処理済みのあとに同じ兆候が続けば新しく立てる")

    # --- 送金者名の共有 ---
    share_a = guild.add_member(StubMember(66_201, guild))
    share_b = guild.add_member(StubMember(66_202, guild))
    for who in (share_a, share_b):
        tx = await bot.db.create_proxy_transaction(
            guild_id=G, user_id=who.id, requested_amount=3_000,
            received_amount=3_000, charge_rate=Decimal("130"),
        )
        await bot.db.credit_transaction(tx, 3_900)
        await bot.db.execute(
            "UPDATE charge_transactions SET sender_name=? WHERE id=?",
            ("ヤマダ タロウ", tx),
        )
    shared = await bot.db.find_shared_senders(
        G, minimum_users=config.FRAUD_SHARED_SENDER_USERS
    )
    check(any(int(r["users"]) >= 2 for r in shared),
          f"同じ送金者名の共有を見つける ({len(shared)}件)")
    await bot.charge.scan_guild_fraud(G)
    for who in (share_a, share_b):
        rows, _ = await bot.db.list_fraud_flags(G, user_id=who.id)
        check(any(str(r["kind"]) == config.FraudKind.SHARED_SENDER for r in rows),
              f"{who.id} に送金者名共有の検知が立つ")
    share_flag = next(
        r for r in (await bot.db.list_fraud_flags(G, user_id=share_a.id))[0]
        if str(r["kind"]) == config.FraudKind.SHARED_SENDER
    )
    evidence = utils.load_json_dict(share_flag["evidence"])
    check("ヤマダ タロウ" not in str(share_flag["detail"])
          and "ヤマダ タロウ" not in str(evidence),
          "送金者名そのものは記録に残さない (第三者の氏名を広めない)")
    masked = str(evidence.get("sender", ""))
    check(masked.endswith("…") and len(masked) < len("ヤマダ タロウ"),
          f"送金者名は先頭だけ残して伏せる ({masked})")

    # --- 招待報酬のみ ---
    invite_only = guild.add_member(StubMember(66_301, guild))
    campaign_id = await bot.db.create_campaign(
        guild_id=G, name="検知テスト", inviter_reward=100, invited_reward=50,
        min_account_age_days=0, daily_limit=0, total_limit=0, require_charge=False,
        require_days=0, require_review=False, starts_at=None, ends_at=None,
        created_by=OWNER_ID,
    )
    for i in range(config.FRAUD_INVITE_ONLY_COUNT):
        record_id, _ = await bot.db.record_invite(
            guild_id=G, campaign_id=campaign_id, inviter_id=invite_only.id,
            invited_id=66_400 + i, code=f"code{i}",
            status=config.InviteStatus.PENDING, reason=None,
        )
        await bot.db.confirm_invite_and_reward(record_id)
    only_rows = await bot.db.find_invite_only_users(
        G, minimum=config.FRAUD_INVITE_ONLY_COUNT
    )
    check(any(int(r["user_id"]) == invite_only.id for r in only_rows),
          "チャージなしで招待報酬だけを集めている人を見つける")
    # チャージがある人は対象にならない
    check(all(int(r["user_id"]) != burst_user.id for r in only_rows),
          "チャージしている人は招待報酬のみの対象にしない")

    # --- チャージ直後に使い切って退出 ---
    drain = guild.add_member(StubMember(66_501, guild))
    drain_tx = await bot.db.create_proxy_transaction(
        guild_id=G, user_id=drain.id, requested_amount=10_000,
        received_amount=10_000, charge_rate=Decimal("100"),
    )
    await bot.db.credit_transaction(drain_tx, 10_000)
    await bot.db.adjust_balance(
        guild_id=G, user_id=drain.id, amount=9_500,
        change_type=config.BalanceChangeType.ADMIN_REMOVE,
        operator_id=OWNER_ID, reason="使い切りを再現",
    )
    # 支出として数えるのは SPEND 系なので、履歴の種別を差し替える
    await bot.db.execute(
        "UPDATE balance_history SET type=? WHERE guild_id=? AND user_id=? AND type=?",
        (config.BalanceChangeType.SPEND, G, drain.id,
         config.BalanceChangeType.ADMIN_REMOVE),
    )
    await bot.db.record_member_leave(G, drain.id)
    drained = await bot.db.find_drain_and_leave(G, window=config.FRAUD_DRAIN_WINDOW)
    check(any(int(r["user_id"]) == drain.id for r in drained),
          "退出前にチャージしていた人を候補に挙げる")
    spent = await bot.db.sum_spending(
        G, drain.id, since=utils.now_ts() - config.FRAUD_DRAIN_WINDOW,
        until=utils.now_ts(),
    )
    check(spent == 9_500, f"期間内の支出を合計できる ({spent})")
    await bot.charge.scan_guild_fraud(G)
    drain_rows, _ = await bot.db.list_fraud_flags(G, user_id=drain.id)
    check(any(str(r["kind"]) == config.FraudKind.DRAIN_AND_LEAVE for r in drain_rows),
          "使い切って退出した人に検知が立つ")
    # 使い切っていない人は検知しない
    keeper = guild.add_member(StubMember(66_502, guild))
    keep_tx = await bot.db.create_proxy_transaction(
        guild_id=G, user_id=keeper.id, requested_amount=10_000,
        received_amount=10_000, charge_rate=Decimal("100"),
    )
    await bot.db.credit_transaction(keep_tx, 10_000)
    await bot.db.record_member_leave(G, keeper.id)
    await bot.charge.scan_guild_fraud(G)
    keep_rows, _ = await bot.db.list_fraud_flags(G, user_id=keeper.id)
    check(all(str(r["kind"]) != config.FraudKind.DRAIN_AND_LEAVE for r in keep_rows),
          "残高を使っていなければ検知しない")

    # --- 1つのルールが壊れても他は動く ---
    broken_called = {"count": 0}

    async def broken_rule(_guild_id: int) -> list[Any]:
        broken_called["count"] += 1
        raise RuntimeError("テスト用の故障")

    original = bot.charge._detect_burst_charge  # type: ignore[attr-defined]
    bot.charge._detect_burst_charge = broken_rule  # type: ignore[assignment]
    safe_outcome = await bot.charge.scan_guild_fraud(G)
    bot.charge._detect_burst_charge = original  # type: ignore[assignment]
    check(broken_called["count"] == 1 and isinstance(safe_outcome, dict),
          "1つのルールが失敗しても検知全体は止まらない")

    # --- 一覧と件数 ---
    open_count = await bot.db.count_open_fraud_flags(G)
    all_rows, all_total = await bot.db.list_fraud_flags(G, status=None, limit=50)
    check(open_count > 0 and all_total >= open_count,
          f"未処理件数と全件数を数えられる (未処理 {open_count} / 全 {all_total})")
    high_first = [str(r["severity"]) for r in all_rows]
    check(high_first == sorted(
        high_first, key=lambda s: {"HIGH": 0, "WARN": 1, "INFO": 2}.get(s, 3)),
        "重要度の高い順に並ぶ")

    print("\n=== 9. 日別推移のグラフ ===")
    import chart as chart_module

    check(chart_module.available(), "Pillow が使える環境ではグラフを描ける")

    # --- 欠けた日を 0 で埋める ---
    filled = chart_module.build_daily_series(
        [{"day": "2026-09-20", "amount": 5_000, "count": 2, "credited": 6_500}],
        days=5, end_day="2026-09-22",
    )
    check(len(filled) == 5, f"指定した日数ぶん並ぶ ({len(filled)}点)")
    check([p.day for p in filled] == [
        "2026-09-18", "2026-09-19", "2026-09-20", "2026-09-21", "2026-09-22"
    ], "日付が連続して並ぶ")
    check(filled[2].amount == 5_000 and filled[0].amount == 0,
          "データのある日だけ値が入り、他は0になる")
    check(filled[2].credited == 6_500, "付与額も引き継ぐ")

    # --- 画像が作れる ---
    png = chart_module.render_daily_chart(
        filled, title="テスト推移", subtitle="日本語の字が入る"
    )
    check(bool(png) and png[:8] == b"\x89PNG\r\n\x1a\n",
          f"PNG 画像が返る ({len(png or b'')} バイト)")
    check(len(png or b"") < 8 * 1024 * 1024, "Discord の添付上限に収まる大きさ")

    # 全部ゼロでも落ちない
    zero = chart_module.render_daily_chart(
        [chart_module.DailyPoint(day="2026-09-22", amount=0, count=0)],
        title="データなし",
    )
    check(bool(zero), "データが無くても画像を返す (空であることを描く)")
    # 1点だけ・大量の点でも落ちない
    single = chart_module.render_daily_chart(
        [chart_module.DailyPoint(day="2026-09-22", amount=1_000, count=1)],
        title="1点",
    )
    check(bool(single), "1点だけでも描ける")
    many = chart_module.render_daily_chart(
        [chart_module.DailyPoint(day=f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                                 amount=i * 1_000, count=i % 20)
         for i in range(config.CHART_MAX_DAYS)],
        title="最大日数",
    )
    check(bool(many), f"最大日数 ({config.CHART_MAX_DAYS}日) でも描ける")
    check(chart_module.render_daily_chart([], title="空") is None,
          "点が1つも無ければ None を返す")

    # --- 目盛りの丸め ---
    check(chart_module._nice_step(0) == 1, "0でも目盛り幅が決まる")
    for maximum in (1, 9, 37, 1_234, 98_765, 12_345_678):
        step = chart_module._nice_step(maximum)
        check(step > 0 and maximum / step <= 20,
              f"目盛りが多すぎない (max={maximum} step={step})")

    # --- DB からの集計 ---
    chart_user = guild.add_member(StubMember(67_101, guild))
    await give_charge(chart_user.id, 4_000)
    rows = await bot.db.get_daily_series(G, days=7)
    check(bool(rows), f"日別集計が取れる ({len(rows)}日ぶん)")
    today = utils.format_jst(utils.now_ts())[:10]
    check(any(str(r["day"]) == today for r in rows),
          f"今日のぶんが JST の日付で入る ({today})")

    image, totals = await bot.charge.build_daily_chart(G, days=7)
    check(totals["days"] == 7, "指定した日数が返る")
    check(totals["amount"] > 0 and totals["count"] > 0,
          f"期間の合計が入る ({totals['amount']} / {totals['count']}件)")
    check(image is not None and getattr(image, "filename", "") == config.CHART_FILENAME,
          "Embed から参照できるファイル名で添付される")
    over = await bot.charge.build_daily_chart(G, days=config.CHART_MAX_DAYS + 50)
    check(over[1]["days"] == config.CHART_MAX_DAYS,
          f"日数は上限で頭打ちにする ({over[1]['days']}日)")

    # --- Pillow が無い環境でも Bot は動く ---
    real_image = chart_module.Image
    chart_module.Image = None  # type: ignore[assignment]
    try:
        check(not chart_module.available(), "Pillow が無い場合は available() が False")
        check("Pillow" in chart_module.unavailable_reason(),
              "描けない理由を説明できる")
        check(chart_module.render_daily_chart(filled, title="x") is None,
              "Pillow が無ければ画像は None")
        no_image, no_totals = await bot.charge.build_daily_chart(G, days=7)
        check(no_image is None and no_totals["count"] > 0,
              "画像が無くても集計は返る (統計は失われない)")
    finally:
        chart_module.Image = real_image  # type: ignore[assignment]
    check(chart_module.available(), "後始末でグラフ機能が戻る")

    print("\n=== 10. 整合性 ===")
    integrity = await bot.db.integrity_check()
    check(integrity["pragma"] == "ok", "PRAGMA quick_check OK")
    check(not integrity["balance_mismatch"], "残高と履歴合計が一致")
    check(not integrity["negative_balance"], "負の残高がない")

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
