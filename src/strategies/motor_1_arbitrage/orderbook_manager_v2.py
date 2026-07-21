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
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from loguru import logger

from src.monitoring.health import BotState
from src.strategies.data_capture import parse_price_to_cents, parse_size
from src.strategies.motor_1_arbitrage.orderbook import (
    BookTop,
    OrderbookDesyncError,
    OrderbookError,
    OrderbookSeqRegressionError,
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
    # Subido 5000→25000: con UN solo sid de ~328 tickers, la recovery debe absorber el feed live
    # de TODOS ellos mientras llegan los ~328 snapshots. A 5000 el buffer se llenaba antes de que
    # la recovery completara → buffer_overflow_x5 → circuit breaker → books_initialized=0. Más
    # headroom da tiempo a que completen los snapshots. Tuneable por env (ver runner/data_capture).
    DEFAULT_MAX_RECOVERY_BUFFER = 25000
    # Circuit breaker: tras N fallos CONSECUTIVOS de recovery de un sid (code 15 "Action
    # required" sobre el get_snapshot, o timeouts/overflow seguidos), se DESHABILITA la recovery
    # de ese sid (book queda stale + alerta) en vez del loop infinito (incidente: ~6764 fallos/día).
    # El contador se resetea cuando una recovery del sid completa OK.
    MAX_RECOVERY_FAILURES = 5
    # Tamaño de lote del get_snapshot de recovery (incidente 2026-07-17): un get_snapshot de un
    # sid GRANDE (sid=1 con 223 tickers) NUNCA devolvía un snapshot en 30s → timeout_x5 →
    # circuit breaker, mientras sids de 26 y 199 recuperaban bien (umbral de fallo medido entre
    # 199 y 223). El WS dropea/no responde la request masiva entera. Se parte `live` en lotes de
    # este tamaño, cada uno con su propio req_id/pending set — un lote frágil no condena al sid.
    DEFAULT_RECOVERY_CHUNK_SIZE = 50
    # Tope del buffer de BOOTSTRAP por ticker (OOM 2026-07-18): los deltas que llegan antes del
    # snapshot inicial de un ticker se encolan en _bootstrap_buffer[ticker]. A diferencia de
    # _pending_deltas (que tiene max_recovery_buffer), este buffer NO tenía tope → si el snapshot
    # inicial NUNCA llega (sid grande cuyo get_snapshot masivo se dropea), el feed live de esos
    # mercados lo llena SIN LÍMITE → crash-loop por OOM (~75 min a 1GB). Un deque(maxlen) descarta
    # los deltas MÁS VIEJOS: no se pierde info útil (el snapshot, cuando llegue, ya contiene los
    # seq bajos; _drain descarta seq <= snapshot_seq de todos modos). Lección del repo: nada sin tope.
    DEFAULT_BOOTSTRAP_BUFFER_CAP = 1000
    # Backoff del circuit breaker por sid (incidente 2026-07-21): el disable era PERMANENTE hasta
    # el redeploy — un sid deshabilitado por una condición transitoria (p.ej. mercados futuros aún
    # sin book que luego abren, o un feed degradado que se recupera) quedaba CIEGO para siempre.
    # Con backoff, el próximo gap del sid tras el cooldown reintenta la recovery UNA vez; si vuelve
    # a fallar, el cooldown crece exponencialmente (30s→2min→8min→30min cap). Ni loop de reintentos
    # calientes ni ceguera permanente.
    DEFAULT_RECOVERY_BACKOFF_BASE_SEC = 30.0
    DEFAULT_RECOVERY_BACKOFF_FACTOR = 4.0
    DEFAULT_RECOVERY_BACKOFF_CAP_SEC = 1800.0

    def __init__(
        self,
        ws: KalshiWebSocket,
        *,
        recovery_timeout_sec: float = DEFAULT_RECOVERY_TIMEOUT_SEC,
        max_recovery_buffer: int = DEFAULT_MAX_RECOVERY_BUFFER,
        recovery_chunk_size: int = DEFAULT_RECOVERY_CHUNK_SIZE,
        bootstrap_buffer_cap: int = DEFAULT_BOOTSTRAP_BUFFER_CAP,
        recovery_backoff_base_sec: float = DEFAULT_RECOVERY_BACKOFF_BASE_SEC,
        recovery_backoff_factor: float = DEFAULT_RECOVERY_BACKOFF_FACTOR,
        recovery_backoff_cap_sec: float = DEFAULT_RECOVERY_BACKOFF_CAP_SEC,
    ) -> None:
        self._ws = ws
        self._recovery_timeout_sec = recovery_timeout_sec
        self._max_recovery_buffer = max_recovery_buffer
        self._recovery_chunk_size = max(1, recovery_chunk_size)
        self._bootstrap_buffer_cap = max(1, bootstrap_buffer_cap)
        self._recovery_backoff_base_sec = max(1.0, recovery_backoff_base_sec)
        self._recovery_backoff_factor = max(1.0, recovery_backoff_factor)
        self._recovery_backoff_cap_sec = max(
            self._recovery_backoff_base_sec, recovery_backoff_cap_sec
        )
        self._books: dict[str, OrderbookState] = {}
        self._last_seq_by_sid: dict[int, int] = {}
        self._tickers_by_sid: dict[int, set[str]] = {}
        self._recovering: set[int] = set()
        self._pending_deltas: dict[int, list[dict]] = {}
        # sid → time.monotonic() de inicio de la recovery EN CURSO (para el watchdog).
        self._recovery_started_at: dict[int, float] = {}
        # Deltas que llegan antes del snapshot inicial de un ticker (bootstrap).
        # Se encolan por ticker y se drenan en _drain_bootstrap_buffer al llegar el snapshot,
        # en vez de descartarse (causa del desync de attempt #3). deque(maxlen) por ticker:
        # tope duro contra el OOM 2026-07-18 (un ticker cuyo snapshot no llega no crece sin fin).
        self._bootstrap_buffer: dict[str, deque[dict]] = {}
        # Tickers que YA saturaron su buffer de bootstrap (log one-shot + telemetría del leak).
        self._bootstrap_capped: set[str] = set()
        # req_id → (sid, set_of_pending_tickers_awaiting_recovery_snapshot)
        self._pending_snapshot_requests: dict[int, tuple[int, set[str]]] = {}
        # Tickers que NO se vuelven a pedir en recovery: settled/expirados (close_time vencido,
        # alimentado por set_close_times desde discovery) o marcados muertos por un rechazo de
        # Kalshi que los nombró. Evita pedir snapshot de mercados cerrados → code 15.
        self._close_time_by_ticker: dict[str, datetime] = {}
        self._dead_tickers: set[str] = set()
        # Metadata adicional de discovery (incidente 2026-07-21, sid=1 con 189 futuros): un mercado
        # que AÚN NO ABRIÓ (open_time futuro) o cuyo status dejó de ser active/open no tiene book
        # operable — pedirle snapshot es el mismo loop que los settled sin close_time. Solo se filtra
        # con conocimiento POSITIVO: ausente → recuperable (discovery a veces no trae los campos;
        # invertir el default purgaría sids VIVOS enteros, cf. close_times_known=0/223 el 07-17).
        self._open_time_by_ticker: dict[str, datetime] = {}
        self._status_by_ticker: dict[str, str] = {}
        # Circuit breaker por sid: fallos consecutivos de recovery + sids deshabilitados.
        self._recovery_failures_by_sid: dict[int, int] = {}
        self._recovery_disabled_sids: set[int] = set()
        # Backoff del breaker (incidente 2026-07-21): cuándo se deshabilitó cada sid (monotonic) y
        # cuántas veces SEGUIDAS se deshabilitó (escala el cooldown exponencialmente). Una recovery
        # que completa OK resetea ambos.
        self._recovery_disabled_at: dict[int, float] = {}
        self._disable_streak_by_sid: dict[int, int] = {}
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
            req_id = raw_msg.get("id")
            # code 15 "Action required" sobre un get_snapshot de recovery PENDIENTE: NO es ruido
            # genérico — es la request RECHAZADA. Se maneja explícito (purga + circuit breaker) en
            # vez de dejar que el watchdog re-pida el set completo a ciegas (loop de 6764/día).
            if code == 15 and isinstance(req_id, int) and req_id in self._pending_snapshot_requests:
                await self._handle_recovery_rejected(req_id, raw_msg)
                return
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
                # Circuit breaker activo para este sid: ya se dio por vencido (book stale + alerta).
                # Dentro del cooldown de backoff: avanzar el baseline y salir silencioso — ni
                # recovery ni buffer ni spam de gaps (una línea por reintento, no por ciclo).
                # Cooldown cumplido: re-habilitar y dejar que ESTE gap dispare el reintento.
                if sid in self._recovery_disabled_sids:
                    if not self._disabled_backoff_elapsed(sid):
                        self._last_seq_by_sid[sid] = new_seq
                        return
                    self._rearm_recovery_after_backoff(sid)
                await self._start_recovery(sid)
                # Solo bufferear si la recovery ARRANCÓ (no si _start_recovery la deshabilitó por
                # quedarse sin tickers vivos → el sid no entra en _recovering).
                if sid in self._recovering:
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
            try:
                applied = self._apply_delta_msg(raw_msg)  # May raise OrderbookDesyncError
            except (OrderbookDesyncError, OrderbookSeqRegressionError):
                # CUARENTENA (incidente 2026-07-12): el desync (new_qty<0) prueba que este
                # book DIVERGIÓ de la realidad, pero la excepción moría en el dispatcher del
                # WS y el book seguía initialized+no-stale → get_top_of_book seguía sirviendo
                # precios FANTASMA → M1 detectaba "arbs" en banda sobre liquidez inexistente
                # → fill parcial → rollback ×3 → circuit breaker (635 desyncs/día del mismo
                # ticker: cada delta siguiente volvía a fallar porque nadie lo re-baseaba).
                # Fail-closed: mark_stale YA (top_of_book→None, deltas dropeados) + recovery
                # del sid (el snapshot fresco lo re-basea). Re-raise intacta (fail-loud).
                ticker = raw_msg.get("msg", {}).get("market_ticker")
                state = self._books.get(ticker) if ticker else None
                if state is not None:
                    state.mark_stale()
                    logger.warning(
                        f"v2.desync_quarantine ticker={ticker} sid={sid}: book stale + "
                        "recovery (dejó de servir precios fantasma)"
                    )
                if sid not in self._recovering:
                    await self._start_recovery(sid)
                raise
        if applied:
            self._last_seq_by_sid[sid] = max(self._last_seq_by_sid.get(sid, 0), new_seq)

    @property
    def tracked_tickers(self) -> frozenset[str]:
        """Snapshot of tickers with an active OrderbookState (may be stale/uninitialized)."""
        return frozenset(self._books.keys())

    def get_top_of_book(self, ticker: str, side: Literal["yes", "no"]) -> BookTop | None:
        """Top of book. Returns None if ticker is unknown, stale, or uninitialized.

        El chequeo de is_stale faltaba (el docstring ya lo prometía): un book stale = esperando el
        snapshot de recovery, sus datos están DESACTUALIZADOS. Servirlos daría un book incoherente
        a quien lea (crítico para market making). None hasta que el snapshot lo re-basee."""
        state = self._books.get(ticker)
        if state is None or not state.is_initialized or state.is_stale:
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
            # Bootstrap buffer (anti-OOM 2026-07-18): msgs encolados esperando snapshot inicial +
            # cuántos tickers ya saturaron su tope. capados>0 sostenido = snapshots que no llegan.
            "bootstrap_buffer_msgs": sum(len(b) for b in self._bootstrap_buffer.values()),
            "bootstrap_capped_tickers": len(self._bootstrap_capped),
            "sids": list(self._last_seq_by_sid.keys()),
            "last_seq_by_sid": dict(self._last_seq_by_sid),
            "gaps_last_60s": gaps_last_60s,
            "last_gap_at": self._last_gap_at.isoformat() if self._last_gap_at else None,
            # Breaker con backoff (2026-07-21): sids deshabilitados + en cuántos segundos vence su
            # cooldown de reintento (0 = ya venció, reintenta en el próximo gap del sid).
            "sids_disabled": sorted(self._recovery_disabled_sids),
            "recovery_retry_in_sec": {
                sid: round(self._retry_remaining_sec(sid), 1)
                for sid in sorted(self._recovery_disabled_sids)
            },
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

    def set_close_times(self, close_times: dict[str, str | None]) -> None:
        """Alimenta los close_time (ISO 8601) por ticker desde discovery (data_capture). Los
        markets ya VENCIDOS se excluyen de los get_snapshot de recovery: pedir snapshot de un
        mercado cerrado/settled es lo que Kalshi rechaza con code 15 (causa del loop). Best-effort:
        un close_time inválido se ignora (ese ticker no se filtra por tiempo)."""
        for ticker, raw in close_times.items():
            parsed = _parse_iso_naive_utc(raw) if raw else None
            if parsed is not None:
                self._close_time_by_ticker[ticker] = parsed

    def set_market_metadata(self, metadata: dict[str, dict]) -> None:
        """Alimenta open_time (ISO 8601) y status por ticker desde discovery, para que la recovery
        no pida snapshot de mercados que AÚN NO ABRIERON (incidente 2026-07-21: sid=1 con 189
        futuros — World Cup/MLB — sin book operable → timeout_x5 → breaker en cada reinicio) ni de
        los que transicionaron fuera de active/open. Best-effort: campo ausente/inválido se ignora
        (ese ticker no se filtra por ese criterio)."""
        for ticker, meta in metadata.items():
            raw_open = meta.get("open_time")
            parsed = _parse_iso_naive_utc(raw_open) if raw_open else None
            if parsed is not None:
                self._open_time_by_ticker[ticker] = parsed
            status = meta.get("status")
            if isinstance(status, str) and status:
                self._status_by_ticker[ticker] = status

    def _is_unrecoverable(self, ticker: str) -> bool:
        """True si NO tiene sentido pedir snapshot de este ticker: marcado muerto, con close_time
        ya vencido (settled), con open_time FUTURO (mercado que aún no abrió → sin book operable,
        incidente 2026-07-21), o con status conocido fuera de active/open (transicionó a closed/
        settled/determined). None/desconocido → recuperable (no sobre-filtrar: discovery a veces
        no trae los campos y purgar por ausencia cegaría sids vivos enteros)."""
        if ticker in self._dead_tickers:
            return True
        status = self._status_by_ticker.get(ticker)
        if status is not None and status not in ("active", "open"):
            return True
        now = datetime.now(UTC).replace(tzinfo=None)
        open_time = self._open_time_by_ticker.get(ticker)
        if open_time is not None and open_time > now:
            return True
        close_time = self._close_time_by_ticker.get(ticker)
        return close_time is not None and close_time <= now

    async def _start_recovery(self, sid: int) -> None:
        """Marca los tickers del sid stale y pide get_snapshot SOLO de los recuperables.

        FIX 1 (purga): excluye tickers settled/dead — pedir snapshot de un mercado cerrado dispara
        code 15. Si NO queda ninguno vivo, el sid entero está cerrado → circuit breaker (no se pide
        nada, no se entra en _recovering, no hay loop)."""
        if sid in self._recovery_disabled_sids:
            return  # ya deshabilitado (circuit breaker) → no reintentar

        all_tickers = self._tickers_by_sid.get(sid, set())
        live = [t for t in all_tickers if not self._is_unrecoverable(t)]
        purged = len(all_tickers) - len(live)
        # DIAG: cuántos tickers del sid tienen cada metadata conocida. known=0 en un campo = ese
        # campo NO está llegando desde discovery → el filtro correspondiente nunca tiene con qué
        # purgar (por eso purged=0 con mercados settled el 07-17, o futuros sin abrir el 07-21).
        known_ct = sum(1 for t in all_tickers if t in self._close_time_by_ticker)
        known_ot = sum(1 for t in all_tickers if t in self._open_time_by_ticker)
        known_st = sum(1 for t in all_tickers if t in self._status_by_ticker)
        # Gap individual = evento benigno AUTO-RECUPERADO → INFO (la escalada por FRECUENCIA sigue
        # en _record_gap_and_should_alert).
        logger.info(
            f"Sid {sid} gap detected (auto-recovery). live={len(live)} purged={purged} "
            f"close_times_known={known_ct}/{len(all_tickers)} open_times_known={known_ot} "
            f"statuses_known={known_st} — requesting WS recovery snapshot."
        )
        for ticker in all_tickers:
            if ticker in self._books:
                self._books[ticker].mark_stale()

        if not live:
            logger.warning(
                f"v2.recovery_all_settled sid={sid}: los {len(all_tickers)} tickers están "
                "settled/dead → circuit breaker (no se pide snapshot)."
            )
            # Settlement esperado, no degradación: WARNING (no CRITICAL). Progress EXPLÍCITO:
            # NO dejar que _disable_recovery lo recompute — acá _pending_snapshot_requests nunca
            # se pobló (no se pidió snapshot) → _recovery_progress daría pending=0 → recovered=
            # total/total, el MISMO artefacto que el fix 2026-07-10 mató en timeout/code15 (por
            # otra causa: allá lo borraba el cleanup, acá nunca existió). La realidad es 0
            # recuperados de N (todos settled) y elapsed n/a (el timer nunca arrancó).
            total = len(all_tickers)
            await self._disable_recovery(
                sid,
                "all_tickers_settled",
                progress=f"recovered=0/{total} elapsed=n/a",
                expected_settlement=True,
            )
            return

        self._recovering.add(sid)
        self._pending_deltas[sid] = []
        self._recovery_started_at[sid] = time.monotonic()

        # CHUNKING (incidente 2026-07-17): un get_snapshot masivo (223 tickers) nunca vuelve; se
        # parte en lotes. Cada lote lleva su propio req_id y pending set — la recovery del sid
        # completa cuando TODOS los lotes drenaron (ver _handle_recovery_snapshot). Un solo lote
        # (live ≤ chunk_size) = comportamiento idéntico al anterior (un req_id).
        chunk = self._recovery_chunk_size
        n_chunks = (len(live) + chunk - 1) // chunk
        if n_chunks > 1:
            logger.info(
                f"v2.recovery_chunked sid={sid} live={len(live)} → {n_chunks} lotes de ≤{chunk} "
                "(request masiva se dropea entera; los lotes recuperan)."
            )
        for i in range(0, len(live), chunk):
            batch = live[i : i + chunk]
            req_id = await self._ws.send_command(
                "update_subscription",
                action="get_snapshot",
                params={"market_tickers": batch, "sids": [sid]},
            )
            self._pending_snapshot_requests[req_id] = (sid, set(batch))

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

    def _recovery_progress(self, sid: int) -> str:
        """Diagnóstico: cuántos tickers del sid YA recuperaron snapshot vs total, y hace cuánto
        arrancó la recovery. recovered creciendo → los snapshots SÍ llegan (falta headroom de
        buffer); recovered=0 → no llega ningún snapshot (causa de cuenta, no de buffer)."""
        total = len(self._tickers_by_sid.get(sid, set()))
        pending = sum(len(t) for s, t in self._pending_snapshot_requests.values() if s == sid)
        recovered = total - pending
        started = self._recovery_started_at.get(sid)
        elapsed = f"{time.monotonic() - started:.1f}s" if started is not None else "?"
        return f"recovered={recovered}/{total} elapsed={elapsed}"

    def _cleanup_recovery(self, sid: int) -> None:
        """Limpia el estado de recovery EN CURSO del sid (pending requests, buffer, timer, flag).
        Un snapshot tardío del intento abortado NO debe drenar el buffer de un intento nuevo."""
        stale_reqs = [r for r, (s, _) in self._pending_snapshot_requests.items() if s == sid]
        for r in stale_reqs:
            del self._pending_snapshot_requests[r]
        self._recovering.discard(sid)
        self._pending_deltas.pop(sid, None)
        self._recovery_started_at.pop(sid, None)

    async def _register_failure_and_maybe_break(
        self,
        sid: int,
        reason: str,
        *,
        progress: str | None = None,
        expected_settlement: bool = False,
    ) -> bool:
        """Contabiliza un fallo de recovery del sid. Si llega a MAX_RECOVERY_FAILURES consecutivos,
        DESHABILITA la recovery del sid (circuit breaker) y devuelve True (el caller NO reintenta).
        El contador se resetea cuando una recovery completa OK (_handle_recovery_snapshot).

        `progress` DEBE venir del caller computado ANTES de su _cleanup_recovery (fix 2026-07-10:
        _disable_recovery lo recomputaba post-cleanup → pending=0 → 'recovered=total/total' FALSO).
        `expected_settlement` baja el disable a WARNING (settlement esperado, no degradación)."""
        self._recovery_failures_by_sid[sid] = self._recovery_failures_by_sid.get(sid, 0) + 1
        if self._recovery_failures_by_sid[sid] >= self.MAX_RECOVERY_FAILURES:
            await self._disable_recovery(
                sid,
                f"{reason}_x{self._recovery_failures_by_sid[sid]}",
                progress=progress,
                expected_settlement=expected_settlement,
            )
            return True
        return False

    async def _disable_recovery(
        self,
        sid: int,
        reason: str,
        *,
        progress: str | None = None,
        expected_settlement: bool = False,
    ) -> None:
        """Circuit breaker: deja de reintentar la recovery de este sid (book queda stale) y ALERTA.
        Evita el loop infinito cuando la causa es persistente (cuenta en 'Action required', sid
        entero settled). Se re-habilita solo si una recovery del sid vuelve a completar OK.

        `progress`: pasarlo desde el caller (computado ANTES del cleanup) — si es None se computa
        acá, pero cuando el caller ya limpió el estado (timeout/code15) eso da un 'recovered' FALSO
        (pending=0). `expected_settlement=True` (settlement conocido) → WARNING en vez de CRITICAL:
        un mercado cerrado dejando su sid stale es esperado, no una degradación del feed."""
        if progress is None:
            progress = self._recovery_progress(sid)  # solo válido si aún no se limpió
        self._cleanup_recovery(sid)
        self._recovery_disabled_sids.add(sid)
        # Backoff (2026-07-21): registrar CUÁNDO y CUÁNTAS veces seguidas — el próximo gap del sid
        # tras el cooldown reintenta (ver handle_message), con cooldown exponencial por streak.
        self._recovery_disabled_at[sid] = time.monotonic()
        self._disable_streak_by_sid[sid] = self._disable_streak_by_sid.get(sid, 0) + 1
        fails = self._recovery_failures_by_sid.get(sid, 0)
        backoff = self._backoff_delay_sec(sid)
        msg = (
            f"sid={sid} recovery DESHABILITADA ({reason}, {fails} fallos, {progress}) — "
            f"book stale; reintento tras backoff de {backoff:.0f}s "
            f"(streak={self._disable_streak_by_sid[sid]})"
        )
        if expected_settlement:
            # Settlement esperado (mercado cerrado / sid entero settled): el book queda stale por
            # diseño, no es una degradación del feed. WARNING + sin record_error (no ensucia BotState).
            logger.warning(f"v2.recovery_disabled {msg}")
        else:
            logger.critical(f"v2.recovery_disabled {msg}")
            BotState.record_error(f"v2.recovery_disabled {msg}")
        await self._fire_alert("recovery_disabled", msg)

    def _backoff_delay_sec(self, sid: int) -> float:
        """Cooldown vigente del sid según su streak de disables: base·factor^(streak−1), capado.
        Defaults: 30s→2min→8min→30min (cap). streak=0/1 → base."""
        streak = max(1, self._disable_streak_by_sid.get(sid, 1))
        return min(
            self._recovery_backoff_cap_sec,
            self._recovery_backoff_base_sec * self._recovery_backoff_factor ** (streak - 1),
        )

    def _retry_remaining_sec(self, sid: int) -> float:
        """Segundos hasta que venza el cooldown del sid deshabilitado (0 = vencido). Un sid sin
        timestamp (estado legado/inconsistente) se considera vencido: mejor UN reintento acotado
        que ceguera permanente."""
        disabled_at = self._recovery_disabled_at.get(sid)
        if disabled_at is None:
            return 0.0
        return max(0.0, self._backoff_delay_sec(sid) - (time.monotonic() - disabled_at))

    def _disabled_backoff_elapsed(self, sid: int) -> bool:
        """True si el cooldown del sid deshabilitado ya venció (toca reintentar)."""
        return self._retry_remaining_sec(sid) <= 0.0

    def _rearm_recovery_after_backoff(self, sid: int) -> None:
        """Re-habilita la recovery del sid tras el cooldown (UNA línea de log, no una por ciclo).
        Resetea el contador de fallos consecutivos (ventana fresca de MAX_RECOVERY_FAILURES
        intentos) pero CONSERVA el streak de disables — si esta ventana también fracasa, el
        próximo cooldown es más largo (exponencial). Solo una recovery OK resetea el streak."""
        streak = self._disable_streak_by_sid.get(sid, 0)
        logger.info(
            f"v2.recovery_retry sid={sid} tras backoff de {self._backoff_delay_sec(sid):.0f}s "
            f"(streak={streak}) — reintentando recovery."
        )
        self._recovery_disabled_sids.discard(sid)
        self._recovery_disabled_at.pop(sid, None)
        self._recovery_failures_by_sid.pop(sid, None)

    async def _handle_recovery_rejected(self, req_id: int, raw_msg: dict) -> None:
        """FIX 2 — code 15 sobre un get_snapshot pendiente: la request fue RECHAZADA. En vez de
        re-pedir el set completo a ciegas: si Kalshi nombra un ticker, marcarlo muerto; limpiar la
        recovery; contar el fallo (circuit breaker); y si no se rompió, reintentar con el set ya
        FILTRADO (sin settled/dead)."""
        sid, tickers = self._pending_snapshot_requests[req_id]
        progress = self._recovery_progress(
            sid
        )  # ANTES de cleanup (evita 'recovered' post-cleanup falso)
        bad = raw_msg.get("market_ticker")
        if isinstance(bad, str):
            self._dead_tickers.add(bad)
        self._cleanup_recovery(sid)
        # DIAG (por qué purged=0): para una muestra de los tickers RECHAZADOS, qué close_time
        # tienen. NO_CLOSE_TIME → discovery no se lo pasó al manager (campo ausente en get_event);
        # una fecha futura → no está settled (el code 15 NO es por ticker vencido, es otra causa,
        # p.ej. cuenta). known=0/N confirma que el filtro de close_time nunca tuvo con qué purgar.
        sample = {
            t: (ct.isoformat() if (ct := self._close_time_by_ticker.get(t)) else "NO_CLOSE_TIME")
            for t in list(tickers)[:5]
        }
        known_ct = sum(1 for t in tickers if t in self._close_time_by_ticker)
        logger.warning(
            f"v2.recovery_rejected sid={sid} code=15 req_id={req_id} tickers={len(tickers)} "
            f"bad={bad} close_times_known={known_ct}/{len(tickers)} sample={sample} "
            "(purga + circuit breaker; NO re-pide el set completo)"
        )
        BotState.record_error(f"v2 recovery rechazada (code 15) sid={sid}")
        if await self._register_failure_and_maybe_break(sid, "code15", progress=progress):
            return
        await self._start_recovery(sid)

    async def _abort_and_restart_recovery(self, sid: int, reason: str) -> None:
        """
        Tira la recovery atascada del sid (descarta el buffer) y arranca una nueva — salvo que el
        circuit breaker se dispare (N fallos consecutivos → se deshabilita y NO reintenta).

        Loguea WARNING con el sid y CUÁNTOS mensajes se descartaron: si se dispara seguido es feed
        degradado (mirar upstream). Re-pide el snapshot vía _start_recovery (buffer/timer frescos,
        bounded por timeout/tamaño) hasta que el breaker corte.
        """
        discarded = len(self._pending_deltas.get(sid, []))
        progress = self._recovery_progress(sid)  # ANTES de cleanup (lee pending/timer)

        # Sub-caso settlement (fix 2026-07-10): tickers cuyo snapshot NUNCA llegó dentro de la
        # ventana. Con progreso PARCIAL (la mayoría recuperó, unos pocos atascados), los atascados
        # casi seguro son settled/dead — Kalshi no manda snapshot de un mercado cerrado, y su
        # close_time puede no haber llegado desde discovery (por eso el filtro _is_unrecoverable no
        # los purgó). Marcarlos dead + reintentar con el resto: la recovery CONVERGE (los 240 vivos
        # completan) en vez de escalar 5× a CRITICAL por 1-2 mercados cerrados. Si NADA recuperó
        # (recovered=0), es feed degradado real → NO se matan tickers, escala como antes (anti-OOM).
        stuck = {
            t
            for _rid, (s, pend) in self._pending_snapshot_requests.items()
            if s == sid
            for t in pend
        }
        total = len(self._tickers_by_sid.get(sid, set()))
        settled_subcase = reason == "timeout" and 0 < len(stuck) < total
        if settled_subcase:
            self._dead_tickers |= stuck
            logger.info(
                f"v2.recovery_stuck_marked_dead sid={sid} stuck={len(stuck)}/{total} "
                f"tickers={sorted(stuck)[:5]} → dead + reintento con el resto (probable settlement)."
            )

        self._cleanup_recovery(sid)
        logger.warning(
            f"v2.recovery_aborted sid={sid} reason={reason} {progress} discarded_msgs={discarded} "
            "→ reintentando snapshot (acotado por circuit breaker)."
        )
        if await self._register_failure_and_maybe_break(
            sid, reason, progress=progress, expected_settlement=settled_subcase
        ):
            return
        await self._start_recovery(sid)

    def _pending_req_id_for_sid(self, sid: int) -> int | None:
        """req_id de la solicitud de snapshot EN CURSO para este sid (para completar la recovery
        de un snapshot sin id/ id que no coincide). None si el sid no tiene request pendiente."""
        for req_id, (req_sid, _tickers) in self._pending_snapshot_requests.items():
            if req_sid == sid:
                return req_id
        return None

    async def _handle_recovery_snapshot(self, raw_msg: dict, req_id: int) -> None:
        """Apply a recovery snapshot. Con CHUNKING un sid tiene varios req_id (lotes): la
        recovery del sid completa cuando NINGÚN lote queda pendiente, no cuando se vacía el
        primero (si no, cerraría con la mayoría de books aún stale)."""
        entry = self._pending_snapshot_requests.get(req_id)
        if entry is None:
            return  # req_id ya completado/desconocido (snapshot duplicado o tardío)
        sid = entry[0]
        ticker: str = raw_msg["msg"]["market_ticker"]

        self._apply_snapshot_msg(raw_msg)
        # Descartar el ticker de SU lote buscándolo por ticker (robusto: el fallback sin id pasa
        # el primer req_id del sid, pero el ticker puede vivir en OTRO lote). Se vacía el lote →
        # se borra su req_id.
        for rid, (s, pend) in list(self._pending_snapshot_requests.items()):
            if s == sid and ticker in pend:
                pend.discard(ticker)
                if not pend:
                    del self._pending_snapshot_requests[rid]
                break

        # ¿Quedan lotes pendientes del sid? Si no, la recovery COMPLETÓ.
        if not any(s == sid for s, _ in self._pending_snapshot_requests.values()):
            self._recovering.discard(sid)
            self._recovery_started_at.pop(sid, None)
            # Recovery OK → resetea el circuit breaker del sid (y lo re-habilita si estaba off),
            # incluido el streak de backoff: el próximo disable arranca de nuevo en el cooldown base.
            self._recovery_failures_by_sid.pop(sid, None)
            self._recovery_disabled_sids.discard(sid)
            self._recovery_disabled_at.pop(sid, None)
            self._disable_streak_by_sid.pop(sid, None)
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
        buffered = self._bootstrap_buffer.pop(ticker, None)
        self._bootstrap_capped.discard(ticker)  # el snapshot llegó → el ticker ya no está capado
        if not buffered:
            return

        for m in sorted(buffered, key=lambda m: m["seq"]):  # deque → sorted (no tiene .sort())
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
            # Encolar (no descartar): el snapshot inicial del ticker aún no se aplicó. Un delta
            # con seq > snapshot_seq es una actualización real que se perdería si se descartara.
            # deque(maxlen): tope duro (OOM 2026-07-18). Si el buffer se llena, descarta el MÁS
            # VIEJO — cuando el snapshot llegue, _drain ya descarta los seq bajos, así que solo
            # se pierde lo que igual se iba a descartar. Que un ticker sature = su snapshot no
            # llega (síntoma del sid grande sin recovery) → log one-shot para el forense.
            buf = self._bootstrap_buffer.get(ticker)
            if buf is None:
                buf = deque(maxlen=self._bootstrap_buffer_cap)
                self._bootstrap_buffer[ticker] = buf
            if len(buf) == self._bootstrap_buffer_cap and ticker not in self._bootstrap_capped:
                self._bootstrap_capped.add(ticker)
                logger.warning(
                    f"v2.bootstrap_buffer_capped ticker={ticker} cap={self._bootstrap_buffer_cap} "
                    "— el snapshot inicial no llega; se descartan deltas viejos (anti-OOM). "
                    f"tickers_capados={len(self._bootstrap_capped)}"
                )
            buf.append(raw_msg)
            return False
        if state.is_stale:
            # CAUSA RAÍZ del desync constante (~10% de los logs): tras una recovery que NO completó
            # (buffer overflow → circuit breaker → sid deshabilitado, o snapshots bloqueados por la
            # cuenta), el book queda STALE pero los deltas seguían aplicándose sobre él → el estado
            # divergía hasta qty<0 → OrderbookDesyncError, mensaje tras mensaje. NO se aplica sobre
            # un book stale: se dropea el delta (se re-basea con el snapshot de recovery cuando
            # llegue; hasta entonces get_top_of_book devuelve None → nadie lee un book corrupto).
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


def _parse_iso_naive_utc(value: str) -> datetime | None:
    """ISO 8601 (con o sin 'Z'/offset) → datetime NAIVE en UTC, para comparar con
    datetime.now(UTC).replace(tzinfo=None). None si no parsea (best-effort)."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


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
