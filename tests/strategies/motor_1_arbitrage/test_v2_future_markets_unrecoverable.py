"""
Purga de mercados FUTUROS de la recovery de V2 (incidente 2026-07-21).

Causa raíz: sid=1 con 189 mercados de eventos futuros (World Cup / MLB futuros) SIN book
operable. _is_unrecoverable solo filtraba por _dead_tickers (RAM, se pierde en cada reinicio)
y close_time <= now (jamás aplica a un futuro) → tras cada redeploy la recovery pedía los 189
snapshots, timeout ×5, circuit breaker, books stale — y M1 ciego (tracked=189 signals=0).

Fix: set_market_metadata() alimenta open_time + status desde discovery; _is_unrecoverable
purga con conocimiento POSITIVO: open_time futuro (aún no abrió) o status fuera de
active/open (transicionó). Metadata AUSENTE sigue siendo recuperable — invertir el default
purgaría sids VIVOS enteros cuando discovery no trae los campos (close_times_known=0/223
el 2026-07-17): el fail-open de lectura no se negocia.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    SidGapError,
)


def _snapshot(ticker: str, sid: int = 1, seq: int = 1, req_id: int | None = None) -> dict:
    msg: dict = {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {"market_ticker": ticker, "yes_dollars_fp": [], "no_dollars_fp": []},
    }
    if req_id is not None:
        msg["id"] = req_id
    return msg


def _delta(ticker: str, sid: int = 1, seq: int = 2) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.4000",
            "delta_fp": "100.00",
            "side": "yes",
        },
    }


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 200))
    return ws


def _capture_alerts(manager: OrderbookManagerV2) -> list[tuple[str, str]]:
    alerts: list[tuple[str, str]] = []

    async def _fake(kind: str, details: str) -> None:
        alerts.append((kind, details))

    manager._fire_alert = _fake  # type: ignore[method-assign]
    return alerts


# =====================================================
# (a) open_time futuro → no recuperable, excluido del get_snapshot
# =====================================================


async def test_future_open_time_is_unrecoverable(mock_ws):
    manager = OrderbookManagerV2(mock_ws)
    manager.set_market_metadata({"FUT": {"open_time": "2099-01-01T00:00:00Z", "status": "active"}})
    assert manager._is_unrecoverable("FUT") is True


async def test_future_open_time_excluded_from_recovery_snapshot(mock_ws):
    """El futuro se purga del get_snapshot; el mercado vivo se pide igual."""
    manager = OrderbookManagerV2(mock_ws)
    await manager.handle_message(_snapshot("LIVE", sid=1, seq=1))
    await manager.handle_message(_snapshot("FUT", sid=1, seq=2))
    manager.set_market_metadata(
        {
            "FUT": {"open_time": "2099-01-01T00:00:00Z", "status": "active"},
            "LIVE": {"open_time": "2020-01-01T00:00:00Z", "status": "active"},
        }
    )
    mock_ws.send_command.reset_mock()

    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("LIVE", sid=1, seq=99))

    _, kwargs = mock_ws.send_command.call_args
    assert kwargs["params"]["market_tickers"] == ["LIVE"]  # FUT purgado


# =====================================================
# (a') status fuera de active/open → no recuperable
# =====================================================


@pytest.mark.parametrize("status", ["initialized", "closed", "determined", "settled"])
async def test_non_active_status_is_unrecoverable(mock_ws, status):
    manager = OrderbookManagerV2(mock_ws)
    manager.set_market_metadata({"T": {"status": status}})
    assert manager._is_unrecoverable("T") is True


@pytest.mark.parametrize("status", ["active", "open"])
async def test_active_status_stays_recoverable(mock_ws, status):
    manager = OrderbookManagerV2(mock_ws)
    manager.set_market_metadata({"T": {"status": status}})
    assert manager._is_unrecoverable("T") is False


# =====================================================
# (a'') El escenario del incidente: sid ENTERO de futuros → breaker inmediato, sin loop
# =====================================================


async def test_all_future_sid_trips_breaker_without_requesting(mock_ws):
    """El caso de producción: TODOS los tickers del sid son futuros sin abrir. Antes: 4 lotes
    de get_snapshot → timeout ×5 → breaker CRITICAL tras ~2.5min de buffering. Ahora: purga
    total → all_tickers_settled → breaker WARNING inmediato, CERO requests."""
    manager = OrderbookManagerV2(mock_ws)
    alerts = _capture_alerts(manager)
    for i, t in enumerate(["F1", "F2", "F3"], start=1):
        await manager.handle_message(_snapshot(t, sid=1, seq=i))
    manager.set_market_metadata(
        {t: {"open_time": "2099-01-01T00:00:00Z", "status": "active"} for t in ["F1", "F2", "F3"]}
    )
    mock_ws.send_command.reset_mock()

    with pytest.raises(SidGapError):
        await manager.handle_message(_delta("F1", sid=1, seq=99))

    assert 1 in manager._recovery_disabled_sids  # breaker (sid sin nada recuperable)
    assert 1 not in manager._recovering  # jamás entró en recovery
    assert mock_ws.send_command.await_count == 0  # CERO get_snapshot (el loop cortado de raíz)
    assert any(k == "recovery_disabled" for k, _ in alerts)


# =====================================================
# (c) CONTROLES: el caso normal no se rompe
# =====================================================


async def test_active_open_market_with_future_close_stays_recoverable(mock_ws):
    """Un mercado abierto normal (open_time pasado, close_time futuro, status active) sigue
    siendo recuperable — el caso de TODOS los mercados líquidos operables."""
    manager = OrderbookManagerV2(mock_ws)
    manager.set_close_times({"NORMAL": "2099-01-01T00:00:00Z"})
    manager.set_market_metadata(
        {"NORMAL": {"open_time": "2020-01-01T00:00:00Z", "status": "active"}}
    )
    assert manager._is_unrecoverable("NORMAL") is False


async def test_unknown_metadata_stays_recoverable_fail_open(mock_ws):
    """FAIL-OPEN (control crítico): un ticker SIN metadata (discovery no la trajo — pasó el
    2026-07-17 con close_times_known=0/223) sigue recuperable. Purgar por ausencia cegaría
    sids vivos enteros: peor que el bug que arregla."""
    manager = OrderbookManagerV2(mock_ws)
    assert manager._is_unrecoverable("SIN_METADATA") is False


async def test_set_market_metadata_ignores_invalid_fields(mock_ws):
    """Best-effort: open_time no parseable / status vacío o no-string se ignoran (el ticker
    no se filtra por ese criterio)."""
    manager = OrderbookManagerV2(mock_ws)
    manager.set_market_metadata(
        {
            "A": {"open_time": "no-es-fecha", "status": ""},
            "B": {"open_time": None, "status": None},
            "C": {},
        }
    )
    assert manager._is_unrecoverable("A") is False
    assert manager._is_unrecoverable("B") is False
    assert manager._is_unrecoverable("C") is False


async def test_metadata_updates_on_refeed(mock_ws):
    """Re-discovery actualiza: un futuro cuyo open_time ya pasó (abrió) vuelve a ser
    recuperable en el próximo feed de metadata."""
    manager = OrderbookManagerV2(mock_ws)
    manager.set_market_metadata({"T": {"open_time": "2099-01-01T00:00:00Z", "status": "active"}})
    assert manager._is_unrecoverable("T") is True
    manager.set_market_metadata({"T": {"open_time": "2020-01-01T00:00:00Z", "status": "active"}})
    assert manager._is_unrecoverable("T") is False
