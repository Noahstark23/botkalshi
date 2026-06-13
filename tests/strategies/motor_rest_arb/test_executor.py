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
    yes = ArbLeg(
        market_ticker="KXWC-26-ARG", side="yes", price_cents=40, count=count, available_size=100
    )
    no = ArbLeg(
        market_ticker="KXWC-26-ARG", side="no", price_cents=45, count=count, available_size=100
    )
    return ArbOpportunity(
        legs=(yes, no),
        count=count,
        gross_profit_cents=75,
        fees_cents=4,
        net_profit_cents=71,
        edge_pct=2.0,
    )


# Shape real de Kalshi CreateOrder (V2): fill_count + remaining_count (no 'status').
# FILL completo = fill_count >= count y remaining_count == 0. _opp() usa count=5.
def _filled_resp(count: int = 5, coid: str | None = None) -> dict:
    return {
        "order": {
            "order_id": "OID",
            "fill_count": count,
            "remaining_count": 0,
            "client_order_id": coid or "",
        }
    }


def _killed_resp(count: int = 5) -> dict:
    return {"order": {"order_id": "OID2", "fill_count": 0, "remaining_count": count}}


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
    """Pata con excepción de red (ERROR_RED) → reconcilia con AMBAS fuentes coincidentes."""
    client = AsyncMock()
    # yes FILL, no lanza excepción (ERROR_RED).
    client.place_order = AsyncMock(side_effect=[_filled_resp(), ConnectionError("net down")])
    # Reconciliación de 'no': fuente 1 (get_orders) vacía + fuente 2 (get_positions) sin
    # posición → ambas coinciden en NO-llena. La pata 'no' no se rollbackea.
    client.get_orders = AsyncMock(return_value={"orders": []})
    client.get_positions = AsyncMock(return_value={"market_positions": []})
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.reconciled is True
    assert LegState.ERROR_RED in out.leg_states
    # La pata yes (FILL) sí queda expuesta → rollback de esa.
    assert out.rollback_triggered is True
    client.get_orders.assert_awaited()  # fuente 1
    client.get_positions.assert_awaited()  # fuente 2 (cruce independiente)


@pytest.mark.asyncio
async def test_error_red_sources_discrepancy_assumes_exposed():
    """ERROR_RED: get_orders dice NO-llena pero get_positions ve posición → exposición (rollback)."""
    client = AsyncMock()
    # Ambas patas ERROR_RED para forzar reconciliación de ambas.
    client.place_order = AsyncMock(
        side_effect=[ConnectionError("x"), ConnectionError("y"), _filled_resp(), _filled_resp()]
    )  # rollbacks
    client.get_orders = AsyncMock(return_value={"orders": []})  # fuente 1: no llena
    client.get_positions = AsyncMock(
        return_value={"market_positions": [{"ticker": "KXWC-26-ARG", "position": 5}]}
    )  # fuente 2: SÍ posición → discrepancia
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.reconciled is True
    # Discrepancia entre fuentes → se asume exposición → rollback.
    assert out.rollback_triggered is True


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
    client.place_order = AsyncMock(
        side_effect=[
            _filled_resp(),
            _killed_resp(),
            _killed_resp(),
            _killed_resp(),
            _killed_resp(),  # 3 reintentos de rollback
        ]
    )
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
    client.place_order = AsyncMock(
        side_effect=[
            _filled_resp(),
            _killed_resp(),
            _filled_resp(),  # exec 1
            _filled_resp(),
            _killed_resp(),
            _filled_resp(),  # exec 2
            _filled_resp(),
            _killed_resp(),
            _filled_resp(),  # exec 3
        ]
    )

    for _ in range(3):
        await ex.execute(_opp())

    assert ex.is_paused is True

    # Nueva orden tras el breaker → rechazada sin tocar el cliente.
    client.place_order.reset_mock()
    out = await ex.execute(_opp())
    assert out.rejected_paused is True
    assert client.place_order.await_count == 0


def test_create_order_filled_shape_and_conservative_fail():
    """El sensor de fill: shape real de Kalshi + fallo conservador hacia KILL."""
    leg = ArbLeg(market_ticker="T", side="yes", price_cents=40, count=5, available_size=100)
    f = RestExecutor._create_order_filled
    # FILL completo: fill_count >= count y remaining 0.
    assert f({"fill_count": 5, "remaining_count": 0}, leg) is True
    # KILL: nada llenado.
    assert f({"fill_count": 0, "remaining_count": 5}, leg) is False
    # Parcial (no debería pasar con FOK, pero por las dudas): no es fill completo.
    assert f({"fill_count": 3, "remaining_count": 2}, leg) is False
    # Fixed-point strings (variante de shape).
    assert f({"fill_count_fp": "5", "remaining_count_fp": "0"}, leg) is True
    # FALLO CONSERVADOR: sin campos de fill → KILL (no FILL).
    assert f({}, leg) is False
    assert f({"status": "executed"}, leg) is False  # campo de OTRO endpoint → no se confía


