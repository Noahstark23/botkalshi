"""
Auditoría 2026-07-07 (P0/P1 de Motor 1): FOK obligatorio + fill_count real + rollback con
pérdida REALIZADA.

Antes: las patas iban con el default gtc (podían quedar RESTING) y un HTTP 200 se trataba
como "fillada" sin mirar fill_count — la misma familia de bugs del incidente 2026-07-07.
Y el rollback marcaba 'rolled_back' sin pnl_cents → la pérdida jamás entraba en las
ventanas de stop-loss (solo agregan filas settled).

Ahora: buys FOK (todo-o-nada), fill_count real de la respuesta (200 con fill 0 = pata
muerta limpia; 409 fill_or_kill_insufficient_resting_volume = kill determinístico), y el
sell de rollback (IOC) realiza la pérdida en una fila settled — con split si el fill es
parcial y fila ABIERTA si no se pudo vender (exposición visible, huérfana gestionable).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import SQLModel, create_engine, select

import src.storage.models as _models
from src.clients.kalshi_rest import KalshiClientError
from src.math.arbitrage import detect_binary_arb
from src.math.fees import kalshi_fee_cents
from src.monitoring.health import BotState
from src.risk.manager import RiskManager, TradeDecision
from src.storage.models import Trade, get_session
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor

# =====================================================
# Fixtures (mismas convenciones que test_executor.py)
# =====================================================


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(_models, "_engine", engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    monkeypatch.setattr(_models, "_engine", None)


@pytest.fixture(autouse=True)
def reset_bot_state():
    BotState.is_paused = False
    BotState.pause_reason = None
    yield
    BotState.is_paused = False
    BotState.pause_reason = None


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.get_available_balance_usd.return_value = 10_000.0
    return client


@pytest.fixture
def mock_risk() -> MagicMock:
    rm = MagicMock(spec=RiskManager)
    rm.check_pre_trade = AsyncMock(
        return_value=TradeDecision(approved=True, reason="ok", max_allowed_count=10)
    )
    return rm


@pytest.fixture
def executor(mock_client, mock_risk) -> ArbitrageExecutor:
    return ArbitrageExecutor(mock_client, mock_risk)


@pytest.fixture
def binary_opp():
    opp = detect_binary_arb("MKT-A", 40, 200, 45, 200, max_count=10)
    assert opp is not None
    return opp


def _settings() -> MagicMock:
    s = MagicMock()
    s.TRADING_ENABLED = True
    s.ACTIVE_CAPITAL_USD = 300.0
    s.MAX_EVENT_DIRECTIONAL_EXPOSURE_USD = 10_000.0
    return s


def _patched():
    return patch("src.strategies.motor_1_arbitrage.executor.get_settings", return_value=_settings())


def _buy_calls(mock_client):
    return [c for c in mock_client.place_order.call_args_list if c.kwargs.get("action") == "buy"]


def _sell_calls(mock_client):
    return [c for c in mock_client.place_order.call_args_list if c.kwargs.get("action") == "sell"]


def _trade(coid_contains: str | None = None, side: str | None = None) -> list[Trade]:
    with get_session() as s:
        trades = list(s.exec(select(Trade)).all())
    if side is not None:
        trades = [t for t in trades if t.side == side]
    if coid_contains is not None:
        trades = [t for t in trades if coid_contains in t.client_order_id]
    return trades


# =====================================================
# FOK en las patas de entrada
# =====================================================


@pytest.mark.asyncio
async def test_buy_legs_use_fill_or_kill(executor, mock_client, binary_opp):
    """Issue #14 / auditoría 2026-07-07: sin FOK el default gtc dejaba la pata RESTING."""
    mock_client.place_order.return_value = {"order": {"order_id": "k-1", "fill_count": 10}}
    with _patched():
        assert await executor.execute(binary_opp) is True
    buys = _buy_calls(mock_client)
    assert len(buys) == 2
    assert all(c.kwargs["time_in_force"] == "fill_or_kill" for c in buys)


# =====================================================
# fill_count real (HTTP 200 ≠ fillada)
# =====================================================


@pytest.mark.asyncio
async def test_zero_fill_count_marks_leg_cancelled_and_rolls_back_other(
    executor, mock_client, binary_opp
):
    """HTTP 200 con fill_count=0 = FOK killed: pata SIN posición → cancelled (no filled),
    y la pata que SÍ llenó se rollbackea."""
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes", "fill_count": 10}},  # YES fills
        {"order": {"order_id": "k-no", "fill_count": 0}},  # NO: 200 pero killed
        {"order": {"fill_count": 10}},  # sell del rollback
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[38, 10]], "no": []}}

    with _patched():
        assert await executor.execute(binary_opp) is False

    (no_row,) = _trade(side="no")
    assert no_row.status == "cancelled"  # jamás "filled" por el 200
    yes_rows = _trade(side="yes")
    assert [t.status for t in yes_rows] == ["settled"]  # rollback realizado
    assert len(_sell_calls(mock_client)) == 1


