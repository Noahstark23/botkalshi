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
from fractions import Fraction
from math import ceil, floor
from typing import Protocol

from loguru import logger
from sqlalchemy import text
from sqlmodel import col, select

from src.math.fees import kalshi_fee_cents
from src.math.quantities import precise_usd
from src.risk.exposure import exposure_count
from src.storage.cost_allocation import usd_text
from src.storage.models import Trade, get_session
from src.storage.order_costs import COST_QUARANTINE_NOTE

_ARB_ID_RE = re.compile(r"arb_id=([0-9a-fA-F-]{36})")
_USD_RE = re.compile(r"[0-9]{1,15}(?:\.[0-9]{1,6})?")


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


@dataclass(frozen=True, slots=True)
class _SettlementAmounts:
    pnl_cents: Fraction
    fee_cents: Fraction
    observed_costs: bool


def _observed_cents(value: object, maximum: Fraction) -> Fraction:
    if not isinstance(value, str) or not _USD_RE.fullmatch(value):
        raise ValueError("importe observado inválido")
    cents = Fraction(value) * 100
    if cents > maximum:
        raise ValueError("importe observado excede pago máximo")
    return cents


def _settlement_amounts(trade: Trade, result: str) -> _SettlementAmounts:
    """BUY usa totales observados; ausencia permite estimación, invalidez no.

    SELL se valora como posición opuesta a 100-P sólo en el camino estimado: el
    ledger todavía no acredita si fill_cost representa ingreso o colateral.
    No se sintetizan costes/fees FP a partir de límites o tarifas.
    """
    if COST_QUARANTINE_NOTE in (trade.notes or ""):
        raise ValueError("costes observados en cuarentena; settlement bloqueado")
    if result not in ("yes", "no") or trade.side not in ("yes", "no"):
        raise ValueError("resultado/lado desconocido")
    if trade.action not in ("buy", "sell"):
        raise ValueError("acción desconocida")
    count = Fraction(exposure_count(trade))
    if count <= 0:
        raise ValueError("settlement sin cantidad ejecutada positiva")
    maximum = count * 100
    cost_fp, fee_fp = trade.fill_cost_usd_fp, trade.fees_usd_fp
    if trade.action == "sell" and cost_fp is not None:
        raise ValueError("coste observado de SELL sin semántica acreditada")

    legacy_fee = trade.fees_cents
    if legacy_fee is not None and (type(legacy_fee) is not int or legacy_fee < 0):
        raise ValueError("fee legacy inválida")
    price = trade.fill_price_cents if trade.fill_price_cents is not None else trade.price_cents
    if (cost_fp is None or (fee_fp is None and legacy_fee is None)) and (
        type(price) is not int or not 1 <= price <= 99
    ):
        raise ValueError("precio estimado fuera de [1,99]")
    side = trade.side
    if trade.action == "sell":
        side = "no" if side == "yes" else "yes"
        price = 100 - price

    gross = _observed_cents(cost_fp, maximum) if cost_fp is not None else count * price
    if fee_fp is not None:
        fees = _observed_cents(fee_fp, maximum)
        if legacy_fee is not None and legacy_fee not in (floor(fees), ceil(fees)):
            raise ValueError("fee observada contradice espejo legacy")
    else:
        fees = Fraction(legacy_fee if legacy_fee is not None else kalshi_fee_cents(count, price))
    payout = maximum if side == result else Fraction(0)
    return _SettlementAmounts(
        payout - gross - fees, fees, cost_fp is not None and fee_fp is not None
    )


def _leg_pnl_and_fees_cents(trade: Trade, result: str) -> tuple[Fraction, Fraction]:
    """Compatibilidad para lectores puros; conserva fracciones de centavo."""
    amounts = _settlement_amounts(trade, result)
    return amounts.pnl_cents, amounts.fee_cents


def _pnl_usd_text(cents: Fraction) -> str:
    # Mantener formato histórico de cuatro decimales cuando sea exacto; no redondear
    # ni perder los microUSD que ya llegaron en costes/fees observados.
    if (cents * 100).denominator == 1:
        return precise_usd(cents)
    return usd_text(cents / 100)


def _settlement_notes(notes: str | None, observed: bool) -> str:
    basis = "settlement_observed_costs" if observed else "settlement_estimated"
    original = notes or ""
    marker = f"{basis} settled_by_poller"
    group = _ARB_ID_RE.search(original)
    if group:
        # La procedencia y arb_id sobreviven incluso con notes ya al límite.
        marker += f" {group.group(0)}"
        original = _ARB_ID_RE.sub("", original, count=1)
    return f"{marker} {original.strip()}".strip()[:500]


def _leg_pnl_cents(trade: Trade, result: str) -> int | Fraction:
    """Compat: solo el PnL (lo consumen tests y análisis)."""
    return _leg_pnl_and_fees_cents(trade, result)[0]


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
    # motor_5_mm (auditoría 2026-07-07, P0 — mismo bug que M1): sus fills quedaban
    # 'filled' para siempre → pérdidas invisibles para los stop-losses Y costo abierto
    # eterno que estrangulaba el canary cap. Coid 'm5mm-<uuid>' cae a grupo unitario;
    # los SELL fillados (ask del MM) los resuelve _leg_pnl_cents como lado opuesto.
    STRATEGIES = ("motor_rest_arb", "motor_2_consensus", "motor_1_arbitrage", "motor_5_mm")

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
                # Serializar con las reducciones/reconciliación locales. Releer TODO
                # el grupo antes de escribir: un cierre concurrente no deja medias patas.
                s.exec(text("BEGIN IMMEDIATE"))
                rows = []
                for t in legs:
                    row = s.exec(
                        select(Trade).where(Trade.client_order_id == t.client_order_id)
                    ).first()
                    if (
                        row is None
                        or row.status != "filled"
                        or row.closed_by_clv
                        or row.ticker != t.ticker
                        or row.strategy != t.strategy
                        or arb_group_key(row) != arb_id
                    ):
                        break
                    amounts = _settlement_amounts(row, resolutions[t.id])  # type: ignore[index]
                    rows.append((row, amounts))
                if len(rows) != len(legs):
                    continue  # rollback al salir; no contabilizar cierres ajenos.
                for row, amounts in rows:
                    pnl, fee = amounts.pnl_cents, amounts.fee_cents
                    # Guardar precisión antes de ofrecer el piso en cents a lectores
                    # legacy: éstos nunca sobreestiman beneficios ni ocultan pérdidas.
                    row.pnl_usd_fp = _pnl_usd_text(pnl)
                    row.pnl_cents = floor(pnl)
                    # Solo si falta: nunca pisar un fee ya registrado (M3 lo ajusta por
                    # tramos al cerrar por CLV; sobrescribirlo rompería esa contabilidad).
                    if row.fees_cents is None:
                        row.fees_cents = ceil(fee)
                    row.status = "settled"
                    row.settled_at = now
                    row.notes = _settlement_notes(row.notes, amounts.observed_costs)
                    s.add(row)
                s.commit()
            settled_count += len(legs)
            logger.info(f"settlement.group_settled arb_id={arb_id} legs={len(legs)}")
        return settled_count
