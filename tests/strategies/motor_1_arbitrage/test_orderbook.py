"""
Tests para OrderbookState — Día 1 (puro en memoria, sin red).

Cubre: construcción, apply_snapshot, apply_delta (happy path + validaciones),
top_of_book, clear, desync recovery, snapshot_view, y edge cases.

Todos los tests son sync — Día 1 no tiene async.
"""
from __future__ import annotations

import pytest

from src.strategies.motor_1_arbitrage.orderbook import (
    BookLevel,
    BookTop,
    OrderbookDesyncError,
    OrderbookNotInitializedError,
    OrderbookState,
)


# =====================================================
# Fixtures helpers
# =====================================================


def make_snapshot(
    seq: int = 100,
    yes: list | None = None,
    no: list | None = None,
    yes_asks: list | None = None,
    no_asks: list | None = None,
) -> dict:
    snap: dict = {"seq": seq}
    snap["yes"] = yes if yes is not None else []
    snap["no"] = no if no is not None else []
    if yes_asks is not None:
        snap["yes_asks"] = yes_asks
    if no_asks is not None:
        snap["no_asks"] = no_asks
    return snap


def make_delta(
    side: str = "yes",
    price: int = 40,
    delta: int = 10,
    seq: int = 101,
    previous_seq: int = 100,
) -> dict:
    return {
        "side": side,
        "price": price,
        "delta": delta,
        "seq": seq,
        "previous_seq": previous_seq,
    }


def initialized_state(
    ticker: str = "KXMLB-24",
    yes: list | None = None,
    no: list | None = None,
    seq: int = 100,
) -> OrderbookState:
    """Devuelve un OrderbookState ya inicializado con snapshot."""
    state = OrderbookState(ticker)
    state.apply_snapshot(make_snapshot(seq=seq, yes=yes, no=no))
    return state


# =====================================================
# Construcción
# =====================================================


def test_init_empty_ticker_raises() -> None:
    with pytest.raises(ValueError, match="ticker requerido"):
        OrderbookState("")


def test_init_starts_empty() -> None:
    state = OrderbookState("KXMLB-24")
    assert state.is_empty()
    assert state.sequence == 0
    assert not state.is_initialized


# =====================================================
# apply_snapshot
# =====================================================


def test_snapshot_loads_levels() -> None:
    state = OrderbookState("TICKER")
    state.apply_snapshot(
        make_snapshot(
            seq=100,
            yes=[[40, 200], [35, 150]],
            no=[[55, 80]],
        )
    )
    yes_top = state.top_of_book("yes")
    assert yes_top.best_bid is not None
    assert yes_top.best_bid.price_cents == 40
    assert yes_top.best_bid.size == 200

    no_top = state.top_of_book("no")
    assert no_top.best_bid is not None
    assert no_top.best_bid.price_cents == 55
    assert no_top.best_bid.size == 80


def test_snapshot_loads_asks_optional() -> None:
    """WS snapshots pueden incluir asks explícitos."""
    state = OrderbookState("TICKER")
    state.apply_snapshot(
        make_snapshot(
            seq=50,
            yes=[[40, 100]],
            no=[[55, 80]],
            yes_asks=[[42, 30]],
            no_asks=[[58, 20]],
        )
    )
    assert state.top_of_book("yes").best_ask == BookLevel(price_cents=42, size=30)
    assert state.top_of_book("no").best_ask == BookLevel(price_cents=58, size=20)


def test_snapshot_sets_sequence() -> None:
    state = OrderbookState("TICKER")
    state.apply_snapshot(make_snapshot(seq=100))
    assert state.sequence == 100


def test_snapshot_marks_initialized() -> None:
    state = OrderbookState("TICKER")
    assert not state.is_initialized
    state.apply_snapshot(make_snapshot(seq=1))
    assert state.is_initialized


def test_snapshot_resets_previous_state() -> None:
    """Segundo apply_snapshot reemplaza el state del primero completamente."""
    state = OrderbookState("TICKER")
    state.apply_snapshot(make_snapshot(seq=100, yes=[[40, 200]]))
    # Estado intermedio verificado
    assert state.top_of_book("yes").best_bid is not None

    # Segundo snapshot sin levels YES
    state.apply_snapshot(make_snapshot(seq=200, yes=[], no=[[60, 100]]))
    assert state.sequence == 200
    assert state.top_of_book("yes").best_bid is None
    assert state.top_of_book("no").best_bid is not None


def test_snapshot_malformed_seq_missing_raises() -> None:
    state = OrderbookState("TICKER")
    with pytest.raises(ValueError, match="seq"):
        state.apply_snapshot({"yes": [], "no": []})


