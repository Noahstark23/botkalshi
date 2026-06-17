"""
Tests de INTEGRACIÓN del RiskManager (FASE 0.3) — los que exige el checklist.

DB real temporal + trades simulados de AMBOS motores. Verifican:
  - la escalera -3/-8/-15 DISPARA con pnl settled real (y la pausa es persistente);
  - el cap del 25% ACUMULA posiciones de los dos motores;
  - el fix de sobrestima: arb completo hedged descontado, pata suelta cuenta entera;
  - el lock de clase serializa check_pre_trade concurrentes (cross-instancia).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.storage.models as models
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.monitoring.health import BotState
from src.risk.manager import RiskManager


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture(autouse=True)
def sqlite_engine(tmp_path):
    """DB real temporal como singleton de models (todas las queries del manager la usan)."""
    db = tmp_path / "risk_integration.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


@pytest.fixture(autouse=True)
def _reset_botstate():
    prev = (BotState.is_paused, BotState.pause_reason)
    BotState.is_paused, BotState.pause_reason = False, None
    yield
    BotState.is_paused, BotState.pause_reason = prev


def _risk_settings() -> MagicMock:
    s = MagicMock()
    s.ACTIVE_CAPITAL_USD = 4000.0  # bankroll escalado
    s.MAX_DAILY_LOSS_PCT = 3.0  # $120
    s.MAX_WEEKLY_LOSS_PCT = 8.0  # $320
    s.MAX_MONTHLY_LOSS_PCT = 15.0  # $600
    s.MAX_SIMULTANEOUS_EXPOSURE_PCT = 25.0  # $1000
    s.MAX_TRADE_SIZE_PCT = 5.0  # $200
    s.MAX_TRADE_SIZE_USD = 200.0  # cap absoluto anti-slippage
    return s


def _manager() -> RiskManager:
    with patch("src.risk.manager.get_settings", return_value=_risk_settings()):
        return RiskManager()


def _opp(count: int = 5) -> ArbOpportunity:
    yes = ArbLeg(market_ticker="KXNEW", side="yes", price_cents=40, count=count, available_size=100)
    no = ArbLeg(market_ticker="KXNEW", side="no", price_cents=45, count=count, available_size=100)
    return ArbOpportunity(
        legs=(yes, no),
        count=count,
        gross_profit_cents=75,
        fees_cents=4,
        net_profit_cents=71,
        edge_pct=2.0,
    )


def _insert_trade(
    *,
    strategy: str,
    status: str,
    side: str = "yes",
    ticker: str = "KXT",
    price: int = 40,
    count: int = 10,
    pnl: int | None = None,
    settled: bool = False,
    arb_id: str | None = None,
) -> None:
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id=f"{arb_id}-{side}" if arb_id else str(uuid.uuid4()),
                ticker=ticker,
                side=side,
                action="buy",
                count=count,
                price_cents=price,
                strategy=strategy,
                status=status,
                pnl_cents=pnl,
                settled_at=_naive_now() if settled else None,
                notes=f"arb_id={arb_id}" if arb_id else None,
            )
        )
        s.commit()


# =====================================================
# Escalera de stop-losses con pnl settled REAL (ambos motores)
# =====================================================


@pytest.mark.asyncio
async def test_daily_stop_loss_fires_on_settled_pnl_from_both_motors():
    """Pérdidas settled de motor_rest (-$70) + motor_1 (-$51) = -$121 ≥ $120 → DISPARA."""
    _insert_trade(strategy="motor_rest_arb", status="settled", pnl=-7000, settled=True)
    _insert_trade(strategy="motor_1_arbitrage", status="settled", pnl=-5100, settled=True)
    mgr = _manager()

    with patch("src.risk.manager.alert_risk_event", new=AsyncMock()) as mock_alert:
        decision = await mgr.check_pre_trade(_opp())

    assert decision.approved is False
    assert "Stop-Loss Diario" in decision.reason
    assert BotState.is_paused is True  # kill-switch global
    assert models.kill_switch_engaged()[0] is True  # y PERSISTENTE (#32)
    mock_alert.assert_awaited()


@pytest.mark.asyncio
async def test_losses_below_threshold_do_not_fire():
    """-$119 < $120 → no dispara; el check sigue su curso y aprueba."""
    _insert_trade(strategy="motor_rest_arb", status="settled", pnl=-11900, settled=True)
    mgr = _manager()
    with patch("src.risk.manager.alert_risk_event", new=AsyncMock()):
        decision = await mgr.check_pre_trade(_opp())
    assert decision.approved is True
    assert BotState.is_paused is False


# =====================================================
# Cap de exposición del 25% acumulando AMBOS motores
# =====================================================


@pytest.mark.asyncio
async def test_exposure_cap_accumulates_both_motors():
    """Motor 1 filled $600 + Motor REST pending $450 = $1050 > $1000 → rechazado por exposición."""
    _insert_trade(strategy="motor_1_arbitrage", status="filled", price=60, count=1000)  # $600
    _insert_trade(strategy="motor_rest_arb", status="pending", price=45, count=1000)  # $450
    mgr = _manager()
    with patch("src.risk.manager.alert_risk_event", new=AsyncMock()):
        decision = await mgr.check_pre_trade(_opp())
    assert decision.approved is False
    assert "Exposición" in decision.reason


# =====================================================
# Fix de sobrestima: arbs hedged descontados, patas sueltas cuentan
# =====================================================


def test_complete_hedged_arb_is_discounted_to_zero():
    """Arb both-FILL (yes+no mismo ticker, mismo arb_id) → riesgo direccional CERO."""
    arb = str(uuid.uuid4())
    _insert_trade(
        strategy="motor_rest_arb",
        status="filled",
        side="yes",
        ticker="KXA",
        price=40,
        count=5,
        arb_id=arb,
    )
    _insert_trade(
        strategy="motor_rest_arb",
        status="filled",
        side="no",
        ticker="KXA",
        price=45,
        count=5,
        arb_id=arb,
    )
    assert _manager()._get_current_exposure_usd() == 0.0


def test_lone_filled_leg_counts_fully():
    """Pata suelta filled (la expuesta de un kill-switch) → cuenta ENTERA."""
    _insert_trade(
        strategy="motor_rest_arb",
        status="filled",
        side="yes",
        ticker="KXB",
        price=40,
        count=5,
        arb_id=str(uuid.uuid4()),
    )
    assert _manager()._get_current_exposure_usd() == pytest.approx(2.00)  # 40×5/100


def test_partial_hedge_discounts_only_paired_contracts():
    """Counts desparejos: solo los contratos EMPAREJADOS se descuentan."""
    arb = str(uuid.uuid4())
    _insert_trade(
        strategy="motor_rest_arb",
        status="filled",
        side="yes",
        ticker="KXC",
        price=40,
        count=10,
        arb_id=arb,
    )  # $4.00
    _insert_trade(
        strategy="motor_rest_arb",
        status="filled",
        side="no",
        ticker="KXC",
        price=45,
        count=5,
        arb_id=arb,
    )  # $2.25
    # paired=5 → descuento 5×(40+45)=425¢ → (400+225−425)/100 = $2.00
    assert _manager()._get_current_exposure_usd() == pytest.approx(2.00)


def test_pending_legs_are_never_discounted():
    """Pending (aún sin saber si llenó) → cuenta entera aunque tenga arb_id (duda=sobrestimar)."""
    arb = str(uuid.uuid4())
    _insert_trade(
        strategy="motor_rest_arb",
        status="pending",
        side="yes",
        ticker="KXD",
        price=40,
        count=5,
        arb_id=arb,
    )
    _insert_trade(
        strategy="motor_rest_arb",
        status="pending",
        side="no",
        ticker="KXD",
        price=45,
        count=5,
        arb_id=arb,
    )
    assert _manager()._get_current_exposure_usd() == pytest.approx(4.25)


def test_cross_ticker_legs_not_treated_as_hedge():
    """Patas en mercados DISTINTOS (no es el arb binario) → sin descuento."""
    arb = str(uuid.uuid4())
    _insert_trade(
        strategy="motor_rest_arb",
        status="filled",
        side="yes",
        ticker="KXE",
        price=40,
        count=5,
        arb_id=arb,
    )
    _insert_trade(
        strategy="motor_rest_arb",
        status="filled",
        side="no",
        ticker="KXF",
        price=45,
        count=5,
        arb_id=arb,
    )
    assert _manager()._get_current_exposure_usd() == pytest.approx(4.25)


# =====================================================
# Race: el lock de CLASE serializa checks concurrentes (cross-instancia)
# =====================================================


@pytest.mark.asyncio
async def test_concurrent_checks_are_serialized_across_instances():
    """Dos RiskManager (dos motores) chequeando a la vez → NUNCA solapados."""
    mgr_a, mgr_b = _manager(), _manager()
    overlap = {"in_flight": 0, "max_seen": 0}

    def make_slow_stop_losses():
        async def slow() -> None:
            overlap["in_flight"] += 1
            overlap["max_seen"] = max(overlap["max_seen"], overlap["in_flight"])
            await asyncio.sleep(0.02)  # ventana donde un check sin lock se colaría
            overlap["in_flight"] -= 1
            return None

        return slow

    with (
        patch.object(mgr_a, "_check_timeframe_stop_losses", new=make_slow_stop_losses()),
        patch.object(mgr_b, "_check_timeframe_stop_losses", new=make_slow_stop_losses()),
    ):
        d1, d2 = await asyncio.gather(mgr_a.check_pre_trade(_opp()), mgr_b.check_pre_trade(_opp()))

    assert overlap["max_seen"] == 1  # el lock de clase impidió el solape
    assert d1.approved is True and d2.approved is True
