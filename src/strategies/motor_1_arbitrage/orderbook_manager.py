"""
OrderbookManager: mantiene OrderbookState por ticker, alimentado por WS y REST.

Responsabilidades:
    - Detectar gaps de seq a nivel sid (el seq de Kalshi WS es per-sid, no per-ticker).
    - Disparar recovery coordinado: clear() de todos los tickers del sid + re-snapshot REST.
    - Normalizar formatos WS (fixed-point dollar strings) al formato de OrderbookState.

Contexto Kalshi:
    El seq en el WS es un contador monotónico global por subscription (sid).
    Un solo sid puede tener múltiples tickers suscritos, y el seq se incrementa
    con cada mensaje de cualquier ticker. Si se detecta un gap (seq esperado != recibido),
    no se sabe qué ticker se perdió — hay que invalidar todos los tickers del sid.

    WS orderbook_snapshot: prices en dollar fixed-point strings ("0.0800" → 8 cents).
    WS orderbook_delta:    price_dollars + delta_fp como strings.
    REST get_orderbook:    prices ya en centavos enteros (formato histórico, sin cambio).
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Literal

from loguru import logger

from src.monitoring.health import BotState
from src.strategies.data_capture import parse_price_to_cents, parse_size
from src.strategies.motor_1_arbitrage.orderbook import (
    BookTop,
    OrderbookError,
    OrderbookState,
)

if TYPE_CHECKING:
    from src.clients.kalshi_rest import KalshiRestClient


class OrderbookDesyncError(OrderbookError):
    """
    Gap detectado en el seq global del sid: el state local está desincronizado.

    Raised internamente por OrderbookManager cuando new_seq != last_seq + 1.
    Recovery automático: clear() de todos los tickers del sid + re-snapshot REST.

    Attributes:
        sid: Subscription ID donde se detectó el gap.
        expected_seq: Seq que esperábamos (last_seq + 1).
        received_seq: Seq que llegó en el mensaje.
    """

    def __init__(self, sid: int, expected_seq: int, received_seq: int) -> None:
        self.sid = sid
        self.expected_seq = expected_seq
        self.received_seq = received_seq
        super().__init__(f"Sid {sid} desync: expected seq={expected_seq}, got {received_seq}")


class OrderbookManager:
    """
    Mantiene OrderbookState por ticker, alimentado por WS deltas y REST snapshots.

    Detecta desync a nivel sid (gap en seq global) y dispara recovery:
    clear() de todos los tickers de esa sid + re-snapshot via REST.

    Usage:
        async with KalshiRestClient() as rest_client:
            manager = OrderbookManager(rest_client)
            # WS handler llama:
            await manager.handle_ws_message(raw_msg)
            # Detector lee:
            top = manager.get_top_of_book("KXNBA-26-NYK", "yes")
    """

    def __init__(self, rest_client: KalshiRestClient) -> None:
        self._rest_client = rest_client
        self._books: dict[str, OrderbookState] = {}
        self._last_seq_by_sid: dict[int, int] = {}
        self._tickers_by_sid: dict[int, set[str]] = {}

    # =====================================================
    # Public API
    # =====================================================

    async def handle_ws_message(self, raw_msg: dict) -> None:
        """
        Dispatch principal para mensajes WS.

        Ignora silenciosamente mensajes que no son orderbook_snapshot/delta.
        Detecta gaps de seq y dispara recovery antes de procesar el mensaje.
        """
        msg_type = raw_msg.get("type")
        if msg_type not in ("orderbook_snapshot", "orderbook_delta"):
            return

        try:
            sid: int = raw_msg["sid"]
            new_seq: int = raw_msg["seq"]
            ticker: str = raw_msg["msg"]["market_ticker"]
        except KeyError as e:
            BotState.record_error(
                f"OrderbookManager: malformed WS message missing field {e}: {raw_msg}"
            )
            return

        # Registrar ticker en su sid (siempre, antes del gap check)
        self._tickers_by_sid.setdefault(sid, set()).add(ticker)

        # Gap detection
        if sid in self._last_seq_by_sid:
            last_seq = self._last_seq_by_sid[sid]
            if new_seq != last_seq + 1:
                tickers_in_sid = self._tickers_by_sid.get(sid, set())
                logger.critical(
                    f"Sid {sid} desync: expected seq={last_seq + 1}, got {new_seq}. "
                    f"Recovering {len(tickers_in_sid)} tickers."
                )
                BotState.record_error(
                    f"OrderbookManager sid {sid} desync: expected={last_seq + 1}, "
                    f"got={new_seq}. Triggering recovery."
                )
                await self._recover_sid(sid)
                # Acepta el nuevo seq como baseline — no re-disparar en el próximo mensaje
                self._last_seq_by_sid[sid] = new_seq
                return

        self._last_seq_by_sid[sid] = new_seq

        if msg_type == "orderbook_snapshot":
            await self._handle_snapshot(raw_msg)
        else:
            await self._handle_delta(raw_msg)

    def get_top_of_book(self, ticker: str, side: Literal["yes", "no"]) -> BookTop | None:
        """
        Top of book para el ticker y side especificados.

        Returns None si el ticker no existe o su state no está inicializado.
        """
        state = self._books.get(ticker)
        if state is None or not state.is_initialized:
            return None
        return state.top_of_book(side)

    def stats(self) -> dict:
        """Estado interno para debugging desde /status."""
        return {
            "tracked_tickers": len(self._books),
            "initialized_tickers": sum(1 for b in self._books.values() if b.is_initialized),
            "sids": list(self._last_seq_by_sid.keys()),
            "last_seq_by_sid": dict(self._last_seq_by_sid),
        }

    # =====================================================
    # Internal handlers
    # =====================================================

    async def _handle_snapshot(self, raw_msg: dict) -> None:
        """
        Procesa WS orderbook_snapshot.

        Formato Kalshi 2026: yes_dollars_fp / no_dollars_fp con listas de
        [price_str, size_str]. Fallback a yes/no para compatibilidad legacy.
        """
        msg = raw_msg["msg"]
        ticker: str = msg["market_ticker"]
        seq: int = raw_msg["seq"]

        yes_raw = msg.get("yes_dollars_fp") or msg.get("yes") or []
        no_raw = msg.get("no_dollars_fp") or msg.get("no") or []

        yes_levels = _parse_fp_levels(yes_raw, ticker, "yes")
        no_levels = _parse_fp_levels(no_raw, ticker, "no")

        if ticker not in self._books:
            self._books[ticker] = OrderbookState(ticker)

        self._books[ticker].apply_snapshot(
            {
                "seq": seq,
                "yes": yes_levels,
                "no": no_levels,
            }
        )

    async def _handle_delta(self, raw_msg: dict) -> None:
        """
        Procesa WS orderbook_delta.

        Si el ticker no tiene state inicializado, solicita snapshot REST y retorna.
        El delta se pierde pero el state quedará fresco desde el REST snapshot;
        los deltas WS siguientes lo actualizarán desde ahí.
        """
        msg = raw_msg["msg"]
        ticker: str = msg["market_ticker"]
        seq: int = raw_msg["seq"]

        state = self._books.get(ticker)
        if state is None or not state.is_initialized:
            # Delta llegó antes del snapshot inicial — pedir snapshot REST
            await self._request_snapshot(ticker)
            return

        price_raw = msg.get("price_dollars") or msg.get("price")
        delta_raw = msg.get("delta_fp") or msg.get("delta")
        side = msg.get("side")

        price_cents = parse_price_to_cents(price_raw)
        delta_size = parse_size(delta_raw)

        if price_cents is None or delta_size is None:
            BotState.record_error(
                f"OrderbookManager delta parse error for {ticker}: "
                f"price_raw={price_raw!r}, delta_raw={delta_raw!r}"
            )
            return

        state.apply_delta(
            {
                "side": side,
                "price": price_cents,
                "delta": delta_size,
                "seq": seq,
            }
        )

    # =====================================================
    # Recovery
    # =====================================================

    async def _recover_sid(self, sid: int) -> None:
        """
        Invalida y re-snapshot todos los tickers del sid.

        Un fallo en el snapshot de un ticker individual no aborta la recovery
        de los demás. El error queda registrado en BotState.last_error.
        """
        tickers = list(self._tickers_by_sid.get(sid, set()))
        for ticker in tickers:
            if ticker in self._books:
                self._books[ticker].clear()
            with suppress(Exception):
                # Error logged in _request_snapshot; continue recovering other tickers
                await self._request_snapshot(ticker)

    async def _request_snapshot(self, ticker: str) -> None:
        """
        Solicita snapshot REST y lo aplica al state del ticker.

        REST get_orderbook retorna prices en centavos enteros (no fixed-point).
        Se usa seq=0 para snapshots REST; el primer delta WS llegará con seq > 0
        y cumple la condición de monotonicidad.

        Raises:
            Exception: cualquier error de red, 429, etc. El caller decide si swallow.
        """
        try:
            response = await self._rest_client.get_orderbook(ticker, depth=50)
            orderbook = response.get("orderbook", {})
            yes_levels = orderbook.get("yes", [])
            no_levels = orderbook.get("no", [])

            if ticker not in self._books:
                self._books[ticker] = OrderbookState(ticker)

            self._books[ticker].apply_snapshot(
                {
                    "seq": 0,
                    "yes": yes_levels,
                    "no": no_levels,
                }
            )
        except Exception as e:
            BotState.record_error(f"Snapshot REST failed for {ticker}: {type(e).__name__}: {e}")
            raise


# =====================================================
# Module helpers
# =====================================================


def _parse_fp_levels(
    raw_levels: list,
    ticker: str,
    side: str,
) -> list[list[int]]:
    """
    Convierte lista de [price_str, size_str] WS al formato [price_cents, size_int].

    Niveles con precio o size inválidos se omiten con warning (no crash).
    """
    result: list[list[int]] = []
    for lvl in raw_levels:
        if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
            logger.warning(f"OrderbookManager: invalid level shape for {ticker}/{side}: {lvl!r}")
            continue
        price_cents = parse_price_to_cents(lvl[0])
        size = parse_size(lvl[1])
        if price_cents is None or size is None:
            logger.warning(f"OrderbookManager: unparseable level for {ticker}/{side}: {lvl!r}")
            continue
        result.append([price_cents, size])
    return result
