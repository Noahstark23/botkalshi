"""
Coordinador del Motor REST — wiring del trigger + grabación shadow de EdgeWindow.

Responsabilidad: escuchar el canal WS `ticker`, evaluar el trigger, y en shadow
GRABAR la ventana de edge en SQLite. NO ejecuta órdenes.

EL MURO DE TRADING_ENABLED (defensa en profundidad):
    - En shadow (TRADING_ENABLED=False), este motor NO construye ni instancia el
      path de ejecución. El FOKExecutor no existe aún, así que es natural ahora;
      el principio queda documentado: el path de ejecución NO se instancia con
      TRADING_ENABLED=False.
    - Segunda capa (cuando exista el FOKExecutor): cualquier invocación de la API
      de órdenes debe verificar TRADING_ENABLED lo más abajo posible (idealmente
      un guard que envuelva toda ejecución / el propio cliente REST), de modo que
      aunque el orquestador tenga un bug, la orden no salga.
    - Objetivo: que sea estructuralmente IMPOSIBLE que salga una orden en shadow,
      no que dependa de un único `if`.

Este módulo, hoy, NO importa ni referencia ningún ejecutor ni `place_order`.
"""
from __future__ import annotations

from typing import Any

from loguru import logger

from src.storage.models import EdgeWindow, get_session
from src.strategies.motor_rest_arb.trigger import TriggerSignal, evaluate_ticker
from src.utils.config import get_settings


class RestArbEngine:
    """
    Motor REST en modo shadow: detecta y graba EdgeWindow, sin ejecutar.

    Wiring (cuando MOTOR_REST_ENABLED=True): ws.on("ticker", self.on_ticker).
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self._signals_seen = 0

    async def on_ticker(self, raw_msg: dict[str, Any]) -> None:
        """Handler del canal `ticker`: evaluar trigger y, si hay señal, grabar shadow."""
        try:
            signal = evaluate_ticker(
                raw_msg,
                min_edge_cents=self.settings.MOTOR_REST_MIN_EDGE_CENTS,
                min_depth=self.settings.MOTOR_REST_MIN_DEPTH,
            )
        except Exception:
            logger.exception("motor_rest.trigger.eval_error")
            return

        if signal is None:
            return

        self._signals_seen += 1
        self._record_edge_window(signal)

    def _record_edge_window(self, signal: TriggerSignal) -> None:
        """
        Graba la ventana de edge en SQLite (sesión SÍNCRONA, patrón del proyecto).

        Shadow: solo observación. Los campos post-trade (leg_states, reconciled,
        kill_switch_fired, rollback_filled) quedan en sus defaults.
        """
        logger.info(
            f"motor_rest.edge.detected ticker={signal.market_ticker} "
            f"net_edge={signal.net_edge_cents}c gross={signal.gross_spread_cents}c "
            f"depth={signal.limiting_depth}"
        )
        try:
            with get_session() as s:
                window = EdgeWindow(
                    market_ticker=signal.market_ticker,
                    magnitude_cents=signal.net_edge_cents,       # edge NETO post-comisión
                    gross_spread_cents=signal.gross_spread_cents,  # spread BRUTO pre-comisión
                )
                s.add(window)
                s.commit()
        except Exception:
            logger.exception("motor_rest.edge.persist_error")
