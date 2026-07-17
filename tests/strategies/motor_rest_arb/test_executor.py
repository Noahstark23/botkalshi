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
    """Hard (no) FILL, barata (yes) KILL → rollback de la pata NO expuesta, que se llena."""
    client = AsyncMock()
    # Secuenciado: hard=no FILL primero, barata=yes KILL; luego el rollback (sell no) llena.
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
    # El rollback fue un sell a 1¢ de la pata NO (la hard, la única llena).
    sell_call = client.place_order.await_args_list[2]
    assert sell_call.kwargs["action"] == "sell"
    assert sell_call.kwargs["no_price"] == 1


@pytest.mark.asyncio
async def test_error_red_cheap_reconciles_filled_rolls_back_both():
    """Hard (no) FILL + barata (yes) ERROR_RED que reconcilia a LLENA → rollback de ambas."""
    client = AsyncMock()
    # Secuenciado: hard=no FILL, barata=yes lanza excepción (ERROR_RED); 2 rollbacks llenan.
    client.place_order = AsyncMock(
        side_effect=[_filled_resp(), ConnectionError("net down"), _filled_resp(), _filled_resp()]
    )
    # Reconciliación de 'yes': fuente 1 (get_orders executed) + fuente 2 (posición) → LLENA.
    client.get_orders = AsyncMock(
        return_value={"orders": [{"status": "executed", "client_order_id": ""}]}
    )
    client.get_positions = AsyncMock(
        return_value={"market_positions": [{"ticker": "KXWC-26-ARG", "position": 5}]}
    )
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.reconciled is True
    assert LegState.ERROR_RED in out.leg_states
    # Ambas expuestas (no FILL + yes reconciliada llena) → rollback de ambas.
    assert out.rollback_triggered is True
    client.get_orders.assert_awaited()  # fuente 1
    client.get_positions.assert_awaited()  # fuente 2 (cruce independiente)


