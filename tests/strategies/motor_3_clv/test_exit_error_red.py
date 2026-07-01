"""
Fill no registrado en el exit path (fix auditoría 2026-07-01, bloqueante #2 Motor 3).

El bug: el SELL del exit no dejaba NINGÚN rastro si la respuesta se perdía (ERROR_RED)
ni si la DB fallaba después de un fill real (_record_exit/_settle_originals eran
best-effort sin kill-switch). Las patas BUY quedaban 'filled' → el tick siguiente
re-vendía contratos que ya no existen (en V2 el segundo sell no se rechaza: abre
posición contraria) y el SettlementPoller settleaba por resolución → PnL fantasma.

Verifica el patrón intent pre-red + reconcile (el que ya usan los otros executors):
fila SELL 'pending' ANTES de tocar la red; ERROR_RED la deja pending; el próximo
exit_position RECONCILIA por client_order_id contra get_orders ANTES de vender
(executed → settlea originals sin re-vender; canceled → libera; irresoluble → no vende);
fallo de DB tras fill real → kill-switch preventivo persistente.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import select

import src.storage.models as models
from src.storage.models import PortfolioPosition, Trade, get_session, kill_switch_engaged
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

TICKER = "KXRED"


def _pos(count: int = 10) -> PortfolioPosition:
    return PortfolioPosition(ticker=TICKER, side="yes", count=count)


def _seed_buy(count: int = 10) -> None:
    with get_session() as s:
        s.add(
            Trade(
                client_order_id=f"buy-{TICKER}-{count}",
                ticker=TICKER,
                side="yes",
                action="buy",
                count=count,
                price_cents=45,
                fill_price_cents=45,
                strategy="motor_2_consensus",
                status="filled",
            )
        )
        s.commit()


def _client(*, place=None, place_exc=None, orders=None) -> MagicMock:
    c = MagicMock()
    c.get_orderbook = AsyncMock(return_value={"orderbook": {"yes": [["0.60", "100"]], "no": []}})
    if place_exc is not None:
        c.place_order = AsyncMock(side_effect=place_exc)
    else:
        c.place_order = AsyncMock(
            return_value=place or {"order": {"order_id": "o", "fill_count": 10}}
        )
    c.get_orders = AsyncMock(return_value={"orders": orders or []})
    return c


def _sells() -> list[Trade]:
    with get_session() as s:
        return list(s.exec(select(Trade).where(Trade.action == "sell")))


def _buys() -> list[Trade]:
    with get_session() as s:
        return list(s.exec(select(Trade).where(Trade.action == "buy")))


def _ex(client) -> Motor3ExitExecutor:
    return Motor3ExitExecutor(client, entry_origin=("motor_2_consensus",))


# =====================================================
# Intent PRE-red
# =====================================================


@pytest.mark.asyncio
async def test_intent_row_written_before_network_and_error_red_leaves_pending():
    """ERROR_RED (excepción de red en place_order) → la fila SELL queda 'pending'
    (rastro para reconciliar), no desaparece como antes."""
    _seed_buy()
    ex = _ex(_client(place_exc=RuntimeError("red muerta post-envío")))

    out = await ex.exit_position(_pos())

    assert out.reason == "error_red"
    sells = _sells()
    assert len(sells) == 1 and sells[0].status == "pending"
    assert sells[0].client_order_id == out.client_order_id


@pytest.mark.asyncio
async def test_persist_intent_failure_aborts_without_selling(monkeypatch):
    """Si la fila intent no se puede escribir, NO se vende (fail-safe pre-red)."""
    _seed_buy()
    client = _client()
    ex = _ex(client)
    monkeypatch.setattr(ex, "_persist_sell_intent", lambda *a, **k: False)

    out = await ex.exit_position(_pos())

    assert not out.placed and out.reason == "persist_intent_failed"
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_clean_fill_transitions_intent_to_settled():
    """Happy path: la fila intent pasa pending→settled (una sola fila SELL, sin duplicar)."""
    _seed_buy()
    ex = _ex(_client())

    out = await ex.exit_position(_pos())

    assert out.filled
    sells = _sells()
    assert len(sells) == 1 and sells[0].status == "settled"
    assert all(b.status == "settled" and b.closed_by_clv for b in _buys())


@pytest.mark.asyncio
async def test_ioc_no_fill_cancels_intent():
    _seed_buy()
    ex = _ex(_client(place={"order": {"order_id": "o", "fill_count": 0}}))

    out = await ex.exit_position(_pos())

    assert out.reason == "ioc_no_fill"
    sells = _sells()
    assert len(sells) == 1 and sells[0].status == "cancelled"


# =====================================================
# Reconcile de intents pendientes ANTES de vender
# =====================================================


async def _leave_pending_intent(fill_after: int | None = None) -> str:
    """Genera un intent pending real vía ERROR_RED y devuelve su coid."""
    ex = _ex(_client(place_exc=RuntimeError("boom")))
    out = await ex.exit_position(_pos())
    assert out.reason == "error_red"
    return out.client_order_id


@pytest.mark.asyncio
async def test_reconcile_executed_settles_without_reselling():
    """El sell del error_red SÍ llenó: el próximo exit NO re-vende (evita abrir posición
    contraria) — settlea las BUY con el fill reconciliado y cierra el intent."""
    _seed_buy(count=10)
    coid = await _leave_pending_intent()

    client = _client(orders=[{"client_order_id": coid, "status": "executed", "fill_count": 10}])
    ex = _ex(client)
    out = await ex.exit_position(_pos())

    client.place_order.assert_not_called()  # NADA de segundo sell
    assert out.reason == "already_closed"  # tras reconciliar, no queda nada abierto
    assert all(b.status == "settled" and b.closed_by_clv for b in _buys())
    sells = _sells()
    assert len(sells) == 1 and sells[0].status == "settled"


@pytest.mark.asyncio
async def test_reconcile_canceled_releases_and_sells_again():
    """El sell del error_red NO llenó (canceled): el intent se libera y el exit procede."""
    _seed_buy(count=10)
    coid = await _leave_pending_intent()

    client = _client(orders=[{"client_order_id": coid, "status": "canceled", "fill_count": 0}])
    ex = _ex(client)
    out = await ex.exit_position(_pos())

    assert out.filled  # la venta nueva procedió
    statuses = sorted(t.status for t in _sells())
    assert statuses == ["cancelled", "settled"]  # intent viejo liberado + venta nueva


@pytest.mark.asyncio
async def test_unresolvable_pending_intent_blocks_selling():
    """Intent pendiente que no se puede resolver (no aparece en get_orders / API caída)
    → NO se vende este tick (ante la duda, ninguna orden nueva)."""
    _seed_buy(count=10)
    await _leave_pending_intent()

    client = _client(orders=[])  # la orden no aparece
    ex = _ex(client)
    out = await ex.exit_position(_pos())

    client.place_order.assert_not_called()
    assert not out.placed and out.reason == "pending_sell_unresolved"


# =====================================================
# Fallo de DB tras fill REAL → kill-switch preventivo
# =====================================================


@pytest.mark.asyncio
async def test_db_failure_after_real_fill_engages_preventive_pause(monkeypatch):
    """SELL llenó pero _settle_originals no pudo persistir → pausa preventiva persistente
    (patrón de los otros executors: un fill real sin registrar = sistema ciego)."""
    _seed_buy()
    ex = _ex(_client())

    def _boom(*a, **k):
        raise RuntimeError("db caída post-fill")

    monkeypatch.setattr(ex, "_settle_originals", _boom)
    from src.monitoring.health import BotState

    BotState.is_paused = False
    try:
        out = await ex.exit_position(_pos())
        assert out.filled  # el fill real ocurrió
        engaged, _ = kill_switch_engaged()
        assert engaged and BotState.is_paused
    finally:
        BotState.is_paused = False
        with models.get_session() as s:
            for row in s.exec(select(models.OperationalState)):
                s.delete(row)
            s.commit()


# =====================================================
# Fix auditoría 2026-07-01 (bloqueante #4): pausa/kill-switch bloquea TAMBIÉN las ventas
# =====================================================


@pytest.mark.asyncio
async def test_paused_bot_never_sells():
    """DECISIÓN DE POLÍTICA (2026-07-01): con BotState.is_paused (kill-switch engaged o
    pausa preventiva) los exits NO venden. El runbook del kill-switch exige reconciliación
    manual con el bot quieto — una venta automática durante esa ventana puede duplicar la
    venta manual del operador (V2: el exceso abre posición contraria)."""
    from src.monitoring.health import BotState

    _seed_buy()
    client = _client()
    ex = _ex(client)
    BotState.is_paused = True
    try:
        out = await ex.exit_position(_pos())
    finally:
        BotState.is_paused = False

    assert not out.placed and out.reason == "paused"
    client.place_order.assert_not_called()
    assert _sells() == []  # ni siquiera intent: el bot está quieto
