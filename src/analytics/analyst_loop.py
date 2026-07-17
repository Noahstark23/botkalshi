"""
Analyst Loop — "loop engineering" aplicado a botkalshi (ADVISORY ONLY).

Bucle agendado que OBSERVA (EdgeWindow consensus + Motor2FunnelSnapshot), ANALIZA con el
árbol de decisión del diagnóstico, RECUERDA el veredicto (AnalystVerdict, comparable
día-a-día) y REPORTA un digest a Telegram. Automatiza el trabajo de analista que se venía
haciendo a mano (leer logs → veredicto eficiente/bug/edge → recomendación).

⛔ NO tradea, NO cambia config/umbrales/aliases, NO mergea código. Es ASESOR, no autoridad:
el humano sigue en el centro de toda decisión con plata. Best-effort: un tick que falla se
registra (BotState.record_error) y el loop sigue (patrón SettlementPoller).

Las "skills" (aggregate_consensus / aggregate_funnel / decide) son funciones puras y
testeables, reusables también por scripts CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlmodel import Session, col, select

from src.monitoring.health import BotState
from src.monitoring.telegram_alerts import send_alert
from src.storage.models import (
    AnalystVerdict,
    EdgeWindow,
    MMFunnelSnapshot,
    Motor2FunnelSnapshot,
    get_session,
)
from src.utils.config import get_settings


def _naive_utc_now() -> datetime:
    """Convención del proyecto: naive UTC para comparar con columnas created_at de SQLite."""
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class ConsensusAgg:
    total: int  # filas consensus en la ventana (ya son señales >umbral del detector)
    gt_thresh: int  # cuántas superan el umbral pasado acá (≈ total, salvo cambio de umbral)
    best_pp: float  # mejor edge grabado (pp); -1 si no hubo filas
    mean_pp: float


@dataclass(frozen=True, slots=True)
class FunnelAgg:
    cycles: int  # nº de snapshots del embudo en la ventana (0 = sin datos)
    matched: int
    reject_absent: int
    reject_cardinality: int
    reject_names: int
    reject_no_fair: int
    best_pp: float  # mejor edge NETO VISTO (incluye near-misses < umbral); -1 si nada


@dataclass(frozen=True, slots=True)
class MMAgg:
    """Motor 5 (F1 shadow) en la ventana. cycles=0 → motor apagado/sin datos (no se
    reporta). El MTM es el ÚLTIMO snapshot (es acumulativo, no se suma)."""

    cycles: int
    quoted: int  # quotes emitidas (suma)
    fills: int  # fills hipotéticos (suma)
    mtm_last_cents: int  # PnL mark-to-market neto de fees, último tick
    inventory_abs_last: int  # Σ|net| simulado, último tick


# =====================================================
# Skills (puras / testeables)
# =====================================================


def aggregate_consensus(session: Session, since: datetime, min_edge_pp: float) -> ConsensusAgg:
    """Agrega las EdgeWindow(kind='consensus') desde `since`."""
    rows = list(
        session.exec(
            select(EdgeWindow).where(
                EdgeWindow.kind == "consensus", col(EdgeWindow.created_at) >= since
            )
        )
    )
    edges = [(r.edge_pct or 0.0) * 100 for r in rows]
    gt = [e for e in edges if e > min_edge_pp]
    return ConsensusAgg(
        total=len(rows),
        gt_thresh=len(gt),
        best_pp=max(edges) if edges else -1.0,
        mean_pp=(sum(gt) / len(gt)) if gt else 0.0,
    )


def aggregate_funnel(session: Session, since: datetime) -> FunnelAgg:
    """Agrega los Motor2FunnelSnapshot desde `since` (suma rechazos, máximo best_edge)."""
    rows = list(
        session.exec(
            select(Motor2FunnelSnapshot).where(col(Motor2FunnelSnapshot.created_at) >= since)
        )
    )
    if not rows:
        return FunnelAgg(0, 0, 0, 0, 0, 0, -1.0)
    return FunnelAgg(
        cycles=len(rows),
        matched=sum(r.events_matched for r in rows),
        reject_absent=sum(r.reject_absent for r in rows),
        reject_cardinality=sum(r.reject_cardinality for r in rows),
        reject_names=sum(r.reject_names for r in rows),
        reject_no_fair=sum(r.reject_no_fair for r in rows),
        best_pp=max(r.best_net_edge_pp for r in rows),
    )


def aggregate_mm(session: Session, since: datetime) -> MMAgg:
    """Agrega los MMFunnelSnapshot desde `since` (tracker del gate F1→F2 del Motor 5)."""
    rows = list(
        session.exec(
            select(MMFunnelSnapshot)
            .where(col(MMFunnelSnapshot.created_at) >= since)
            .order_by(col(MMFunnelSnapshot.created_at))
        )
    )
    if not rows:
        return MMAgg(0, 0, 0, 0, 0)
    return MMAgg(
        cycles=len(rows),
        quoted=sum(r.quoted for r in rows),
        fills=sum(r.fills for r in rows),
        mtm_last_cents=rows[-1].mtm_pnl_cents,
        inventory_abs_last=rows[-1].inventory_abs,
    )


def decide(consensus: ConsensusAgg, funnel: FunnelAgg, min_edge_pp: float) -> tuple[str, str]:
    """
    Árbol de decisión determinístico → (verdict, recomendación). Puro/testeable.

    Usa el EMBUDO (matched/rejects/best_edge sobre TODOS los ciclos, incluidos los de 0
    señales) como fuente primaria; las filas consensus solo existen cuando algo cruzó el umbral.
    """
    if funnel.cycles == 0:
        return (
            "sin_datos",
            "El embudo no escribió en la ventana (¿deploy reciente / odds no-live?).",
        )

    if funnel.matched == 0:
        rejects = {
            "names": funnel.reject_names,
            "cardinality": funnel.reject_cardinality,
            "absent": funnel.reject_absent,
            "no_fair": funnel.reject_no_fair,
        }
        dominant = max(rejects, key=lambda k: rejects[k])
        if dominant == "names" and funnel.reject_names > 0:
            return (
                "matching_bug",
                "Nombres no matchean: revisar motor2.name_debug y sumar aliases a TEAM_ALIASES.",
            )
        if dominant == "cardinality" and funnel.reject_cardinality > 0:
            return (
                "cardinalidad",
                "Kalshi 2-way vs odds 3-way: manejar cardinalidad o markets ya resueltos.",
            )
        if dominant == "no_fair" and funnel.reject_no_fair > 0:
            return (
                "sin_consenso",
                "Matcheó pero sin consenso usable: revisar cobertura de bookmakers (regions).",
            )
        return (
            "sin_prematch",
            "Casi todo absent/in-play: pocos partidos pre-match en la ventana (benigno).",
        )

    # matched > 0 → el matcher funciona; la pregunta es si hay edge.
    if funnel.best_pp < min_edge_pp:
        return (
            "mercado_eficiente",
            "Matchea pero el mejor edge no cruza el umbral: Kalshi alineado con las casas. "
            "Evaluar pivot a MLB (#55).",
        )
    if consensus.gt_thresh > 0:
        return (
            "edge_candidato",
            f"{consensus.gt_thresh} señales >{min_edge_pp:.1f}pp (best {consensus.best_pp:.1f}pp): "
            "revisar consistencia varios días antes de encender capital chico.",
        )
    return (
        "inconsistente",
        "best_edge del embudo cruzó el umbral pero no se grabaron señales — revisar persistencia/umbral.",
    )


# =====================================================
# El loop
# =====================================================


class AnalystLoop:
    """Loop supervisado advisory-only: observa → analiza → recuerda → reporta."""

    def __init__(
        self,
        *,
        interval_sec: float | None = None,
        window_hours: int | None = None,
        min_edge_pp: float | None = None,
    ) -> None:
        # Solo leer settings si falta algún parámetro (los tests pasan los 3 → sin get_settings).
        s = (
            get_settings()
            if (interval_sec is None or window_hours is None or min_edge_pp is None)
            else None
        )
        self._interval = interval_sec if interval_sec is not None else s.ANALYST_LOOP_INTERVAL_SEC
        self._window_hours = (
            window_hours if window_hours is not None else s.ANALYST_LOOP_WINDOW_HOURS
        )
        self._min_edge_pp = min_edge_pp if min_edge_pp is not None else s.MOTOR_2_MIN_EDGE_PCT

    async def run(self, stop_event: asyncio.Event) -> None:
        logger.info(
            f"analyst_loop started interval={self._interval:.0f}s window={self._window_hours}h"
        )
        while not stop_event.is_set():
            try:
                await self.analyze_once()
            except Exception as exc:
                logger.exception("analyst_loop.tick_failed")
                BotState.record_error(f"analyst_loop: {type(exc).__name__}: {exc}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)

    async def analyze_once(self) -> AnalystVerdict:
        """Un ciclo: agrega la ventana, computa veredicto, lo persiste (memoria) y reporta."""
        since = _naive_utc_now() - timedelta(hours=self._window_hours)
        with get_session() as session:
            consensus = aggregate_consensus(session, since, self._min_edge_pp)
            funnel = aggregate_funnel(session, since)
            prev = session.exec(
                select(AnalystVerdict).order_by(col(AnalystVerdict.recorded_at).desc())
            ).first()
            prev_gt = prev.consensus_gt_thresh if prev else None
            verdict, rec = decide(consensus, funnel, self._min_edge_pp)
            row = AnalystVerdict(
                window_hours=self._window_hours,
                consensus_total=consensus.total,
                consensus_gt_thresh=consensus.gt_thresh,
                best_edge_pp=consensus.best_pp,
                mean_edge_pp=consensus.mean_pp,
                funnel_cycles=funnel.cycles,
                matched=funnel.matched,
                reject_absent=funnel.reject_absent,
                reject_cardinality=funnel.reject_cardinality,
                reject_names=funnel.reject_names,
                reject_no_fair=funnel.reject_no_fair,
                verdict=verdict,
                recommendation=rec[:500],
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            # Motor 5 (F1 shadow): línea informativa del digest — NO entra al veredicto ni
            # al schema (misma decisión que los contadores nuevos del funnel: log-only).
            mm = aggregate_mm(session, since)
        await self._report(row, funnel, prev_gt, mm)
        return row

    async def _report(
        self, row: AnalystVerdict, funnel: FunnelAgg, prev_gt: int | None, mm: MMAgg | None = None
    ) -> None:
        trend = (
            "" if prev_gt is None else f" (tendencia >umbral: {prev_gt}→{row.consensus_gt_thresh})"
        )
        digest = (
            f"📊 Analyst Loop — veredicto: *{row.verdict}*\n"
            f"ventana {row.window_hours}h · consensus={row.consensus_total} "
            f"(>{self._min_edge_pp:.1f}pp: {row.consensus_gt_thresh}){trend}\n"
            f"embudo: ciclos={row.funnel_cycles} matched={row.matched} best={funnel.best_pp:.1f}pp · "
            f"rej[absent={row.reject_absent} card={row.reject_cardinality} "
            f"names={row.reject_names} nofair={row.reject_no_fair}]\n"
            f"→ {row.recommendation}\n"
            f"(bot: mercados={BotState.tracked_markets_count} paused={BotState.is_paused})"
        )
        if mm is not None and mm.cycles:
            digest += (
                f"\nM5 shadow: ticks={mm.cycles} quotes={mm.quoted} fills={mm.fills} "
                f"mtm=${mm.mtm_last_cents / 100:+.2f} inv={mm.inventory_abs_last}"
            )
        logger.info(f"analyst_loop.verdict {row.verdict} | {row.recommendation}")
        with contextlib.suppress(Exception):
            await send_alert(digest)  # no-op si Telegram no está configurado
