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
async def test_hard_first_full_arb_places_hard_then_easy(executor, mock_client, binary_opp):
    """Caso feliz hard-first: la dura (NO) llena → recién ahí la fácil (YES) → arb completo.
    Verifica el ORDEN (dura primero) y que ambas quedan filled."""
    mock_client.place_order.return_value = {"order": {"order_id": "k", "fill_count": 10}}
    with _patched():
        assert await executor.execute(binary_opp) is True

    buys = _buy_calls(mock_client)
    assert [c.kwargs["side"] for c in buys] == [
        "no",
        "yes",
    ]  # dura (45) primero, fácil (40) después
    assert all(t.status == "filled" and t.count == 10 for t in _trade())
    assert len(_sell_calls(mock_client)) == 0  # sin rollback


@pytest.mark.asyncio
async def test_hard_leg_killed_skips_without_sending_easy(executor, mock_client, binary_opp):
    """Hard-first (fix 2026-07-09): la pata DURA (NO, precio 45 > 40) se coloca PRIMERO. Si
    killa (HTTP 200 con fill 0), la fácil (YES) NUNCA se envía → cero exposición, skip limpio,
    SIN rollback. Es el caso DOMINANTE que antes encadenaba partial→rollback→circuit breaker."""
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-no", "fill_count": 0}},  # NO (dura) FOK killed
    ]
    with _patched():
        assert await executor.execute(binary_opp) is False

    buys = _buy_calls(mock_client)
    assert len(buys) == 1  # SOLO la dura se envió — la fácil NUNCA
    assert buys[0].kwargs["side"] == "no"  # la dura (precio mayor) va primero
    assert len(_sell_calls(mock_client)) == 0  # sin rollback: no se compró nada
    by_side = {t.side: t for t in _trade()}
    assert by_side["no"].status == "cancelled"  # killed
    assert by_side["yes"].status == "cancelled"  # nunca enviada


@pytest.mark.asyncio
async def test_hard_leg_409_kill_skips_without_sending_easy(executor, mock_client, binary_opp):
    """Ídem con el 409 determinístico de FOK sin volumen: la dura killa → la fácil no se
    envía, cero rollback."""
    kill = KalshiClientError(409, "conflict", "{}", "fill_or_kill_insufficient_resting_volume")
    mock_client.place_order.side_effect = [kill]  # NO (dura) 409-kill
    with _patched():
        assert await executor.execute(binary_opp) is False

    assert len(_buy_calls(mock_client)) == 1  # la fácil no se envió
    assert len(_sell_calls(mock_client)) == 0
    by_side = {t.side: t for t in _trade()}
    assert by_side["no"].status == "cancelled" and by_side["yes"].status == "cancelled"


@pytest.mark.asyncio
async def test_hard_leg_other_409_error_skips_and_stays_pending(executor, mock_client, binary_opp):
    """CONTROL: un 409 con OTRO code en la dura = estado DESCONOCIDO → la fila dura queda
    pending (la resuelve reconcile), la fácil NO se envía. Jamás se asume kill."""
    other = KalshiClientError(409, "conflict", "{}", "market_closed")
    mock_client.place_order.side_effect = [other]  # NO (dura) error desconocido
    with _patched():
        assert await executor.execute(binary_opp) is False

    assert len(_buy_calls(mock_client)) == 1  # la fácil no se envió
    by_side = {t.side: t for t in _trade()}
    assert by_side["no"].status == "pending"  # desconocido → reconcile
    assert by_side["yes"].status == "cancelled"  # no enviada


@pytest.mark.asyncio
async def test_hard_partial_fill_rolls_back_without_sending_easy(executor, mock_client, binary_opp):
    """Defensivo: si la dura devolviera un parcial (no debería con FOK), hay exposición pero
    el arb no puede quedar simétrico → la fácil NO se envía y se rollbackea la dura por el
    count REAL (6), no el pedido (10)."""
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-no", "fill_count": 6}},  # NO (dura) parcial 6/10
        {"order": {"fill_count": 6}},  # sell del rollback de la dura
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"no": [[43, 10]], "yes": []}}
    with _patched():
        assert await executor.execute(binary_opp) is False

    buys = _buy_calls(mock_client)
    assert len(buys) == 1 and buys[0].kwargs["side"] == "no"  # la fácil no se envió
    sells = _sell_calls(mock_client)
    assert len(sells) == 1 and sells[0].kwargs["count"] == 6  # vende lo REAL, no 10
    (no_row,) = _trade(side="no")
    assert no_row.count == 6 and no_row.status == "settled"


