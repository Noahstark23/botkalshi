"""Read-only account snapshot builder.

This module accepts an authenticated client but only calls balance, positions,
and fills GET methods.  Its output is evidence for later reconciliation, never
reconciliation or trading authority by itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext
import json
import os
from pathlib import Path
import re
from typing import Any


class AccountReadError(RuntimeError):
    """Account evidence could not be collected completely."""


def _cents(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise AccountReadError(f"{field} invalid")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise AccountReadError(f"{field} invalid") from None
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise AccountReadError(f"{field} invalid")
    return int(amount)


def _safe_id(value: object, *, required: bool = False) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./:-]{1,160}", value):
        return value
    if required:
        raise AccountReadError("provider identifier invalid")
    return None


_MAX_NUMBER_CHARS = 32
_MAX_NUMBER_EXPONENT = 15
_MAX_INT_MAGNITUDE = 10**_MAX_NUMBER_CHARS
_NUMBER_PATTERN = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")


def _safe_number(value: object, *, field: str) -> str | None:
    """Return a sanitized decimal string, or None only when value is absent (unknown)."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise AccountReadError(f"{field} invalid")
    if isinstance(value, int):
        if abs(value) >= _MAX_INT_MAGNITUDE:
            raise AccountReadError(f"{field} invalid")
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        raise AccountReadError(f"{field} invalid")
    if not text or len(text) > _MAX_NUMBER_CHARS:
        raise AccountReadError(f"{field} invalid")
    if not _NUMBER_PATTERN.fullmatch(text):
        raise AccountReadError(f"{field} invalid")
    try:
        number = Decimal(text)
    except (InvalidOperation, TypeError, ValueError):
        raise AccountReadError(f"{field} invalid") from None
    if not number.is_finite():
        raise AccountReadError(f"{field} invalid")
    if number != 0 and not (-_MAX_NUMBER_EXPONENT <= number.adjusted() <= _MAX_NUMBER_EXPONENT):
        raise AccountReadError(f"{field} invalid")
    return format(number, "f")


def _shift_decimal(text: str, exponent: int) -> str:
    """Multiply a sanitized decimal string by 10**exponent without rounding.

    Used only for exact cents<->dollars conversions (exponent +-2). scaleb
    only moves the exponent; it is the local context's prec=60 (ample for
    the <=32-char bounded input) that guarantees no coefficient digit is
    rounded away, regardless of any caller's ambient global context.
    """
    with localcontext() as ctx:
        ctx.prec = 60
        value = Decimal(text).scaleb(exponent)
    return format(value, "f")


def _effective_decimal_places(text: str) -> int:
    """Count decimal places after stripping value-preserving trailing zeros.

    Uses a local high-precision context (not the caller's global context)
    so the count never depends on ambient rounding.
    """
    with localcontext() as ctx:
        ctx.prec = 60
        normalized = Decimal(text).normalize()
    exponent = normalized.as_tuple().exponent
    return max(0, -exponent)


def _check_max_effective_decimals(text: str, max_decimals: int, *, field: str) -> None:
    if _effective_decimal_places(text) > max_decimals:
        raise AccountReadError(f"{field} invalid")


