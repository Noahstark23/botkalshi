"""
Motor 8 — detector de ORDER FLOW IMBALANCE (puro, sin red/DB/reloj propio).

TESIS A VALIDAR (F1): un desequilibrio anómalo del flujo de órdenes (deltas del WS)
precede al movimiento del precio. Se mide con z-score: el OFI de la ventana corta
(default 60s) contra la media/desvío de su propia historia reciente por ticker.

RESERVAS DOCUMENTADAS (por qué la tesis puede fallar — el shadow existe para medirlas):
  - books FINOS: en Kalshi deportes una sola orden grande "es" el book → el
    desequilibrio puede ser una persona, no presión;
  - flujo INFORMADO: cerca del partido, quien empuja suele saber algo → ir contra ese
    flujo es adverse selection (el riesgo anotado en la propuesta original).
Por eso el shadow NO asume dirección ganadora: registra la presión y el movimiento
REAL posterior (T+30/T+60), y F2 decide contrarian vs momentum vs nada CON datos.

Semántica del delta (protocolo Kalshi): side="yes" con delta>0 = sube la profundidad
de BIDS de YES = interés comprador de YES; side="no" = interés comprador de NO (=
presión vendedora sobre YES). Flujo firmado: +delta si yes, −delta si no.

PURO: recibe (ticker, side, delta, now) y devuelve señal o None. Sin time.time()
interno (el reloj lo inyecta el caller — testeable y sin sorpresas de monotonic).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class OfiSignal:
    """Desequilibrio anómalo detectado (aún sin veredicto de dirección — eso lo da F2)."""

    ticker: str
    pressure: str  # "YES" (flujo comprador de yes) | "NO"
    ofi: float  # flujo neto firmado de la ventana (contratos)
    zscore: float
    n_baseline: int  # muestras del baseline al momento de la señal


@dataclass
class _TickerState:
    flows: deque = field(default_factory=deque)  # (ts, signed_delta) dentro de la ventana
    ofi: float = 0.0  # suma corriente de la ventana (evita re-sumar)
    baseline: deque = field(default_factory=lambda: deque(maxlen=1000))  # muestras de ofi
    cooldown_until: float = 0.0


class OfiTracker:
    """Ventana rodante de OFI + z-score por ticker. Estado en memoria, nada persiste acá."""

    def __init__(
        self,
        *,
        window_sec: float,
        z_min: float,
        min_baseline: int,
        cooldown_sec: float,
    ) -> None:
        self._window = window_sec
        self._z_min = z_min
        self._min_baseline = min_baseline
        self._cooldown = cooldown_sec
        self._by_ticker: dict[str, _TickerState] = {}

    def observe(self, ticker: str, side: str, delta: int, now: float) -> OfiSignal | None:
        """Un delta del WS → actualiza la ventana; señal si |z| ≥ z_min con baseline maduro.

        El baseline se muestrea ANTES de decidir (la señal se compara contra la historia
        que NO la incluye — evita que un spike se normalice a sí mismo)."""
        st = self._by_ticker.setdefault(ticker, _TickerState())
        signed = float(delta) if side == "yes" else -float(delta)

        # Evict: sacar de la ventana lo más viejo que window_sec.
        cutoff = now - self._window
        while st.flows and st.flows[0][0] < cutoff:
            _, old = st.flows.popleft()
            st.ofi -= old

        prev_ofi = st.ofi  # foto PRE-delta → baseline sin el spike actual
        st.baseline.append(prev_ofi)
        st.flows.append((now, signed))
        st.ofi += signed

        if len(st.baseline) < self._min_baseline or now < st.cooldown_until:
            return None
        mean = sum(st.baseline) / len(st.baseline)
        var = sum((x - mean) ** 2 for x in st.baseline) / len(st.baseline)
        std = var**0.5
        if std <= 0:
            return None
        z = (st.ofi - mean) / std
        if abs(z) < self._z_min:
            return None
        st.cooldown_until = now + self._cooldown  # anti-ráfaga: una señal por episodio
        return OfiSignal(
            ticker=ticker,
            pressure="YES" if st.ofi > mean else "NO",
            ofi=st.ofi,
            zscore=z,
            n_baseline=len(st.baseline),
        )
