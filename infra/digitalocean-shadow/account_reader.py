"""Read-only account snapshot builder.

This module accepts an authenticated client but only calls balance, positions,
and fills GET methods.  Its output is evidence for later reconciliation, never
reconciliation or trading authority by itself.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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


def _sanitize_position(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AccountReadError("position row invalid")
    ticker = _safe_id(raw.get("ticker") or raw.get("market_ticker"), required=True)
    return {
        "ticker": ticker,
        "position": _safe_number(raw.get("position"), field="position"),
        "market_exposure_cents": _safe_number(raw.get("market_exposure"), field="market_exposure"),
        "realized_pnl_cents": _safe_number(raw.get("realized_pnl"), field="realized_pnl"),
        "fees_paid_cents": _safe_number(raw.get("fees_paid"), field="fees_paid"),
    }


def _sanitize_fill(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise AccountReadError("fill row invalid")
    ticker = _safe_id(raw.get("ticker") or raw.get("market_ticker"), required=True)
    trade_id = _safe_id(raw.get("trade_id") or raw.get("fill_id"), required=True)
    if "price" in raw:
        price = _safe_number(raw.get("price"), field="price")
    elif "yes_price" in raw:
        price = _safe_number(raw.get("yes_price"), field="yes_price")
    else:
        price = None
    return {
        "trade_id": trade_id,
        "ticker": ticker,
        "order_id": _safe_id(raw.get("order_id"), required=False),
        "side": raw.get("side") if raw.get("side") in {"yes", "no", "bid", "ask"} else None,
        "action": raw.get("action") if raw.get("action") in {"buy", "sell"} else None,
        "count": _safe_number(raw.get("count"), field="count"),
        "price": price,
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
