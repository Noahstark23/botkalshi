import importlib.util
from datetime import UTC, datetime
from pathlib import Path
import sys
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location("risk_guard", BASE / "risk_guard.py")
risk_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(risk_guard)


class RiskGuardTests(unittest.TestCase):
    NOW = datetime(2026, 9, 16, 16, 30, tzinfo=UTC)

    def real_state(self, **overrides):
        state = {
            "mode": "REAL_SEPARATED",
            "capital_reconciled_usd": "200.00",
            "capital_reconciled_at": "2026-09-16T16:00:00Z",
            "source": "confirmed-fill-ledger",
            "open_risk_usd": "0.00",
            "today_new_risk_usd": "0.00",
            "week_net_pnl_usd": "0.00",
            "cumulative_net_pnl_usd": "0.00",
        }
        state.update(overrides)
        return state

    def test_missing_reconciliation_is_fail_closed(self):
        status = risk_guard.build_status(None, now=self.NOW)
        self.assertEqual(status["bank_state"], "PENDING_RECONCILIATION")
        self.assertFalse(status["real_entry_eligible"])
        self.assertEqual(status["new_risk_headroom_usd"], "0.00")
        self.assertFalse(status["execution_authorized"])

    def test_unit_reduces_when_capital_falls(self):
        status = risk_guard.build_status(
            self.real_state(capital_reconciled_usd="180.00"), now=self.NOW
        )
        self.assertEqual(status["unit_usd"], "1.80")
        self.assertEqual(status["max_open_risk_usd"], "5.40")
        self.assertEqual(status["max_new_daily_risk_usd"], "5.40")
        self.assertEqual(status["new_risk_headroom_usd"], "0.00")
        self.assertEqual(status["illustrative_headroom_usd"], "1.80")
        self.assertEqual(status["bank_state"], "DECLARED")

    def test_weekly_loss_pauses_new_real_risk(self):
        status = risk_guard.build_status(
            self.real_state(week_net_pnl_usd="-12.00"), now=self.NOW
        )
        self.assertFalse(status["real_entry_eligible"])
        self.assertEqual(status["new_risk_headroom_usd"], "0.00")
        self.assertTrue(any("pausa semanal" in reason for reason in status["reasons"]))

    def test_open_worst_case_cannot_cross_experiment_stop(self):
        status = risk_guard.build_status(
            self.real_state(
                cumulative_net_pnl_usd="-18.50",
                open_risk_usd="1.50",
            ),
            now=self.NOW,
        )
        self.assertFalse(status["real_entry_eligible"])
        self.assertEqual(status["new_risk_headroom_usd"], "0.00")
        self.assertTrue(any("límite experimental" in reason for reason in status["reasons"]))

    def test_simulation_never_enables_real_entry(self):
        state = self.real_state(mode="SIMULATION")
        status = risk_guard.build_status(state, now=self.NOW)
        self.assertFalse(status["real_entry_eligible"])
        self.assertEqual(status["bank_state"], "SIMULATION")

    def test_invalid_file_writes_fail_closed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "risk-input.json").write_text("not-json", encoding="utf-8")
            status = risk_guard.write_status(root)
            self.assertEqual(status["bank_state"], "INVALID_INPUT_FAIL_CLOSED")
            self.assertFalse(status["real_entry_eligible"])
            self.assertTrue((root / "risk-status.json").exists())

    def test_invalid_old_future_naive_and_extreme_dates_fail_closed(self):
        values = (
            "banana",
            "2026-09-15T16:29:59Z",
            "2026-09-16T16:30:01Z",
            "2026-09-16T16:00:00",
            "0001-01-01T00:00:00+01:00",
            "9999-12-31T23:59:59-01:00",
        )
        for value in values:
            with self.subTest(value=value), self.assertRaises(risk_guard.RiskInputError):
                risk_guard.build_status(
                    self.real_state(capital_reconciled_at=value),
                    now=self.NOW,
                )

    def test_unknown_source_and_authority_overrides_are_rejected(self):
        with self.assertRaises(risk_guard.RiskInputError):
            risk_guard.build_status(self.real_state(source="trust-me"), now=self.NOW)
        for field in (
            "evidence_verified",
            "execution_authorized",
            "order_capability_present",
            "real_entry_eligible",
        ):
            with self.subTest(field=field), self.assertRaises(risk_guard.RiskInputError):
                risk_guard.build_status(self.real_state(**{field: True}), now=self.NOW)

    def test_write_status_replaces_old_file_on_extreme_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "risk-status.json").write_text('{"sentinel": true}', encoding="utf-8")
            import json

            (root / "risk-input.json").write_text(
                json.dumps(self.real_state(capital_reconciled_at="0001-01-01T00:00:00+01:00")),
                encoding="utf-8",
            )
            status = risk_guard.write_status(root, now=self.NOW, cycle_id="cycle-1")
            self.assertEqual(status["bank_state"], "INVALID_INPUT_FAIL_CLOSED")
            self.assertNotIn("sentinel", (root / "risk-status.json").read_text())


if __name__ == "__main__":
    unittest.main()
