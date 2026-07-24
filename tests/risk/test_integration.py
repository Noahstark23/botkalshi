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
    RiskManager._daily_stop_alert_date = None  # anti-spam del stop diario soft (estado de clase)
    yield
    BotState.is_paused, BotState.pause_reason = prev
    RiskManager._daily_stop_alert_date = None


def _risk_settings() -> MagicMock:
    s = MagicMock()
    s.ACTIVE_CAPITAL_USD = 4000.0  # bankroll escalado
    s.MAX_DAILY_LOSS_PCT = 3.0  # $120
    s.MAX_WEEKLY_LOSS_PCT = 8.0  # $320
    s.MAX_MONTHLY_LOSS_PCT = 15.0  # $600
    s.MAX_SIMULTANEOUS_EXPOSURE_PCT = 25.0  # $1000
    s.MAX_TRADE_SIZE_PCT = 5.0  # $200
    s.MAX_TRADE_SIZE_USD = 200.0  # cap absoluto anti-slippage
    # Stop-loss a escala chica NEUTRALIZADO por default (los tests dedicados overridean):
    # pisos 0 (solo %) y diario nuclear (comportamiento histórico de estos tests).
    s.MAX_DAILY_LOSS_FLOOR_USD = 0.0
    s.MAX_WEEKLY_LOSS_FLOOR_USD = 0.0
    s.MAX_MONTHLY_LOSS_FLOOR_USD = 0.0
    s.DAILY_STOP_ENTRIES_ONLY = False
    # Rolling + MTM neutralizados (2026-07-17): off = comportamiento histórico.
    s.ROLLING_DRAWDOWN_STOP_ENABLED = False
    s.MAX_ROLLING_DRAWDOWN_PCT = 15.0
    s.MAX_ROLLING_DRAWDOWN_DAYS = 30
    s.MAX_ROLLING_DRAWDOWN_FLOOR_USD = 0.0
    s.UNREALIZED_STOP_ENABLED = False
    s.MAX_UNREALIZED_LOSS_PCT = 10.0
    s.MAX_UNREALIZED_LOSS_FLOOR_USD = 0.0
    s.UNREALIZED_MARK_TTL_SEC = 900.0
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


# =====================================================
# check_and_reserve (deuda auditoría 2026-07-01): check + intent bajo el MISMO lock
# =====================================================


def _persist_intent_row(count: int, *, price: int = 50, coid: str | None = None) -> bool:
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id=coid or str(uuid.uuid4()),
                ticker="KXRSV",
                side="yes",
                action="buy",
                count=count,
                price_cents=price,
                strategy="motor_2_consensus",
                status="pending",
            )
        )
        s.commit()
    return True


def _single_leg_opp(count: int, *, price: int = 50) -> ArbOpportunity:
    leg = ArbLeg(
        market_ticker="KXRSV", side="yes", price_cents=price, count=count, available_size=count
    )
    return ArbOpportunity(
        legs=(leg,),
        count=count,
        gross_profit_cents=0,
        fees_cents=0,
        net_profit_cents=0,
        edge_pct=5.0,
    )


@pytest.mark.asyncio
async def test_concurrent_reserves_cannot_approve_against_same_exposure():
    """Dos reservas CONCURRENTES: la segunda ya ve el intent de la primera (la fila se
    escribe antes de soltar el lock). Antes, con el persist fuera del lock, ambas podían
    aprobarse contra la misma exposición leída → overshoot de hasta 2× el cap por trade."""
    # Exposición previa $800 → remaining $200 del cap de 25% ($1000).
    _insert_trade(strategy="motor_2_consensus", status="filled", price=50, count=1600)
    rm = _manager()

    async def _reserve():
        with patch("src.risk.manager.get_settings", return_value=_risk_settings()):
            return await rm.check_and_reserve(
                _single_leg_opp(400), lambda d: _persist_intent_row(d.max_allowed_count)
            )

    first, second = await asyncio.gather(_reserve(), _reserve())

    assert first.approved and second.approved
    # La primera toma casi todo el remaining; la segunda queda con las migas — NUNCA
    # el mismo tamaño (eso sería la carrera vieja: dos aprobaciones por el mismo cupo).
    counts = sorted((first.max_allowed_count, second.max_allowed_count), reverse=True)
    assert counts[1] < counts[0]
    # El total comprometido (previo + ambos intents, a 50c + fee/unit) respeta el cap de $1000.
    total_usd = 800 + sum(c * 50 for c in counts) / 100
    assert total_usd <= 1000.0


