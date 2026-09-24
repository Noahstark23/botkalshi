"""Tests for risk_policy — the ONE formula for unit, habitual size and aggregate caps.

Pinned from the CTO's decision (2026-09-22), "option A corrected":
  - effective unit = min(USD 2, 1% of reconciled simulated capital), floored to the cent
  - habitual risk  = half the effective unit, floored to the cent
  - max per operation and per correlated thesis = one effective unit
  - global open risk and new daily risk = three effective units (never above USD 6)

The aggregate caps SHRINK with capital. Fixing them at USD 6 when capital falls would
allow MORE exposure than the existing guard — a risk change, not a clarification.
No network, no I/O.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import risk_policy as policy  # noqa: E402

M = policy.MICRO  # microdollars per USD


def usd(text: str) -> int:
    whole, _, frac = text.partition(".")
    return int(whole) * M + int((frac + "000000")[:6])


class PolicyTableTests(unittest.TestCase):
    """The CTO's table, exactly."""

    def check(self, capital, unit, habitual, aggregate):
        limits = policy.policy_limits(usd(capital))
        self.assertEqual(limits["unit"], usd(unit), capital)
        self.assertEqual(limits["habitual"], usd(habitual), capital)
        self.assertEqual(limits["max_per_operation"], usd(unit), capital)
        self.assertEqual(limits["max_per_thesis"], usd(unit), capital)
        self.assertEqual(limits["max_open"], usd(aggregate), capital)
        self.assertEqual(limits["max_daily"], usd(aggregate), capital)

    def test_reference_capital(self):
        self.check("200.00", "2.00", "1.00", "6.00")

    def test_capital_below_reference_shrinks_every_cap(self):
        # 1% = 1.95; half = 0.975 → floored 0.97; 3 × 1.95 = 5.85 (NOT a fixed 6.00)
        self.check("195.00", "1.95", "0.97", "5.85")

    def test_capital_above_reference_never_raises_the_unit(self):
        self.check("220.00", "2.00", "1.00", "6.00")
        self.check("100000.00", "2.00", "1.00", "6.00")

    def test_one_percent_is_floored_to_the_cent_not_rounded(self):
        # 1% of 199.99 = 1.9999 → 1.99 (never 2.00); half = 0.995 → 0.99
        self.check("199.99", "1.99", "0.99", "5.97")

    def test_zero_capital_gives_zero_caps(self):
        self.check("0.00", "0.00", "0.00", "0.00")


class InvariantTests(unittest.TestCase):
    def test_caps_never_exceed_the_reference_limits_at_any_capital(self):
        for cents in range(0, 60_001, 7):  # 0.00 .. 600.00 in odd steps
            limits = policy.policy_limits(cents * 10_000)
            with self.subTest(capital_cents=cents):
                self.assertLessEqual(limits["unit"], usd("2.00"))
                self.assertLessEqual(limits["max_open"], usd("6.00"))
                self.assertLessEqual(limits["max_daily"], usd("6.00"))
                self.assertLessEqual(limits["habitual"], limits["unit"])
                self.assertEqual(limits["unit"] % 10_000, 0, "whole cents only")
                self.assertEqual(limits["habitual"] % 10_000, 0, "whole cents only")

    def test_caps_are_monotonic_in_capital(self):
        previous = policy.policy_limits(0)
        for cents in range(1, 30_001, 13):
            current = policy.policy_limits(cents * 10_000)
            for key in ("unit", "habitual", "max_open", "max_daily"):
                self.assertGreaterEqual(current[key], previous[key], (key, cents))
            previous = current

    def test_ceiling_below_reference_is_respected(self):
        limits = policy.policy_limits(usd("200.00"), unit_ceiling=usd("1.50"))
        self.assertEqual(limits["unit"], usd("1.50"))
        self.assertEqual(limits["habitual"], usd("0.75"))
        self.assertEqual(limits["max_open"], usd("4.50"))

    def test_ceiling_above_reference_is_rejected(self):
        with self.assertRaises(ValueError):
            policy.policy_limits(usd("200.00"), unit_ceiling=usd("2.01"))

    def test_negative_or_non_integer_capital_is_rejected(self):
        for bad in (-1, 1.5, "200", True, None):
            with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
                policy.policy_limits(bad)


class IsolationTests(unittest.TestCase):
    def test_policy_module_imports_nothing(self):
        """A money formula shared by two modules must not drag I/O, clock or clients."""
        import ast

        tree = ast.parse((BASE / "risk_policy.py").read_text())
        imports = [
            node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        modules = {getattr(n, "module", None) or n.names[0].name for n in imports}
        self.assertLessEqual(modules, {"__future__"}, modules)


class SingleFormulaTests(unittest.TestCase):
    """Both callers must report exactly what the shared formula says."""

    def test_risk_guard_agrees_with_the_policy(self):
        from datetime import UTC, datetime

        import risk_guard

        now = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
        for capital in ("200.00", "195.00", "220.00", "199.99"):
            with self.subTest(capital=capital):
                status = risk_guard.build_status(
                    {
                        "mode": "SIMULATION",
                        "capital_reconciled_usd": capital,
                        "capital_reconciled_at": now.isoformat(),
                        "source": "confirmed-fill-ledger",
                        "open_risk_usd": "0.00",
                        "today_new_risk_usd": "0.00",
                        "week_net_pnl_usd": "0.00",
                        "cumulative_net_pnl_usd": "0.00",
                    },
                    now=now,
                )
                limits = policy.policy_limits(usd(capital))
                to_usd = lambda m: f"{m // M}.{(m % M) // 10_000:02d}"  # noqa: E731
                self.assertEqual(status["unit_usd"], to_usd(limits["unit"]))
                self.assertEqual(status["habitual_risk_usd"], to_usd(limits["habitual"]))
                self.assertEqual(status["max_open_risk_usd"], to_usd(limits["max_open"]))
                self.assertEqual(status["max_new_daily_risk_usd"], to_usd(limits["max_daily"]))
                # The guard still grants nothing in SIMULATION: this refactor changes no authority.
                self.assertEqual(status["new_risk_headroom_usd"], "0.00")
                self.assertIs(status["real_entry_eligible"], False)

    def test_reference_unit_constants_agree(self):
        import risk_guard

        self.assertEqual(int(risk_guard.REFERENCE_UNIT * M), policy.REFERENCE_UNIT)


if __name__ == "__main__":
    unittest.main()
