"""Composes `m5_shadow_fill_review.review_m5_fill` with
`simulation_bank.reserve_with_evidence`. Nothing else.

`apply_m5_fill` reviews a caller-DECLARED M5 shadow fill and, only when the
review accepts it, reserves the fill's exact worst-case loss against the
FICTIONAL `SIMULATION_ONLY` bank, with the review's own evidence persisted in
the same transaction as the reservation.

This module never reads `src.storage.models.MMShadowFill` or any other
durable row, never opens a database connection of its own (all persistence is
inside `simulation_bank`), and never calls a Kalshi client, `RiskManager`, an
executor, or the network. Because it never reads the source fill back from
its table of record, it cannot and does not claim ledger verification: every
result carries `evidence_state = "DECLARED_FILL_NOT_LEDGER_VERIFIED"`, the
same label `review_m5_fill` uses on its own. Composing review + reservation is
NOT the same as confirming the declaration against the durable MMShadowFill
row — calling this "M5 fully connected to the simulation bank" would overstate
what it does; it is declared-input reservation, not ledger-verified
reservation.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import simulation_bank
from m5_shadow_fill_review import ORIGIN, review_m5_fill

SCHEMA_VERSION_OUTPUT = "botkalshi-m5-bank-bridge-v1"


def _base(review: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION_OUTPUT,
        "review": review,
        "reservation": None,
        "reservation_error": None,
        "mode": "SIMULATION_ONLY",
        "capital_source": "FICTIONAL_TEST_CAPITAL",
        "evidence_state": "DECLARED_FILL_NOT_LEDGER_VERIFIED",
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
    }


def apply_m5_fill(db_path: str | Path, raw: object, *, now: datetime | None = None) -> dict[str, Any]:
    """Review a declared M5 shadow fill and, if ACCEPTED, reserve it.

    Never calls the bank on a BLOCKED review — `result["reservation"]` stays
    `None` and `result["review"]` carries the `reason_codes` explaining why.
    `cycle_id`/`idempotency_key` are never accepted here or from the outer
    caller; they come entirely from `review_m5_fill`, which derives them from
    the fill's own `experiment_id` AND `id` together (see that module's
    docstring for why neither alone is enough and why this must not be
    confused with a collector `packet_id`). A bank-side failure (not
    initialized, conflicting replay, insufficient capital) is caught and
    reported in `reservation_error` rather than raised, so the caller always
    gets back the review that explains the state — no partial write is
    possible either way, since `reserve_with_evidence` rolls back on any
    exception before this function ever sees one.
    """
    review = review_m5_fill(raw, now=now)
    result = _base(review)
    if review["status"] != "ACCEPTED":
        return result
    try:
        result["reservation"] = simulation_bank.reserve_with_evidence(
            db_path,
            idempotency_key=review["idempotency_key"],
            origin=ORIGIN,
            cycle_id=review["cycle_id"],
            amount_usd=review["max_loss_usd"],
            evidence=review["evidence"],
        )
    except simulation_bank.SimulationBankError as exc:
        result["reservation_error"] = f"{type(exc).__name__}: {exc}"
    return result
