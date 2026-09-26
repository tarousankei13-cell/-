"""スキーマの前方互換 (古い DB を最新のコードで開けるか) を検証する。

v1 相当の DB を作り、最新のコードで開いてマイグレーションが通ることと、
v2 / v3 で追加した機能が既存データの上でも動くことを確認する。

実行:
    python3 tests/test_migration.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_migration"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "migration.db"

import database  # noqa: E402
import utils  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(f"{'  ok  ' if condition else ' FAIL '} {label}")


#: v1 時点のスキーマ (v2 で追加した列を持たない最小構成)
V1_SCHEMA = (
    """
    CREATE TABLE system_settings (
        key TEXT PRIMARY KEY, value TEXT, updated_at INTEGER NOT NULL DEFAULT 0)
    """,
    """
    CREATE TABLE allowed_guilds (
        guild_id INTEGER PRIMARY KEY, allowed_by INTEGER,
        allowed_at INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'ALLOWED', note TEXT)
    """,
    """
    CREATE TABLE guild_settings (
        guild_id INTEGER PRIMARY KEY, charge_rate TEXT NOT NULL, minimum_charge INTEGER NOT NULL,
        maximum_charge INTEGER NOT NULL, daily_limit INTEGER NOT NULL,
        guild_daily_limit INTEGER NOT NULL DEFAULT 0, admin_role_id INTEGER,
        achievement_channel_id INTEGER, log_channel_id INTEGER,
        maintenance INTEGER NOT NULL DEFAULT 0, emergency_stop INTEGER NOT NULL DEFAULT 0,
        ranking_enabled INTEGER NOT NULL DEFAULT 1, ranking_limit INTEGER NOT NULL DEFAULT 10,
        ranking_interval INTEGER NOT NULL DEFAULT 300,
        ranking_hide_absent INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)
    """,
    """
    CREATE TABLE users (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, frozen INTEGER NOT NULL DEFAULT 0,
        frozen_reason TEXT, frozen_by INTEGER, frozen_at INTEGER,
        created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id))
    """,
    """
    CREATE TABLE balances (
        guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        balance INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, user_id))
    """,
    """
    CREATE TABLE charge_transactions (
        id TEXT PRIMARY KEY, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        requested_amount INTEGER NOT NULL, received_amount INTEGER, charge_rate TEXT NOT NULL,
        credited_amount INTEGER, link_hash TEXT UNIQUE, link_uuid TEXT UNIQUE,
        status TEXT NOT NULL, retry_count INTEGER NOT NULL DEFAULT 0, error_code TEXT,
        error_message TEXT, balance_before INTEGER, balance_after INTEGER, wallet_before INTEGER,
        source TEXT NOT NULL DEFAULT 'AUTOMATIC', achievement_channel_id INTEGER,
        achievement_message_id INTEGER, expires_at INTEGER, processing_started_at INTEGER,
        created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0,
        completed_at INTEGER)
    """,
    """
    CREATE TABLE balance_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        change_amount INTEGER NOT NULL, balance_before INTEGER NOT NULL,
        balance_after INTEGER NOT NULL, type TEXT NOT NULL, transaction_id TEXT,
        operator_id INTEGER, reason TEXT, created_at INTEGER NOT NULL DEFAULT 0)
    """,
    """
    CREATE TABLE panels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL UNIQUE,
        panel_type TEXT NOT NULL DEFAULT 'CHARGE', active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)
    """,
    """
    CREATE TABLE ranking_panels (
        id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL UNIQUE,
        active INTEGER NOT NULL DEFAULT 1, last_signature TEXT, last_updated_at INTEGER,
        created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)
    """,
    """
    CREATE TABLE admin_audit_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT, guild_id INTEGER,
        actor_id INTEGER NOT NULL, action TEXT NOT NULL, target_user_id INTEGER, detail TEXT,
        created_at INTEGER NOT NULL DEFAULT 0)
    """,
    """
    CREATE TABLE charge_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT, transaction_id TEXT NOT NULL UNIQUE
            REFERENCES charge_transactions(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'QUEUED', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL DEFAULT 0, enqueued_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0, last_error TEXT)
    """,
    """
    CREATE TABLE kyash_account (
        id INTEGER PRIMARY KEY CHECK (id = 1), email TEXT, client_uuid TEXT,
        installation_uuid TEXT, access_token_enc TEXT,
        status TEXT NOT NULL DEFAULT 'UNCONFIGURED', username TEXT, wallet_uuid TEXT,
        last_checked_at INTEGER, last_error TEXT, created_at INTEGER NOT NULL DEFAULT 0,
        updated_at INTEGER NOT NULL DEFAULT 0)
    """,
    """
    CREATE TABLE notification_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, guild_id INTEGER,
        user_id INTEGER, channel_id INTEGER, transaction_id TEXT, payload TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'PENDING', last_error TEXT,
        created_at INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0)
    """,
)


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()

    # --- v1 相当の DB を作り、データを入れる ---
    conn = sqlite3.connect(str(config.DB_PATH))
    for statement in V1_SCHEMA:
        conn.execute(statement)
    now = utils.now_ts()
    conn.execute("INSERT INTO system_settings(key, value, updated_at) VALUES('schema_version','1',?)",
                 (now,))
    conn.execute("INSERT INTO allowed_guilds(guild_id, allowed_by, allowed_at, status) "
                 "VALUES(1,2,?, 'ALLOWED')", (now,))
    conn.execute(
        "INSERT INTO guild_settings(guild_id, charge_rate, minimum_charge, maximum_charge, "
        "daily_limit, created_at, updated_at) VALUES(1,'130',100,50000,100000,?,?)", (now, now)
    )
    conn.execute("INSERT INTO balances(guild_id, user_id, balance, updated_at) VALUES(1,10,4200,?)",
                 (now,))
    conn.execute(
        "INSERT INTO balance_history(guild_id, user_id, change_amount, balance_before, "
        "balance_after, type, transaction_id, reason, created_at) "
        "VALUES(1,10,4200,0,4200,'CHARGE','TX-OLD01','v1データ',?)", (now,)
    )
    conn.execute(
        "INSERT INTO charge_transactions(id, guild_id, user_id, requested_amount, "
        "received_amount, charge_rate, credited_amount, status, created_at, updated_at, "
        "completed_at) VALUES('TX-OLD01',1,10,3231,3231,'130',4200,'COMPLETED',?,?,?)",
        (now, now, now)
    )
    conn.commit()
    conn.close()
    print("v1 相当の DB を作成しました")

    # --- v2 のコードで開く (マイグレーションが走る) ---
    db = database.Database(config.DB_PATH)
    await db.connect()
    check(await db.get_system_value("schema_version") == str(config.SCHEMA_VERSION),
          f"スキーマバージョンが v{config.SCHEMA_VERSION} へ更新された")
    check(await db.get_balance(1, 10) == 4200, "既存の残高が保持されている")
    settings = await db.get_settings(1)
    check(str(settings.charge_rate) == "130", "既存の設定が保持されている")
    check(settings.max_balance == 0 and settings.balance_log_scope == "MANUAL"
          and settings.shop_enabled is True,
          "v2 で追加した設定に既定値が入っている")
    check(await db.is_guild_allowed(1), "許可サーバーが保持されている")

    row = await db.get_transaction("TX-OLD01")
    check(row is not None and row["refunded_at"] is None, "v2 で追加した列が読める (refunded_at)")

    # --- v2 の新機能が既存 DB 上でも動く ---
    item_id = await db.add_shop_item(
        guild_id=1, role_id=77, name="VIP", price=1000, duration_days=30, stock=-1,
        purchase_limit=1, created_by=2,
    )
    purchase = await db.purchase_shop_item(guild_id=1, user_id=10, item_id=item_id)
    check(purchase["balance_after"] == 3200, "移行後の DB でショップ購入ができる")
    await db.set_role_rate(1, 77, Decimal("150"), 10)
    check(len(await db.list_role_rates(1)) == 1, "移行後の DB でロール別レートを登録できる")
    campaign_id = await db.create_campaign(
        guild_id=1, name="移行テスト", inviter_reward=500, invited_reward=300,
        min_account_age_days=7, daily_limit=5, total_limit=50, require_charge=True,
        require_days=0, require_review=False, starts_at=None, ends_at=None, created_by=2,
    )
    record_id, created = await db.record_invite(
        guild_id=1, campaign_id=campaign_id, inviter_id=10, invited_id=11, code="abc",
        status=config.InviteStatus.PENDING, reason=None,
    )
    reward = await db.confirm_invite_and_reward(record_id)
    check(created and reward["reward_inviter"] == 500, "移行後の DB で招待報酬を付与できる")
    panels = await db.list_panels(1, panel_type=config.PANEL_TYPE_SHOP)
    check(panels == [], "panel_type で絞り込める")
    audit = await db.audit_balance(1, 10)
    check(audit["diff"] == 0, "移行後も残高と履歴合計が一致")
    integrity = await db.integrity_check()
    check(integrity["pragma"] == "ok" and not integrity["balance_mismatch"],
          "整合性チェックが通る")

    # --- v3 (チャージ方式) の新機能が既存 DB 上でも動く ---
    tables = await db.run(
        lambda c: {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    )
    check({"charge_requests", "provider_settings", "payment_destinations"} <= tables,
          "v3 で追加したテーブルが作られた")
    await db.set_provider_settings(
        1, config.ChargeProvider.LTC, enabled=True, charge_rate=Decimal("150"),
        updated_by=2,
    )
    provider_row = await db.get_provider_settings(1, config.ChargeProvider.LTC)
    check(provider_row is not None
          and utils.to_decimal(provider_row["charge_rate"]) == Decimal("150"),
          "移行後の DB で方式別レートを登録できる")
    await db.set_destination(
        config.ChargeProvider.PAYPAY, address="paypay-recv", label="受取",
        note=None, updated_by=2,
    )
    check((await db.get_destination(config.ChargeProvider.PAYPAY)) is not None,
          "移行後の DB で入金先を登録できる")
    request_id = await db.create_request(
        guild_id=1, user_id=10, provider=config.ChargeProvider.PAYPAY,
        requested_amount=1000, charge_rate=Decimal("120"), role_id=None,
        estimated_credit=1200, destination="paypay-recv",
    )
    proof = "MIG-TX-0001"
    await db.submit_request_proof(
        request_id, user_id=10, proof_ref=proof,
        proof_hash=utils.proof_hash(config.ChargeProvider.PAYPAY, proof),
        proof_note=None,
    )
    await db.claim_request_for_review(
        request_id, status=config.RequestStatus.APPROVED, operator_id=2
    )
    manual_tx = await db.create_manual_transaction(
        guild_id=1, user_id=10, requested_amount=1000, received_amount=1000,
        charge_rate=Decimal("120"), source=config.TxSource.MANUAL_PAYPAY,
    )
    credited = await db.credit_transaction(manual_tx, 1200)
    check(credited["balance_after"] == 4900,
          f"移行後の DB で申請の承認と残高付与ができる ({credited['balance_after']})")
    audit = await db.audit_balance(1, 10)
    check(audit["diff"] == 0, "申請の承認後も残高と履歴合計が一致")

    # --- 再接続しても壊れない (冪等なマイグレーション) ---
    await db.close()
    db2 = database.Database(config.DB_PATH)
    await db2.connect()
    # 4200 (v1) − 1000 (ショップ購入) + 500 (招待報酬) + 1200 (PayPay承認) = 4900
    final_balance = await db2.get_balance(1, 10)
    check(final_balance == 4900, f"再接続してもデータが保持される ({final_balance})")
    check((await db2.get_request(request_id))["status"] == config.RequestStatus.APPROVED,
          "再接続後も申請の状態が保持される")
    await db2.close()

    print("\n" + "=" * 70)
    print(f"結果: {len(PASS)} 件成功 / {len(FAIL)} 件失敗")
    for label in FAIL:
        print(f"  ✗ {label}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if FAIL else 0)
