"""Reinicio, cuarentena durable y reconocimiento manual usando SQLite temporal."""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.strategies.motor_5_mm.incidents as incidents
from src.risk.manager import RiskManager
from src.storage.models import (
    OperationalState,
    Trade,
    get_session,
    mm_quotes_paused,
    set_mm_quotes_paused,
)
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.engine import Motor5Engine
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.strategies.motor_5_mm.incidents import (
    ACKNOWLEDGEMENT,
    INCIDENT_KEY,
    SESSION_KEY,
    acknowledge_incident,
    begin_execution_session,
    finish_execution_session,
    incident_status,
    latch_incident,
)
from src.strategies.motor_5_mm.order_state import COUNT_QUARANTINE_NOTE
from src.strategies.motor_5_mm.reconciler import MMReconciler


def _row(key):
    with get_session() as session:
        return session.get(OperationalState, key)


def _trade(status="pending", notes=None):
    with get_session() as session:
        session.add(
            Trade(
                client_order_id="m5mm-persisted",
                ticker="T",
                side="yes",
                action="buy",
                count=1,
                price_cents=45,
                strategy="motor_5_mm",
                status=status,
                notes=notes,
            )
        )
        session.commit()


def test_unclean_session_becomes_incident_on_restart_and_never_auto_clears():
    first = begin_execution_session()
    assert _row(SESSION_KEY).value == "active"
    second = begin_execution_session()
    assert incident_status()[0]
    with pytest.raises(RuntimeError, match="no posee"):
        finish_execution_session(first)
    assert incidents._current_execution_owner == second
    finish_execution_session(second)
    assert _row(SESSION_KEY).value == "clean"
    assert incident_status()[0]
    third = begin_execution_session()
    finish_execution_session(third)
    assert incident_status()[0]


def test_session_close_refuses_unresolved_pending():
    owner = begin_execution_session()
    _trade()
    with pytest.raises(RuntimeError, match="pending"):
        finish_execution_session(owner)
    assert _row(SESSION_KEY).value == "active"


def test_incident_retains_first_reason_and_is_bounded():
    latch_incident("first " * 200)
    latch_incident("second")
    assert incident_status() == (True, ("first " * 200)[:500])
    assert _row(INCIDENT_KEY).value == "blocked"


def test_unreadable_state_blocks_placement_and_write_errors_propagate():
    with patch("src.strategies.motor_5_mm.incidents.get_session", side_effect=OSError("disk")):
        assert incident_status()[0]
        with pytest.raises(OSError):
            latch_incident("failure")
        with pytest.raises(OSError):
            begin_execution_session()


def test_unknown_incident_value_is_not_treated_as_clear():
    with get_session() as session:
        session.add(OperationalState(key=INCIDENT_KEY, value="malformed"))
        session.commit()
    assert incident_status()[0]


class _Client:
    def __init__(self, orders=None):
        self.orders = orders or []
        self.place_order = AsyncMock()
        self.cancel_order = AsyncMock(
            return_value={
                "order": {
                    "order_id": "PHANTOM",
                    "client_order_id": "m5mm-phantom",
                    "status": "canceled",
                    "fill_count_fp": "0.00",
                }
            }
        )

    async def get_all_orders(self, **_):
        return self.orders


@pytest.mark.asyncio
async def test_phantom_incident_survives_new_engine_and_empty_api():
    phantom = {
        "client_order_id": "m5mm-phantom",
        "order_id": "PHANTOM",
        "ticker": "T",
        "status": "resting",
        "fill_count_fp": "0.00",
    }
    first_client = _Client([phantom])
    report = await MMReconciler(first_client).reconcile(live_coids=set())
    assert report.phantom_cancelled == 1
    assert incident_status()[0]
    client = _Client()
    engine = Motor5Engine(trading_enabled=True)
    engine._executor = Motor5Executor(client, risk=MagicMock())
    engine._reconciler = MMReconciler(client)
    engine._book_top = AsyncMock(return_value=(40, 60, Decimal(10), Decimal(10)))
    FairValueBook.publish({"T": 0.5})
    await engine._tick()
    assert client.place_order.await_count == 0
    assert incident_status()[0]


