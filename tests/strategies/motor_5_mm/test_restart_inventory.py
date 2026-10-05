"""Reinicio con fills durables; clientes falsos y SQLite temporal, sin red."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import select

from src.storage.models import MMShadowFill, Trade, get_session
from src.strategies.motor_5_mm.engine import Motor5Engine
from src.strategies.motor_5_mm.incidents import incident_status
from src.strategies.motor_5_mm.reconciler import MMReconciler, MMReconcileReport


def _trade(*, count=10, filled_count=6, status="filled", action="buy"):
    return Trade(
        client_order_id="m5mm-restart",
        kalshi_order_id="restart-order",
        ticker="KXMLBGAME-E1-YES",
        side="yes",
        action=action,
        count=count,
        filled_count=filled_count,
        price_cents=47,
        strategy="motor_5_mm",
        status=status,
    )


def test_execution_and_shadow_have_distinct_cohorts():
    shadow = Motor5Engine(experiment_label="same-label")
    execution = Motor5Engine(experiment_label="same-label", trading_enabled=True)
    assert shadow._experiment_id != execution._experiment_id
    # La identidad de cada modo sigue siendo determinística entre procesos.
    assert shadow._experiment_id == Motor5Engine(experiment_label="same-label")._experiment_id
    assert (
        execution._experiment_id
        == Motor5Engine(experiment_label="same-label", trading_enabled=True)._experiment_id
    )


@pytest.mark.asyncio
async def test_execution_start_never_rehydrates_shadow_inventory_or_markouts():
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    engine = Motor5Engine(trading_enabled=True, client_factory=Client)
    with get_session() as session:
        # Incluso un registro histórico con el mismo id no es evidencia de fill real.
        session.add(
            MMShadowFill(
                ticker="KXMLBGAME-E1-YES",
                side="buy",
                price_cents=47,
                count=9,
                rule="strict_cross",
                inventory_after=9,
                metric_version=engine.F1_METRIC_VERSION,
                experiment_id=engine._experiment_id,
            )
        )
        session.add(_trade())
        session.commit()

    class Executor:
        async def cancel_all(self, _):
            return 0

        def forget_confirmed(self, _):
            pass

        def live_coids(self):
            return set()

    reconciler = AsyncMock()
    reconciler.reconcile.return_value = MMReconcileReport(complete=True)
    stop = asyncio.Event()

    async def observe_first_tick():
        engine._apply_settled_fills()
        assert engine._inventory.net("KXMLBGAME-E1-YES") == 6
        assert engine._markouts_pendientes == []
        stop.set()

    engine._tick = observe_first_tick
    with (
        patch("src.strategies.motor_5_mm.engine.Motor5Executor", return_value=Executor()),
        patch("src.strategies.motor_5_mm.engine.MMReconciler", return_value=reconciler),
    ):
        await engine.run(stop)
    with get_session() as session:
        shadow_fill = session.exec(select(MMShadowFill)).one()
        assert shadow_fill.markout1_cents is None
        assert shadow_fill.markout2_cents is None


@pytest.mark.parametrize("action, expected", [("buy", 6), ("sell", -6)])
def test_new_engine_recovers_only_confirmed_quantity_once(action, expected):
    with get_session() as session:
        session.add(_trade(action=action))
        session.commit()
    first = Motor5Engine(trading_enabled=True)
    assert first._apply_settled_fills() == 1
    assert first._inventory.net("KXMLBGAME-E1-YES") == expected
    restarted = Motor5Engine(trading_enabled=True)
    assert restarted._apply_settled_fills() == 1
    assert restarted._apply_settled_fills() == 0
    assert restarted._inventory.net("KXMLBGAME-E1-YES") == expected


@pytest.mark.asyncio
async def test_resting_partial_then_cancel_race_recovers_actual_fill_after_restart():
    class Client:
        async def get_all_orders(self, **_):
            return [
                {
                    "order_id": "restart-order",
                    "client_order_id": "m5mm-restart",
                    "ticker": "KXMLBGAME-E1-YES",
                    "side": "yes",
                    "action": "buy",
                    "status": "resting",
                    "fill_count_fp": "2.00",
                }
            ]

        async def cancel_order(self, _):
            # Otro contrato se llenó mientras la cancelación estaba en tránsito.
            return {
                "order": {
                    "order_id": "restart-order",
                    "client_order_id": "m5mm-restart",
                    "status": "canceled",
                    "fill_count_fp": "3.00",
                    "remaining_count_fp": "0.00",
                    "initial_count_fp": "10.00",
                }
            }

    with get_session() as session:
        session.add(_trade(status="pending", filled_count=0))
        session.commit()
    report = await MMReconciler(Client()).reconcile(live_coids={"m5mm-restart"})
    assert not report.complete  # El fill resting pausa; nunca se cotiza con cero ficticio.
    assert incident_status()[0]
    with get_session() as session:
        row = session.exec(select(Trade)).one()
        assert row.status == "pending" and row.filled_count == 2

    report = await MMReconciler(Client()).reconcile(live_coids=set())
    assert report.complete
    assert report.terminal_coids == {"m5mm-restart"}
    restarted = Motor5Engine(trading_enabled=True)
    assert restarted._apply_settled_fills() == 1
    assert restarted._apply_settled_fills() == 0
    assert restarted._inventory.net("KXMLBGAME-E1-YES") == 3
    # Una conciliación posterior no autoriza a borrar el incidente anterior.
    assert incident_status()[0]
