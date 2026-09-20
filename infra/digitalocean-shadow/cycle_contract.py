"""Shared, side-effect-free contract for one research capture cycle."""

from __future__ import annotations


MAX_ORDERBOOKS = 100
PACKET_SCHEMA = "botkalshi-research-packet-v1"
COVERAGE_SCHEMA = "botkalshi-coverage-v2"
RISK_SCHEMA = "botkalshi-risk-status-v2"
HEALTH_MODE = "SHADOW_READONLY"


class CycleContractError(ValueError):
    """The requested capture shape is outside the reviewed safety contract."""


def validate_orderbook_limit(value: int) -> int:
    """Return a valid limit or fail before any provider request is attempted."""
    if type(value) is not int or not 1 <= value <= MAX_ORDERBOOKS:
        raise CycleContractError(f"max_orderbooks must be between 1 and {MAX_ORDERBOOKS}")
    return value