def _resolve_label(value: object, allowed: set[str], *, field: str) -> str | None:
    """Resolve an enum-like string field without ever raising on containment checks.

    Absence/None stays UNKNOWN (None). A non-string present value (list,
    dict, int, bool, ...) is explicitly malformed and rejects the row. An
    unrecognized string stays UNKNOWN (None) and is never treated as a
    match against any allowed label.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise AccountReadError(f"{field} invalid")
    return value if value in allowed else None


def _resolve_same_unit(
    raw: dict[str, Any],
    *,
    legacy_key: str,
    canonical_key: str,
    field: str,
    non_negative: bool = False,
    max_decimals: int | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a legacy field and its same-unit fixed-point canonical twin.

    Precedence: if the canonical key is present in the row, it always wins
    (an explicit null means UNKNOWN and never falls back to legacy). If both
    are present with values, they must agree numerically or the row is
    rejected. A malformed legacy value is never silently dropped. When
    max_decimals is set, the canonical value's effective decimal places
    (after stripping value-preserving trailing zeros) must not exceed it;
    the legacy twin is never precision-restricted.
    Returns (legacy_field_value, canonical_field_value).
    """
    canonical_present = canonical_key in raw
    legacy_present = legacy_key in raw
    canonical_value = _safe_number(raw.get(canonical_key), field=canonical_key) if canonical_present else None
    legacy_value = _safe_number(raw.get(legacy_key), field=legacy_key) if legacy_present else None
    if non_negative:
        if canonical_value is not None and Decimal(canonical_value) < 0:
            raise AccountReadError(f"{canonical_key} invalid")
        if legacy_value is not None and Decimal(legacy_value) < 0:
            raise AccountReadError(f"{legacy_key} invalid")
    if max_decimals is not None and canonical_value is not None:
        _check_max_effective_decimals(canonical_value, max_decimals, field=canonical_key)
    if canonical_present:
        if canonical_value is None:
            return None, None
        if legacy_present and legacy_value is not None and Decimal(legacy_value) != Decimal(canonical_value):
            raise AccountReadError(f"{field} conflict")
        return canonical_value, canonical_value
    return legacy_value, None


def _resolve_cents_dollars(
    raw: dict[str, Any],
    *,
    cents_key: str,
    dollars_key: str,
    field: str,
    price_range: bool = False,
    max_decimals: int | None = None,
) -> tuple[str | None, str | None]:
    """Resolve a legacy cents field against its canonical dollars twin.

    The dollars field, when present, is authoritative; the cents field
    returned is always derived from it EXACTLY (scaleb, no rounding). When
    only the legacy cents field is present it is preserved verbatim and
    dollars are derived exactly from it. An explicit null canonical value
    means UNKNOWN (no legacy fallback). Mismatched values between the two
    formats reject the row instead of picking one silently. When
    max_decimals is set, the resulting dollars value's effective decimal
    places (after stripping value-preserving trailing zeros) must not
    exceed it; callers that need unrestricted monetary precision (e.g.
    aggregate exposure/P&L/fees) simply omit max_decimals.
    Returns (cents_field_value, dollars_field_value).
    """
    dollars_present = dollars_key in raw
    cents_present = cents_key in raw
    dollars_value = _safe_number(raw.get(dollars_key), field=dollars_key) if dollars_present else None
    cents_value = _safe_number(raw.get(cents_key), field=cents_key) if cents_present else None

    def _check_range(amount: Decimal, *, source: str) -> None:
        if price_range and not (Decimal("0") <= amount <= Decimal("1")):
            raise AccountReadError(f"{source} invalid")

    if dollars_present:
        if dollars_value is None:
            return None, None
        dollars_decimal = Decimal(dollars_value)
        _check_range(dollars_decimal, source=dollars_key)
        if max_decimals is not None:
            _check_max_effective_decimals(dollars_value, max_decimals, field=dollars_key)
        if cents_present and cents_value is not None:
            derived_dollars = Decimal(_shift_decimal(cents_value, -2))
            if derived_dollars != dollars_decimal:
                raise AccountReadError(f"{field} conflict")
        return _shift_decimal(dollars_value, 2), dollars_value
    if cents_present and cents_value is not None:
        derived_dollars = _shift_decimal(cents_value, -2)
        _check_range(Decimal(derived_dollars), source=cents_key)
        if max_decimals is not None:
            _check_max_effective_decimals(derived_dollars, max_decimals, field=dollars_key)
        return cents_value, derived_dollars
    return None, None


