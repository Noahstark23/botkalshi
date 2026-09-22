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

    async def test_official_payload_preserves_canonical_values_and_flags(self):
        class Client(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [
                        {
                            "ticker": "KX-SYN",
                            "position_fp": "2.00",
                            "market_exposure_dollars": "0.8000",
                            "realized_pnl_dollars": "0.0000",
                            "fees_paid_dollars": "0.0200",
                        }
                    ],
                    "cursor": "",
                }

            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {
                            "fill_id": "fill-1",
                            "trade_id": "trade-1",
                            "order_id": "order-1",
                            "ticker": "KX-SYN",
                            "side": "yes",
                            "action": "buy",
                            "count_fp": "2.00",
                            "yes_price_dollars": "0.4000",
                            "no_price_dollars": "0.6000",
                            "fee_cost": "0.0200",
                            "created_time": "2026-09-20T15:00:00Z",
                        }
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        position = result["positions"][0]
        self.assertEqual(position["position"], "2.00")
        self.assertEqual(position["position_fp"], "2.00")
        self.assertEqual(position["market_exposure_dollars"], "0.8000")
        self.assertEqual(position["market_exposure_cents"], "80.00")
        self.assertEqual(position["realized_pnl_dollars"], "0.0000")
        self.assertEqual(position["fees_paid_dollars"], "0.0200")
        self.assertEqual(position["fees_paid_cents"], "2.00")
        fill = result["fills"][0]
        self.assertEqual(fill["trade_id"], "trade-1")
        self.assertEqual(fill["fill_id"], "fill-1")
        self.assertEqual(fill["count"], "2.00")
        self.assertEqual(fill["count_fp"], "2.00")
        self.assertEqual(fill["yes_price_dollars"], "0.4000")
        self.assertEqual(fill["no_price_dollars"], "0.6000")
        self.assertEqual(fill["price_dollars"], "0.4000")
        self.assertEqual(fill["fee_cost_dollars"], "0.0200")
        self.assertFalse(result["reconciled"])
        self.assertFalse(result["real_entry_eligible"])
        self.assertFalse(result["execution_authorized"])
        self.assertFalse(result["order_capability_present"])

    async def test_unknown_side_never_selects_a_price(self):
        class Client(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {
                            "trade_id": "trade-1",
                            "ticker": "KX-SYN",
                            "yes_price_dollars": "0.40",
                        }
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertIsNone(result["fills"][0]["side"])
        self.assertIsNone(result["fills"][0]["price_dollars"])

    async def test_no_side_does_not_fall_back_to_yes_price(self):
        class Client(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {
                            "trade_id": "trade-1",
                            "ticker": "KX-SYN",
                            "side": "no",
                            "yes_price_dollars": "0.40",
                        }
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertIsNone(result["fills"][0]["no_price_dollars"])
        self.assertIsNone(result["fills"][0]["price_dollars"])

    async def test_price_boundaries_zero_and_one_preserved(self):
        class Client(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {
                            "trade_id": "trade-1",
                            "ticker": "KX-SYN",
                            "side": "yes",
                            "yes_price_dollars": "0",
                            "no_price_dollars": "1",
                        }
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertEqual(result["fills"][0]["yes_price_dollars"], "0")
        self.assertEqual(result["fills"][0]["no_price_dollars"], "1")
        self.assertEqual(result["fills"][0]["price_dollars"], "0")

    async def test_negative_position_is_preserved(self):
        class Client(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [{"ticker": "KX-SYN", "position_fp": "-3.00"}],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertEqual(result["positions"][0]["position"], "-3.00")

    async def test_fractional_count_fp_is_preserved(self):
        class Client(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {"trade_id": "trade-1", "ticker": "KX-SYN", "count_fp": "1.50"}
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertEqual(result["fills"][0]["count"], "1.50")
        self.assertEqual(result["fills"][0]["count_fp"], "1.50")

    async def test_subcent_exposure_dollars_preserved_exactly_in_cents(self):
        class Client(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [
                        {"ticker": "KX-SYN", "market_exposure_dollars": "0.80005"}
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertEqual(result["positions"][0]["market_exposure_cents"], "80.005")

    async def test_legacy_only_position_and_fill_fields_still_supported(self):
        class Client(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [
                        {"ticker": "KX-SYN", "position": "2", "fees_paid": "2"}
                    ],
                    "cursor": "",
                }

            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {"trade_id": "trade-1", "ticker": "KX-SYN", "count": "2"}
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        position = result["positions"][0]
        self.assertEqual(position["position"], "2")
        self.assertIsNone(position["position_fp"])
        self.assertEqual(position["fees_paid_cents"], "2")
        self.assertEqual(position["fees_paid_dollars"], "0.02")
        fill = result["fills"][0]
        self.assertEqual(fill["count"], "2")
        self.assertIsNone(fill["count_fp"])

    async def test_canonical_explicit_none_does_not_fall_back_to_legacy(self):
        class Client(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [
                        {"ticker": "KX-SYN", "position": "2", "position_fp": None}
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertIsNone(result["positions"][0]["position"])
        self.assertIsNone(result["positions"][0]["position_fp"])

    async def test_legacy_canonical_conflict_rejects_row(self):
        class Broken(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [
                        {"ticker": "KX-SYN", "position": "2", "position_fp": "3.00"}
                    ],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_cents_dollars_conflict_rejects_row(self):
        class Broken(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [
                        {
                            "ticker": "KX-SYN",
                            "market_exposure": "100",
                            "market_exposure_dollars": "2.00",
                        }
                    ],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_malformed_canonical_never_falls_back_to_legacy(self):
        class Broken(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [
                        {"ticker": "KX-SYN", "position": "2", "position_fp": "not-a-number"}
                    ],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())

    async def test_fee_cost_absent_stays_none(self):
        class Client(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [{"trade_id": "trade-1", "ticker": "KX-SYN"}],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertIsNone(result["fills"][0]["fee_cost_dollars"])

    async def test_fee_cost_zero_is_preserved(self):
        class Client(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {"trade_id": "trade-1", "ticker": "KX-SYN", "fee_cost": "0.0000"}
                    ],
                    "cursor": "",
                }

        result = await ar.collect_account_snapshot(Client())
        self.assertEqual(result["fills"][0]["fee_cost_dollars"], "0.0000")

    async def test_canonical_float_and_exponent_fields_invalidate(self):
        class BrokenFloat(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {
                    "market_positions": [{"ticker": "KX-SYN", "position_fp": 2.5}],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(BrokenFloat())

        class BrokenExponent(FakeClient):
            async def get_fills(self, *, limit, cursor=None):
                return {
                    "fills": [
                        {"trade_id": "trade-1", "ticker": "KX-SYN", "count_fp": "1e2"}
                    ],
                    "cursor": "",
                }

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(BrokenExponent())

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


class FixedPointSupervisorRegressionTests(unittest.TestCase):
    """Independent review regressions; fixtures never touch a real account."""

    def fill(self, **fields):
        return ar._sanitize_fill({"trade_id": "synthetic-trade", "ticker": "KX-SYN", **fields})

    def test_canonical_quantity_rejects_excess_effective_precision(self):
        for value in ("1.001", "0.0001"):
            with self.subTest(value=value), self.assertRaises(ar.AccountReadError):
                self.fill(count_fp=value)
            with self.subTest(position=value), self.assertRaises(ar.AccountReadError):
                ar._sanitize_position({"ticker": "KX-SYN", "position_fp": value})

    def test_canonical_prices_reject_excess_effective_precision(self):
        for key in ("yes_price_dollars", "no_price_dollars"):
            with self.subTest(key=key), self.assertRaises(ar.AccountReadError):
                self.fill(**{key: "0.40001"})

    def test_value_preserving_trailing_zeros_are_allowed(self):
        result = self.fill(side="yes", count_fp="1.5000", yes_price_dollars="0.40000")
        self.assertEqual(result["count"], "1.5000")
        self.assertEqual(result["price_dollars"], "0.40000")
        self.assertEqual(self.fill(count_fp="-0.00000")["count"], "-0.00000")

    def test_exact_conversion_under_low_ambient_decimal_precision(self):
        from decimal import localcontext
        with localcontext() as ctx:
            ctx.prec = 4
            result = ar._sanitize_position({
                "ticker": "KX-SYN", "market_exposure": "123456789012345.1234567890123456"
            })
            self.assertEqual(result["market_exposure_dollars"], "1234567890123.451234567890123456")
            subcent = ar._sanitize_position({"ticker": "KX-SYN", "market_exposure_dollars": "0.80005"})
            self.assertEqual(subcent["market_exposure_cents"], "80.005")
            self.assertEqual(self.fill(count_fp="1.5000")["count"], "1.5000")

    def test_null_canonical_money_and_price_never_fall_back(self):
        result = ar._sanitize_position({
            "ticker": "KX-SYN", "market_exposure": "80", "market_exposure_dollars": None
        })
        self.assertIsNone(result["market_exposure_cents"])
        self.assertIsNone(result["market_exposure_dollars"])
        price = self.fill(side="yes", yes_price="40", yes_price_dollars=None)
        self.assertIsNone(price["price_dollars"])
        self.assertEqual(price["price"], "40")  # The legacy field remains explicitly separate.

    def test_matching_legacy_and_canonical_units_are_accepted(self):
        result = self.fill(side="no", count="2", count_fp="2.00", no_price="60", no_price_dollars="0.6000")
        self.assertEqual(result["price_dollars"], "0.6000")
        self.assertEqual(result["count"], "2.00")

    def test_malformed_legacy_is_not_ignored_with_valid_canonical(self):
        for legacy in ({}, [], True, "garbage"):
            with self.subTest(legacy=legacy), self.assertRaises(ar.AccountReadError):
                self.fill(count=legacy, count_fp="2.00")
            with self.subTest(price=legacy), self.assertRaises(ar.AccountReadError):
                self.fill(yes_price=legacy, yes_price_dollars="0.4000")

    def test_malformed_enum_types_raise_account_read_error(self):
        for key in ("side", "action"):
            for value in ({}, [], 7, True):
                with self.subTest(key=key, value=value), self.assertRaises(ar.AccountReadError):
                    self.fill(**{key: value})

    def test_present_malformed_fill_id_is_not_discarded(self):
        for value in ("bad id!", [], {}, 123, True):
            with self.subTest(value=value), self.assertRaises(ar.AccountReadError):
                self.fill(fill_id=value)
        self.assertIsNone(self.fill(fill_id=None)["fill_id"])

    def test_no_side_selects_no_price_even_when_yes_is_available(self):
        result = self.fill(side="no", yes_price_dollars="0.4000", no_price_dollars="0.6000")
        self.assertEqual(result["price_dollars"], "0.6000")
        self.assertEqual(result["yes_price_dollars"], "0.4000")

    def test_price_and_count_range_checks_are_fail_closed(self):
        for fields in ({"count_fp": "-0.01"}, {"yes_price_dollars": "-0.0001"}, {"no_price_dollars": "1.0001"}):
            with self.subTest(fields=fields), self.assertRaises(ar.AccountReadError):
                self.fill(**fields)

    def test_missing_or_unknown_labels_do_not_invent_a_side(self):
        for value in (None, "unrecognized"):
            with self.subTest(value=value):
                result = self.fill(side=value, action=value, yes_price_dollars="0.4000")
                self.assertIsNone(result["side"])
                self.assertIsNone(result["action"])
                self.assertIsNone(result["price_dollars"])
