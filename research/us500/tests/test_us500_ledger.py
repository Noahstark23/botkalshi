"""Offline ledger invariants. Uses only temporary SQLite and stdlib unittest."""

import copy
import json
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

    def test_variation_margin_preserves_equity_and_close_never_pays_twice(self):
        self.mark()
        self.reserve()
        self.fill()
        before = self.mark("110")
        settled = self.send("variation_margin", settlement_price="105")
        self.assertEqual(settled["equity_usd"], before["equity_usd"])
        self.assertEqual(settled["mark_price"], "110")
        self.assertEqual(settled["last_mark_at_ms"], before["last_mark_at_ms"])
        self.assertEqual(settled["average_entry_price"], "105")
        self.assertEqual(settled["realized_pnl_usd"], "10")
        self.assertEqual(settled["variation_margin_usd"], "10")
        self.assertEqual(settled["unrealized_pnl_usd"], "10")
        self.assert_money_identity(settled)
        self.reserve("close", "sell", "2", "110", True)
        closed = self.fill("close", "2", "110")
        self.assertEqual(closed["realized_pnl_usd"], "20")
        self.assertEqual(closed["variation_margin_usd"], "10")
        self.assertEqual(Decimal(closed["cash_usd"]), Decimal("1019.58"))
        self.assertEqual(closed["unrealized_pnl_usd"], "0")
        self.assert_money_identity(closed)

    def test_short_negative_settlement_restart_and_idempotence(self):
        self.mark()
        self.reserve(side="sell")
        self.fill()
        before = self.mark("110")
        event = self.event("variation_margin", settlement_price="105")
        settled = self.ledger.apply(event)
        self.assertEqual(settled["equity_usd"], before["equity_usd"])
        self.assertEqual(settled["variation_margin_usd"], "-10")
        self.assertEqual(self.ledger.apply(event), settled)
        self.ledger.close()
        self.ledger = PaperLedger(self.path, config())
        self.assertEqual(self.ledger.apply(event), settled)
        with self.assertRaisesRegex(LedgerError, "EVENT_ID_CONFLICT"):
            self.ledger.apply({**event, "settlement_price": "106"})
        self.assertEqual(self.ledger.state(), settled)
        self.reserve("close", "buy", "2", "110", True)
        closed = self.fill("close", "2", "110")
        self.assertEqual(closed["realized_pnl_usd"], "-20")
        self.assertEqual(Decimal(closed["cash_usd"]), Decimal("979.58"))
        self.assert_money_identity(closed)

    def test_variation_margin_uses_decimals_without_execution_tick_rounding(self):
        self.ledger.close()
        self.ledger = PaperLedger(
            ":memory:", config(quantity_step="0.01", tick_size="0.0001", fee_bps="0")
        )
        self.mark("13.8028")
        self.reserve(quantity="0.03", limit_price="13.8028")
        self.fill(quantity="0.03", price="13.8028")
        before = self.mark("13.804321")
        settled = self.send("variation_margin", settlement_price="13.803333")
        self.assertEqual(
            Decimal(settled["variation_margin_usd"]), Decimal("0.00001599")
        )
        self.assertEqual(Decimal(settled["unrealized_pnl_usd"]), Decimal("0.00002964"))
        self.assertEqual(Decimal(settled["equity_usd"]), Decimal(before["equity_usd"]))
        self.assert_money_identity(settled)

    def test_variation_margin_partial_fill_keeps_unknown_reserve(self):
        self.mark()
        self.reserve(quantity="3")
        self.fill(quantity="1")
        before = self.send("unknown", order_id="o1")
        settled = self.send("variation_margin", settlement_price="105")
        self.assertEqual(settled["reserved_margin_usd"], before["reserved_margin_usd"])
        self.assertEqual(settled["position_quantity"], "1")
        self.assertEqual(settled["equity_usd"], before["equity_usd"])
        self.assertEqual(settled["orders"]["o1"]["status"], "UNKNOWN")
        state = self.fill(quantity="1")
        self.assertEqual(Decimal(state["average_entry_price"]), Decimal("102.5"))
        self.assertEqual(state["variation_margin_usd"], "5")
        self.assertEqual(state["orders"]["o1"]["status"], "UNKNOWN")
        self.assert_money_identity(state)

    def test_repeated_settlement_price_and_flat_settlement_cannot_create_cash(self):
        before = self.ledger.state()
        flat = self.send("variation_margin", settlement_price="100")
        self.assertEqual(flat["cash_usd"], before["cash_usd"])
        self.assertEqual(flat["average_entry_price"], "0")
        self.assertIsNone(flat["mark_price"])
        self.mark()
        self.reserve()
        self.fill()
        first = self.send("variation_margin", settlement_price="105")
        second = self.send("variation_margin", settlement_price="105")
        for key in (
            "cash_usd",
            "realized_pnl_usd",
            "variation_margin_usd",
            "equity_usd",
        ):
            self.assertEqual(first[key], second[key])
        self.assert_money_identity(second)

    def test_settlement_does_not_refresh_risk_mark_or_clear_pause(self):
        self.mark()
        self.reserve()
        self.fill()
        first = self.event("variation_margin", settlement_price="101")
        first["at_ms"] = T + 900
        self.ledger.apply(first)
        second = self.event("variation_margin", settlement_price="102")
        second["at_ms"] = T + 1200
        state = self.ledger.apply(second)
        self.assertIn("STALE_MARK", state["pause_reasons"])
        self.assertEqual(state["last_mark_at_ms"], T + 1)
        self.assertEqual(state["mark_price"], "100")
        self.assert_money_identity(state)

    def test_live_margin_rates_revalue_positions_and_reserves_but_freeze_config(self):
        self.mark()
        self.reserve(quantity="3")
        self.fill(quantity="1")
        old_cash = self.ledger.state()["cash_usd"]
        changed = self.send(
            "margin_rates", initial_margin_rate="0.2", maintenance_margin_rate="0.1"
        )
        self.assertEqual(Decimal(changed["initial_margin_usd"]), Decimal("20"))
        self.assertEqual(Decimal(changed["maintenance_margin_usd"]), Decimal("10"))
        self.assertEqual(Decimal(changed["reserved_margin_usd"]), Decimal("40.20"))
        self.assertEqual(changed["cash_usd"], old_cash)
        self.assertEqual(self.ledger.config["initial_margin_rate"], Decimal("0.1"))
        with self.assertRaises(TypeError):
            self.ledger.config["initial_margin_rate"] = Decimal("1")
        stored_config = json.loads(
            self.ledger._db.execute("SELECT config FROM ledger_state").fetchone()[0]
        )
        self.assertEqual(stored_config, config())
        self.assert_money_identity(changed)

    def test_severe_margin_change_pauses_without_liquidation_and_survives_restart(self):
        self.mark()
        self.reserve(quantity="40")
        self.fill(quantity="40")
        event = self.event(
            "margin_rates", initial_margin_rate="0.9", maintenance_margin_rate="0.8"
        )
        state = self.ledger.apply(event)
        self.assertIn("INITIAL_MARGIN_BREACH", state["pause_reasons"])
        self.assertIn("MAINTENANCE_MARGIN_BREACH", state["pause_reasons"])
        self.assertEqual(state["position_quantity"], "40")
        self.assertEqual(state["realized_pnl_usd"], "0")
        self.assertEqual(self.ledger.apply(event), state)
        self.ledger.close()
        self.ledger = PaperLedger(self.path, config())
        self.assertEqual(self.ledger.apply(event), state)
        lower = self.send(
            "margin_rates", initial_margin_rate="0.1", maintenance_margin_rate="0.05"
        )
        self.assertTrue(lower["paused"])
        self.assertEqual(lower["position_quantity"], "40")
        self.assertEqual(Decimal(lower["initial_margin_usd"]), Decimal("400"))

    def test_margin_rate_decrease_never_releases_unknown_reservation(self):
        self.mark()
        self.reserve()
        self.send("unknown", order_id="o1")
        higher = self.send(
            "margin_rates", initial_margin_rate="0.3", maintenance_margin_rate="0.2"
        )
        lower = self.send(
            "margin_rates", initial_margin_rate="0.05", maintenance_margin_rate="0.02"
        )
        self.assertEqual(higher["reserved_margin_usd"], lower["reserved_margin_usd"])
        self.assertEqual(Decimal(lower["reserved_margin_usd"]), Decimal("60.20"))
        self.assertEqual(lower["orders"]["o1"]["status"], "UNKNOWN")
        self.assertTrue(lower["paused"])
        self.assert_money_identity(lower)

    def test_invalid_new_event_fields_rates_and_prices_roll_back(self):
        self.mark()
        state = self.ledger.state()
        cases = [
            self.event("variation_margin", settlement_price="0"),
            self.event("variation_margin", settlement_price="NaN"),
            self.event("variation_margin", settlement_price=100),
            self.event(
                "margin_rates", initial_margin_rate="0.1", maintenance_margin_rate="0.2"
            ),
            self.event(
                "margin_rates",
                initial_margin_rate="1.01",
                maintenance_margin_rate="0.2",
            ),
            self.event(
                "margin_rates", initial_margin_rate="0.1", maintenance_margin_rate="0"
            ),
            self.event(
                "margin_rates", initial_margin_rate=True, maintenance_margin_rate="0.05"
            ),
        ]
        for event in cases:
            with self.subTest(event=event), self.assertRaises(LedgerError):
                self.ledger.apply(event)
            self.assertEqual(self.ledger.state(), state)

    def test_legacy_state_defaults_do_not_rewrite_history_on_read_or_replay(self):
        self.mark()
        event = self.event(
            "reserve",
            order_id="o1",
            side="buy",
            quantity="2",
            limit_price="100",
            reduce_only=False,
        )
        self.ledger.apply(event)
        original = self.ledger._db.execute("SELECT state FROM ledger_state").fetchone()[
            0
        ]
        legacy = json.loads(original)
        for key in (
            "variation_margin_usd",
            "last_settlement_price",
            "last_settlement_at_ms",
            "initial_margin_rate",
            "maintenance_margin_rate",
            "margin_rates_at_ms",
        ):
            legacy.pop(key)
        legacy.pop("position_cost_usd")
        legacy.pop("cost_basis_source")
        legacy["orders"]["o1"].pop("reserve_initial_margin_rate")
        raw_legacy = json.dumps(legacy)
        self.ledger._db.execute("UPDATE ledger_state SET state=?", (raw_legacy,))
        self.ledger.close()
        self.ledger = PaperLedger(self.path, config())
        read = self.ledger.state()
        replay = self.ledger.apply(event)
        self.assertEqual(read, replay)
        self.assertEqual(read["variation_margin_usd"], "0")
        self.assertEqual(read["initial_margin_rate"], "0.1")
        self.assertEqual(read["cost_basis_source"], "LEGACY_AVERAGE_DERIVATION")
        self.assertEqual(Decimal(read["reserved_margin_usd"]), Decimal("20.20"))
        self.assertEqual(
            self.ledger._db.execute("SELECT state FROM ledger_state").fetchone()[0],
            raw_legacy,
        )

    def test_recurring_average_cannot_leave_residual_on_full_close(self):
        for side, close_side, expected in (("buy", "sell", "1"), ("sell", "buy", "-1")):
            with self.subTest(side=side):
                self.ledger.close()
                self.ledger = PaperLedger(":memory:", config(fee_bps="0"))
                self.mark("2")
                self.reserve("a", side, "1", "1")
                self.fill("a", "1", "1")
                self.reserve("b", side, "2", "2")
                before = self.fill("b", "2", "2")
                self.assertEqual(before["position_cost_usd"], "5")
                self.assertEqual(before["unrealized_pnl_usd"], expected)
                self.reserve("close", close_side, "3", "2", True)
                closed = self.fill("close", "3", "2")
                self.assertEqual(closed["realized_pnl_usd"], expected)
                self.assertEqual(closed["position_cost_usd"], "0")
                self.assertEqual(closed["unrealized_pnl_usd"], "0")
                self.assert_money_identity(closed)

    def test_partial_close_then_variation_releases_exact_remaining_basis(self):
        self.ledger.close()
        self.ledger = PaperLedger(":memory:", config(fee_bps="0"))
        self.mark("2")
        self.reserve("a", "buy", "1", "1")
        self.fill("a", "1", "1")
        self.reserve("b", "buy", "2", "2")
        self.fill("b", "2", "2")
        self.reserve("partial", "sell", "1", "2", True)
        before = self.fill("partial", "1", "2")
        settled = self.send("variation_margin", settlement_price="2")
        self.assertEqual(Decimal(settled["realized_pnl_usd"]), Decimal("1"))
        self.assertEqual(Decimal(settled["equity_usd"]), Decimal(before["equity_usd"]))
        self.assertEqual(settled["position_cost_usd"], "4")
        self.reserve("close", "sell", "2", "2", True)
        closed = self.fill("close", "2", "2")
        self.assertEqual(Decimal(closed["realized_pnl_usd"]), Decimal("1"))
        self.assertEqual(Decimal(closed["cash_usd"]), Decimal("1001"))
        self.assert_money_identity(closed)


if __name__ == "__main__":
    unittest.main()