@pytest.mark.asyncio
async def test_fok_409_kill_marks_leg_cancelled(executor, mock_client, binary_opp):
    """El FOK sin volumen vuelve como 409 fill_or_kill_insufficient_resting_volume:
    kill DETERMINÍSTICO → cancelled limpio (no queda pending eterno ni cuenta como error)."""
    kill = KalshiClientError(409, "conflict", "{}", "fill_or_kill_insufficient_resting_volume")
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes", "fill_count": 10}},
        kill,
        {"order": {"fill_count": 10}},  # sell del rollback
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[38, 10]], "no": []}}

    with _patched():
        assert await executor.execute(binary_opp) is False

    (no_row,) = _trade(side="no")
    assert no_row.status == "cancelled"
    assert len(_sell_calls(mock_client)) == 1


@pytest.mark.asyncio
async def test_other_409_stays_pending_for_reconcile(executor, mock_client, binary_opp):
    """CONTROL: un 409 con OTRO code es estado DESCONOCIDO → la fila queda pending
    (la resuelve reconcile contra Kalshi), NO se asume kill."""
    other = KalshiClientError(409, "conflict", "{}", "market_closed")
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes", "fill_count": 10}},
        other,
        {"order": {"fill_count": 10}},
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[38, 10]], "no": []}}

    with _patched():
        assert await executor.execute(binary_opp) is False

    (no_row,) = _trade(side="no")
    assert no_row.status == "pending"


@pytest.mark.asyncio
async def test_unexpected_partial_fill_records_real_count(executor, mock_client, binary_opp):
    """Defensivo: si FOK devolviera un parcial (no debería), se registra el count REAL
    (no la ficción del pedido) y el rollback vende exactamente lo fillado."""
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes", "fill_count": 6}},  # parcial inesperado 6/10
        Exception("no buy"),
        {"order": {"fill_count": 6}},  # sell del rollback
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[38, 10]], "no": []}}

    with _patched():
        assert await executor.execute(binary_opp) is False

    sells = _sell_calls(mock_client)
    assert len(sells) == 1
    assert sells[0].kwargs["count"] == 6  # vende lo REAL, no 10
    (yes_row,) = _trade(side="yes")
    assert yes_row.count == 6
    assert yes_row.status == "settled"


# =====================================================
# Rollback: pérdida realizada (visible para stop-losses)
# =====================================================


@pytest.mark.asyncio
async def test_rollback_settles_row_with_realized_pnl(executor, mock_client, binary_opp):
    """La fila del BUY pasa a settled con pnl_cents (venta − compra − ambas fees):
    exactamente lo que agregan las ventanas de stop-loss diario/semanal/mensual."""
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes", "fill_count": 10}},
        Exception("no buy"),
        {"order": {"fill_count": 10}},
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[38, 10]], "no": []}}

    with _patched():
        await executor.execute(binary_opp)

    (yes_row,) = _trade(side="yes")
    expected = 10 * (38 - 40) - kalshi_fee_cents(10, 38) - kalshi_fee_cents(10, 40)
    assert yes_row.status == "settled"
    assert yes_row.pnl_cents == expected
    assert yes_row.settled_at is not None and yes_row.settled_at.tzinfo is None  # naive UTC
    assert "rollback_sell~38c" in (yes_row.notes or "")


@pytest.mark.asyncio
async def test_rollback_partial_sell_splits_row(executor, mock_client, binary_opp):
    """El IOC vende 4 y después 6: la fila se PARTE (patrón _settle_originals de M3) —
    hija settled por lo vendido, original con el remanente hasta que también se vende."""
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes", "fill_count": 10}},
        Exception("no buy"),
        {"order": {"fill_count": 4}},  # sell parcial
        {"order": {"fill_count": 6}},  # sell del remanente
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[38, 10]], "no": []}}

    with _patched():
        await executor.execute(binary_opp)

    yes_rows = _trade(side="yes")
    assert len(yes_rows) == 2
    child = next(t for t in yes_rows if "-rbs" in t.client_order_id)
    original = next(t for t in yes_rows if "-rbs" not in t.client_order_id)
    assert child.status == "settled" and child.count == 4
    assert child.pnl_cents == 4 * (38 - 40) - kalshi_fee_cents(4, 38) - kalshi_fee_cents(4, 40)
    assert original.status == "settled" and original.count == 6
    assert original.pnl_cents == 6 * (38 - 40) - kalshi_fee_cents(6, 38) - kalshi_fee_cents(6, 40)


@pytest.mark.asyncio
async def test_rollback_sell_without_fill_keeps_row_open(mock_client, mock_risk, binary_opp):
    """FAIL-SAFE: el IOC no llenó nada en ningún intento → la fila queda ABIERTA (filled,
    sin pnl): exposición visible para el RiskManager y huérfana gestionable por Motor 3.
    Jamás se marca cerrada una posición que sigue viva."""
    ex = ArbitrageExecutor(mock_client, mock_risk, max_rollback_retries=1)
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-yes", "fill_count": 10}},
        Exception("no buy"),
        {"order": {"fill_count": 0}},  # IOC sin fill
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[38, 10]], "no": []}}

    with _patched():
        assert await ex.execute(binary_opp) is False

    (yes_row,) = _trade(side="yes")
    assert yes_row.status == "filled"  # sigue abierta — nada se realizó
    assert yes_row.pnl_cents is None
    assert yes_row.count == 10
