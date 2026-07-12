"""
Tests para OrderbookManagerV2 — 6 acceptance criteria scenarios.

AC1: gap en seq → SidGapError raised, todos los tickers del sid reciben mark_stale()
AC2: recovery snapshot llega → apply_snapshot, drain buffer
AC3: new_qty < 0 → OrderbookDesyncError raised (no swallow)
AC4: subscribed message (sin seq) → no SidGapError, no state change
AC5: ok message → no exception, no state change
AC6: error message → BotState.last_error populated, no exception propagated

También incluye: fixture loading, multi-ticker recovery, stats().
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.monitoring.health import BotState
from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    SidGapError,
)

# =====================================================
# Helpers
# =====================================================

FIXTURES_DIR = Path(__file__).parents[2] / "fixtures" / "ws"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def make_ws_snapshot(
    ticker: str,
    sid: int = 1,
    seq: int = 1,
    yes_fp: list | None = None,
    no_fp: list | None = None,
    req_id: int | None = None,
) -> dict:
    msg: dict = {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": yes_fp or [],
            "no_dollars_fp": no_fp or [],
        },
    }
    if req_id is not None:
        msg["id"] = req_id
    return msg


def make_ws_delta(
    ticker: str,
    sid: int = 1,
    seq: int = 2,
    price_dollars: str = "0.4000",
    delta_fp: str = "100.00",
    side: str = "yes",
) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": price_dollars,
            "delta_fp": delta_fp,
            "side": side,
        },
    }


# =====================================================
# Fixtures
# =====================================================


@pytest.fixture(autouse=True)
def reset_botstate():
    BotState.last_error = None
    BotState.last_error_at = None
    yield
    BotState.last_error = None
    BotState.last_error_at = None


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.return_value = 42
    return ws


@pytest.fixture
def manager(mock_ws: AsyncMock) -> OrderbookManagerV2:
    return OrderbookManagerV2(mock_ws)


# =====================================================
# Fixture files
# =====================================================


def test_fixtures_loadable_and_correct_shape() -> None:
    """All fixture files load and have the expected protocol shape."""
    snapshot = load_fixture("snapshot_seq_1.json")
    assert snapshot["type"] == "orderbook_snapshot"
    assert "seq" in snapshot
    assert "yes_dollars_fp" in snapshot["msg"]

    subscribed = load_fixture("subscribed.json")
    assert subscribed["type"] == "subscribed"
    assert "seq" not in subscribed  # H5: subscribed has NO seq

    delta = load_fixture("delta_seq_7.json")
    assert delta["type"] == "orderbook_delta"
    assert "price_dollars" in delta["msg"]
    assert "delta_fp" in delta["msg"]

    recovery = load_fixture("get_snapshot_response.json")
    assert recovery["type"] == "orderbook_snapshot"
    assert "id" in recovery  # correlation field

    ok_msg = load_fixture("add_markets_ok.json")
    assert ok_msg["type"] == "ok"

    error_msg = load_fixture("unknown_command_error.json")
    assert error_msg["type"] == "error"
    assert error_msg["code"] == 5


# =====================================================
# AC1: gap → SidGapError + mark_stale
# =====================================================


@pytest.mark.asyncio
async def test_ac1_gap_raises_sid_gap_error(
    manager: OrderbookManagerV2, mock_ws: AsyncMock
) -> None:
    """Gap in seq → SidGapError raised with correct attrs."""
    ticker = "KXNBA-26-NYK"
    await manager.handle_message(make_ws_snapshot(ticker, sid=1, seq=1))

    with pytest.raises(SidGapError) as exc_info:
        await manager.handle_message(make_ws_delta(ticker, sid=1, seq=3))  # expected 2

    assert exc_info.value.sid == 1
    assert exc_info.value.expected_seq == 2
    assert exc_info.value.received_seq == 3


@pytest.mark.asyncio
async def test_ac1_gap_marks_all_tickers_stale(
    manager: OrderbookManagerV2, mock_ws: AsyncMock
) -> None:
    """Gap → all tickers in that sid are marked stale, not other sids."""
    ticker_a = "TICKER-SID1-A"
    ticker_b = "TICKER-SID1-B"
    ticker_c = "TICKER-SID2"

    # Two tickers on sid=1
    await manager.handle_message(make_ws_snapshot(ticker_a, sid=1, seq=1))
    await manager.handle_message(make_ws_snapshot(ticker_b, sid=1, seq=2))
    # One ticker on sid=2
    await manager.handle_message(make_ws_snapshot(ticker_c, sid=2, seq=10))

    with pytest.raises(SidGapError):
        await manager.handle_message(make_ws_delta(ticker_a, sid=1, seq=5))  # expected 3

    assert manager._books[ticker_a].is_stale
    assert manager._books[ticker_b].is_stale
    # sid=2 unaffected
    assert manager._books[ticker_c].is_initialized
    assert not manager._books[ticker_c].is_stale


@pytest.mark.asyncio
async def test_ac1_gap_triggers_ws_command(manager: OrderbookManagerV2, mock_ws: AsyncMock) -> None:
    """Gap → WS get_snapshot command sent for all tickers in sid."""
    ticker = "KXNBA-26-NYK"
    await manager.handle_message(make_ws_snapshot(ticker, sid=1, seq=1))

    with pytest.raises(SidGapError):
        await manager.handle_message(make_ws_delta(ticker, sid=1, seq=5))

    mock_ws.send_command.assert_called_once_with(
        "update_subscription",
        action="get_snapshot",
        params={"market_tickers": [ticker], "sids": [1]},
    )


# =====================================================
# AC2: recovery snapshot → apply + drain buffer
# =====================================================


@pytest.mark.asyncio
async def test_ac2_recovery_snapshot_restores_state(
    manager: OrderbookManagerV2, mock_ws: AsyncMock
) -> None:
    """Recovery snapshot applies correctly and exits recovery mode."""
    ticker = "KXNBA-26-NYK"
    mock_ws.send_command.return_value = 42

    await manager.handle_message(make_ws_snapshot(ticker, sid=1, seq=1))

    with pytest.raises(SidGapError):
        await manager.handle_message(make_ws_delta(ticker, sid=1, seq=5))

    assert 1 in manager._recovering
    assert manager._books[ticker].is_stale

    # Recovery snapshot arrives with id=42
    recovery = make_ws_snapshot(ticker, sid=1, seq=50, yes_fp=[["0.1200", "500.00"]], req_id=42)
    await manager.handle_message(recovery)

    assert manager._books[ticker].is_initialized
    assert not manager._books[ticker].is_stale
    assert manager._books[ticker].sequence == 50
    assert 1 not in manager._recovering


@pytest.mark.asyncio
async def test_ac2_buffered_messages_drained_after_recovery(
    manager: OrderbookManagerV2, mock_ws: AsyncMock
) -> None:
    """Messages buffered during recovery are applied after snapshot (if seq > snapshot)."""
    ticker = "KXNBA-26-NYK"
    mock_ws.send_command.return_value = 42

    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=10, yes_fp=[["0.4000", "100.00"]])
    )

    # Gap at seq=12 (expected 11)
    with pytest.raises(SidGapError):
        await manager.handle_message(make_ws_delta(ticker, sid=1, seq=12))

    # Send another delta while in recovery — goes to buffer
    await manager.handle_message(make_ws_delta(ticker, sid=1, seq=13, delta_fp="50.00"))

    # Recovery snapshot at seq=11
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=11, yes_fp=[["0.4000", "200.00"]], req_id=42)
    )

    # Both seq=12 (gap-trigger, +100) and seq=13 (+50) were buffered.
    # After drain: 200 + 100 + 50 = 350.
    assert manager._books[ticker].is_initialized
    top = manager.get_top_of_book(ticker, "yes")
    assert top is not None
    assert top.best_bid is not None
    assert top.best_bid.price_cents == 40
    assert top.best_bid.size == 350  # 200 (snapshot) + 100 (seq=12) + 50 (seq=13)


# =====================================================
# AC3: new_qty < 0 → OrderbookDesyncError
# =====================================================


@pytest.mark.asyncio
async def test_ac3_negative_qty_raises_orderbook_desync(manager: OrderbookManagerV2) -> None:
    """Delta producing new_qty < 0 raises OrderbookDesyncError (not silently dropped)."""
    ticker = "KXNBA-26-NYK"
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=1, yes_fp=[["0.4000", "10.00"]])
    )

    # 10 existing - 15 delta = -5 → feed corruption
    with pytest.raises(OrderbookDesyncError) as exc_info:
        await manager.handle_message(
            make_ws_delta(
                ticker, sid=1, seq=2, price_dollars="0.4000", delta_fp="-15.00", side="yes"
            )
        )

    assert exc_info.value.ticker == ticker
    assert exc_info.value.price_cents == 40
    assert exc_info.value.new_qty == -5


@pytest.mark.asyncio
async def test_desync_emits_error_diagnostic_and_reraises(manager: OrderbookManagerV2) -> None:
    """En desync: log ERROR con el estado PRE-delta + raw_msg, y la excepcion se re-lanza intacta."""
    from loguru import logger

    ticker = "KXNBA-26-NYK"
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=1, yes_fp=[["0.4000", "10.00"]])
    )

    records: list = []
    sink_id = logger.add(records.append, level="ERROR", format="{message}")
    try:
        with pytest.raises(OrderbookDesyncError) as exc_info:
            await manager.handle_message(
                make_ws_delta(
                    ticker, sid=1, seq=2, price_dollars="0.4000", delta_fp="-15.00", side="yes"
                )
            )
    finally:
        logger.remove(sink_id)

    # La excepcion original propaga intacta (mismo control flow que antes).
    assert exc_info.value.new_qty == -5
    assert exc_info.value.price_cents == 40

    # Se emitio el diagnostico a nivel ERROR (persiste con LOG_LEVEL=INFO) con los campos clave.
    captured = "\n".join(str(r) for r in records)
    assert "V2 desync diagnostic" in captured
    assert "bucket_qty_pre_delta=10" in captured  # estado PRE-delta (book aun no mutado)
    assert "delta_size=-15" in captured
    assert "msg_seq=2" in captured
    assert "raw_msg=" in captured


@pytest.mark.asyncio
async def test_desync_logging_failure_is_silenced(
    manager: OrderbookManagerV2, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si el logging diagnostico falla, se silencia: se re-lanza la OrderbookDesyncError, no otra."""
    ticker = "KXNBA-26-NYK"
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=1, yes_fp=[["0.4000", "10.00"]])
    )

    def boom() -> dict:
        raise RuntimeError("diagnostic logging path broken")

    monkeypatch.setattr(manager._books[ticker], "snapshot_view", boom)

    # Aun con el diagnostico roto, propaga la OrderbookDesyncError original (no el RuntimeError).
    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(
            make_ws_delta(
                ticker, sid=1, seq=2, price_dollars="0.4000", delta_fp="-15.00", side="yes"
            )
        )


