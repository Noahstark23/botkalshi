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

from src.math.fees import kalshi_fee_cents
from src.storage.models import EdgeWindow, get_session
from src.strategies.motor_9_spillover.detector import SpilloverTracker, SpilloverTrigger

MidFn = Callable[[str], float | None]
SiblingsFn = Callable[[str], set[str]]  # tickers del MISMO evento (excluyendo el propio)
# (yes_bid, no_bid) en cents del book sano, o None. Derivación Kalshi: yes_ask = 100 − no_bid,
# no_ask = 100 − yes_bid — con los dos bids se reconstruyen las cuatro puntas ejecutables.
QuoteFn = Callable[[str], "tuple[int, int] | None"]


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
    # F2 (2026-07-28, veredicto mid: +2.69¢ t=8.2 n=518 — falta saber si es CAPTURABLE):
    # lado que un F3 compraría (inverso al salto) y sus puntas ejecutables. ask0 al trigger;
    # bid60 al madurar T+60. None = book sin punta ejecutable en ese momento (se mide solo
    # el mid — cobertura parcial honesta, jamás un precio inventado).
    exec_side: str | None = None
    ask0: int | None = None
    bid60: int | None = None


class Motor9SpilloverShadow:
    """Estado del shadow: tracker de saltos + mediciones pendientes. Best-effort TOTAL."""

    MEASURE_60 = 60.0
    MEASURE_120 = 120.0
    MEASURE_GRACE = 240.0  # book del hermano caído toda la ventana → se descarta
    # Tope de mediciones simultáneas (nada sin tope): un mercado hiperactivo no debe
    # poder crecer la lista de pendings sin límite; al tope se descarta el más viejo.
    MAX_PENDING = 500
    # F2: count al que se computa el fee del roundtrip ejecutable — el stake flat chico que
    # usaría un F3 (lección M2: el fee a count=1 sobreestima por el ceil POR ORDEN).
    EXEC_COUNT = 10

    def __init__(
        self,
        mid_fn: MidFn,
        siblings_fn: SiblingsFn,
        *,
        trigger_move_cents: float,
        window_sec: float,
        cooldown_sec: float,
        quote_fn: QuoteFn | None = None,
    ) -> None:
        self._mid = mid_fn
        self._siblings = siblings_fn
        self._quote = quote_fn  # None = sin medición ejecutable (solo mid, back-compat)
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
        # F2: lado que un F3 compraría en el hermano — INVERSO al salto (conservación de
        # probabilidad): trigger sube → hermano debe bajar → se compra NO; trigger baja → YES.
        exec_side = "no" if trig.move_cents > 0 else "yes"
        armed = 0
        for sib in sorted(siblings):
            mid0 = self._mid(sib)
            if mid0 is None:
                self._dropped += 1  # hermano sin book sano: sin experimento válido
                continue
            ask0 = self._entry_ask(sib, exec_side)
            if len(self._pending) >= self.MAX_PENDING:
                self._pending.pop(0)
                self._dropped += 1
            self._pending.append(
                _Pending(
                    trigger=trig,
                    sibling=sib,
                    mid0=mid0,
                    t0=now,
                    exec_side=exec_side if ask0 is not None else None,
                    ask0=ask0,
                )
            )
            armed += 1
        logger.info(
            f"[MOTOR 9 SHADOW] derrame trigger={trig.ticker} move={trig.move_cents:+.1f}¢ "
            f"hermanos_midiendo={armed} — follow a T+60/T+120 (NO ejecuta, F1)"
        )

    def _entry_ask(self, ticker: str, side: str) -> int | None:
        """Punta de ENTRADA ejecutable (ask del lado a comprar), en cents. Kalshi: el book
        expone yes_bid y no_bid; el ask de un lado es 100 − bid del otro. None si el book
        no tiene la punta (sin quote_fn, book stale, o lado vacío)."""
        if self._quote is None:
            return None
        q = self._quote(ticker)
        if q is None:
            return None
        yes_bid, no_bid = q
        ask = 100 - no_bid if side == "yes" else 100 - yes_bid
        return ask if 1 <= ask <= 99 else None

    def _exit_bid(self, ticker: str, side: str) -> int | None:
        """Punta de SALIDA ejecutable (bid del lado comprado) a T+60, en cents."""
        if self._quote is None:
            return None
        q = self._quote(ticker)
        if q is None:
            return None
        yes_bid, no_bid = q
        bid = yes_bid if side == "yes" else no_bid
        return bid if 1 <= bid <= 99 else None

    def _advance_measurements(self, now: float) -> None:
        """Madura las mediciones pendientes; el reloj lo empuja el propio flujo de deltas."""
        still: list[_Pending] = []
        for p in self._pending:
            age = now - p.t0
            if p.follow60 is None and age >= self.MEASURE_60:
                mid = self._mid(p.sibling)
                if mid is not None:
                    p.follow60 = mid - p.mid0  # crudo; se firma al persistir
                    # F2: la salida ejecutable se captura EN el mismo instante que el
                    # follow del mid (T+60) — mismas condiciones, comparables.
                    if p.exec_side is not None and p.bid60 is None:
                        p.bid60 = self._exit_bid(p.sibling, p.exec_side)
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
                        # UNIDAD: CENTAVOS del move del trigger, NO porcentaje (columna
                        # polimórfica; ver _EDGE_UNITS en monitoring/health.py). El trigger
                        # tiene umbral mínimo, así que tampoco hay valores entre 0 y ese
                        # umbral: no leer esta serie con buckets de puntos porcentuales.
                        edge_pct=move,
                        kind="spillover",
                        leg_states=f"src={src_suffix}"[:50],
                    )
                )
                # F2 (2026-07-28): la fila EJECUTABLE, solo si hubo puntas reales en ambos
                # extremos. gross = bid_salida − ask_entrada (lo que el mid no ve: el spread);
                # magnitude = NETO por contrato tras fee de ida y vuelta a EXEC_COUNT. La
                # DIFERENCIA entre esta serie y la del mid mide cuánto se come el spread —
                # exactamente donde murió REST arb (detectado ≠ capturable).
                if p.exec_side is not None and p.ask0 is not None and p.bid60 is not None:
                    fee_in = kalshi_fee_cents(self.EXEC_COUNT, p.ask0)
                    fee_out = kalshi_fee_cents(self.EXEC_COUNT, p.bid60)
                    gross_exec = p.bid60 - p.ask0
                    net_per_contract = gross_exec - (fee_in + fee_out) / self.EXEC_COUNT
                    db.add(
                        EdgeWindow(
                            market_ticker=p.sibling,
                            magnitude_cents=int(round(net_per_contract)),
                            gross_spread_cents=gross_exec,
                            fees_cents=fee_in + fee_out,
                            count=self.EXEC_COUNT,
                            edge_pct=move,
                            kind="spillover_exec",
                            leg_states=(f"src={src_suffix}|{p.exec_side}|a{p.ask0}b{p.bid60}"[:50]),
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
