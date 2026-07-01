"""
Coordinación entre exit engines + atribución de posición (fix auditoría 2026-07-01).

Los bugs: (a) Motor 2 exits y Motor 3 construían instancias SEPARADAS de
Motor3ExitExecutor con locks POR INSTANCIA → ambos podían vender la misma posición en la
misma ventana, y en la API V2 la segunda venta no se rechaza: abre posición contraria;
(b) Motor 3 iteraba TODAS las PortfolioPosition de la cuenta (posición NETA, sin origen)
→ podía vender la leg ganadora de un arb hedged del Motor REST (rompe el hedge: profit
lockeado → pérdida) o posiciones manuales del dueño; (c) _settle_originals cerraba FIFO
cross-strategy.

Verifica: locks compartidos a nivel proceso; re-check DENTRO del lock contra filas Trade
(fuente de verdad sincrónica) con venta capeada al total abierto atribuible; el filtro de
atribución del engine; y que _settle_originals respeta entry_origin.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import select

import src.storage.models as models
from src.storage.models import PortfolioPosition, Trade, get_session
from src.strategies.motor_3_clv.engine import Motor3Engine
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

TICKER = "KXCOORD"


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _pos(count: int = 10, side: str = "yes") -> PortfolioPosition:
    return PortfolioPosition(ticker=TICKER, side=side, count=count)


def _client(*, bid_cents: int = 60, fill_count: int = 10) -> MagicMock:
    c = MagicMock()
    c.get_orderbook = AsyncMock(
        return_value={"orderbook": {"yes": [[f"0.{bid_cents:02d}", "100"]], "no": []}}
    )
    c.place_order = AsyncMock(return_value={"order": {"order_id": "o", "fill_count": fill_count}})
    return c


def _seed_buy(
    *,
    ticker: str = TICKER,
    side: str = "yes",
    count: int = 10,
    strategy: str = "motor_2_consensus",
    placed_at: datetime | None = None,
    coid: str | None = None,
) -> None:
    with get_session() as s:
        t = Trade(
            client_order_id=coid or f"seed-{strategy}-{ticker}-{side}-{count}",
            ticker=ticker,
            side=side,
            action="buy",
            count=count,
            price_cents=45,
            fill_price_cents=45,
            strategy=strategy,
            status="filled",
        )
        if placed_at is not None:
            t.placed_at = placed_at
        s.add(t)
        s.commit()


def _rows(strategy: str) -> list[Trade]:
    with get_session() as s:
        return list(s.exec(select(Trade).where(Trade.strategy == strategy)))


# =====================================================
# Locks compartidos a nivel proceso
# =====================================================


@pytest.mark.asyncio
async def test_locks_are_shared_across_executor_instances():
    """El lock del ticker tomado por el executor de un motor bloquea al del OTRO motor."""
    _seed_buy(count=10)
    ex_m2 = Motor3ExitExecutor(
        _client(), strategy="motor_2_consensus", entry_origin=("motor_2_consensus",)
    )
    ex_m3 = Motor3ExitExecutor(_client(), entry_origin=("motor_2_consensus",))

    await ex_m2._lock_for(TICKER).acquire()  # M2 está vendiendo este ticker ahora mismo
    try:
        out = await ex_m3.exit_position(_pos())
    finally:
        ex_m2._lock_for(TICKER).release()

    assert out.reason == "busy"
    ex_m3.client.place_order.assert_not_called()


# =====================================================
# Re-check dentro del lock (anti doble-venta con cache stale)
# =====================================================


@pytest.mark.asyncio
async def test_second_engine_with_stale_position_skips_already_closed():
    """M2 vende y settlea; M3 llega segundos después con su PortfolioPosition stale (cache
    de 60s) → el re-check contra las filas ve todo cerrado y NO re-vende (la re-venta en
    V2 abriría una posición NO contraria)."""
    _seed_buy(count=10)
    ex_m2 = Motor3ExitExecutor(
        _client(fill_count=10), strategy="motor_2_consensus", entry_origin=("motor_2_consensus",)
    )
    first = await ex_m2.exit_position(_pos(count=10))
    assert first.filled and first.filled_count == 10

    ex_m3 = Motor3ExitExecutor(_client(), entry_origin=("motor_2_consensus",))
    second = await ex_m3.exit_position(_pos(count=10))  # posición stale: aún dice 10

    assert not second.placed and second.reason == "already_closed"
    ex_m3.client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_sell_count_capped_to_open_attributable_rows():
    """La venta se dimensiona contra las filas abiertas propias, no contra la posición
    NETA de la cuenta (que puede incluir contratos ajenos)."""
    _seed_buy(count=4)
    ex = Motor3ExitExecutor(_client(fill_count=4), entry_origin=("motor_2_consensus",))

    out = await ex.exit_position(_pos(count=10))  # cuenta neta dice 10; nuestras filas: 4

    assert out.placed
    assert ex.client.place_order.call_args.kwargs["count"] == 4


@pytest.mark.asyncio
async def test_position_without_own_rows_is_never_sold():
    """Posición manual del dueño (sin filas de origen) → no se vende (no la abrimos)."""
    ex = Motor3ExitExecutor(_client(), entry_origin=("motor_2_consensus",))
    out = await ex.exit_position(_pos(count=10))
    assert not out.placed and out.reason == "already_closed"
    ex.client.place_order.assert_not_called()


# =====================================================
# _settle_originals respeta entry_origin
# =====================================================


@pytest.mark.asyncio
async def test_settle_originals_does_not_touch_other_strategy_rows():
    """Con una pata REST más vieja en el mismo (ticker, side), el exit de Motor 2 cierra
    SOLO sus filas: la de motor_rest_arb queda intacta (antes: FIFO cross-strategy la
    settleaba → hedge roto en DB + re-venta del remanente de Motor 2)."""
    old = _naive_now() - timedelta(hours=3)
    _seed_buy(strategy="motor_rest_arb", count=10, placed_at=old, coid="rest-leg")
    _seed_buy(strategy="motor_2_consensus", count=10, coid="m2-buy")
    ex = Motor3ExitExecutor(
        _client(fill_count=10), strategy="motor_2_consensus", entry_origin=("motor_2_consensus",)
    )

    out = await ex.exit_position(_pos(count=20))
    assert out.filled and out.filled_count == 10  # capeado a las filas M2 (no 20)

    rest = _rows("motor_rest_arb")
    assert len(rest) == 1 and rest[0].status == "filled" and not rest[0].closed_by_clv
    m2_buys = [t for t in _rows("motor_2_consensus") if t.action == "buy"]
    assert all(t.status == "settled" and t.closed_by_clv for t in m2_buys)


# =====================================================
# Filtro de atribución del engine (Motor 3)
# =====================================================


def _engine() -> Motor3Engine:
    eng = Motor3Engine(trading_enabled=False)
    eng._poller = MagicMock()
    return eng


def test_engine_skips_rest_arb_backed_ticker():
    """Ticker con CUALQUIER pata abierta de motor_rest_arb → fuera (leg de arb hedged:
    venderla suelta convierte profit lockeado en pérdida)."""
    _seed_buy(strategy="motor_rest_arb", count=10)
    _seed_buy(strategy="motor_2_consensus", count=5)  # incluso con pata M2 en el mismo ticker
    kept = _engine()._attributable_positions([_pos(count=15)])
    assert kept == []


def test_engine_skips_manual_position():
    """Posición sin ninguna fila propia (manual/desconocida) → fuera."""
    kept = _engine()._attributable_positions([_pos(count=10)])
    assert kept == []


def test_engine_keeps_motor2_backed_position():
    _seed_buy(strategy="motor_2_consensus", count=10)
    positions = [_pos(count=10)]
    kept = _engine()._attributable_positions(positions)
    assert kept == positions


def test_engine_ignores_closed_rows_for_attribution():
    """Filas closed_by_clv/settled no atribuyen: posición ya cerrada por exit → fuera."""
    _seed_buy(strategy="motor_2_consensus", count=10)
    with get_session() as s:
        t = s.exec(select(Trade)).first()
        t.status = "settled"
        t.closed_by_clv = True
        s.add(t)
        s.commit()
    kept = _engine()._attributable_positions([_pos(count=10)])
    assert kept == []


def test_engine_attribution_db_error_is_failsafe(monkeypatch):
    """Error de DB en la carga → tick sin posiciones (ante la duda, no gestionar nada)."""

    def _boom():
        raise RuntimeError("db caída")

    monkeypatch.setattr("src.strategies.motor_3_clv.engine.get_session", _boom)
    kept = _engine()._attributable_positions([_pos(count=10)])
    assert kept == []