# =====================================================
# Bootstrap buffer (Parte A — fix desync bootstrap)
# =====================================================


@pytest.mark.asyncio
async def test_bootstrap_reordered_delta_before_snapshot(manager: OrderbookManagerV2) -> None:
    """Delta que llega antes del snapshot inicial se encola y aplica (no se descarta).

    Reproduce el patrón del attempt #3: un add pre-snapshot que, si se descartara,
    dejaría el book sub-construido y un delta posterior lo llevaría a qty<0.
    """
    ticker = "KXMLB-26-PHI"

    # Delta seq=2 (add +13 a 3c) llega ANTES del snapshot seq=1 → debe encolarse.
    await manager.handle_message(
        make_ws_delta(ticker, sid=1, seq=2, price_dollars="0.0300", delta_fp="13.00", side="yes")
    )
    assert manager._bootstrap_buffer.get(ticker), "el delta pre-snapshot debió encolarse"
    assert 1 not in manager._last_seq_by_sid, "encolar no debe avanzar el baseline del sid"

    # Snapshot seq=1: bucket 3c = 2.
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=1, yes_fp=[["0.0300", "2.00"]])
    )

    # El delta encolado (seq 2 > 1) se drenó: 2 + 13 = 15.
    top = manager.get_top_of_book(ticker, "yes")
    assert top is not None and top.best_bid is not None
    assert top.best_bid.price_cents == 3
    assert top.best_bid.size == 15
    assert ticker not in manager._bootstrap_buffer
    assert manager._last_seq_by_sid[1] == 2

    # Y el delta -13 posterior (seq 3) ya NO produce desync: 15 - 13 = 2.
    await manager.handle_message(
        make_ws_delta(ticker, sid=1, seq=3, price_dollars="0.0300", delta_fp="-13.00", side="yes")
    )
    top = manager.get_top_of_book(ticker, "yes")
    assert top.best_bid.size == 2


