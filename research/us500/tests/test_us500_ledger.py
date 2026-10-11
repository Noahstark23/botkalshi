"""Offline ledger invariants. Uses only temporary SQLite and stdlib unittest."""

import copy
import tempfile
import unittest
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from us500_ledger import LedgerError, PaperLedger


T = 1791700000000


def config(**changes):
    value = {
        "initial_cash_usd": "1000",
        "initial_margin_rate": "0.1",
        "maintenance_margin_rate": "0.05",
        "fee_bps": "10",
        "fee_quantum_usd": "0.01",
        "max_notional_usd": "5000",
        "max_loss_usd": "100",
        "max_gap_ms": 1000,
        "quantity_step": "1",
        "tick_size": "0.01",
    }
    value.update(changes)
    return value


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "paper.sqlite"
        self.ledger = PaperLedger(self.path, config())
        self.serial = 0

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def event(self, kind, **fields):
        self.serial += 1
        return {
            "event_id": f"e{self.serial}",
            "type": kind,
            "at_ms": T + self.serial,
            **fields,
        }

    def send(self, kind, **fields):
        return self.ledger.apply(self.event(kind, **fields))

    def mark(self, price="100"):
        return self.send("mark", price=price)

    def reserve(
        self,
        order_id="o1",
        side="buy",
        quantity="2",
        limit_price="100",
        reduce_only=False,
    ):
        return self.send(
            "reserve",
            order_id=order_id,
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            reduce_only=reduce_only,
        )

    def fill(self, order_id="o1", quantity="2", price="100"):
        return self.send("fill", order_id=order_id, quantity=quantity, price=price)

    def assert_money_identity(self, state):
        with localcontext() as ctx:
            ctx.prec = 80

            def d(key):
                return Decimal(state[key])

            self.assertEqual(
                d("cash_usd"),
                d("initial_cash_usd")
                + d("realized_pnl_usd")
                - d("fees_usd")
                + d("funding_usd")
                - d("costs_usd"),
            )
            self.assertEqual(d("equity_usd"), d("cash_usd") + d("unrealized_pnl_usd"))
            self.assertEqual(
                d("available_margin_usd"),
                d("equity_usd") - d("initial_margin_usd") - d("reserved_margin_usd"),
            )

    def test_initial_state_and_mark_required(self):
        initial = self.ledger.state()
        self.assertEqual(initial["cash_usd"], "1000")
        self.assertIsNone(initial["mark_price"])
        state = self.reserve()
        self.assertEqual(state["last_event"]["reason"], "MARK_REQUIRED")
        self.assertEqual(state["orders"], {})

    def test_long_partial_close_fees_funding_cost_and_restart(self):
        self.mark()
        self.reserve()
        state = self.fill(quantity="1")
        self.assertEqual(state["position_quantity"], "1")
        self.assertEqual(Decimal(state["reserved_margin_usd"]), Decimal("10.10"))
        self.fill(quantity="1")
        self.mark("110")
        self.reserve("close", "sell", "1", "110", True)
        self.fill("close", "1", "110")
        self.send("funding", cashflow_usd="-0.25")
        state = self.send("cost", amount_usd="0.10")
        self.assertEqual(Decimal(state["realized_pnl_usd"]), Decimal("10"))
        self.assertEqual(Decimal(state["unrealized_pnl_usd"]), Decimal("10"))
        self.assertEqual(Decimal(state["cash_usd"]), Decimal("1009.34"))
        self.assert_money_identity(state)
        self.ledger.close()
        self.ledger = PaperLedger(self.path, config())
        self.assertEqual(self.ledger.state(), state)

    def test_short_close_and_flip_cost_basis(self):
        self.mark()
        self.reserve(side="sell")
        self.fill()
        self.assertEqual(self.ledger.state()["position_quantity"], "-2")
        self.mark("90")
        self.reserve("flip", "buy", "3", "90")
        state = self.fill("flip", "3", "90")
        self.assertEqual(state["position_quantity"], "1")
        self.assertEqual(state["average_entry_price"], "90")
        self.assertEqual(state["realized_pnl_usd"], "20")
        self.assert_money_identity(state)

    def test_weighted_entry_and_flat_reset(self):
        self.mark()
        self.reserve(quantity="1")
        self.fill(quantity="1")
        self.mark("120")
        self.reserve("o2", "buy", "1", "120")
        state = self.fill("o2", "1", "120")
        self.assertEqual(state["average_entry_price"], "110")
        self.reserve("close", "sell", "2", "120", True)
        state = self.fill("close", "2", "120")
        self.assertEqual(state["position_quantity"], "0")
        self.assertEqual(state["average_entry_price"], "0")
        self.assert_money_identity(state)

    def test_unknown_preserves_remaining_reserve_and_can_reconcile(self):
        self.mark()
        self.reserve()
        self.fill(quantity="1")
        before = self.ledger.state()["reserved_margin_usd"]
        state = self.send("unknown", order_id="o1")
        self.assertEqual(state["reserved_margin_usd"], before)
        self.assertTrue(state["paused"])
        self.ledger.close()
        self.ledger = PaperLedger(self.path, config())
        self.assertEqual(self.ledger.state()["reserved_margin_usd"], before)
        state = self.fill(quantity="1")
        self.assertEqual(state["orders"]["o1"]["status"], "FILLED")
        self.assertEqual(state["reserved_margin_usd"], "0")
        self.assertTrue(state["paused"])

    def test_cancel_releases_only_unfilled_reserve(self):
        self.mark()
        self.reserve()
        self.fill(quantity="1")
        state = self.send("cancel", order_id="o1")
        self.assertEqual(state["reserved_margin_usd"], "0")
        self.assertEqual(state["position_quantity"], "1")
        self.assertEqual(state["initial_margin_usd"], "10.0")
        with self.assertRaises(LedgerError):
            self.fill(quantity="1")

    def test_event_idempotence_conflict_and_restart(self):
        event = self.event("mark", price="100")
        state = self.ledger.apply(event)
        self.assertEqual(self.ledger.apply(event), state)
        self.send("cost", amount_usd="0.5")
        latest = self.ledger.state()
        self.assertEqual(self.ledger.apply(event), latest)
        with self.assertRaisesRegex(LedgerError, "EVENT_ID_CONFLICT"):
            self.ledger.apply({**event, "price": "101"})
        self.assertEqual(self.ledger.state(), latest)
        self.ledger.close()
        self.ledger = PaperLedger(self.path, config())
        self.assertEqual(self.ledger.apply(event), latest)

    def test_config_conflict_does_not_change_state(self):
        state = self.mark()
        with self.assertRaisesRegex(LedgerError, "CONFIG_CONFLICT"):
            PaperLedger(self.path, config(initial_cash_usd="2000"))
        self.assertEqual(self.ledger.state(), state)

    def test_order_id_conflict_is_atomic(self):
        self.mark()
        state = self.reserve()
        with self.assertRaisesRegex(LedgerError, "ORDER_ID_CONFLICT"):
            self.reserve(quantity="3")
        self.assertEqual(self.ledger.state(), state)

    def test_gap_pause_durable_and_reserve_rejected(self):
        self.mark()
        event = self.event(
            "reserve",
            order_id="o1",
            side="buy",
            quantity="1",
            limit_price="100",
            reduce_only=False,
        )
        event["at_ms"] += 2000
        state = self.ledger.apply(event)
        self.assertIn("OBSERVATION_GAP", state["pause_reasons"])
        self.assertIn("STALE_MARK", state["pause_reasons"])
        self.assertEqual(state["last_event"]["status"], "REJECTED")
        self.assertEqual(state["orders"], {})
        self.ledger.close()
        self.ledger = PaperLedger(self.path, config())
        self.assertTrue(self.ledger.state()["paused"])

    def test_cost_events_do_not_refresh_mark(self):
        self.mark()
        for delta in (500, 1000, 1500):
            event = self.event("cost", amount_usd="0")
            event["at_ms"] = T + 1 + delta
            state = self.ledger.apply(event)
        self.assertIn("STALE_MARK", state["pause_reasons"])

    def test_margin_breach_never_fabricates_liquidation_and_allows_reduction(self):
        self.mark()
        self.reserve(quantity="40")
        self.fill(quantity="40")
        state = self.mark("75")
        self.assertTrue(state["paused"])
        self.assertIn("MAINTENANCE_MARGIN_BREACH", state["pause_reasons"])
        self.assertEqual(state["position_quantity"], "40")
        self.assertEqual(state["realized_pnl_usd"], "0")
        state = self.reserve("close", "sell", "1", "75", True)
        self.assertEqual(state["last_event"]["status"], "APPLIED")
        state = self.fill("close", "1", "75")
        self.assertEqual(state["position_quantity"], "39")
        self.assert_money_identity(state)

    def test_risk_limits_and_reduce_only_overcommit(self):
        self.mark()
        state = self.reserve(quantity="51")
        self.assertEqual(state["last_event"]["reason"], "MAX_NOTIONAL_LIMIT")
        self.reserve("o2", quantity="2")
        self.fill("o2")
        self.reserve("r1", "sell", "2", "100", True)
        state = self.reserve("r2", "sell", "1", "100", True)
        self.assertEqual(state["last_event"]["reason"], "REDUCE_ONLY_EXCEEDS_POSITION")

    def test_minimum_partial_fee_reserves_remain_conservative(self):
        self.ledger.close()
        self.ledger = PaperLedger(":memory:", config(quantity_step="0.01"))
        self.mark()
        state = self.reserve(quantity="0.03")
        self.assertEqual(Decimal(state["reserved_margin_usd"]), Decimal("0.33"))
        for _ in range(3):
            state = self.fill(quantity="0.01")
            self.assert_money_identity(state)
        self.assertEqual(Decimal(state["fees_usd"]), Decimal("0.03"))
        self.assertEqual(state["reserved_margin_usd"], "0")

    def test_malformed_inputs_never_mutate(self):
        self.mark()
        self.reserve()
        good = self.event("fill", order_id="o1", quantity="1", price="100")
        cases = [
            None,
            [],
            {**good, "extra": 1},
            {**good, "quantity": True},
            {**good, "quantity": "NaN"},
            {**good, "quantity": "0"},
            {**good, "quantity": "0.001"},
            {**good, "price": "100.001"},
            {**good, "at_ms": True},
            {**good, "at_ms": T},
            {**good, "quantity": "3"},
            {**good, "price": "101"},
            {**good, "order_id": "missing"},
            {**good, "event_id": "bad\n"},
        ]
        before = self.ledger.state()
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(LedgerError):
                    self.ledger.apply(case)
                self.assertEqual(self.ledger.state(), before)

    def test_invalid_config_rejected(self):
        for change in (
            {"quantity_step": "0.001"},
            {"quantity_step": "0.015"},
            {"maintenance_margin_rate": "0.2"},
            {"fee_bps": "10001"},
            {"max_gap_ms": True},
            {"initial_cash_usd": "NaN"},
        ):
            with self.subTest(change=change), self.assertRaises(LedgerError):
                PaperLedger(":memory:", config(**change))

    def test_input_objects_not_mutated(self):
        event = self.event("mark", price="100")
        before = copy.deepcopy(event)
        self.ledger.apply(event)
        self.assertEqual(event, before)

    def test_explicit_pause_and_cost_loss_pause(self):
        state = self.send("cost", amount_usd="100")
        self.assertIn("MAX_LOSS_BREACH", state["pause_reasons"])
        state = self.send("pause", reason="manual_check")
        self.assertIn("MANUAL:manual_check", state["pause_reasons"])
        self.assertEqual(state["position_quantity"], "0")

    def test_mark_does_not_have_to_be_execution_tick(self):
        state = self.mark("100.001234")
        self.assertEqual(state["mark_price"], "100.001234")

    def test_nonzero_funding_requires_position(self):
        before = self.ledger.state()
        with self.assertRaisesRegex(LedgerError, "FUNDING_WITHOUT_POSITION"):
            self.send("funding", cashflow_usd="1")
        self.assertEqual(self.ledger.state(), before)
        state = self.send("funding", cashflow_usd="0")
        self.assertEqual(state["cash_usd"], "1000")

    def test_event_capacity_preserves_reserves_and_allows_idempotent_replay(self):
        with patch("us500_ledger.MAX_EVENTS", 2):
            first = self.event("mark", price="100")
            self.ledger.apply(first)
            last = self.event(
                "reserve",
                order_id="o1",
                side="buy",
                quantity="2",
                limit_price="100",
                reduce_only=False,
            )
            before = self.ledger.apply(last)
            self.assertEqual(before["event_count"], 2)
            self.assertGreater(Decimal(before["reserved_margin_usd"]), 0)
            with self.assertRaisesRegex(LedgerError, "EVENT_CAPACITY_EXHAUSTED"):
                self.fill(quantity="1")
            self.assertEqual(self.ledger.state(), before)
            self.assertEqual(self.ledger.apply(first), before)
            self.assertEqual(self.ledger.apply(last), before)
            self.ledger.close()
            self.ledger = PaperLedger(self.path, config())
            self.assertEqual(self.ledger.apply(first), before)
            with self.assertRaisesRegex(LedgerError, "EVENT_CAPACITY_EXHAUSTED"):
                self.send("cancel", order_id="o1")
            self.assertEqual(self.ledger.state(), before)

    def test_order_capacity_counts_terminal_orders_and_preserves_history(self):
        with patch("us500_ledger.MAX_ORDERS", 2):
            self.mark()
            self.reserve("o1")
            last = self.event(
                "reserve",
                order_id="o2",
                side="buy",
                quantity="2",
                limit_price="100",
                reduce_only=False,
            )
            before = self.ledger.apply(last)
            with self.assertRaisesRegex(LedgerError, "ORDER_CAPACITY_EXHAUSTED"):
                self.reserve("o3")
            self.assertEqual(self.ledger.state(), before)
            self.assertEqual(self.ledger.apply(last), before)
            after_cancel = self.send("cancel", order_id="o1")
            self.assertEqual(len(after_cancel["orders"]), 2)
            self.assertEqual(after_cancel["orders"]["o1"]["status"], "CANCELED")
            self.assertGreater(Decimal(after_cancel["reserved_margin_usd"]), 0)
            with self.assertRaisesRegex(LedgerError, "ORDER_CAPACITY_EXHAUSTED"):
                self.reserve("o3")
            self.assertEqual(self.ledger.state(), after_cancel)
            self.assertEqual(self.ledger.apply(last), after_cancel)


if __name__ == "__main__":
    unittest.main()
