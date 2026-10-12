"""Recuperación aislada por motor, sin instanciar clientes ni enviar órdenes reales."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
from src.monitoring.health import BotState
from src.runner import ProductionRunner
from src.storage.quantities import quantity_fields
from src.utils.config import Settings


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    db = create_engine(f"sqlite:///{tmp_path}/boot.db")
    models.SQLModel.metadata.create_all(db)
    monkeypatch.setattr(models, "_engine", db)
    monkeypatch.setattr(BotState, "is_paused", False)
    monkeypatch.setattr(BotState, "pause_reason", None)
    monkeypatch.setattr("socket.socket.connect", Mock(side_effect=AssertionError("offline")))
    yield
    db.dispose()


def intent(strategy, coid):
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id=coid,
                ticker="T",
                side="yes",
                action="buy",
                price_cents=45,
                strategy=strategy,
                status="pending",
                **quantity_fields(1),
            )
        )
        s.commit()


def ledger():
    with models.get_session() as s:
        return {r.client_order_id: r for r in s.exec(select(models.Trade))}


@pytest.mark.parametrize(
    "strategy,flag",
    [
        ("motor_2_consensus", "MOTOR_2_SPORTSBOOK_ENABLED"),
        ("motor_rest_arb", "MOTOR_REST_ENABLED"),
    ],
)
async def test_boot_m1_off_recovers_only_enabled_motor(strategy, flag):
    settings = Settings.model_construct(
        TRADING_ENABLED=False,
        MOTOR_1_ARBITRAGE_ENABLED=False,
        MOTOR_2_SPORTSBOOK_ENABLED=False,
        MOTOR_REST_ENABLED=False,
    )
    setattr(settings, flag, True)
    for name in ("motor_1_arbitrage", "motor_2_consensus", "motor_rest_arb", "motor_5_mm"):
        intent(name, name)
    client = AsyncMock()
    client.get_orders.return_value = {
        "cursor": "",
        "orders": [
            {
                "client_order_id": strategy,
                "status": "canceled",
                "fill_count_fp": "0.25",
                "fill_count": None,
            },
        ],
    }
    with (
        patch("src.runner.get_settings", return_value=settings),
        patch("src.runner.KalshiRestClient", return_value=client),
        patch("src.runner.ArbitrageExecutor", side_effect=AssertionError("executor forbidden")),
    ):
        client.__aenter__.return_value = client
        await ProductionRunner()._reconcile_on_boot()
    rows = ledger()
    assert rows[strategy].status == "filled" and rows[strategy].filled_count_fp == "0.25"
    assert all(r.status == "pending" for key, r in rows.items() if key != strategy)
    assert not models.kill_switch_engaged()[0]
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


@pytest.mark.parametrize(
    "orders",
    [
        [],
        [
            {"client_order_id": "pending", "status": "executed", "fill_count_fp": None},
        ],
        [
            {"client_order_id": "pending", "status": "resting", "fill_count_fp": "0.25"},
        ],
        [
            {"client_order_id": "pending", "status": "executed", "fill_count_fp": "0.25"},
            {"client_order_id": "pending", "status": "executed", "fill_count_fp": "0.25"},
        ],
    ],
)
async def test_boot_unknown_or_live_order_keeps_risk_and_durable_pause(orders):
    intent("motor_2_consensus", "pending")
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get_orders.return_value = {"cursor": "", "orders": orders}
    settings = Settings.model_construct(
        MOTOR_1_ARBITRAGE_ENABLED=False,
        MOTOR_2_SPORTSBOOK_ENABLED=True,
        MOTOR_REST_ENABLED=False,
    )
    with (
        patch("src.runner.get_settings", return_value=settings),
        patch("src.runner.KalshiRestClient", return_value=client),
    ):
        await ProductionRunner()._reconcile_on_boot()
    assert ledger()["pending"].status == "pending"
    assert BotState.is_paused and models.kill_switch_engaged()[0]
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


async def test_boot_all_motors_disabled_never_constructs_client():
    settings = Settings.model_construct(
        MOTOR_1_ARBITRAGE_ENABLED=False,
        MOTOR_2_SPORTSBOOK_ENABLED=False,
        MOTOR_REST_ENABLED=False,
    )
    with (
        patch("src.runner.get_settings", return_value=settings),
        patch("src.runner.KalshiRestClient", side_effect=AssertionError("client forbidden")),
    ):
        await ProductionRunner()._reconcile_on_boot()
