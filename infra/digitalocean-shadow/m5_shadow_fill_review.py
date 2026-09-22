"""Pure, read-only review of ONE declared M5 (market-maker) shadow-fill evidence.

`review_m5_fill` validates a caller-declared shadow fill against a REQUIRED
shape (id, side, price_cents, count, fee_effective_cents, fee_model, ticker,
experiment_id, metric_version, fee_source, fee_type, fee_multiplier — every
field mandatory, no optional fallbacks) and, only when every field is
present, well-formed, AND the declared `fee_effective_cents` reconciles
EXACTLY against the fee this module recomputes from first principles, returns
the worst-case loss in cents for that one fill:

  - side == "buy"  (long YES):              max_loss_cents = price_cents*count + fee_effective_cents
  - side == "sell" (short YES / long NO):    max_loss_cents = (100-price_cents)*count + fee_effective_cents

That number is EXACT only CONDITIONAL on the declared evidence validating —
a BLOCKED result makes no claim of exactness, or of anything else, about the
fill it was given. `evidence_state` is always `DECLARED_FILL_NOT_LEDGER_VERIFIED`:
this module never reads the durable `MMShadowFill` row back to confirm the
caller told the truth, so even an ACCEPTED result is a claim about a
self-consistent DECLARATION, not a ledger-verified fact.

This mirrors `InventoryBook.apply_fill`'s cash convention (buy debits price*count,
sell credits price*count) taken to its worst case: a long YES can lose at most
what it paid (price*count) plus its fee; a short YES / long NO can lose at most
100-price_cents per contract (the most YES can rise to) plus its fee.

Fee reconciliation (the exactness gate): `fee_effective_cents` must equal the
fee this module recomputes from `count`, `price_cents`, `fee_model` and
`fee_multiplier` using Kalshi's official quadratic schedule
(`src/math/fees.py::kalshi_fee_cents` / `kalshi_maker_fee_cents`), and must be
strictly positive — a declared fee that is merely non-negative but wrong, or
that is exactly zero, is blocked exactly like a missing field. `fee_multiplier`
is converted with `Fraction(str(value))` (never a raw float division) so the
comparison can never drift by binary floating-point error; a `bool` is
explicitly rejected even though `bool` is an `int` subclass in Python.

This module does NOT talk to Kalshi, a database, `RiskManager`, an executor, or
any account/portfolio state — it is math and validation only, exactly like
`m1_observation.review_m1_book`. It never reserves capital; `m5_bank_bridge.py`
composes this with `simulation_bank.reserve_with_evidence` for that.

`cycle_id` and `idempotency_key` are NOT accepted from the caller. They are
derived here, deterministically, from BOTH `fill.experiment_id` AND `fill.id`
together — never from `ticker` alone (repeats across fills), never from just
one of the two (two different experiments replaying the same numeric fill id
would otherwise collide), and NEVER from a collector-captured research
`packet_id`. There is no captured cycle for an M5 shadow fill — pretending
otherwise would misrepresent the evidence chain; this is a SIMULATED,
self-contained identity for the bank ledger only.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

SCHEMA_VERSION_INPUT = "botkalshi-m5-fill-input-v1"
SCHEMA_VERSION_OUTPUT = "botkalshi-m5-fill-review-v1"
ORIGIN = "M5"

METRIC_VERSION = "f1-v2-bbo-depth"
FEE_TYPE = "quadratic_with_maker_fees"

MAX_FILL_ID = 2**63 - 1
MAX_COUNT = 1_000_000
MAX_FEE_EFFECTIVE_CENTS = 100_000_000  # $1,000,000 — generous, still bounded.
MIN_PRICE_CENTS = 1
MAX_PRICE_CENTS = 99
MAX_FEE_MULTIPLIER = Fraction(1_000, 1)
_MAX_FEE_MULTIPLIER_TEXT_LEN = 64

_TAKER_BASE_DENOMINATOR = 10_000
_MAKER_BASE_DENOMINATOR = 40_000

_SIDES = ("buy", "sell")
_FEE_MODELS = ("maker", "taker")
_FEE_SOURCES = ("series", "event_override")

_TICKER_RE = re.compile(r"^[A-Z0-9-]{3,128}$")
_EXPERIMENT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")

_ROOT_FIELDS = {"schema_version", "fill"}
_REQUIRED_FILL_FIELDS = {
    "id",
    "side",
    "price_cents",
    "count",
    "fee_effective_cents",
    "fee_model",
    "ticker",
    "experiment_id",
    "metric_version",
    "fee_source",
    "fee_type",
    "fee_multiplier",
}
_ALLOWED_FILL_FIELDS = _REQUIRED_FILL_FIELDS


def _resolve_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if not isinstance(now, datetime) or now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(UTC)


def _is_plain_int(value: object) -> bool:
    # bool is a subclass of int in Python; True/False must never pass as a count/id/price.
    return type(value) is int


def _validate_fill_id(raw: object, reasons: list[str]) -> int | None:
    if not _is_plain_int(raw) or not 1 <= raw <= MAX_FILL_ID:
        reasons.append("INVALID_FILL_ID")
        return None
    return raw


def _validate_side(raw: object, reasons: list[str]) -> str | None:
    if not isinstance(raw, str) or raw not in _SIDES:
        reasons.append("INVALID_SIDE")
        return None
    return raw


def _validate_price_cents(raw: object, reasons: list[str]) -> int | None:
    if not _is_plain_int(raw) or not MIN_PRICE_CENTS <= raw <= MAX_PRICE_CENTS:
        reasons.append("INVALID_PRICE_CENTS")
        return None
    return raw


def _validate_count(raw: object, reasons: list[str]) -> int | None:
    if not _is_plain_int(raw) or not 1 <= raw <= MAX_COUNT:
        reasons.append("INVALID_COUNT")
        return None
    return raw


def _validate_fee_effective_cents(raw: object, reasons: list[str]) -> int | None:
    if not _is_plain_int(raw) or not 0 <= raw <= MAX_FEE_EFFECTIVE_CENTS:
        reasons.append("INVALID_FEE_EFFECTIVE_CENTS")
        return None
    return raw


def _validate_fee_model(raw: object, reasons: list[str]) -> str | None:
    if not isinstance(raw, str) or raw not in _FEE_MODELS:
        reasons.append("INVALID_FEE_MODEL")
        return None
    return raw


def _validate_fee_source(raw: object, reasons: list[str]) -> str | None:
    if not isinstance(raw, str) or raw not in _FEE_SOURCES:
        reasons.append("INVALID_FEE_SOURCE")
        return None
    return raw


def _validate_fee_type(raw: object, reasons: list[str]) -> str | None:
    if not isinstance(raw, str) or raw != FEE_TYPE:
        reasons.append("INVALID_FEE_TYPE")
        return None
    return raw


def _validate_metric_version(raw: object, reasons: list[str]) -> str | None:
    if not isinstance(raw, str) or raw != METRIC_VERSION:
        reasons.append("INVALID_METRIC_VERSION")
        return None
    return raw


def _validate_experiment_id(raw: object, reasons: list[str]) -> str | None:
    if not isinstance(raw, str) or not _EXPERIMENT_ID_RE.fullmatch(raw):
        reasons.append("INVALID_EXPERIMENT_ID")
        return None
    return raw


def _validate_ticker(raw: object, reasons: list[str]) -> str | None:
    if not isinstance(raw, str) or not _TICKER_RE.fullmatch(raw):
        reasons.append("INVALID_TICKER")
        return None
    return raw


def _validate_fee_multiplier(raw: object, reasons: list[str]) -> tuple[Fraction, str] | None:
    # bool is an int subclass in Python — isinstance(True, int) is True — so it must be
    # rejected explicitly before the (int, float, str) check would otherwise admit it.
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        reasons.append("INVALID_FEE_MULTIPLIER")
        return None
    text = str(raw)
    if len(text) > _MAX_FEE_MULTIPLIER_TEXT_LEN:
        reasons.append("INVALID_FEE_MULTIPLIER")
        return None
    try:
        # Fraction(str(value)), never a raw float division: exact conversion, and it
        # naturally rejects "nan"/"inf" text (Fraction raises ValueError on those),
        # which is what makes the "finite" requirement hold without a separate check.
        value = Fraction(text)
    except (ValueError, ZeroDivisionError):
        reasons.append("INVALID_FEE_MULTIPLIER")
        return None
    if value <= 0 or value > MAX_FEE_MULTIPLIER:
        reasons.append("INVALID_FEE_MULTIPLIER")
        return None
    return value, text


def _recompute_fee_cents(
    *, fee_model: str, price_cents: int, count: int, multiplier: Fraction
) -> tuple[int, int, str]:
    """Recompute the fee from first principles — never trust the declared value.

    Same official quadratic schedule as `src/math/fees.py::kalshi_fee_cents` /
    `kalshi_maker_fee_cents` (taker denominator 10_000, maker denominator
    40_000 — maker is 1/4 the taker rate), reimplemented here rather than
    imported so this module keeps zero non-stdlib dependencies.
    """
    base_denominator = _TAKER_BASE_DENOMINATOR if fee_model == "taker" else _MAKER_BASE_DENOMINATOR
    numerator = 7 * count * price_cents * (100 - price_cents) * multiplier.numerator
    denominator = base_denominator * multiplier.denominator
    fee_cents = (numerator + denominator - 1) // denominator
    formula = (
        f"{fee_model}: fee_cents = ceil(7*count*price_cents*(100-price_cents)"
        f"*fee_multiplier/{base_denominator})"
    )
    return fee_cents, base_denominator, formula


def _max_loss_cents(*, side: str, price_cents: int, count: int, fee_effective_cents: int) -> tuple[int, str]:
    if side == "buy":
        return (
            price_cents * count + fee_effective_cents,
            "buy: max_loss_cents = price_cents*count + fee_effective_cents",
        )
    return (
        (100 - price_cents) * count + fee_effective_cents,
        "sell: max_loss_cents = (100-price_cents)*count + fee_effective_cents",
    )


def _usd(cents: int) -> str:
    whole, frac = divmod(cents, 100)
    return f"{whole}.{frac:02d}"


def _build_result(
    *,
    status: str,
    reason_codes: list[str],
    now: datetime,
    fill_id: int | None = None,
    experiment_id: str | None = None,
    max_loss_usd: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Derived from BOTH experiment_id and fill_id together — see module docstring for
    # why neither alone is sufficient and why this must never become a collector packet_id.
    have_identity = fill_id is not None and experiment_id is not None
    cycle_id = f"m5-shadow-fill-{experiment_id}-{fill_id}" if have_identity else None
    idempotency_key = f"m5-shadow-fill-risk-{experiment_id}-{fill_id}" if have_identity else None
    return {
        "schema": SCHEMA_VERSION_OUTPUT,
        "status": status,
        "reason_codes": reason_codes,
        "origin": ORIGIN,
        "mode": "SIMULATION_ONLY",
        "capital_source": "FICTIONAL_TEST_CAPITAL",
        "evidence_state": "DECLARED_FILL_NOT_LEDGER_VERIFIED",
        "reviewed_at": now.isoformat(),
        "fill_id": fill_id,
        "cycle_id": cycle_id,
        "idempotency_key": idempotency_key,
        "max_loss_usd": max_loss_usd,
        "evidence": evidence,
        "execution_authorized": False,
        "order_capability_present": False,
        "real_entry_eligible": False,
    }


def review_m5_fill(raw: object, *, now: datetime | None = None) -> dict[str, Any]:
    """Validate a declared M5 shadow fill and compute its exact worst-case loss.

    Returns a structured diagnostic dict, never a reservation. See module
    docstring for scope: fail-closed on any missing/malformed/ambiguous/extra
    field or on a declared fee that does not reconcile with the recomputed
    fee, never a silent default. `evidence`/`max_loss_usd`/`cycle_id`/
    `idempotency_key` are populated only when `status == "ACCEPTED"`.
    """
    resolved_now = _resolve_now(now)

    if not isinstance(raw, dict):
        return _build_result(status="BLOCKED", reason_codes=["INVALID_INPUT_SHAPE"], now=resolved_now)

    reason_codes: list[str] = []
    if set(raw) - _ROOT_FIELDS:
        reason_codes.append("UNEXPECTED_INPUT_FIELDS")
    if raw.get("schema_version") != SCHEMA_VERSION_INPUT:
        reason_codes.append("INVALID_SCHEMA_VERSION")

    fill = raw.get("fill")
    if not isinstance(fill, dict):
        reason_codes.append("INVALID_FILL_SHAPE")
        return _build_result(status="BLOCKED", reason_codes=reason_codes, now=resolved_now)
    if not _REQUIRED_FILL_FIELDS <= set(fill):
        reason_codes.append("MISSING_FILL_FIELDS")
    if set(fill) - _ALLOWED_FILL_FIELDS:
        reason_codes.append("UNEXPECTED_FILL_FIELDS")

    fill_id = _validate_fill_id(fill.get("id"), reason_codes)
    side = _validate_side(fill.get("side"), reason_codes)
    price_cents = _validate_price_cents(fill.get("price_cents"), reason_codes)
    count = _validate_count(fill.get("count"), reason_codes)
    fee_effective_cents = _validate_fee_effective_cents(fill.get("fee_effective_cents"), reason_codes)
    fee_model = _validate_fee_model(fill.get("fee_model"), reason_codes)
    ticker = _validate_ticker(fill.get("ticker"), reason_codes)
    experiment_id = _validate_experiment_id(fill.get("experiment_id"), reason_codes)
    metric_version = _validate_metric_version(fill.get("metric_version"), reason_codes)
    fee_source = _validate_fee_source(fill.get("fee_source"), reason_codes)
    fee_type = _validate_fee_type(fill.get("fee_type"), reason_codes)
    fee_multiplier_parsed = _validate_fee_multiplier(fill.get("fee_multiplier"), reason_codes)

    if reason_codes:
        return _build_result(
            status="BLOCKED", reason_codes=reason_codes, now=resolved_now, fill_id=fill_id, experiment_id=experiment_id
        )

    fee_multiplier_value, fee_multiplier_text = fee_multiplier_parsed
    recomputed_fee_cents, base_denominator, fee_formula = _recompute_fee_cents(
        fee_model=fee_model, price_cents=price_cents, count=count, multiplier=fee_multiplier_value
    )
    if fee_effective_cents != recomputed_fee_cents or fee_effective_cents <= 0:
        return _build_result(
            status="BLOCKED",
            reason_codes=["FEE_EFFECTIVE_CENTS_INVALID"],
            now=resolved_now,
            fill_id=fill_id,
            experiment_id=experiment_id,
        )

    max_loss_cents, formula = _max_loss_cents(
        side=side, price_cents=price_cents, count=count, fee_effective_cents=fee_effective_cents
    )
    evidence = {
        "exactness": "EXACT_CONDITIONAL_ON_VALIDATED_DECLARED_EVIDENCE",
        "formula": formula,
        "fill_id": fill_id,
        "side": side,
        "price_cents": price_cents,
        "count": count,
        "fee_effective_cents": fee_effective_cents,
        "fee_model": fee_model,
        "fee_source": fee_source,
        "fee_type": fee_type,
        "fee_multiplier": fee_multiplier_text,
        "experiment_id": experiment_id,
        "metric_version": metric_version,
        "ticker": ticker,
        "fee_formula_provenance": {
            "recomputed_fee_cents": recomputed_fee_cents,
            "base_denominator": base_denominator,
            "formula": fee_formula,
        },
    }
    return _build_result(
        status="ACCEPTED",
        reason_codes=[],
        now=resolved_now,
        fill_id=fill_id,
        experiment_id=experiment_id,
        max_loss_usd=_usd(max_loss_cents),
        evidence=evidence,
    )
