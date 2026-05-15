"""
Tests del RiskManager.

Mezcla:
- Tests con mocks para casos simples (bot paused, sizing math)
- AL MENOS UN test end-to-end con SQLite in-memory + Trade real para validar
  que las queries funcionan contra el schema real (NO solo contra mocks)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.storage.models import Trade


@pytest.fixture(autouse=True)
def reset_botstate():
    """Asegurar BotState limpio entre tests."""
    BotState.is_paused = False
    BotState.pause_reason = None
    yield
    BotState.is_paused = False
    BotState.pause_reason = None


@pytest.fixture
def mock_settings():
    with patch("src.risk.manager.get_settings") as m:
        s = MagicMock()
        s.TRADING_ENABLED = True
        s.ACTIVE_CAPITAL_USD = 300.0
        s.MAX_DAILY_LOSS_PCT = 3.0
        s.MAX_SIMULTANEOUS_EXPOSURE_PCT = 25.0
        s.MAX_TRADE_SIZE_PCT = 5.0
        m.return_value = s
        yield s


@pytest.fixture
def risk_manager(mock_settings):
    return RiskManager()


@pytest.fixture
def sample_opp():
    """Arb binario: cost=85¢, count=15. Capital cap = 1500//85 = 17, so opp.count=15 wins."""
    leg1 = ArbLeg(market_ticker="KX-TEST", side="yes", price_cents=40, count=15, available_size=100)
    leg2 = ArbLeg(market_ticker="KX-TEST", side="no", price_cents=45, count=15, available_size=100)
    return ArbOpportunity(
        legs=(leg1, leg2),
        count=15,
        gross_profit_cents=225,
        fees_cents=4,
        net_profit_cents=221,
        edge_pct=17.0,
    )


# =========================================================
# MOCK-BASED TESTS
# =========================================================


@pytest.mark.asyncio
async def test_bot_paused_rejects_trade(risk_manager, sample_opp):
    BotState.is_paused = True
    BotState.pause_reason = "Rollback loop"
    decision = await risk_manager.check_pre_trade(sample_opp)
    assert decision.approved is False
    assert "Rollback loop" in decision.reason


@pytest.mark.asyncio
@patch("src.risk.manager.get_session")
async def test_no_active_trades_allows_full_sizing(mock_session, risk_manager, sample_opp):
    mock_db = MagicMock()
    mock_session.return_value.__enter__.return_value = mock_db
    mock_db.exec.return_value = []

    decision = await risk_manager.check_pre_trade(sample_opp)

    assert decision.approved is True
    # 1500 cents max // 85 cents = 17 contracts cap, but opp.count=15 wins
    assert decision.max_allowed_count == 15


@pytest.mark.asyncio
@patch("src.risk.manager.get_session")
async def test_exposure_exhausted_rejects(mock_session, risk_manager, sample_opp):
    """$80 ya invertido, cap=$75 (25% of $300), debe rechazar."""
    mock_trade = MagicMock()
    mock_trade.price_cents = 80
    mock_trade.count = 100  # $80 exposure

    mock_db = MagicMock()
    mock_session.return_value.__enter__.return_value = mock_db
    # Daily PnL query → [], Exposure query → [mock_trade]
    mock_db.exec.side_effect = [[], [mock_trade]]

    decision = await risk_manager.check_pre_trade(sample_opp)
    assert decision.approved is False
    assert "Exposición" in decision.reason


@pytest.mark.asyncio
@patch("src.risk.manager.get_session")
async def test_sizing_cap_strictly_applied(mock_session, risk_manager):
    """Cuando opp.count > cap de capital, capital manda."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__.return_value = mock_db
    mock_db.exec.side_effect = [[], []]

    leg1 = ArbLeg(
        market_ticker="KX-TEST", side="yes", price_cents=50, count=1000, available_size=1000
    )
    leg2 = ArbLeg(
        market_ticker="KX-TEST", side="no", price_cents=40, count=1000, available_size=1000
    )
    opp = ArbOpportunity(
        legs=(leg1, leg2),
        count=1000,
        gross_profit_cents=10000,
        fees_cents=10,
        net_profit_cents=9990,
        edge_pct=11.1,
    )

    decision = await risk_manager.check_pre_trade(opp)
    assert decision.approved is True
    # usable=$15 (5% of $300), cost/unit=90¢ → 1500//90 = 16
    assert decision.max_allowed_count == 16


@pytest.mark.asyncio
@patch("src.risk.manager.get_session")
async def test_opp_count_constrains_when_below_capital(mock_session, risk_manager):
    """Cuando opp.count < cap de capital, opp.count manda."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__.return_value = mock_db
    mock_db.exec.side_effect = [[], []]

    leg1 = ArbLeg(market_ticker="KX-TEST", side="yes", price_cents=50, count=5, available_size=5)
    leg2 = ArbLeg(market_ticker="KX-TEST", side="no", price_cents=40, count=5, available_size=5)
    opp = ArbOpportunity(
        legs=(leg1, leg2),
        count=5,
        gross_profit_cents=50,
        fees_cents=5,
        net_profit_cents=45,
        edge_pct=11.1,
    )

    decision = await risk_manager.check_pre_trade(opp)
    assert decision.approved is True
    assert decision.max_allowed_count == 5