def _sanitize_position(raw: object) -> dict[str, Any]:
    """Sanitize one positions row.

    Adds canonical fields (position_fp, market_exposure_dollars,
    realized_pnl_dollars, fees_paid_dollars) alongside the legacy
    position/*_cents fields. Legacy *_cents values are exact dollars*100
    derivations (Decimal.scaleb, no quantize) whenever a canonical dollars
    value is present; when only the legacy cents value exists it is kept
    verbatim and dollars are derived from it exactly. position_fp accepts
    at most 2 effective decimal places (value-preserving trailing zeros,
    e.g. "1.5000", do not count against the limit). The monetary aggregates
    (exposure/P&L/fees) are deliberately left unrestricted: this module is
    a read-only evidence collector, not a tick-size validator.
    """
    if not isinstance(raw, dict):
        raise AccountReadError("position row invalid")
    ticker = _safe_id(raw.get("ticker") or raw.get("market_ticker"), required=True)
    position, position_fp = _resolve_same_unit(
        raw, legacy_key="position", canonical_key="position_fp", field="position", max_decimals=2
    )
    market_exposure_cents, market_exposure_dollars = _resolve_cents_dollars(
        raw, cents_key="market_exposure", dollars_key="market_exposure_dollars", field="market_exposure"
    )
    realized_pnl_cents, realized_pnl_dollars = _resolve_cents_dollars(
        raw, cents_key="realized_pnl", dollars_key="realized_pnl_dollars", field="realized_pnl"
    )
    fees_paid_cents, fees_paid_dollars = _resolve_cents_dollars(
        raw, cents_key="fees_paid", dollars_key="fees_paid_dollars", field="fees_paid"
    )
    return {
        "ticker": ticker,
        "position": position,
        "position_fp": position_fp,
        "market_exposure_cents": market_exposure_cents,
        "market_exposure_dollars": market_exposure_dollars,
        "realized_pnl_cents": realized_pnl_cents,
        "realized_pnl_dollars": realized_pnl_dollars,
        "fees_paid_cents": fees_paid_cents,
        "fees_paid_dollars": fees_paid_dollars,
    }


def _sanitize_fill(raw: object) -> dict[str, Any]:
    """Sanitize one fills row.

    The legacy generic "price" field keeps its exact historical semantics
    (price=0 prevails over yes_price, explicit null stays unknown, absence
    falls back to legacy yes_price) because its unit is not defined by this
    module and must not be reinterpreted. New consumers should read
    price_dollars instead, which is selected ONLY from yes_price_dollars or
    no_price_dollars based on side being exactly "yes" or "no"; an unknown
    side yields an unknown (None) price_dollars, never a guessed value.
    fill_id is added alongside trade_id (kept as-is for compatibility, both
    preserved separately); a present, non-null, malformed fill_id rejects
    the row rather than being silently dropped. side/action are UNKNOWN
    (None) when absent, None, or an unrecognized string, but a present
    non-string value (list, dict, int, bool, ...) explicitly rejects the
    row instead of risking a TypeError on set containment. count_fp/
    yes_price_dollars/no_price_dollars/fee_cost_dollars are added as
    canonical fixed-point/dollar fields alongside their legacy twins.
    count_fp accepts at most 2 effective decimal places and
    yes_price_dollars/no_price_dollars at most 4 (value-preserving
    trailing zeros do not count against either limit); this is a
    canonical-shape check, not a per-market tick-size validation, which
    would require price_ranges and is out of scope here.
    """
    if not isinstance(raw, dict):
        raise AccountReadError("fill row invalid")
    ticker = _safe_id(raw.get("ticker") or raw.get("market_ticker"), required=True)
    trade_id = _safe_id(raw.get("trade_id") or raw.get("fill_id"), required=True)
    fill_id_raw = raw.get("fill_id")
    fill_id = _safe_id(fill_id_raw, required=True) if "fill_id" in raw and fill_id_raw is not None else None
    if "price" in raw:
        price = _safe_number(raw.get("price"), field="price")
    elif "yes_price" in raw:
        price = _safe_number(raw.get("yes_price"), field="yes_price")
    else:
        price = None
    side = _resolve_label(raw.get("side"), {"yes", "no", "bid", "ask"}, field="side")
    action = _resolve_label(raw.get("action"), {"buy", "sell"}, field="action")
    count, count_fp = _resolve_same_unit(
        raw, legacy_key="count", canonical_key="count_fp", field="count", non_negative=True, max_decimals=2
    )
    _, yes_price_dollars = _resolve_cents_dollars(
        raw,
        cents_key="yes_price",
        dollars_key="yes_price_dollars",
        field="yes_price",
        price_range=True,
        max_decimals=4,
    )
    _, no_price_dollars = _resolve_cents_dollars(
        raw,
        cents_key="no_price",
        dollars_key="no_price_dollars",
        field="no_price",
        price_range=True,
        max_decimals=4,
    )
    if side == "yes":
        price_dollars = yes_price_dollars
    elif side == "no":
        price_dollars = no_price_dollars
    else:
        price_dollars = None
    return {
        "trade_id": trade_id,
        "fill_id": fill_id,
        "ticker": ticker,
        "order_id": _safe_id(raw.get("order_id"), required=False),
        "side": side,
        "action": action,
        "count": count,
        "count_fp": count_fp,
        "price": price,
        "yes_price_dollars": yes_price_dollars,
        "no_price_dollars": no_price_dollars,
        "price_dollars": price_dollars,
        "fee_cost_dollars": _safe_number(raw.get("fee_cost"), field="fee_cost"),
        "created_time": raw.get("created_time") if isinstance(raw.get("created_time"), str) else None,
        "is_taker": raw.get("is_taker") if type(raw.get("is_taker")) is bool else None,
    }


