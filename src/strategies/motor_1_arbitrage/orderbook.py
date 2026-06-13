"""
OrderbookState: estado en memoria del orderbook de un market.

Mantiene bids por side (yes/no), validados por sequence number.
100% puro: sin IO, sin async, sin dependencias externas. La capa de red
(OrderbookManager) consume esta clase pero no la conoce internamente.

Contexto Kalshi — sequence numbers:
    El seq en el WS de Kalshi es per-sid (subscription), NO per-ticker.
    Es un contador monotónico global que se incrementa por cada mensaje
    recibido en esa sid, intercalando entre todos los tickers suscritos.

    Implicación para este módulo: OrderbookState SOLO garantiza monotonicidad
    local (new_seq > self.sequence). La detección de gaps entre mensajes WS y
    la recuperación coordinada por sid es responsabilidad de OrderbookManager.

    REST GET /markets/{ticker}/orderbook retorna solo bids:
        {"orderbook": {"yes": [[price_cents, size], ...], "no": [[price_cents, size], ...]}}
    Precios ya en centavos enteros (no fixed-point). Sorted highest-bid first.

    WS orderbook_delta afecta bids únicamente. OrderbookManager normaliza los
    mensajes WS al formato dict que consume apply_delta().

Diseño:
    - Levels como dict[price_cents, size] → lookup O(1) por precio.
    - Best bid/ask computados on-demand (rápido en libros <50 niveles típicos).
    - Monotonicidad de seq validada en cada delta; gap detection en Manager.
    - Asks solo se populan si el WS snapshot los incluye explícitamente (raro).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["yes", "no"]


class OrderbookError(Exception):
    """Error base de la lógica de orderbook."""


class OrderbookNotInitializedError(OrderbookError):
    """Se intentó aplicar delta antes de cargar snapshot inicial."""


class OrderbookDesyncError(OrderbookError):
    """
    Raised by apply_delta when a delta produces new_qty < 0.

    new_qty < 0 indicates feed-level corruption: the delta is inconsistent
    with the current snapshot. Propagates to OrderbookManagerV2 which calls
    mark_stale() and requests a WS recovery snapshot.

    Attributes:
        ticker: Market ticker where the corruption was detected.
        price_cents: Price level that produced the negative qty.
        new_qty: The negative quantity (current_qty + delta).
    """

    def __init__(self, ticker: str, price_cents: int, new_qty: int) -> None:
        self.ticker = ticker
        self.price_cents = price_cents
        self.new_qty = new_qty
        super().__init__(
            f"{ticker} at {price_cents}c: delta produces qty={new_qty} < 0 (feed corruption)"
        )


@dataclass(frozen=True, slots=True)
class BookLevel:
    """Un nivel de precio en el book."""

    price_cents: int
    size: int


@dataclass(frozen=True, slots=True)
class BookTop:
    """Top of book: mejor bid y mejor ask para un side."""

    best_bid: BookLevel | None
    best_ask: BookLevel | None


class OrderbookState:
    """
    Estado en memoria del orderbook de un market.

    Lifecycle:
        1. Crear con __init__(ticker).
        2. Cargar snapshot inicial con apply_snapshot(snapshot_data).
        3. Aplicar deltas con apply_delta(delta_data) — valida monotonicidad.
        4. Leer top of book con top_of_book(side).
        5. En caso de desync (detectado por OrderbookManager): clear() + paso 2.

    Thread-safety:
        NO thread-safe. Cada market debe tener un solo writer.
        OrderbookManager garantiza esto procesando mensajes secuencialmente.
    """

    def __init__(self, ticker: str) -> None:
        if not ticker:
            raise ValueError("ticker requerido")
        self.ticker = ticker
        self._sequence: int = 0
        self._initialized: bool = False
        self._is_stale: bool = False
        self._last_ts_ms: int | None = None
        # Kalshi REST/WS expone solo bids; asks derivados en detector (Día 3).
        self._yes_bids: dict[int, int] = {}
        self._yes_asks: dict[int, int] = {}
        self._no_bids: dict[int, int] = {}
        self._no_asks: dict[int, int] = {}

    # =====================================================
    # State queries
    # =====================================================

    @property
    def sequence(self) -> int:
        """Último sequence number aplicado."""
        return self._sequence

    @property
    def is_initialized(self) -> bool:
        """True si ya se cargó snapshot inicial."""
        return self._initialized

    @property
    def is_stale(self) -> bool:
        """True si mark_stale() fue llamado y el snapshot de recovery aún no llegó."""
        return self._is_stale

    def is_empty(self) -> bool:
        """True si no hay levels en ningún lado."""
        return not (self._yes_bids or self._yes_asks or self._no_bids or self._no_asks)

    # =====================================================
    # Mutations
    # =====================================================

    def mark_stale(self) -> None:
        """
        Marca el state como stale: bloquea reads pero conserva datos.

        Usar cuando se detecta un gap en el sid y se espera recovery snapshot.
        El snapshot de recovery llama apply_snapshot() que limpia el flag.
        Diferente de clear(): los datos siguen en memoria para debugging.

        Example:
            >>> state = OrderbookState("TICKER")
            >>> state.apply_snapshot({"seq": 1, "yes": [[40, 100]], "no": []})
            >>> state.mark_stale()
            >>> state.is_initialized
            False
            >>> state.is_stale
            True
        """
        self._initialized = False
        self._is_stale = True

    def clear(self) -> None:
        """
        Vaciar el state completo.

        Usar antes de re-snapshot tras desync. Idempotente.

        Example:
            >>> state = OrderbookState("TICKER")
            >>> state.apply_snapshot({"seq": 1, "yes": [[40, 100]], "no": []})
            >>> state.clear()
            >>> state.is_initialized
            False
            >>> state.sequence
            0
        """
        self._sequence = 0
        self._initialized = False
        self._is_stale = False
        self._last_ts_ms = None
        self._yes_bids.clear()
        self._yes_asks.clear()
        self._no_bids.clear()
        self._no_asks.clear()

    def apply_snapshot(self, snapshot: dict) -> None:
        """
        Carga el estado base desde un snapshot. Reemplaza state previo de forma atómica.

        Levels con size <= 0 se ignoran. El sequence se fija al valor del snapshot.

        Args:
            snapshot: dict normalizado (Day 2 lo prepara desde REST o WS snapshot):
                {
                    "seq": int,                    # sequence number (>= 0)
                    "yes": [[price, size], ...],   # YES bids (de REST get_orderbook)
                    "no":  [[price, size], ...],   # NO bids  (de REST get_orderbook)
                    # Campos opcionales — solo WS orderbook_snapshot los incluye:
                    "yes_asks": [[price, size], ...],
                    "no_asks":  [[price, size], ...],
                }
                Day 2 extrae response["orderbook"] del REST y añade "seq" manualmente.

        Raises:
            ValueError: Si snapshot está malformado o contiene precios/sizes inválidos.

        Example:
            >>> state = OrderbookState("TICKER")
            >>> state.apply_snapshot({"seq": 100, "yes": [[40, 200]], "no": [[55, 100]]})
            >>> state.sequence
            100
            >>> state.is_initialized
            True
        """
        seq = snapshot.get("seq")
        if not isinstance(seq, int):
            raise ValueError(f"snapshot missing valid 'seq' (int), got: {seq!r}")

        yes_levels = snapshot.get("yes") or []
        no_levels = snapshot.get("no") or []
        yes_ask_levels = snapshot.get("yes_asks") or []
        no_ask_levels = snapshot.get("no_asks") or []

        new_yes_bids: dict[int, int] = {}
        for lvl in yes_levels:
            price, size = _parse_level(lvl, "yes bids")
            if size > 0:
                new_yes_bids[price] = size

        new_no_bids: dict[int, int] = {}
        for lvl in no_levels:
            price, size = _parse_level(lvl, "no bids")
            if size > 0:
                new_no_bids[price] = size

        new_yes_asks: dict[int, int] = {}
        for lvl in yes_ask_levels:
            price, size = _parse_level(lvl, "yes asks")
            if size > 0:
                new_yes_asks[price] = size

        new_no_asks: dict[int, int] = {}
        for lvl in no_ask_levels:
            price, size = _parse_level(lvl, "no asks")
            if size > 0:
                new_no_asks[price] = size

        # Atomic replace
        self._yes_bids = new_yes_bids
        self._no_bids = new_no_bids
        self._yes_asks = new_yes_asks
        self._no_asks = new_no_asks
        self._sequence = seq
        self._last_ts_ms = None
        self._is_stale = False
        self._initialized = True

    def apply_delta(self, delta: dict) -> None:
        """
        Aplica un delta del WebSocket validando monotonicidad de sequence.

        Kalshi WS orderbook_delta afecta BIDS únicamente. OrderbookManager
        normaliza el mensaje WS al formato esperado aquí.

        Garantía de este método: new_seq > self.sequence (monotonicidad local).
        Garantía que NO ofrece: detección de gaps entre mensajes WS consecutivos.
        Esa responsabilidad pertenece a OrderbookManager, que conoce el seq
        global por sid y puede coordinar recovery entre todos los tickers.

        Args:
            delta: dict normalizado por OrderbookManager:
                {
                    "side":  "yes" | "no",
                    "price": int,   # cents, rango [0, 100]
                    "delta": int,   # cambio en size (puede ser negativo)
                    "seq":   int,   # nuevo sequence number (> self.sequence)
                }

        Raises:
            OrderbookNotInitializedError: si apply_delta se llama antes de snapshot.
            ValueError: si delta está malformado, price/side inválidos, o seq no avanza.

        Example:
            >>> state = OrderbookState("TICKER")
            >>> state.apply_snapshot({"seq": 100, "yes": [[40, 200]], "no": []})
            >>> state.apply_delta({"side": "yes", "price": 40, "delta": -50, "seq": 101})
            >>> state.sequence
            101
            >>> state.top_of_book("yes").best_bid.size
            150
        """
        if not self._initialized:
            raise OrderbookNotInitializedError(
                f"Cannot apply delta to {self.ticker}: no snapshot loaded"
            )

        side = delta.get("side")
        price = delta.get("price")
        delta_size = delta.get("delta")
        new_seq = delta.get("seq")
        ts_ms = delta.get("ts_ms")

        if side not in ("yes", "no"):
            raise ValueError(f"Invalid side: {side!r}")
        if not isinstance(price, int) or not (0 <= price <= 100):
            raise ValueError(f"Invalid price_cents: {price!r}")
        if not isinstance(delta_size, int):
            raise ValueError(f"Invalid delta size: {delta_size!r}")
        if not isinstance(new_seq, int) or new_seq <= self._sequence:
            raise ValueError(
                f"Invalid new sequence: {new_seq!r} (must be > current {self._sequence})"
            )

        if ts_ms is not None:
            if not isinstance(ts_ms, (int, float)):
                raise ValueError(f"Invalid ts_ms: {ts_ms!r}")
            ts_ms_int = int(ts_ms)
            if self._last_ts_ms is not None and ts_ms_int < self._last_ts_ms:
                raise ValueError(f"ts_ms not monotonic: {ts_ms_int} < {self._last_ts_ms}")
            self._last_ts_ms = ts_ms_int

        # Kalshi orderbook_delta always targets bids
        book = self._yes_bids if side == "yes" else self._no_bids
        new_size = book.get(price, 0) + delta_size

        if new_size < 0:
            raise OrderbookDesyncError(
                ticker=self.ticker,
                price_cents=price,
                new_qty=new_size,
            )
        elif new_size == 0:
            book.pop(price, None)
        else:
            book[price] = new_size

        self._sequence = new_seq

    # =====================================================
    # Reads
    # =====================================================

    def top_of_book(self, side: Side) -> BookTop:
        """
        Mejor bid y mejor ask del side especificado.

        Returns:
            BookTop con best_bid (mayor precio) y best_ask (menor precio).
            Cualquiera puede ser None si no hay levels en ese lado.

        Raises:
            ValueError: si side no es "yes" o "no".

        Example:
            >>> state = OrderbookState("TICKER")
            >>> state.apply_snapshot({"seq": 1, "yes": [[40, 100], [35, 200]], "no": []})
            >>> top = state.top_of_book("yes")
            >>> top.best_bid.price_cents
            40
        """
        if side == "yes":
            bids, asks = self._yes_bids, self._yes_asks
        elif side == "no":
            bids, asks = self._no_bids, self._no_asks
        else:
            raise ValueError(f"Invalid side: {side!r}")

        best_bid = None
        if bids:
            p = max(bids.keys())
            best_bid = BookLevel(price_cents=p, size=bids[p])

        best_ask = None
        if asks:
            p = min(asks.keys())
            best_ask = BookLevel(price_cents=p, size=asks[p])

        return BookTop(best_bid=best_bid, best_ask=best_ask)

    def total_size(self, side: Side, kind: Literal["bid", "ask"]) -> int:
        """
        Suma de sizes en todos los levels del lado y tipo especificado.

        Example:
            >>> state = OrderbookState("TICKER")
            >>> state.apply_snapshot({"seq": 1, "yes": [[40, 100], [35, 50]], "no": []})
            >>> state.total_size("yes", "bid")
            150
        """
        if side == "yes" and kind == "bid":
            return sum(self._yes_bids.values())
        if side == "yes" and kind == "ask":
            return sum(self._yes_asks.values())
        if side == "no" and kind == "bid":
            return sum(self._no_bids.values())
        if side == "no" and kind == "ask":
            return sum(self._no_asks.values())
        raise ValueError(f"Invalid side/kind: {side!r}/{kind!r}")

    def snapshot_view(self) -> dict:
        """
        Vista read-only del state actual. Útil para debugging y tests.

        Returns COPIA de todos los dicts internos — modificar el resultado
        no afecta el state.

        Example:
            >>> state = OrderbookState("TICKER")
            >>> state.apply_snapshot({"seq": 5, "yes": [[42, 10]], "no": []})
            >>> view = state.snapshot_view()
            >>> view["sequence"]
            5
        """
        return {
            "ticker": self.ticker,
            "sequence": self._sequence,
            "initialized": self._initialized,
            "yes_bids": dict(self._yes_bids),
            "yes_asks": dict(self._yes_asks),
            "no_bids": dict(self._no_bids),
            "no_asks": dict(self._no_asks),
        }


# =====================================================
# Module-level helpers
# =====================================================


def _parse_level(level: object, context: str) -> tuple[int, int]:
    """
    Parsea un par [price, size] validando tipos y rangos.

    Raises:
        ValueError: si level no es [int, int] con price en [0, 100] y size >= 0.
    """
    if not isinstance(level, (list, tuple)) or len(level) != 2:  # type: ignore[arg-type]
        raise ValueError(f"Invalid level in {context}: expected [price, size], got {level!r}")
    price, size = level
    if not isinstance(price, int) or not (0 <= price <= 100):
        raise ValueError(f"Invalid price in {context}: {price!r} (must be int in [0, 100])")
    if not isinstance(size, int) or size < 0:
        raise ValueError(f"Invalid size in {context}: {size!r} (must be int >= 0)")
    return price, size
