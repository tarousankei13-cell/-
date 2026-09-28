"""Tests for McDonald's Concierge Bot core logic."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import (
    OrderStatus,
    TransactionType,
    can_transition,
    calculate_user_amount,
    validate_hex,
    VALID_TRANSITIONS,
)
from db import Database
from mcd_adapter import decode_hex_sync


class TestModels(unittest.TestCase):
    def test_calculate_user_amount(self):
        self.assertEqual(calculate_user_amount(1000, 60), 600)
        self.assertEqual(calculate_user_amount(999, 60), 600)  # ceil(999*60/100)=600
        self.assertEqual(calculate_user_amount(1, 100), 1)
        self.assertEqual(calculate_user_amount(0, 60), 0)

    def test_validate_hex_empty(self):
        ok, _ = validate_hex("")
        self.assertFalse(ok)

    def test_validate_hex_odd_length(self):
        ok, _ = validate_hex("abc")
        self.assertFalse(ok)

    def test_validate_hex_invalid_chars(self):
        ok, _ = validate_hex("zzzz")
        self.assertFalse(ok)

    def test_validate_hex_too_long(self):
        ok, _ = validate_hex("aa" * 5001)
        self.assertFalse(ok)

    def test_validate_hex_ok(self):
        ok, _ = validate_hex("aabbcc")
        self.assertTrue(ok)

    def test_state_transitions(self):
        self.assertTrue(can_transition(OrderStatus.PENDING, OrderStatus.PROCESSING))
        self.assertTrue(can_transition(OrderStatus.PROCESSING, OrderStatus.COMPLETED))
        self.assertTrue(can_transition(OrderStatus.COMPLETED, OrderStatus.REFUNDED))
        self.assertFalse(can_transition(OrderStatus.COMPLETED, OrderStatus.PROCESSING))
        self.assertFalse(can_transition(OrderStatus.REFUNDED, OrderStatus.COMPLETED))
        self.assertFalse(can_transition(OrderStatus.CANCELLED, OrderStatus.PROCESSING))

    def test_all_statuses_in_transitions(self):
        for status in OrderStatus:
            self.assertIn(status, VALID_TRANSITIONS)


class AsyncTestCase(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

    def tearDown(self):
        self.loop.close()

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)


class TestDatabase(AsyncTestCase):
    def setUp(self):
        super().setUp()
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db = Database(self.tmpfile.name)
        self.run_async(self.db.initialize())

    def tearDown(self):
        self.run_async(self.db.close())
        os.unlink(self.tmpfile.name)
        super().tearDown()

    def test_db_init(self):
        stats = self.run_async(self.db.get_stats())
        self.assertEqual(stats["user_count"], 0)
        self.assertEqual(stats["total_orders"], 0)

    def test_user_creation(self):
        self.run_async(self.db.ensure_user(12345))
        balance = self.run_async(self.db.get_balance(12345))
        self.assertEqual(balance, 0)
        count = self.run_async(self.db.get_user_count())
        self.assertEqual(count, 1)

    def test_balance_add(self):
        new = self.run_async(
            self.db.add_balance(1, 1000, TransactionType.ADMIN_ADD, "test")
        )
        self.assertEqual(new, 1000)
        balance = self.run_async(self.db.get_balance(1))
        self.assertEqual(balance, 1000)

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
        balance = self.run_async(self.db.get_balance(1))
        self.assertEqual(balance, 100)

    def test_transaction_ledger(self):
        self.run_async(self.db.add_balance(1, 1000, TransactionType.ADMIN_ADD))
        self.run_async(self.db.deduct_balance(1, 300, TransactionType.ORDER_PAYMENT))
        txs = self.run_async(self.db.get_transactions(1))
        self.assertEqual(len(txs), 2)
        self.assertEqual(txs[0]["balance_after"], 700)
        self.assertEqual(txs[1]["balance_after"], 1000)

    def test_deposit_create_and_approve(self):
        self.run_async(self.db.ensure_user(1))
        dep_id = self.run_async(self.db.create_deposit(1, 500))
        user_id, amount, new_bal = self.run_async(self.db.approve_deposit(dep_id, 99))
        self.assertEqual(user_id, 1)
        self.assertEqual(amount, 500)
        self.assertEqual(new_bal, 500)

    def test_double_approval_prevention(self):
        self.run_async(self.db.ensure_user(1))
        dep_id = self.run_async(self.db.create_deposit(1, 500))
        self.run_async(self.db.approve_deposit(dep_id, 99))
        with self.assertRaises(ValueError):
            self.run_async(self.db.approve_deposit(dep_id, 99))

    def test_order_creation(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="123", store_name="Test",
                pickup_method="テイクアウト", total_amount=1000,
                user_amount=600, subsidy_amount=400,
                hex_data="aabb", products_json="[]",
            )
        )
        self.assertGreater(oid, 0)
        balance = self.run_async(self.db.get_balance(1))
        self.assertEqual(balance, 1400)

    def test_double_order_prevention(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="1", store_name="T",
                pickup_method="", total_amount=100,
                user_amount=60, subsidy_amount=40,
                hex_data="aa", products_json="[]",
            )
        )
        with self.assertRaises(ValueError):
            self.run_async(
                self.db.create_order_with_payment(
                    user_id=1, store_id="2", store_name="T2",
                    pickup_method="", total_amount=100,
                    user_amount=60, subsidy_amount=40,
                    hex_data="bb", products_json="[]",
                )
            )

    def test_order_completion(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="1", store_name="T",
                pickup_method="", total_amount=1000,
                user_amount=600, subsidy_amount=400,
                hex_data="aa", products_json="[]",
            )
        )
        self.run_async(self.db.update_order_status(oid, OrderStatus.PROCESSING))
        self.run_async(
            self.db.update_order_status(
                oid, OrderStatus.COMPLETED, receipt_number="42"
            )
        )
        order = self.run_async(self.db.get_order(oid))
        self.assertEqual(order["status"], "completed")
        self.assertEqual(order["receipt_number"], "42")

    def test_refund(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="1", store_name="T",
                pickup_method="", total_amount=1000,
                user_amount=600, subsidy_amount=400,
                hex_data="aa", products_json="[]",
            )
        )
        self.run_async(self.db.update_order_status(oid, OrderStatus.PROCESSING))
        self.run_async(self.db.update_order_status(oid, OrderStatus.COMPLETED))
        amount = self.run_async(self.db.refund_order(oid))
        self.assertEqual(amount, 600)
        balance = self.run_async(self.db.get_balance(1))
        self.assertEqual(balance, 2000)

    def test_double_refund_prevention(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="1", store_name="T",
                pickup_method="", total_amount=1000,
                user_amount=600, subsidy_amount=400,
                hex_data="aa", products_json="[]",
            )
        )
        self.run_async(self.db.update_order_status(oid, OrderStatus.PROCESSING))
        self.run_async(self.db.update_order_status(oid, OrderStatus.COMPLETED))
        self.run_async(self.db.refund_order(oid))
        with self.assertRaises(ValueError):
            self.run_async(self.db.refund_order(oid))

    def test_manual_review(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="1", store_name="T",
                pickup_method="", total_amount=1000,
                user_amount=600, subsidy_amount=400,
                hex_data="aa", products_json="[]",
            )
        )
        self.run_async(self.db.update_order_status(oid, OrderStatus.PROCESSING))
        self.run_async(self.db.update_order_status(oid, OrderStatus.MANUAL_REVIEW))
        self.run_async(self.db.update_order_status(oid, OrderStatus.COMPLETED))
        order = self.run_async(self.db.get_order(oid))
        self.assertEqual(order["status"], "completed")

    def test_invalid_transition(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="1", store_name="T",
                pickup_method="", total_amount=1000,
                user_amount=600, subsidy_amount=400,
                hex_data="aa", products_json="[]",
            )
        )
        with self.assertRaises(ValueError):
            self.run_async(
                self.db.update_order_status(oid, OrderStatus.COMPLETED)
            )

    def test_settings(self):
        val = self.run_async(self.db.get_setting("user_rate"))
        self.assertEqual(val, "60")
        self.run_async(self.db.set_setting("user_rate", "70"))
        val = self.run_async(self.db.get_setting("user_rate"))
        self.assertEqual(val, "70")

    def test_panel_persistence(self):
        self.run_async(self.db.save_panel(111, 222, 333))
        panel = self.run_async(self.db.get_panel(111))
        self.assertIsNotNone(panel)
        self.assertEqual(panel["message_id"], 333)
        self.run_async(self.db.save_panel(111, 222, 444))
        panel = self.run_async(self.db.get_panel(111))
        self.assertEqual(panel["message_id"], 444)

    def test_achievement_duplicate_prevention(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        oid = self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="1", store_name="T",
                pickup_method="", total_amount=1000,
                user_amount=600, subsidy_amount=400,
                hex_data="aa", products_json="[]",
            )
        )
        self.run_async(self.db.update_order_status(oid, OrderStatus.PROCESSING))
        self.run_async(self.db.update_order_status(oid, OrderStatus.COMPLETED))
        self.run_async(self.db.set_achievement_posted(oid))
        order = self.run_async(self.db.get_order(oid))
        self.assertEqual(order["achievement_posted"], 1)

    def test_balance_set(self):
        self.run_async(self.db.ensure_user(1))
        new = self.run_async(self.db.set_balance(1, 5000, "test"))
        self.assertEqual(new, 5000)
        self.run_async(self.db.set_balance(1, 0, "reset"))
        balance = self.run_async(self.db.get_balance(1))
        self.assertEqual(balance, 0)

    def test_deposit_reject(self):
        self.run_async(self.db.ensure_user(1))
        dep_id = self.run_async(self.db.create_deposit(1, 500))
        uid, amt = self.run_async(self.db.reject_deposit(dep_id, 99, "test"))
        self.assertEqual(uid, 1)
        self.assertEqual(amt, 500)
        balance = self.run_async(self.db.get_balance(1))
        self.assertEqual(balance, 0)

    def test_export_orders(self):
        self.run_async(self.db.add_balance(1, 2000, TransactionType.ADMIN_ADD))
        self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="1", store_name="T",
                pickup_method="", total_amount=1000,
                user_amount=600, subsidy_amount=400,
                hex_data="aa", products_json="[]",
            )
        )
        data = self.run_async(self.db.export_orders())
        self.assertEqual(len(data), 1)

    def test_search_orders(self):
        self.run_async(self.db.add_balance(1, 5000, TransactionType.ADMIN_ADD))
        oid = self.run_async(
            self.db.create_order_with_payment(
                user_id=1, store_id="1", store_name="T",
                pickup_method="", total_amount=1000,
                user_amount=600, subsidy_amount=400,
                hex_data="aa", products_json="[]",
            )
        )
        results = self.run_async(self.db.search_orders(user_id=1))
        self.assertEqual(len(results), 1)
        results = self.run_async(self.db.search_orders(status="pending"))
        self.assertEqual(len(results), 1)
        results = self.run_async(self.db.search_orders(status="completed"))
        self.assertEqual(len(results), 0)


class TestHexDecoder(unittest.TestCase):
    def test_malformed_hex(self):
        with self.assertRaises(ValueError):
            decode_hex_sync("zzzz")

    def test_empty_decode(self):
        result = decode_hex_sync("0a00")
        self.assertEqual(result.total_amount, 0)

    def test_short_hex(self):
        result = decode_hex_sync("0a023132")
        self.assertEqual(result.store_id, "12")


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
            data = image_gen.render_order_complete_sync(n)
            self.assertTrue(data.startswith(b"\x89PNG"))

    def test_render_empty_fallback(self):
        import image_gen
        data = image_gen.render_order_complete_sync("")
        self.assertTrue(data.startswith(b"\x89PNG"))


class TestRaceConditions(AsyncTestCase):
    def setUp(self):
        super().setUp()
        self.tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmpfile.close()
        self.db = Database(self.tmpfile.name)
        self.run_async(self.db.initialize())

    def tearDown(self):
        self.run_async(self.db.close())
        os.unlink(self.tmpfile.name)
        super().tearDown()

    def test_concurrent_balance_adds(self):
        self.run_async(self.db.ensure_user(1))

        async def add_many():
            tasks = [
                self.db.add_balance(1, 100, TransactionType.ADMIN_ADD)
                for _ in range(10)
            ]
            await asyncio.gather(*tasks)

        self.run_async(add_many())
        balance = self.run_async(self.db.get_balance(1))
        self.assertEqual(balance, 1000)

    def test_concurrent_deposits(self):
        self.run_async(self.db.ensure_user(1))
        dep1 = self.run_async(self.db.create_deposit(1, 500))
        dep2 = self.run_async(self.db.create_deposit(1, 300))

        async def approve_both():
            results = await asyncio.gather(
                self.db.approve_deposit(dep1, 99),
                self.db.approve_deposit(dep2, 99),
            )
            return results

        results = self.run_async(approve_both())
        balance = self.run_async(self.db.get_balance(1))
        self.assertEqual(balance, 800)


if __name__ == "__main__":
    unittest.main()
