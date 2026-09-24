"""Pure, read-only diagnostic review of a caller-declared M1 YES/NO book.

`review_m1_book` is a data-validation + math-only research probe. It is NOT
the deployed M1 circuit, does not talk to any exchange/websocket/engine, and
never produces orders, fills, reservations, or realized profit. It reuses
`src.math.arbitrage.detect_binary_arb` strictly inside that function's
supported domain (integer cents 1..99, integer contract counts) and fails
closed (NOT_EVALUATED / BLOCKED) outside that domain instead of truncating.

A NO_ARBITRAGE result only means the legacy binary detector found nothing for
THIS ticker's two-sided book; it says nothing about multi-outcome/event-level
arbitrage (see `detect_multi_outcome_arb`), which this module does not touch.

mode is always SIMULATION_ONLY and evidence_state is always
DECLARED_BOOK_NOT_EXCHANGE_VERIFIED: `book_observed_at` is whatever the
caller declares and is never derived from a `generated_at`/capture-time
field, and its presence proves nothing about WebSocket sequencing or
executability against the live order book.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from fractions import Fraction
from typing import NamedTuple

import repo_root  # noqa: F401 — puts the repo root on sys.path (system python3)

from src.math.arbitrage import detect_binary_arb

SCHEMA_VERSION_INPUT = "botkalshi-m1-book-input-v1"
SCHEMA_VERSION_OUTPUT = "botkalshi-m1-book-review-v1"

MAX_BOOK_AGE_SECONDS = 180
MAX_LEVELS = 100
PRICE_MAX_DECIMALS = 4
QTY_MAX_DECIMALS = 2

_CYCLE_ID_RE = re.compile(r"^[\x21-\x7e]{1,64}$")
_TICKER_RE = re.compile(r"^[A-Z0-9-]{3,128}$")
_PRICE_RE = re.compile(r"^(0(\.\d{1,10})?|1(\.0{1,10})?)$")
_QTY_RE = re.compile(r"^\d{1,15}(\.\d{1,10})?$")
_EXPECTED_FIELDS = {
    "schema_version",
    "cycle_id",
    "ticker",
    "book_observed_at",
    "price_unit",
    "yes_bids",
    "no_bids",
}


class _Parsed(NamedTuple):
    value: Fraction
    canonical: str


class _Level(NamedTuple):
    price: Fraction
    price_str: str
    quantity: Fraction
    quantity_str: str


def _validate_cycle_id(raw: object) -> str | None:
    if type(raw) is not str or not _CYCLE_ID_RE.fullmatch(raw):
        return None
    return raw


def _validate_ticker(raw: object) -> str | None:
    if type(raw) is not str or not _TICKER_RE.fullmatch(raw):
        return None
    return raw


def _effective_decimals(fractional_digits: str) -> int:
    return len(fractional_digits.rstrip("0"))


def _parse_price(raw: object) -> _Parsed | None:
    if type(raw) is not str or not raw or len(raw) > 16:
        return None
    if not _PRICE_RE.fullmatch(raw):
        return None
    frac = raw.split(".", 1)[1] if "." in raw else ""
    if _effective_decimals(frac) > PRICE_MAX_DECIMALS:
        return None
    return _Parsed(value=Fraction(raw), canonical=raw)


def _parse_quantity(raw: object) -> _Parsed | None:
    if type(raw) is not str or not raw or len(raw) > 26:
        return None
    if not _QTY_RE.fullmatch(raw):
        return None
    frac = raw.split(".", 1)[1] if "." in raw else ""
    if _effective_decimals(frac) > QTY_MAX_DECIMALS:
        return None
    value = Fraction(raw)
    if value <= 0:
        return None
    return _Parsed(value=value, canonical=raw)


def _validate_levels(raw: object, *, side: str) -> tuple[list[_Level] | None, str | None]:
    if not isinstance(raw, list):
        return None, f"INVALID_{side}_BIDS"
    if len(raw) > MAX_LEVELS:
        return None, f"TOO_MANY_{side}_LEVELS"
    levels: list[_Level] = []
    seen: set[Fraction] = set()
    for row in raw:
        if type(row) is not list or len(row) != 2:
            return None, f"INVALID_{side}_BID_ROW"
        price = _parse_price(row[0])
        qty = _parse_quantity(row[1])
        if price is None or qty is None:
            return None, f"INVALID_{side}_BID_ROW"
        if price.value in seen:
            return None, f"DUPLICATE_{side}_PRICE_LEVEL"
        seen.add(price.value)
        levels.append(_Level(price.value, price.canonical, qty.value, qty.canonical))
    return levels, None


def _parse_observed_at(raw: object) -> datetime | None:
    if type(raw) is not str or not raw or len(raw) > 40:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(UTC)


def _check_freshness(observed_at: datetime, now: datetime) -> str | None:
    if observed_at > now:
        return "BOOK_OBSERVED_IN_FUTURE"
    if (now - observed_at).total_seconds() > MAX_BOOK_AGE_SECONDS:
        return "BOOK_OBSERVATION_STALE"
    return None


def _format_price(value: Fraction) -> str:
    scaled = value * 10_000
    n = scaled.numerator
    int_part, frac_part = divmod(n, 10_000)
    return f"{int_part}.{frac_part:04d}"


def _level_view(level: _Level | None) -> dict | None:
    if level is None:
        return None
    return {"price_usd": level.price_str, "quantity_contracts": level.quantity_str}


def _ask_view(price: Fraction, depth_str: str) -> dict:
    return {"price_usd": _format_price(price), "quantity_contracts": depth_str}


def _build_result(
    *,
    status: str,
    reason_codes: list[str],
    now: datetime,
    cycle_id: str | None = None,
    ticker: str | None = None,
    observed_at: datetime | None = None,
    book_age_seconds: float | None = None,
    best_yes_bid: _Level | None = None,
    best_no_bid: _Level | None = None,
    yes_ask: dict | None = None,
    no_ask: dict | None = None,
    legacy_detector_called: bool = False,
) -> dict:
    return {
        "schema": SCHEMA_VERSION_OUTPUT,
        "status": status,
        "reason_codes": reason_codes,
        "origin": "M1",
        "mode": "SIMULATION_ONLY",
        "evidence_state": "DECLARED_BOOK_NOT_EXCHANGE_VERIFIED",
        "cycle_id": cycle_id,
        "ticker": ticker,
        "book_observed_at": observed_at.isoformat() if observed_at else None,
        "evaluated_at": now.isoformat(),
        "book_age_seconds": book_age_seconds,
        "freshness_window_seconds": MAX_BOOK_AGE_SECONDS,
        "best_yes_bid": _level_view(best_yes_bid),
        "best_no_bid": _level_view(best_no_bid),
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "legacy_detector_called": legacy_detector_called,
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
        "recommendations": [],
        "real_orders": [],
        "profit_realized_usd": None,
    }


def review_m1_book(raw: object, *, now: datetime | None = None) -> dict:
    """Validate a declared M1 YES/NO book and run the legacy pure-math check.

    Returns a structured diagnostic dict, never an order/fill/position. See
    module docstring for scope and the invariants this deliberately enforces
    (fail-closed on ambiguity, crossed books never treated as safe arb).
    """
    resolved_now = _resolve_now(now)

    if not isinstance(raw, dict):
        return _build_result(status="BLOCKED", reason_codes=["INVALID_INPUT_SHAPE"], now=resolved_now)

    reason_codes: list[str] = []
    if set(raw) - _EXPECTED_FIELDS:
        reason_codes.append("UNEXPECTED_INPUT_FIELDS")
    if raw.get("schema_version") != SCHEMA_VERSION_INPUT:
        reason_codes.append("INVALID_SCHEMA_VERSION")

    cycle_id = _validate_cycle_id(raw.get("cycle_id"))
    if cycle_id is None:
        reason_codes.append("INVALID_CYCLE_ID")

    ticker = _validate_ticker(raw.get("ticker"))
    if ticker is None:
        reason_codes.append("INVALID_TICKER")

    if raw.get("price_unit") != "USD":
        reason_codes.append("INVALID_PRICE_UNIT")

    observed_at = _parse_observed_at(raw.get("book_observed_at"))
    if observed_at is None:
        reason_codes.append("INVALID_BOOK_OBSERVED_AT")

    yes_levels, yes_reason = _validate_levels(raw.get("yes_bids"), side="YES")
    if yes_reason:
        reason_codes.append(yes_reason)

    no_levels, no_reason = _validate_levels(raw.get("no_bids"), side="NO")
    if no_reason:
        reason_codes.append(no_reason)

    common: dict = {"now": resolved_now, "cycle_id": cycle_id, "ticker": ticker}

    if reason_codes:
        return _build_result(status="BLOCKED", reason_codes=reason_codes, **common)

    freshness_reason = _check_freshness(observed_at, resolved_now)
    if freshness_reason:
        return _build_result(
            status="BLOCKED", reason_codes=[freshness_reason], observed_at=observed_at, **common
        )

    common["observed_at"] = observed_at
    common["book_age_seconds"] = (resolved_now - observed_at).total_seconds()

    if not yes_levels or not no_levels:
        return _build_result(status="NO_DATA", reason_codes=["EMPTY_BOOK_SIDE"], **common)

    best_yes = max(yes_levels, key=lambda lvl: lvl.price)
    best_no = max(no_levels, key=lambda lvl: lvl.price)
    common["best_yes_bid"] = best_yes
    common["best_no_bid"] = best_no

    if best_yes.price + best_no.price > 1:
        return _build_result(status="BLOCKED", reason_codes=["CROSSED_OR_INCOHERENT_BOOK"], **common)

    yes_ask_price = 1 - best_no.price
    no_ask_price = 1 - best_yes.price
    common["yes_ask"] = _ask_view(yes_ask_price, best_no.quantity_str)
    common["no_ask"] = _ask_view(no_ask_price, best_yes.quantity_str)

    if (yes_ask_price * 100).denominator != 1 or (no_ask_price * 100).denominator != 1:
        return _build_result(status="NOT_EVALUATED", reason_codes=["UNSUPPORTED_LEGACY_PRECISION"], **common)
    if best_no.quantity.denominator != 1 or best_yes.quantity.denominator != 1:
        return _build_result(status="NOT_EVALUATED", reason_codes=["UNSUPPORTED_LEGACY_PRECISION"], **common)

    yes_ask_cents = int(yes_ask_price * 100)
    no_ask_cents = int(no_ask_price * 100)
    if yes_ask_cents in (0, 100) or no_ask_cents in (0, 100):
        return _build_result(status="NOT_EVALUATED", reason_codes=["BOUNDARY_PRICE"], **common)

    opportunity = detect_binary_arb(
        ticker,
        yes_ask_cents,
        int(best_no.quantity),
        no_ask_cents,
        int(best_yes.quantity),
        max_count=1,
    )

    if opportunity is None:
        return _build_result(status="NO_ARBITRAGE", reason_codes=[], legacy_detector_called=True, **common)

    return _build_result(
        status="BLOCKED",
        reason_codes=["INVARIANT_REQUIRES_REVIEW"],
        legacy_detector_called=True,
        **common,
    )