def test_snapshot_malformed_seq_string_raises() -> None:
    state = OrderbookState("TICKER")
    with pytest.raises(ValueError, match="seq"):
        state.apply_snapshot({"seq": "100", "yes": [], "no": []})


def test_snapshot_prices_out_of_range_high() -> None:
    state = OrderbookState("TICKER")
    with pytest.raises(ValueError, match="Invalid price"):
        state.apply_snapshot(make_snapshot(yes=[[101, 100]]))


def test_snapshot_prices_out_of_range_negative() -> None:
    state = OrderbookState("TICKER")
    with pytest.raises(ValueError, match="Invalid price"):
        state.apply_snapshot(make_snapshot(yes=[[-1, 100]]))


def test_snapshot_level_wrong_shape_raises() -> None:
    state = OrderbookState("TICKER")
    with pytest.raises(ValueError, match="expected \\[price, size\\]"):
        state.apply_snapshot(make_snapshot(yes=[[40]]))


def test_snapshot_float_price_raises() -> None:
    state = OrderbookState("TICKER")
    with pytest.raises(ValueError, match="Invalid price"):
        state.apply_snapshot(make_snapshot(yes=[[40.5, 100]]))


def test_snapshot_negative_size_raises() -> None:
    state = OrderbookState("TICKER")
    with pytest.raises(ValueError, match="Invalid size"):
        state.apply_snapshot(make_snapshot(yes=[[40, -1]]))


def test_snapshot_zero_size_level_ignored() -> None:
    """Level con size=0 no se agrega al book."""
    state = OrderbookState("TICKER")
    state.apply_snapshot(make_snapshot(yes=[[40, 0], [35, 100]]))
    assert state.top_of_book("yes").best_bid == BookLevel(price_cents=35, size=100)


# =====================================================
# apply_delta — happy path
# =====================================================


def test_delta_after_snapshot_updates_book() -> None:
    state = initialized_state(yes=[[40, 200]])
    state.apply_delta(make_delta(side="yes", price=40, delta=50, seq=101, previous_seq=100))
    assert state.sequence == 101
    assert state.top_of_book("yes").best_bid == BookLevel(price_cents=40, size=250)


def test_delta_positive_adds_size() -> None:
    state = initialized_state(yes=[[40, 50]])
    state.apply_delta(make_delta(side="yes", price=40, delta=10, seq=101, previous_seq=100))
    assert state.top_of_book("yes").best_bid.size == 60  # type: ignore[union-attr]


def test_delta_negative_reduces_size() -> None:
    state = initialized_state(yes=[[40, 50]])
    state.apply_delta(make_delta(side="yes", price=40, delta=-10, seq=101, previous_seq=100))
    assert state.top_of_book("yes").best_bid.size == 40  # type: ignore[union-attr]


def test_delta_to_zero_removes_level() -> None:
    state = initialized_state(yes=[[40, 10]])
    state.apply_delta(make_delta(side="yes", price=40, delta=-10, seq=101, previous_seq=100))
    assert state.top_of_book("yes").best_bid is None


def test_delta_to_negative_removes_level() -> None:
    """Delta mayor al size existente: el level desaparece, no quedan sizes negativos."""
    state = initialized_state(yes=[[40, 10]])
    state.apply_delta(make_delta(side="yes", price=40, delta=-15, seq=101, previous_seq=100))
    assert state.top_of_book("yes").best_bid is None
    # Verificar que el dict interno no tiene el price (no tamaños negativos)
    view = state.snapshot_view()
    assert 40 not in view["yes_bids"]


def test_delta_on_no_side_updates_no_bids() -> None:
    state = initialized_state(no=[[55, 80]])
    state.apply_delta(make_delta(side="no", price=55, delta=20, seq=101, previous_seq=100))
    assert state.top_of_book("no").best_bid.size == 100  # type: ignore[union-attr]


def test_delta_new_price_level_created() -> None:
    """Delta en un precio que no existía crea el level."""
    state = initialized_state(yes=[[40, 100]])
    state.apply_delta(make_delta(side="yes", price=38, delta=50, seq=101, previous_seq=100))
    top = state.top_of_book("yes")
    assert top.best_bid == BookLevel(price_cents=40, size=100)
    assert state.total_size("yes", "bid") == 150


# =====================================================
# apply_delta — validaciones
# =====================================================


def test_delta_before_snapshot_raises() -> None:
    state = OrderbookState("TICKER")
    with pytest.raises(OrderbookNotInitializedError):
        state.apply_delta(make_delta())


def test_delta_with_wrong_previous_seq_raises_desync() -> None:
    state = initialized_state(seq=100)
    with pytest.raises(OrderbookDesyncError) as exc_info:
        state.apply_delta(make_delta(seq=101, previous_seq=99))
    err = exc_info.value
    assert err.expected_seq == 100
    assert err.received_prev_seq == 99
    assert err.ticker == "KXMLB-24"


