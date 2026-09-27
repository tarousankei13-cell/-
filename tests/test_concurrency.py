#!/usr/bin/env python3
"""同時実行の監査: お金が増えも減りもしないことを確かめる。

v4 で追加した「残高が動く経路」を、同時に何度も叩いて次を確認する。

* 同じ処理が二重に通らない (二重課金 / 二重配布 / 二重返金がない)
* 残高と ``balance_history`` の合計が最後まで一致する
* 拒否された処理では残高が中途半端に動かない

Discord へはつながず、DB の層だけを直接叩く。

実行:
    python3 tests/test_concurrency.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

SCRATCH = Path(os.environ.get("CHARGE_BOT_TEST_DIR", "/tmp/charge_bot_conc_test"))
SCRATCH.mkdir(parents=True, exist_ok=True)

import config  # noqa: E402

config.DATA_DIR = SCRATCH
config.DB_PATH = SCRATCH / "test_concurrency.db"
config.SECRET_KEY_PATH = SCRATCH / "secret.key"
config.BACKUP_DIR = SCRATCH / "backups"

import database  # noqa: E402
import utils  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []

G = 90_001


def check(condition: bool, label: str) -> None:
    (PASS if condition else FAIL).append(label)
    print(f"{'  ok  ' if condition else ' FAIL '} {label}")


async def give_balance(db: database.Database, user_id: int, amount: int) -> None:
    await db.ensure_user(G, user_id)
    await db.adjust_balance(
        guild_id=G, user_id=user_id, amount=amount,
        change_type=config.BalanceChangeType.ADMIN_ADD,
        operator_id=1, reason="テスト用の残高",
    )


async def completed_charge(db: database.Database, user_id: int, amount: int) -> str:
    tx = await db.create_proxy_transaction(
        guild_id=G, user_id=user_id, requested_amount=amount,
        received_amount=amount, charge_rate=Decimal("100"),
    )
    await db.credit_transaction(tx, amount)
    return tx


async def main() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(config.DB_PATH) + suffix)
        if path.exists():
            path.unlink()
    db = database.Database(config.DB_PATH)
    await db.connect()
    await db.set_guild_permission(G, "ALLOWED", 1)

    print("=== 1. 残高付与の冪等性 ===")
    user = 91_001
    tx = await db.create_proxy_transaction(
        guild_id=G, user_id=user, requested_amount=1_000,
        received_amount=1_000, charge_rate=Decimal("130"),
    )
    results = await asyncio.gather(
        *[db.credit_transaction(tx, 1_300) for _ in range(12)],
        return_exceptions=True,
    )
    # 2回目以降は例外にせず already_credited を返す契約
    # (呼び出し側が「初回かどうか」を気にしなくて済むようにするため)
    ok = [r for r in results if not isinstance(r, BaseException)]
    fresh = [r for r in ok if not r.get("already_credited")]
    check(len(ok) == 12, f"重複した付与は例外にせず結果を返す ({len(ok)}件)")
    check(len(fresh) == 1, f"実際に付与したのは1回だけ ({len(fresh)}件)")
    balance = await db.get_balance(G, user)
    check(balance == 1_300, f"残高は1回ぶんだけ増える ({balance})")
    history, _ = await db.list_balance_history_filtered(
        G, user_id=user, types=(config.BalanceChangeType.CHARGE,), limit=50
    )
    check(len(history) == 1, f"履歴も1件だけ ({len(history)}件)")

    print("\n=== 2. ショップ購入の同時実行 ===")
    buyer = 91_002
    await give_balance(db, buyer, 10_000)
    item = await db.add_shop_item(
        guild_id=G, role_id=1, name="限定品", price=1_000, stock=3,
        purchase_limit=0, created_by=1,
    )
    results = await asyncio.gather(
        *[db.purchase_shop_item(guild_id=G, user_id=buyer, item_id=item)
          for _ in range(10)],
        return_exceptions=True,
    )
    bought = [r for r in results if not isinstance(r, BaseException)]
    row = await db.get_shop_item(item, G)
    check(len(bought) == 3, f"在庫を超えて買えない ({len(bought)}件)")
    check(int(row["stock"]) == 0, f"在庫が0になる ({row['stock']})")
    check(await db.get_balance(G, buyer) == 10_000 - 3_000,
          f"引き落としは買えたぶんだけ ({await db.get_balance(G, buyer)})")

    print("\n=== 3. 購入上限の同時実行 ===")
    limited = await db.add_shop_item(
        guild_id=G, role_id=2, name="1人1回", price=100, stock=-1,
        purchase_limit=1, created_by=1,
    )
    results = await asyncio.gather(
        *[db.purchase_shop_item(guild_id=G, user_id=buyer, item_id=limited)
          for _ in range(8)],
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, BaseException)]
    check(len(ok) == 1, f"1人1回の上限を同時実行でも守る ({len(ok)}件)")

    print("\n=== 4. サブスク更新の同時実行 ===")
    sub_item = await db.add_shop_item(
        guild_id=G, role_id=3, name="月額", price=500, duration_days=30,
        subscription=True, purchase_limit=0, created_by=1,
    )
    purchase = await db.purchase_shop_item(guild_id=G, user_id=buyer, item_id=sub_item)
    pid = int(purchase["purchase_id"])
    await db.activate_purchase(pid)
    await db.execute(
        "UPDATE shop_purchases SET next_charge_at=?, expires_at=? WHERE id=?",
        (utils.now_ts() - 1, utils.now_ts() - 1, pid),
    )
    before = await db.get_balance(G, buyer)
    outcomes = await asyncio.gather(
        *[db.renew_subscription(pid) for _ in range(10)], return_exceptions=True
    )
    renewed = [o for o in outcomes
               if not isinstance(o, BaseException) and o.get("renewed")]
    check(len(renewed) == 1, f"更新は1回だけ ({len(renewed)}件)")
    check(await db.get_balance(G, buyer) == before - 500,
          f"更新料の引き落としも1回だけ ({await db.get_balance(G, buyer)})")

    print("\n=== 5. オークションの同時入札 ===")
    bidders = [91_010 + i for i in range(6)]
    for who in bidders:
        await give_balance(db, who, 20_000)
    held_before = sum([await db.get_balance(G, w) for w in bidders])
    auction = await db.create_auction(
        guild_id=G, name="同時入札", role_id=4, start_price=1_000,
        min_increment=100, ends_at=utils.now_ts() + 3_600, created_by=1,
    )
    outcomes = await asyncio.gather(
        *[db.place_bid(auction_id=auction, guild_id=G, user_id=w, amount=2_000)
          for w in bidders],
        return_exceptions=True,
    )
    accepted = [o for o in outcomes if not isinstance(o, BaseException)]
    check(len(accepted) == 1, f"同額の同時入札は1件しか通らない ({len(accepted)}件)")
    bids = await db.list_auction_bids(auction, limit=50)
    holding = [b for b in bids if not int(b["refunded"])]
    check(len(holding) == 1, f"預かり中の入札は1件だけ ({len(holding)}件)")
    total_now = sum([await db.get_balance(G, w) for w in bidders])
    check(total_now + int(holding[0]["amount"]) == held_before,
          f"残高と預かり額の合計が保たれる ({total_now} + "
          f"{int(holding[0]['amount'])} = {held_before})")

    # 上書き入札を繰り返しても総額が変わらない
    for index, who in enumerate(bidders[1:], start=1):
        try:
            await db.place_bid(
                auction_id=auction, guild_id=G, user_id=who,
                amount=2_000 + index * 200,
            )
        except database.AuctionError:
            pass
    bids = await db.list_auction_bids(auction, limit=50)
    holding = [b for b in bids if not int(b["refunded"])]
    total_now = sum([await db.get_balance(G, w) for w in bidders])
    check(len(holding) == 1, "入札を重ねても預かりは常に1件")
    check(total_now + int(holding[0]["amount"]) == held_before,
          "入札を重ねても総額が保たれる")

    # 二重に締め切っても結果は1つ
    await db.execute(
        "UPDATE auctions SET ends_at=? WHERE id=?", (utils.now_ts() - 1, auction)
    )
    closes = await asyncio.gather(
        *[db.close_auction(auction) for _ in range(6)], return_exceptions=True
    )
    closed = [c for c in closes
              if not isinstance(c, BaseException) and c.get("closed")]
    check(len(closed) == 1, f"締切の確定は1回だけ ({len(closed)}件)")

    print("\n=== 6. 目標報酬の同時配布 ===")
    goal_users = [91_020 + i for i in range(4)]
    for who in goal_users:
        await completed_charge(db, who, 5_000)
    goal = await db.create_goal(
        guild_id=G, name="同時配布", target_amount=1_000, reward_amount=300,
        starts_at=utils.now_ts() - 60, created_by=1,
    )
    goal_row = await db.get_goal(goal)
    participants = await db.list_goal_participants(goal_row)
    before_map = {w: await db.get_balance(G, w) for w in goal_users}

    async def grant_all() -> int:
        granted = 0
        for row in participants:
            if await db.record_goal_grant(
                goal_id=goal, guild_id=G, user_id=int(row["user_id"]),
                amount=300,
            ):
                await db.adjust_balance(
                    guild_id=G, user_id=int(row["user_id"]), amount=300,
                    change_type=config.BalanceChangeType.GOAL_REWARD,
                    operator_id=config.SYSTEM_ACTOR_ID, reason="目標達成報酬",
                    transaction_id=f"GOAL-{goal}-U{int(row['user_id'])}",
                )
                granted += 1
        return granted

    totals = await asyncio.gather(*[grant_all() for _ in range(5)])
    check(sum(totals) == len(participants),
          f"配布は参加者1人につき1回だけ ({sum(totals)} / {len(participants)}人)")
    after_map = {w: await db.get_balance(G, w) for w in goal_users}
    check(all(after_map[w] == before_map[w] + 300 for w in goal_users),
          f"同時配布でも残高は1回ぶんだけ増える ({list(after_map.values())})")

    print("\n=== 7. 返金申請と取消の同時実行 ===")
    refunder = 91_030
    tx_id = await completed_charge(db, refunder, 4_000)
    requests = await asyncio.gather(
        *[db.create_refund_request(
            guild_id=G, user_id=refunder, transaction_id=tx_id, reason="同時申請")
          for _ in range(6)],
        return_exceptions=True,
    )
    created = [r for r in requests if not isinstance(r, BaseException)]
    check(len(created) == 1, f"同じ取引の申請は1件だけ ({len(created)}件)")
    request_id = int(created[0]["request_id"])
    transitions = await asyncio.gather(
        *[db.transition_refund_request(
            request_id, status=config.RefundRequestStatus.APPROVED, reviewed_by=1)
          for _ in range(6)],
        return_exceptions=True,
    )
    moved = [t for t in transitions if not isinstance(t, BaseException)]
    check(len(moved) == 1, f"承認への遷移は1回だけ ({len(moved)}件)")
    before = await db.get_balance(G, refunder)
    refunds = await asyncio.gather(
        *[db.refund_transaction(tx_id, operator_id=1, reason="同時取消")
          for _ in range(6)],
        return_exceptions=True,
    )
    done = [r for r in refunds if not isinstance(r, BaseException)]
    check(len(done) == 1, f"取消は1回だけ通る ({len(done)}件)")
    check(await db.get_balance(G, refunder) == before - 4_000,
          f"回収も1回ぶんだけ ({await db.get_balance(G, refunder)})")

    print("\n=== 8. 不正検知フラグの同時作成 ===")
    flagged = await asyncio.gather(
        *[db.create_fraud_flag(
            guild_id=G, user_id=refunder, kind=config.FraudKind.BURST_CHARGE,
            severity=config.FraudSeverity.WARN, detail="同時検知")
          for _ in range(8)],
        return_exceptions=True,
    )
    created_flags = [f for f in flagged
                     if not isinstance(f, BaseException) and f[1]]
    rows, total = await db.list_fraud_flags(G, user_id=refunder)
    check(len(created_flags) == 1 and total == 1,
          f"未処理のフラグは1つだけ ({len(created_flags)}件作成 / 全{total}件)")

    print("\n=== 9. 最終的な整合性 ===")
    integrity = await db.integrity_check()
    check(integrity["pragma"] == "ok", "PRAGMA quick_check OK")
    check(not integrity["balance_mismatch"],
          f"全員の残高と履歴合計が一致 ({integrity['balance_mismatch']})")
    check(not integrity["negative_balance"], "負の残高がない")
    await db.close()

    print("\n" + "=" * 70)
    print(f"結果: {len(PASS)} 件成功 / {len(FAIL)} 件失敗")
    for label in FAIL:
        print(f"  ✗ {label}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if FAIL else 0)
