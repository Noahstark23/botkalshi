"""
Tests del RestExecutor (Motor REST) — máquina de 4 estados + rollback + circuit breaker.

Mocks rigurosos sobre KalshiRestClient (AsyncMock). Escenarios:
  - Happy path: doble FILL.
  - Fallo asimétrico (FILL + KILL) → rollback exitoso.
  - ERROR_RED (excepción de red) → reconcilia con get_orders, NO asume KILL.
  - Rollback que no llena → kill-switch (pausa + alerta).
  - Circuit breaker: 3 rollbacks/1h → pausa, rechaza nuevas órdenes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.strategies.motor_rest_arb.executor import LegState, RestExecutor


def _opp(count: int = 5) -> ArbOpportunity:
    yes = ArbLeg(market_ticker="KXWC-26-ARG", side="yes", price_cents=40, count=count, available_size=100)
    no = ArbLeg(market_ticker="KXWC-26-ARG", side="no", price_cents=45, count=count, available_size=100)
    return ArbOpportunity(
        legs=(yes, no), count=count, gross_profit_cents=75, fees_cents=4,
        net_profit_cents=71, edge_pct=2.0,
    )


def _filled_resp(coid: str | None = None) -> dict:
    return {"order": {"order_id": "OID", "status": "filled", "client_order_id": coid or ""}}


def _killed_resp() -> dict:
    return {"order": {"order_id": "OID2", "status": "canceled"}}


@pytest.mark.asyncio
async def test_happy_path_double_fill():
    """Ambas patas FILL → arb capturado, sin rollback."""
    client = AsyncMock()
    client.place_order = AsyncMock(return_value=_filled_resp())
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.filled is True
    assert out.leg_states == [LegState.FILL, LegState.FILL]
    assert out.rollback_triggered is False
    assert client.place_order.await_count == 2  # solo las 2 patas, sin rollback


@pytest.mark.asyncio
async def test_asymmetric_fill_triggers_rollback():
    """Una pata FILL, otra KILL → rollback de la pata expuesta, que se llena."""
    client = AsyncMock()
    # 1ra pata (yes) FILL, 2da (no) KILL; luego el rollback (sell) se llena.
    client.place_order = AsyncMock(side_effect=[_filled_resp(), _killed_resp(), _filled_resp()])
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.filled is False
    assert LegState.FILL in out.leg_states and LegState.KILL in out.leg_states
    assert out.rollback_triggered is True
    assert out.rollback_filled is True
    assert out.kill_switch_fired is False
    # 2 patas + 1 rollback sell.
    assert client.place_order.await_count == 3
    # El rollback fue un sell a 1¢.
    sell_call = client.place_order.await_args_list[2]
    assert sell_call.kwargs["action"] == "sell"
    assert sell_call.kwargs["yes_price"] == 1


@pytest.mark.asyncio
async def test_error_red_reconciles_not_filled():
    """Pata con excepción de red (ERROR_RED) → reconcilia; si no estaba llena, no rollback."""
    client = AsyncMock()
    # yes FILL, no lanza excepción (ERROR_RED).
    client.place_order = AsyncMock(side_effect=[_filled_resp(), ConnectionError("net down")])
    # Reconciliación: la orden 'no' NO aparece llena → no estaba ejecutada.
    client.get_orders = AsyncMock(return_value={"orders": []})
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.reconciled is True
    assert LegState.ERROR_RED in out.leg_states
    # Solo la pata yes (FILL) queda expuesta → rollback de esa.
    assert out.rollback_triggered is True  # yes quedó llena
    client.get_orders.assert_awaited()  # se consultó el estado real


@pytest.mark.asyncio
async def test_error_red_never_assumed_as_kill():
    """ERROR_RED + reconciliación que falla → fail-safe a exposición (NO se asume KILL)."""
    client = AsyncMock()
    # Ambas patas ERROR_RED.
    client.place_order = AsyncMock(side_effect=[ConnectionError("x"), ConnectionError("y")])
    # get_orders también falla → fail-safe: tratar como expuesto.
    client.get_orders = AsyncMock(side_effect=RuntimeError("still down"))
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.reconciled is True
    assert out.leg_states == [LegState.ERROR_RED, LegState.ERROR_RED]
    # fail-safe: ambas se tratan como expuestas → rollback intentado.
    assert out.rollback_triggered is True


@pytest.mark.asyncio
async def test_rollback_not_filled_fires_kill_switch():
    """Rollback que no se llena tras reintentos → kill-switch (pausa + alerta)."""
    client = AsyncMock()
    # yes FILL, no KILL; rollback sell NUNCA llena (3 intentos canceled).
    client.place_order = AsyncMock(side_effect=[
        _filled_resp(), _killed_resp(),
        _killed_resp(), _killed_resp(), _killed_resp(),  # 3 reintentos de rollback
    ])
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.rollback_triggered is True
    assert out.rollback_filled is False
    assert out.kill_switch_fired is True
    assert ex.is_paused is True  # el motor queda pausado


@pytest.mark.asyncio
async def test_circuit_breaker_after_3_rollbacks():
    """3 rollbacks en la ventana → circuit breaker pausa y rechaza nuevas órdenes."""
    ex = RestExecutor(AsyncMock())

    # Forzar 3 rollbacks exitosos (sin kill-switch) para tripear el breaker.
    client = ex.client
    # Cada execute: FILL + KILL + rollback-fill (3 place_order).
    client.place_order = AsyncMock(side_effect=[
        _filled_resp(), _killed_resp(), _filled_resp(),  # exec 1
        _filled_resp(), _killed_resp(), _filled_resp(),  # exec 2
        _filled_resp(), _killed_resp(), _filled_resp(),  # exec 3
    ])

    for _ in range(3):
        await ex.execute(_opp())

    assert ex.is_paused is True

    # Nueva orden tras el breaker → rechazada sin tocar el cliente.
    client.place_order.reset_mock()
    out = await ex.execute(_opp())
    assert out.rejected_paused is True
    assert client.place_order.await_count == 0