def test_get_orders_filled_status():
    """Reconciliación: GetOrders usa status=='executed'."""
    leg = ArbLeg(market_ticker="T", side="yes", price_cents=40, count=5, available_size=100)
    g = RestExecutor._get_orders_filled
    assert g({"status": "executed"}, leg) is True
    assert g({"status": "resting"}, leg) is False
    assert g({"status": "canceled"}, leg) is False
    assert g({}, leg) is False  # conservador


@pytest.mark.asyncio
async def test_fok_kill_409_read_as_kill_not_error_red():
    """HTTP 409 + code de FOK-sin-volumen → KILL determinístico (NO ERROR_RED, no reconcilia)."""
    from src.clients.kalshi_rest import KalshiClientError

    client = AsyncMock()
    kill_409 = KalshiClientError(
        409, "Client error", "", error_code="fill_or_kill_insufficient_resting_volume"
    )
    # Ambas patas devuelven el 409-KILL → ambas KILL → cero exposición, sin reconciliar.
    client.place_order = AsyncMock(side_effect=[kill_409, kill_409])
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.leg_states == [LegState.KILL, LegState.KILL]
    assert out.filled is False
    assert out.rollback_triggered is False
    assert out.reconciled is False  # KILL determinístico: nada que reconciliar
    client.get_orders.assert_not_called()  # no se consultó nada
    client.get_positions.assert_not_called()


@pytest.mark.asyncio
async def test_other_409_is_error_red_not_kill():
    """Un 409 con OTRO error code (no FOK) → ERROR_RED (se reconcilia), NUNCA KILL."""
    from src.clients.kalshi_rest import KalshiClientError

    client = AsyncMock()
    other_409 = KalshiClientError(409, "Client error", "", error_code="market_closed")
    # yes FILL, no → 409 de otra causa → ERROR_RED → reconcilia.
    client.place_order = AsyncMock(side_effect=[_filled_resp(), other_409, _filled_resp()])
    client.get_orders = AsyncMock(return_value={"orders": []})
    client.get_positions = AsyncMock(return_value={"market_positions": []})
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert LegState.ERROR_RED in out.leg_states  # NO se trató como KILL
    assert out.reconciled is True  # se reconcilió (es lo correcto para 'desconocido')


@pytest.mark.asyncio
async def test_fill_plus_kill409_rolls_back_immediately_no_reconcile():
    """
    EL CASO PELIGROSO (a nivel máquina de estados, no solo sensor):
    pata 1 FILL (200) + pata 2 KILL-409 determinístico → resultado FILL/KILL →
    rollback INMEDIATO de la pata 1, SIN reconciliación de la pata 2.

    Si el KILL-409 se tratara como ERROR_RED, el executor reconciliaría la pata 2
    (get_orders/get_positions) antes de rollbackear la pata 1 expuesta — demora que
    cuesta plata mientras el mercado se mueve. Este test garantiza que NO pasa.
    """
    from src.clients.kalshi_rest import KalshiClientError

    client = AsyncMock()
    kill_409 = KalshiClientError(
        409, "Client error", "", error_code="fill_or_kill_insufficient_resting_volume"
    )
    # pata 1 (yes) FILL, pata 2 (no) KILL-409; luego el rollback (sell) de la pata 1 llena.
    client.place_order = AsyncMock(side_effect=[_filled_resp(), kill_409, _filled_resp()])
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.leg_states == [LegState.FILL, LegState.KILL]
    assert out.rollback_triggered is True  # rollback de la pata 1 (FILL)
    assert out.rollback_filled is True
    assert out.reconciled is False  # ← clave: NO se reconcilió la pata 2
    client.get_orders.assert_not_called()  # KILL-409 es determinístico, no se consulta
    client.get_positions.assert_not_called()
    # El rollback fue un sell a 1¢ de la pata YES (la única llena).
    sell_call = client.place_order.await_args_list[2]
    assert sell_call.kwargs["action"] == "sell"
    assert sell_call.kwargs["yes_price"] == 1
