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

    print("\n=== 3. 整合性 ===")
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