@pytest.mark.asyncio
async def test_bootstrap_buffer_discards_redundant_delta(manager: OrderbookManagerV2) -> None:
    """Un delta encolado con seq <= snapshot_seq ya está contenido en el snapshot: se descarta."""
    ticker = "KXMLB-26-PHI"

    # Delta seq=5 llega antes del snapshot → encolado.
    await manager.handle_message(
        make_ws_delta(ticker, sid=1, seq=5, price_dollars="0.0300", delta_fp="100.00", side="yes")
    )
    assert manager._bootstrap_buffer.get(ticker)

    # Snapshot seq=10 (más nuevo que el delta encolado seq=5) con bucket 3c = 2.
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=10, yes_fp=[["0.0300", "2.00"]])
    )

    # seq 5 <= 10 → descartado (ya en el snapshot). El bucket queda en 2, no 102.
    top = manager.get_top_of_book(ticker, "yes")
    assert top is not None and top.best_bid is not None
    assert top.best_bid.size == 2
    assert ticker not in manager._bootstrap_buffer
    assert manager._last_seq_by_sid[1] == 10


# =====================================================
# AC4: subscribed (no seq) → no SidGapError
# =====================================================


@pytest.mark.asyncio
async def test_ac4_subscribed_has_no_seq_no_gap_error(manager: OrderbookManagerV2) -> None:
    """subscribed message has no seq — must not trigger gap detection or change state."""
    # First establish a seq baseline
    ticker = "KXNBA-26-NYK"
    await manager.handle_message(make_ws_snapshot(ticker, sid=1, seq=5))
    assert manager._last_seq_by_sid[1] == 5

    subscribed = load_fixture("subscribed.json")
    await manager.handle_message(subscribed)  # Must not raise

    # seq baseline unchanged
    assert manager._last_seq_by_sid[1] == 5
    assert len(manager._books) == 1
    assert BotState.last_error is None


