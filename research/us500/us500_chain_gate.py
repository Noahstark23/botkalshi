"""Read-only continuity gate for bounded US500 public capture segments.

Reads only bounded JSON manifests, external plans and prior status files. Never
opens the collector, SQLite, network, credentials, or an order/execution interface.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import time
from pathlib import Path

from us500_engine import DataError, body_digest, decode, timestamp
from us500_window_capture import CADENCE_MS, MAX_PLAN_BYTES, validate_plan

CHAIN_SCHEMA = "us500-public-chain-v1"
MAX_CHAIN_BYTES = 131_072
MAX_CHAIN_PLANS = 8
MAX_CHAIN_GETS = 15_000
MAX_FUTURE_STATUS_MS = 5_000
CHAIN_FIELDS = {
    "schema",
    "chain_id",
    "market_open_ms",
    "observed_close_ms",
    "discovery_gets",
    "max_gets",
    "segments",
}
SLOT_STATES = {
    "USABLE",
    "INVALID",
    "MISSED",
    "NOT_COLLECTED",
    "CLOSED",
    "WAITING",
    "IN_PROGRESS",
    "INTERRUPTED_UNKNOWN",
}
FORBIDDEN_SLOT_STATES = {"CLOSED", "WAITING", "IN_PROGRESS", "INTERRUPTED_UNKNOWN"}
ALLOWED_TERMINALS = {
    ("COMPLETE", "ALL_SLOTS_FINISHED"),
    ("INCOMPLETE", "ALL_SLOTS_FINISHED"),
    ("INCOMPLETE", "WINDOW_ENDED"),
    ("INCOMPLETE", "DEADLINE_REACHED"),
}
FALSE_FLAGS = {
    "continuous_market_history_verified",
    "source_authenticity_verified",
    "independence_verified",
    "strategy_validated",
    "execution_available",
}


def validate_chain(chain):
    """Validate exact immutable plan contents; a digest is not origin authentication."""
    if type(chain) is not dict or set(chain) != CHAIN_FIELDS:
        raise DataError("INVALID_CHAIN_FIELDS")
    if (
        chain["schema"] != CHAIN_SCHEMA
        or not isinstance(chain["chain_id"], str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", chain["chain_id"])
    ):
        raise DataError("INVALID_CHAIN_IDENTITY")
    opening = timestamp(chain["market_open_ms"])
    close = timestamp(chain["observed_close_ms"])
    if not opening < close or opening % CADENCE_MS or close % CADENCE_MS:
        raise DataError("INVALID_CHAIN_MARKET_TIMES")
    if type(chain["discovery_gets"]) is not int or chain["discovery_gets"] != 1:
        raise DataError("INVALID_CHAIN_DISCOVERY_BUDGET")
    if (
        type(chain["max_gets"]) is not int
        or not 1 <= chain["max_gets"] <= MAX_CHAIN_GETS
    ):
        raise DataError("INVALID_CHAIN_TOTAL_BUDGET")
    segments = chain["segments"]
    if not isinstance(segments, list) or not 1 <= len(segments) <= MAX_CHAIN_PLANS:
        raise DataError("INVALID_CHAIN_SEGMENT_COUNT")
    previous_end = opening
    collection_ids = set()
    total_gets = chain["discovery_gets"]
    checked = []
    for index, segment in enumerate(segments, 1):
        name = f"part{index:02d}"
        if (
            type(segment) is not dict
            or set(segment) != {"name", "state_directory", "plan"}
            or segment["name"] != name
            or segment["state_directory"] != ("session" if index == 1 else name)
        ):
            raise DataError("INVALID_CHAIN_SEGMENT_IDENTITY")
        plan = validate_plan(segment["plan"])
        if plan["start_ms"] != previous_end:
            raise DataError("NONCONTIGUOUS_CHAIN_SEGMENTS")
        if plan["collection_id"] in collection_ids:
            raise DataError("DUPLICATE_CHAIN_COLLECTION_ID")
        collection_ids.add(plan["collection_id"])
        previous_end = plan["end_ms"]
        total_gets += plan["max_gets"]
        checked.append(
            {"name": name, "state_directory": segment["state_directory"], "plan": plan}
        )
    if previous_end != close + CADENCE_MS:
        raise DataError("CHAIN_MUST_INCLUDE_DECLARED_CLOSE_SLOT")
    if total_gets != chain["max_gets"] or total_gets > MAX_CHAIN_GETS:
        raise DataError("CHAIN_GET_BUDGET_MISMATCH")
    return {**chain, "segments": checked}


def _safe_permissions(info, *, immutable=False):
    if (
        info.st_uid not in ({0} if immutable else {0, os.geteuid()})
        or info.st_mode & 0o022
        or (immutable and info.st_mode & 0o222)
    ):
        raise DataError("CHAIN_JSON_UNSAFE_PERMISSIONS")


def _read_descriptor(descriptor, maximum, *, immutable=False):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise DataError("CHAIN_JSON_REGULAR_FILE_REQUIRED")
    _safe_permissions(info, immutable=immutable)
    if info.st_size > maximum:
        raise DataError("CHAIN_JSON_TOO_LARGE")
    chunks, remaining = [], maximum + 1
    while remaining:
        block = os.read(descriptor, min(65_536, remaining))
        if not block:
            break
        chunks.append(block)
        remaining -= len(block)
    raw = b"".join(chunks)
    if len(raw) > maximum:
        raise DataError("CHAIN_JSON_TOO_LARGE")
    return decode(raw)


def read_json(path, *, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        return _read_descriptor(descriptor, maximum, immutable=True)
    finally:
        os.close(descriptor)


def _read_previous(state_root, directory):
    """Fixed child path, no symlink traversal through the root/segment/status."""
    root = os.open(state_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    child = descriptor = None
    try:
        _safe_permissions(os.fstat(root))
        child = os.open(
            directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root
        )
        _safe_permissions(os.fstat(child))
        descriptor = os.open(
            "status.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=child
        )
        return _read_descriptor(descriptor, MAX_PLAN_BYTES)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if child is not None:
            os.close(child)
        os.close(root)


def _nonnegative_int(value):
    return type(value) is int and value >= 0


def _validate_status(status, plan, now_ms):
    """Return one stable blocker or None. Invalid shape/identity raises DataError."""
    if (
        not isinstance(status, dict)
        or status.get("schema") != "us500-public-window-status-v1"
    ):
        raise DataError("INVALID_PREVIOUS_STATUS_SCHEMA")
    if status.get("collection_id") != plan["collection_id"] or status.get(
        "plan_sha256"
    ) != body_digest(plan):
        raise DataError("PREVIOUS_PLAN_IDENTITY_MISMATCH")
    if any(status.get(field) is not False for field in FALSE_FLAGS):
        raise DataError("UNSUPPORTED_PREVIOUS_STATUS_CLAIM")
    if type(status.get("actual_orders")) is not int or status["actual_orders"] != 0:
        raise DataError("UNSUPPORTED_PREVIOUS_STATUS_CLAIM")
    if (
        type(status.get("expected_slots")) is not int
        or status["expected_slots"] != plan["max_slots"]
    ):
        raise DataError("PREVIOUS_SLOT_BUDGET_MISMATCH")
    if (
        type(status.get("max_gets")) is not int
        or status["max_gets"] != plan["max_gets"]
    ):
        raise DataError("PREVIOUS_GET_BUDGET_MISMATCH")
    counts = status.get("slot_counts")
    if (
        not isinstance(counts, dict)
        or not counts
        or set(counts) - SLOT_STATES
        or any(not _nonnegative_int(value) for value in counts.values())
        or sum(counts.values()) != plan["max_slots"]
        or status.get("slot_counts_meaning") != "classification_when_saved"
    ):
        raise DataError("INVALID_PREVIOUS_SLOT_COUNTS")
    reserved = status.get("reserved_gets")
    attempted = status.get("attempted_gets")
    unused = status.get("unused_reserved_gets")
    expected_reserved = 2 * sum(
        counts.get(key, 0)
        for key in ("USABLE", "INVALID", "CLOSED", "IN_PROGRESS", "INTERRUPTED_UNKNOWN")
    )
    if (
        not all(_nonnegative_int(value) for value in (reserved, attempted, unused))
        or not attempted <= reserved <= plan["max_gets"]
        or attempted < 2 * counts.get("USABLE", 0) + counts.get("CLOSED", 0)
        or unused != reserved - attempted
        or reserved != expected_reserved
    ):
        raise DataError("INVALID_PREVIOUS_GET_ACCOUNTING")
    if "instrument_metadata_sha256" not in status:
        raise DataError("MISSING_PREVIOUS_INSTRUMENT_DIGEST")
    digest = status["instrument_metadata_sha256"]
    if digest is not None and (
        not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise DataError("INVALID_PREVIOUS_INSTRUMENT_DIGEST")
    if counts.get("USABLE", 0) and digest is None:
        raise DataError("MISSING_PREVIOUS_INSTRUMENT_DIGEST")
    written = timestamp(status.get("status_written_at_ms"))
    if written > now_ms + MAX_FUTURE_STATUS_MS:
        return "PREVIOUS_STATUS_IN_FUTURE"
    if written < plan["end_ms"] - CADENCE_MS:
        return "PREVIOUS_STATUS_BEFORE_LAST_SLOT"
    if status.get("retained_artifacts_integrity_checked") is not True:
        return "PREVIOUS_EVIDENCE_INTEGRITY_UNCONFIRMED"
    if any(counts.get(key, 0) for key in FORBIDDEN_SLOT_STATES):
        return "PREVIOUS_UNRESOLVED_OR_CLOSED_SLOT"
    state, reason = status.get("status"), status.get("reason")
    if state == "ACTIVE":
        return "PREVIOUS_SEGMENT_NOT_CONTINUABLE"
    if not isinstance(state, str) or not isinstance(reason, str):
        raise DataError("INVALID_PREVIOUS_TERMINAL_FIELDS")
    if (state, reason) not in ALLOWED_TERMINALS:
        return "PREVIOUS_SEGMENT_NOT_CONTINUABLE"
    full = status.get("full_planned_slot_coverage")
    if type(full) is not bool:
        raise DataError("INVALID_PREVIOUS_COVERAGE_FLAG")
    expected_full = counts.get("USABLE", 0) == plan["max_slots"]
    if full != expected_full or (state == "COMPLETE" and not full):
        raise DataError("CONTRADICTORY_PREVIOUS_COVERAGE")
    return None


def gate(chain, segment, external_plan, state_root, *, now_ms):
    """Pure read-only decision. It never starts a collector or modifies state."""
    chain = validate_chain(chain)
    now = timestamp(now_ms)
    if not isinstance(segment, str) or not re.fullmatch(r"part0[1-8]", segment):
        raise DataError("INVALID_REQUESTED_SEGMENT")
    indices = {part["name"]: i for i, part in enumerate(chain["segments"])}
    if segment not in indices:
        raise DataError("REQUESTED_SEGMENT_NOT_IN_CHAIN")
    index = indices[segment]
    current = chain["segments"][index]
    supplied = validate_plan(external_plan)
    if supplied != current["plan"] or body_digest(supplied) != body_digest(
        current["plan"]
    ):
        raise DataError("EXTERNAL_PLAN_MISMATCH")
    plan = current["plan"]
    result = {
        "schema": "us500-chain-gate-result-v1",
        "chain_id": chain["chain_id"],
        "chain_sha256": body_digest(chain),
        "segment": segment,
        "plan_sha256": body_digest(plan),
        "checked_at_ms": now,
        "allowed": False,
        "reason": None,
        "prior_segments_checked": [],
        "prior_terminal_states": {},
        "prior_coverage_incomplete": False,
        "prior_gap_counts": {},
        "local_finalization_only": now >= plan["end_ms"],
        "full_session_verified": False,
        "source_authenticity_verified": False,
        "independence_verified": False,
        "strategy_validated": False,
        "execution_available": False,
        "actual_orders": 0,
    }
    if index == 0:
        result["reason"] = "FIRST_SEGMENT_SEPARATELY_MANAGED"
        return result
    if now < plan["start_ms"]:
        result["reason"] = "BEFORE_SEGMENT_START"
        return result
    if now >= plan["end_ms"] + CADENCE_MS:
        result["reason"] = "SEGMENT_WINDOW_EXPIRED"
        return result
    known_instrument_digest = None
    for prior in chain["segments"][:index]:
        try:
            status = _read_previous(state_root, prior["state_directory"])
        except FileNotFoundError:
            result.update(
                reason="PREVIOUS_STATUS_MISSING", blocking_segment=prior["name"]
            )
            return result
        blocker = _validate_status(status, prior["plan"], now)
        result["prior_segments_checked"].append(prior["name"])
        if blocker:
            result.update(reason=blocker, blocking_segment=prior["name"])
            return result
        instrument_digest = status["instrument_metadata_sha256"]
        if instrument_digest is not None:
            if (
                known_instrument_digest is not None
                and instrument_digest != known_instrument_digest
            ):
                result.update(
                    reason="PREVIOUS_INSTRUMENT_IDENTITY_CHANGED",
                    blocking_segment=prior["name"],
                )
                return result
            known_instrument_digest = instrument_digest
        result["prior_terminal_states"][prior["name"]] = status["status"]
        if not status["full_planned_slot_coverage"]:
            result["prior_coverage_incomplete"] = True
        for kind in ("INVALID", "MISSED", "NOT_COLLECTED"):
            count = status["slot_counts"].get(kind, 0)
            if count:
                result["prior_gap_counts"][kind] = (
                    result["prior_gap_counts"].get(kind, 0) + count
                )
    result.update(
        allowed=True,
        reason="LOCAL_FINALIZATION_ALLOWED"
        if result["local_finalization_only"]
        else "PRIOR_SEGMENTS_CONTINUABLE",
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", type=Path, required=True)
    parser.add_argument("--segment", required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = gate(
            read_json(args.chain, maximum=MAX_CHAIN_BYTES),
            args.segment,
            read_json(args.plan, maximum=MAX_PLAN_BYTES),
            args.state_root,
            now_ms=time.time_ns() // 1_000_000,
        )
    except (DataError, OSError):
        print(
            json.dumps(
                {
                    "schema": "us500-chain-gate-result-v1",
                    "allowed": False,
                    "error": "CHAIN_GATE_VALIDATION_FAILED",
                    "execution_available": False,
                }
            )
        )
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["allowed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
