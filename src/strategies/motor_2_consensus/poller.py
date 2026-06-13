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

from src.storage.models import EdgeWindow, get_session
from src.strategies.motor_2_consensus.detector import ConsensusSignal, find_signals
from src.strategies.motor_2_consensus.sources import KalshiQuoteSource, OddsSource
from src.utils.config import get_settings


class Motor2ShadowPoller:
    """Loop de detección shadow del Motor 2. No ejecuta capital."""

    DEFAULT_INTERVAL_SEC = 300.0  # 5 min — calibrable; el consenso no se mueve por segundo.

    def __init__(
        self,
        kalshi_source: KalshiQuoteSource,
        odds_source: OddsSource,
        *,
        interval_sec: float | None = None,
        capital_usd: float | None = None,
    ):
        self._kalshi = kalshi_source
        self._odds = odds_source
        self._interval = interval_sec if interval_sec is not None else self.DEFAULT_INTERVAL_SEC
        # Capital real del bot para el sizing (¼ Kelly); en prod-shadow es chico ($5).
        self._capital_usd = (
            capital_usd if capital_usd is not None else get_settings().ACTIVE_CAPITAL_USD
        )

    async def poll_once(self) -> list[ConsensusSignal]:
        """Un ciclo: extrae, cruza, detecta, (persiste si live). Devuelve las señales."""
        kalshi_events = await self._kalshi.fetch()
        if not kalshi_events:
            return []
        odds_events = await self._odds.fetch()
        if not odds_events:
            logger.info("motor2.shadow sin eventos de odds este ciclo")
            return []

        signals = find_signals(kalshi_events, odds_events, capital_usd=self._capital_usd)
        logger.info(
            f"motor2.shadow ciclo: kalshi={len(kalshi_events)} odds={len(odds_events)} "
            f"señales={len(signals)} live={self._odds.is_live}"
        )
        if signals and self._odds.is_live:
            self._persist(signals)
        return signals

    def _persist(self, signals: list[ConsensusSignal]) -> None:
        """
        Graba cada señal como EdgeWindow(kind="consensus") — el mismo sustrato de medición
        del shadow de arbitraje. Solo se llega acá con odds REALES. Fail-safe: un error de
        DB se loguea, nunca tira el loop.
        """
        try:
            with get_session() as s:
                for sig in signals:
                    s.add(
                        EdgeWindow(
                            market_ticker=sig.market_ticker[:100],
                            magnitude_cents=int(round(sig.edge_pct * 100)),
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