@pytest.mark.asyncio
async def test_reserve_persist_failure_degrades_to_rejection():
    """Si el intent no se puede escribir, la decisión aprobada se degrada a rechazo
    (el caller no debe operar sin rastro para el RiskManager)."""
    rm = _manager()
    with patch("src.risk.manager.get_settings", return_value=_risk_settings()):
        decision = await rm.check_and_reserve(_single_leg_opp(10), lambda d: False)
    assert not decision.approved and decision.reason == "persist_intent_failed"


# =====================================================
# Netting multi-outcome (auditoría rentabilidad 2026-07-07): el winner-take-all
# completo es hedge (payout 100c/set) — antes contaba su notional BRUTO por días
# =====================================================


def _insert_multi_leg(
    arb: str, ticker: str, price: int, count: int = 10, status: str = "filled"
) -> None:
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id=str(uuid.uuid4()),
                ticker=ticker,
                side="yes",
                action="buy",
                count=count,
                price_cents=price,
                strategy="motor_rest_arb",
                status=status,
                notes=f"arb_id={arb}",
            )
        )
        s.commit()


def test_multi_outcome_complete_set_discounted_to_zero():
    """1X2 completo (3 patas YES del mismo evento, Σ=96c<100, todas filled) →
    payout garantizado → exposición direccional CERO."""
    arb = str(uuid.uuid4())
    for tk, p in (("KXWC-EV-A", 31), ("KXWC-EV-B", 32), ("KXWC-EV-C", 33)):
        _insert_multi_leg(arb, tk, p)
    assert _manager()._get_current_exposure_usd() == 0.0


def test_multi_outcome_incomplete_set_counts_fully():
    """CONTROL fail-safe: una pata del arb_id quedó cancelled (KILL) → el set es
    INCOMPLETO (mixto FILL+KILL = direccional real) → cero descuento."""
    arb = str(uuid.uuid4())
    _insert_multi_leg(arb, "KXWC-EV-A", 31)
    _insert_multi_leg(arb, "KXWC-EV-B", 32)
    _insert_multi_leg(arb, "KXWC-EV-C", 33)
    _insert_multi_leg(arb, "KXWC-EV-D", 5, status="cancelled")  # la pata que KILLeó
    assert _manager()._get_current_exposure_usd() == pytest.approx(9.60)  # (31+32+33)×10/100


def test_multi_outcome_two_legs_not_discounted():
    """CONTROL de certeza: 2 patas all-yes de un evento NO son un set completo
    verificable (cobertura parcial posible) → cuentan enteras."""
    arb = str(uuid.uuid4())
    _insert_multi_leg(arb, "KXWC-EV-A", 31)
    _insert_multi_leg(arb, "KXWC-EV-B", 32)
    assert _manager()._get_current_exposure_usd() == pytest.approx(6.30)


def test_multi_outcome_cross_event_not_discounted():
    """CONTROL: 3 patas all-yes pero de EVENTOS distintos → no es un set del mismo
    partido → cuentan enteras."""
    arb = str(uuid.uuid4())
    _insert_multi_leg(arb, "KXWC-EV1-A", 31)
    _insert_multi_leg(arb, "KXWC-EV2-B", 32)
    _insert_multi_leg(arb, "KXWC-EV3-C", 33)
    assert _manager()._get_current_exposure_usd() == pytest.approx(9.60)


# =====================================================
# Stop-loss a escala chica (2026-07-12): pisos USD + breach diario auto-recuperable
# =====================================================


def _small_capital_settings(*, entries_only: bool = True) -> MagicMock:
    """Settings a escala REAL del incidente: capital $180, pisos 20/40/60."""
    s = _risk_settings()
    s.ACTIVE_CAPITAL_USD = 180.0  # sin balance cacheado, el fallback ES el capital
    s.MAX_DAILY_LOSS_FLOOR_USD = 20.0
    s.MAX_WEEKLY_LOSS_FLOOR_USD = 40.0
    s.MAX_MONTHLY_LOSS_FLOOR_USD = 60.0
    s.DAILY_STOP_ENTRIES_ONLY = entries_only
    return s


