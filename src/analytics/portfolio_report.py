"""Snapshot financiero readonly: Decimal, ámbito explícito y exhaustividad falsable.

Los totales de un recurso incompleto son None, jamás cero. No hay DB, estimación
de fees ni autorización de trading. Los fills recientes no certifican PnL histórico.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any


class ReportDataError(ValueError):
    """Error seguro: los mensajes no contienen valores ni respuestas de la API."""


def decimal_sum(values) -> Decimal:
    # Hasta 100k filas de montos acotados: no perder fracciones al crecer el agregado.
    with localcontext() as ctx:
        ctx.prec = 40
        return sum(values, Decimal(0))


def decimal_value(raw: object, field: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
        raise ReportDataError(f"{field}: valor ausente o inválido")
    if len(str(raw)) > 128:
        raise ReportDataError(f"{field}: tamaño inválido")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ReportDataError(f"{field}: número inválido") from exc
    if not value.is_finite() or abs(value) > Decimal("1e12"):
        raise ReportDataError(f"{field}: número fuera de rango")
    if value.as_tuple().exponent < -12 or (nonnegative and value < 0):
        raise ReportDataError(f"{field}: precisión o signo inválido")
    return value


def number(row: dict, *names: str, nonnegative: bool = False) -> Decimal:
    for name in names:
        if name in row:
            return decimal_value(row[name], name, nonnegative=nonnegative)
    raise ReportDataError(f"{names[0]}: campo ausente")


def dollars(row: dict, dollar_field: str, *cent_fields: str, nonnegative=False) -> Decimal:
    if dollar_field in row:
        return number(row, dollar_field, nonnegative=nonnegative)
    return number(row, *cent_fields, nonnegative=nonnegative) / Decimal(100)


def text_field(row: dict, name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ReportDataError(f"{name}: identificador inválido")
    if any(ord(c) < 32 for c in value):
        raise ReportDataError(f"{name}: identificador inválido")
    return value


def timestamp(raw: object, field: str) -> datetime:
    try:
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            dt = datetime.fromtimestamp(raw, UTC)
        elif isinstance(raw, str):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            raise ValueError
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(UTC)
    except (ValueError, TypeError, OverflowError, OSError) as exc:
        raise ReportDataError(f"{field}: timestamp inválido") from exc


def _balance(row: dict) -> dict:
    return {
        "cash_usd": dollars(row, "balance_dollars", "balance", nonnegative=True),
        "portfolio_value_usd": dollars(
            row, "portfolio_value_dollars", "portfolio_value", nonnegative=True
        ),
        "updated_at": timestamp(row.get("updated_ts"), "updated_ts").isoformat(),
    }


def _position(row: dict) -> dict:
    return {
        "ticker": text_field(row, "ticker"),
        "quantity": number(row, "position_fp", "position"),
        "market_exposure_usd": dollars(
            row,
            "market_exposure_dollars",
            "market_exposure",
            nonnegative=True,
        ),
    }


def _side(row: dict) -> str:
    side = row.get("outcome_side", row.get("side"))
    if side not in {"yes", "no"}:
        raise ReportDataError("side: estado desconocido")
    if "outcome_side" in row and "side" in row and row["side"] != side:
        raise ReportDataError("side: campos contradictorios")
    return side


def _price(row: dict, side: str) -> Decimal:
    price = dollars(row, f"{side}_price_dollars", f"{side}_price", nonnegative=True)
    if price > 1:
        raise ReportDataError("price: fuera del intervalo de contrato")
    return price


def _order(row: dict) -> dict:
    if row.get("status") != "resting":
        raise ReportDataError("status: orden no resting en consulta filtrada")
    side = _side(row)
    remaining = number(row, "remaining_count_fp", "remaining_count", nonnegative=True)
    if remaining <= 0:
        raise ReportDataError("remaining_count: resting sin cantidad pendiente")
    return {
        "order_id": text_field(row, "order_id"),
        "ticker": text_field(row, "ticker"),
        "side": side,
        "remaining_quantity": remaining,
        "price_usd": _price(row, side),
    }


def _fill(row: dict) -> dict:
    side = _side(row)
    count = number(row, "count_fp", "count", nonnegative=True)
    if count <= 0:
        raise ReportDataError("count: fill sin cantidad ejecutada")
    # fee_cost es el cargo TOTAL del fill en dólares; no se multiplica por count.
    fee = number(row, "fee_cost", nonnegative=True)
    return {
        "fill_id": text_field(row, "fill_id" if "fill_id" in row else "trade_id"),
        "order_id": text_field(row, "order_id"),
        "ticker": text_field(row, "ticker"),
        "side": side,
        "quantity": count,
        "price_usd": _price(row, side),
        "fee_usd": fee,
        "created_at": timestamp(row.get("created_time", row.get("ts")), "created_time").isoformat(),
    }


def _settlement(row: dict) -> dict:
    return {
        "ticker": text_field(row, "ticker"),
        "settled_at": timestamp(row.get("settled_time"), "settled_time").isoformat(),
        "revenue_usd": dollars(row, "revenue_dollars", "revenue", nonnegative=True),
    }


async def _pages(
    fetch: Callable[..., Awaitable[dict]],
    resource: str,
    normalize: Callable[[dict], dict],
    identity: Callable[[dict], object],
    *,
    limit: int,
    max_pages: int,
    params: dict,
    window: tuple[datetime, datetime] | None = None,
    time_field: str | None = None,
) -> dict:
    rows: list[dict] = []
    cursors: set[str] = set()
    identities: set[object] = set()
    cursor = None
    pages = 0
    try:
        for _ in range(max_pages):
            page = await fetch(limit=limit, cursor=cursor, **params)
            pages += 1
            if not isinstance(page, dict) or not isinstance(page.get(resource), list):
                raise ReportDataError(f"{resource}: estructura inválida")
            raw_rows = page[resource]
            if len(raw_rows) > limit:
                raise ReportDataError(f"{resource}: página mayor al límite solicitado")
            for raw in raw_rows:
                if not isinstance(raw, dict):
                    raise ReportDataError(f"{resource}: fila inválida")
                row = normalize(raw)
                identifier = identity(row)
                if identifier in identities:
                    raise ReportDataError(f"{resource}: identidad duplicada entre páginas")
                identities.add(identifier)
                if window and time_field:
                    dt = timestamp(row[time_field], time_field)
                    if not window[0] <= dt <= window[1]:
                        raise ReportDataError(f"{resource}: fila fuera de la ventana consultada")
                rows.append(row)
            next_cursor = page.get("cursor")
            if not isinstance(next_cursor, str):
                raise ReportDataError(f"{resource}: exhaustividad sin cursor terminal válido")
            if not next_cursor:
                return {"complete": True, "pages": pages, "rows": rows, "error": None}
            if len(next_cursor) > 4096 or next_cursor in cursors:
                raise ReportDataError(f"{resource}: cursor repetido o inválido")
            cursors.add(next_cursor)
            cursor = next_cursor
        raise ReportDataError(f"{resource}: presupuesto de páginas agotado")
    except ReportDataError as exc:
        error = str(exc)
    except Exception as exc:
        # No incluir str(exc): puede contener headers, cuerpos o material de auth.
        error = f"{resource}: consulta falló ({type(exc).__name__})"
    return {"complete": False, "pages": pages, "rows": rows, "error": error}


async def collect_portfolio(
    client: Any,
    *,
    environment: str,
    source_fingerprint: str,
    subaccount: int = 0,
    hours: int = 24,
    limit: int = 100,
    max_pages: int = 50,
    timeout_seconds: float = 60,
    now: datetime | None = None,
) -> dict:
    """Consulta acotada. 'OK' significa lectura válida, no permiso ni rentabilidad."""
    if environment not in {"production", "demo"}:
        raise ValueError("environment inválido")
    if not isinstance(source_fingerprint, str) or not re.fullmatch(
        r"[a-f0-9]{64}", source_fingerprint
    ):
        raise ValueError("source_fingerprint inválido")
    if not isinstance(subaccount, int) or isinstance(subaccount, bool) or not 0 <= subaccount <= 63:
        raise ValueError("subaccount inválido")
    if any(not isinstance(v, int) or isinstance(v, bool) for v in (hours, limit, max_pages)):
        raise ValueError("Presupuesto de consulta inválido")
    if not 1 <= hours <= 24 * 31 or not 1 <= limit <= 1000 or not 1 <= max_pages <= 100:
        raise ValueError("Presupuesto de consulta inválido")
    if not 0 < timeout_seconds <= 120:
        raise ValueError("Timeout inválido")
    end = now or datetime.now(UTC)
    if end.tzinfo is None:
        raise ValueError("now debe llevar timezone")
    # Los filtros de API usan segundos enteros; normalizar también la comprobación.
    end = end.astimezone(UTC).replace(microsecond=0)
    start = end - timedelta(hours=hours)
    scope = {"subaccount": subaccount}
    period = {**scope, "min_ts": int(start.timestamp()), "max_ts": int(end.timestamp())}
    report: dict = {
        "schema_version": 1,
        "read_only": True,
        "authorizes_trading": False,
        "scope": {
            "environment": environment,
            "subaccount": subaccount,
            "source_fingerprint": source_fingerprint,
            "exchange_indexes": "all",
        },
        "window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "historical_coverage": "recent_api_only",
        },
        "collection_started_at": datetime.now(UTC).isoformat(),
        "atomic_snapshot": False,
        "errors": [],
        "pnl_net_usd": None,
        "profit_status": "NO_EVALUADO",
    }
    resources = (
        (
            "positions",
            client.get_positions,
            "market_positions",
            _position,
            lambda r: r["ticker"],
            scope,
            None,
        ),
        (
            "orders",
            client.get_orders,
            "orders",
            _order,
            lambda r: r["order_id"],
            {**scope, "status": "resting"},
            None,
        ),
        ("fills", client.get_fills, "fills", _fill, lambda r: r["fill_id"], period, "created_at"),
        (
            "settlements",
            client.get_settlements,
            "settlements",
            _settlement,
            lambda r: (r["ticker"], r["settled_at"]),
            period,
            "settled_at",
        ),
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            first = _balance(await client.get_balance(**scope))
            report["balance"] = first
            for name, fetch, key, parser, identity, params, time_field in resources:
                result = await _pages(
                    fetch,
                    key,
                    parser,
                    identity,
                    limit=limit,
                    max_pages=max_pages,
                    params=params,
                    window=(start, end) if time_field else None,
                    time_field=time_field,
                )
                report[name] = result
                if result["error"]:
                    report["errors"].append(result["error"])
            last = _balance(await client.get_balance(**scope))
            report["balance_after"] = last
            report["balance_unchanged"] = first == last
            if first != last:
                report["errors"].append("balance: cambió durante la lectura; repetir diagnóstico")
    except ReportDataError as exc:
        report["errors"].append(str(exc))
    except Exception as exc:
        report["errors"].append(f"colección: falló ({type(exc).__name__})")
    report["collection_finished_at"] = datetime.now(UTC).isoformat()
    for name, *_ in resources:
        report.setdefault(
            name, {"complete": False, "pages": 0, "rows": [], "error": "recurso no evaluado"}
        )
    p, o, f, s = (report[name] for name in ("positions", "orders", "fills", "settlements"))
    report["totals"] = {
        "nonzero_positions": sum(r["quantity"] != 0 for r in p["rows"]) if p["complete"] else None,
        "market_exposure_usd": decimal_sum(r["market_exposure_usd"] for r in p["rows"])
        if p["complete"]
        else None,
        "resting_orders": len(o["rows"]) if o["complete"] else None,
        "fill_fees_usd": decimal_sum(r["fee_usd"] for r in f["rows"]) if f["complete"] else None,
        "settlement_revenue_usd": decimal_sum(r["revenue_usd"] for r in s["rows"])
        if s["complete"]
        else None,
    }
    report["status"] = (
        "OK"
        if not report["errors"] and all(report[name]["complete"] for name, *_ in resources)
        else "ATENCION"
    )
    return report


def json_safe(value: Any) -> Any:
    """Decimal se serializa como string exacto; nunca pasa por float."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def validate_complete_snapshot(report: dict) -> None:
    """Revalidar un export antes de mostrar OK o usarlo en un cálculo financiero."""
    try:
        if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
            raise ReportDataError("snapshot: esquema desconocido")
        if (
            report.get("status") != "OK"
            or report.get("errors") != []
            or report.get("read_only") is not True
            or report.get("authorizes_trading") is not False
            or report.get("balance_unchanged") is not True
        ):
            raise ReportDataError("snapshot: lectura incompleta o cambiante")
        scope = report["scope"]
        subaccount = scope["subaccount"]
        if (
            scope.get("environment") not in {"production", "demo"}
            or type(subaccount) is not int
            or not 0 <= subaccount <= 63
            or scope.get("exchange_indexes") != "all"
            or not re.fullmatch(r"[a-f0-9]{64}", scope.get("source_fingerprint", ""))
        ):
            raise ReportDataError("snapshot: ámbito desconocido")
        balances = []
        for name in ("balance", "balance_after"):
            row = report[name]
            balances.append(
                (
                    decimal_value(row.get("cash_usd"), "cash_usd", nonnegative=True),
                    decimal_value(
                        row.get("portfolio_value_usd"), "portfolio_value_usd", nonnegative=True
                    ),
                    timestamp(row.get("updated_at"), "updated_at"),
                )
            )
        if balances[0] != balances[1]:
            raise ReportDataError("snapshot: saldos contradictorios")
        rows = {}
        for name in ("positions", "orders", "fills", "settlements"):
            resource = report[name]
            if (
                resource.get("complete") is not True
                or resource.get("error") is not None
                or type(resource.get("pages")) is not int
                or not 1 <= resource["pages"] <= 100
                or not isinstance(resource.get("rows"), list)
                or len(resource["rows"]) > 100_000
            ):
                raise ReportDataError("snapshot: recurso incompleto")
            rows[name] = resource["rows"]
        quantities = [decimal_value(r.get("quantity"), "quantity") for r in rows["positions"]]
        exposures = [
            decimal_value(r.get("market_exposure_usd"), "exposure", nonnegative=True)
            for r in rows["positions"]
        ]
        for row in rows["orders"]:
            if (
                decimal_value(row.get("remaining_quantity"), "remaining_quantity", nonnegative=True)
                <= 0
            ):
                raise ReportDataError("snapshot: resting sin cantidad")
        fees = [decimal_value(r.get("fee_usd"), "fee_usd", nonnegative=True) for r in rows["fills"]]
        revenue = [
            decimal_value(r.get("revenue_usd"), "revenue_usd", nonnegative=True)
            for r in rows["settlements"]
        ]
        expected = {
            "nonzero_positions": sum(q != 0 for q in quantities),
            "market_exposure_usd": decimal_sum(exposures),
            "resting_orders": len(rows["orders"]),
            "fill_fees_usd": decimal_sum(fees),
            "settlement_revenue_usd": decimal_sum(revenue),
        }
        for name, value in expected.items():
            if decimal_value(report["totals"].get(name), name, nonnegative=True) != value:
                raise ReportDataError("snapshot: totales contradicen filas")
    except (KeyError, AttributeError, TypeError) as exc:
        raise ReportDataError("snapshot: estructura inválida") from exc
