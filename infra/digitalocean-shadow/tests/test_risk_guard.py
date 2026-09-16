import importlib.util
from pathlib import Path
import tempfile
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("risk_guard", BASE / "risk_guard.py")
risk_guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(risk_guard)


class RiskGuardTests(unittest.TestCase):
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
        status = risk_guard.build_status(None)
        self.assertEqual(status["bank_state"], "PENDING_RECONCILIATION")
        self.assertFalse(status["real_entry_eligible"])
        self.assertEqual(status["new_risk_headroom_usd"], "0.00")
        self.assertFalse(status["execution_authorized"])

    def test_unit_reduces_when_capital_falls(self):
        status = risk_guard.build_status(self.real_state(capital_reconciled_usd="180.00"))
        self.assertEqual(status["unit_usd"], "1.80")
        self.assertEqual(status["max_open_risk_usd"], "5.40")
        self.assertEqual(status["max_new_daily_risk_usd"], "5.40")
        self.assertEqual(status["new_risk_headroom_usd"], "1.80")

    def test_weekly_loss_pauses_new_real_risk(self):
        status = risk_guard.build_status(self.real_state(week_net_pnl_usd="-12.00"))
        self.assertFalse(status["real_entry_eligible"])
        self.assertEqual(status["new_risk_headroom_usd"], "0.00")
        self.assertTrue(any("pausa semanal" in reason for reason in status["reasons"]))

    def test_open_worst_case_cannot_cross_experiment_stop(self):
        status = risk_guard.build_status(self.real_state(
            cumulative_net_pnl_usd="-18.50",
            open_risk_usd="1.50",
        ))
        self.assertFalse(status["real_entry_eligible"])
        self.assertEqual(status["new_risk_headroom_usd"], "0.00")
        self.assertTrue(any("límite experimental" in reason for reason in status["reasons"]))

    def test_simulation_never_enables_real_entry(self):
        state = self.real_state(mode="SIMULATION")
        status = risk_guard.build_status(state)
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


if __name__ == "__main__":
    unittest.main()
