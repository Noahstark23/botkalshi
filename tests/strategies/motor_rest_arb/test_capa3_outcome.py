"""
Tests de la Capa 3 del cable: alert_trade (Telegram) + _update_edge_window_outcome.

Cubren:
  - alert_trade: una campana por evento — fill suena, kill-switch NO re-suena (ya
    alertó el executor), rollback/circuit-breaker suenan urgent, ambas-KILL silencio.
  - _update_edge_window_outcome: pobla la fila real (SQLite tmp) con leg_states/
    reconciled/kill_switch/rollback/latencia; best-effort (fallo no propaga; edge_id
    None no-op). rest_rtt_ms queda en default (no medible desde el engine).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session

import src.storage.models as models
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.monitoring.telegram_alerts import alert_trade
from src.strategies.motor_rest_arb.engine import RestArbEngine
from src.strategies.motor_rest_arb.executor import ExecutionOutcome, LegState
from src.strategies.motor_rest_arb.trigger import TriggerSignal


def _opp() -> ArbOpportunity:
    return ArbOpportunity(
        legs=(
            ArbLeg(market_ticker="KXTEST", side="yes", price_cents=55, count=10, available_size=80),
            ArbLeg(market_ticker="KXTEST", side="no", price_cents=40, count=10, available_size=100),
        ),
        count=10,
        gross_profit_cents=50,
        fees_cents=10,
        net_profit_cents=40,
        edge_pct=4.2,
    )


# =====================================================
# alert_trade — una campana por evento
# =====================================================


@pytest.mark.asyncio
async def test_alert_trade_filled_sends_normal_message():
    """Fill → mensaje normal (no urgent). El que queremos oír en demo."""
    outcome = ExecutionOutcome(filled=True, leg_states=[LegState.FILL, LegState.FILL])
    with patch("src.monitoring.telegram_alerts.send_alert", new=AsyncMock()) as mock_send:
        await alert_trade(outcome, _opp())

    mock_send.assert_awaited_once()
    msg = mock_send.call_args.args[0]
    assert "Arb ejecutado" in msg and "KXTEST" in msg and "40c" in msg
    assert mock_send.call_args.kwargs.get("urgent", False) is False


@pytest.mark.asyncio
async def test_alert_trade_kill_switch_does_not_realert():
    """Kill-switch → SIN alerta desde alert_trade (el executor YA mandó alert_error)."""
    outcome = ExecutionOutcome(
        filled=False,
        leg_states=[LegState.FILL, LegState.ERROR_RED],
        rollback_triggered=True,
        rollback_filled=False,
        kill_switch_fired=True,  # gana sobre rollback: una sola campana (la del executor)
    )
    with patch("src.monitoring.telegram_alerts.send_alert", new=AsyncMock()) as mock_send:
        await alert_trade(outcome, _opp())

    mock_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_trade_rollback_sends_urgent():
    """Rollback (sin kill-switch) → alerta urgent con leg_states y si recuperó."""
    outcome = ExecutionOutcome(
        filled=False,
        leg_states=[LegState.FILL, LegState.KILL],
        rollback_triggered=True,
        rollback_filled=True,
    )
    with patch("src.monitoring.telegram_alerts.send_alert", new=AsyncMock()) as mock_send:
        await alert_trade(outcome, _opp())

    mock_send.assert_awaited_once()
    assert mock_send.call_args.kwargs.get("urgent") is True
    assert "FILL/KILL" in mock_send.call_args.args[0]


@pytest.mark.asyncio
async def test_alert_trade_circuit_breaker_paused_sends_urgent():
    """rejected_paused → urgent (el executor solo loguea CRITICAL; esta es su campana)."""
    outcome = ExecutionOutcome(filled=False, rejected_paused=True)
    with patch("src.monitoring.telegram_alerts.send_alert", new=AsyncMock()) as mock_send:
        await alert_trade(outcome, _opp())

    mock_send.assert_awaited_once()
    assert mock_send.call_args.kwargs.get("urgent") is True


@pytest.mark.asyncio
async def test_alert_trade_both_kill_is_silent():
    """Ambas patas KILL (sin fill/rollback/kill-switch) → SIN mensaje (no spamea)."""
    outcome = ExecutionOutcome(filled=False, leg_states=[LegState.KILL, LegState.KILL])
    with patch("src.monitoring.telegram_alerts.send_alert", new=AsyncMock()) as mock_send:
        await alert_trade(outcome, _opp())

    mock_send.assert_not_awaited()


# =====================================================
# _update_edge_window_outcome — registro best-effort
# =====================================================


@pytest.fixture
def sqlite_engine(tmp_path):
    """Engine SQLite tmp como singleton de models (get_session del engine lo usa)."""
    db = tmp_path / "test_capa3.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


def _rest_engine() -> RestArbEngine:
    settings = MagicMock()
    settings.MOTOR_REST_MIN_EDGE_CENTS = 1
    settings.MOTOR_REST_MIN_DEPTH = 2
    with patch("src.strategies.motor_rest_arb.engine.get_settings", return_value=settings):
        return RestArbEngine()


def _signal() -> TriggerSignal:
    return TriggerSignal(
        market_ticker="KXTEST",
        opportunity=_opp(),
        net_edge_cents=40,
        gross_spread_cents=50,
        limiting_depth=10,
    )


def test_update_populates_edge_window_row(sqlite_engine):
    """La fila grabada en la detección se pobla con el outcome real + latencia."""
    eng = _rest_engine()
    edge_id = eng._record_edge_window(_signal())
    assert edge_id is not None  # _record_edge_window devuelve el id (Capa 2)

    outcome = ExecutionOutcome(
        filled=False,
        leg_states=[LegState.FILL, LegState.KILL],
        rollback_triggered=True,
        rollback_filled=True,
        reconciled=True,
        kill_switch_fired=False,
    )
    eng._update_edge_window_outcome(edge_id, outcome, cycle_latency_ms=237)

    with Session(sqlite_engine) as s:
        row = s.get(models.EdgeWindow, edge_id)
        assert row is not None
        assert row.leg_states == "FILL/KILL"
        assert row.reconciled is True
        assert row.kill_switch_fired is False
        assert row.rollback_filled is True
        assert row.cycle_latency_ms == 237
        assert row.rest_rtt_ms is None  # default — no medible desde el engine
        assert row.magnitude_cents == 40  # la detección original no se pisa


def test_update_with_none_edge_id_is_noop(sqlite_engine):
    """edge_id None (la detección no se grabó) → no-op, sin excepción."""
    eng = _rest_engine()
    outcome = ExecutionOutcome(filled=True, leg_states=[LegState.FILL, LegState.FILL])
    eng._update_edge_window_outcome(None, outcome, cycle_latency_ms=10)  # no debe lanzar


def test_update_failure_never_raises(sqlite_engine):
    """Fallo de DB en el update → tragado y logueado; JAMÁS propaga al flujo."""
    eng = _rest_engine()
    outcome = ExecutionOutcome(filled=True, leg_states=[LegState.FILL, LegState.FILL])
    with patch(
        "src.strategies.motor_rest_arb.engine.get_session",
        side_effect=RuntimeError("db down"),
    ):
        eng._update_edge_window_outcome(1, outcome, cycle_latency_ms=10)  # no debe lanzar
