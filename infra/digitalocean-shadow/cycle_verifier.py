"""Offline verifier for the four artifacts that form one research cycle.

The verifier proves only local technical coherence.  It cannot authenticate an
account, approve money, validate a strategy, or authorize an order.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any

from cycle_contract import (
    COVERAGE_SCHEMA,
    HEALTH_MODE,
    MAX_ORDERBOOKS,
    PACKET_SCHEMA,
    RISK_SCHEMA,
)


class CycleVerificationError(ValueError):
    """The verifier itself received invalid parameters."""


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise CycleVerificationError("INVALID_TIMESTAMP")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CycleVerificationError("INVALID_TIMESTAMP") from None
    if parsed.tzinfo is None:
        raise CycleVerificationError("NAIVE_TIMESTAMP")
    try:
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise CycleVerificationError("TIMESTAMP_OUT_OF_RANGE") from None


def _cycle_id(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        return value
    return None


def _digest(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    try:
        raw = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return None
    return hashlib.sha256(raw).hexdigest()


def _fresh(
    artifact: dict[str, Any] | None,
    *,
    field: str,
    prefix: str,
    now: datetime,
    max_age_seconds: int,
    reasons: list[str],
) -> None:
    if artifact is None:
        return
    try:
        when = _timestamp(artifact.get(field))
    except CycleVerificationError:
        reasons.append(f"{prefix}_TIME_INVALID")
        return
    age = (now - when).total_seconds()
    if not 0 <= age <= max_age_seconds:
        reasons.append(f"{prefix}_STALE_OR_FUTURE")


def _authority_false(
    artifact: dict[str, Any] | None,
    *,
    prefix: str,
    fields: tuple[str, ...],
    reasons: list[str],
) -> None:
    if artifact is None:
        return
    for field in fields:
        if artifact.get(field) is not False:
            reasons.append(f"{prefix}_{field.upper()}_NOT_FALSE")


def verify_cycle(
    health: dict[str, Any] | None,
    packet: dict[str, Any] | None,
    coverage: dict[str, Any] | None,
    risk: dict[str, Any] | None,
    *,
    now: datetime,
    max_age_seconds: int = 180,
) -> dict[str, Any]:
    """Return a bounded, fail-closed technical decision for one cycle."""
    if now.tzinfo is None:
        raise CycleVerificationError("NAIVE_CHECK_TIME")
    if not 1 <= max_age_seconds <= 3600:
        raise CycleVerificationError("INVALID_MAX_AGE")
    now = now.astimezone(UTC)
    reasons: list[str] = []
    artifacts = {
        "HEALTH": health,
        "PACKET": packet,
        "COVERAGE": coverage,
        "RISK": risk,
    }
    for name, artifact in artifacts.items():
        if not isinstance(artifact, dict):
            reasons.append(f"{name}_UNAVAILABLE")

    if isinstance(health, dict):
        if health.get("mode") != HEALTH_MODE:
            reasons.append("HEALTH_MODE_NOT_READONLY")
        if health.get("running") is not True:
            reasons.append("HEALTH_NOT_RUNNING")
        if health.get("last_error"):
            reasons.append("HEALTH_COLLECTOR_ERROR")
    _authority_false(
        health,
        prefix="HEALTH",
        fields=("execution_authorized",),
        reasons=reasons,
    )

    market_count: int | None = None
    if isinstance(packet, dict):
        if packet.get("schema_version") != PACKET_SCHEMA:
            reasons.append("PACKET_SCHEMA_UNSUPPORTED")
        if packet.get("mode") != HEALTH_MODE:
            reasons.append("PACKET_MODE_NOT_READONLY")
        source = packet.get("kalshi")
        source = source if isinstance(source, dict) else {}
        markets = source.get("markets")
        stated = source.get("market_count")
        if (
            not isinstance(markets, list)
            or type(stated) is not int
            or not 0 <= stated <= MAX_ORDERBOOKS
            or len(markets) != stated
        ):
            reasons.append("PACKET_MARKET_COUNT_INVALID")
        else:
            market_count = stated
    _authority_false(
        packet,
        prefix="PACKET",
        fields=("execution_authorized",),
        reasons=reasons,
    )
    if isinstance(health, dict) and market_count is not None:
        if health.get("kalshi_markets") != market_count:
            reasons.append("HEALTH_PACKET_COUNT_MISMATCH")

    coverage_state = "UNVERIFIED"
    if isinstance(coverage, dict):
        if coverage.get("schema_version") != COVERAGE_SCHEMA:
            reasons.append("COVERAGE_SCHEMA_UNSUPPORTED")
        boolean_fields = (
            "cursor_exhausted",
            "truncated_by_page_limit",
            "truncated_by_orderbook_limit",
        )
        if any(type(coverage.get(field)) is not bool for field in boolean_fields):
            reasons.append("COVERAGE_FLAGS_INVALID")
        numeric_fields = (
            "open_markets_seen",
            "eligible_in_horizon",
            "orderbooks_fetched",
        )
        if any(type(coverage.get(field)) is not int or coverage[field] < 0 for field in numeric_fields):
            reasons.append("COVERAGE_COUNT_INVALID")
        else:
            fetched = coverage["orderbooks_fetched"]
            eligible = coverage["eligible_in_horizon"]
            seen = coverage["open_markets_seen"]
            if fetched > MAX_ORDERBOOKS or fetched > eligible or eligible > seen:
                reasons.append("COVERAGE_COUNT_INVALID")
            if market_count is not None and fetched != market_count:
                reasons.append("COVERAGE_PACKET_COUNT_MISMATCH")
            truncated = coverage.get("truncated_by_orderbook_limit") is True
            page_partial = coverage.get("truncated_by_page_limit") is True
            if eligible > fetched and not truncated:
                reasons.append("COVERAGE_TRUNCATION_NOT_DECLARED")
            coverage_state = "PARTIAL" if truncated or page_partial else "BOUNDED_COMPLETE"

    if isinstance(risk, dict):
        if risk.get("schema_version") != RISK_SCHEMA:
            reasons.append("RISK_SCHEMA_UNSUPPORTED")
        if risk.get("bank_state") not in {"PENDING_RECONCILIATION", "DECLARED", "SIMULATION"}:
            reasons.append("RISK_STATUS_BLOCKED")
    _authority_false(
        risk,
        prefix="RISK",
        fields=("execution_authorized", "order_capability_present", "real_entry_eligible"),
        reasons=reasons,
    )

    _fresh(
        health,
        field="updated_at",
        prefix="HEALTH",
        now=now,
        max_age_seconds=max_age_seconds,
        reasons=reasons,
    )
    for name, artifact in (("PACKET", packet), ("COVERAGE", coverage), ("RISK", risk)):
        _fresh(
            artifact,
            field="generated_at",
            prefix=name,
            now=now,
            max_age_seconds=max_age_seconds,
            reasons=reasons,
        )

    ids = {
        "health": _cycle_id(health.get("last_cycle_id")) if isinstance(health, dict) else None,
        "packet": _cycle_id(packet.get("packet_id")) if isinstance(packet, dict) else None,
        "coverage": _cycle_id(coverage.get("cycle_id")) if isinstance(coverage, dict) else None,
        "risk": _cycle_id(risk.get("cycle_id")) if isinstance(risk, dict) else None,
    }
    for name, value in ids.items():
        if value is None:
            reasons.append(f"{name.upper()}_CYCLE_ID_INVALID")
    valid_ids = {value for value in ids.values() if value is not None}
    if len(valid_ids) > 1:
        reasons.append("CYCLE_MISMATCH")
    cycle_id = next(iter(valid_ids)) if len(valid_ids) == 1 else None

    reasons = sorted(set(reasons))
    return {
        "schema_version": "botkalshi-cycle-verification-v1",
        "checked_at": now.isoformat(),
        "cycle_id": cycle_id,
        "technical_status": "VERIFIED" if not reasons else "BLOCKED",
        "blockers": reasons,
        "coverage_status": coverage_state,
        "observed_market_count": market_count,
        "artifact_sha256": {
            "health": _digest(health),
            "packet": _digest(packet),
            "coverage": _digest(coverage),
            "risk": _digest(risk),
        },
        "assistant_assessment_allowed": not reasons,
        "bank_reconciled": False,
        "real_entry_eligible": False,
        "execution_authorized": False,
        "order_capability_present": False,
    }