@pytest.mark.asyncio
@patch("src.risk.manager.get_session")
async def test_zero_cost_legs_rejected(mock_session, risk_manager):
    mock_db = MagicMock()
    mock_session.return_value.__enter__.return_value = mock_db
    mock_db.exec.side_effect = [[], []]

    leg1 = ArbLeg(market_ticker="KX-TEST", side="yes", price_cents=0, count=5, available_size=5)
    leg2 = ArbLeg(market_ticker="KX-TEST", side="no", price_cents=0, count=5, available_size=5)
    opp = ArbOpportunity(
        legs=(leg1, leg2),
        count=5,
        gross_profit_cents=500,
        fees_cents=0,
        net_profit_cents=500,
        edge_pct=999.0,
    )

    decision = await risk_manager.check_pre_trade(opp)
    assert decision.approved is False
    assert "inválidos" in decision.reason


# =========================================================
# END-TO-END TESTS (real SQLite, valida queries contra schema real)
# =========================================================


@pytest.fixture
def real_db_engine():
    """SQLite in-memory con schema real para tests E2E."""
    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    yield engine


@pytest.mark.asyncio
async def test_daily_pnl_breach_triggers_killswitch_e2e(
    risk_manager, sample_opp, real_db_engine, monkeypatch
):
    """
    E2E test: inserta Trade real con status='settled' y pnl_cents=-1000,
    verifica que check_pre_trade detecta el breach contra el schema REAL.

    Este test atrapa bugs como "filtré por status que no existe" que los
    tests mock-puros no detectan.
    """
    import src.risk.manager as rm_module

    def make_session():
        return Session(real_db_engine)

    monkeypatch.setattr(rm_module, "get_session", make_session)

    # Insertar trade settled hoy con pérdida $10 (excede límite $9 = 3% de $300)
    with Session(real_db_engine) as s:
        losing_trade = Trade(
            client_order_id="e2e-loss-1",
            ticker="KX-TEST",
            side="yes",
            action="buy",
            count=100,
            price_cents=50,
            strategy="motor_1_arbitrage",
            status="settled",
            pnl_cents=-1000,  # -$10
            placed_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2),
            filled_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
            settled_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30),
        )
        s.add(losing_trade)
        s.commit()

    with patch("src.risk.manager.alert_risk_event", new_callable=AsyncMock) as mock_alert:
        decision = await risk_manager.check_pre_trade(sample_opp)

    assert decision.approved is False
    assert "Stop-Loss" in decision.reason
    assert BotState.is_paused is True
    assert BotState.pause_reason is not None
    assert "Stop-Loss" in BotState.pause_reason
    mock_alert.assert_called_once()


@pytest.mark.asyncio
async def test_daily_pnl_only_counts_settled_not_filled_e2e(
    risk_manager, sample_opp, real_db_engine, monkeypatch
):
    """
    Trade fillado con pnl=-1000 pero status='filled' (no settled) →
    NO debe disparar stop loss (PnL no realizado).
    """
    import src.risk.manager as rm_module

    def make_session():
        return Session(real_db_engine)

    monkeypatch.setattr(rm_module, "get_session", make_session)

    with Session(real_db_engine) as s:
        filled_only = Trade(
            client_order_id="e2e-filled-1",
            ticker="KX-TEST",
            side="yes",
            action="buy",
            count=100,
            price_cents=50,
            strategy="motor_1_arbitrage",
            status="filled",
            pnl_cents=-1000,
            placed_at=datetime.now(UTC).replace(tzinfo=None),
            filled_at=datetime.now(UTC).replace(tzinfo=None),
        )
        s.add(filled_only)
        s.commit()

    decision = await risk_manager.check_pre_trade(sample_opp)

    # Stop loss NO debe disparar (trade no settled)
    if not decision.approved:
        # Aceptable que rechace por exposure pero NO por stop loss
        assert "Stop-Loss" not in decision.reason
    assert BotState.is_paused is False
