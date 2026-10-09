"""Ingresos SELL observados: validación pura, sin clientes, DB ni órdenes.

Contrato público consultado el 2026-10-08:
https://docs.kalshi.com/api-reference/portfolio/get-fills
https://docs.kalshi.com/api-reference/historical/get-historical-fills
https://docs.kalshi.com/getting_started/order_direction
https://docs.kalshi.com/getting_started/fee_rounding

El llamador debe agotar ambas colecciones y sus cursores, filtrar únicamente por
order_id y confirmar por separado que la orden es terminal. ``complete=True``
atestigua esa paginación; este módulo no puede verificar una petición de red.
Histórico no admite filtro order_id. Ausencia, HTTP404 o cursor faltante no son
una colección completa. La cantidad terminal debe proceder de conciliación.

``outcome_side`` es dirección, no el contrato vendido: SELL YES -> no/ask;
SELL NO -> yes/bid. El ingreso usa el precio explícito del contrato vendido.
Nunca se interpreta ``fill_cost`` de una orden SELL como ingresos. ``fee_cost``
es el importe del fill y se suma una vez, sin multiplicarlo por su cantidad.
"""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

_COUNT = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,2})?\Z", re.ASCII)
_USD = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,6})?\Z", re.ASCII)
_MAX_ROWS = 10_000
_MAX_ROW_BYTES = 16_384
_MAX_QUANTITY = 1_000_000_000


class ExitFillError(ValueError):
    """Datos insuficientes o contradictorios; el mensaje nunca contiene la fila."""


@dataclass(frozen=True)
class ExitFillSummary:
    order_id: str
    ticker: str
    side: str
    subaccount: int
    count: Fraction
    gross_usd: Fraction
    fee_usd: Fraction
    fill_ids: tuple[str, ...]
    evidence_sha256: str


def _identifier(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or value.strip() != value
        or not value.isprintable()
    ):
        raise ExitFillError("INVALID_IDENTITY")
    return value


def _decimal(value: Any, pattern: re.Pattern[str], code: str) -> Fraction:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ExitFillError(code)
    return Fraction(value)