@pytest.mark.asyncio
async def test_transient_snapshot_error_blocks_tick_without_latching_incident():
    client = _Client()
    client.get_all_orders = AsyncMock(side_effect=TimeoutError())
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert not report.complete
    assert incident_status() == (False, None)


def test_manual_acknowledgement_requires_literal_and_review_reference():
    latch_incident("review me")
    for ref, confirmation in [("", ACKNOWLEDGEMENT), ("review-123", "CLEAR")]:
        with pytest.raises(ValueError):
            acknowledge_incident(review_reference=ref, confirmation=confirmation)
    assert incident_status()[0]


def test_manual_acknowledgement_cannot_clear_active_owner():
    begin_execution_session()
    latch_incident("review me")
    with pytest.raises(RuntimeError, match="sesión M5 no cerrada"):
        acknowledge_incident(review_reference="review-123", confirmation=ACKNOWLEDGEMENT)
    assert incident_status()[0]


@pytest.mark.parametrize(
    "status, notes", [("pending", None), ("filled", None), ("cancelled", COUNT_QUARANTINE_NOTE)]
)
def test_manual_acknowledgement_refuses_unresolved_ledger(status, notes):
    _trade(status, notes)
    latch_incident("review me")
    with pytest.raises(RuntimeError, match="exposición o intenciones"):
        acknowledge_incident(review_reference="review-123", confirmation=ACKNOWLEDGEMENT)
    assert incident_status()[0]


def test_manual_acknowledgement_preserves_external_pause():
    set_mm_quotes_paused(True, "external pause")
    owner = begin_execution_session()
    finish_execution_session(owner)
    latch_incident("review me")
    acknowledge_incident(review_reference="review-123", confirmation=ACKNOWLEDGEMENT)
    assert incident_status() == (False, None)
    assert mm_quotes_paused() == (True, "external pause")
    assert _row(INCIDENT_KEY).reason == "revisión manual: review-123"


def test_prior_process_active_session_blocks_other_motors_without_m5_boot(monkeypatch):
    owner = begin_execution_session()
    assert _row(INCIDENT_KEY) is None
    # Simula el proceso nuevo: M5 está deshabilitado y no llama begin nuevamente.
    monkeypatch.setattr(incidents, "_current_execution_owner", None, raising=False)
    blocked, reason = incident_status()
    assert blocked and "sesión" in reason
    decision = RiskManager._shared_incident_block()
    assert decision is not None and not decision.approved
    assert _row(SESSION_KEY).value == "active"
    assert _row(SESSION_KEY).reason == owner
    assert _row(INCIDENT_KEY) is None  # consulta read-only, ningún auto-clear/write


def test_current_process_healthy_session_and_clean_session_allow_gate():
    owner = begin_execution_session()
    assert incident_status() == (False, None)
    assert RiskManager._shared_incident_block() is None
    finish_execution_session(owner)
    assert incidents._current_execution_owner is None
    assert incident_status() == (False, None)


def test_failed_incident_write_invalidates_runtime_ownership():
    begin_execution_session()
    with (
        patch("src.strategies.motor_5_mm.incidents.get_session", side_effect=OSError("disk")),
        pytest.raises(OSError),
    ):
        latch_incident("fatal with unavailable DB")
    assert _row(INCIDENT_KEY) is None
    # Recuperarse la DB no convierte la sesión que falló en una sesión sana.
    assert incident_status()[0]
    assert RiskManager._shared_incident_block() is not None


def test_unknown_session_state_blocks_without_an_incident():
    with get_session() as session:
        session.add(OperationalState(key=SESSION_KEY, value="malformed"))
        session.commit()
    assert incident_status()[0]


def test_failed_begin_commit_does_not_register_a_healthy_owner(monkeypatch):
    monkeypatch.setattr(incidents, "_current_execution_owner", None)
    with patch("sqlmodel.Session.commit", side_effect=OSError("disk")), pytest.raises(OSError):
        begin_execution_session()
    assert incidents._current_execution_owner is None
    assert _row(SESSION_KEY) is None
