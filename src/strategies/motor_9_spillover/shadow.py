"""
Motor 9 "Derrame" — F1 SHADOW AUTO-VALIDANTE: mide el trigger Y el follow-through real.

Sin executor, sin importar el cliente de órdenes (test-guard estructural, igual que M6/M8).
Pasajero del handler de deltas de data_capture (cero requests extra, cero disco de deltas).

AUTO-VALIDACIÓN (el corazón del diseño): al detectar un salto en el ticker A, captura el mid
de cada HERMANO del mismo evento EN ESE INSTANTE (mid0) y agenda mediciones a T+60 y T+120.
El follow-through se firma desde la dirección ESPERADA (la INVERSA del salto — conservación
de probabilidad en outcomes del mismo evento): follow > 0 = el hermano ajustó como se
esperaba DESPUÉS del trigger (derrame rezagado = capturable); ~0 = ajuste instantáneo o sin
propagación (nada que capturar); < 0 = se movió al revés (la tesis está mal). Como mid0 se
toma AL trigger, lo que se mide es exactamente la parte capturable — por construcción.

Cooldown por EVENTO: el ajuste del hermano dispararía un trigger espejo (cascada); tras un
trigger en el evento E, ningún ticker de E re-dispara hasta que venza el cooldown.

Mapping EdgeWindow (campos del Motor REST — reuso documentado, patrón M8):
  kind="spillover" · market_ticker=HERMANO (el candidato rezagado, donde se compraría)
  edge_pct=move del trigger en ¢ (firmado) · count=int(move del trigger)
  gross_spread_cents=follow del hermano a T+60 (¢, firmado desde la dirección esperada)
  magnitude_cents=follow a T+120 · leg_states="src=<sufijo del ticker que saltó>"
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger

from src.storage.models import EdgeWindow, get_session
from src.strategies.motor_9_spillover.detector import SpilloverTracker, SpilloverTrigger

MidFn = Callable[[str], float | None]
SiblingsFn = Callable[[str], set[str]]  # tickers del MISMO evento (excluyendo el propio)


def event_key_of(market_ticker: str) -> str:
    """Evento (partido) de un market ticker: todo menos el sufijo de outcome. Local al
    paquete (sin cross-import) — misma convención que event_ticker_of de M1."""
    return market_ticker.rsplit("-", 1)[0] if "-" in market_ticker else market_ticker


@dataclass
class _Pending:
    trigger: SpilloverTrigger
    sibling: str
    mid0: float
    t0: float
    follow60: float | None = None


class Motor9SpilloverShadow:
    """Estado del shadow: tracker de saltos + mediciones pendientes. Best-effort TOTAL."""

    MEASURE_60 = 60.0
    MEASURE_120 = 120.0
    MEASURE_GRACE = 240.0  # book del hermano caído toda la ventana → se descarta
    # Tope de mediciones simultáneas (nada sin tope): un mercado hiperactivo no debe
    # poder crecer la lista de pendings sin límite; al tope se descarta el más viejo.
    MAX_PENDING = 500

    def __init__(
        self,
        mid_fn: MidFn,
        siblings_fn: SiblingsFn,
        *,
        trigger_move_cents: float,
        window_sec: float,
        cooldown_sec: float,
    ) -> None:
        self._mid = mid_fn
        self._siblings = siblings_fn
        self._cooldown = cooldown_sec
        self._tracker = SpilloverTracker(
            trigger_move_cents=trigger_move_cents,
            window_sec=window_sec,
            cooldown_sec=cooldown_sec,
        )
        self._event_cooldown_until: dict[str, float] = {}
        self._pending: list[_Pending] = []
        self._triggers_seen = 0
        self._measured = 0
        self._dropped = 0

    def observe(self, ticker: str, now: float | None = None) -> None:
        """Un delta del WS ya aplicado al book: lee el mid actual, alimenta el tracker y
        empuja las mediciones maduras. Best-effort TOTAL (Lección 7): jamás rompe el feed."""
        try:
            now = time.monotonic() if now is None else now
            self._advance_measurements(now)
            mid = self._mid(ticker)
            if mid is None:
                return  # book stale/cuarentena: ni se alimenta el tracker (mid basura)
            trig = self._tracker.observe(ticker, mid, now)
            if trig is None:
                return
            event = event_key_of(ticker)
            if now < self._event_cooldown_until.get(event, 0.0):
                return  # cascada: el ajuste de un hermano no es un trigger nuevo
            self._event_cooldown_until[event] = now + self._cooldown
            self._on_trigger(trig, now)
        except Exception:
            logger.exception("motor9.shadow observe falló (la captura sigue)")

    def _on_trigger(self, trig: SpilloverTrigger, now: float) -> None:
        siblings = self._siblings(trig.ticker)
        if not siblings:
            return  # evento de 1 solo market trackeado: no hay a quién derramar
        self._triggers_seen += 1
        armed = 0
        for sib in sorted(siblings):
            mid0 = self._mid(sib)
            if mid0 is None:
                self._dropped += 1  # hermano sin book sano: sin experimento válido
                continue
            if len(self._pending) >= self.MAX_PENDING:
                self._pending.pop(0)
                self._dropped += 1
            self._pending.append(_Pending(trigger=trig, sibling=sib, mid0=mid0, t0=now))
            armed += 1
        logger.info(
            f"[MOTOR 9 SHADOW] derrame trigger={trig.ticker} move={trig.move_cents:+.1f}¢ "
            f"hermanos_midiendo={armed} — follow a T+60/T+120 (NO ejecuta, F1)"
        )

    def _advance_measurements(self, now: float) -> None:
        """Madura las mediciones pendientes; el reloj lo empuja el propio flujo de deltas."""
        still: list[_Pending] = []
        for p in self._pending:
            age = now - p.t0
            if p.follow60 is None and age >= self.MEASURE_60:
                mid = self._mid(p.sibling)
                if mid is not None:
                    p.follow60 = mid - p.mid0  # crudo; se firma al persistir
            if age >= self.MEASURE_120:
                mid120 = self._mid(p.sibling)
                if mid120 is not None and p.follow60 is not None:
                    self._persist(p, mid120)
                    self._measured += 1
                    continue
                if age >= self.MEASURE_GRACE:
                    self._dropped += 1
                    continue
            still.append(p)
        self._pending = still

    def _persist(self, p: _Pending, mid120: float) -> None:
        """EdgeWindow kind='spillover' — el trigger CON el follow-through del hermano.
        Firmado desde la dirección ESPERADA (inversa del salto): follow > 0 = el hermano
        ajustó como la conservación de probabilidad predice, DESPUÉS del trigger."""
        expected_sign = -1.0 if p.trigger.move_cents > 0 else 1.0
        follow60 = (p.follow60 or 0.0) * expected_sign
        follow120 = (mid120 - p.mid0) * expected_sign
        src_suffix = p.trigger.ticker.rsplit("-", 1)[-1]
        move = p.trigger.move_cents
        try:
            with get_session() as db:
                db.add(
                    EdgeWindow(
                        market_ticker=p.sibling,
                        magnitude_cents=int(round(follow120)),
                        gross_spread_cents=int(round(follow60)),
                        fees_cents=None,
                        count=int(max(min(move, 99), -99)),
                        edge_pct=move,
                        kind="spillover",
                        leg_states=f"src={src_suffix}"[:50],
                    )
                )
                db.commit()
        except Exception:
            logger.exception("motor9.shadow persist_error (se sigue)")

    def stats(self) -> dict[str, int]:
        return {
            "triggers": self._triggers_seen,
            "measured": self._measured,
            "dropped": self._dropped,
            "pending": len(self._pending),
        }
