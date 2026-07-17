"""
Motor 6 — F1 SHADOW: observa, loguea y graba. ESTRUCTURALMENTE incapaz de operar.

Este módulo NO tiene executor, NO importa el cliente REST de Kalshi y NO conoce la
API de órdenes — la Capa A llevada al extremo: en F1 el path de ejecución no existe
(hay un test-guard que rompe si alguien lo importa acá). Pasajero del ciclo de M2:
recibe (quotes, fair) ya extraídos → CERO requests extra a Kalshi o The Odds API.

Salidas del shadow (las que exige el protocolo para validar ANTES de F2/F3):
  - log `[MOTOR 6 SHADOW] ... net=$` con fees reales, por señal;
  - EdgeWindow kind="linemove" (solo con odds LIVE — señales del fixture fake no
    son data) → sustrato para medir frecuencia y ROI simulado contra settlements.
"""

from __future__ import annotations

from loguru import logger

from src.storage.models import EdgeWindow, get_session
from src.strategies.motor_2_consensus.detector import KalshiEventQuotes
from src.strategies.motor_6_linemove.detector import LineMoveSignal, find_linemove_signals


class Motor6LineMoveShadow:
    """Estado mínimo del shadow: la foto previa del fair (para el delta entre ciclos)."""

    def __init__(
        self,
        *,
        move_min_pp: float,
        edge_min_pp: float,
        max_edge_pp: float,
        stake_usd: float = 1.80,
    ) -> None:
        self._move_min_pp = move_min_pp
        self._edge_min_pp = edge_min_pp
        self._max_edge_pp = max_edge_pp
        # Solo para el net=$ del log shadow (dimensiona como M2: stake flat chico).
        self._stake_usd = stake_usd
        self._fair_prev: dict[str, float] = {}
        self._signals_seen = 0

    def observe(
        self,
        kalshi_events: list[KalshiEventQuotes],
        fair_now: dict[str, float],
        *,
        is_live: bool,
    ) -> list[LineMoveSignal]:
        """Un tick del shadow: detecta line-moves entre la foto previa y la actual.

        Best-effort TOTAL: cualquier error se loguea y devuelve [] — M6 es pasajero
        del loop de M2 y JAMÁS debe romper el ciclo del host. La foto previa se
        actualiza SIEMPRE (aunque no haya señales) para que el delta sea ciclo-a-ciclo.
        """
        try:
            quotes = {q.market_ticker: q for ev in kalshi_events for q in ev.outcomes}
            signals = (
                find_linemove_signals(
                    quotes,
                    fair_now,
                    self._fair_prev,
                    move_min_pp=self._move_min_pp,
                    edge_min_pp=self._edge_min_pp,
                    max_edge_pp=self._max_edge_pp,
                )
                if self._fair_prev
                else []  # primer ciclo: sin foto previa no hay delta
            )
            for s in signals:
                self._signals_seen += 1
                count = max(1, int(self._stake_usd * 100 // s.ask_cents))
                net_usd = count * s.net_edge_pp / 100.0
                logger.info(
                    f"[MOTOR 6 SHADOW] ticker={s.market_ticker} side={s.side} "
                    f"move={s.move_pp:+.2f}pp ask={s.ask_cents}c fee={s.fee_cents}c "
                    f"net={s.net_edge_pp:+.2f}pp (~${net_usd:+.2f} a {count} contratos) "
                    f"— NO ejecuta (F1)"
                )
                if is_live:
                    self._persist(s)
            return signals
        except Exception:
            logger.exception("motor6.shadow observe falló (el ciclo de M2 sigue)")
            return []
        finally:
            # Actualizar SIEMPRE: si un ciclo falla, el próximo delta parte de la foto
            # más fresca disponible en vez de acumular un salto de dos ciclos (falso move).
            self._fair_prev = dict(fair_now)

    def _persist(self, s: LineMoveSignal) -> None:
        """EdgeWindow kind='linemove' — el registro con el que F2 se valida. Best-effort."""
        try:
            with get_session() as db:
                db.add(
                    EdgeWindow(
                        market_ticker=s.market_ticker,
                        magnitude_cents=int(round(s.net_edge_pp)),  # neto por contrato (≈¢)
                        gross_spread_cents=int(round(s.net_edge_pp + s.fee_cents)),
                        fees_cents=s.fee_cents,
                        count=1,
                        edge_pct=s.net_edge_pp,
                        kind="linemove",
                    )
                )
                db.commit()
        except Exception:
            logger.exception("motor6.shadow persist_error (se sigue)")