def _small_manager(*, entries_only: bool = True) -> RiskManager:
    with patch(
        "src.risk.manager.get_settings",
        return_value=_small_capital_settings(entries_only=entries_only),
    ):
        return RiskManager()


@pytest.mark.asyncio
async def test_floor_usd_absorbs_noise_losses_small_capital():
    """CONTROL del piso: con $180, -$10 rompía el límite % puro ($5.40) y apagaba todo.
    Con piso $20 el límite efectivo es max(5.40, 20)=$20 → -$10 NO dispara y se opera."""
    _insert_trade(strategy="motor_2_consensus", status="settled", pnl=-1000, settled=True)
    mgr = _small_manager()

    with patch("src.risk.manager.alert_risk_event", new=AsyncMock()):
        decision = await mgr.check_pre_trade(_opp(count=1))

    assert decision.approved is True  # el ruido de $10 ya no apaga el bot
    assert BotState.is_paused is False
    assert models.kill_switch_engaged()[0] is False


@pytest.mark.asyncio
async def test_daily_breach_soft_rejects_entries_without_kill_switch():
    """MECANISMO escalonado: breach diario REAL (-$21 ≥ piso $20) → rechaza entradas
    nuevas PERO sin kill-switch persistente ni is_paused (auto-recupera en el rollover).
    El aviso es one-shot por día (anti-spam) y deja RiskEvent de auditoría."""
    _insert_trade(strategy="motor_2_consensus", status="settled", pnl=-2100, settled=True)
    mgr = _small_manager()

    with patch("src.risk.manager.alert_risk_event", new=AsyncMock()) as mock_alert:
        d1 = await mgr.check_pre_trade(_opp(count=1))
        d2 = await mgr.check_pre_trade(_opp(count=1))  # segundo intento el mismo día

    assert d1.approved is False and "Diario" in d1.reason
    assert d2.approved is False
    assert BotState.is_paused is False  # NO nuclear: el resto del bot sigue
    assert models.kill_switch_engaged()[0] is False  # NO persistente: mañana se recupera solo
    assert mock_alert.await_count == 1  # one-shot: 2 checks, 1 aviso
    from sqlmodel import select

    with models.get_session() as s:
        events = list(s.exec(select(models.RiskEvent)).all())
    assert [e.event_type for e in events] == ["daily_stop"]  # auditoría, sin kill_switch


@pytest.mark.asyncio
async def test_weekly_breach_still_engages_persistent_kill_switch():
    """CONTROL nuclear: una pérdida a escala SEMANAL (≥ piso $40) sigue latcheando el
    kill-switch persistente AUNQUE el diario soft también esté roto — el orden
    mensual→semanal→diario garantiza que lo severo no quede tapado por lo soft."""
    _insert_trade(strategy="motor_2_consensus", status="settled", pnl=-4500, settled=True)
    mgr = _small_manager()

    with patch("src.risk.manager.alert_risk_event", new=AsyncMock()):
        decision = await mgr.check_pre_trade(_opp(count=1))

    assert decision.approved is False
    assert "Semanal" in decision.reason  # la ventana severa gana, no la diaria
    assert BotState.is_paused is True
    assert models.kill_switch_engaged()[0] is True  # nuclear intacto


@pytest.mark.asyncio
async def test_daily_stop_legacy_mode_still_nuclear():
    """CONTROL legacy: con DAILY_STOP_ENTRIES_ONLY=false, el breach diario vuelve a
    latchear el kill-switch persistente (comportamiento histórico intacto por flag)."""
    _insert_trade(strategy="motor_2_consensus", status="settled", pnl=-2100, settled=True)
    mgr = _small_manager(entries_only=False)

    with patch("src.risk.manager.alert_risk_event", new=AsyncMock()):
        decision = await mgr.check_pre_trade(_opp(count=1))

    assert decision.approved is False and "Diario" in decision.reason
    assert models.kill_switch_engaged()[0] is True
