"""
Tests del incidente 2026-07-07 (−$139: 186 trades en 6min, huérfanas + direccional HOUWSH).

Bug 1 (P0): pre-check de balance REAL antes de colocar CUALQUIER pata del arb.
Bug 2 (P0): guard de exposición direccional por EVENTO (tickers hermanos del mismo partido).
Bug 3 (P1): UN rollback abortado por slippage = pausa INMEDIATA y PERSISTENTE (kill-switch DB).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import SQLModel, create_engine, select

import src.storage.models as _models
from src.math.arbitrage import ArbLeg, detect_binary_arb
from src.monitoring.health import BotState
from src.risk.manager import RiskManager, TradeDecision
from src.storage.models import PortfolioPosition, RiskEvent, get_session, kill_switch_engaged
from src.strategies.motor_1_arbitrage.event_exposure import EventExposureTracker, event_ticker_of
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor

EVENT = "KXMLBGAME-26JUL061845HOUWSH"


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
    client.place_order.return_value = {"order": {"order_id": "k-1", "fill_count": 10}}
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
    opp = detect_binary_arb(f"{EVENT}-HOU", 18, 200, 75, 200, max_count=10)
    assert opp is not None
    return opp


def _settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.TRADING_ENABLED = True
    s.ACTIVE_CAPITAL_USD = 1200.0
    s.MAX_EVENT_DIRECTIONAL_EXPOSURE_USD = 25.0
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def _patched(settings):
    return patch("src.strategies.motor_1_arbitrage.executor.get_settings", return_value=settings)


# =====================================================
# Bug 1 — pre-check de balance
# =====================================================


@pytest.mark.asyncio
async def test_bug1_insufficient_balance_aborts_before_any_leg(executor, mock_client, binary_opp):
    """Cash real < costo del arb entero → abort limpio: CERO patas colocadas, cero intents."""
    mock_client.get_available_balance_usd.return_value = 5.0  # arb cuesta ~$9.30 + fees

    with _patched(_settings()):
        result = await executor.execute(binary_opp)

    assert result is False
    mock_client.place_order.assert_not_called()  # ninguna pata → ninguna huérfana posible
    with get_session() as s:
        assert s.exec(select(_models.Trade)).first() is None  # sin intents huérfanos


@pytest.mark.asyncio
async def test_bug1_sufficient_balance_places_both_legs(executor, mock_client, binary_opp):
    mock_client.get_available_balance_usd.return_value = 100.0
    with _patched(_settings()):
        result = await executor.execute(binary_opp)
    assert result is True
    assert mock_client.place_order.await_count == 2


@pytest.mark.asyncio
async def test_bug1_balance_error_fails_open(executor, mock_client, binary_opp):
    """La lectura de balance falla → fail-open documentado (comportamiento previo)."""
    mock_client.get_available_balance_usd.side_effect = RuntimeError("api caída")
    with _patched(_settings()):
        result = await executor.execute(binary_opp)
    assert result is True  # se intentó igual (peor caso = insufficient_balance de Kalshi)


@pytest.mark.asyncio
async def test_bug1_balance_cached_within_ttl(executor, mock_client):
    """Dos lecturas dentro del TTL → UNA llamada a la API; y un fill invalida el caché."""
    v1 = await executor._available_balance_usd()
    v2 = await executor._available_balance_usd()
    assert v1 == v2 == 10_000.0
    assert mock_client.get_available_balance_usd.await_count == 1  # cacheado

    executor._balance_cache = None  # lo que hace el fill loop tras cada place exitoso
    await executor._available_balance_usd()
    assert mock_client.get_available_balance_usd.await_count == 2  # lectura fresca


@pytest.mark.asyncio
async def test_bug1_fill_invalidates_balance_cache(executor, mock_client, binary_opp):
    """Tras un execute con fills, el caché quedó invalidado (el place consumió cash)."""
    with _patched(_settings()):
        await executor.execute(binary_opp)
    assert executor._balance_cache is None


# =====================================================
# Bug 2 — exposición direccional por evento
# =====================================================


def test_event_ticker_of():
    assert event_ticker_of(f"{EVENT}-HOU") == EVENT
    assert event_ticker_of(f"{EVENT}-WSH") == EVENT
    assert event_ticker_of("SINGUION") == "SINGUION"


def test_tracker_hedged_pair_is_zero():
    """Un arb intra-ticker completo (yes+no mismo count) → exposición CERO."""
    t = EventExposureTracker(max_exposure_usd=25.0)
    t.record_fill(f"{EVENT}-HOU", "yes", 100, 18)
    t.record_fill(f"{EVENT}-HOU", "no", 100, 75)
    assert t.exposure_usd(EVENT) == 0.0


def test_tracker_cross_ticker_same_direction_sums():
    """El caso del incidente: yes@-HOU + no@-WSH = MISMA dirección (pro-HOU) → SUMAN."""
    t = EventExposureTracker(max_exposure_usd=25.0)
    t.record_fill(f"{EVENT}-HOU", "yes", 21, 18)  # huérfanas: $3.78
    t.record_fill(f"{EVENT}-WSH", "no", 190, 30)  # residual: $57.00
    assert t.exposure_usd(EVENT) == pytest.approx(21 * 0.18 + 190 * 0.30)
    allowed, exposure, event = t.check_new_arb([f"{EVENT}-HOU"])
    assert allowed is False and event == EVENT


def test_tracker_db_source_uses_portfolio_positions():
    """La verdad de Kalshi (portfolio_positions, netting incluido) también cuenta: la réplica
    exacta del incidente (HOU yes $78.75 + WSH no $56.36 = $135.11 pro-HOU)."""
    with get_session() as s:
        s.add(PortfolioPosition(ticker=f"{EVENT}-HOU", side="yes", count=433, exposure_cents=7875))
        s.add(PortfolioPosition(ticker=f"{EVENT}-WSH", side="no", count=190, exposure_cents=5636))
        s.commit()
    t = EventExposureTracker(max_exposure_usd=25.0)
    assert t.exposure_usd(EVENT) == pytest.approx(135.11)


@pytest.mark.asyncio
async def test_bug2_guard_rejects_arb_on_exposed_event(executor, mock_client, mock_risk):
    """Replay del incidente: con el evento ya cargado direccional, el próximo arb se RECHAZA
    sin colocar patas y alerta 1 sola vez."""
    with get_session() as s:
        s.add(PortfolioPosition(ticker=f"{EVENT}-WSH", side="no", count=190, exposure_cents=5636))
        s.commit()
    opp = detect_binary_arb(f"{EVENT}-HOU", 18, 200, 75, 200, max_count=10)

    with (
        _patched(_settings()),
        patch(
            "src.strategies.motor_1_arbitrage.executor.alert_risk_event", new_callable=AsyncMock
        ) as alert,
    ):
        r1 = await executor.execute(opp)
        r2 = await executor.execute(opp)

    assert r1 is False and r2 is False
    mock_client.place_order.assert_not_called()
    alert.assert_awaited_once()  # throttle: 1 alerta por evento, no por burst


@pytest.mark.asyncio
async def test_bug2_other_event_unaffected(executor, mock_client):
    """La exposición de UN evento no bloquea arbs de OTRO partido."""
    with get_session() as s:
        s.add(PortfolioPosition(ticker=f"{EVENT}-WSH", side="no", count=190, exposure_cents=5636))
        s.commit()
    opp = detect_binary_arb("KXMLBGAME-26JUL07MILSTL-STL", 40, 200, 45, 200, max_count=10)
    with _patched(_settings()):
        result = await executor.execute(opp)
    assert result is True
    assert mock_client.place_order.await_count == 2


# =====================================================
# Bug 3 — rollback abortado por slippage = pausa inmediata y persistente
# =====================================================


@pytest.mark.asyncio
async def test_bug3_single_aborted_rollback_pauses_persistently(executor, mock_client):
    """UN abort por slippage: RiskEvent crítico + BotState pausado + kill-switch EN DB
    (sobrevive redeploy — el boot re-hidrata la pausa)."""
    # bid actual 2c vs original 18c → slippage 89% > max 10% → abort
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[2, 100]]}}
    leg = ArbLeg(
        market_ticker=f"{EVENT}-HOU", side="yes", price_cents=18, count=10, available_size=100
    )

    with (
        patch("src.strategies.motor_1_arbitrage.executor.alert_error", new_callable=AsyncMock),
        patch("src.strategies.motor_1_arbitrage.executor.alert_risk_event", new_callable=AsyncMock),
    ):
        await executor._execute_iterative_rollback([(leg, "coid-1", 10)])

    assert BotState.is_paused is True
    assert "rollback_aborted_slippage" in (BotState.pause_reason or "")
    engaged, reason = kill_switch_engaged()
    assert engaged is True  # PERSISTENTE: un redeploy arranca pausado
    assert "rollback_aborted_slippage" in (reason or "")
    with get_session() as s:
        ev = s.exec(
            select(RiskEvent).where(RiskEvent.event_type == "rollback_aborted_slippage")
        ).first()
    assert ev is not None and ev.severity == "critical"


@pytest.mark.asyncio
async def test_bug3_successful_rollback_does_not_pause(executor, mock_client):
    """Un rollback NORMAL (slippage bajo, venta ok) NO dispara la pausa del Bug 3."""
    mock_client.get_orderbook.return_value = {"orderbook": {"yes": [[17, 100]]}}  # slippage ~5.6%
    leg = ArbLeg(
        market_ticker=f"{EVENT}-HOU", side="yes", price_cents=18, count=10, available_size=100
    )
    with patch(
        "src.strategies.motor_1_arbitrage.executor.alert_risk_event", new_callable=AsyncMock
    ):
        await executor._execute_iterative_rollback([(leg, "coid-1", 10)])
    engaged, _ = kill_switch_engaged()
    assert engaged is False
    # el circuit breaker genérico (3 en 60min) sigue intacto para rollbacks normales