@pytest.mark.asyncio
async def test_ac4_subscribed_before_any_snapshot(manager: OrderbookManagerV2) -> None:
    """subscribed before any snapshot — silently ignored, no state created."""
    subscribed = {"type": "subscribed", "sid": 1, "msg": {"channel": "orderbook_delta"}}
    await manager.handle_message(subscribed)

    assert len(manager._books) == 0
    assert not manager._last_seq_by_sid


# =====================================================
# AC5: ok → no exception, no state change
# =====================================================


@pytest.mark.asyncio
async def test_ac5_ok_message_no_exception_no_state(manager: OrderbookManagerV2) -> None:
    """ok message silently acknowledged — no state change, no exception."""
    ok_msg = load_fixture("add_markets_ok.json")
    await manager.handle_message(ok_msg)

    assert len(manager._books) == 0
    assert not manager._last_seq_by_sid
    assert BotState.last_error is None


# =====================================================
# AC6: error → BotState populated, no exception
# =====================================================


@pytest.mark.asyncio
async def test_ac6_error_message_records_to_botstate(manager: OrderbookManagerV2) -> None:
    """error message records to BotState.last_error but does not raise."""
    error_msg = load_fixture("unknown_command_error.json")
    await manager.handle_message(error_msg)  # Must not raise

    assert BotState.last_error is not None
    assert "5" in BotState.last_error or "unknown" in BotState.last_error.lower()


