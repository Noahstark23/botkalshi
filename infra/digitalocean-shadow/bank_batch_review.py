"""Offline, read-only review of ONE shared-bank proposal batch.

This is NOT a ledger, allocator, account reconciler, or trading gate. Results are
hypothetical and cannot reserve funds. Concurrent invocations/restarts do not
share state. Inputs are declarations, even when labelled REAL_SEPARATED.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from zoneinfo import ZoneInfo

from risk_guard import EXPERIMENT_STOP, REFERENCE_UNIT, WEEKLY_STOP, build_status

INPUT_SCHEMA = "botkalshi-bank-batch-input-v1"
OUTPUT_SCHEMA = "botkalshi-bank-batch-review-v1"
MICRO = 1_000_000
CENT_MICRO = 10_000
MAX_BYTES = 64_000
MAX_ROWS = 100
MAX_AGE = timedelta(seconds=180)
LOCAL_ZONE = ZoneInfo("America/Los_Angeles")
ORIGINS = frozenset({"M1", "M5", "RADAR"})
AUTHORITY_FIELDS = {
    "evidence_verified", "execution_authorized", "order_capability_present",
    "real_entry_eligible",
}
RISK_FIELDS = {
    "mode", "source", "capital_reconciled_at", "capital_reconciled_usd",
    "open_risk_usd", "today_new_risk_usd", "week_net_pnl_usd",
    "cumulative_net_pnl_usd",
}
ROOT_FIELDS = {
    "schema_version", "snapshot_id", "risk", "risk_day", "week_start",
    "cash_available_usd", "unit_ceiling_usd", "paused",
    "thesis_open_risk_usd", "proposals",
}
PROPOSAL_FIELDS = {
    "proposal_id", "origin", "thesis_id", "snapshot_id", "observed_at",
    "risk_usd", "includes_all_costs",
}


class BatchInputError(ValueError):
    """A declared input does not satisfy the closed review contract."""


def _object(value: Any, keys: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise BatchInputError(f"{field}: missing or unexpected fields")
    return value


def _id(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value) is None:
        raise BatchInputError("invalid identifier")
    return value


def _amount(value: Any, *, signed: bool = False) -> int:
    # Fixed-point strings only. Reject floats, bools, exponents and excess scale;
    # integer microdollars preserve sub-cent values without context rounding.
    if not isinstance(value, str) or re.fullmatch(
        r"-?(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?", value
    ) is None:
        raise BatchInputError("money must be a bounded fixed-point USD string")
    negative = value.startswith("-")
    if negative and not signed:
        raise BatchInputError("negative risk or cash")
    whole, _, fraction = value.lstrip("-").partition(".")
    result = int(whole) * MICRO + int((fraction + "000000")[:6])
    return -result if negative else result


def _usd(value: int) -> str:
    sign = "-" if value < 0 else ""
    whole, fraction = divmod(abs(value), MICRO)
    return f"{sign}{whole}.{fraction:06d}"


def _moment(value: Any, now: datetime) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise BatchInputError("missing timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        parsed = parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise BatchInputError("invalid timestamp") from None
    if not timedelta(0) <= now - parsed <= MAX_AGE:
        raise BatchInputError("stale or future timestamp")
    return parsed


def _base() -> dict[str, Any]:
    return {
        "schema_version": OUTPUT_SCHEMA,
        "status": "BLOCKED",
        "evidence_state": "DECLARATIVE_NOT_VERIFIED",
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
        "reservations_persisted": False,
        "new_risk_headroom_usd": "0.000000",
        "hypothetical_total_risk_usd": "0.000000",
        "decisions": [],
    }


def review_batch(raw: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Review proposals in supplied order; never return financial authority.

    All input is validated before any hypothetical capacity is assigned. The
    ordering is a caller choice, NOT an economic ranking. A whole request fits
    or is rejected; this module does not choose prices or fractional quantities.
    """
    base = _base()
    try:
        clock = now if now is not None else datetime.now(UTC)
        if not isinstance(clock, datetime) or clock.tzinfo is None or clock.utcoffset() is None:
            raise BatchInputError("clock must include timezone")
        clock = clock.astimezone(UTC)
        data = _object(raw, ROOT_FIELDS, "input")
        if data["schema_version"] != INPUT_SCHEMA:
            raise BatchInputError("unsupported schema")
        snapshot = _id(data["snapshot_id"])
        local_day = clock.astimezone(LOCAL_ZONE).date()
        if data["risk_day"] != local_day.isoformat() or data["week_start"] != (
            local_day - timedelta(days=local_day.weekday())
        ).isoformat():
            raise BatchInputError("daily or weekly window does not match local clock")
        if type(data["paused"]) is not bool:
            raise BatchInputError("paused must be an explicit boolean")

        risk = data["risk"]
        if not isinstance(risk, dict) or not RISK_FIELDS <= set(risk) <= RISK_FIELDS | AUTHORITY_FIELDS:
            raise BatchInputError("risk: missing or unexpected fields")
        if risk["mode"] not in ("SIMULATION", "REAL_SEPARATED"):
            raise BatchInputError("unsupported risk mode")
        if risk["source"] != "confirmed-fill-ledger":
            raise BatchInputError("unsupported declarative source")
        for field in AUTHORITY_FIELDS & set(risk):
            if risk[field] is not False:
                raise BatchInputError("input cannot grant authority")
        stamp = _moment(risk["capital_reconciled_at"], clock)
        capital = _amount(risk["capital_reconciled_usd"])
        opened = _amount(risk["open_risk_usd"])
        daily = _amount(risk["today_new_risk_usd"])
        weekly = _amount(risk["week_net_pnl_usd"], signed=True)
        cumulative = _amount(risk["cumulative_net_pnl_usd"], signed=True)
        cash = _amount(data["cash_available_usd"])
        ceiling = _amount(data["unit_ceiling_usd"])
        if cash > capital or not 0 < ceiling <= _amount(format(REFERENCE_UNIT, "f")):
            raise BatchInputError("cash or unit ceiling inconsistent")
        if ceiling % CENT_MICRO:
            raise BatchInputError("unit ceiling must be in whole cents")
        # Reuse the existing guard without changing its output or live consumers.
        existing = build_status(risk, now=clock)
        if any(existing[field] is not False for field in (
            "execution_authorized", "order_capability_present", "real_entry_eligible"
        )):
            raise BatchInputError("existing guard unexpectedly grants authority")

        by_thesis = data["thesis_open_risk_usd"]
        if not isinstance(by_thesis, dict) or len(by_thesis) > MAX_ROWS:
            raise BatchInputError("invalid thesis breakdown")
        thesis = {_id(key): _amount(value) for key, value in by_thesis.items()}
        if sum(thesis.values()) != opened:
            raise BatchInputError("thesis breakdown does not reconcile to global open risk")
        rows = data["proposals"]
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise BatchInputError("invalid batch size")
        unique: dict[str, tuple[str, str, int, str]] = {}
        duplicates = 0
        for raw_proposal in rows:
            proposal = _object(raw_proposal, PROPOSAL_FIELDS, "proposal")
            key = _id(proposal["proposal_id"])
            origin = proposal["origin"]
            if not isinstance(origin, str) or origin not in ORIGINS:
                raise BatchInputError("unknown proposal origin")
            thesis_id = _id(proposal["thesis_id"])
            if proposal["snapshot_id"] != snapshot:
                raise BatchInputError("mixed snapshots")
            observed = _moment(proposal["observed_at"], clock)
            requested = _amount(proposal["risk_usd"])
            if requested <= 0 or proposal["includes_all_costs"] is not True:
                raise BatchInputError("positive worst-case risk inclusive of costs required")
            normalized = (origin, thesis_id, requested, observed.isoformat())
            if key in unique:
                if unique[key] != normalized:
                    raise BatchInputError("conflicting duplicate proposal identifier")
                duplicates += 1
            else:
                unique[key] = normalized

        unit = min(ceiling, (capital // 100 // CENT_MICRO) * CENT_MICRO)
        open_cap = daily_cap = 3 * unit
        stop = _amount(format(EXPERIMENT_STOP, "f"))
        paused = data["paused"] or weekly <= -_amount(format(WEEKLY_STOP, "f")) or cumulative <= -stop
        total = 0
        decisions = []
        totals_by_origin = dict.fromkeys(sorted(ORIGINS), 0)
        for key, (origin, thesis_id, requested, _) in unique.items():
            limits = {
                "THESIS_CAP": max(0, unit - thesis.get(thesis_id, 0)),
                "OPEN_CAP": max(0, open_cap - opened - total),
                "DAILY_CAP": max(0, daily_cap - daily - total),
                "CASH_CAP": max(0, cash - total),
                "EXPERIMENT_CAP": max(0, stop + cumulative - opened - total),
            }
            reasons = [name for name, remaining in limits.items() if requested > remaining]
            if paused:
                reasons.append("PAUSED")
            fits = not reasons
            assigned = requested if fits else 0
            total += assigned
            thesis[thesis_id] = thesis.get(thesis_id, 0) + assigned
            totals_by_origin[origin] += assigned
            decisions.append({
                "proposal_id": key, "origin": origin, "thesis_id": thesis_id,
                "result": "FITS_HYPOTHETICALLY" if fits else "DOES_NOT_FIT",
                "hypothetical_risk_usd": _usd(assigned), "reasons": reasons,
            })
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return {
            **base,
            "status": "REVIEW_ONLY",
            "input_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "snapshot_id": snapshot,
            "generated_at": clock.isoformat(),
            "declared_snapshot_at": stamp.isoformat(),
            "input_mode": risk["mode"],
            "unit_usd": _usd(unit),
            "max_open_risk_usd": _usd(open_cap),
            "policy_paused": paused,
            "duplicates_ignored": duplicates,
            "hypothetical_total_risk_usd": _usd(total),
            "hypothetical_by_origin_usd": {key: _usd(value) for key, value in totals_by_origin.items()},
            "decisions": decisions,
        }
    except (BatchInputError, ValueError, TypeError, ArithmeticError) as exc:
        # Do not copy raw input/paths/credentials into errors. Known contract
        # failures are fixed messages; unexpected numerical failures are generic.
        return {**base, "reasons": [str(exc) if isinstance(exc, BatchInputError) else "invalid input"]}


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BatchInputError("duplicate JSON key")
        result[key] = value
    return result


def read_input(path: Path) -> Any:
    """Bounded regular-file read. No writes, credentials, SQL, or network."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_BYTES:
            raise BatchInputError("input must be a bounded regular file")
        content = handle.read(MAX_BYTES + 1)
        if len(content) > MAX_BYTES:
            raise BatchInputError("input too large")
    return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_keys)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = review_batch(read_input(args.input))
    except (OSError, UnicodeError, ValueError, RecursionError):
        result = {**_base(), "reasons": ["input unreadable or invalid"]}
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0 if result["status"] == "REVIEW_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