def _canonical(row: Mapping[str, Any]) -> bytes:
    # Comparar también campos extra impide deduplicar observaciones diferentes.
    try:
        serialized = json.dumps(
            dict(row), sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ExitFillError("INVALID_FILL_RECORD") from None
    if len(serialized) > _MAX_ROW_BYTES:
        raise ExitFillError("FILL_RECORD_TOO_LARGE")
    return serialized


def _direction(row: Mapping[str, Any], side: str) -> None:
    expected_outcome, expected_book = ("no", "ask") if side == "yes" else ("yes", "bid")
    for key, expected in (
        ("side", side),
        ("action", "sell"),
        ("outcome_side", expected_outcome),
        ("book_side", expected_book),
    ):
        if key in row and row[key] != expected:
            raise ExitFillError("FILL_DIRECTION_MISMATCH")
    # Se admite el formato legado completo, o cualquier dirección canónica.
    if not ("outcome_side" in row or "book_side" in row or ("side" in row and "action" in row)):
        raise ExitFillError("MISSING_FILL_DIRECTION")


def summarize_exit_fills(
    rows: Sequence[Mapping[str, Any]],
    *,
    order_id: str,
    ticker: str,
    side: str,
    quantity: Fraction,
    subaccount: int = 0,
    complete: bool = False,
) -> ExitFillSummary:
    """Resume exclusivamente fills SELL de una intención previamente identificada.

    Requiere subaccount_number explícito (0..63), precio USD del lado vendido,
    count_fp y fee_cost. Campos legado/canónicos presentes deben concordar.
    Duplicados con igual fill_id sólo se deduplican si sus registros JSON son
    idénticos (el orden de las claves no importa); todo conflicto rechaza todo.
    El hash identifica evidencia normalizada, no autentica al proveedor. Cantidad
    y dinero son Fraction. Se rechazan productos inferiores a la precisión
    microUSD documentada para ingresos por fill; nunca se redondea en silencio.
    """
    if complete is not True:
        raise ExitFillError("INCOMPLETE_FILL_COLLECTION")
    _identifier(order_id)
    _identifier(ticker)
    if side not in ("yes", "no"):
        raise ExitFillError("INVALID_EXIT_SIDE")
    if type(subaccount) is not int or not 0 <= subaccount <= 63:
        raise ExitFillError("INVALID_SUBACCOUNT")
    if (
        isinstance(quantity, bool)
        or not isinstance(quantity, (int, Fraction))
        or not 0 <= quantity <= _MAX_QUANTITY
        or (quantity * 100).denominator != 1
    ):
        raise ExitFillError("INVALID_TERMINAL_QUANTITY")
    quantity = Fraction(quantity)
    if not isinstance(rows, (list, tuple)) or len(rows) > _MAX_ROWS:
        raise ExitFillError("INVALID_FILL_COLLECTION")

    seen: dict[str, bytes] = {}
    evidence: dict[str, dict[str, Any]] = {}
    shards: set[int] = set()
    total = gross = fees = Fraction(0)
    for row in rows:
        if not isinstance(row, Mapping):
            raise ExitFillError("INVALID_FILL_RECORD")
        fill_id = _identifier(row.get("fill_id"))
        if "trade_id" in row and row["trade_id"] != fill_id:
            raise ExitFillError("FILL_ID_ALIAS_MISMATCH")
        if row.get("order_id") != order_id or row.get("ticker") != ticker:
            raise ExitFillError("FILL_IDENTITY_MISMATCH")
        if "market_ticker" in row and row["market_ticker"] != ticker:
            raise ExitFillError("FILL_TICKER_ALIAS_MISMATCH")
        if type(row.get("subaccount_number")) is not int or row["subaccount_number"] != subaccount:
            raise ExitFillError("FILL_SUBACCOUNT_MISMATCH")
        if "exchange_index" in row:
            shard = row["exchange_index"]
            if type(shard) is not int or not 0 <= shard <= 2**31 - 1:
                raise ExitFillError("INVALID_EXCHANGE_INDEX")
            shards.add(shard)
            if len(shards) > 1:
                raise ExitFillError("FILL_EXCHANGE_MISMATCH")
        _direction(row, side)
        count = _decimal(row.get("count_fp"), _COUNT, "INVALID_FILL_QUANTITY")
        if not 0 < count <= _MAX_QUANTITY:
            raise ExitFillError("INVALID_FILL_QUANTITY")
        if "count" in row and (type(row["count"]) is not int or row["count"] != count):
            raise ExitFillError("FILL_COUNT_ALIAS_MISMATCH")
        price = _decimal(row.get(f"{side}_price_dollars"), _USD, "INVALID_FILL_PRICE")
        if not 0 <= price <= 1:
            raise ExitFillError("INVALID_FILL_PRICE")
        other = "no" if side == "yes" else "yes"
        if f"{other}_price_dollars" in row:
            complement = _decimal(row[f"{other}_price_dollars"], _USD, "INVALID_FILL_PRICE")
            if price + complement != 1:
                raise ExitFillError("FILL_PRICE_COMPLEMENT_MISMATCH")
        for leg in ("yes", "no"):
            if f"{leg}_price" in row:
                legacy = row[f"{leg}_price"]
                expected = price if leg == side else 1 - price
                if type(legacy) is not int or legacy != expected * 100:
                    raise ExitFillError("FILL_PRICE_ALIAS_MISMATCH")
        fee = _decimal(row.get("fee_cost"), _USD, "INVALID_FILL_FEE")
        if fee > count:
            raise ExitFillError("INVALID_FILL_FEE")
        revenue = count * price
        if (revenue * 1_000_000).denominator != 1:
            raise ExitFillError("UNSUPPORTED_FILL_REVENUE_PRECISION")
        fingerprint = _canonical(row)
        if fill_id in seen:
            if fingerprint != seen[fill_id]:
                raise ExitFillError("CONFLICTING_DUPLICATE_FILL")
            continue
        seen[fill_id] = fingerprint
        evidence[fill_id] = {
            "fill_id": fill_id,
            "count": str(count),
            "price_usd": str(price),
            "fee_usd": str(fee),
            "exchange_index": row.get("exchange_index"),
        }
        total += count
        gross += revenue
        fees += fee
    if total != quantity:
        raise ExitFillError("TERMINAL_FILL_QUANTITY_MISMATCH")
    fill_ids = tuple(sorted(seen))
    # Sólo identidad y valores públicos validados; jamás extras opacos del payload.
    digest_data = {
        "schema": "kalshi-exit-fills-v1",
        "order_id": order_id,
        "ticker": ticker,
        "side": side,
        "action": "sell",
        "subaccount": subaccount,
        "quantity": str(quantity),
        "fills": [evidence[fill_id] for fill_id in fill_ids],
    }
    digest = hashlib.sha256(
        json.dumps(digest_data, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()
    return ExitFillSummary(order_id, ticker, side, subaccount, total, gross, fees, fill_ids, digest)