@pytest.mark.asyncio
async def test_ac6_error_does_not_affect_book_state(manager: OrderbookManagerV2) -> None:
    """Error message must not corrupt existing orderbook state."""
    ticker = "KXNBA-26-NYK"
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=5, yes_fp=[["0.4000", "100.00"]])
    )

    await manager.handle_message({"type": "error", "id": 99, "code": 3, "msg": "timeout"})

    assert manager._books[ticker].is_initialized
    assert manager._books[ticker].sequence == 5


# =====================================================
# Additional: stats, multi-ticker, fixture smoke
# =====================================================


@pytest.mark.asyncio
async def test_stats_after_snapshots(manager: OrderbookManagerV2) -> None:
    """stats() reflects tracked/initialized/stale counts correctly."""
    await manager.handle_message(
        make_ws_snapshot("TICKER-A", sid=1, seq=1, yes_fp=[["0.4000", "100.00"]])
    )
    await manager.handle_message(
        make_ws_snapshot("TICKER-B", sid=1, seq=2, yes_fp=[["0.3500", "50.00"]])
    )

    s = manager.stats()
    assert s["tracked_tickers"] == 2
    assert s["initialized_tickers"] == 2
    assert s["stale_tickers"] == 0
    assert s["recovering_sids"] == []


@pytest.mark.asyncio
async def test_snapshot_from_fixture_file(manager: OrderbookManagerV2) -> None:
    """Apply the fixture snapshot_seq_1.json and verify state."""
    snapshot = load_fixture("snapshot_seq_1.json")
    await manager.handle_message(snapshot)

    state = manager._books["KXNBA-26-NYK"]
    assert state.is_initialized
    assert state.sequence == 1

    top_yes = manager.get_top_of_book("KXNBA-26-NYK", "yes")
    assert top_yes is not None
    assert top_yes.best_bid is not None
    assert top_yes.best_bid.price_cents == 12  # "0.1200" → 12 cents

    top_no = manager.get_top_of_book("KXNBA-26-NYK", "no")
    assert top_no is not None
    assert top_no.best_bid is not None
    assert top_no.best_bid.price_cents == 85  # "0.8500" → 85 cents


@pytest.mark.asyncio
async def test_delta_from_fixture_after_snapshot(manager: OrderbookManagerV2) -> None:
    """Apply snapshot_seq_1 then delta_seq_7 (consecutive seqs 1→7 means gap, need seq=2)."""
    # Load snapshot (seq=1)
    snapshot = load_fixture("snapshot_seq_1.json")
    await manager.handle_message(snapshot)

    # delta_seq_7 has seq=7 but we're at seq=1 — that's a gap.
    # To test normal delta application, apply intermediate seqs or use seq=2 directly.
    # This test verifies fixture processing, not gap detection (AC1 covers that).
    delta = make_ws_delta(
        "KXNBA-26-NYK", sid=1, seq=2, price_dollars="0.8500", delta_fp="-100.00", side="no"
    )
    await manager.handle_message(delta)

    top_no = manager.get_top_of_book("KXNBA-26-NYK", "no")
    assert top_no is not None
    assert top_no.best_bid is not None
    assert top_no.best_bid.price_cents == 85
    assert top_no.best_bid.size == 2900  # 3000 - 100


# =====================================================
# Cuarentena en desync (incidente 2026-07-12: rollbacks por precios fantasma)
# =====================================================


