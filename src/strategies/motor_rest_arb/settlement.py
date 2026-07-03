"""
Settlement del Motor REST (PR-B) — pasa trades 'filled' a 'settled' con PnL real.

POR QUÉ EXISTE: los stop-losses del RiskManager (−3/−8/−15%) leen Trade con
status='settled' + pnl_cents. Sin settlement, NINGÚN trade llega a 'settled' y la
escalera no puede disparar jamás. A.1 settlea al instante las pérdidas de rollback;
este módulo settlea el resto: posiciones que esperan la RESOLUCIÓN del mercado
(arb both-FILL = profit bloqueado; pata expuesta de kill-switch = direccional).

EL INVARIANTE (la pérdida fantasma): un arb both-FILL se settlea con AMBAS patas en
UNA transacción o con NINGUNA. Settlear la ganadora dejando la perdedora 'filled'
contaría medio PnL (+) mientras la otra mitad (−) sigue como "exposición" → un
−$8.25 real podría verse como +$X y el stop diario de −$9 no dispararía. Imposible
por construcción: el grupo (arb_id) se procesa completo o se saltea.

FUENTE DE RESOLUCIÓN detrás de una interfaz (SettlementSource): el core se construye
y testea HOY con un fake; el adaptador Kalshi real es un stub hasta verificar los
shapes contra producción (read-only: GET /portfolio/settlements u
get_market().result — una sesión cuando los shapes estén confirmados).

Reglas del proyecto: select()+s.exec() (nunca .query()), naive UTC en writes,
supervisor pattern con try/except (sin gather(return_exceptions)).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from loguru import logger
from sqlmodel import col, select

from src.math.fees import kalshi_fee_cents
from src.storage.models import Trade, get_session

_ARB_ID_RE = re.compile(r"arb_id=([0-9a-fA-F-]{36})")


def _naive_utc_now() -> datetime:
    """Convención del proyecto para writes a Trade (manager.py): naive UTC."""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class MarketResolution:
    """Resolución de un mercado: a qué lado pagó."""

    ticker: str
    result: str  # "yes" | "no" — el lado que paga 100¢/contrato


class SettlementSource(Protocol):
    """Fuente de resoluciones. None = el mercado AÚN no resolvió (o no se sabe)."""

    async def get_resolution(self, ticker: str) -> MarketResolution | None: ...


class KalshiSettlementSource:
    """
    Adaptador REAL contra Kalshi vía get_market(ticker).result.

    Un mercado de Kalshi expone `result` ∈ {"yes","no"} cuando RESOLVIÓ; vacío/ausente
    mientras sigue abierto. Read-only (no toca capital). FAIL-SAFE: cualquier valor que
    NO sea exactamente "yes"/"no" → None (aún no resuelto) → el poller atómico SALTEA el
    grupo (nunca settlea de más). [verificar en smoke] el shape exacto del campo result
    contra un mercado del Mundial ya resuelto; si el nombre difiere, se ajusta acá sin
    tocar el poller.

    (Opción 2 del diseño: usa get_market —ya en el cliente— sin endpoint nuevo. La
    opción 1, GET /portfolio/settlements, queda como mejora si se necesita revenue
    oficial agregado.)
    """

    def __init__(self, client: object) -> None:
        self.client = client

    async def get_resolution(self, ticker: str) -> MarketResolution | None:
        resp = await self.client.get_market(ticker)  # type: ignore[attr-defined]
        market = resp.get("market", resp) if isinstance(resp, dict) else {}
        result = str(market.get("result", "")).strip().lower()
        if result in ("yes", "no"):
            return MarketResolution(ticker=ticker, result=result)
        return None  # aún no resolvió (open / result vacío) → el grupo espera


def arb_group_key(trade: Trade) -> str:
    """
    Clave de grupo del arb: arb_id desde notes ('arb_id=<uuid>'), con fallback al
    prefijo del client_order_id ('<uuid>-yes'). Si nada matchea, el trade es su
    propio grupo (se settlea solo — no hay pata hermana que esperar).
    """
    m = _ARB_ID_RE.search(trade.notes or "")
    if m:
        return m.group(1)
    coid = trade.client_order_id
    if "-" in coid:
        prefix = coid.rsplit("-", 1)[0]
        if len(prefix) == 36:  # uuid4 con guiones
            return prefix
    return coid  # grupo unitario


def _leg_pnl_cents(trade: Trade, result: str) -> int:
    """
    PnL realizado de una pata al resolverse el mercado.

    Ganadora (side == result): payout 100¢/contrato − costo − fees.
    Perdedora: −costo − fees. Usa fill_price_cents si existe (precio real), si no el
    límite; fees de la fila (A.1 los registró al fill) o recomputados como fallback.
    """
    price = trade.fill_price_cents or trade.price_cents
    fees = (
        trade.fees_cents if trade.fees_cents is not None else kalshi_fee_cents(trade.count, price)
    )
    if trade.side == result:
        return (100 - price) * trade.count - fees
    return -price * trade.count - fees


class SettlementPoller:
    """
    Poller liviano: cada tick busca trades 'filled' de motor_rest_arb, los agrupa por
    arb_id y settlea cada grupo COMPLETO (todas sus patas filled) o lo saltea.

    Idempotente: solo procesa status='filled'; una vez 'settled', la fila sale de la
    query y re-entregas de la misma resolución son no-op por construcción.
    """

    POLL_INTERVAL_SEC = 300.0  # 5 min — los stop-losses son de escala diaria
    # Motores cuyos trades 'filled' este poller settlea. Motor 2 (apuestas direccionales
    # single-leg) se agrega acá: cada apuesta es su propio grupo (arb_group_key cae al
    # prefijo del coid), y _leg_pnl_cents resuelve un lado direccional sin cambios.
    # motor_1_arbitrage (bug producción 2026-07-02): estaba FUERA de la lista → sus
    # filled jamás se settleaban — un par hedged del 30-jun quedó 2 días como $235 de
    # "exposición" permanente y estranguló el headroom compartido (Motor 2 → 0
    # contratos). Sus filas legacy (notes=None, coid uuid pelado) caen a grupo unitario
    # en arb_group_key: cada pata se settlea sola por la resolución de SU ticker.
    STRATEGIES = ("motor_rest_arb", "motor_2_consensus", "motor_1_arbitrage")

    def __init__(self, source: SettlementSource) -> None:
        self.source = source

    async def run(self, stop_event: asyncio.Event) -> None:
        """
        Loop supervisado: un fallo de tick se registra (BotState.record_error) y el
        loop SIGUE — nunca muere en silencio ni traga excepciones sin reportar.
        """
        logger.info(f"settlement.poller started interval={self.POLL_INTERVAL_SEC:.0f}s")
        while not stop_event.is_set():
            try:
                settled = await self.settle_once()
                if settled:
                    logger.info(f"settlement.tick settled_legs={settled}")
            except Exception as exc:
                logger.exception("settlement.tick_failed")
                try:
                    from src.monitoring.health import BotState

                    BotState.record_error(f"settlement: {type(exc).__name__}: {exc}")
                except Exception:
                    pass  # best-effort
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self.POLL_INTERVAL_SEC)

    async def settle_once(self) -> int:
        """
        Un tick. Devuelve cuántas patas se settlearon (para logs/tests).

        Por grupo (arb_id): consulta la resolución de CADA pata filled; si TODAS
        tienen resolución → settlea TODAS en una transacción. Si falta alguna →
        skip del grupo entero (atomicidad: la pérdida fantasma es imposible).
        """
        with get_session() as s:
            stmt = select(Trade).where(
                Trade.status == "filled",
                col(Trade.strategy).in_(self.STRATEGIES),
            )
            filled = list(s.exec(stmt))
        # Motor 3: una posición cerrada con SELL anticipado de CLV NO se settlea por
        # resolución del mercado — su PnL ya quedó realizado al salir. Saltearla evita
        # el doble-conteo (NULL/0 en filas viejas = no-CLV → se procesan normal).
        filled = [t for t in filled if not t.closed_by_clv]
        if not filled:
            return 0

        groups: dict[str, list[Trade]] = {}
        for t in filled:
            groups.setdefault(arb_group_key(t), []).append(t)

        settled_count = 0
        for arb_id, legs in groups.items():
            # 1) Resolver TODAS las patas del grupo ANTES de tocar la DB.
            resolutions: dict[int, str] = {}  # trade.id → result
            complete = True
            for t in legs:
                res = await self.source.get_resolution(t.ticker)
                if res is None:
                    complete = False
                    break  # el mercado de esta pata aún no resolvió → grupo entero espera
                resolutions[t.id] = res.result  # type: ignore[index]
            if not complete:
                continue

            # 2) Settlear el grupo COMPLETO en UNA transacción (un solo commit):
            #    un crash a mitad → rollback de SQLAlchemy → ninguna pata queda a medias.
            now = _naive_utc_now()
            with get_session() as s:
                for t in legs:
                    row = s.exec(
                        select(Trade).where(Trade.client_order_id == t.client_order_id)
                    ).first()
                    if row is None or row.status != "filled":
                        continue  # carrera benigna: otro proceso la cerró
                    row.pnl_cents = _leg_pnl_cents(row, resolutions[t.id])  # type: ignore[index]
                    row.status = "settled"
                    row.settled_at = now
                    row.notes = f"{row.notes or ''} settled_by_poller".strip()[:500]
                    s.add(row)
                s.commit()
            settled_count += len(legs)
            logger.info(f"settlement.group_settled arb_id={arb_id} legs={len(legs)}")
        return settled_count
