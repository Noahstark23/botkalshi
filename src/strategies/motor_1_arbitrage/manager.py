"""
OrderbookManager: capa de red para Día 2.

Responsabilidades:
- Mantiene N OrderbookState (uno por ticker) en memoria.
- Recibe orderbook_snapshot y orderbook_delta del WS, normaliza, aplica.
- Gap detection a nivel STREAM (no per-market) usando seq global de Kalshi.
- Buffer de deltas mientras un ticker está en sync inicial.
- Recovery vía callback inyectado ante desync per-market.
- purge_all_books() para WS reconnects.

Diseño de seq: dos contadores separados.

  stream_seq (WS global): monotónico por sid, compartido entre todos los markets
    de la misma subscription. Usado SOLO para gap detection. Si hay gap → purge_all_books.

  local_seq (por market): contador interno del manager, 0-based, independiente
    del stream_seq. Usado para las llamadas a apply_snapshot/apply_delta de Día 1.
    Motivo: los deltas buffereados llegan en stream seqs MENORES al snapshot que
    viene después. Si usásemos stream_seq como seq de estado, apply_delta(seq=2)
    sobre state.sequence=5 fallaría (Día 1 exige new_seq > current). El local_seq
    garantiza que cada delta aplicado siempre avanza el contador local (0 → 1 → 2…).

Inversión de control:
- No referencia a KalshiRestClient ni KalshiWebSocket.
- on_desync_callback(ticker) para recovery per-market (resubscribe externo).
- El runner wirea handle_snapshot_message / handle_delta_message a ws.on(...).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from loguru import logger

from src.strategies.motor_1_arbitrage.manager_normalize import (
    extract_sid,
    extract_ticker,
    normalize_delta_payload,
    normalize_snapshot_payload,
)
from src.strategies.motor_1_arbitrage.orderbook import (
    BookTop,
    OrderbookDesyncError,
    OrderbookNotInitializedError,
    OrderbookState,
)

DesyncCallback = Callable[[str], Awaitable[None]]


@dataclass
class _MarketBookEntry:
    state: OrderbookState
    buffer: list[dict] = field(default_factory=list)
    is_syncing: bool = True
    local_seq: int = 0


class OrderbookManager:
    def __init__(self, on_desync_callback: DesyncCallback) -> None:
        """
        Args:
            on_desync_callback: corrutina invocada con el ticker cuando se detecta
                desync irrecuperable (OrderbookDesyncError) o fallo de normalización.
                El runner conecta esto al mecanismo de resubscribe del WS.
        """
        self._markets: dict[str, _MarketBookEntry] = {}
        self._on_desync_callback = on_desync_callback
        self._last_stream_seq: int | None = None
        self._current_sid: int | None = None

    # =====================================================
    # Public API
    # =====================================================

    def initialize_market(self, ticker: str) -> None:
        """Registra ticker pre-discovery. Idempotente."""
        if ticker not in self._markets:
            self._markets[ticker] = _MarketBookEntry(state=OrderbookState(ticker=ticker))
            logger.info(f"manager.market_registered ticker={ticker}")

    def purge_all_books(self) -> None:
        """
        Clear destructivo de todos los states, buffers y stream tracker.

        Invocado ante reconexión del WS o gap de stream irrecuperable.
        Después de purge, cada ticker necesita un nuevo snapshot para re-inicializarse.
        """
        count = len(self._markets)
        for entry in self._markets.values():
            entry.state.clear()
            entry.buffer.clear()
            entry.is_syncing = True
            entry.local_seq = 0
        self._last_stream_seq = None
        self._current_sid = None
        logger.warning(
            f"manager.purge_all_books markets={count} reason=ws_reconnect_or_gap"
        )

    def get_market_tops(self, ticker: str) -> dict[str, BookTop] | None:
        """
        Top of book YES y NO para un ticker.

        Returns None si: ticker desconocido, en sync inicial, o state no inicializado.
        """
        entry = self._markets.get(ticker)
        if entry is None or entry.is_syncing or not entry.state.is_initialized:
            return None
        return {
            "yes": entry.state.top_of_book("yes"),
            "no": entry.state.top_of_book("no"),
        }

    # =====================================================
    # WS Handlers — registrar con ws.on(type, handler)
    # =====================================================

    async def handle_snapshot_message(self, raw_msg: dict) -> None:
        """Handler para mensajes type=orderbook_snapshot."""
        self._check_and_update_stream_state(raw_msg)

        ticker = extract_ticker(raw_msg)
        if ticker is None or ticker not in self._markets:
            return

        entry = self._markets[ticker]
        was_syncing = entry.is_syncing
        buffered_count = len(entry.buffer)

        try:
            parsed = normalize_snapshot_payload(raw_msg)
        except ValueError as e:
            logger.error(f"manager.snapshot_normalize_failed ticker={ticker} error={e}")
            await self._trigger_local_recovery(ticker, reason="snapshot_normalize_failed")
            return

        try:
            entry.state.apply_snapshot({"seq": 0, "yes": parsed["yes"], "no": parsed["no"]})
            entry.local_seq = 0
        except Exception as e:
            logger.error(f"manager.snapshot_apply_failed ticker={ticker} error={e}")
            await self._trigger_local_recovery(ticker, reason="snapshot_apply_failed")
            return

        await self._drain_buffer(ticker)

        if was_syncing and not entry.is_syncing:
            logger.info(
                f"manager.market_synced ticker={ticker} "
                f"stream_seq={raw_msg.get('seq')} "
                f"local_seq={entry.local_seq} "
                f"buffered_deltas_drained={buffered_count}"
            )

    async def handle_delta_message(self, raw_msg: dict) -> None:
        """Handler para mensajes type=orderbook_delta."""
        if not self._check_and_update_stream_state(raw_msg):
            return

        ticker = extract_ticker(raw_msg)
        if ticker is None or ticker not in self._markets:
            return

        entry = self._markets[ticker]

        if entry.is_syncing:
            entry.buffer.append(raw_msg)
            return

        await self._apply_delta_with_recovery(ticker, raw_msg)

    # =====================================================
    # Internal
    # =====================================================

    def _check_and_update_stream_state(self, raw_msg: dict) -> bool:
        """
        Valida el seq global del stream y actualiza los trackers de sid y seq.

        Returns True si OK para procesar, False si detectó gap (purge ya aplicado).

        Nota: el stream seq de Kalshi es global por sid (subscription ID).
        Un sid diferente indica resubscribe — se resetea el tracker sin gap error.
        """
        new_sid = extract_sid(raw_msg)
        new_seq = raw_msg.get("seq")

        if not isinstance(new_seq, int):
            return True

        if new_sid is not None and new_sid != self._current_sid:
            logger.info(
                f"manager.sid_changed old={self._current_sid} new={new_sid} "
                f"stream_seq={new_seq}"
            )
            self._current_sid = new_sid
            self._last_stream_seq = new_seq
            return True

        if self._last_stream_seq is None:
            self._last_stream_seq = new_seq
            return True

        expected = self._last_stream_seq + 1
        if new_seq != expected:
            logger.critical(
                f"manager.stream_gap expected_seq={expected} "
                f"received_seq={new_seq} gap={new_seq - expected}"
            )
            self.purge_all_books()
            return False

        self._last_stream_seq = new_seq
        return True

    async def _drain_buffer(self, ticker: str) -> None:
        """
        Swap-and-drain: aplica todos los deltas buffereados sobre el snapshot reciente.

        Loop para atrapar deltas que llegan DURANTE el drain (edge case de concurrencia).
        Los deltas buffereados siempre avanzan local_seq (1, 2, 3…) sobre el base 0 del
        snapshot, sin depender del stream_seq, evitando colisiones con Día 1's invariante.
        """
        entry = self._markets[ticker]
        while True:
            pending = entry.buffer
            entry.buffer = []

            for raw_delta in pending:
                applied = await self._apply_delta_with_recovery(ticker, raw_delta)
                if not applied:
                    return

            if not entry.buffer:
                entry.is_syncing = False
                return

    async def _apply_delta_with_recovery(self, ticker: str, raw_msg: dict) -> bool:
        """
        Normaliza y aplica un delta usando el contador local del market.

        Usa local_seq en lugar del stream seq de Kalshi para la llamada a apply_delta.
        Motivo: Día 1 exige new_seq > state.sequence. El stream seq puede ser menor
        que el seq del snapshot (deltas buffereados pre-snapshot), pero local_seq
        siempre avanza desde 0.

        Returns True si aplicó o si fue un error aislado (skip sin recovery).
        Returns False si disparó recovery (ticker en is_syncing, callback invocado).
        """
        entry = self._markets[ticker]

        try:
            parsed = normalize_delta_payload(
                raw_msg, synthetic_previous_seq=entry.local_seq
            )
        except ValueError as e:
            logger.error(f"manager.delta_normalize_failed ticker={ticker} error={e}")
            return True

        entry.local_seq += 1
        try:
            entry.state.apply_delta({
                "seq": entry.local_seq,
                "side": parsed["side"],
                "price": parsed["price"],
                "delta": parsed["delta"],
            })
            logger.debug(
                f"manager.delta_applied ticker={ticker} local_seq={entry.local_seq} "
                f"side={parsed['side']} price={parsed['price']} delta={parsed['delta']}"
            )
            return True
        except OrderbookDesyncError as e:
            entry.local_seq -= 1
            logger.critical(
                f"manager.market_desync ticker={ticker} "
                f"price_cents={e.price_cents} new_qty={e.new_qty}"
            )
            await self._trigger_local_recovery(ticker, reason="apply_delta_desync")
            return False
        except (ValueError, OrderbookNotInitializedError) as e:
            entry.local_seq -= 1
            logger.error(f"manager.delta_apply_failed ticker={ticker} error={e}")
            return True

    async def _trigger_local_recovery(self, ticker: str, *, reason: str) -> None:
        """
        Pone el ticker en is_syncing, limpia state/buffer/local_seq, invoca callback.

        El callback externo (runner) es responsable de resubscribir el ticker al WS.
        Una vez que el WS re-emita el snapshot, handle_snapshot_message lo re-inicializará.
        """
        entry = self._markets[ticker]
        entry.state.clear()
        entry.buffer.clear()
        entry.is_syncing = True
        entry.local_seq = 0

        logger.warning(
            f"manager.local_recovery_triggered ticker={ticker} reason={reason}"
        )

        try:
            await self._on_desync_callback(ticker)
        except Exception as e:
            logger.exception(
                f"manager.recovery_callback_failed ticker={ticker} error={e}"
            )