@pytest.mark.asyncio
async def test_error_red_sources_discrepancy_assumes_exposed():
    """ERROR_RED: get_orders dice NO-llena pero get_positions ve posición → exposición (rollback)."""
    client = AsyncMock()
    # Secuenciado: hard=no FILL, barata=yes ERROR_RED (la que se reconcilia); 2 rollbacks.
    client.place_order = AsyncMock(
        side_effect=[_filled_resp(), ConnectionError("y"), _filled_resp(), _filled_resp()]
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
    """Hard ERROR_RED + reconciliación que falla → fail-safe a exposición (NO se asume KILL)."""
    client = AsyncMock()
    # Secuenciado: hard=no ERROR_RED → la barata NUNCA se envía; solo se reconcilia la hard.
    # 1ra: hard ERROR_RED; luego el rollback (sell) se intenta.
    client.place_order = AsyncMock(
        side_effect=[ConnectionError("x"), _filled_resp(), _filled_resp(), _filled_resp()]
    )
    # get_orders también falla → fail-safe: tratar la hard como expuesta.
    client.get_orders = AsyncMock(side_effect=RuntimeError("still down"))
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.reconciled is True
    # Solo se envió la hard → un único estado en leg_states.
    assert out.leg_states == [LegState.ERROR_RED]
    # fail-safe: la hard se trata como expuesta → rollback intentado.
    assert out.rollback_triggered is True


@pytest.mark.asyncio
async def test_rollback_not_filled_fires_kill_switch():
    """Rollback que no se llena tras reintentos → kill-switch (pausa + alerta)."""
    client = AsyncMock()
    # Secuenciado: hard=no FILL, barata=yes KILL; rollback sell NUNCA llena (3 intentos).
    client.place_order = AsyncMock(
        side_effect=[
            _filled_resp(),  # hard=no FILL
            _killed_resp(),  # barata=yes KILL
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
    # Cada execute secuenciado: hard=no FILL + barata=yes KILL + rollback-fill (3 place_order).
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


@pytest.mark.asyncio
async def test_has_open_position_reads_position_fp():
    """
    _has_open_position (2da fuente del reconcile real-money) debe leer `position_fp`
    (fixed-point string de Kalshi, ej. "-1.00"), no `position` (que puede no venir).
    Antes leía `position` → siempre None → siempre "sin posición" → degradaba el
    cross-check de doble fuente a una sola.
    """
    client = AsyncMock()
    client.get_positions = AsyncMock(
        return_value={"market_positions": [{"ticker": "KXWC-26-ARG", "position_fp": "-1.00"}]}
    )
    ex = RestExecutor(client)
    assert await ex._has_open_position("KXWC-26-ARG") is True

    # position_fp="0.00" → sin posición abierta.
    client.get_positions = AsyncMock(
        return_value={"market_positions": [{"ticker": "KXWC-26-ARG", "position_fp": "0.00"}]}
    )
    assert await ex._has_open_position("KXWC-26-ARG") is False

    # Fallback: `position` plano (int) sigue funcionando.
    client.get_positions = AsyncMock(
        return_value={"market_positions": [{"ticker": "KXWC-26-ARG", "position": 5}]}
    )
    assert await ex._has_open_position("KXWC-26-ARG") is True


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
    """HTTP 409 + code de FOK-sin-volumen en la HARD → KILL determinístico, barata no se envía."""
    from src.clients.kalshi_rest import KalshiClientError

    client = AsyncMock()
    kill_409 = KalshiClientError(
        409, "Client error", "", error_code="fill_or_kill_insufficient_resting_volume"
    )
    # Secuenciado: la hard=no devuelve el 409-KILL → la barata NUNCA se envía → no-op.
    client.place_order = AsyncMock(side_effect=[kill_409])
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    # Ambas patas terminan no-llenas (KILL): la hard por el 409, la barata por no enviarse.
    assert out.leg_states == [LegState.KILL, LegState.KILL]
    assert out.filled is False
    assert out.rollback_triggered is False
    assert out.reconciled is False  # KILL determinístico: nada que reconciliar
    assert client.place_order.await_count == 1  # SOLO la hard se envió
    client.get_orders.assert_not_called()  # no se consultó nada
    client.get_positions.assert_not_called()


@pytest.mark.asyncio
async def test_other_409_is_error_red_not_kill():
    """Un 409 con OTRO error code (no FOK) → ERROR_RED (se reconcilia), NUNCA KILL."""
    from src.clients.kalshi_rest import KalshiClientError

    client = AsyncMock()
    other_409 = KalshiClientError(409, "Client error", "", error_code="market_closed")
    # Secuenciado: la hard=no → 409 de otra causa → ERROR_RED → reconcilia (barata no se envía).
    client.place_order = AsyncMock(side_effect=[other_409])
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
    # Secuenciado: hard=no FILL, barata=yes KILL-409; el rollback (sell) de la pata NO llena.
    client.place_order = AsyncMock(side_effect=[_filled_resp(), kill_409, _filled_resp()])
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert out.leg_states == [LegState.FILL, LegState.KILL]
    assert out.rollback_triggered is True  # rollback de la pata hard (FILL)
    assert out.rollback_filled is True
    assert out.reconciled is False  # ← clave: NO se reconcilió la barata KILL-409
    client.get_orders.assert_not_called()  # KILL-409 es determinístico, no se consulta
    client.get_positions.assert_not_called()
    # El rollback fue un sell a 1¢ de la pata NO (la hard, la única llena).
    sell_call = client.place_order.await_args_list[2]
    assert sell_call.kwargs["action"] == "sell"
    assert sell_call.kwargs["no_price"] == 1


@pytest.mark.asyncio
async def test_hard_leg_kill_sends_no_cheap_orders():
    """
    EL CORAZÓN DE LA FASE 3b: si la pata HARD (no, 45c) KILL-ea, NO se envía ninguna
    pata barata → cero exposición → no-op (en vez de comprar barato y rollbackear con
    pérdida garantizada). Esta es la prueba de que el guardrail elimina la pérdida.
    """
    from src.clients.kalshi_rest import KalshiClientError

    client = AsyncMock()
    kill_409 = KalshiClientError(
        409, "Client error", "", error_code="fill_or_kill_insufficient_resting_volume"
    )
    client.place_order = AsyncMock(side_effect=[kill_409])
    ex = RestExecutor(client)

    out = await ex.execute(_opp())

    assert client.place_order.await_count == 1  # SOLO la hard se envió
    assert out.filled is False
    assert out.rollback_triggered is False  # nada que rollbackear: no se compró nada
    assert out.reconciled is False
    # No hubo NINGÚN sell (no hay exposición que cerrar).
    assert all(c.kwargs.get("action") != "sell" for c in client.place_order.await_args_list)


@pytest.mark.asyncio
async def test_hard_leg_is_highest_price_fired_first():
    """La pata HARD = la de mayor precio; se dispara PRIMERO y si KILL-ea, el resto no sale."""
    from src.clients.kalshi_rest import KalshiClientError

    legs = (
        ArbLeg(market_ticker="EV-AUT", side="yes", price_cents=80, count=5, available_size=100),
        ArbLeg(market_ticker="EV-TIE", side="yes", price_cents=15, count=5, available_size=100),
        ArbLeg(market_ticker="EV-JOR", side="yes", price_cents=3, count=5, available_size=100),
    )
    opp = ArbOpportunity(
        legs=legs, count=5, gross_profit_cents=20, fees_cents=4, net_profit_cents=16, edge_pct=1.0
    )
    client = AsyncMock()
    kill_409 = KalshiClientError(
        409, "Client error", "", error_code="fill_or_kill_insufficient_resting_volume"
    )
    client.place_order = AsyncMock(side_effect=[kill_409])
    ex = RestExecutor(client)

    out = await ex.execute(opp)

    # La PRIMERA (y única) orden fue la pata cara (80c).
    assert client.place_order.await_count == 1
    first_call = client.place_order.await_args_list[0]
    assert first_call.kwargs["ticker"] == "EV-AUT"
    assert first_call.kwargs["yes_price"] == 80
    # Hard KILL → las otras dos NUNCA se envían.
    assert out.filled is False
    assert out.rollback_triggered is False
