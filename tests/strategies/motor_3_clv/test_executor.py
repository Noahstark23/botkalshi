"""
Tests del Motor3ExitExecutor (Motor 3, FASE 3) — liquidación SELL IOC al bid.

Cubre: sin bid → no vende; fill al bid con params correctos (sell/side/IOC); lock
por-ticker que descarta una venta concurrente. `place_order` mockeado (no toca red).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import src.storage.models as models
from src.storage.models import PortfolioPosition
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor


def _pos(side: str = "yes", count: int = 10) -> PortfolioPosition:
    return PortfolioPosition(ticker="KXTEST", side=side, count=count)


def _seed_open_buy(
    *, side: str = "yes", count: int = 10, strategy: str = "motor_2_consensus"
) -> None:
    """Pata BUY filled que respalda la posición: el executor re-verifica DENTRO del lock
    contra las filas Trade (atribución) y capea la venta al total abierto — sin respaldo,
    exit_position devuelve already_closed (fix auditoría 2026-07-01)."""
    with models.get_session() as s:
        s.add(
            models.Trade(
                client_order_id=f"seed-{side}-{count}",
                ticker="KXTEST",
                side=side,
                action="buy",
                count=count,
                price_cents=45,
                fill_price_cents=45,
                strategy=strategy,
                status="filled",
            )
        )
        s.commit()


def _client(orderbook: dict, *, place_resp: dict | None = None) -> MagicMock:
    c = MagicMock()
    c.get_orderbook = AsyncMock(return_value=orderbook)
    c.place_order = AsyncMock(return_value=place_resp)
    return c


@pytest.mark.asyncio
async def test_no_bid_does_not_place():
    """Orderbook sin bids en nuestro lado → no hay con quién cerrar → no se coloca orden."""
    _seed_open_buy(side="yes", count=10)
    ex = Motor3ExitExecutor(_client({"orderbook": {"yes": [], "no": []}}))
    out = await ex.exit_position(_pos("yes"))
    assert out.reason == "no_bid" and out.placed is False
    ex.client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_filled_sells_at_bid_with_ioc():
    """Vende al MEJOR bid del lado, action=sell, IOC; reporta el fill."""
    c = _client(
        {"orderbook": {"yes": [["0.55", "100"], ["0.60", "50"]], "no": []}},
        place_resp={"order": {"order_id": "o1", "fill_count": 10}},
    )
    _seed_open_buy(side="yes", count=10)
    ex = Motor3ExitExecutor(c)
    out = await ex.exit_position(_pos("yes", count=10))

    assert out.filled is True and out.filled_count == 10 and out.sell_price_cents == 60
    kwargs = c.place_order.call_args.kwargs
    assert kwargs["action"] == "sell"
    assert kwargs["side"] == "yes"
    assert kwargs["yes_price"] == 60  # el mejor bid (0.60)
    assert kwargs["no_price"] is None
    assert kwargs["count"] == 10
    assert kwargs["time_in_force"] == "immediate_or_cancel"


@pytest.mark.asyncio
async def test_no_side_uses_no_price():
    """Posición NO → vende el lado 'no' al bid del 'no' (no_price)."""
    c = _client(
        {"orderbook": {"yes": [], "no": [["0.40", "30"]]}},
        place_resp={"order": {"fill_count": 5}},
    )
    _seed_open_buy(side="no", count=5)
    ex = Motor3ExitExecutor(c)
    out = await ex.exit_position(_pos("no", count=5))
    assert out.filled is True and out.sell_price_cents == 40
    kwargs = c.place_order.call_args.kwargs
    assert kwargs["side"] == "no" and kwargs["no_price"] == 40 and kwargs["yes_price"] is None


@pytest.mark.asyncio
async def test_lock_released_after_exit_allows_reattempt():
    """FASE 3: tras un exit (incl. fill parcial), el lock del ticker se LIBERA → el próximo
    tick puede reintentar el remanente. Si no se liberara, la salida quedaría trabada."""
    c = _client(
        {"orderbook": {"yes": [["0.60", "50"]], "no": []}},
        place_resp={"order": {"fill_count": 4}},  # parcial: vende 4 de 10
    )
    _seed_open_buy(side="yes", count=10)
    ex = Motor3ExitExecutor(c)
    pos = _pos("yes", count=10)
    out = await ex.exit_position(pos)
    assert out.filled is True and out.filled_count == 4
    assert ex._lock_for(pos.ticker).locked() is False  # liberado → reintento posible


@pytest.mark.asyncio
async def test_concurrent_exit_skips_busy():
    """Si el lock del ticker ya está tomado (otra venta en curso) → se descarta, no doble-vende."""
    c = _client(
        {"orderbook": {"yes": [["0.60", "50"]], "no": []}},
        place_resp={"order": {"fill_count": 10}},
    )
    ex = Motor3ExitExecutor(c)
    pos = _pos("yes")
    await ex._lock_for(pos.ticker).acquire()  # simular ejecución concurrente en curso
    try:
        out = await ex.exit_position(pos)
    finally:
        ex._lock_for(pos.ticker).release()
    assert out.reason == "busy"
    c.place_order.assert_not_called()
