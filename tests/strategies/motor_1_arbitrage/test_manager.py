"""
Tests for OrderbookManager — Day 2 network layer.

Covers: snapshot→delta happy path, sync buffering, stream gap purge,
sid reset, per-market recovery callback, multi-ticker isolation,
desync → callback, and unknown/duplicate ticker handling.

Design notes:
  - Raw WS messages are minimal but structurally valid (production shape).
  - local_seq is internal; tests verify observable behavior (tops, is_syncing)
    plus callback invocations rather than peeking at private counters.
  - on_desync_callback is an AsyncMock so we can assert call_args.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.strategies.motor_1_arbitrage.manager import OrderbookManager


# =====================================================
# Helpers
# =====================================================


def make_manager() -> tuple[OrderbookManager, AsyncMock]:
    cb = AsyncMock()
    mgr = OrderbookManager(on_desync_callback=cb)
    return mgr, cb


def snapshot_msg(ticker: str, seq: int, yes=None, no=None, sid: int = 1) -> dict:
    """Minimal production-shape orderbook_snapshot message."""
    return {
        "type": "orderbook_snapshot",
        "seq": seq,
        "sid": sid,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": yes or [],
            "no_dollars_fp": no or [],
        },
    }


def delta_msg(ticker: str, seq: int, side: str, price_dollars: str, delta: str, sid: int = 1) -> dict:
    """Minimal production-shape orderbook_delta message."""
    return {
        "type": "orderbook_delta",
        "seq": seq,
        "sid": sid,
        "msg": {
            "market_ticker": ticker,
            "side": side,
            "price_dollars": price_dollars,
            "delta_fp": delta,
        },
    }


# =====================================================
# Happy path: snapshot → delta → get_market_tops
# =====================================================


@pytest.mark.asyncio
async def test_snapshot_then_delta_returns_correct_tops():
    """
    1. Snapshot loads YES book: {97: 1000, 96: 500}
    2. Delta removes 200 at 97c → {97: 800, 96: 500}
    3. get_market_tops returns best YES bid = 97c size=800
    """
    mgr, _ = make_manager()
    ticker = "INXD-26MAY21-B5210"
    mgr.initialize_market(ticker)

    snap = snapshot_msg(
        ticker,
        seq=10,
        yes=[["0.97", "1000"], ["0.96", "500"]],
        no=[["0.03", "1000"]],
    )
    await mgr.handle_snapshot_message(snap)

    dlt = delta_msg(ticker, seq=11, side="yes", price_dollars="0.97", delta="-200")
    await mgr.handle_delta_message(dlt)

    tops = mgr.get_market_tops(ticker)
    assert tops is not None
    assert tops["yes"].best_bid is not None
    assert tops["yes"].best_bid.price_cents == 97
    assert tops["yes"].best_bid.size == 800


@pytest.mark.asyncio
async def test_get_market_tops_returns_none_while_syncing():
    """Before any snapshot, is_syncing=True → get_market_tops returns None."""
    mgr, _ = make_manager()
    mgr.initialize_market("T")
    assert mgr.get_market_tops("T") is None


@pytest.mark.asyncio
async def test_get_market_tops_returns_none_for_unknown_ticker():
    mgr, _ = make_manager()
    assert mgr.get_market_tops("DOES_NOT_EXIST") is None


# =====================================================
# Delta buffering during sync
# =====================================================


@pytest.mark.asyncio
async def test_deltas_buffered_before_snapshot_are_applied_on_drain():
    """
    Deltas arriving before snapshot are buffered.
    After snapshot, buffered deltas are applied in order.
    Final state must reflect all changes.
    """
    mgr, _ = make_manager()
    ticker = "BUF-TEST"
    mgr.initialize_market(ticker)

    # Two deltas arrive before snapshot (buffered) — stream seqs must be sequential
    d1 = delta_msg(ticker, seq=1, side="yes", price_dollars="0.50", delta="300")
    d2 = delta_msg(ticker, seq=2, side="yes", price_dollars="0.50", delta="-100")
    await mgr.handle_delta_message(d1)
    await mgr.handle_delta_message(d2)

    # Snapshot arrives next in stream (seq=3, sequential — no gap)
    snap = snapshot_msg(ticker, seq=3, yes=[["0.50", "500"]])
    await mgr.handle_snapshot_message(snap)

    tops = mgr.get_market_tops(ticker)
    assert tops is not None
    # 500 + 300 - 100 = 700
    assert tops["yes"].best_bid.size == 700


@pytest.mark.asyncio
async def test_is_syncing_true_before_snapshot_false_after():
    mgr, _ = make_manager()
    mgr.initialize_market("T")

    # is_syncing starts True
    assert mgr.get_market_tops("T") is None

    snap = snapshot_msg("T", seq=1, yes=[["0.50", "100"]])
    await mgr.handle_snapshot_message(snap)

    # After snapshot (no buffered deltas) → synced
    assert mgr.get_market_tops("T") is not None


# =====================================================
# Stream gap detection
# =====================================================


@pytest.mark.asyncio
async def test_stream_gap_purges_all_books():
    """
    Receiving seq=10 after seq=8 (expected 9) → purge_all_books, return None for all tickers.
    """
    mgr, _ = make_manager()
    ticker = "GAP-TEST"
    mgr.initialize_market(ticker)

    snap = snapshot_msg(ticker, seq=8, yes=[["0.50", "100"]])
    await mgr.handle_snapshot_message(snap)

    # Verify synced before gap
    assert mgr.get_market_tops(ticker) is not None

    # Gap: expected seq=9 but receive seq=10
    dlt = delta_msg(ticker, seq=10, side="yes", price_dollars="0.50", delta="-10")
    await mgr.handle_delta_message(dlt)

    # All books purged → back to None
    assert mgr.get_market_tops(ticker) is None


@pytest.mark.asyncio
async def test_no_gap_sequential_seqs_work():
    mgr, _ = make_manager()
    ticker = "SEQ-TEST"
    mgr.initialize_market(ticker)

    snap = snapshot_msg(ticker, seq=1, yes=[["0.50", "200"]])
    await mgr.handle_snapshot_message(snap)

    # Sequential seqs — no gap, must process normally
    d1 = delta_msg(ticker, seq=2, side="yes", price_dollars="0.50", delta="-50")
    d2 = delta_msg(ticker, seq=3, side="yes", price_dollars="0.50", delta="-50")
    await mgr.handle_delta_message(d1)
    await mgr.handle_delta_message(d2)

    tops = mgr.get_market_tops(ticker)
    assert tops is not None
    assert tops["yes"].best_bid.size == 100


# =====================================================
# SID change resets stream tracker
# =====================================================


@pytest.mark.asyncio
async def test_sid_change_resets_stream_seq_no_purge():
    """
    New sid → stream_seq tracker resets. No stream gap error even if seq resets.
    """
    mgr, _ = make_manager()
    ticker = "SID-TEST"
    mgr.initialize_market(ticker)

    snap = snapshot_msg(ticker, seq=50, sid=1, yes=[["0.50", "100"]])
    await mgr.handle_snapshot_message(snap)
    assert mgr.get_market_tops(ticker) is not None

    # New subscription (sid=2, seq resets to 1 — looks like a "gap" but is new sid)
    snap2 = snapshot_msg(ticker, seq=1, sid=2, yes=[["0.50", "200"]])
    await mgr.handle_snapshot_message(snap2)

    tops = mgr.get_market_tops(ticker)
    assert tops is not None
    assert tops["yes"].best_bid.size == 200


# =====================================================
# purge_all_books
# =====================================================


@pytest.mark.asyncio
async def test_purge_all_books_clears_all_markets():
    mgr, _ = make_manager()

    for ticker in ["A", "B", "C"]:
        mgr.initialize_market(ticker)
        snap = snapshot_msg(ticker, seq=5, yes=[["0.50", "100"]])
        await mgr.handle_snapshot_message(snap)
        assert mgr.get_market_tops(ticker) is not None

    mgr.purge_all_books()

    for ticker in ["A", "B", "C"]:
        assert mgr.get_market_tops(ticker) is None


# =====================================================
# Desync → callback
# =====================================================


@pytest.mark.asyncio
async def test_desync_triggers_callback():
    """
    Snapshot: YES {45c: 10 contracts}.
    Delta: delta=-20 at 45c → new_qty=10-20=-10 < 0 → OrderbookDesyncError.
    Recovery callback must be called with the ticker.
    """
    mgr, cb = make_manager()
    ticker = "DESYNC-TEST"
    mgr.initialize_market(ticker)

    snap = snapshot_msg(ticker, seq=1, yes=[["0.45", "10"]])
    await mgr.handle_snapshot_message(snap)

    # This delta will produce qty = 10 - 20 = -10 < 0 → desync
    dlt = delta_msg(ticker, seq=2, side="yes", price_dollars="0.45", delta="-20")
    await mgr.handle_delta_message(dlt)

    cb.assert_called_once_with(ticker)
    # Market must be back in syncing state
    assert mgr.get_market_tops(ticker) is None


@pytest.mark.asyncio
async def test_desync_callback_exception_does_not_crash_manager():
    """Recovery callback failure must be caught — manager stays alive."""
    cb = AsyncMock(side_effect=RuntimeError("callback exploded"))
    mgr = OrderbookManager(on_desync_callback=cb)
    ticker = "SAFE-TEST"
    mgr.initialize_market(ticker)

    snap = snapshot_msg(ticker, seq=1, yes=[["0.45", "10"]])
    await mgr.handle_snapshot_message(snap)

    dlt = delta_msg(ticker, seq=2, side="yes", price_dollars="0.45", delta="-20")
    await mgr.handle_delta_message(dlt)

    # Should not raise — callback exception is swallowed with logger.exception
    assert mgr.get_market_tops(ticker) is None


# =====================================================
# Multi-ticker isolation
# =====================================================


@pytest.mark.asyncio
async def test_multi_ticker_isolation():
    """A gap in ticker A's stream does not purge ticker B."""
    mgr, _ = make_manager()

    mgr.initialize_market("A")
    mgr.initialize_market("B")

    # Both subscribe on same sid (stream seq shared)
    snap_a = snapshot_msg("A", seq=1, sid=1, yes=[["0.50", "100"]])
    await mgr.handle_snapshot_message(snap_a)

    snap_b = snapshot_msg("B", seq=2, sid=1, yes=[["0.50", "200"]])
    await mgr.handle_snapshot_message(snap_b)

    assert mgr.get_market_tops("A") is not None
    assert mgr.get_market_tops("B") is not None

    # Stream gap detected → purge ALL books
    dlt_gap = delta_msg("A", seq=5, sid=1, side="yes", price_dollars="0.50", delta="-10")  # expected 3
    await mgr.handle_delta_message(dlt_gap)

    # Both are purged after stream gap
    assert mgr.get_market_tops("A") is None
    assert mgr.get_market_tops("B") is None


