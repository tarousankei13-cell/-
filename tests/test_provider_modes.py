#!/usr/bin/env python3
"""受け取り方 (Kyash / PayPay) の切り替えが、表示だけでなく実際の動作に効くか。

v4.1 では次の不具合があった。ここではそのすべてを再発させないよう確認する。

1. 受け取り方を切り替えると、チャージ率と金額上下限が別の値へ化けていた
   (設定が方式ごとに分かれていたため)
2. ``start_charge`` が受付停止 (PROVIDER_DISABLED) を見ていなかった
   (古い入力欄が手元に残っていれば、停止中でもチャージできた)
3. ``start_charge`` が受け取り方を見ていなかった
   (請求リンク方式に切り替えても送金リンクの取引が作れた)
4. ``/provider rate`` と ``/provider limits`` が送金リンク方式に効かなかった
5. 前の受け取り方の取引が残っていると、切り替えても前の画面しか出ず、
   理由も書かれていなかった (「方式を変えても送金リンクしか使えない」の正体)
6. 「Kyash (送金リンク)」だけを受付停止にできたため、
   「Kyash 自体が使えなくなった」という状態が作れてしまった
7. 旧 DB から移行したとき、方式ごとに分かれた設定が残っていた

実行:
    python3 tests/test_provider_modes.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_modes_test"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "test_modes.db"
config.SECRET_KEY_PATH = SCRATCH / "secret.key"
config.RECEIPT_KEY_PATH = SCRATCH / "receipt.key"
config.BACKUP_DIR = SCRATCH / "backups"

import ui  # noqa: E402
import utils  # noqa: E402
from charge_service import ChargeError  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

G = 92_001


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(f"{'  ok  ' if condition else ' FAIL '} {label}")


sys.path.insert(0, str(BASE / "tests"))
from test_interaction_timeout import (  # noqa: E402
    StubGuild, StubInteraction, StubMember,
)


class RecordingInteraction(StubInteraction):
    """応答に渡された Embed を覚えておく Interaction スタブ。

    「画面に何と書かれたか」を検証したいので、送られた Embed を集める。
    """

    def __init__(self, bot: Any, guild: Any, user: Any) -> None:
        super().__init__(bot, guild, user)
        self.embeds: list[Any] = []
        outer = self

        class _Response(type(self.response)):  # type: ignore[misc]
            async def send_message(self, **kwargs: Any) -> None:
                if kwargs.get("embed") is not None:
                    outer.embeds.append(kwargs["embed"])
                await super().send_message(**kwargs)

            async def edit_message(self, **kwargs: Any) -> None:
                if kwargs.get("embed") is not None:
                    outer.embeds.append(kwargs["embed"])
                await super().edit_message(**kwargs)

        class _Followup(type(self.followup)):  # type: ignore[misc]
            async def send(self, **kwargs: Any) -> Any:
                if kwargs.get("embed") is not None:
                    outer.embeds.append(kwargs["embed"])
                return await super().send(**kwargs)

        self.response = _Response(self)
        self.followup = _Followup(self)

    def embed_text(self) -> str:
        """集めた Embed の本文をひとつの文字列にする。"""
        return "".join(
            (embed.description or "")
            + "".join(f.name + f.value for f in embed.fields)
            for embed in self.embeds
        )


class StubKyashClient:
    """受取用アカウントを「使える」状態に見せるための最小スタブ。

    このテストは受け取り方の判定を見るので、実際のリンク発行までは行かない
    (請求リンクの発行そのものは tests/test_claim_link.py で検証している)。
    """


async def main() -> None:  # noqa: C901 - 検証項目が多いため1本で通す
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    import main as main_module

    guild = StubGuild(G)

    class TestBot(main_module.ChargeBot):
        def get_guild(self, guild_id: int):  # type: ignore[override]
            return guild if guild_id == guild.id else None

        def get_channel(self, channel_id: int):  # type: ignore[override]
            return guild.get_channel(channel_id)

        @property
        def guilds(self):  # type: ignore[override]
            return [guild]

        async def alert_owner(self, message: str) -> None:
            return None

    bot = TestBot()
    await bot.db.connect()
    await bot.db.set_guild_permission(G, "ALLOWED", 1)
    await bot.db.update_settings(
        G, charge_rate="130", minimum_charge=100, maximum_charge=100_000,
        daily_limit=0,
    )
    member = StubMember(92_101, guild)
    guild.members[member.id] = member

    account_id = await bot.kyash.add_account("main", threshold=0, priority=0)
    slot = bot.kyash.get_slot(account_id)
    assert slot is not None
    slot.client = StubKyashClient()
    slot.status = config.KyashAccountStatus.ACTIVE
    slot.wallet_balance = 0

    def fresh() -> RecordingInteraction:
        return RecordingInteraction(bot, guild, member)

    async def set_mode(mode: str) -> Any:
        await bot.db.update_settings(G, kyash_mode=mode)
        bot.charge.invalidate_panel_view(G)
        return await bot.db.get_settings(G)

    async def clear_active() -> None:
        row = await bot.db.get_active_transaction(G, member.id)
        if row is not None:
            await bot.charge.cancel_transaction(str(row["id"]), member.id)

    def reset_limits() -> None:
        """検証を続けるためにレート制限を戻す (制限自体は別のテストで見る)。"""
        for prefix in ("charge", "claim", "req"):
            bot.charge._charge_rate_limiter.reset(f"{prefix}:{G}:{member.id}")
        bot.charge._button_rate_limiter.reset(str(member.id))

    # ==================================================================
    print("=== 1. 設定は受け取り方で分かれない ===")
    # ==================================================================
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH, charge_rate=Decimal("150"),
        minimum_charge=500, maximum_charge=20_000, updated_by=1,
    )
    seen: dict[str, tuple[str, int, int]] = {}
    for mode in (config.KyashMode.TRANSFER, config.KyashMode.CLAIM):
        settings = await set_mode(mode)
        provider = bot.charge.selected_provider(config.ChargeProvider.KYASH, settings)
        rate, _role = await bot.charge.resolve_provider_rate(
            G, member.id, provider, settings
        )
        low, high = await bot.charge.provider_limits(G, provider, settings)
        seen[mode] = (str(rate), low, high)
    check(seen[config.KyashMode.TRANSFER] == seen[config.KyashMode.CLAIM],
          f"受け取り方を変えてもレートと金額範囲が同じ ({seen})")
    check(seen[config.KyashMode.TRANSFER] == ("150", 500, 20_000),
          f"設定した値がそのまま効く ({seen[config.KyashMode.TRANSFER]})")

    # 請求リンク方式のキーで書いても、同じ1行が更新される
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH_CLAIM, charge_rate=Decimal("160"), updated_by=1
    )
    row = await bot.db.get_provider_settings(G, config.ChargeProvider.KYASH)
    check(utils.to_decimal(row["charge_rate"]) == Decimal("160"),
          "請求リンク方式のキーで設定しても同じ行を更新する")
    stored = await bot.db.fetchall(
        "SELECT provider FROM provider_settings WHERE guild_id=?", (G,)
    )
    check([str(r["provider"]) for r in stored] == [config.ChargeProvider.KYASH],
          f"保存される行は系統ごとに1つだけ ({[str(r['provider']) for r in stored]})")
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH, clear_rate=True, clear_limits=True, updated_by=1
    )

    # ==================================================================
    print("\n=== 2. 受付停止は取引作成時にも効く ===")
    # ==================================================================
    reset_limits()
    await set_mode(config.KyashMode.TRANSFER)
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH, enabled=False, updated_by=1
    )
    try:
        await bot.charge.start_charge(G, member.id, "1000")
        check(False, "受付停止中は送金リンクの取引を作れない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.PROVIDER_DISABLED,
              f"受付停止中は送金リンクの取引を作れない ({exc.code})")
    await set_mode(config.KyashMode.CLAIM)
    try:
        await bot.charge.start_claim_charge(G, member.id, "1000")
        check(False, "受付停止中は請求リンクも発行できない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.PROVIDER_DISABLED,
              f"受付停止中は請求リンクも発行できない ({exc.code})")
    # 片方の方式だけを止めることはできない (系統ごと停止になる)
    entries = await bot.charge.provider_availability(G)
    kyash_entries = [
        e for e in entries if str(e["provider"]) in config.KYASH_PROVIDERS
    ]
    check(len(kyash_entries) == 1 and not kyash_entries[0]["available"],
          "Kyash を止めると、選ばれている方式も止まる")
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH, enabled=True, updated_by=1
    )

    # ==================================================================
    print("\n=== 3. 選ばれていない受け取り方では取引を作れない ===")
    # ==================================================================
    reset_limits()
    await set_mode(config.KyashMode.CLAIM)
    try:
        await bot.charge.start_charge(G, member.id, "1000")
        check(False, "請求リンク方式のとき送金リンクの取引は作れない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.PROVIDER_MODE_MISMATCH,
              f"請求リンク方式のとき送金リンクの取引は作れない ({exc.code})")
    await set_mode(config.KyashMode.TRANSFER)
    try:
        await bot.charge.start_claim_charge(G, member.id, "1000")
        check(False, "送金リンク方式のとき請求リンクは発行できない")
    except ChargeError as exc:
        check(exc.code == config.ErrorCode.PROVIDER_MODE_MISMATCH,
              f"送金リンク方式のとき請求リンクは発行できない ({exc.code})")
    check(config.ErrorCode.PROVIDER_MODE_MISMATCH in config.USER_ERROR_MESSAGES
          and config.ErrorCode.PROVIDER_MODE_MISMATCH in config.USER_ERROR_NEXT_ACTIONS,
          "利用者向けの文言と次の一手が用意されている")

    # ==================================================================
    print("\n=== 4. 方式別のレート・上下限が送金リンク方式にも効く ===")
    # ==================================================================
    reset_limits()
    await set_mode(config.KyashMode.TRANSFER)
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH, charge_rate=Decimal("145"),
        minimum_charge=1_000, maximum_charge=5_000, updated_by=1,
    )
    tx_id, amount, _settings = await bot.charge.start_charge(G, member.id, "2000")
    tx_row = await bot.db.get_transaction(tx_id)
    check(utils.to_decimal(tx_row["charge_rate"]) == Decimal("145"),
          f"方式別レートが取引に乗る ({tx_row['charge_rate']})")
    await bot.charge.cancel_transaction(tx_id, member.id)
    for bad, label in (("500", "下限"), ("9000", "上限")):
        try:
            await bot.charge.start_charge(G, member.id, bad)
            check(False, f"方式別の{label}を超える金額を弾く")
        except ChargeError as exc:
            check(exc.code in (config.ErrorCode.AMOUNT_BELOW_MIN,
                               config.ErrorCode.AMOUNT_ABOVE_MAX),
                  f"方式別の{label}を超える金額を弾く ({exc.code})")
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH, clear_rate=True, clear_limits=True, updated_by=1
    )
    await clear_active()

    # ==================================================================
    print("\n=== 5. 切り替え後も前の取引を続けられ、理由が書かれる ===")
    # ==================================================================
    reset_limits()
    await set_mode(config.KyashMode.TRANSFER)
    tx_id, _amount, _s = await bot.charge.start_charge(G, member.id, "1000")
    await bot.db.transition_status(
        tx_id, config.TxStatus.WAITING_LINK, expected=(config.TxStatus.CREATED,)
    )
    await set_mode(config.KyashMode.CLAIM)
    await bot.charge.refresh_panel_view(G)
    interaction = fresh()
    await bot.on_charge_button(interaction)
    text = interaction.embed_text()
    check("受け取り方が変わりました" in text,
          "受け取り方が変わったことを画面で伝える")
    check("請求リンク" in text and "キャンセル" in text,
          "新しい方式と、そこへ進む手順を書く")
    check(interaction.first_response_at is not None
          and interaction.latency < 2.5,
          f"この画面も3秒以内に返る ({interaction.latency * 1000:.0f} ms)")
    still = await bot.db.get_active_transaction(G, member.id)
    check(still is not None and str(still["id"]) == tx_id,
          "切り替えても進行中の取引は消さない (送金済みの可能性がある)")
    await bot.charge.cancel_transaction(tx_id, member.id)

    # キャンセル後の案内は、いま受け付けている方式のものになる。
    # 取引は DB へ直接作る (ここで見たいのは案内の文章だけ)。
    async def cancel_message(mode: str) -> str:
        await clear_active()
        await set_mode(mode)
        new_tx = await bot.db.create_transaction(
            guild_id=G, user_id=member.id, requested_amount=1_000,
            charge_rate=Decimal("130"),
            expires_at=utils.now_ts() + config.LINK_WAIT_SECONDS,
        )
        cancel_interaction = fresh()
        await bot.handle_charge_cancel(cancel_interaction, new_tx)
        return cancel_interaction.embed_text()

    claim_cancel = await cancel_message(config.KyashMode.CLAIM)
    check("請求リンク" in claim_cancel and "Kyash アプリ" not in claim_cancel,
          f"請求リンク方式のキャンセル案内が方式に合っている ({claim_cancel[:36]}…)")
    transfer_cancel = await cancel_message(config.KyashMode.TRANSFER)
    check("Kyash アプリ" in transfer_cancel,
          "送金リンク方式のキャンセル案内は Kyash アプリ側の操作を伝える")

    # ==================================================================
    print("\n=== 6. 管理コマンドの選択肢は方式ごとに1つ ===")
    # ==================================================================
    import commands as commands_module

    labels = [c.name for c in commands_module._PROVIDER_CHOICES]
    check(len(labels) == 3 and labels.count("Kyash") == 1,
          f"Kyash は1つだけ並ぶ ({labels})")
    check(all("送金リンク" not in name and "請求リンク" not in name for name in labels),
          f"受け取り方は選択肢に混ぜない ({labels})")
    check(
        [c.value for c in commands_module._PROVIDER_CHOICES]
        == list(config.ADMIN_PROVIDERS),
        "選択肢の値が代表キーになっている",
    )

    # ==================================================================
    print("\n=== 7. パネルと案内の文章が受け取り方で変わる ===")
    # ==================================================================
    async def panel_text(mode: str) -> str:
        settings = await set_mode(mode)
        entries = await bot.charge.provider_availability(G, settings)
        embed = ui.charge_panel_embed(
            settings, kyash_ready=True, providers=entries
        )
        return (embed.description or "") + "".join(
            f.name + f.value for f in embed.fields
        )

    transfer_panel = await panel_text(config.KyashMode.TRANSFER)
    claim_panel = await panel_text(config.KyashMode.CLAIM)
    check("送金リンク" in transfer_panel and "請求リンク" not in transfer_panel,
          "送金リンク方式のパネルは送金リンクの手順だけを書く")
    check("請求リンク" in claim_panel and "送金リンク" not in claim_panel,
          "請求リンク方式のパネルは請求リンクの手順だけを書く")
    check(transfer_panel != claim_panel, "受け取り方でパネルの文章が変わる")

    # PayPay も受け取り方で文章が変わる
    from database import GuildSettings

    id_settings = GuildSettings(guild_id=G, paypay_mode=config.PayPayMode.ID)
    link_settings = GuildSettings(
        guild_id=G, paypay_mode=config.PayPayMode.CLAIM_LINK
    )
    id_steps = " ".join(ui.provider_steps(config.ChargeProvider.PAYPAY, id_settings))
    link_steps = " ".join(
        ui.provider_steps(config.ChargeProvider.PAYPAY, link_settings)
    )
    check("送金先ID" in id_steps and "請求リンク" not in id_steps,
          "PayPay ID方式は ID へ送ると書く")
    check("請求リンク" in link_steps,
          "PayPay 請求リンク方式はリンクを開くと書く")
    check(
        ui.provider_method_label(config.ChargeProvider.PAYPAY, link_settings)
        == "PayPay (請求リンク)",
        "PayPay の方式名に受け取り方が出る",
    )

    # ==================================================================
    print("\n=== 8. パネルとヘルプは、その方法に実際に効く条件を出す ===")
    # ==================================================================
    # 方式別のレート・上下限を設定しているのにサーバー既定を表示すると、
    # 「書いてある額を送れない」ことになるため必ず実際の値を出す。
    from database import GuildSettings as _GS

    base_settings = _GS(
        guild_id=G, charge_rate=Decimal("130"), minimum_charge=100,
        maximum_charge=100_000,
    )

    def make_entry(provider: str, rate: str, low: int, high: int) -> dict[str, Any]:
        return {
            "provider": provider, "available": True, "reason": None,
            "error_code": None, "minimum": low, "maximum": high,
            "rate": Decimal(rate), "destination": None,
        }

    single = [make_entry(config.ChargeProvider.KYASH, "150", 1_000, 5_000)]
    panel = ui.charge_panel_embed(base_settings, kyash_ready=True, providers=single)
    rate_field = next(f for f in panel.fields if "レート" in f.name)
    amount_field = next(f for f in panel.fields if "1回の金額" in f.name)
    check("150%" in rate_field.value and "130%" not in rate_field.value,
          f"方法が1つならその方法のレートを出す ({rate_field.value!r})")
    check("1,000円" in amount_field.value and "5,000円" in amount_field.value,
          f"方法が1つならその方法の金額範囲を出す ({amount_field.value!r})")
    check("1,500" in rate_field.value,
          f"例の獲得額もその方法のレートで計算する ({rate_field.value!r})")

    help_single = ui.help_embed(base_settings, providers=single)
    help_text = "".join(f.name + f.value for f in help_single.fields)
    check("150%" in help_text and "チャージ率は **130%**" not in help_text,
          "ヘルプも方法1つのときはその方法のレートを出す")
    check("1,000円" in help_text and "5,000円" in help_text,
          "ヘルプにその方法の金額範囲を出す")

    # 方法が複数のときはサーバー既定を出し、行末に方式別レートを添える
    multi = single + [make_entry(config.ChargeProvider.PAYPAY, "120", 100, 100_000)]
    panel_multi = ui.charge_panel_embed(base_settings, kyash_ready=True, providers=multi)
    rate_multi = next(f for f in panel_multi.fields if "レート" in f.name)
    method_multi = next(f for f in panel_multi.fields if "チャージの方法" in f.name)
    check("130%" in rate_multi.value,
          f"方法が複数ならサーバー既定を出す ({rate_multi.value!r})")
    check("150%" in method_multi.value and "120%" in method_multi.value,
          f"方式ごとのレートは一覧の行に出す ({method_multi.value!r})")

    # ==================================================================
    print("\n=== 9. 旧 DB からの移行で設定が1つにまとまる ===")
    # ==================================================================
    legacy_path = SCRATCH / "legacy_modes.db"
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(legacy_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    conn = sqlite3.connect(str(legacy_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE system_settings (
            key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER DEFAULT 0
        );
        CREATE TABLE guild_settings (
            guild_id INTEGER PRIMARY KEY,
            kyash_mode TEXT NOT NULL DEFAULT 'TRANSFER'
        );
        CREATE TABLE provider_settings (
            guild_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            charge_rate TEXT,
            minimum_charge INTEGER,
            maximum_charge INTEGER,
            updated_by INTEGER,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (guild_id, provider)
        );
        """
    )
    conn.execute(
        "INSERT INTO system_settings(key, value) VALUES('schema_version', '5')"
    )
    # 請求リンク方式を使っているサーバー: 請求リンク側の値が残るべき
    conn.execute("INSERT INTO guild_settings(guild_id, kyash_mode) VALUES(1, 'CLAIM')")
    conn.execute(
        "INSERT INTO provider_settings(guild_id, provider, enabled, charge_rate, "
        "minimum_charge, maximum_charge) VALUES(1,'KYASH',1,'130',100,10000)"
    )
    conn.execute(
        "INSERT INTO provider_settings(guild_id, provider, enabled, charge_rate, "
        "minimum_charge, maximum_charge) VALUES(1,'KYASH_CLAIM',1,'155',300,30000)"
    )
    # 送金リンク方式のまま停止していたサーバー: 停止のまま引き継ぐべき
    conn.execute(
        "INSERT INTO guild_settings(guild_id, kyash_mode) VALUES(2, 'TRANSFER')"
    )
    conn.execute(
        "INSERT INTO provider_settings(guild_id, provider, enabled, charge_rate) "
        "VALUES(2,'KYASH',0,'140')"
    )
    conn.execute(
        "INSERT INTO provider_settings(guild_id, provider, enabled, charge_rate) "
        "VALUES(2,'KYASH_CLAIM',1,'120')"
    )
    # 送金リンク方式だが送金リンク側の行が無いサーバー:
    # 既定値で動いていたので、請求リンク側の値を持ち込んではいけない
    conn.execute(
        "INSERT INTO guild_settings(guild_id, kyash_mode) VALUES(3, 'TRANSFER')"
    )
    conn.execute(
        "INSERT INTO provider_settings(guild_id, provider, enabled, charge_rate) "
        "VALUES(3,'KYASH_CLAIM',0,'199')"
    )
    conn.commit()
    conn.close()

    import database as database_module

    legacy = database_module.Database(legacy_path)
    await legacy.connect()
    rows = await legacy.fetchall(
        "SELECT * FROM provider_settings ORDER BY guild_id, provider"
    )
    providers = [(int(r["guild_id"]), str(r["provider"])) for r in rows]
    check(providers == [(1, "KYASH"), (2, "KYASH")],
          f"方式別の行が系統ごとの1行へまとまる ({providers})")
    merged1 = next(r for r in rows if int(r["guild_id"]) == 1)
    check(
        str(merged1["charge_rate"]) == "155"
        and int(merged1["minimum_charge"]) == 300
        and int(merged1["maximum_charge"]) == 30_000,
        "請求リンク方式のサーバーは請求リンク側の値を引き継ぐ "
        f"({merged1['charge_rate']} / {merged1['minimum_charge']}〜{merged1['maximum_charge']})",
    )
    merged2 = next(r for r in rows if int(r["guild_id"]) == 2)
    check(not merged2["enabled"] and str(merged2["charge_rate"]) == "140",
          f"送金リンク方式のサーバーは送金リンク側の値を引き継ぐ "
          f"(enabled={merged2['enabled']} rate={merged2['charge_rate']})")
    check(not any(int(r["guild_id"]) == 3 for r in rows),
          "使っていない側の値は持ち込まない (既定値のまま残る)")
    orphan = await legacy.get_provider_settings(3, config.ChargeProvider.KYASH)
    check(orphan is None,
          "使っていない側の停止設定を引き継いで勝手に止めたりしない")
    version = await legacy.get_system_value("schema_version")
    check(version == str(config.SCHEMA_VERSION),
          f"スキーマバージョンが v{config.SCHEMA_VERSION} になる ({version})")
    await legacy.close()

    # ==================================================================
    print("\n=== 10. 受付停止のままなら起動時に知らせる ===")
    # ==================================================================
    # 「方式を変えたのにチャージできない」の原因はほぼこれなので、
    # 黙って止まったままにしない。
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH, enabled=False, updated_by=1
    )
    stopped = await bot.warn_disabled_providers()
    check((G, config.ChargeProvider.KYASH) in stopped,
          f"受付停止中の方式を見つける ({stopped})")
    await bot.db.set_provider_settings(
        G, config.ChargeProvider.KYASH, enabled=True, updated_by=1
    )
    check(await bot.warn_disabled_providers() == [],
          "再開すれば警告は出ない")

    # ==================================================================
    print("\n=== 11. 整合性 ===")
    # ==================================================================
    await clear_active()
    integrity = await bot.db.integrity_check()
    check(integrity["pragma"] == "ok", "PRAGMA quick_check OK")
    check(not integrity["balance_mismatch"], "残高と履歴合計が一致")
    check(not integrity["negative_balance"], "負の残高がない")

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
