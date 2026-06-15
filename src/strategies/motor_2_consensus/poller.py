"""
Poller shadow del Motor 2 — orquesta el cable Kalshi ↔ consenso, en loop, SIN ejecutar.

Por ciclo:
  1. Extrae los quotes 1X2 de Kalshi (KalshiQuoteSource).
  2. Trae el consenso de odds (OddsSource: fake hoy, The Odds API mañana).
  3. find_signals() → ConsensusSignal por outcome con edge neto > umbral.
  4. Graba EdgeWindow(kind="consensus") SOLO si la fuente de odds es real (is_live).
     Con la fuente fake las edges no son data → se loguea pero NO se persiste basura.

SHADOW estricto: emite/loguea/graba señales, JAMÁS coloca órdenes. Independiente de
TRADING_ENABLED. Best-effort: un ciclo que falla se loguea y el loop sigue.
"""

from __future__ import annotations

import asyncio
import contextlib

from loguru import logger

from src.storage.models import EdgeWindow, Motor2FunnelSnapshot, get_session
from src.strategies.motor_2_consensus.detector import MIN_EDGE_PCT, ConsensusSignal, find_signals
from src.strategies.motor_2_consensus.executor import Motor2Executor
from src.strategies.motor_2_consensus.sources import KalshiQuoteSource, OddsSource
from src.utils.config import get_settings