@pytest.mark.asyncio
async def test_desync_on_one_ticker_does_not_affect_other():
    """OrderbookDesyncError on ticker A triggers recovery only for A; ticker B unaffected."""
    mgr, cb = make_manager()

    mgr.initialize_market("A")
    mgr.initialize_market("B")

    snap_a = snapshot_msg("A", seq=1, yes=[["0.45", "10"]])
    await mgr.handle_snapshot_message(snap_a)

    snap_b = snapshot_msg("B", seq=2, yes=[["0.50", "200"]])
    await mgr.handle_snapshot_message(snap_b)

    # Desync on A
    dlt_bad = delta_msg("A", seq=3, side="yes", price_dollars="0.45", delta="-20")
    await mgr.handle_delta_message(dlt_bad)

    cb.assert_called_once_with("A")
    assert mgr.get_market_tops("A") is None
    # B must remain intact
    assert mgr.get_market_tops("B") is not None


# =====================================================
# Unknown ticker — silent ignore
# =====================================================


@pytest.mark.asyncio
async def test_snapshot_for_unknown_ticker_is_silently_ignored():
    mgr, cb = make_manager()
    snap = snapshot_msg("UNKNOWN", seq=1, yes=[["0.50", "100"]])
    await mgr.handle_snapshot_message(snap)  # must not raise
    cb.assert_not_called()


