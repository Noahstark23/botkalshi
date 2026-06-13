"""
Tests del RiskManager.

Mezcla:
- Tests con mocks para casos simples (bot paused, sizing math)
- AL MENOS UN test end-to-end con SQLite in-memory + Trade real para validar
  que las queries funcionan contra el schema real (NO solo contra mocks)
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
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
        s.MAX_WEEKLY_LOSS_PCT = 8.0
        s.MAX_MONTHLY_LOSS_PCT = 15.0
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
            # Anclado DENTRO de la ventana diaria de HOY (00:00 UTC + 1h): con
            # now−30min, correr el test entre 00:00 y 00:30 UTC caía en AYER →
            # el daily no veía la pérdida → flaky de medianoche (visto 2026-06-13).
            placed_at=datetime.combine(datetime.now(UTC).date(), time.min),
            filled_at=datetime.combine(datetime.now(UTC).date(), time.min) + timedelta(minutes=30),
            settled_at=datetime.combine(datetime.now(UTC).date(), time.min) + timedelta(hours=1),
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


@pytest.mark.asyncio
async def test_weekly_pnl_breach_triggers_killswitch_e2e(
    risk_manager, sample_opp, real_db_engine, monkeypatch
):
    """
    Pérdida dentro de la ventana semanal (> 8% capital = $24) dispara kill switch.

    El trade se inserta justo después del inicio de semana (lunes 00:01 UTC).
    Si hoy ES lunes, settled_at cae en today_start → también cuenta para daily.
    En ambos casos el resultado debe ser approved=False con "Stop-Loss" en reason.
    Capital=$300, weekly cap=$24. Pérdida=$30 supera el límite.
    """
    import src.risk.manager as rm_module

    def make_session():
        return Session(real_db_engine)

    monkeypatch.setattr(rm_module, "get_session", make_session)

    now_naive = datetime.now(UTC).replace(tzinfo=None)
    days_since_monday = now_naive.weekday()
    week_start = datetime.combine(now_naive.date() - timedelta(days=days_since_monday), time.min)
    settled_time = week_start + timedelta(hours=1)

    with Session(real_db_engine) as s:
        s.add(
            Trade(
                client_order_id="test-weekly-breach",
                ticker="KX-TEST",
                side="yes",
                action="buy",
                count=100,
                price_cents=50,
                strategy="motor_1_arbitrage",
                status="settled",
                pnl_cents=-3000,  # -$30 > weekly cap $24
                placed_at=settled_time - timedelta(hours=1),
                filled_at=settled_time - timedelta(hours=1),
                settled_at=settled_time,
            )
        )
        s.commit()

    with patch("src.risk.manager.alert_risk_event", new_callable=AsyncMock):
        decision = await risk_manager.check_pre_trade(sample_opp)

    assert decision.approved is False
    assert "Stop-Loss" in decision.reason
    assert BotState.is_paused is True


@pytest.mark.asyncio
async def test_weekly_pnl_old_trades_outside_window_dont_count(
    risk_manager, sample_opp, real_db_engine, monkeypatch
):
    """
    Trade de hace 10 días NO cuenta para la ventana semanal (siempre fuera del
    calendario de lunes a hoy). Pérdida $15 < weekly cap $24 y < monthly cap $45.
    """
    import src.risk.manager as rm_module

    def make_session():
        return Session(real_db_engine)

    monkeypatch.setattr(rm_module, "get_session", make_session)

    now_naive = datetime.now(UTC).replace(tzinfo=None)

    with Session(real_db_engine) as s:
        s.add(
            Trade(
                client_order_id="test-old-trade",
                ticker="KX-TEST",
                side="yes",
                action="buy",
                count=100,
                price_cents=50,
                strategy="motor_1_arbitrage",
                status="settled",
                pnl_cents=-1500,  # -$15: < weekly $24, < monthly $45
                placed_at=now_naive - timedelta(days=11),
                filled_at=now_naive - timedelta(days=11),
                settled_at=now_naive - timedelta(days=10),
            )
        )
        s.commit()

    # 10 días > 7 días → siempre fuera de la ventana semanal.
    # -$15 < cap mensual $45 → sin breach mensual tampoco.
    decision = await risk_manager.check_pre_trade(sample_opp)

    assert decision.approved is True


@pytest.mark.asyncio
async def test_monthly_pnl_breach_triggers_killswitch_e2e(
    risk_manager, sample_opp, real_db_engine, monkeypatch
):
    """
    Pérdida > 15% capital ($45) en el mes actual dispara kill switch.

    Trade insertado al inicio del mes (mes actual, fuera de la semana actual
    cuando el día del mes lo permite). Capital=$300, monthly cap=$45.
    Pérdida=$50 supera el límite sin importar la ventana que lo atrape primero.
    """
    import src.risk.manager as rm_module

    def make_session():
        return Session(real_db_engine)

    monkeypatch.setattr(rm_module, "get_session", make_session)

    now_naive = datetime.now(UTC).replace(tzinfo=None)
    month_start = datetime.combine(now_naive.date().replace(day=1), time.min)
    settled_time = month_start + timedelta(hours=2)

    with Session(real_db_engine) as s:
        s.add(
            Trade(
                client_order_id="test-monthly-breach",
                ticker="KX-TEST",
                side="yes",
                action="buy",
                count=200,
                price_cents=50,
                strategy="motor_1_arbitrage",
                status="settled",
                pnl_cents=-5000,  # -$50 > monthly cap $45
                placed_at=settled_time - timedelta(hours=1),
                filled_at=settled_time - timedelta(hours=1),
                settled_at=settled_time,
            )
        )
        s.commit()

    with patch("src.risk.manager.alert_risk_event", new_callable=AsyncMock):
        decision = await risk_manager.check_pre_trade(sample_opp)

    assert decision.approved is False
    assert "Stop-Loss" in decision.reason
    assert BotState.is_paused is True


@pytest.mark.asyncio
async def test_daily_breach_priority_over_weekly_monthly(
    risk_manager, sample_opp, real_db_engine, monkeypatch
):
    """
    Cuando hay pérdida HOY, el reason debe decir 'Diario' (primer ítem del loop).

    El loop de limits = [Diario, Semanal, Mensual] retorna el primero que dispara.
    Un trade de hace 30 minutos cae en daily, weekly y monthly → 'Diario' gana.
    Capital=$300, daily cap=$9 (3%). Pérdida=$15 supera.
    """
    import src.risk.manager as rm_module

    def make_session():
        return Session(real_db_engine)

    monkeypatch.setattr(rm_module, "get_session", make_session)

    now_naive = datetime.now(UTC).replace(tzinfo=None)

    with Session(real_db_engine) as s:
        s.add(
            Trade(
                client_order_id="test-daily-priority",
                ticker="KX-TEST",
                side="yes",
                action="buy",
                count=100,
                price_cents=50,
                strategy="motor_1_arbitrage",
                status="settled",
                pnl_cents=-1500,  # -$15 > daily $9, weekly $24 y monthly $45 → daily primero
                # Anclado a HOY 00:00 UTC (+offsets) — now−30min caía en AYER si el
                # test corría entre 00:00-00:30 UTC → flaky de medianoche (2026-06-13).
                placed_at=datetime.combine(now_naive.date(), time.min),
                filled_at=datetime.combine(now_naive.date(), time.min) + timedelta(minutes=30),
                settled_at=datetime.combine(now_naive.date(), time.min) + timedelta(hours=1),
            )
        )
        s.commit()

    with patch("src.risk.manager.alert_risk_event", new_callable=AsyncMock):
        decision = await risk_manager.check_pre_trade(sample_opp)

    assert decision.approved is False
    assert "Diario" in decision.reason
    assert BotState.is_paused is True