class Motor2ShadowPoller:
    """
    Loop del Motor 2. Por defecto SHADOW (solo detecta/graba). Si se le inyecta un
    `executor` Y la fuente de odds es REAL (is_live), apuesta — nunca sobre el fixture
    fake (apostar contra odds inventadas sería quemar plata).
    """

    DEFAULT_INTERVAL_SEC = 300.0  # 5 min — calibrable; el consenso no se mueve por segundo.
    MAX_BETS_PER_CYCLE = 5  # tope de órdenes por ciclo (el RiskManager además frena por exposición)

    def __init__(
        self,
        kalshi_source: KalshiQuoteSource,
        odds_source: OddsSource,
        *,
        interval_sec: float | None = None,
        capital_usd: float | None = None,
        min_edge: float | None = None,
        executor: Motor2Executor | None = None,
    ):
        self._kalshi = kalshi_source
        self._odds = odds_source
        self._interval = interval_sec if interval_sec is not None else self.DEFAULT_INTERVAL_SEC
        # Capital real del bot para el sizing (¼ Kelly); en prod-shadow es chico ($5).
        self._capital_usd = (
            capital_usd if capital_usd is not None else get_settings().ACTIVE_CAPITAL_USD
        )
        # Umbral de edge NETO como FRACCIÓN (0.03 = 3pp). Default = el del detector; el
        # runner lo pasa desde config (MOTOR_2_MIN_EDGE_PCT / 100).
        self._min_edge = min_edge if min_edge is not None else MIN_EDGE_PCT
        # Presente SOLO con TRADING_ENABLED=true (lo construye el runner, Capa A). None = shadow.
        self._executor = executor

    async def poll_once(self) -> list[ConsensusSignal]:
        """Un ciclo: extrae, cruza, detecta, (persiste + apuesta si live). Devuelve las señales."""
        kalshi_events = await self._kalshi.fetch()
        if not kalshi_events:
            return []
        odds_events = await self._odds.fetch()
        if not odds_events:
            logger.info("motor2.shadow sin eventos de odds este ciclo")
            return []

        diag: dict[str, float] = {}
        signals = find_signals(
            kalshi_events,
            odds_events,
            capital_usd=self._capital_usd,
            min_edge=self._min_edge,
            diag=diag,
        )
        logger.info(
            f"motor2.shadow ciclo: kalshi={len(kalshi_events)} odds={len(odds_events)} "
            f"señales={len(signals)} live={self._odds.is_live} executor={self._executor is not None}"
        )
        # Embudo diagnóstico: cuando señales=0, distingue "mercado eficiente" (best_edge bajo,
        # matched>0) de "el matcher rechaza" (reject_names/cardinality altos) o "pocas ventanas
        # pre-match" (started_skip alto / reject_absent alto). best_edge<0 = nada evaluado.
        best_pp = diag.get("best_net_edge", -1.0) * 100
        logger.info(
            f"motor2.funnel odds={int(diag.get('odds_total', 0))} "
            f"started_skip={int(diag.get('odds_started_skip', 0))} "
            f"kalshi={int(diag.get('kalshi_total', 0))} matched={int(diag.get('events_matched', 0))} "
            f"rej_absent={int(diag.get('reject_absent', 0))} "
            f"rej_card={int(diag.get('reject_cardinality', 0))} "
            f"rej_names={int(diag.get('reject_names', 0))} "
            f"rej_nofair={int(diag.get('reject_no_fair', 0))} "
            f"best_edge={best_pp:.2f}pp umbral={self._min_edge * 100:.1f}pp"
        )
        # GATE DE DINERO REAL: persistir/apostar SOLO con odds reales (nunca sobre el fixture).
        if self._odds.is_live:
            # Memoria del embudo: una foto por ciclo (también con 0 señales — ESE es el dato
            # que el Analyst Loop necesita para trendear eficiente-vs-bug). Best-effort.
            self._persist_funnel(diag, len(signals))
            if signals:
                self._persist(signals)
                if self._executor is not None:
                    await self._execute(signals)
        return signals

    def _persist_funnel(self, diag: dict[str, float], n_signals: int) -> None:
        """Graba la foto del embudo del ciclo (Loop Engineering — memoria del diagnóstico)."""
        try:
            with get_session() as s:
                s.add(
                    Motor2FunnelSnapshot(
                        odds_total=int(diag.get("odds_total", 0)),
                        started_skip=int(diag.get("odds_started_skip", 0)),
                        kalshi_total=int(diag.get("kalshi_total", 0)),
                        events_matched=int(diag.get("events_matched", 0)),
                        reject_absent=int(diag.get("reject_absent", 0)),
                        reject_cardinality=int(diag.get("reject_cardinality", 0)),
                        reject_names=int(diag.get("reject_names", 0)),
                        reject_no_fair=int(diag.get("reject_no_fair", 0)),
                        best_net_edge_pp=diag.get("best_net_edge", -1.0) * 100,
                        signals=n_signals,
                    )
                )
                s.commit()
        except Exception:
            logger.exception("motor2.funnel.persist_error")

    async def _execute(self, signals: list[ConsensusSignal]) -> None:
        """
        Apuesta las mejores señales del ciclo (mayor edge primero), hasta MAX_BETS_PER_CYCLE.
        El RiskManager (dentro del executor) corta por exposición/stop-loss. Best-effort:
        un error de una señal se loguea y NO frena las demás ni el loop.
        """
        top = sorted(signals, key=lambda s: s.edge_pct, reverse=True)[: self.MAX_BETS_PER_CYCLE]
        for sig in top:
            try:
                outcome = await self._executor.execute(sig)  # type: ignore[union-attr]
                if outcome.filled:
                    logger.info(
                        f"motor2.bet FILLED ticker={sig.market_ticker} side={sig.kalshi_side} "
                        f"count={outcome.filled_count}"
                    )
            except Exception as e:
                logger.exception(
                    f"motor2.bet error ticker={sig.market_ticker}: {type(e).__name__}: {e}"
                )

    def _persist(self, signals: list[ConsensusSignal]) -> None:
        """
        Graba cada señal como EdgeWindow(kind="consensus") — el mismo sustrato de medición
        del shadow de arbitraje. Solo se llega acá con odds REALES. Fail-safe: un error de
        DB se loguea, nunca tira el loop.
        """
        try:
            with get_session() as s:
                for sig in signals:
                    # magnitude = edge NETO en ¢/contrato; gross/fees pueblan el desglose
                    # AUDITABLE (antes quedaban None → no se podía ver de dónde salía el edge).
                    s.add(
                        EdgeWindow(
                            market_ticker=sig.market_ticker[:100],
                            magnitude_cents=int(round(sig.edge_pct * 100)),
                            gross_spread_cents=sig.gross_edge_cents,
                            fees_cents=sig.fee_cents,
                            edge_pct=sig.edge_pct,
                            kind="consensus",
                        )
                    )
                s.commit()
        except Exception:
            logger.exception("motor2.shadow persist_error")

    async def run(self, stop_event: asyncio.Event) -> None:
        """Loop supervisado hasta stop_event. Cada ciclo es best-effort."""
        logger.info(
            f"motor2.shadow arrancado (interval={self._interval}s, "
            f"odds={'LIVE' if self._odds.is_live else 'FAKE'}, capital=${self._capital_usd})"
        )
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except Exception as e:
                logger.exception(f"motor2.shadow ciclo falló: {type(e).__name__}: {e}")
            # Espera interrumpible: el timeout es el ritmo normal del loop; un set() del
            # stop_event lo corta de inmediato (shutdown sin esperar el intervalo entero).
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
        logger.info("motor2.shadow detenido (stop_event)")
