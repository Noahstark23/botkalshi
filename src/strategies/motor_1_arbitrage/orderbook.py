"""
OrderbookState: estado en memoria del orderbook de un market.

Mantiene bids por side (yes/no), validados por sequence number.
100% puro: sin IO, sin async, sin dependencias externas. La capa de red
(Día 2) consume esta clase pero no la conoce internamente.

Contexto Kalshi:
    El orderbook expone BIDS por lado: cuánto paga alguien por YES y cuánto
    por NO. Las "asks" implícitas se derivan (100 - mejor_bid_opuesto) — esa
    conversión la hace el detector (Día 3), no este módulo.

    REST GET /markets/{ticker}/orderbook retorna solo bids:
        {"orderbook": {"yes": [[price, size], ...], "no": [[price, size], ...]}}
    sorted highest-bid first (ref: executor.py comments).

    WS orderbook_delta afecta bids únicamente. La capa de red (Día 2) normaliza
    los mensajes WS reales al formato dict que consume apply_delta(), incluyendo
    seq y previous_seq explícitos.

    Nota sobre sequence numbers: el _on_orderbook_delta actual (data_capture.py)
    NO extrae campos seq ni previous_seq del WS — Día 2 deberá añadir esa
    extracción del mensaje WS real para usar esta clase correctamente.

Diseño:
    - Levels como dict[price_cents, size] → lookup O(1) por precio.
    - Best bid/ask computados on-demand (rápido en libros <50 niveles típicos).
    - Sequence number validado estrictamente en cada delta.
    - Asks solo se populan si el WS snapshot los incluye explícitamente (raro).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["yes", "no"]


class OrderbookError(Exception):
    """Error base de la lógica de orderbook."""


class OrderbookDesyncError(OrderbookError):
    """
    Sequence number incorrecto: el orderbook local está desincronizado del server.

    El handler de capa superior (Día 2) debe:
    1. Loguear y record_error con el contexto del gap.
    2. Vaciar el state local (call clear()).
    3. Re-hacer snapshot via REST.
    4. Re-aplicar deltas pendientes desde el buffer.

    Attributes:
        ticker: Market ticker desincronizado.
        expected_seq: Sequence que esperábamos como previous_seq.
        received_prev_seq: Sequence que llegó como previous_seq en el delta.
    """

    def __init__(self, ticker: str, expected_seq: int, received_prev_seq: int) -> None:
        self.ticker = ticker
        self.expected_seq = expected_seq
        self.received_prev_seq = received_prev_seq
        super().__init__(
            f"Desync on {ticker}: expected previous_seq={expected_seq}, "
            f"received previous_seq={received_prev_seq}"
        )


class OrderbookNotInitializedError(OrderbookError):
    """Se intentó aplicar delta antes de cargar snapshot inicial."""


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
        3. Aplicar deltas con apply_delta(delta_data) — valida sequence.
        4. Leer top of book con top_of_book(side).
        5. En caso de desync: clear() + repetir desde paso 2.

    Thread-safety:
        NO thread-safe. Cada market debe tener un solo writer.
        En el detector (Día 2) garantizamos esto procesando deltas
        secuencialmente por ticker.
    """

    def __init__(self, ticker: str) -> None:
        if not ticker:
            raise ValueError("ticker requerido")
        self.ticker = ticker
        self._sequence: int = 0
        self._initialized: bool = False
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

    def is_empty(self) -> bool:
        """True si no hay levels en ningún lado."""
        return not (self._yes_bids or self._yes_asks or self._no_bids or self._no_asks)

    # =====================================================
    # Mutations
    # =====================================================

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
        self._initialized = True

    def apply_delta(self, delta: dict) -> None:
        """
        Aplica un delta del WebSocket validando sequence number.

        Kalshi WS orderbook_delta afecta BIDS únicamente. La capa de red (Día 2)
        normaliza el mensaje WS real a este formato, incluyendo seq y previous_seq
        (que el _on_orderbook_delta actual no extrae — requiere actualización en Día 2).

        Args:
            delta: dict normalizado por la capa de red:
                {
                    "side":         "yes" | "no",
                    "price":        int,   # cents, rango [0, 100]
                    "delta":        int,   # cambio en size (puede ser negativo)
                    "seq":          int,   # nuevo sequence number (> self.sequence)
                    "previous_seq": int,   # debe coincidir con self.sequence exactamente
                }

        Raises:
            OrderbookNotInitializedError: si apply_delta se llama antes de snapshot.
            OrderbookDesyncError: si delta["previous_seq"] != self.sequence.
            ValueError: si delta está malformado, price/side inválidos, o seq no avanza.

        Example:
            >>> state = OrderbookState("TICKER")
            >>> state.apply_snapshot({"seq": 100, "yes": [[40, 200]], "no": []})
            >>> state.apply_delta(
            ...     {"side": "yes", "price": 40, "delta": -50, "seq": 101, "previous_seq": 100}
            ... )
            >>> state.sequence
            101
            >>> state.top_of_book("yes").best_bid.size
            150
        """
        if not self._initialized:
            raise OrderbookNotInitializedError(
                f"Cannot apply delta to {self.ticker}: no snapshot loaded"
            )

        prev_seq = delta.get("previous_seq")
        if prev_seq is None:
            raise ValueError(f"delta missing 'previous_seq': {delta}")
        if not isinstance(prev_seq, int):
            raise ValueError(f"delta 'previous_seq' must be int, got: {prev_seq!r}")

        if prev_seq != self._sequence:
            raise OrderbookDesyncError(
                ticker=self.ticker,
                expected_seq=self._sequence,
                received_prev_seq=prev_seq,
            )

        side = delta.get("side")
        price = delta.get("price")
        delta_size = delta.get("delta")
        new_seq = delta.get("seq")

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

        # Kalshi orderbook_delta always targets bids
        book = self._yes_bids if side == "yes" else self._no_bids
        new_size = book.get(price, 0) + delta_size

        if new_size <= 0:
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
        raise ValueError(
            f"Invalid level in {context}: expected [price, size], got {level!r}"
        )
    price, size = level
    if not isinstance(price, int) or not (0 <= price <= 100):
        raise ValueError(
            f"Invalid price in {context}: {price!r} (must be int in [0, 100])"
        )
    if not isinstance(size, int) or size < 0:
        raise ValueError(
            f"Invalid size in {context}: {size!r} (must be int >= 0)"
        )
    return price, size
