"""Reconciliación M5: solo evidencia terminal íntegra libera una intención.

Todos los cursores deben terminar antes de mutar la DB. Una ausencia, un formato
desconocido o una escritura fallida conservan pending y bloquean nuevas quotes.
Paginar no hace atómica la lectura: identidades repetidas invalidan el barrido.
Las órdenes históricas ausentes requieren resolución aparte.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

from loguru import logger
from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.executor import COID_PREFIX, STRATEGY
from src.strategies.motor_5_mm.incidents import latch_incident
from src.strategies.motor_5_mm.ledger import confirmed_fill, record_fill
from src.strategies.motor_5_mm.order_state import (
    COUNT_QUARANTINE_NOTE,
    filled_count,
    terminal_fill_count,
    unrepresentable_fill,
)


@dataclass(slots=True)
class MMReconcileReport:
    # Un informe vacío no acredita una reconciliación terminada.
    complete: bool = False
    pending_checked: int = 0
    resolved_filled: int = 0
    resolved_cancelled: int = 0
    still_resting: int = 0
    phantom_cancelled: int = 0
    orphan_cancelled: int = 0
    discrepancies: int = 0
    corrupted_tickers: set[str] = field(default_factory=set)
    terminal_coids: set[str] = field(default_factory=set)


class MMReconciler:
    def __init__(self, client: KalshiRestClient) -> None:
        self._client = client

    @staticmethod
    def _uncertain(
        report: MMReconcileReport, ticker: str | None = None, *, durable: bool = True
    ) -> None:
        report.discrepancies += 1
        if ticker:
            report.corrupted_tickers.add(ticker)
        if durable:
            latch_incident(f"reconciliación M5 incierta: {ticker or 'identidad desconocida'}")

    async def reconcile(self, live_coids: set[str] | None = None) -> MMReconcileReport:
        """None no autoriza cancelar huérfanas; set vacío identifica un reinicio."""
        report = MMReconcileReport()
        try:
            orders = await self._client.get_all_orders(limit=200)
            api_orders: dict[str, dict] = {}
            order_ids: set[str] = set()
            for order in orders:
                coid, oid = order.get("client_order_id"), order.get("order_id")
                if not isinstance(coid, str) or not coid or not isinstance(oid, str) or not oid:
                    raise ValueError("identidad de orden incompleta")
                if coid in api_orders or oid in order_ids:
                    raise ValueError("identidad repetida en barrido de órdenes")
                api_orders[coid] = order
                order_ids.add(oid)
            with get_session() as session:
                pending = list(
                    session.exec(
                        select(Trade).where(Trade.strategy == STRATEGY, Trade.status == "pending")
                    )
                )
        except Exception:
            logger.exception("motor5.reconcile.snapshot_incomplete → sin nuevas quotes")
            self._uncertain(report, durable=False)
            return report

        for row in pending:
            report.pending_checked += 1
            if COUNT_QUARANTINE_NOTE in (row.notes or ""):
                self._uncertain(report, row.ticker)
                continue
            order = api_orders.get(row.client_order_id)
            if order is None or not self._matches(row, order):
                self._uncertain(report, row.ticker)
                continue
            status = str(order.get("status", "")).lower()
            if status == "resting":
                observed_fill = filled_count(order)
                if observed_fill is None or observed_fill > row.count:
                    # El ledger no representa esta evidencia. El marcador se
                    # preserva en DB: otro cancel o reinicio no la convierte en cero.
                    if not self._update(row.client_order_id, note=COUNT_QUARANTINE_NOTE):
                        # Sin registrar el bloqueo, no continuar hacia otros cancels.
                        latch_incident("no se pudo persistir cuarentena de cantidad M5")
                        raise RuntimeError("no se pudo persistir cuarentena de cantidad M5")
                    self._uncertain(report, row.ticker)
                    continue
                if observed_fill < (confirmed_fill(row) or 0):
                    self._uncertain(report, row.ticker)
                    continue
                if not self._update(
                    row.client_order_id,
                    kalshi_order_id=order["order_id"],
                    filled_count=observed_fill,
                ):
                    # Volver al engine permitiría cancelar usando una DB que perdió
                    # el fill recién observado. El supervisor debe ver un fallo fatal.
                    latch_incident("no se pudo persistir evidencia resting M5")
                    raise RuntimeError("no se pudo persistir evidencia resting M5")
                row.kalshi_order_id = order["order_id"]
                record_fill(row, observed_fill)
                if live_coids is not None and row.client_order_id not in live_coids:
                    await self._cancel_orphan(row, order, report)
                    continue
                # El engine reconstruye el inventario desde esta cantidad durable
                # ANTES de cotizar; pending conserva la reserva de la orden completa.
                report.still_resting += 1
            else:
                self._resolve_terminal(row, order, report, note="reconcile: terminal")

        pending_coids = {row.client_order_id for row in pending}
        for coid, order in api_orders.items():
            if (
                not coid.startswith(COID_PREFIX)
                or coid in pending_coids
                or str(order.get("status", "")).lower() != "resting"
            ):
                continue
            # Sin ledger no se reconstruyen exposición/fees: ni un cancel confirmado
            # elimina la discrepancia ni autoriza nuevas quotes.
            ticker = order.get("ticker")
            self._uncertain(report, ticker if isinstance(ticker, str) else None)
            try:
                response = await self._client.cancel_order(order["order_id"])
                terminal = response.get("order", response) if isinstance(response, dict) else None
                if (
                    terminal_fill_count(terminal, order_id=order["order_id"], client_order_id=coid)
                    is not None
                ):
                    report.phantom_cancelled += 1
            except Exception:
                logger.exception("motor5.reconcile.phantom_cancel_unconfirmed")

        report.complete = report.discrepancies == 0
        if report.pending_checked or report.discrepancies:
            logger.info(
                f"motor5.reconcile complete={report.complete} pending={report.pending_checked} "
                f"resting={report.still_resting} filled={report.resolved_filled} "
                f"cancelled={report.resolved_cancelled} phantom={report.phantom_cancelled} "
                f"orphan={report.orphan_cancelled} discrep={report.discrepancies}"
            )
        return report

    @staticmethod
    def _matches(row: Trade, order: dict) -> bool:
        return (
            order.get("client_order_id") == row.client_order_id
            and order.get("ticker") == row.ticker
            and (not row.kalshi_order_id or order.get("order_id") == row.kalshi_order_id)
            and order.get("action", row.action) == row.action
            and order.get("side", row.side) == row.side
        )

    @staticmethod
    def _observed_identity_matches(row: Trade, order: object) -> bool:
        oid = row.kalshi_order_id or (order.get("order_id") if isinstance(order, dict) else None)
        return (
            isinstance(order, dict)
            and bool(oid)
            and order.get("order_id") == oid
            and order.get("client_order_id", row.client_order_id) == row.client_order_id
        )

    def _quarantine_observed_count(self, row: Trade, order: object) -> bool:
        """Evidencia imposible sólo bloquea; nunca confirma el payload de una excepción."""
        if self._observed_identity_matches(row, order) and unrepresentable_fill(
            order, count=row.count
        ):
            # Una cantidad explícita imposible no desaparece porque el siguiente
            # cancel/reinicio diga cero. La reserva global requiere revisión manual.
            if not self._update(row.client_order_id, note=COUNT_QUARANTINE_NOTE):
                latch_incident("no se pudo persistir cuarentena de cantidad terminal M5")
                raise RuntimeError("no se pudo persistir cuarentena de cantidad terminal M5")
            row.notes = f"{COUNT_QUARANTINE_NOTE} | {row.notes or ''}"[:500]
            return True
        return False

    def _preserve_observed_count(self, row: Trade, order: object) -> None:
        """Guarda el piso observado antes del error sin dar por terminal la orden."""
        if not self._observed_identity_matches(row, order):
            return
        if self._quarantine_observed_count(row, order):
            return
        observed_fill = filled_count(order)
        if observed_fill is None or observed_fill <= (confirmed_fill(row) or 0):
            return
        if not self._update(row.client_order_id, filled_count=observed_fill):
            latch_incident("no se pudo persistir evidencia de cancel M5")
            raise RuntimeError("no se pudo persistir evidencia de cancel M5")
        # La siguiente observación de la MISMA excepción jamás rebaja este piso.
        record_fill(row, observed_fill)

    def _resolve_terminal(
        self, row: Trade, order: object, report: MMReconcileReport, *, note: str
    ) -> bool:
        if self._quarantine_observed_count(row, order):
            self._uncertain(report, row.ticker)
            return False
        oid = row.kalshi_order_id or (order.get("order_id") if isinstance(order, dict) else None)
        filled = terminal_fill_count(
            order, order_id=oid, client_order_id=row.client_order_id, count=row.count
        )
        if filled is None or filled < (confirmed_fill(row) or 0):
            self._uncertain(report, row.ticker)
            return False
        if not self._update(
            row.client_order_id,
            status="filled" if filled else "cancelled",
            filled_count=filled,
            kalshi_order_id=oid,
            note=note,
        ):
            latch_incident("no se pudo persistir evidencia terminal M5")
            raise RuntimeError("no se pudo persistir evidencia terminal M5")
        report.terminal_coids.add(row.client_order_id)
        if filled:
            report.resolved_filled += 1
        else:
            report.resolved_cancelled += 1
        return True

    async def _cancel_orphan(self, row: Trade, order: dict, report: MMReconcileReport) -> None:
        oid = row.kalshi_order_id or order["order_id"]
        # Identidad y mínimo fill conocido ya persistidos por la rama resting común.
        try:
            response = await self._client.cancel_order(oid)
        except BaseException as exc:
            # El cliente V2 pudo rechazar el sobre DESPUÉS de observar un conteo
            # real. Timeout/cancelación también conservan esa evidencia sin cierre.
            try:
                observations = getattr(exc, "observed_orders", None)
                if not isinstance(observations, list) or not observations:
                    observations = [getattr(exc, "observed_order", None)]
                for observed in observations:
                    self._preserve_observed_count(row, observed)
                self._uncertain(report, row.ticker)
            except Exception:
                if not isinstance(exc, Exception):
                    # Un fallo secundario de DB no consume la cancelación original.
                    logger.error("motor5.reconcile.cancel_observation_persist_failed")
                    raise exc from None
                raise
            if not isinstance(exc, Exception):
                raise
            logger.warning("motor5.reconcile.orphan_cancel_unconfirmed")
            return
        terminal = response.get("order", response) if isinstance(response, dict) else None
        if self._resolve_terminal(
            row, terminal, report, note="reconcile: orphan_resting_cancelled"
        ):
            report.orphan_cancelled += 1

    def _update(
        self,
        coid: str,
        *,
        status: str | None = None,
        filled_count: int | Fraction | None = None,
        kalshi_order_id: str | None = None,
        note: str | None = None,
    ) -> bool:
        try:
            with get_session() as session:
                row = session.exec(select(Trade).where(Trade.client_order_id == coid)).first()
                if row is None or row.strategy != STRATEGY or row.status != "pending":
                    return False
                if status and COUNT_QUARANTINE_NOTE in (row.notes or ""):
                    return False
                if filled_count is not None and filled_count < (confirmed_fill(row) or 0):
                    return False
                if kalshi_order_id and row.kalshi_order_id not in (None, "", kalshi_order_id):
                    return False
                if status:
                    row.status = status
                if filled_count is not None:
                    record_fill(row, filled_count)
                if kalshi_order_id:
                    row.kalshi_order_id = kalshi_order_id
                if note:
                    row.notes = (
                        f"{note} | {row.notes or ''}"
                        if note == COUNT_QUARANTINE_NOTE
                        else f"{row.notes or ''} | {note}"
                    )[:500]
                session.add(row)
                session.commit()
            return True
        except Exception:
            logger.exception(f"motor5.reconcile.update_error coid={coid}")
            return False
