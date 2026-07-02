"""
MMReconciler (F2) — la VERDAD runtime: get_orders() de Kalshi ↔ tabla Trade por
client_order_id (+ fills por WS como fast-path, ver fill_feed.py).

El gap #1 del F0 (§1.3): la reconciliación de resting orders solo existía al boot.
Una orden cancelada por Kalshi/UI dejaba un 'pending' fantasma reservando capital para
siempre; un fill no visto dejaba exposición invisible. Este loop cierra ambos:

  Por cada fila Trade pending de strategy='motor_5_mm':
    - orden 'resting' en la API   → OK (se refresca kalshi_order_id si faltaba).
    - 'executed'                  → fila filled (+ filled_count si la API lo trae).
    - 'canceled' con fill parcial → filled con filled_count; sin fill → cancelled.
    - NO aparece en la API        → DISCREPANCIA → ticker corrupto.
  Por cada orden resting de la API con prefijo m5mm- SIN fila pending nuestra:
    - FANTASMA (la DB la perdió) → se CANCELA + ticker corrupto.

Toda discrepancia marca el ticker corrupto → el executor cancela todo lo suyo y
re-sincroniza antes de volver a cotizar (estado corrupto no opera, Lección 9).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger
from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.executor import COID_PREFIX, STRATEGY, _parse_count


@dataclass(slots=True)
class MMReconcileReport:
    pending_checked: int = 0
    resolved_filled: int = 0
    resolved_cancelled: int = 0
    still_resting: int = 0
    phantom_cancelled: int = 0
    discrepancies: int = 0
    corrupted_tickers: set[str] = field(default_factory=set)


class MMReconciler:
    def __init__(self, client: KalshiRestClient) -> None:
        self._client = client

    async def reconcile(self) -> MMReconcileReport:
        report = MMReconcileReport()
        try:
            resp = await self._client.get_orders(limit=200)
        except Exception:
            logger.exception("motor5.reconcile.get_orders_error → tick sin reconcile")
            report.discrepancies += 1  # sin verdad de la API, nada se des-corrompe
            return report
        api_orders = {
            o.get("client_order_id"): o
            for o in (resp.get("orders") or [])
            if o.get("client_order_id")
        }

        with get_session() as s:
            pending = list(
                s.exec(select(Trade).where(Trade.strategy == STRATEGY, Trade.status == "pending"))
            )
        for row in pending:
            report.pending_checked += 1
            order = api_orders.get(row.client_order_id)
            if order is None:
                # La API no la conoce (¿nunca entró? ¿purgada?) — irresoluble acá.
                report.discrepancies += 1
                report.corrupted_tickers.add(row.ticker)
                logger.warning(
                    f"motor5.reconcile.unknown_order coid={row.client_order_id[:13]}… "
                    f"ticker={row.ticker} (pending sin orden en la API → corrupto)"
                )
                continue
            status = str(order.get("status", "")).lower()
            filled = _parse_count(order.get("fill_count")) or 0
            if status == "resting":
                report.still_resting += 1
                if not row.kalshi_order_id and order.get("order_id"):
                    self._update(row.client_order_id, kalshi_order_id=order["order_id"])
            elif status == "executed":
                report.resolved_filled += 1
                self._update(
                    row.client_order_id,
                    status="filled",
                    filled_count=filled or row.count,
                    note="reconcile: executed",
                )
            elif status in ("canceled", "cancelled"):
                if filled > 0:
                    report.resolved_filled += 1
                    self._update(
                        row.client_order_id,
                        status="filled",
                        filled_count=filled,
                        note="reconcile: partial_then_canceled",
                    )
                else:
                    report.resolved_cancelled += 1
                    self._update(
                        row.client_order_id, status="cancelled", note="reconcile: canceled"
                    )
            else:
                report.discrepancies += 1
                report.corrupted_tickers.add(row.ticker)
                logger.warning(
                    f"motor5.reconcile.status_desconocido {row.client_order_id[:13]}… "
                    f"status={status!r} → corrupto"
                )

        # Fantasmas: resting en la API con nuestro prefijo, sin fila pending en DB.
        pending_coids = {row.client_order_id for row in pending}
        for coid, order in api_orders.items():
            if not coid.startswith(COID_PREFIX) or coid in pending_coids:
                continue
            if str(order.get("status", "")).lower() != "resting":
                continue
            oid = order.get("order_id")
            ticker = order.get("ticker", "?")
            report.phantom_cancelled += 1
            report.discrepancies += 1
            report.corrupted_tickers.add(ticker)
            logger.warning(f"motor5.reconcile.phantom coid={coid[:13]}… ticker={ticker} → cancel")
            if oid:
                try:
                    await self._client.cancel_order(oid)
                except Exception:
                    logger.exception(f"motor5.reconcile.phantom_cancel_error {oid}")

        if report.pending_checked or report.phantom_cancelled:
            logger.info(
                f"motor5.reconcile pending={report.pending_checked} "
                f"resting={report.still_resting} filled={report.resolved_filled} "
                f"cancelled={report.resolved_cancelled} phantom={report.phantom_cancelled} "
                f"discrep={report.discrepancies}"
            )
        return report

    def _update(
        self,
        coid: str,
        *,
        status: str | None = None,
        filled_count: int | None = None,
        kalshi_order_id: str | None = None,
        note: str | None = None,
    ) -> None:
        try:
            with get_session() as s:
                row = s.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                if row is None:
                    return
                if status:
                    row.status = status
                if filled_count is not None:
                    row.filled_count = filled_count
                if kalshi_order_id:
                    row.kalshi_order_id = kalshi_order_id
                if note:
                    row.notes = f"{row.notes or ''} | {note}"[:500]
                s.add(row)
                s.commit()
        except Exception:
            logger.exception(f"motor5.reconcile.update_error coid={coid}")
