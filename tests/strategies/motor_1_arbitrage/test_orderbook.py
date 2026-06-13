"""
Tests para OrderbookState — Día 1 (puro en memoria, sin red).

Cubre: construcción, apply_snapshot, apply_delta (happy path + validaciones),
top_of_book, clear, recovery, snapshot_view, y edge cases.

Todos los tests son sync — OrderbookState no tiene async.

Nota: previous_seq eliminado de apply_delta en refactor Día 2. El state
garantiza monotonicidad local; gap detection es responsabilidad de
OrderbookManager (ver test_orderbook_manager.py).
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
) -> dict:
    return {
        "side": side,
        "price": price,
        "delta": delta,
        "seq": seq,
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
    assert state.top_of_book("yes").best_bid is not None

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
    state.apply_delta(make_delta(side="yes", price=40, delta=50, seq=101))
    assert state.sequence == 101
    assert state.top_of_book("yes").best_bid == BookLevel(price_cents=40, size=250)


def test_delta_positive_adds_size() -> None:
    state = initialized_state(yes=[[40, 50]])
    state.apply_delta(make_delta(side="yes", price=40, delta=10, seq=101))
    assert state.top_of_book("yes").best_bid.size == 60  # type: ignore[union-attr]


def test_delta_negative_reduces_size() -> None:
    state = initialized_state(yes=[[40, 50]])
    state.apply_delta(make_delta(side="yes", price=40, delta=-10, seq=101))
    assert state.top_of_book("yes").best_bid.size == 40  # type: ignore[union-attr]


def test_delta_to_zero_removes_level() -> None:
    state = initialized_state(yes=[[40, 10]])
    state.apply_delta(make_delta(side="yes", price=40, delta=-10, seq=101))
    assert state.top_of_book("yes").best_bid is None


def test_delta_to_negative_raises_orderbook_desync() -> None:
    """Delta mayor al size existente: raises OrderbookDesyncError (feed corruption)."""
    state = initialized_state(yes=[[40, 10]])
    with pytest.raises(OrderbookDesyncError) as exc_info:
        state.apply_delta(make_delta(side="yes", price=40, delta=-15, seq=101))
    assert exc_info.value.ticker == "KXMLB-24"
    assert exc_info.value.price_cents == 40
    assert exc_info.value.new_qty == -5


def test_delta_on_no_side_updates_no_bids() -> None:
    state = initialized_state(no=[[55, 80]])
    state.apply_delta(make_delta(side="no", price=55, delta=20, seq=101))
    assert state.top_of_book("no").best_bid.size == 100  # type: ignore[union-attr]


def test_delta_new_price_level_created() -> None:
    """Delta en un precio que no existía crea el level."""
    state = initialized_state(yes=[[40, 100]])
    state.apply_delta(make_delta(side="yes", price=38, delta=50, seq=101))
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


def test_delta_with_seq_not_advancing_raises() -> None:
    """seq == self.sequence (no avanza) → ValueError."""
    state = initialized_state(seq=100)
    with pytest.raises(ValueError, match="Invalid new sequence"):
        state.apply_delta(make_delta(seq=100))


def test_delta_seq_less_than_current_raises() -> None:
    """seq < self.sequence → ValueError."""
    state = initialized_state(seq=100)
    with pytest.raises(ValueError, match="Invalid new sequence"):
        state.apply_delta(make_delta(seq=99))


def test_delta_with_gap_is_applied_without_error() -> None:
    """
    Dos deltas con seq 100→102 (saltándose el 101) se aplican sin error.

    OrderbookState NO detecta gaps; solo valida monotonicidad local.
    La detección de gaps es responsabilidad de OrderbookManager.
    """
    state = initialized_state(seq=100, yes=[[40, 100]])
    state.apply_delta(make_delta(side="yes", price=40, delta=10, seq=102))
    assert state.sequence == 102
    assert state.top_of_book("yes").best_bid.size == 110  # type: ignore[union-attr]


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
# clear y recovery
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
    state.clear()
    assert not state.is_initialized
    assert state.is_empty()


def test_recovery_pattern() -> None:
    """
    Ciclo completo de recovery: clear() + nuevo snapshot REST.

    En el flujo real, OrderbookManager detecta el gap de seq,
    llama clear() y luego aplica un snapshot REST (seq=0).
    El delta WS siguiente tendrá seq > 0 (cumple monotonicidad).
    """
    state = initialized_state(seq=100, yes=[[40, 200]])
    assert state.is_initialized

    # Recovery: clear + nuevo snapshot (simula lo que hace OrderbookManager)
    state.clear()
    assert not state.is_initialized

    state.apply_snapshot(make_snapshot(seq=0, yes=[[42, 150]]))
    assert state.is_initialized
    assert state.sequence == 0
    assert state.top_of_book("yes").best_bid == BookLevel(price_cents=42, size=150)

    # Delta WS posterior (seq=110 > 0) se aplica sin error
    state.apply_delta(make_delta(side="yes", price=42, delta=10, seq=110))
    assert state.sequence == 110


# =====================================================
# snapshot_view
# =====================================================


def test_snapshot_view_returns_copy() -> None:
    """Modificar el dict retornado NO afecta el state interno."""
    state = initialized_state(yes=[[40, 100]])

    view = state.snapshot_view()
    original_yes_bids = dict(view["yes_bids"])

    view["yes_bids"][40] = 9999
    view["yes_bids"][99] = 1

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

    state.apply_delta(make_delta(price=0, delta=50, seq=101))
    assert state.snapshot_view()["yes_bids"].get(0) == 50

    state.apply_delta(make_delta(price=100, delta=30, seq=102))
    assert state.snapshot_view()["yes_bids"].get(100) == 30


def test_multiple_levels_same_side() -> None:
    """Deltas en distintos precios del mismo side coexisten en el dict."""
    state = initialized_state(yes=[[40, 100]])

    state.apply_delta(make_delta(price=40, delta=10, seq=101))
    state.apply_delta(make_delta(price=35, delta=50, seq=102))
    state.apply_delta(make_delta(price=45, delta=30, seq=103))

    bids = state.snapshot_view()["yes_bids"]
    assert bids == {40: 110, 35: 50, 45: 30}
    assert state.total_size("yes", "bid") == 190


def test_sequence_skipping_is_valid() -> None:
    """
    Seq salta de 100 a 105 (gap de 4) — válido desde la perspectiva del state.
    OrderbookState solo valida monotonicidad; el gap lo detecta OrderbookManager.
    """
    state = initialized_state(seq=100, yes=[[40, 100]])
    state.apply_delta(make_delta(price=40, delta=10, seq=105))
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

    state.apply_delta(make_delta(price=40, delta=50, seq=101))
    state.apply_delta(make_delta(price=38, delta=30, seq=102))

    state.apply_snapshot(make_snapshot(seq=200, yes=[[45, 75]], no=[[50, 60]]))
    assert state.sequence == 200
    assert state.snapshot_view()["yes_bids"] == {45: 75}
    assert 40 not in state.snapshot_view()["yes_bids"]
    assert 38 not in state.snapshot_view()["yes_bids"]


# =====================================================
# mark_stale / is_stale
# =====================================================


def test_mark_stale_blocks_reads() -> None:
    """mark_stale() → is_initialized=False, is_stale=True; data preserved."""
    state = initialized_state(yes=[[40, 100]], seq=50)
    assert state.is_initialized

    state.mark_stale()

    assert not state.is_initialized
    assert state.is_stale
    # Data still present internally (useful for debugging)
    assert state.snapshot_view()["yes_bids"] == {40: 100}
    assert state.sequence == 50


def test_apply_snapshot_clears_stale() -> None:
    """apply_snapshot() after mark_stale() restores initialized=True, is_stale=False."""
    state = initialized_state(yes=[[40, 100]], seq=50)
    state.mark_stale()
    assert state.is_stale

    state.apply_snapshot(make_snapshot(seq=60, yes=[[42, 200]]))

    assert state.is_initialized
    assert not state.is_stale
    assert state.sequence == 60
    assert state.top_of_book("yes").best_bid == BookLevel(price_cents=42, size=200)  # type: ignore[union-attr]


def test_clear_resets_stale_flag() -> None:
    """clear() resets both is_initialized and is_stale."""
    state = initialized_state()
    state.mark_stale()
    assert state.is_stale

    state.clear()

    assert not state.is_initialized
    assert not state.is_stale


def test_mark_stale_idempotent() -> None:
    state = initialized_state()
    state.mark_stale()
    state.mark_stale()  # second call must not raise
    assert state.is_stale
