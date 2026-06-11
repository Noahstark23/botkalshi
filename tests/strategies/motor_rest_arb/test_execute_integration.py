"""
Harness de integración del camino de ejecución del Motor REST (item 4 del sprint).

Corre `RestExecutor.execute()` COMPLETO contra un fake del API de Kalshi que devuelve
los SHAPES REALES ya validados en demo, cubriendo las 5 rutas de la máquina de estados:

  1. FILL/FILL          — ambas patas llenan (arb capturado, cero exposición)
  2. KILL/KILL          — 409 fill_or_kill_insufficient_resting_volume en ambas
  3. FILL + rollback OK  — una llena, la otra KILL → rollback que SÍ cierra
  4. FILL + rollback FAIL — rollback no llena → kill-switch (pausa + alerta)
  5. ERROR_RED + reconcile — excepción de red → reconcilia (get_orders+get_positions)

POR QUÉ AsyncMock-sobre-cliente y no respx: respx exigiría sumar una dependencia
(viola "solo tests/"). El parsing HTTP→excepción del cliente (ej. 409→error_code) ya
está cubierto en tests/clients/test_kalshi_rest_429.py. Acá el fake devuelve los dicts
REALES y se ejercita el parsing del EXECUTOR (_create_order_filled lee fill_count_fp,
_get_orders_filled lee status=="executed", _has_open_position lee market_positions).

═══════════════════════════════════════════════════════════════════════════════════
PUNTO DE EXTENSIÓN PARA A.1 (persistencia de Trade rows + pausa persistente)
───────────────────────────────────────────────────────────────────────────────────
Cuando A.1 exista, NO hay que reescribir estas rutas: ya corren execute() de punta a
punta. Solo se agrega, en cada test, donde dice `# === A.1 EXTENSION POINT ===`:
  - un fixture de DB temporal (patrón de test_capa3_outcome.sqlite_engine),
  - asserts sobre las filas Trade (status por pata según la ruta), y
  - en la ruta 4, assert de la pausa persistente (models.kill_switch_engaged()).
El `HarnessClient` ya registra cada place_order con su client_order_id (.calls) para
verificar el arb_id-prefijo y el mapeo coid→Trade.
═══════════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.clients.kalshi_rest import KalshiClientError
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.strategies.motor_rest_arb.executor import LegState, RestExecutor

_FOK_KILL_CODE = "fill_or_kill_insufficient_resting_volume"


def _opp(count: int = 5) -> ArbOpportunity:
    yes = ArbLeg(market_ticker="KXWC-26-ARG", side="yes", price_cents=40, count=count, available_size=100)
    no = ArbLeg(market_ticker="KXWC-26-ARG", side="no", price_cents=45, count=count, available_size=100)
    return ArbOpportunity(
        legs=(yes, no), count=count, gross_profit_cents=75, fees_cents=4,
        net_profit_cents=71, edge_pct=2.0,
    )


# ── Shapes REALES de Kalshi (validados en demo, ver checklist_activacion_capital) ──

def _create_order_fill(count: int = 5) -> dict:
    """CreateOrder FILL completo: fill_count_fp / remaining_count_fp como strings fp."""
    return {"order": {"order_id": "OID", "fill_count_fp": f"{count}.00", "remaining_count_fp": "0.00"}}


def _create_order_nofill(count: int = 5) -> dict:
    """CreateOrder que no llenó (FOK cancelado): fill 0, remaining = count."""
    return {"order": {"order_id": "OID", "fill_count_fp": "0.00", "remaining_count_fp": f"{count}.00"}}


def _kill_409() -> KalshiClientError:
    """KILL real: HTTP 409 + error.code de FOK sin volumen (shape validado en demo)."""
    body = f'{{"error":{{"code":"{_FOK_KILL_CODE}"}}}}'
    return KalshiClientError(409, "Client error", body, _FOK_KILL_CODE)


class HarnessClient:
    """
    Fake del KalshiRestClient: devuelve shapes reales programables por ruta y graba
    cada llamada (con client_order_id) para que A.1 verifique las Trade rows.

    Config por test:
      buy[side]      → dict de CreateOrder a devolver, o Exception a lanzar.
      sell_result    → dict que devuelven los place_order de rollback (sell).
      reconcile_status   → status que GetOrders reporta para las patas (reconcile).
      reconcile_position → contratos netos que get_positions reporta por ticker.
    """

    def __init__(self) -> None:
        self.buy: dict[str, Any] = {}
        self.sell_result: dict | None = None
        self.reconcile_status: str = "executed"
        self.reconcile_position: int = 0
        self.calls: list[tuple[str, dict]] = []

    async def place_order(self, **kw: Any) -> dict:
        self.calls.append(("place_order", kw))  # grabado ANTES de un posible raise
        if kw["action"] == "buy":
            resp = self.buy[kw["side"]]
            if isinstance(resp, BaseException):
                raise resp
            return resp
        # sell = rollback
        return self.sell_result or _create_order_nofill(kw["count"])

    async def get_orders(self, *, ticker: str | None = None, **kw: Any) -> dict:
        self.calls.append(("get_orders", {"ticker": ticker, **kw}))
        # Reconstruye órdenes desde los buys grabados de ese ticker → el client_order_id
        # coincide con el que execute() acuñó internamente (no hace falta conocerlo).
        orders = [
            {"client_order_id": c["client_order_id"], "status": self.reconcile_status, "order_id": "OID"}
            for m, c in self.calls
            if m == "place_order" and c.get("action") == "buy" and c.get("ticker") == ticker
        ]
        return {"orders": orders}

    async def get_positions(self, **kw: Any) -> dict:
        self.calls.append(("get_positions", kw))
        pos = []
        if self.reconcile_position:
            pos = [{"ticker": "KXWC-26-ARG", "position": self.reconcile_position}]
        return {"market_positions": pos}

    # Helpers de aserción
    def buys(self) -> list[dict]:
        return [c for m, c in self.calls if m == "place_order" and c.get("action") == "buy"]

    def sells(self) -> list[dict]:
        return [c for m, c in self.calls if m == "place_order" and c.get("action") == "sell"]


# =====================================================
# Las 5 rutas
# =====================================================


@pytest.mark.asyncio
async def test_route_fill_fill():
    """Ruta 1: ambas patas FILL → arb capturado, sin rollback, sin reconcile."""
    client = HarnessClient()
    client.buy = {"yes": _create_order_fill(), "no": _create_order_fill()}
    outcome = await RestExecutor(client).execute(_opp())

    assert outcome.filled is True
    assert outcome.leg_states == [LegState.FILL, LegState.FILL]
    assert outcome.rollback_triggered is False
    assert len(client.buys()) == 2 and len(client.sells()) == 0
    # === A.1 EXTENSION POINT === ambas Trade rows → status="filled" (exposición).


@pytest.mark.asyncio
async def test_route_kill_kill():
    """Ruta 2: 409 FOK en ambas → KILL/KILL, cero exposición, sin rollback."""
    client = HarnessClient()
    client.buy = {"yes": _kill_409(), "no": _kill_409()}
    outcome = await RestExecutor(client).execute(_opp())

    assert outcome.filled is False
    assert outcome.leg_states == [LegState.KILL, LegState.KILL]
    assert outcome.rollback_triggered is False
    assert outcome.reconciled is False
    assert len(client.sells()) == 0
    # === A.1 EXTENSION POINT === ambas Trade rows → status="cancelled" (audit trail).


@pytest.mark.asyncio
async def test_route_fill_plus_rollback_ok():
    """Ruta 3: yes FILL + no KILL(409) → rollback que SÍ cierra la pata expuesta."""
    client = HarnessClient()
    client.buy = {"yes": _create_order_fill(), "no": _kill_409()}
    client.sell_result = _create_order_fill()  # el sell de rollback llena
    outcome = await RestExecutor(client).execute(_opp())

    assert outcome.filled is False
    assert outcome.leg_states == [LegState.FILL, LegState.KILL]
    assert outcome.rollback_triggered is True
    assert outcome.rollback_filled is True
    assert outcome.kill_switch_fired is False
    assert len(client.sells()) == 1  # se vendió la pata yes expuesta
    # === A.1 EXTENSION POINT === yes → settled, pnl_cents<0 (pérdida realizada); no → cancelled.


@pytest.mark.asyncio
async def test_route_fill_plus_rollback_fails_kill_switch():
    """Ruta 4: rollback NO llena en 3 intentos → kill-switch (pausa + alerta)."""
    client = HarnessClient()
    client.buy = {"yes": _create_order_fill(), "no": _kill_409()}
    client.sell_result = _create_order_nofill()  # el rollback nunca llena
    executor = RestExecutor(client)
    with patch("src.monitoring.telegram_alerts.alert_error", new=AsyncMock()) as mock_alert:
        outcome = await executor.execute(_opp())

    assert outcome.rollback_triggered is True
    assert outcome.rollback_filled is False
    assert outcome.kill_switch_fired is True
    assert executor.is_paused is True                       # circuit/kill-switch pausó
    assert len(client.sells()) == executor.ROLLBACK_MAX_RETRIES  # reintentó el máximo
    mock_alert.assert_awaited()                             # sonó la alerta de kill-switch
    # === A.1 EXTENSION POINT === yes → filled + notes="exposed_killswitch" (el 25% la ve);
    #                             y models.kill_switch_engaged() == (True, ...) (pausa persistente A.3).


@pytest.mark.asyncio
async def test_route_error_red_reconciles_to_filled():
    """Ruta 5: yes ERROR_RED (excepción de red) → reconcilia (get_orders+get_positions)."""
    client = HarnessClient()
    client.buy = {"yes": RuntimeError("network down"), "no": _create_order_fill()}
    client.reconcile_status = "executed"   # get_orders dice que la pata SÍ llenó
    client.reconcile_position = 5          # get_positions confirma posición abierta
    client.sell_result = _create_order_fill()  # el rollback de las patas expuestas cierra
    outcome = await RestExecutor(client).execute(_opp())

    assert outcome.leg_states[0] == LegState.ERROR_RED
    assert outcome.leg_states[1] == LegState.FILL
    assert outcome.reconciled is True
    # Doble fuente consultada para resolver el estado desconocido:
    assert any(m == "get_orders" for m, _ in client.calls)
    assert any(m == "get_positions" for m, _ in client.calls)
    assert outcome.rollback_triggered is True  # ambas expuestas (yes reconciliada llena + no FILL)
    # === A.1 EXTENSION POINT === las patas reconciliadas-llenas → settled/cancelled según rollback.


@pytest.mark.asyncio
async def test_route_error_red_reconciles_to_not_filled():
    """Ruta 5b: ERROR_RED pero ambas fuentes coinciden en NO-llena → sin rollback."""
    client = HarnessClient()
    client.buy = {"yes": RuntimeError("timeout"), "no": _kill_409()}
    client.reconcile_status = "canceled"   # get_orders: no ejecutó
    client.reconcile_position = 0          # get_positions: sin posición
    outcome = await RestExecutor(client).execute(_opp())

    assert outcome.leg_states[0] == LegState.ERROR_RED
    assert outcome.reconciled is True
    assert outcome.rollback_triggered is False  # nada expuesto → no rollback
    assert len(client.sells()) == 0
    # === A.1 EXTENSION POINT === yes → cancelled notes="error_red_reconciled".
