"""
Tests for OrderbookManagerV2 gap rate alerting.

Covers: warning/critical thresholds (5/min, 20/min sustained for 3 evaluations),
throttle (5min warning, 2min critical), single burst suppression,
old-entry pruning, and alert send failure isolation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


def make_v2() -> OrderbookManagerV2:
    ws = MagicMock()
    ws.send_command = AsyncMock(return_value=1)
    return OrderbookManagerV2(ws)


# =====================================================
# _record_gap_and_should_alert — unit tests (sync)
# =====================================================


def test_single_burst_does_not_fire():
    """3 gaps in < 60s (count=3 < warning threshold 5) → no alert."""
    mgr = make_v2()
    for _ in range(3):
        result = mgr._record_gap_and_should_alert()
    assert result is None
    assert mgr._consecutive_warning == 0
    assert mgr._consecutive_critical == 0


def test_warning_fires_at_5_per_min_sustained():
    """
    7 gaps in quick succession:
      gaps 1-4: count < 5 → consecutive_warning = 0
      gap 5:    count = 5 → consecutive_warning = 1
      gap 6:    count = 6 → consecutive_warning = 2
      gap 7:    count = 7 → consecutive_warning = 3 → fires warning
    """
    mgr = make_v2()
    result = None
    for _ in range(7):
        result = mgr._record_gap_and_should_alert()
    assert result is not None
    kind, details = result
    assert kind == "sid_gap_warning"
    assert "gaps_last_60s=7" in details


def test_critical_fires_at_20_per_min_sustained():
    """
    22 gaps in quick succession:
      gaps 1-19: consecutive_critical = 0 (count < 20)
      gap 20:    consecutive_critical = 1
      gap 21:    consecutive_critical = 2
      gap 22:    consecutive_critical = 3 → fires critical
    """
    mgr = make_v2()
    # Suppress the warning alert that fires around gap 7 by pre-setting throttle
    import time

    mgr._last_warning_alert_at = time.monotonic()  # already "fired" recently

    result = None
    for _ in range(22):
        result = mgr._record_gap_and_should_alert()
    assert result is not None
    kind, _ = result
    assert kind == "sid_gap_critical"


def test_critical_takes_priority_over_warning():
    """When both thresholds are met, critical fires (not warning)."""
    mgr = make_v2()
    # Warning is not throttled, critical is not throttled
    # Fire 22 gaps to hit critical threshold
    result = None
    for _ in range(22):
        result = mgr._record_gap_and_should_alert()
    # The first alert at gap 7 would be warning; after that, warning is throttled
    # At gap 22 we get critical. Verify it's critical (not warning).
    # (warning was already fired and throttled at gap 7, so gap 22 → critical)
    assert result is not None
    kind, _ = result
    assert kind == "sid_gap_critical"


def test_warning_throttled_to_once_per_5min():
    """After warning fires, same rate for next 4min → no second alert."""
    import time

    mgr = make_v2()

    # Fire 7 gaps → warning fires
    for _ in range(7):
        mgr._record_gap_and_should_alert()

    # warning was just throttled. Next 7 gaps within throttle window → no alert
    with patch("src.strategies.motor_1_arbitrage.orderbook_manager_v2.time") as mock_t:
        mock_t.monotonic.return_value = time.monotonic() + 240  # 4 min later (within 5min throttle)
        # count in window: 7 old + new ones — but old ones are "60s" old in real time
        # To isolate just the throttle test, reset everything except the throttle timestamp
        mgr2 = make_v2()
        mgr2._last_warning_alert_at = mock_t.monotonic.return_value - 240  # 4min ago (recent)
        # Force consecutive_warning >= 3 and count >= 5
        # by directly setting internal state:
        mgr2._consecutive_warning = 5
        mgr2._gap_timestamps = [mock_t.monotonic.return_value - 1] * 7  # 7 recent gaps

        result = mgr2._record_gap_and_should_alert()
        # throttle (300s) not expired (only 240s) → no alert
        assert result is None


def test_warning_fires_again_after_throttle_expires():
    """After 5min, warning can fire again."""
    import time

    mgr = make_v2()

    now = time.monotonic()
    mgr._last_warning_alert_at = now - 310  # 5min10s ago → throttle expired
    mgr._consecutive_warning = 5
    mgr._gap_timestamps = [now - 1] * 7  # 7 gaps in last 60s

    with patch("src.strategies.motor_1_arbitrage.orderbook_manager_v2.time") as mock_t:
        mock_t.monotonic.return_value = now
        result = mgr._record_gap_and_should_alert()

    assert result is not None
    kind, _ = result
    assert kind == "sid_gap_warning"


def test_old_entries_dropped_from_window():
    """Gaps older than 60s are excluded from the count and don't contribute to rate."""
    mgr = make_v2()

    # Add 10 "old" gaps at timestamp t=1000
    old_time = 1000.0
    mgr._gap_timestamps = [old_time] * 10

    # Now evaluate at t=1070 (70s later) → old entries pruned, only 1 new gap counted
    with patch("src.strategies.motor_1_arbitrage.orderbook_manager_v2.time") as mock_t:
        mock_t.monotonic.return_value = 1070.0
        result = mgr._record_gap_and_should_alert()

    # count = 1 (only the new gap at t=1070) → below warning threshold
    assert result is None
    assert mgr._consecutive_warning == 0
    assert len(mgr._gap_timestamps) == 1


