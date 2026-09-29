"""Tests for McDonald's Concierge Bot core logic."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (
    OrderStatus,
    TransactionType,
    VALID_TRANSITIONS,
    calculate_user_amount,
    can_transition,
    clamp_rate,
    day_start_utc,
    hex_digest,
    next_rank,
    normalize_hex,
    normalize_number,
    resolve_rank,
    utc_now,
    validate_hex,
)
from db import Database
from mcd_adapter import _classify, _sanitize, decode_hex_sync


class TestModels(unittest.TestCase):
    def test_calculate_user_amount(self):
        self.assertEqual(calculate_user_amount(1000, 60), 600)
        self.assertEqual(calculate_user_amount(999, 60), 600)
        self.assertEqual(calculate_user_amount(1, 100), 1)
        self.assertEqual(calculate_user_amount(0, 60), 0)

    def test_validate_hex_empty(self):
        self.assertFalse(validate_hex("")[0])

    def test_validate_hex_odd_length(self):
        self.assertFalse(validate_hex("abc")[0])

    def test_validate_hex_invalid_chars(self):
        self.assertFalse(validate_hex("zzzz")[0])

    def test_validate_hex_too_long(self):
        self.assertFalse(validate_hex("aa" * 5001)[0])

    def test_validate_hex_ok(self):
        self.assertTrue(validate_hex("aabbcc")[0])

    def test_state_transitions(self):
        self.assertTrue(can_transition(OrderStatus.PENDING, OrderStatus.PROCESSING))
        self.assertTrue(can_transition(OrderStatus.PROCESSING, OrderStatus.COMPLETED))
        self.assertTrue(can_transition(OrderStatus.COMPLETED, OrderStatus.REFUNDED))
        self.assertFalse(can_transition(OrderStatus.COMPLETED, OrderStatus.PROCESSING))
        self.assertFalse(can_transition(OrderStatus.REFUNDED, OrderStatus.COMPLETED))

    def test_all_statuses_in_transitions(self):
        for status in OrderStatus:
            self.assertIn(status, VALID_TRANSITIONS)

    def test_hex_digest_stable(self):
        self.assertEqual(hex_digest("AABB"), hex_digest("aabb "))
        self.assertNotEqual(hex_digest("aabb"), hex_digest("aabc"))

    def test_rank_progression(self):
        self.assertEqual(resolve_rank(0).name, "ブロンズ")
        self.assertEqual(resolve_rank(9).name, "ブロンズ")
        self.assertEqual(resolve_rank(10).name, "シルバー")
        self.assertEqual(resolve_rank(30).name, "ゴールド")
        self.assertEqual(resolve_rank(60).name, "プラチナ")
        self.assertEqual(resolve_rank(1000).name, "ダイヤモンド")

    def test_next_rank(self):
        self.assertEqual(next_rank(0).name, "シルバー")
        self.assertIsNone(next_rank(1000))

    def test_clamp_rate(self):
        self.assertEqual(clamp_rate(0), 1)
        self.assertEqual(clamp_rate(150), 100)
        self.assertEqual(clamp_rate(55), 55)

    def test_day_start_format(self):
        s = day_start_utc(9)
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestInputNormalization(unittest.TestCase):
    """貼り付け時に混入する文字でHexが弾かれる不具合の回帰テスト。"""

    CLEAN = "0a0531323334"

    def test_newline_in_hex_accepted(self):
        ok, msg = validate_hex("0a053132\n3334")
        self.assertTrue(ok, msg)

    def test_crlf_in_hex_accepted(self):
        self.assertTrue(validate_hex("0a05\r\n3132\r\n3334")[0])

    def test_spaces_in_hex_accepted(self):
        self.assertTrue(validate_hex("0a 05 31 32 33 34")[0])

    def test_tab_in_hex_accepted(self):
        self.assertTrue(validate_hex("0a05\t31323334")[0])

    def test_fullwidth_space_accepted(self):
        self.assertTrue(validate_hex("0a05　3132 3334")[0])

    def test_fullwidth_digits_accepted(self):
        self.assertTrue(validate_hex("０ａ０５31323334")[0])

    def test_zero_width_chars_stripped(self):
        self.assertTrue(validate_hex("0a05​3132﻿3334")[0])

    def test_normalize_hex_equivalence(self):
        for dirty in ("0a05\n3132 3334", "0a05\t31323334", " 0a0531323334 "):
            self.assertEqual(normalize_hex(dirty), self.CLEAN)

    def test_digest_stable_across_whitespace(self):
        self.assertEqual(hex_digest(self.CLEAN), hex_digest("0a05\n3132 3334"))

    def test_truly_odd_hex_still_rejected(self):
        ok, msg = validate_hex("0a053132333")
        self.assertFalse(ok)
        self.assertIn("不正", msg)

    def test_non_hex_still_rejected(self):
        self.assertFalse(validate_hex("zzzz")[0])

    def test_normalize_number_variants(self):
        for raw in ("1000", "1,000", "¥1000", "￥1000", "１０００", "1000円", " 1000 "):
            self.assertEqual(normalize_number(raw), "1000")


class AsyncTestCase(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)


class DBTestCase(AsyncTestCase):
    def setUp(self):
        super().setUp()
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db = Database(self.tmpfile.name)
        self.run_async(self.db.initialize())

    def tearDown(self):
        self.run_async(self.db.close())
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.tmpfile.name + suffix)
            except OSError:
                pass
        super().tearDown()

    def make_order(self, user_id=1, total=1000, user_amount=600, hex_data="aa"):
        return self.run_async(
            self.db.create_order_with_payment(
                user_id=user_id, store_id="1", store_name="T",
                pickup_method="", total_amount=total,
                user_amount=user_amount, subsidy_amount=total - user_amount,
                hex_data=hex_data, products_json="[]",
            )
        )

    def complete(self, oid, receipt=""):
        self.run_async(self.db.update_order_status(oid, OrderStatus.PROCESSING))
        self.run_async(
            self.db.update_order_status(
                oid, OrderStatus.COMPLETED, receipt_number=receipt
            )
        )


class TestDatabase(DBTestCase):
    def test_db_init(self):
        stats = self.run_async(self.db.get_stats())
        self.assertEqual(stats["user_count"], 0)
        self.assertEqual(stats["total_orders"], 0)

    def test_user_creation(self):
        self.run_async(self.db.ensure_user(12345))
        self.assertEqual(self.run_async(self.db.get_balance(12345)), 0)
        self.assertEqual(self.run_async(self.db.get_user_count()), 1)

    def test_balance_add(self):
        new = self.run_async(
            self.db.add_balance(1, 1000, TransactionType.ADMIN_ADD, "test")
        )
        self.assertEqual(new, 1000)
        self.assertEqual(self.run_async(self.db.get_balance(1)), 1000)

    def test_balance_deduct(self):
        self.run_async(self.db.add_balance(1, 1000, TransactionType.ADMIN_ADD))
        new = self.run_async(
            self.db.deduct_balance(1, 600, TransactionType.ORDER_PAYMENT)
        )
        self.assertEqual(new, 400)

    def test_balance_insufficient(self):
        self.run_async(self.db.add_balance(1, 100, TransactionType.ADMIN_ADD))
        with self.assertRaises(ValueError):
            self.run_async(
                self.db.deduct_balance(1, 200, TransactionType.ORDER_PAYMENT)
            )
        self.assertEqual(self.run_async(self.db.get_balance(1)), 100)

    def test_transaction_ledger(self):
        self.run_async(self.db.add_balance(1, 1000, TransactionType.ADMIN_ADD))
        self.run_async(self.db.deduct_balance(1, 300, TransactionType.ORDER_PAYMENT))
        txs = self.run_async(self.db.get_transactions(1))
        self.assertEqual(len(txs), 2)
        self.assertEqual(txs[0]["balance_after"], 700)
        self.assertEqual(txs[1]["balance_after"], 1000)

    def test_balance_cache_consistency(self):
        self.run_async(self.db.add_balance(1, 1000, TransactionType.ADMIN_ADD))
        self.assertEqual(self.run_async(self.db.get_balance(1)), 1000)
        self.run_async(self.db.deduct_balance(1, 400, TransactionType.ADMIN_REMOVE))
        self.assertEqual(self.run_async(self.db.get_balance(1)), 600)
        self.run_async(self.db.set_balance(1, 50))
        self.assertEqual(self.run_async(self.db.get_balance(1)), 50)

    def test_deposit_create_and_approve(self):
        self.run_async(self.db.ensure_user(1))
        dep_id = self.run_async(self.db.create_deposit(1, 500))
        user_id, amount, new_bal = self.run_async(self.db.approve_deposit(dep_id, 99))
        self.assertEqual((user_id, amount, new_bal), (1, 500, 500))
        self.assertEqual(self.run_async(self.db.get_balance(1)), 500)

    def test_double_approval_prevention(self):
        self.run_async(self.db.ensure_user(1))
        dep_id = self.run_async(self.db.create_deposit(1, 500))
        self.run_async(self.db.approve_deposit(dep_id, 99))
        with self.assertRaises(ValueError):
            self.run_async(self.db.approve_deposit(dep_id, 99))

    def test_order_creation(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        self.assertGreater(oid, 0)
        self.assertEqual(self.run_async(self.db.get_balance(1)), 1400)

    def test_double_order_prevention(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        self.make_order(hex_data="aa")
        with self.assertRaises(ValueError):
            self.make_order(hex_data="bb")

    def test_order_completion(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        self.complete(oid, "42")
        order = self.run_async(self.db.get_order(oid))
        self.assertEqual(order["status"], "completed")
        self.assertEqual(order["receipt_number"], "42")

    def test_refund(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        self.complete(oid)
        self.assertEqual(self.run_async(self.db.refund_order(oid)), 600)
        self.assertEqual(self.run_async(self.db.get_balance(1)), 2000)

    def test_double_refund_prevention(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        self.complete(oid)
        self.run_async(self.db.refund_order(oid))
        with self.assertRaises(ValueError):
            self.run_async(self.db.refund_order(oid))

    def test_manual_review(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        self.run_async(self.db.update_order_status(oid, OrderStatus.PROCESSING))
        self.run_async(self.db.update_order_status(oid, OrderStatus.MANUAL_REVIEW))
        self.run_async(self.db.update_order_status(oid, OrderStatus.COMPLETED))
        self.assertEqual(
            self.run_async(self.db.get_order(oid))["status"], "completed"
        )

    def test_invalid_transition(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        with self.assertRaises(ValueError):
            self.run_async(
                self.db.update_order_status(oid, OrderStatus.COMPLETED)
            )

    def test_settings_cache(self):
        self.assertEqual(self.run_async(self.db.get_setting("user_rate")), "60")
        self.run_async(self.db.set_setting("user_rate", "70"))
        self.assertEqual(self.run_async(self.db.get_setting("user_rate")), "70")
        self.assertEqual(self.run_async(self.db.get_int_setting("user_rate")), 70)

    def test_panel_persistence(self):
        self.run_async(self.db.save_panel(111, 222, 333))
        self.assertEqual(self.run_async(self.db.get_panel(111))["message_id"], 333)
        self.run_async(self.db.save_panel(111, 222, 444))
        self.assertEqual(self.run_async(self.db.get_panel(111))["message_id"], 444)

    def test_achievement_flags(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        self.complete(oid)
        self.run_async(self.db.set_achievement_posted(oid))
        self.assertEqual(
            self.run_async(self.db.get_order(oid))["achievement_posted"], 1
        )
        self.run_async(self.db.reset_achievement_posted(oid))
        self.assertEqual(
            self.run_async(self.db.get_order(oid))["achievement_posted"], 0
        )

    def test_export_orders(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        self.make_order()
        self.assertEqual(len(self.run_async(self.db.export_orders())), 1)

    def test_search_orders(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        self.make_order()
        self.assertEqual(len(self.run_async(self.db.search_orders(user_id=1))), 1)
        self.assertEqual(
            len(self.run_async(self.db.search_orders(status="pending"))), 1
        )
        self.assertEqual(
            len(self.run_async(self.db.search_orders(status="completed"))), 0
        )

    def test_backup(self):
        self.run_async(self.db.add_balance(1, 1000, TransactionType.ADMIN_ADD))
        dest = self.tmpfile.name + ".bak"
        size = self.run_async(self.db.backup_to(dest))
        self.assertGreater(size, 0)
        self.assertTrue(os.path.exists(dest))
        os.unlink(dest)


class TestHexReuse(DBTestCase):
    def test_hex_reuse_detected(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.make_order(hex_data="deadbeef")
        found = self.run_async(self.db.find_reused_hex("deadbeef"))
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], oid)

    def test_hex_reuse_case_insensitive(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        self.make_order(hex_data="deadbeef")
        self.assertIsNotNone(self.run_async(self.db.find_reused_hex("DEADBEEF")))

    def test_hex_reuse_ignores_failed(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.make_order(hex_data="cafe")
        self.run_async(self.db.update_order_status(oid, OrderStatus.PROCESSING))
        self.run_async(self.db.update_order_status(oid, OrderStatus.FAILED))
        self.assertIsNone(self.run_async(self.db.find_reused_hex("cafe")))

    def test_hex_reuse_ignores_refunded(self):
        """返金済みの注文は再注文を妨げない。"""
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.make_order(hex_data="beef")
        self.complete(oid)
        self.run_async(self.db.refund_order(oid))
        self.assertIsNone(self.run_async(self.db.find_reused_hex("beef")))

    def test_hex_reuse_manual_review_optional(self):
        """外部API未設定時（manual_review）は既定で再注文を妨げない。"""
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.make_order(hex_data="f00d")
        self.run_async(self.db.update_order_status(oid, OrderStatus.PROCESSING))
        self.run_async(self.db.update_order_status(oid, OrderStatus.MANUAL_REVIEW))
        self.assertIsNone(self.run_async(self.db.find_reused_hex("f00d")))
        # API有効時は要確認も重複扱いにできる
        blocked = self.run_async(
            self.db.find_reused_hex(
                "f00d", ("pending", "processing", "completed", "manual_review")
            )
        )
        self.assertIsNotNone(blocked)

    def test_hex_reuse_blocks_completed(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.make_order(hex_data="abcd")
        self.complete(oid)
        found = self.run_async(self.db.find_reused_hex("abcd"))
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], oid)

    def test_hex_reuse_matches_whitespace_variant(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        self.make_order(hex_data="0a0531323334")
        self.assertIsNotNone(
            self.run_async(self.db.find_reused_hex("0a05\n3132 3334"))
        )

    def test_unrelated_hex_not_flagged(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        self.make_order(hex_data="aabb")
        self.assertIsNone(self.run_async(self.db.find_reused_hex("ccdd")))

    def test_daily_count(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        self.assertEqual(self.run_async(self.db.count_orders_today(1)), 0)
        self.make_order()
        self.assertEqual(self.run_async(self.db.count_orders_today(1)), 1)


class TestRateResolution(DBTestCase):
    def test_base_rate(self):
        br = self.run_async(self.db.resolve_rate(1))
        self.assertEqual(br.final, 60)
        self.assertFalse(br.vip)

    def test_vip_rate(self):
        self.run_async(self.db.set_custom_rate(1, 40))
        br = self.run_async(self.db.resolve_rate(1))
        self.assertTrue(br.vip)
        self.assertEqual(br.base, 40)
        self.assertEqual(br.final, 40)

    def test_vip_clear(self):
        self.run_async(self.db.set_custom_rate(1, 40))
        self.run_async(self.db.set_custom_rate(1, None))
        self.assertFalse(self.run_async(self.db.resolve_rate(1)).vip)

    def test_campaign_rate(self):
        self.run_async(self.db.set_setting("campaign_rate", "50"))
        self.run_async(self.db.set_setting("campaign_end", ""))
        br = self.run_async(self.db.resolve_rate(1))
        self.assertTrue(br.campaign)
        self.assertEqual(br.final, 50)

    def test_expired_campaign_ignored(self):
        past = (utc_now() - timedelta(hours=1)).isoformat()
        self.run_async(self.db.set_setting("campaign_rate", "50"))
        self.run_async(self.db.set_setting("campaign_end", past))
        br = self.run_async(self.db.resolve_rate(1))
        self.assertFalse(br.campaign)
        self.assertEqual(br.final, 60)

    def test_rank_discount_applied(self):
        self.run_async(self.db.add_balance(1, 100000, TransactionType.ADMIN_ADD))
        for i in range(10):
            oid = self.make_order(hex_data=f"{i:04x}")
            self.complete(oid)
        br = self.run_async(self.db.resolve_rate(1))
        self.assertEqual(br.rank.name, "シルバー")
        self.assertEqual(br.rank_bonus, 2)
        self.assertEqual(br.final, 58)

    def test_rank_disabled(self):
        self.run_async(self.db.set_setting("rank_enabled", "0"))
        br = self.run_async(self.db.resolve_rate(1))
        self.assertEqual(br.rank_bonus, 0)
        self.assertEqual(br.final, 60)

    def test_coupon_applied(self):
        self.run_async(self.db.create_coupon("SAVE10", 10))
        self.run_async(self.db.set_active_coupon(1, "SAVE10"))
        br = self.run_async(self.db.resolve_rate(1))
        self.assertEqual(br.coupon_code, "SAVE10")
        self.assertEqual(br.final, 50)

    def test_rate_never_below_one(self):
        self.run_async(self.db.set_custom_rate(1, 2))
        self.run_async(self.db.create_coupon("HUGE", 99))
        self.run_async(self.db.set_active_coupon(1, "HUGE"))
        self.assertEqual(self.run_async(self.db.resolve_rate(1)).final, 1)


class TestCoupons(DBTestCase):
    def test_create_and_get(self):
        self.run_async(self.db.create_coupon("abc", 5, uses=3, per_user=1))
        coupon = self.run_async(self.db.get_coupon("ABC"))
        self.assertEqual(coupon["bonus"], 5)
        self.assertEqual(coupon["uses_left"], 3)

    def test_usable_then_consumed(self):
        self.run_async(self.db.create_coupon("ONE", 5, uses=1, per_user=1))
        self.assertTrue(self.run_async(self.db.is_coupon_usable("ONE", 1)))
        self.run_async(self.db.consume_coupon("ONE", 1, 1))
        self.assertFalse(self.run_async(self.db.is_coupon_usable("ONE", 1)))

    def test_per_user_limit(self):
        self.run_async(self.db.create_coupon("MULTI", 5, uses=-1, per_user=1))
        self.run_async(self.db.consume_coupon("MULTI", 1, 1))
        self.assertFalse(self.run_async(self.db.is_coupon_usable("MULTI", 1)))
        self.assertTrue(self.run_async(self.db.is_coupon_usable("MULTI", 2)))

    def test_expired_coupon(self):
        past = (utc_now() - timedelta(days=1)).isoformat()
        self.run_async(self.db.create_coupon("OLD", 5, expires_at=past))
        self.assertFalse(self.run_async(self.db.is_coupon_usable("OLD", 1)))

    def test_consume_clears_active(self):
        self.run_async(self.db.create_coupon("X", 5))
        self.run_async(self.db.set_active_coupon(1, "X"))
        self.run_async(self.db.consume_coupon("X", 1, 1))
        self.assertEqual(self.run_async(self.db.get_user(1))["active_coupon"], "")

    def test_delete_coupon(self):
        self.run_async(self.db.create_coupon("DEL", 5))
        self.assertTrue(self.run_async(self.db.delete_coupon("DEL")))
        self.assertIsNone(self.run_async(self.db.get_coupon("DEL")))


class TestPointsAndBlacklist(DBTestCase):
    def test_add_points(self):
        self.assertEqual(self.run_async(self.db.add_points(1, 100)), 100)
        self.assertEqual(self.run_async(self.db.add_points(1, 50)), 150)

    def test_redeem_points(self):
        self.run_async(self.db.add_points(1, 500))
        remaining, balance = self.run_async(self.db.redeem_points(1, 200))
        self.assertEqual(remaining, 300)
        self.assertEqual(balance, 200)
        self.assertEqual(self.run_async(self.db.get_balance(1)), 200)

    def test_redeem_insufficient(self):
        self.run_async(self.db.add_points(1, 100))
        with self.assertRaises(ValueError):
            self.run_async(self.db.redeem_points(1, 500))
        self.assertEqual(self.run_async(self.db.get_points(1)), 100)

    def test_blacklist(self):
        self.run_async(self.db.set_blacklist(1, True, "迷惑行為"))
        blocked, reason = self.run_async(self.db.is_blacklisted(1))
        self.assertTrue(blocked)
        self.assertEqual(reason, "迷惑行為")
        self.assertEqual(len(self.run_async(self.db.get_blacklist())), 1)

    def test_blacklist_remove(self):
        self.run_async(self.db.set_blacklist(1, True, "x"))
        self.run_async(self.db.set_blacklist(1, False, ""))
        self.assertFalse(self.run_async(self.db.is_blacklisted(1))[0])

    def test_notify_toggle(self):
        self.run_async(self.db.set_notify(1, False))
        self.assertEqual(self.run_async(self.db.get_user(1))["notify_dm"], 0)


class TestFavoritesAndProducts(DBTestCase):
    def test_favorite_crud(self):
        fid = self.run_async(self.db.add_favorite(1, "いつもの", "aabb"))
        favs = self.run_async(self.db.get_favorites(1))
        self.assertEqual(len(favs), 1)
        self.assertEqual(favs[0]["name"], "いつもの")
        self.assertTrue(self.run_async(self.db.delete_favorite(fid, 1)))
        self.assertEqual(len(self.run_async(self.db.get_favorites(1))), 0)

    def test_favorite_owner_isolation(self):
        fid = self.run_async(self.db.add_favorite(1, "mine", "aa"))
        self.assertIsNone(self.run_async(self.db.get_favorite(fid, 2)))
        self.assertFalse(self.run_async(self.db.delete_favorite(fid, 2)))

    def test_product_names(self):
        self.run_async(self.db.set_product_name("P100", "ビッグマック"))
        self.assertEqual(
            self.run_async(self.db.get_product_name("P100")), "ビッグマック"
        )
        self.assertTrue(self.run_async(self.db.delete_product_name("P100")))

    def test_unknown_product_recorded(self):
        decoded = decode_hex_sync("0a023132")
        from models import ProductInfo
        decoded.products = [ProductInfo(product_id="UNKNOWN1")]
        self.run_async(self.db.resolve_product_names(decoded))
        unknown = self.run_async(self.db.list_unknown_products())
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["product_id"], "UNKNOWN1")

    def test_known_product_resolved(self):
        self.run_async(self.db.set_product_name("P1", "ポテト"))
        from models import ProductInfo
        decoded = decode_hex_sync("0a023132")
        decoded.products = [ProductInfo(product_id="P1")]
        self.run_async(self.db.resolve_product_names(decoded))
        self.assertEqual(decoded.products[0].display_name, "ポテト")


class TestStats(DBTestCase):
    def test_daily_stats(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        self.complete(oid)
        rows = self.run_async(self.db.get_daily_stats(7))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cnt"], 1)
        self.assertEqual(rows[0]["revenue"], 600)

    def test_top_stores(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        self.complete(oid)
        top = self.run_async(self.db.get_top_stores())
        self.assertEqual(top[0]["store"], "T")

    def test_user_savings(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.make_order(total=1000, user_amount=600)
        self.complete(oid)
        self.assertEqual(self.run_async(self.db.get_user_savings(1)), 400)

    def test_max_order_id(self):
        self.assertEqual(self.run_async(self.db.get_max_order_id()), 0)
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.make_order()
        self.assertEqual(self.run_async(self.db.get_max_order_id()), oid)


class TestMigration(AsyncTestCase):
    def test_migrate_from_v1(self):
        """v1相当のDBを作りマイグレーションが通ることを確認する。"""
        import aiosqlite

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()

        async def build_v1():
            conn = await aiosqlite.connect(tmp.name, isolation_level=None)
            await conn.executescript("""
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
                INSERT INTO schema_version VALUES (1);
                CREATE TABLE users (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO users (user_id, balance) VALUES (42, 1500);
                CREATE TABLE orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    store_id TEXT NOT NULL DEFAULT '',
                    store_name TEXT NOT NULL DEFAULT '',
                    pickup_method TEXT NOT NULL DEFAULT '',
                    total_amount INTEGER NOT NULL DEFAULT 0,
                    user_amount INTEGER NOT NULL DEFAULT 0,
                    subsidy_amount INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    receipt_number TEXT NOT NULL DEFAULT '',
                    order_token TEXT NOT NULL DEFAULT '',
                    order_group TEXT NOT NULL DEFAULT '',
                    hex_data TEXT NOT NULL DEFAULT '',
                    products_json TEXT NOT NULL DEFAULT '[]',
                    error_info TEXT NOT NULL DEFAULT '',
                    achievement_posted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    completed_at TEXT NOT NULL DEFAULT ''
                );
            """)
            await conn.close()

        self.run_async(build_v1())

        db = Database(tmp.name)
        self.run_async(db.initialize())

        # 既存データが保持されている
        self.assertEqual(self.run_async(db.get_balance(42)), 1500)
        # 新カラムが使える
        self.assertEqual(self.run_async(db.get_points(42)), 0)
        br = self.run_async(db.resolve_rate(42))
        self.assertEqual(br.final, 60)

        self.run_async(db.close())
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(tmp.name + suffix)
            except OSError:
                pass


class TestHexDecoder(unittest.TestCase):
    def test_malformed_hex(self):
        with self.assertRaises(ValueError):
            decode_hex_sync("zzzz")

    def test_empty_decode(self):
        self.assertEqual(decode_hex_sync("0a00").total_amount, 0)

    def test_short_hex(self):
        self.assertEqual(decode_hex_sync("0a023132").store_id, "12")


class TestErrorClassification(unittest.TestCase):
    def test_timeout_not_retried(self):
        self.assertEqual(_classify("Read timed out"), "timeout")
        self.assertEqual(_classify("Connection timeout"), "timeout")

    def test_retryable(self):
        self.assertEqual(_classify("Connection refused"), "retryable")
        self.assertEqual(_classify("503 Service Unavailable"), "retryable")

    def test_fatal(self):
        self.assertEqual(_classify("Invalid order payload"), "fatal")

    def test_sanitize_hides_secrets(self):
        self.assertEqual(
            _sanitize("Invalid Bearer token abc123"), "外部API処理エラー"
        )
        self.assertEqual(
            _sanitize("authorization header missing"), "外部API処理エラー"
        )

    def test_sanitize_keeps_safe(self):
        self.assertEqual(_sanitize("Store is closed"), "Store is closed")


# Unicode Emoji_Presentation=Yes（VS16なしで絵文字として表示される）BMP範囲
_BMP_EMOJI_PRESENTATION = (
    (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3),
    (0x25FD, 0x25FE), (0x2614, 0x2615), (0x2648, 0x2653), (0x267F, 0x267F),
    (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE),
    (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4), (0x26EA, 0x26EA),
    (0x26F2, 0x26F3), (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD),
    (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C),
    (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797),
    (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50),
    (0x2B55, 0x2B55),
)

# SMPだが Emoji_Presentation=No（VS16が必要）な主な文字
_SMP_NEEDS_VS16 = (
    (0x1F321, 0x1F321), (0x1F324, 0x1F32C), (0x1F336, 0x1F336),
    (0x1F37D, 0x1F37D), (0x1F396, 0x1F397), (0x1F399, 0x1F39B),
    (0x1F39E, 0x1F39F), (0x1F3CB, 0x1F3CE), (0x1F3D4, 0x1F3DF),
    (0x1F3F3, 0x1F3F3), (0x1F3F5, 0x1F3F5), (0x1F3F7, 0x1F3F7),
    (0x1F43F, 0x1F43F), (0x1F441, 0x1F441), (0x1F4FD, 0x1F4FD),
    (0x1F549, 0x1F54A), (0x1F56F, 0x1F570), (0x1F573, 0x1F579),
    (0x1F587, 0x1F587), (0x1F58A, 0x1F58D), (0x1F590, 0x1F590),
    (0x1F5A5, 0x1F5A5), (0x1F5A8, 0x1F5A8), (0x1F5B1, 0x1F5B2),
    (0x1F5BC, 0x1F5BC), (0x1F5C2, 0x1F5C4), (0x1F5D1, 0x1F5D3),
    (0x1F5DC, 0x1F5DE), (0x1F5E1, 0x1F5E1), (0x1F5E3, 0x1F5E3),
    (0x1F5E8, 0x1F5E8), (0x1F5EF, 0x1F5EF), (0x1F5F3, 0x1F5F3),
    (0x1F5FA, 0x1F5FA), (0x1F6CB, 0x1F6CB), (0x1F6CD, 0x1F6CF),
    (0x1F6E0, 0x1F6E5), (0x1F6E9, 0x1F6E9), (0x1F6F0, 0x1F6F0),
    (0x1F6F3, 0x1F6F3),
)


def _in_ranges(cp: int, ranges) -> bool:
    return any(lo <= cp <= hi for lo, hi in ranges)


def check_button_emoji(value: str) -> str:
    """Discordがボタン絵文字として受け付けるか判定し、問題があれば理由を返す。"""
    if not value:
        return "空文字"
    if "️" in value:
        return ""  # VS16付きは絵文字表示が明示されている
    cp = ord(value[0])
    if cp < 0x1F000:
        if _in_ranges(cp, _BMP_EMOJI_PRESENTATION):
            return ""
        return (
            f"U+{cp:04X} は Emoji_Presentation=No / 絵文字ではありません"
            "（DiscordがInvalid emojiで拒否します）"
        )
    if _in_ranges(cp, _SMP_NEEDS_VS16):
        return f"U+{cp:04X} は VS16(U+FE0F) が必要です"
    return ""


class TestButtonEmoji(unittest.TestCase):
    """ボタン絵文字がDiscordに拒否される不具合の回帰テスト。

    U+2715(✕) のような「記号だが絵文字ではない」文字を使うと
    400 Invalid Form Body になり、そのViewを含むメッセージが一切送れなくなる。
    """

    def _all_views(self):
        import discord
        import views as v

        return [
            ("PanelView", v.PanelView()),
            ("OrderConfirmView", v.OrderConfirmView(1, None, 600, 400, None)),
            ("SaveFavoriteView", v.SaveFavoriteView(1, "aa")),
            ("FavoritesView", v.FavoritesView(1, [
                {"id": 1, "name": "test", "created_at": "2026-01-01"}
            ])),
            ("TopUpView", v.TopUpView(1, 500)),
            ("DepositConfirmView", v.DepositConfirmView(1, 1000)),
            ("HistoryView", v.HistoryView(1, 1, 1)),
            ("BalanceDetailView", v.BalanceDetailView(1)),
            ("TxHistoryView", v.TxHistoryView(1, 1, 1)),
        ]

    def test_all_button_emoji_valid(self):
        problems = []
        for name, view in self._all_views():
            for child in view.children:
                emoji = getattr(child, "emoji", None)
                if emoji is None:
                    continue
                reason = check_button_emoji(str(emoji))
                if reason:
                    problems.append(
                        f"{name}.{getattr(child, 'label', '?')}: "
                        f"{str(emoji)!r} -> {reason}"
                    )
        self.assertEqual(problems, [], "不正なボタン絵文字:\n" + "\n".join(problems))

    def test_validator_rejects_multiplication_x(self):
        # 実際に400を引き起こした文字
        self.assertNotEqual(check_button_emoji("✕"), "")

    def test_validator_rejects_check_mark(self):
        self.assertNotEqual(check_button_emoji("✓"), "")

    def test_validator_accepts_known_good(self):
        for good in ("❌", "✅", "⭐", "❓", "🍔", "💳", "🎁"):
            self.assertEqual(check_button_emoji(good), "", f"{good!r} が誤判定")

    def test_validator_accepts_vs16(self):
        self.assertEqual(check_button_emoji("\U0001f5d1️"), "")

    def test_validator_flags_missing_vs16(self):
        self.assertNotEqual(check_button_emoji("\U0001f5d1"), "")

    def test_panel_view_components_serialize(self):
        for name, view in self._all_views():
            try:
                view.to_components()
            except Exception as exc:
                self.fail(f"{name}.to_components() failed: {exc}")


class TestImageGen(unittest.TestCase):
    def test_available(self):
        import image_gen
        self.assertTrue(image_gen.is_available())

    def test_render_produces_png(self):
        import image_gen
        data = image_gen.render_order_complete_sync("7161")
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertGreater(len(data), 1000)

    def test_render_various_lengths(self):
        import image_gen
        for n in ["1", "842", "7161", "12345"]:
            self.assertTrue(
                image_gen.render_order_complete_sync(n).startswith(b"\x89PNG")
            )

    def test_render_empty_fallback(self):
        import image_gen
        self.assertTrue(
            image_gen.render_order_complete_sync("").startswith(b"\x89PNG")
        )


class TestRaceConditions(DBTestCase):
    def test_concurrent_balance_adds(self):
        self.run_async(self.db.ensure_user(1))

        async def add_many():
            await asyncio.gather(*[
                self.db.add_balance(1, 100, TransactionType.ADMIN_ADD)
                for _ in range(10)
            ])

        self.run_async(add_many())
        self.assertEqual(self.run_async(self.db.get_balance(1)), 1000)

    def test_concurrent_deposits(self):
        self.run_async(self.db.ensure_user(1))
        dep1 = self.run_async(self.db.create_deposit(1, 500))
        dep2 = self.run_async(self.db.create_deposit(1, 300))

        async def approve_both():
            await asyncio.gather(
                self.db.approve_deposit(dep1, 99),
                self.db.approve_deposit(dep2, 99),
            )

        self.run_async(approve_both())
        self.assertEqual(self.run_async(self.db.get_balance(1)), 800)

    def test_concurrent_same_deposit_only_once(self):
        self.run_async(self.db.ensure_user(1))
        dep = self.run_async(self.db.create_deposit(1, 500))

        async def approve_twice():
            return await asyncio.gather(
                self.db.approve_deposit(dep, 99),
                self.db.approve_deposit(dep, 99),
                return_exceptions=True,
            )

        results = self.run_async(approve_twice())
        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.run_async(self.db.get_balance(1)), 500)

    def test_concurrent_point_redeem(self):
        self.run_async(self.db.add_points(1, 100))

        async def redeem_twice():
            return await asyncio.gather(
                self.db.redeem_points(1, 100),
                self.db.redeem_points(1, 100),
                return_exceptions=True,
            )

        results = self.run_async(redeem_twice())
        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.run_async(self.db.get_points(1)), 0)
        self.assertEqual(self.run_async(self.db.get_balance(1)), 100)


if __name__ == "__main__":
    unittest.main()
