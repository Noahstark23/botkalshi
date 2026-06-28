"""
OrderbookManagerV2: WS-based recovery, buffer-and-drain, fail-loud.

Differences from OrderbookManager (v1):
  - Recovery via WS update_subscription/get_snapshot — no REST dependency.
  - Buffer-and-drain: messages during recovery are queued and applied post-snapshot.
  - mark_stale() instead of clear() before recovery snapshot arrives.
  - SidGapError and OrderbookDesyncError propagate to caller (fail-loud).
  - No asyncio.Lock — single-threaded asyncio guarantees sequential execution.
  - No try/except Exception: pass — from 2026-05-09 incident learnings.

Recovery flow on gap detection:
  1. handle_message detects new_seq != last_seq + 1 → calls _start_recovery(sid).
  2. _start_recovery: all tickers in sid → mark_stale(); sends WS get_snapshot command.
  3. The gap-triggering message is buffered in _pending_deltas[sid].
  4. All subsequent messages for sid in _recovering → buffered.
  5. When orderbook_snapshot arrives with `id` in _pending_snapshot_requests:
     - apply_snapshot on the state.
     - Ticker removed from pending set.
     - If set empty: sid exits _recovering, buffer is drained.
  6. Drain: messages sorted by seq, those <= snapshot seq discarded, rest applied.
  7. SidGapError raised AFTER recovery is initiated (caller sees the error).
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from loguru import logger

from src.monitoring.health import BotState
from src.strategies.data_capture import parse_price_to_cents, parse_size
from src.strategies.motor_1_arbitrage.orderbook import (
    BookTop,
    OrderbookDesyncError,
    OrderbookError,
    OrderbookState,
)

if TYPE_CHECKING:
    from src.clients.kalshi_ws import KalshiWebSocket


class SidGapError(OrderbookError):
    """
    Gap detected in the global seq of a sid.

    Raised by OrderbookManagerV2.handle_message when new_seq != last_seq + 1.
    Recovery is initiated before raise: all tickers in sid are marked stale
    and a WS get_snapshot command is sent.

    Attributes:
        sid: Subscription ID where gap was detected.
        expected_seq: The seq we expected (last_seq + 1).
        received_seq: The seq we received.
    """

    def __init__(self, sid: int, expected_seq: int, received_seq: int) -> None:
        self.sid = sid
        self.expected_seq = expected_seq
        self.received_seq = received_seq
        super().__init__(f"Sid {sid} gap: expected seq={expected_seq}, got {received_seq}")


class OrderbookManagerV2:
    """
    Maintains OrderbookState per ticker, fed exclusively by WebSocket.

    WS-based recovery: on gap detection, sends update_subscription/get_snapshot
    and buffers messages until recovery snapshot arrives.

    Usage:
        manager = OrderbookManagerV2(ws)
        ws.on("orderbook_delta", manager.handle_message)
        ws.on("orderbook_snapshot", manager.handle_message)
        ws.on("ok", manager.handle_message)
        ws.on("error", manager.handle_message)
        # Then start ws.run()
    """

    # Watchdog de recovery (anti-leak): una recovery que nunca completa (snapshot perdido
    # en un feed degradado, ticker settled que no devuelve snapshot) dejaría el sid en
    # _recovering PARA SIEMPRE, y _pending_deltas[sid] crecería a la tasa del feed sin
    # límite → OOM. El watchdog aborta una recovery atascada por TIMEOUT o por TAMAÑO de
    # buffer, descarta lo encolado (logueando sid + cuántos mensajes) y RE-PIDE el snapshot.
    DEFAULT_RECOVERY_TIMEOUT_SEC = 30.0
    DEFAULT_MAX_RECOVERY_BUFFER = 5000

    def __init__(
        self,
        ws: KalshiWebSocket,
        *,
        recovery_timeout_sec: float = DEFAULT_RECOVERY_TIMEOUT_SEC,
        max_recovery_buffer: int = DEFAULT_MAX_RECOVERY_BUFFER,
    ) -> None:
        self._ws = ws
        self._recovery_timeout_sec = recovery_timeout_sec
        self._max_recovery_buffer = max_recovery_buffer
        self._books: dict[str, OrderbookState] = {}
        self._last_seq_by_sid: dict[int, int] = {}
        self._tickers_by_sid: dict[int, set[str]] = {}
        self._recovering: set[int] = set()
        self._pending_deltas: dict[int, list[dict]] = {}
        # sid → time.monotonic() de inicio de la recovery EN CURSO (para el watchdog).
        self._recovery_started_at: dict[int, float] = {}
        # Deltas que llegan antes del snapshot inicial de un ticker (bootstrap).
        # Se encolan por ticker y se drenan en _drain_bootstrap_buffer al llegar
        # el snapshot, en vez de descartarse (causa del desync de attempt #3).
        self._bootstrap_buffer: dict[str, list[dict]] = {}
        # req_id → (sid, set_of_pending_tickers_awaiting_recovery_snapshot)
        self._pending_snapshot_requests: dict[int, tuple[int, set[str]]] = {}
        # Gap rate tracking (monotonic timestamps within last 60s)
        self._gap_timestamps: list[float] = []
        self._consecutive_warning: int = 0
        self._consecutive_critical: int = 0
        # "Nunca alertó" = -inf, NO 0.0: el throttle compara now - last > THRESHOLD con
        # now = time.monotonic() (segundos desde el boot). Con 0.0, en un proceso recién
        # arrancado (uptime < THRESHOLD, ej. container/CI fresco) now-0 < THRESHOLD →
        # bloquearía POR ERROR la primera alerta durante los primeros minutos de vida.
        # -inf hace que la primera alerta SIEMPRE pase, sin depender del uptime absoluto.
        self._last_warning_alert_at: float = float("-inf")
        self._last_critical_alert_at: float = float("-inf")
        self._last_gap_at: datetime | None = None

    # =====================================================
    # Public API
    # =====================================================

    async def handle_message(self, raw_msg: dict) -> None:
        """
        Main dispatch for WS messages.

        Handles: orderbook_snapshot, orderbook_delta, ok, error, subscribed.
        Unknown types silently ignored.

        Raises:
            SidGapError: gap in sid seq. Recovery initiated before raise.
            OrderbookDesyncError: delta produces new_qty < 0 (feed corruption).
            ValueError: malformed message (required field missing).
        """
        msg_type = raw_msg.get("type")

        if msg_type == "ok":
            return

        if msg_type == "error":
            code = raw_msg.get("code", "?")
            err_text = raw_msg.get("msg", "")
            logger.error(f"WS error code={code}: {err_text}")
            BotState.record_error(f"WS error code={code}: {err_text}")
            return

        if msg_type == "subscribed":
            return  # Has sid but no seq — skip gap detection

        if msg_type not in ("orderbook_snapshot", "orderbook_delta"):
            return

        try:
            sid: int = raw_msg["sid"]
            new_seq: int = raw_msg["seq"]
            ticker: str = raw_msg["msg"]["market_ticker"]
        except KeyError as e:
            raise ValueError(f"Malformed WS message missing field {e}: {raw_msg}") from e

        # Register ticker in its sid (always, before buffering or gap check)
        self._tickers_by_sid.setdefault(sid, set()).add(ticker)

        # Recovery snapshot: has `id` matching a pending request
        if msg_type == "orderbook_snapshot" and "id" in raw_msg:
            req_id = raw_msg["id"]
            if req_id in self._pending_snapshot_requests:
                await self._handle_recovery_snapshot(raw_msg, req_id)
                return

        # Fallback por ticker/sid (incidente 2026-05-28): un orderbook_snapshot para un sid EN
        # recovery que NO trae el id del comando (Kalshi puede omitirlo o eco-devolver uno viejo
        # en reconnects) igual COMPLETA la recovery — se rutea por ticker/sid, no solo por id.
        # Sin esto el snapshot caería al buffer de abajo y el sid quedaría atascado en _recovering
        # (book stale + buffer creciendo hasta que aborta el watchdog). La invariante "el snapshot
        # siempre eco-devuelve el id" no está garantizada por los docs públicos.
        if msg_type == "orderbook_snapshot" and sid in self._recovering:
            req_id = self._pending_req_id_for_sid(sid)
            if req_id is not None:
                await self._handle_recovery_snapshot(raw_msg, req_id)
                return

        # Buffer all messages while sid is recovering
        if sid in self._recovering:
            # Watchdog anti-leak: si la recovery lleva demasiado tiempo o el buffer creció
            # demasiado, está atascada (snapshot perdido en feed degradado) → abortar y
            # reintentar ANTES de seguir encolando, para que el buffer no crezca sin límite.
            await self._guard_stuck_recovery(sid)
            self._pending_deltas.setdefault(sid, []).append(raw_msg)
            return

        # Gap detection
        if sid in self._last_seq_by_sid:
            expected_seq = self._last_seq_by_sid[sid] + 1
            if new_seq != expected_seq:
                await self._start_recovery(sid)
                self._pending_deltas[sid].append(raw_msg)
                alert_args = self._record_gap_and_should_alert()
                if alert_args:
                    asyncio.create_task(self._fire_alert(*alert_args))
                raise SidGapError(sid=sid, expected_seq=expected_seq, received_seq=new_seq)

        if msg_type == "orderbook_snapshot":
            self._apply_snapshot_msg(raw_msg)
            applied = True
        else:
            # Devuelve False si encoló el delta (ticker sin snapshot inicial):
            # en ese caso NO se avanza el baseline del sid, para que el snapshot
            # posterior no se interprete como gap.
            applied = self._apply_delta_msg(raw_msg)  # May raise OrderbookDesyncError
        if applied:
            self._last_seq_by_sid[sid] = max(self._last_seq_by_sid.get(sid, 0), new_seq)

    @property
    def tracked_tickers(self) -> frozenset[str]:
        """Snapshot of tickers with an active OrderbookState (may be stale/uninitialized)."""
        return frozenset(self._books.keys())

    def get_top_of_book(self, ticker: str, side: Literal["yes", "no"]) -> BookTop | None:
        """Top of book. Returns None if ticker is unknown, stale, or uninitialized."""
        state = self._books.get(ticker)
        if state is None or not state.is_initialized:
            return None
        return state.top_of_book(side)

    def stats(self) -> dict:
        """Internal state for debugging via /status."""
        now = time.monotonic()
        gaps_last_60s = sum(1 for t in self._gap_timestamps if now - t < 60.0)
        return {
            "tracked_tickers": len(self._books),
            "initialized_tickers": sum(1 for b in self._books.values() if b.is_initialized),
            "stale_tickers": sum(1 for b in self._books.values() if b.is_stale),
            "recovering_sids": list(self._recovering),
            "pending_buffer_msgs": sum(len(b) for b in self._pending_deltas.values()),
            "sids": list(self._last_seq_by_sid.keys()),
            "last_seq_by_sid": dict(self._last_seq_by_sid),
            "gaps_last_60s": gaps_last_60s,
            "last_gap_at": self._last_gap_at.isoformat() if self._last_gap_at else None,
        }

    # =====================================================
    # Gap rate tracking and alerting
    # =====================================================

    def _record_gap_and_should_alert(self) -> tuple[str, str] | None:
        """
        Record this gap occurrence and decide if an alert should fire.

        Returns (kind, details) if an alert should be sent, None otherwise.
        Called synchronously from handle_message before the SidGapError raise.
        Caller (async) schedules _fire_alert via asyncio.create_task.
        """
        now = time.monotonic()
        self._gap_timestamps.append(now)
        self._gap_timestamps = [t for t in self._gap_timestamps if now - t < 60.0]
        self._last_gap_at = datetime.now(UTC)
        count = len(self._gap_timestamps)

        if count >= 20:
            self._consecutive_critical += 1
            self._consecutive_warning += 1
        elif count >= 5:
            self._consecutive_critical = 0
            self._consecutive_warning += 1
        else:
            self._consecutive_critical = 0
            self._consecutive_warning = 0

        details = f"gaps_last_60s={count}"

        if self._consecutive_critical >= 3 and now - self._last_critical_alert_at > 120.0:
            self._last_critical_alert_at = now
            return "sid_gap_critical", details

        if self._consecutive_warning >= 3 and now - self._last_warning_alert_at > 300.0:
            self._last_warning_alert_at = now
            return "sid_gap_warning", details

        return None

    async def _fire_alert(self, kind: str, details: str) -> None:
        """Fire-and-forget alert. Catches all exceptions to protect the caller."""
        try:
            from src.monitoring.telegram_alerts import alert_orderbook_anomaly

            await alert_orderbook_anomaly(kind, details)
        except Exception as e:
            logger.warning(f"v2.alert_send_failed kind={kind} error={e}")

    # =====================================================
    # Recovery
    # =====================================================

    async def _start_recovery(self, sid: int) -> None:
        """Mark all tickers in sid as stale and send WS get_snapshot command."""
        tickers = list(self._tickers_by_sid.get(sid, set()))
        # Gap individual = evento benigno AUTO-RECUPERADO (resync por snapshot) → INFO, no
        # CRITICAL (era ruido: ~32/día). La escalada por FRECUENCIA anormal sigue intacta
        # vía _record_gap_and_should_alert (Telegram sid_gap_warning/critical a umbrales).
        logger.info(
            f"Sid {sid} gap detected (auto-recovery). Marking {len(tickers)} tickers stale, "
            "requesting WS recovery snapshot."
        )

        self._recovering.add(sid)
        self._pending_deltas[sid] = []
        self._recovery_started_at[sid] = time.monotonic()

        for ticker in tickers:
            if ticker in self._books:
                self._books[ticker].mark_stale()

        if not tickers:
            return

        req_id = await self._ws.send_command(
            "update_subscription",
            action="get_snapshot",
            params={"market_tickers": tickers, "sids": [sid]},
        )
        self._pending_snapshot_requests[req_id] = (sid, set(tickers))

    async def _guard_stuck_recovery(self, sid: int) -> None:
        """
        Watchdog anti-leak: aborta+reintenta una recovery atascada del sid.

        Se llama por cada mensaje que se encolaría durante recovery. Dispara si la recovery
        excede `recovery_timeout_sec`, O si el buffer ya alcanzó `max_recovery_buffer`. Sin
        esto, una recovery que nunca recibe su snapshot (feed degradado / ticker settled)
        deja el sid en _recovering para siempre y _pending_deltas[sid] crece sin tope → OOM.
        """
        started = self._recovery_started_at.get(sid)
        timed_out = (
            started is not None and (time.monotonic() - started) >= self._recovery_timeout_sec
        )
        overflow = len(self._pending_deltas.get(sid, [])) >= self._max_recovery_buffer
        if timed_out or overflow:
            await self._abort_and_restart_recovery(
                sid, "timeout" if timed_out else "buffer_overflow"
            )

    async def _abort_and_restart_recovery(self, sid: int, reason: str) -> None:
        """
        Tira la recovery atascada del sid (descarta el buffer) y arranca una nueva.

        Loguea WARNING con el sid y CUÁNTOS mensajes se descartaron: si esto se dispara
        seguido en producción, es la señal de que el feed está degradado y hay que mirar la
        causa upstream (no es un fix de raíz, es la red de seguridad anti-OOM). Re-pide el
        snapshot vía _start_recovery → buffer y timer frescos, así el watchdog vuelve a
        acotar el próximo intento (cada ciclo queda bounded por timeout/tamaño).
        """
        discarded = len(self._pending_deltas.get(sid, []))
        # Limpiar requests de snapshot viejos de este sid (un snapshot tardío del intento
        # abortado NO debe drenar el buffer del intento nuevo).
        stale_reqs = [r for r, (s, _) in self._pending_snapshot_requests.items() if s == sid]
        for r in stale_reqs:
            del self._pending_snapshot_requests[r]
        self._recovering.discard(sid)
        self._pending_deltas.pop(sid, None)
        self._recovery_started_at.pop(sid, None)
        logger.warning(
            f"v2.recovery_aborted sid={sid} reason={reason} discarded_msgs={discarded} → "
            "reintentando snapshot. Si se repite seguido = feed degradado (mirar upstream)."
        )
        await self._start_recovery(sid)

    def _pending_req_id_for_sid(self, sid: int) -> int | None:
        """req_id de la solicitud de snapshot EN CURSO para este sid (para completar la recovery
        de un snapshot sin id/ id que no coincide). None si el sid no tiene request pendiente."""
        for req_id, (req_sid, _tickers) in self._pending_snapshot_requests.items():
            if req_sid == sid:
                return req_id
        return None

    async def _handle_recovery_snapshot(self, raw_msg: dict, req_id: int) -> None:
        """Apply a recovery snapshot. Drain buffer when all tickers in the sid recovered."""
        sid, tickers_pending = self._pending_snapshot_requests[req_id]
        ticker: str = raw_msg["msg"]["market_ticker"]

        self._apply_snapshot_msg(raw_msg)
        tickers_pending.discard(ticker)

        if not tickers_pending:
            del self._pending_snapshot_requests[req_id]
            self._recovering.discard(sid)
            self._recovery_started_at.pop(sid, None)
            self._drain_buffer(sid)

    def _drain_buffer(self, sid: int) -> None:
        """Apply buffered messages after recovery. Messages at/below snapshot seq discarded."""
        buffered = self._pending_deltas.pop(sid, [])
        if not buffered:
            return

        buffered.sort(key=lambda m: m["seq"])

        # Update sid baseline to the max snapshot seq across all recovered tickers
        max_seq = max(
            (
                self._books[t].sequence
                for t in self._tickers_by_sid.get(sid, set())
                if t in self._books
            ),
            default=self._last_seq_by_sid.get(sid, 0),
        )
        self._last_seq_by_sid[sid] = max_seq

        for msg in buffered:
            msg_seq = msg["seq"]
            msg_ticker = msg["msg"]["market_ticker"]
            state = self._books.get(msg_ticker)

            if state is None or not state.is_initialized:
                continue
            if msg_seq <= state.sequence:
                continue

            if msg["type"] == "orderbook_snapshot":
                self._apply_snapshot_msg(msg)
            elif msg["type"] == "orderbook_delta":
                self._apply_delta_msg(msg)

            if msg_seq > self._last_seq_by_sid.get(sid, 0):
                self._last_seq_by_sid[sid] = msg_seq

    # =====================================================
    # Internal apply helpers
    # =====================================================

    def _apply_snapshot_msg(self, raw_msg: dict) -> None:
        """Apply WS orderbook_snapshot to state. Creates state entry if missing."""
        msg = raw_msg["msg"]
        ticker: str = msg["market_ticker"]
        seq: int = raw_msg["seq"]

        yes_raw = msg.get("yes_dollars_fp") or msg.get("yes") or []
        no_raw = msg.get("no_dollars_fp") or msg.get("no") or []

        logger.info(
            f"V2 snapshot: ticker={ticker} seq={seq} "
            f"num_yes={len(yes_raw)} num_no={len(no_raw)} "
            f"sample_yes={yes_raw[:3]} sample_no={no_raw[:3]}"
        )
        logger.debug(f"V2 snapshot raw: ticker={ticker} payload={msg!r}")
        # Observabilidad: envelope completo (incluye sid/seq/type/id) antes de procesar.
        logger.debug(f"V2 snapshot raw_msg (full envelope): ticker={ticker} raw_msg={raw_msg!r}")

        yes_levels = _parse_fp_levels(yes_raw, ticker, "yes")
        no_levels = _parse_fp_levels(no_raw, ticker, "no")

        if ticker not in self._books:
            self._books[ticker] = OrderbookState(ticker)

        self._books[ticker].apply_snapshot({"seq": seq, "yes": yes_levels, "no": no_levels})

        # Drenar deltas pre-snapshot encolados para este ticker (bootstrap reordenado).
        self._drain_bootstrap_buffer(raw_msg["sid"], ticker, seq)

    def _drain_bootstrap_buffer(self, sid: int, ticker: str, snapshot_seq: int) -> None:
        """
        Aplica los deltas encolados antes del snapshot inicial de un ticker.

        Descarta los ya contenidos en el snapshot (seq <= snapshot_seq) y aplica
        el resto en orden de seq. Avanza el baseline del sid al mayor seq aplicado
        para no generar un falso gap en el próximo delta en vivo.
        """
        buffered = self._bootstrap_buffer.pop(ticker, [])
        if not buffered:
            return

        buffered.sort(key=lambda m: m["seq"])
        for m in buffered:
            if m["seq"] <= snapshot_seq:
                continue  # ya incluido en el snapshot
            self._apply_delta_msg(m)
            if m["seq"] > self._last_seq_by_sid.get(sid, 0):
                self._last_seq_by_sid[sid] = m["seq"]

    def _apply_delta_msg(self, raw_msg: dict) -> bool:
        """
        Apply WS orderbook_delta to state.

        Returns True si el delta se aplicó; False si se encoló en el bootstrap
        buffer porque el ticker aún no tiene snapshot inicial (se drenará al
        llegar el snapshot). Raises OrderbookDesyncError si produce new_qty < 0.
        """
        msg = raw_msg["msg"]
        ticker: str = msg["market_ticker"]
        seq: int = raw_msg["seq"]

        state = self._books.get(ticker)
        if state is None or not state.is_initialized:
            # Encolar (no descartar): el snapshot inicial del ticker aún no se
            # aplicó. Un delta con seq > snapshot_seq es una actualización real que
            # se perdería si se descartara, dejando el book sub-construido.
            self._bootstrap_buffer.setdefault(ticker, []).append(raw_msg)
            return False

        price_raw = msg.get("price_dollars") or msg.get("price")
        delta_raw = msg.get("delta_fp") or msg.get("delta")
        side = msg.get("side")

        price_cents = parse_price_to_cents(price_raw)
        delta_size = parse_size(delta_raw)

        if price_cents is None or delta_size is None:
            raise ValueError(
                f"Delta parse error for {ticker}: price_raw={price_raw!r}, delta_raw={delta_raw!r}"
            )

        # May raise OrderbookDesyncError (new_qty < 0). Capturamos SOLO para emitir
        # logging diagnostico defensivo y re-lanzamos la excepcion intacta: no se
        # altera la logica ni el control flow (la misma excepcion propaga igual).
        try:
            state.apply_delta({"side": side, "price": price_cents, "delta": delta_size, "seq": seq})
        except OrderbookDesyncError:
            # El bloque de logging va envuelto en su propio try/except para que
            # ningun fallo del diagnostico introduzca un path de excepcion nuevo.
            # apply_delta hace raise ANTES de mutar el book, asi que snapshot_view()
            # refleja el estado PRE-delta del bucket (el punto ciego que buscabamos).
            try:
                view = state.snapshot_view()
                bids = view.get("yes_bids" if side == "yes" else "no_bids", {})
                logger.error(
                    "V2 desync diagnostic: "
                    f"ticker={ticker} sid={raw_msg.get('sid')} msg_seq={seq} "
                    f"state_seq={view.get('sequence')} side={side} "
                    f"price_cents={price_cents} delta_size={delta_size} "
                    f"bucket_qty_pre_delta={bids.get(price_cents)} "
                    f"raw_msg={raw_msg!r}"
                )
            except Exception:
                pass
            # Re-lanzar la OrderbookDesyncError original intacta (fuera del try de logging).
            raise

        return True


# =====================================================
# Module helpers
# =====================================================


def _parse_fp_levels(
    raw_levels: list,
    ticker: str,
    side: str,
) -> list[list[int]]:
    """Convert [price_str, size_str] WS list to [price_cents, size_int] list."""
    result: list[list[int]] = []
    for lvl in raw_levels:
        if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
            logger.warning(f"OrderbookManagerV2: invalid level shape for {ticker}/{side}: {lvl!r}")
            continue
        price_cents = parse_price_to_cents(lvl[0])
        size = parse_size(lvl[1])
        if price_cents is None or size is None:
            logger.warning(f"OrderbookManagerV2: unparseable level for {ticker}/{side}: {lvl!r}")
            continue
        if size == 0:
            continue
        result.append([price_cents, size])
    return result