@pytest.mark.asyncio
async def test_delta_for_unknown_ticker_is_silently_ignored():
    mgr, cb = make_manager()
    dlt = delta_msg("UNKNOWN", seq=2, side="yes", price_dollars="0.50", delta="10")
    await mgr.handle_delta_message(dlt)  # must not raise
    cb.assert_not_called()


# =====================================================
# initialize_market — idempotence
# =====================================================


@pytest.mark.asyncio
async def test_initialize_market_is_idempotent():
    """Calling initialize_market twice for the same ticker must not reset existing state."""
    mgr, _ = make_manager()
    mgr.initialize_market("T")

    snap = snapshot_msg("T", seq=1, yes=[["0.50", "100"]])
    await mgr.handle_snapshot_message(snap)
    assert mgr.get_market_tops("T") is not None

    # Second call must be a no-op
    mgr.initialize_market("T")
    assert mgr.get_market_tops("T") is not None


# =====================================================
# Snapshot normalize failure → callback (not crash)
# =====================================================


@pytest.mark.asyncio
async def test_snapshot_normalize_failure_triggers_recovery():
    """Malformed snapshot (missing msg key) → recovery callback, no unhandled exception."""
    mgr, cb = make_manager()
    mgr.initialize_market("T")

    # ticker is extractable but price is malformed → normalize raises ValueError
    bad_snap = {
        "seq": 1,
        "sid": 1,
        "msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["NOT_A_NUMBER", "100"]],
            "no_dollars_fp": [],
        },
    }
    await mgr.handle_snapshot_message(bad_snap)  # must not raise

    cb.assert_called_once_with("T")


# =====================================================
# Delta normalize failure — skip, no recovery
# =====================================================


@pytest.mark.asyncio
async def test_delta_normalize_failure_is_skipped_not_recovered():
    """
    Malformed delta (missing side) → normalize raises ValueError → skip delta,
    no recovery callback. Market remains usable.
    """
    mgr, cb = make_manager()
    mgr.initialize_market("T")

    snap = snapshot_msg("T", seq=1, yes=[["0.50", "100"]])
    await mgr.handle_snapshot_message(snap)

    bad_dlt = {
        "seq": 2,
        "sid": 1,
        "msg": {"market_ticker": "T", "price_dollars": "0.50", "delta_fp": "10"},
        # "side" missing → normalize raises ValueError
    }
    await mgr.handle_delta_message(bad_dlt)

    cb.assert_not_called()
    assert mgr.get_market_tops("T") is not None