# =====================================================
# Rollback (dura llena + fácil falla → path #137): pérdida realizada
# =====================================================


@pytest.mark.asyncio
async def test_rollback_settles_row_with_realized_pnl(executor, mock_client, binary_opp):
    """La dura (NO) llena, la fácil (YES) falla → rollback de la dura con la pérdida REALIZADA
    en pnl_cents (venta − compra − ambas fees): lo que agregan las ventanas de stop-loss."""
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-no", "fill_count": 10}},  # NO (dura) llena
        Exception("no easy"),  # YES (fácil) falla
        {"order": {"fill_count": 10}},  # sell del rollback de NO
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"no": [[43, 10]], "yes": []}}

    with _patched():
        await executor.execute(binary_opp)

    (no_row,) = _trade(side="no")
    expected = 10 * (43 - 45) - kalshi_fee_cents(10, 43) - kalshi_fee_cents(10, 45)
    assert no_row.status == "settled"
    assert no_row.pnl_cents == expected
    assert no_row.settled_at is not None and no_row.settled_at.tzinfo is None  # naive UTC
    assert "rollback_sell~43c" in (no_row.notes or "")


@pytest.mark.asyncio
async def test_rollback_partial_sell_splits_row(executor, mock_client, binary_opp):
    """El IOC vende 4 y después 6: la fila se PARTE (patrón _settle_originals de M3) —
    hija settled por lo vendido, original con el remanente hasta que también se vende."""
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-no", "fill_count": 10}},  # NO (dura) llena
        Exception("no easy"),  # YES (fácil) falla
        {"order": {"fill_count": 4}},  # sell parcial
        {"order": {"fill_count": 6}},  # sell del remanente
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"no": [[43, 10]], "yes": []}}

    with _patched():
        await executor.execute(binary_opp)

    no_rows = _trade(side="no")
    assert len(no_rows) == 2
    child = next(t for t in no_rows if "-rbs" in t.client_order_id)
    original = next(t for t in no_rows if "-rbs" not in t.client_order_id)
    assert child.status == "settled" and child.count == 4
    assert child.pnl_cents == 4 * (43 - 45) - kalshi_fee_cents(4, 43) - kalshi_fee_cents(4, 45)
    assert original.status == "settled" and original.count == 6
    assert original.pnl_cents == 6 * (43 - 45) - kalshi_fee_cents(6, 43) - kalshi_fee_cents(6, 45)


@pytest.mark.asyncio
async def test_rollback_sell_without_fill_keeps_row_open(mock_client, mock_risk, binary_opp):
    """FAIL-SAFE: el IOC no llenó nada en ningún intento → la fila queda ABIERTA (filled,
    sin pnl): exposición visible para el RiskManager y huérfana gestionable por Motor 3.
    Jamás se marca cerrada una posición que sigue viva."""
    ex = ArbitrageExecutor(mock_client, mock_risk, max_rollback_retries=1)
    mock_client.place_order.side_effect = [
        {"order": {"order_id": "k-no", "fill_count": 10}},  # NO (dura) llena
        Exception("no easy"),  # YES (fácil) falla
        {"order": {"fill_count": 0}},  # IOC sin fill
    ]
    mock_client.get_orderbook.return_value = {"orderbook": {"no": [[43, 10]], "yes": []}}

    with _patched():
        assert await ex.execute(binary_opp) is False

    (no_row,) = _trade(side="no")
    assert no_row.status == "filled"  # sigue abierta — nada se realizó
    assert no_row.pnl_cents is None
    assert no_row.count == 10
