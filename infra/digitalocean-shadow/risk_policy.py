"""The ONE formula for the simulated experiment's unit, habitual size and caps.

Why a separate module (CTO review, 2026-09-22): the same policy lived twice —
`risk_guard.build_status` (Decimal) and `bank_batch_review` (integer microdollars) —
and neither had the habitual size. Two copies of a money formula drift. Both now call
this pure function; it has no I/O, no clock and no authority of any kind.

Policy, "option A corrected" (decided by the owner on the CTO's recommendation):

    effective unit    = min(USD 2, 1% of reconciled simulated capital), floored to the cent
    habitual risk     = half the effective unit, floored to the cent
    max per operation = one effective unit
    max per thesis    = one effective unit (correlated positions share it)
    global open risk  = 3 effective units (never above USD 6)
    new daily risk    = 3 effective units (never above USD 6); closes do not give it back

The aggregate caps SHRINK with capital on purpose. Pinning them at USD 6 when capital
falls would allow MORE exposure than the existing guard. Weekly (USD 12) and
experiment (USD 20) pauses are absolute and live in risk_guard.

"Habitual" is a budget, not a contract count and not a loss target: a proposal is sized
FROM it, and if its minimum size does not fit, it is rejected — never bumped up to the
unit to make it fit.

Amounts are integer microdollars (1 USD = 1_000_000) so no rounding context exists.
"""

from __future__ import annotations

MICRO = 1_000_000
CENT = 10_000  # microdollars in one cent
REFERENCE_UNIT = 2 * MICRO  # USD 2.00
AGGREGATE_UNITS = 3
POLICY_VERSION = "SIM_UNIT_1PCT_CAP2_HABITUAL_HALF_AGG3_V1"


def _floor_cent(micros: int) -> int:
    return (micros // CENT) * CENT


def _plain_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{field} must be integer microdollars")
    if value < 0:
        raise ValueError(f"{field} must be non-negative")
    return value


def policy_limits(capital: int, *, unit_ceiling: int = REFERENCE_UNIT) -> dict[str, int]:
    """Caps for a given reconciled simulated capital, all in integer microdollars."""
    capital = _plain_int(capital, "capital")
    unit_ceiling = _plain_int(unit_ceiling, "unit_ceiling")
    if unit_ceiling > REFERENCE_UNIT:
        raise ValueError("unit_ceiling cannot exceed the USD 2 reference unit")
    unit = _floor_cent(min(unit_ceiling, capital // 100))
    aggregate = min(AGGREGATE_UNITS * unit, AGGREGATE_UNITS * REFERENCE_UNIT)
    return {
        "unit": unit,
        "habitual": _floor_cent(unit // 2),
        "max_per_operation": unit,
        "max_per_thesis": unit,
        "max_open": aggregate,
        "max_daily": aggregate,
    }
