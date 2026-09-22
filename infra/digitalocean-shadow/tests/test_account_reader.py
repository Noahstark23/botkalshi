from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("account_reader", BASE / "account_reader.py")
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)


class FakeClient:
    def __init__(self):
        self.position_calls = []
        self.fill_calls = []

    async def get_balance(self):
        return {"balance": 20000, "updated_ts": "2026-09-20T15:59:00Z"}

    async def get_positions(self, *, limit, cursor=None):
        self.position_calls.append((limit, cursor))
        if cursor is None:
            return {
                "market_positions": [{"ticker": "KX-SYN", "position": "2"}],
                "cursor": "p2",
            }
        return {"market_positions": [], "cursor": ""}

    async def get_fills(self, *, limit, cursor=None):
        self.fill_calls.append((limit, cursor))
        if cursor is None:
            return {
                "fills": [
                    {
                        "trade_id": "trade-1",
                        "ticker": "KX-SYN",
                        "side": "yes",
                        "action": "buy",
                        "count": "2",
                        "price": "0.40",
                    }
                ],
                "cursor": "f2",
            }
        return {"fills": [], "cursor": None}


class AccountReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_all_pages_but_grants_no_authority(self):
        client = FakeClient()
        result = await ar.collect_account_snapshot(
            client,
            now=datetime(2026, 9, 20, 16, 0, tzinfo=UTC),
        )
        self.assertEqual(result["balance_usd"], "200.00")
        self.assertEqual(result["positions_count"], 1)
        self.assertEqual(result["fills_count"], 1)
        self.assertEqual(client.position_calls, [(100, None), (100, "p2")])
        self.assertEqual(client.fill_calls, [(100, None), (100, "f2")])
        self.assertFalse(result["reconciled"])
        self.assertFalse(result["execution_authorized"])

    async def test_repeated_cursor_blocks_incomplete_snapshot(self):
        class Broken(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {"market_positions": [], "cursor": "same"}

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_non_object_row_rejects_whole_snapshot(self):
        class Broken(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {"market_positions": ["not-a-dict"], "cursor": ""}

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_invalid_ticker_rejects_whole_snapshot(self):
        class Broken(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {"market_positions": [{"ticker": "bad ticker!!"}], "cursor": ""}

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_invalid_trade_id_rejects_whole_snapshot(self):
        class Broken(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [{"trade_id": "bad id!!", "ticker": "KX-SYN"}],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_invalid_row_on_second_page_rejects_whole_snapshot(self):
        class Broken(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                if cursor is None:
                    return {
                        "market_positions": [{"ticker": "KX-SYN", "position": "2"}],
                        "cursor": "p2",
                    }
                return {"market_positions": ["not-a-dict"], "cursor": ""}

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_bool_number_field_rejects_whole_snapshot(self):
        class Broken(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {"trade_id": "trade-1", "ticker": "KX-SYN", "count": True}
                    ],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_nan_number_field_rejects_whole_snapshot(self):
        class Broken(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {"trade_id": "trade-1", "ticker": "KX-SYN", "count": "nan"}
                    ],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_infinity_number_field_rejects_whole_snapshot(self):
        class Broken(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {
                            "trade_id": "trade-1",
                            "ticker": "KX-SYN",
                            "count": "infinity",
                        }
                    ],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_float_number_field_rejects_whole_snapshot(self):
        class Broken(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {"trade_id": "trade-1", "ticker": "KX-SYN", "count": 2.5}
                    ],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_price_zero_is_preserved_not_replaced_by_yes_price(self):
        class Client(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {
                            "trade_id": "trade-1",
                            "ticker": "KX-SYN",
                            "price": 0,
                            "yes_price": "0.40",
                        }
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertEqual(result["fills"][0]["price"], "0")

    async def test_price_explicit_none_stays_unknown_no_fallback(self):
        class Client(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {
                            "trade_id": "trade-1",
                            "ticker": "KX-SYN",
                            "price": None,
                            "yes_price": "0.40",
                        }
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertIsNone(result["fills"][0]["price"])

    async def test_price_absent_falls_back_to_legacy_yes_price(self):
        class Client(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {
                            "trade_id": "trade-1",
                            "ticker": "KX-SYN",
                            "yes_price": "0.40",
                        }
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertEqual(result["fills"][0]["price"], "0.40")

    async def test_valid_int_and_decimal_numbers_are_preserved(self):
        class Client(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [
                        {"ticker": "KX-SYN", "position": 3, "market_exposure": "1.50"}
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        position = result["positions"][0]
        self.assertEqual(position["position"], "3")
        self.assertEqual(position["market_exposure_cents"], "1.50")

    async def test_duplicate_ticker_rejects_snapshot(self):
        class Broken(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [
                        {"ticker": "KX-SYN", "position": "1"},
                        {"ticker": "KX-SYN", "position": "2"},
                    ],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())


class SafeNumberTests(unittest.TestCase):
    def test_zero_negative_exponent_notation_rejected(self):
        with self.assertRaises(ar.AccountReadError):
            ar._safe_number("0e-9999999", field="count")

    def test_zero_positive_exponent_notation_rejected(self):
        with self.assertRaises(ar.AccountReadError):
            ar._safe_number("0e+9999999", field="count")

    def test_huge_int_magnitude_rejected(self):
        with self.assertRaises(ar.AccountReadError):
            ar._safe_number(10**5000, field="count")

    def test_invalid_string_rejected(self):
        with self.assertRaises(ar.AccountReadError):
            ar._safe_number("banana", field="count")

    def test_valid_zero_and_signed_decimals_preserved(self):
        self.assertEqual(ar._safe_number("0", field="count"), "0")
        self.assertEqual(ar._safe_number("-0", field="count"), "-0")
        self.assertEqual(ar._safe_number("+0", field="count"), "0")
        self.assertEqual(ar._safe_number("0.00", field="count"), "0.00")
        self.assertEqual(ar._safe_number("-1.5", field="count"), "-1.5")
        self.assertEqual(ar._safe_number("+3", field="count"), "3")
        self.assertIsNone(ar._safe_number(None, field="count"))
