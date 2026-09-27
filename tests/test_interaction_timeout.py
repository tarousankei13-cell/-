#!/usr/bin/env python3
"""ボタン操作が Discord の3秒制限を守ることを検証する。

Discord はボタン・選択メニューの操作に **3秒以内の応答** を求める。
超えると「アプリケーションは応答しませんでした」と表示され、その操作は
無効になる (利用者からは「ボタンが押せない」ように見える)。

この Bot は DB を単一スレッドで直列化しているため、重い処理 (バックアップ・
整合性チェック・大きな集計) と重なると、応答前の問い合わせが数秒待たされる。
ここでは次を確認する。

1. DB が数秒塞がっていても、チャージボタンが3秒以内に応答すること
2. 応答前に DB を待たない経路 (キャッシュ) が実際に使われていること
3. キャッシュが無い場合でも、先に defer して3秒以内に応答権を確保すること
4. 設定を変えるとキャッシュが捨てられ、古い内容でボタンが出ないこと
5. 重い処理が対話操作と別の接続で動くこと

実行:
    python3 tests/test_interaction_timeout.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_timeout_test"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "test_timeout.db"
config.SECRET_KEY_PATH = SCRATCH / "secret.key"
config.RECEIPT_KEY_PATH = SCRATCH / "receipt.key"
config.BACKUP_DIR = SCRATCH / "backups"

PASS: list[str] = []
FAIL: list[str] = []

#: Discord の制限は3秒。余裕を見て 2.5 秒を上限として扱う。
BUDGET = 2.5
G = 70_001


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(f"{'  ok  ' if condition else ' FAIL '} {label}")


# ---------------------------------------------------------------------------
# Discord スタブ (応答したかどうかと、その時刻だけを見る)
# ---------------------------------------------------------------------------
class StubResponse:
    def __init__(self, record: "StubInteraction") -> None:
        self._record = record
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs: Any) -> None:
        self._done = True
        self._record.mark("defer")

    async def send_message(self, **kwargs: Any) -> None:
        self._done = True
        self._record.mark("send_message")

    async def send_modal(self, modal: Any) -> None:
        self._done = True
        self._record.mark("send_modal")
        self._record.modals.append(modal)

    async def edit_message(self, **kwargs: Any) -> None:
        self._done = True
        self._record.mark("edit_message")


class StubFollowup:
    def __init__(self, record: "StubInteraction") -> None:
        self._record = record

    async def send(self, **kwargs: Any) -> Any:
        self._record.calls.append("followup")
        return None


class StubInteraction:
    def __init__(self, bot: Any, guild: Any, user: Any) -> None:
        self.client = bot
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.channel = guild.channel
        self.message = None
        self.calls: list[str] = []
        self.modals: list[Any] = []
        self.response = StubResponse(self)
        self.followup = StubFollowup(self)
        self.created = time.monotonic()
        self.first_response_at: float | None = None

    def mark(self, kind: str) -> None:
        if self.first_response_at is None:
            self.first_response_at = time.monotonic()
        self.calls.append(kind)

    @property
    def latency(self) -> float:
        """操作から最初の応答までの秒数。"""
        if self.first_response_at is None:
            return float("inf")
        return self.first_response_at - self.created


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


class StubPermissions:
    def __getattr__(self, name: str) -> bool:
        return True


class StubMember:
    def __init__(self, user_id: int, guild: "StubGuild") -> None:
        self.id = user_id
        self.guild = guild
        self.bot = False
        self.name = f"user{user_id}"
        self.display_name = self.name
        self.mention = f"<@{user_id}>"
        self.roles: list[StubRole] = []


class StubChannel:
    def __init__(self, channel_id: int, guild: "StubGuild") -> None:
        self.id = channel_id
        self.guild = guild
        self.name = "general"
        self.mention = f"<#{channel_id}>"

    def permissions_for(self, member: Any) -> StubPermissions:
        return StubPermissions()

    async def send(self, **kwargs: Any) -> Any:
        return None


class StubGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.name = "タイムアウト検証サーバー"
        self.icon = None
        self.owner_id = 1
        self.members: dict[int, StubMember] = {}
        self.roles: dict[int, StubRole] = {}
        self.me = StubMember(999_999, self)
        self.me.guild_permissions = StubPermissions()  # type: ignore[attr-defined]
        self.me.top_role = StubRole(9_999, "Bot", position=100)  # type: ignore[attr-defined]
        self.channel = StubChannel(700_001, self)
        self.text_channels = [self.channel]
        self.default_role = StubRole(guild_id, "@everyone", position=0)

    def get_member(self, user_id: int) -> StubMember | None:
        return self.members.get(user_id)

    def get_role(self, role_id: int) -> StubRole | None:
        return self.roles.get(role_id)

    def get_channel(self, channel_id: int) -> StubChannel | None:
        return self.channel if channel_id == self.channel.id else None


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    import main as main_module

    guild = StubGuild(G)

    class TestBot(main_module.ChargeBot):
        def __init__(self) -> None:
            super().__init__()
            self.owner_alerts: list[str] = []

        def get_guild(self, guild_id: int):  # type: ignore[override]
            return guild if guild_id == guild.id else None

        def get_channel(self, channel_id: int):  # type: ignore[override]
            return guild.get_channel(channel_id)

        @property
        def guilds(self):  # type: ignore[override]
            return [guild]

        async def alert_owner(self, message: str) -> None:
            self.owner_alerts.append(message)

    bot = TestBot()
    await bot.db.connect()
    await bot.db.set_guild_permission(G, "ALLOWED", 1)
    await bot.db.update_settings(G, charge_rate="130", minimum_charge=100,
                                 maximum_charge=100_000)
    member = StubMember(70_101, guild)
    guild.members[member.id] = member

    def fresh() -> StubInteraction:
        return StubInteraction(bot, guild, member)

    # 受取用 Kyash を使える状態にする
    account_id = await bot.kyash.add_account("main", threshold=0, priority=0)
    slot = bot.kyash.get_slot(account_id)
    assert slot is not None
    slot.client = object()  # type: ignore[assignment]
    slot.status = config.KyashAccountStatus.ACTIVE
    slot.wallet_balance = 0

    print("=== 1. キャッシュが無い状態での応答 ===")
    bot.charge.invalidate_panel_view()
    interaction = fresh()
    await bot.on_charge_button(interaction)
    check(interaction.first_response_at is not None,
          f"必ず応答する ({interaction.calls})")
    check(interaction.latency < BUDGET,
          f"キャッシュが無くても {interaction.latency * 1000:.0f} ms で応答")
    check(interaction.calls[0] == "defer",
          f"キャッシュが無いときは先に defer する ({interaction.calls[0]})")

    print("\n=== 2. キャッシュがある状態での応答 ===")
    await bot.charge.refresh_panel_view(G)
    snapshot = bot.charge.cached_panel_view(G)
    assert snapshot is not None
    interaction = fresh()
    await bot.on_charge_button(interaction)
    check(interaction.latency < BUDGET,
          f"キャッシュありで {interaction.latency * 1000:.0f} ms で応答")
    if len(snapshot["usable"]) == 1:
        check(interaction.calls[0] == "send_modal",
              f"方式が1つなら直接 Modal を出せる ({interaction.calls[0]})")
    else:
        check(interaction.calls[0] == "send_message",
              f"方式が複数なら選択画面を出す "
              f"({len(snapshot['usable'])}件 → {interaction.calls[0]})")
    # 方式を1つに絞ると、キャッシュ経由で Modal まで一気に進む
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH_CLAIM, enabled=False, updated_by=1
    )
    await bot.charge.refresh_panel_view(G)
    single = bot.charge.cached_panel_view(G)
    assert single is not None
    interaction = fresh()
    await bot.on_charge_button(interaction)
    check(len(single["usable"]) == 1 and interaction.calls[0] == "send_modal",
          f"方式が1つに絞られていれば Modal 直行 ({interaction.calls[0]})")

    print("\n=== 3. DB が塞がっている最中の応答 (本題) ===")

    def slow_job(conn: Any) -> None:
        # バックアップや整合性チェックが動いている状況を模す
        time.sleep(4.0)

    await bot.charge.refresh_panel_view(G)   # キャッシュを温めておく
    blocker = asyncio.create_task(bot.db.run(slow_job))
    await asyncio.sleep(0.05)
    interaction = fresh()
    await bot.on_charge_button(interaction)
    latency = interaction.latency
    check(latency < BUDGET,
          f"DB が4秒塞がっていても {latency * 1000:.0f} ms で応答 "
          f"(修正前はここで3秒を超えていた)")
    check(interaction.first_response_at is not None, "応答そのものが返る")
    await blocker

    print("\n=== 4. 方式選択メニューも3秒以内 ===")
    await bot.charge.refresh_panel_view(G)
    blocker = asyncio.create_task(bot.db.run(slow_job))
    await asyncio.sleep(0.05)
    interaction = fresh()
    await bot.on_provider_selected(interaction, config.ChargeProvider.KYASH)
    check(interaction.latency < BUDGET,
          f"方式選択も {interaction.latency * 1000:.0f} ms で応答")
    await blocker

    print("\n=== 5. 設定を変えるとキャッシュが捨てられる ===")
    await bot.charge.refresh_panel_view(G)
    check(bot.charge.cached_panel_view(G) is not None, "温めた直後はキャッシュがある")
    await bot.db.update_settings(G, charge_rate="150")
    check(bot.charge.cached_panel_view(G) is None,
          "設定を変えるとキャッシュが捨てられる (古い率で表示しない)")
    snapshot = await bot.charge.panel_view(G)
    check(str(snapshot["settings"].charge_rate) == "150",
          f"作り直すと新しい設定が反映される ({snapshot['settings'].charge_rate})")

    # 方式の設定を変えたときも捨てる
    await bot.charge.refresh_panel_view(G)
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.PAYPAY, enabled=False, updated_by=1
    )
    check(bot.charge.cached_panel_view(G) is None,
          "方式の有効/無効を変えてもキャッシュが捨てられる")

    print("\n=== 6. 重い処理は別の接続で動く ===")
    started = time.monotonic()
    heavy = asyncio.create_task(bot.db.integrity_check())
    await asyncio.sleep(0.02)
    t = time.monotonic()
    await bot.db.get_settings(G)
    interactive = time.monotonic() - t
    await heavy
    check(interactive < 1.0,
          f"整合性チェック中でも通常の問い合わせが {interactive * 1000:.0f} ms で返る")
    backup_started = time.monotonic()
    backup = asyncio.create_task(bot.db.backup(SCRATCH / "backups" / "t.db"))
    await asyncio.sleep(0.02)
    t = time.monotonic()
    await bot.db.get_settings(G)
    interactive = time.monotonic() - t
    await backup
    check(interactive < 1.0,
          f"バックアップ中でも通常の問い合わせが {interactive * 1000:.0f} ms で返る")
    print(f"  (重い処理そのものの所要: 整合性 "
          f"{(backup_started - started) * 1000:.0f} ms)")

    print("\n=== 7. 応答前に DB を待つ経路が残っていないか ===")
    # キャッシュがある状態では、ボタン押下から Modal までの DB 問い合わせは
    # 「進行中の取引を探す1回」だけであるべき
    await bot.charge.refresh_panel_view(G)
    counter = {"n": 0}
    real_run = bot.db.run

    async def counting_run(fn, *, write=False):
        counter["n"] += 1
        return await real_run(fn, write=write)

    bot.db.run = counting_run  # type: ignore[assignment]
    interaction = fresh()
    await bot.on_charge_button(interaction)
    bot.db.run = real_run  # type: ignore[assignment]
    check(counter["n"] <= 2,
          f"応答前の DB 問い合わせは {counter['n']} 回 (修正前は 38 回)")

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