def test_delta_with_seq_not_advancing_raises() -> None:
    """seq == self.sequence (no avanza) → ValueError."""
    state = initialized_state(seq=100)
    with pytest.raises(ValueError, match="Invalid new sequence"):
        state.apply_delta(make_delta(seq=100, previous_seq=100))


def test_delta_seq_less_than_current_raises() -> None:
    """seq < self.sequence → ValueError."""
    state = initialized_state(seq=100)
    with pytest.raises(ValueError, match="Invalid new sequence"):
        state.apply_delta(make_delta(seq=99, previous_seq=100))


def test_delta_invalid_side_raises() -> None:
    state = initialized_state()
    with pytest.raises(ValueError, match="Invalid side"):
        state.apply_delta(make_delta(side="invalid"))


def test_delta_price_out_of_range_high_raises() -> None:
    state = initialized_state()
    with pytest.raises(ValueError, match="Invalid price_cents"):
        state.apply_delta(make_delta(price=101))


def test_delta_price_out_of_range_negative_raises() -> None:
    state = initialized_state()
    with pytest.raises(ValueError, match="Invalid price_cents"):
        state.apply_delta(make_delta(price=-5))


def test_delta_float_price_raises() -> None:
    state = initialized_state()
    with pytest.raises(ValueError, match="Invalid price_cents"):
        state.apply_delta(make_delta(price=40.5))  # type: ignore[arg-type]


def test_delta_missing_previous_seq_raises() -> None:
    state = initialized_state()
    d = {"side": "yes", "price": 40, "delta": 10, "seq": 101}
    with pytest.raises(ValueError, match="previous_seq"):
        state.apply_delta(d)


def test_delta_non_int_previous_seq_raises() -> None:
    state = initialized_state()
    d = make_delta()
    d["previous_seq"] = "100"
    with pytest.raises(ValueError, match="previous_seq.*int"):
        state.apply_delta(d)


def test_delta_float_delta_size_raises() -> None:
    state = initialized_state()
    with pytest.raises(ValueError, match="Invalid delta size"):
        state.apply_delta(make_delta(delta=10.5))  # type: ignore[arg-type]


# =====================================================
# top_of_book
# =====================================================


def test_top_of_book_empty_side_returns_none() -> None:
    state = initialized_state(yes=[], no=[])
    top = state.top_of_book("yes")
    assert top == BookTop(best_bid=None, best_ask=None)


def test_top_of_book_returns_max_bid_min_ask() -> None:
    state = OrderbookState("TICKER")
    # Bids: 40 y 35. Best bid = max = 40.
    # Asks explícitos: 42 y 45. Best ask = min = 42.
    state.apply_snapshot(
        make_snapshot(
            yes=[[40, 100], [35, 200]],
            yes_asks=[[42, 50], [45, 30]],
        )
    )
    top = state.top_of_book("yes")
    assert top.best_bid == BookLevel(price_cents=40, size=100)
    assert top.best_ask == BookLevel(price_cents=42, size=50)


def test_top_of_book_single_level() -> None:
    state = initialized_state(yes=[[50, 100]])
    top = state.top_of_book("yes")
    assert top.best_bid == BookLevel(price_cents=50, size=100)
    assert top.best_ask is None


def test_top_of_book_invalid_side_raises() -> None:
    state = initialized_state()
    with pytest.raises(ValueError, match="Invalid side"):
        state.top_of_book("invalid")  # type: ignore[arg-type]


# =====================================================
# clear y desync recovery
# =====================================================


def test_clear_resets_state() -> None:
    state = initialized_state(yes=[[40, 100]])
    assert state.is_initialized
    assert state.sequence == 100

    state.clear()

    assert not state.is_initialized
    assert state.sequence == 0
    assert state.is_empty()


def test_clear_idempotent() -> None:
    state = OrderbookState("TICKER")
    state.clear()  # No debe explotar en state vacío
    assert not state.is_initialized
    assert state.is_empty()


def test_recovery_pattern() -> None:
    """Ciclo completo de desync recovery: delta inválido → clear → nuevo snapshot."""
    state = initialized_state(seq=100, yes=[[40, 200]])

    # Delta con previous_seq incorrecto (gap en la secuencia)
    with pytest.raises(OrderbookDesyncError):
        state.apply_delta(make_delta(seq=103, previous_seq=102))

    # State todavía tiene el snapshot anterior (delta no se aplicó)
    assert state.sequence == 100
    assert state.is_initialized

    # Recovery: clear + nuevo snapshot
    state.clear()
    assert not state.is_initialized

    state.apply_snapshot(make_snapshot(seq=110, yes=[[42, 150]]))
    assert state.is_initialized
    assert state.sequence == 110
    assert state.top_of_book("yes").best_bid == BookLevel(price_cents=42, size=150)


