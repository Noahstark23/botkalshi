"""Resultados de períodos con cartera vacía en ambos extremos, separados de aportes.

Usa snapshots del lector y un registro de flujos conciliado por el operador. No
inventa valuaciones para posiciones abiertas, ni deduce una ganancia desde fills.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

from src.analytics.portfolio_report import (
    ReportDataError,
    decimal_value,
    timestamp,
    validate_complete_snapshot,
)


def _cash_snapshot(snapshot: dict) -> tuple[Decimal, dict]:
    validate_complete_snapshot(snapshot)
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != 1:
        raise ReportDataError("snapshot: esquema desconocido")
    if snapshot.get("status") != "OK" or snapshot.get("balance_unchanged") is not True:
        raise ReportDataError("snapshot: lectura incompleta o cambiante")
    if snapshot.get("read_only") is not True or snapshot.get("authorizes_trading") is not False:
        raise ReportDataError("snapshot: origen no readonly")
    scope = snapshot.get("scope")
    if not isinstance(scope, dict) or not scope.get("source_fingerprint"):
        raise ReportDataError("snapshot: ámbito desconocido")
    for name in ("positions", "orders", "fills", "settlements"):
        if snapshot.get(name, {}).get("complete") is not True:
            raise ReportDataError("snapshot: consultas incompletas")
    totals = snapshot.get("totals", {})
    for name in ("nonzero_positions", "market_exposure_usd", "resting_orders"):
        if decimal_value(totals.get(name), name, nonnegative=True) != 0:
            raise ReportDataError("snapshot: hay exposición; requiere valuación independiente")
    balance = snapshot.get("balance", {})
    after = snapshot.get("balance_after", {})
    for row in (balance, after):
        if decimal_value(row.get("portfolio_value_usd"), "portfolio_value", nonnegative=True) != 0:
            raise ReportDataError(
                "snapshot: valor pendiente; no se puede usar cash como patrimonio"
            )
    cash = decimal_value(balance.get("cash_usd"), "cash", nonnegative=True)
    if cash != decimal_value(after.get("cash_usd"), "cash_after", nonnegative=True):
        raise ReportDataError("snapshot: saldo cambiante")
    # Revalidar filas: un resumen alterado o inconsistente no certifica cartera vacía.
    for row in snapshot["positions"]["rows"]:
        if (
            decimal_value(row.get("quantity"), "quantity") != 0
            or decimal_value(row.get("market_exposure_usd"), "exposure", nonnegative=True) != 0
        ):
            raise ReportDataError("snapshot: filas contradicen ausencia de exposición")
    if snapshot["orders"]["rows"]:
        raise ReportDataError("snapshot: hay órdenes resting")
    return cash, scope


def period_performance(document: dict) -> dict:
    """No estima retornos porcentuales: aportes durante el período cambian el capital."""
    result = {
        "schema_version": 1,
        "status": "ATENCION",
        "account_net_pnl_usd": None,
        "project_pnl_usd": None,
        "authorizes_trading": False,
    }
    try:
        if not isinstance(document, dict) or document.get("cash_flows_complete") is not True:
            raise ReportDataError("cash_flows: falta conciliación completa del operador")
        if document.get("boundary_activity_excluded") is not True:
            raise ReportDataError(
                "snapshots: falta confirmar ausencia de actividad durante las lecturas"
            )
        opening, closing = document.get("opening"), document.get("closing")
        first, first_scope = _cash_snapshot(opening)
        last, last_scope = _cash_snapshot(closing)
        if first_scope != last_scope:
            raise ReportDataError("scope: snapshots de fuentes o subcuentas distintas")
        start = timestamp(opening.get("collection_finished_at"), "opening_time")
        first_started = timestamp(opening.get("collection_started_at"), "opening_start")
        last_started = timestamp(closing.get("collection_started_at"), "closing_start")
        end = timestamp(closing.get("collection_finished_at"), "closing_time")
        if not first_started <= start < last_started <= end:
            raise ReportDataError("period: intervalo vacío o invertido")
        events = document.get("cash_flows")
        if not isinstance(events, list) or len(events) > 10_000:
            raise ReportDataError("cash_flows: lista ausente o fuera de presupuesto")
        deposits = withdrawals = external_costs = Decimal(0)
        ids = set()
        with localcontext() as ctx:
            ctx.prec = 40
            for event in events:
                if not isinstance(event, dict):
                    raise ReportDataError("cash_flows: fila inválida")
                identifier = event.get("id")
                if (
                    not isinstance(identifier, str)
                    or not identifier
                    or len(identifier) > 200
                    or identifier in ids
                ):
                    raise ReportDataError("cash_flows: identidad ausente o duplicada")
                ids.add(identifier)
                when = timestamp(event.get("at"), "cash_flow_time")
                if not first_started < when <= end:
                    raise ReportDataError("cash_flows: movimiento fuera del período")
                if when <= start or when >= last_started:
                    raise ReportDataError(
                        "cash_flows: movimiento durante lectura de frontera; repetir snapshot"
                    )
                amount = decimal_value(
                    event.get("amount_usd"), "cash_flow_amount", nonnegative=True
                )
                if amount <= 0:
                    raise ReportDataError("cash_flows: monto debe ser positivo")
                kind = event.get("kind")
                if kind == "deposit":
                    deposits += amount
                elif kind == "withdrawal":
                    withdrawals += amount
                elif kind == "external_cost" and event.get("paid_outside_account") is True:
                    external_costs += amount
                else:
                    raise ReportDataError("cash_flows: tipo desconocido o costo no externo")
            net = last - first - deposits + withdrawals
            result.update(
                {
                    "status": "OK",
                    "scope": first_scope,
                    "period": {"start": start.isoformat(), "end": end.isoformat()},
                    "opening_cash_usd": first,
                    "closing_cash_usd": last,
                    "deposits_usd": deposits,
                    "withdrawals_usd": withdrawals,
                    "external_costs_usd": external_costs,
                    "account_net_pnl_usd": net,
                    "project_pnl_usd": net - external_costs,
                    "fees_already_reflected_in_cash": True,
                    "method": "cash_only_boundaries_and_operator_reconciled_flows",
                    "attribution": "account_period_not_individual_engine",
                    "error": None,
                }
            )
    except ReportDataError as exc:
        result["error"] = str(exc)
    except (KeyError, TypeError, AttributeError) as exc:
        result["error"] = f"documento: estructura inválida ({type(exc).__name__})"
    return result