async def _collect_pages(
    fetch,
    *,
    list_keys: tuple[str, ...],
    sanitizer,
    page_size: int,
    max_pages: int,
) -> list[dict[str, Any]]:
    cursor: str | None = None
    seen_cursors: set[str] = set()
    rows: list[dict[str, Any]] = []
    for _ in range(max_pages):
        body = await fetch(limit=page_size, cursor=cursor)
        if not isinstance(body, dict):
            raise AccountReadError("provider page invalid")
        source = None
        for key in list_keys:
            if isinstance(body.get(key), list):
                source = body[key]
                break
        if source is None:
            raise AccountReadError("provider rows missing")
        for item in source:
            rows.append(sanitizer(item))
        next_cursor = body.get("cursor")
        if next_cursor in (None, ""):
            return rows
        if not isinstance(next_cursor, str) or len(next_cursor) > 512:
            raise AccountReadError("provider cursor invalid")
        if next_cursor in seen_cursors:
            raise AccountReadError("provider cursor repeated")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise AccountReadError("account pagination truncated")


async def collect_account_snapshot(
    client,
    *,
    now: datetime | None = None,
    page_size: int = 100,
    max_pages: int = 50,
) -> dict[str, Any]:
    if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 200:
        raise AccountReadError("pagination limits invalid")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    balance_body = await client.get_balance()
    if not isinstance(balance_body, dict):
        raise AccountReadError("balance response invalid")
    balance_cents = _cents(balance_body.get("balance"), field="balance")
    positions = await _collect_pages(
        client.get_positions,
        list_keys=("market_positions", "positions"),
        sanitizer=_sanitize_position,
        page_size=page_size,
        max_pages=max_pages,
    )
    fills = await _collect_pages(
        client.get_fills,
        list_keys=("fills",),
        sanitizer=_sanitize_fill,
        page_size=page_size,
        max_pages=max_pages,
    )
    if len({row["ticker"] for row in positions}) != len(positions):
        raise AccountReadError("duplicate position ticker")
    if len({row["trade_id"] for row in fills}) != len(fills):
        raise AccountReadError("duplicate fill identifier")
    return {
        "schema_version": "botkalshi-account-snapshot-v1",
        "observed_at": now.isoformat(),
        "source": "kalshi-authenticated-readonly",
        "balance_usd": f"{Decimal(balance_cents) / Decimal(100):.2f}",
        "balance_updated_ts": (
            balance_body.get("updated_ts")
            if isinstance(balance_body.get("updated_ts"), str)
            else None
        ),
        "positions_count": len(positions),
        "fills_count": len(fills),
        "positions": positions,
        "fills": fills,
        "reconciled": False,
        "real_entry_eligible": False,
        "execution_authorized": False,
        "order_capability_present": False,
        "note": "Read-only evidence. Ledger reconciliation and authority remain separate.",
    }


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise AccountReadError("output path cannot be a symlink")
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
