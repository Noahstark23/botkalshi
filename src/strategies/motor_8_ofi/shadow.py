"""
Motor 8 — F1 SHADOW AUTO-VALIDANTE: mide la señal Y su resultado. No opera.

Sin executor, sin importar el cliente de órdenes (test-guard estructural, igual que
M6). Pasajero del handler de deltas de data_capture: recibe cada delta ya parseado
(cero requests extra, cero disco de deltas — la lección de los 57GB).

AUTO-VALIDACIÓN (lo distintivo de este shadow): al detectar un desequilibrio anómalo
guarda el MID actual del book (del OrderbookManagerV2, en memoria) y agenda dos
mediciones: T+30s y T+60s. Cuando ambas maduran (empujadas por el propio flujo de
deltas — sin task nuevo), persiste UNA EdgeWindow kind="ofi" con el movimiento REAL
firmado DESDE la presión. F2 lee esa tabla y decide con datos: si move>0 domina →
momentum; move<0 → contrarian; ~0 → no hay señal y se archiva.

Mapping EdgeWindow (los campos son del Motor REST — acá se documenta el reuso):
  kind="ofi" · market_ticker · edge_pct=z-score · count=OFI neto (contratos, int)
  gross_spread_cents=move a T+30 (¢, firmado desde la presión)
  magnitude_cents=move a T+60 (¢, firmado desde la presión)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

from src.storage.models import EdgeWindow, get_session
from src.strategies.motor_8_ofi.detector import OfiSignal, OfiTracker

# Callable inyectado que devuelve el MID del ticker en cents (float) o None si el book
# no está disponible/sano. La implementación real lee el OrderbookManagerV2.
MidFn = Callable[[str], float | None]


@dataclass
class _Pending:
    signal: OfiSignal
    mid0: float
    t0: float
    mid30: float | None = None


class Motor8OfiShadow:
    """Estado del shadow: tracker OFI + mediciones pendientes. Best-effort TOTAL."""

    MEASURE_30 = 30.0
    MEASURE_60 = 60.0
    MEASURE_GRACE = 120.0  # si a T+grace no se pudo medir (book caído), se descarta

    def __init__(
        self,
        mid_fn: MidFn,
        *,
        window_sec: float,
        z_min: float,
        min_baseline: int,
        cooldown_sec: float,
    ) -> None:
        self._mid = mid_fn
        self._tracker = OfiTracker(
            window_sec=window_sec,
            z_min=z_min,
            min_baseline=min_baseline,
            cooldown_sec=cooldown_sec,
        )
        self._pending: list[_Pending] = []
        self._signals_seen = 0
        self._measured = 0
        self._dropped = 0

    def observe_delta(self, ticker: str, side: str, delta: int, now: float | None = None) -> None:
        """Un delta del WS: alimenta el tracker + empuja las mediciones maduras.

        Best-effort TOTAL: cualquier error se loguea y NO propaga — este código corre
        dentro del handler del feed y jamás puede romper la captura (Lección 7)."""
        try:
            now = time.monotonic() if now is None else now
            self._advance_measurements(now)
            sig = self._tracker.observe(ticker, side, delta, now)
            if sig is None:
                return
            mid0 = self._mid(sig.ticker)
            if mid0 is None:
                self._dropped += 1  # sin book sano no hay experimento válido
                return
            self._signals_seen += 1
            self._pending.append(_Pending(signal=sig, mid0=mid0, t0=now))
            logger.info(
                f"[MOTOR 8 SHADOW] ofi ticker={sig.ticker} presión={sig.pressure} "
                f"z={sig.zscore:+.2f} ofi={sig.ofi:+.0f} mid0={mid0:.1f}¢ — midiendo "
                f"T+30/T+60 (NO ejecuta, F1)"
            )
        except Exception:
            logger.exception("motor8.shadow observe_delta falló (la captura sigue)")

    def _advance_measurements(self, now: float) -> None:
        """Madura las mediciones pendientes. El reloj lo empuja el flujo de deltas: sin
        deltas no hay mercado moviéndose y la medición tardía sigue siendo válida."""
        still: list[_Pending] = []
        for p in self._pending:
            age = now - p.t0
            if p.mid30 is None and age >= self.MEASURE_30:
                p.mid30 = self._mid(p.signal.ticker)  # None → se reintenta hasta grace
            if age >= self.MEASURE_60:
                mid60 = self._mid(p.signal.ticker)
                if mid60 is not None and p.mid30 is not None:
                    self._persist(p, mid60)
                    self._measured += 1
                    continue
                if age >= self.MEASURE_GRACE:
                    self._dropped += 1  # book caído toda la ventana → sin resultado
                    continue
            still.append(p)
        self._pending = still

    def _persist(self, p: _Pending, mid60: float) -> None:
        """EdgeWindow kind='ofi' — la señal CON su resultado. Firmado desde la presión:
        move > 0 = el precio siguió a la presión (momentum); < 0 = la contradijo."""
        sign = 1.0 if p.signal.pressure == "YES" else -1.0
        move30 = (p.mid30 - p.mid0) * sign if p.mid30 is not None else 0.0
        move60 = (mid60 - p.mid0) * sign
        try:
            with get_session() as db:
                db.add(
                    EdgeWindow(
                        market_ticker=p.signal.ticker,
                        magnitude_cents=int(round(move60)),
                        gross_spread_cents=int(round(move30)),
                        fees_cents=None,
                        count=int(max(min(p.signal.ofi, 2**30), -(2**30))),
                        # UNIDAD: z-score, NO porcentaje (la columna se llama edge_pct por
                        # los kinds binarios). Además el detector solo emite con |z| >= z_min,
                        # así que esta serie NO tiene valores entre 0 y z_min: leerla con
                        # buckets de puntos porcentuales produce un hueco que parece
                        # artefacto de datos y no lo es (_EDGE_UNITS en monitoring/health.py).
                        edge_pct=p.signal.zscore,
                        kind="ofi",
                    )
                )
                db.commit()
        except Exception:
            logger.exception("motor8.shadow persist_error (se sigue)")

    def stats(self) -> dict[str, int]:
        return {
            "signals": self._signals_seen,
            "measured": self._measured,
            "dropped": self._dropped,
            "pending": len(self._pending),
        }