@pytest.mark.asyncio
async def test_desync_quarantines_book_and_triggers_recovery(
    manager: OrderbookManagerV2, mock_ws: AsyncMock
) -> None:
    """MECANISMO del fix: en OrderbookDesyncError el book queda EN CUARENTENA (stale →
    get_top_of_book None: M1 deja de ver precios fantasma en el MISMO tick) y se dispara
    la recovery del sid. Antes del fix el book seguía sirviendo datos divergidos → M1
    detectaba arbs sobre liquidez inexistente → fill parcial → rollback ×3 → breaker."""
    ticker = "KXMLBGAME-26JUL121610AZLAD-AZ"
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=1, yes_fp=[["0.4000", "10.00"]])
    )
    assert manager.get_top_of_book(ticker, "yes") is not None  # book sano sirve

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(
            make_ws_delta(ticker, sid=1, seq=2, price_dollars="0.4000", delta_fp="-15.00")
        )

    # Cuarentena inmediata: nadie más lee el book corrupto.
    assert manager._books[ticker].is_stale
    assert manager.get_top_of_book(ticker, "yes") is None
    # Y la recovery del sid quedó en marcha (snapshot fresco lo re-baseará).
    assert 1 in manager._recovering
    mock_ws.send_command.assert_called_once_with(
        "update_subscription",
        action="get_snapshot",
        params={"market_tickers": [ticker], "sids": [1]},
    )


@pytest.mark.asyncio
async def test_desync_quarantine_stops_repeat_desyncs(
    manager: OrderbookManagerV2, mock_ws: AsyncMock
) -> None:
    """CONTROL anti-spam: tras la cuarentena, el MISMO delta corrupto ya no vuelve a
    lanzar desync (el guard de stale lo dropea) — se corta el loop de 635 logs/día."""
    ticker = "KXMLBGAME-26JUL121610AZLAD-AZ"
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=1, yes_fp=[["0.4000", "10.00"]])
    )
    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(
            make_ws_delta(ticker, sid=1, seq=2, price_dollars="0.4000", delta_fp="-15.00")
        )

    # El sid está en recovery → el delta siguiente se BUFFEREA (no re-lanza desync).
    await manager.handle_message(
        make_ws_delta(ticker, sid=1, seq=2, price_dollars="0.4000", delta_fp="-15.00")
    )  # no raise


@pytest.mark.asyncio
async def test_desync_recovery_snapshot_rebases_and_serves_again(
    manager: OrderbookManagerV2, mock_ws: AsyncMock
) -> None:
    """El ciclo completo sana: desync → cuarentena → snapshot de recovery → el book se
    re-basea y vuelve a servir precios REALES (no queda muerto para siempre)."""
    ticker = "KXMLBGAME-26JUL121610AZLAD-AZ"
    mock_ws.send_command.return_value = 42
    await manager.handle_message(
        make_ws_snapshot(ticker, sid=1, seq=1, yes_fp=[["0.4000", "10.00"]])
    )
    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(
            make_ws_delta(ticker, sid=1, seq=2, price_dollars="0.4000", delta_fp="-15.00")
        )
    assert manager.get_top_of_book(ticker, "yes") is None  # en cuarentena

    recovery = make_ws_snapshot(ticker, sid=1, seq=50, yes_fp=[["0.1200", "500.00"]], req_id=42)
    await manager.handle_message(recovery)

    assert manager._books[ticker].is_initialized and not manager._books[ticker].is_stale
    top = manager.get_top_of_book(ticker, "yes")
    assert top is not None and top.best_bid is not None
    assert top.best_bid.price_cents == 12  # precios frescos, no fantasma


@pytest.mark.asyncio
async def test_healthy_book_unaffected_by_other_sid_desync(
    manager: OrderbookManagerV2, mock_ws: AsyncMock
) -> None:
    """CONTROL: el desync de un sid NO toca los books de otro sid (la cuarentena es
    quirúrgica, no un apagón general del feed)."""
    sano = "KXWCGAME-26JUL15ARGFRA-ARG"
    roto = "KXMLBGAME-26JUL121610AZLAD-AZ"
    await manager.handle_message(make_ws_snapshot(sano, sid=7, seq=1, yes_fp=[["0.5000", "20.00"]]))
    await manager.handle_message(make_ws_snapshot(roto, sid=1, seq=1, yes_fp=[["0.4000", "10.00"]]))

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(
            make_ws_delta(roto, sid=1, seq=2, price_dollars="0.4000", delta_fp="-15.00")
        )

    assert manager.get_top_of_book(roto, "yes") is None  # cuarentena
    assert manager.get_top_of_book(sano, "yes") is not None  # el sano sigue sirviendo