# =====================================================
# snapshot_view
# =====================================================


def test_snapshot_view_returns_copy() -> None:
    """Modificar el dict retornado NO afecta el state interno."""
    state = initialized_state(yes=[[40, 100]])

    view = state.snapshot_view()
    original_yes_bids = dict(view["yes_bids"])

    # Mutar el dict retornado
    view["yes_bids"][40] = 9999
    view["yes_bids"][99] = 1

    # State interno intacto
    assert state.top_of_book("yes").best_bid.size == 100  # type: ignore[union-attr]
    assert state.snapshot_view()["yes_bids"] == original_yes_bids


def test_snapshot_view_fields() -> None:
    state = initialized_state(yes=[[40, 100]], no=[[55, 80]], seq=42)
    view = state.snapshot_view()

    assert view["ticker"] == "KXMLB-24"
    assert view["sequence"] == 42
    assert view["initialized"] is True
    assert view["yes_bids"] == {40: 100}
    assert view["no_bids"] == {55: 80}
    assert view["yes_asks"] == {}
    assert view["no_asks"] == {}


# =====================================================
# Edge cases
# =====================================================


def test_delta_price_at_boundaries() -> None:
    """price=0 y price=100 son válidos aunque semánticamente raros en Kalshi."""
    state = initialized_state()

    state.apply_delta(make_delta(price=0, delta=50, seq=101, previous_seq=100))
    assert state.snapshot_view()["yes_bids"].get(0) == 50

    state.apply_delta(make_delta(price=100, delta=30, seq=102, previous_seq=101))
    assert state.snapshot_view()["yes_bids"].get(100) == 30


def test_multiple_levels_same_side() -> None:
    """Deltas en distintos precios del mismo side coexisten en el dict."""
    state = initialized_state(yes=[[40, 100]])

    state.apply_delta(make_delta(price=40, delta=10, seq=101, previous_seq=100))
    state.apply_delta(make_delta(price=35, delta=50, seq=102, previous_seq=101))
    state.apply_delta(make_delta(price=45, delta=30, seq=103, previous_seq=102))

    bids = state.snapshot_view()["yes_bids"]
    assert bids == {40: 110, 35: 50, 45: 30}
    assert state.total_size("yes", "bid") == 190


def test_sequence_skipping_is_valid() -> None:
    """
    Kalshi puede saltar sequence numbers (seq=105 tras seq=100 es válido).
    Lo que importa para desync es que previous_seq == self.sequence exactamente.

    Nota: la tarea original llama este test 'test_sequence_skipping_raises' pero
    su descripción dice que es VÁLIDO (no raises). Renombrado para reflejar el
    comportamiento real.
    """
    state = initialized_state(seq=100, yes=[[40, 100]])

    # seq salta de 100 a 105 (válido, Kalshi puede saltar seq)
    state.apply_delta(make_delta(price=40, delta=10, seq=105, previous_seq=100))
    assert state.sequence == 105


def test_total_size_all_combinations() -> None:
    """total_size cubre los 4 casos: bid/ask × yes/no."""
    state = OrderbookState("TICKER")
    state.apply_snapshot(
        make_snapshot(
            yes=[[40, 100], [35, 50]],
            no=[[55, 80]],
            yes_asks=[[42, 20]],
            no_asks=[[58, 15]],
        )
    )
    assert state.total_size("yes", "bid") == 150
    assert state.total_size("yes", "ask") == 20
    assert state.total_size("no", "bid") == 80
    assert state.total_size("no", "ask") == 15


def test_total_size_empty_returns_zero() -> None:
    state = initialized_state()
    assert state.total_size("yes", "bid") == 0
    assert state.total_size("no", "ask") == 0


def test_apply_snapshot_after_deltas() -> None:
    """Snapshot tras varios deltas resetea todo el state limpiamente."""
    state = initialized_state(yes=[[40, 100]], seq=100)

    state.apply_delta(make_delta(price=40, delta=50, seq=101, previous_seq=100))
    state.apply_delta(make_delta(price=38, delta=30, seq=102, previous_seq=101))

    # Nuevo snapshot borra todo lo anterior
    state.apply_snapshot(make_snapshot(seq=200, yes=[[45, 75]], no=[[50, 60]]))
    assert state.sequence == 200
    assert state.snapshot_view()["yes_bids"] == {45: 75}
    # El price 40 del state anterior ya no está
    assert 40 not in state.snapshot_view()["yes_bids"]
    assert 38 not in state.snapshot_view()["yes_bids"]
