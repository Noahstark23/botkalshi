"""Cada punto de reserva usa SQLite de prueba y órdenes/observaciones ficticias."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import SQLModel, create_engine, select

import src.storage.models as models
from src.control.channel import ControlError, Intent
from src.control.permissions import (
    Admission,
    DurablePermissionAdapter,
    GateFacts,
    PermissionBinding,
)
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor
from src.strategies.motor_2_consensus.executor import Motor2Executor
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.strategies.motor_rest_arb.executor import RestExecutor

NOW = 1791900000000


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    yield engine
    engine.dispose()


def adapter(engine, scope, enabled):
    binding = PermissionBinding(
        11, 22, "demo", "a" * 40, "b" * 32, "c" * 64, frozenset({scope}), frozenset({scope})
    )
    result = DurablePermissionAdapter(
        engine, binding, lambda *_: GateFacts(NOW, True, True, 0, 0, 0), clock=lambda: NOW
    )
    result.provision()
    if enabled:
        result.apply_if_current(
            Intent(
                "d" * 32,
                11,
                22,
                "demo",
                "a" * 40,
                "b" * 32,
                "c" * 64,
                scope,
                True,
                0,
                NOW,
                NOW + 60000,
            )
        )
    return result


def opportunity():
    return ArbOpportunity(
        (ArbLeg("KXTEST", "yes", 30, 1, 10), ArbLeg("KXTEST", "no", 30, 1, 10)), 1, 40, 4, 36, 36.0
    )


def assert_rows(scope, expected):
    with models.get_session() as s:
        trades = s.exec(select(models.Trade)).all()
        audit = s.exec(select(Admission)).all()
        assert len(trades) == len(audit) == expected
        assert {t.client_order_id for t in trades} == {a.client_order_id for a in audit}
        assert all(a.scope == scope and a.permission_revision == 1 for a in audit)
        assert all(t.status == "pending" for t in trades)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("motor", ["m1", "m2", "rest"])
def test_legacy_reservation_hooks(ledger, motor, enabled):
    scope = f"{motor}.entries"
    permission = adapter(ledger, scope, enabled)
    client, risk = MagicMock(), MagicMock()
    risk.multi_profile_enabled.return_value = False
    opp = opportunity()
    if motor == "m1":
        executor = ArbitrageExecutor(client, risk, permissions=permission)
        if enabled:
            assert executor._persist_intents(opp, 1, ["a", "b"])
        else:
            with pytest.raises(ControlError, match="PERMISSION_DISABLED"):
                executor._persist_intents(opp, 1, ["a", "b"])
    elif motor == "rest":
        executor = RestExecutor(client, permissions=permission)
        assert (
            executor._persist_intents("arb", opp, dict(zip(opp.legs, ["a", "b"], strict=True)))
            is enabled
        )
    else:
        executor = Motor2Executor(client, risk, permissions=permission)
        signal = SimpleNamespace(
            event_key="KX", market_ticker="KXTEST", edge_pct=4, odds_api_fair_prob=0.7
        )
        assert executor._persist_intent(signal, "yes", 30, 1, "a") is enabled
    client.place_order.assert_not_called()
    assert_rows(scope, (1 if motor == "m2" else 2) if enabled else 0)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("action", ["buy", "sell"])
async def test_m5_quote_both_sides_require_permission(ledger, monkeypatch, enabled, action):
    permission = adapter(ledger, "m5.quotes", enabled)
    client, risk = MagicMock(), MagicMock()
    client.place_order = AsyncMock(side_effect=TimeoutError("offline lost ACK"))
    risk.multi_profile_enabled.return_value = False

    async def reserve(_trade, persist, **_kwargs):
        return SimpleNamespace(approved=persist(), reason="test reservation")

    risk.check_quote_and_reserve = reserve
    monkeypatch.setattr("src.strategies.motor_5_mm.executor.incident_status", lambda: (False, None))
    executor = Motor5Executor(client, risk, permissions=permission)
    await executor._place("KXTEST", action, 30, 1)
    assert client.place_order.await_count == int(enabled)
    assert_rows("m5.quotes", int(enabled))