def test_consecutive_counter_resets_when_count_drops():
    """If rate drops below threshold, consecutive counters reset."""
    import time

    mgr = make_v2()

    # Build up 6 gaps in window
    now = time.monotonic()
    mgr._gap_timestamps = [now - 1] * 6
    mgr._consecutive_warning = 2  # about to hit 3

    # Now advance time so those 6 gaps age out (70s later), add only 1 new
    with patch("src.strategies.motor_1_arbitrage.orderbook_manager_v2.time") as mock_t:
        mock_t.monotonic.return_value = now + 70
        result = mgr._record_gap_and_should_alert()

    assert result is None
    assert mgr._consecutive_warning == 0  # reset because count dropped below 5


# =====================================================
# _fire_alert — alert send failure isolation
# =====================================================


@pytest.mark.asyncio
async def test_alert_failure_does_not_propagate():
    """If telegram send raises, _fire_alert swallows it — never propagates."""
    mgr = make_v2()
    with patch(
        "src.monitoring.telegram_alerts.alert_orderbook_anomaly",
        new=AsyncMock(side_effect=RuntimeError("Telegram down")),
    ):
        # Must not raise
        await mgr._fire_alert("sid_gap_warning", "gaps_last_60s=7")


@pytest.mark.asyncio
async def test_alert_failure_does_not_break_recovery():
    """
    Even if alert fails, handle_message still raises SidGapError and recovery completes.
    """
    mgr = make_v2()
    mgr._tickers_by_sid[1] = {"TICKER-A"}
    mgr._books["TICKER-A"] = MagicMock()
    mgr._last_seq_by_sid[1] = 10

    snap_msg = {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": 5,  # gap: expected 11, got 5
        "msg": {"market_ticker": "TICKER-A"},
    }

    # Make alert send fail
    with (
        patch(
            "src.monitoring.telegram_alerts.alert_orderbook_anomaly",
            new=AsyncMock(side_effect=Exception("send failed")),
        ),
        patch("asyncio.create_task"),
    ):
        # Trigger the gap — even with failing alert, SidGapError must raise
        from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import SidGapError

        with pytest.raises(SidGapError):
            await mgr.handle_message(snap_msg)

        # Recovery was initiated (mark_stale called on books)
        assert 1 in mgr._recovering


# =====================================================
# Integration: asyncio.create_task is called when threshold met
# =====================================================


@pytest.mark.asyncio
async def test_create_task_called_when_alert_fires():
    """asyncio.create_task is scheduled when _record_gap_and_should_alert returns non-None."""
    mgr = make_v2()
    mgr._last_seq_by_sid[1] = 100

    # Pre-load state so next gap fires warning (consecutive_warning=2, count=6)
    import time

    now = time.monotonic()
    mgr._gap_timestamps = [now - 1] * 6
    mgr._consecutive_warning = 2

    gap_msg = {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": 999,  # gap: expected 101
        "msg": {"market_ticker": "TICKER-A"},
    }
    mgr._tickers_by_sid[1] = {"TICKER-A"}

    from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import SidGapError

    with patch("asyncio.create_task") as mock_create_task:
        with pytest.raises(SidGapError):
            await mgr.handle_message(gap_msg)
        # create_task must have been called with the alert coroutine
        assert mock_create_task.called
