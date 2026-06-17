"""
FASE 2 QA — colisión de settlement. Cuando Motor 3 cierra una posición con un SELL
anticipado, la pata BUY original NO debe settlearse de nuevo por resolución de mercado
(doble-conteo de PnL). Verifica: el BUY queda settled+closed_by_clv con PnL realizado;
el SettlementPoller lo saltea; el parcial PARTE la pata.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import select

from src.storage.models import Trade, get_session
from src.strategies.motor_2_consensus.executor import STRATEGY as M2_STRATEGY
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor
from src.strategies.motor_rest_arb.settlement import SettlementPoller


def _seed_buy(ticker="KXC", side="yes", count=10, fill_price=40, strategy=M2_STRATEGY) -> str:
    """Inserta una pata BUY 'filled' (como la dejaría Motor 2) y devuelve su coid."""
    coid = f"buy-{ticker}-{side}"
    with get_session() as s:
        s.add(
            Trade(
                client_order_id=coid,
                ticker=ticker,
                side=side,
                action="buy",
                count=count,
                price_cents=fill_price,
                fill_price_cents=fill_price,
                strategy=strategy,
                status="filled",
            )
        )
        s.commit()
    return coid


def _exec_with_fill(orderbook_side_levels, fill_count) -> Motor3ExitExecutor:
    client = MagicMock()
    client.get_orderbook = AsyncMock(
        return_value={"orderbook": {"yes": orderbook_side_levels, "no": orderbook_side_levels}}
    )
    client.place_order = AsyncMock(
        return_value={"order": {"order_id": "o1", "fill_count": fill_count}}
    )
    return Motor3ExitExecutor(client)


def _get(coid: str) -> Trade | None:
    with get_session() as s:
        return s.exec(select(Trade).where(Trade.client_order_id == coid)).first()


@pytest.mark.asyncio
async def test_full_exit_settles_original_with_realized_pnl():
    """Exit que cierra toda la posición → BUY original settled+closed_by_clv con PnL real."""
    from src.storage.models import PortfolioPosition

    _seed_buy(count=10, fill_price=40)
    ex = _exec_with_fill([["0.60", "100"]], fill_count=10)  # bid 60, llena 10
    out = await ex.exit_position(PortfolioPosition(ticker="KXC", side="yes", count=10))

    assert out.filled and out.filled_count == 10
    buy = _get("buy-KXC-yes")
    assert buy.status == "settled" and buy.closed_by_clv is True
    # PnL = 10*(60-40) − fee_venta − fee_compra. Debe ser claramente positivo y < bruto 200.
    assert buy.pnl_cents is not None and 0 < buy.pnl_cents < 200


@pytest.mark.asyncio
async def test_settlement_poller_skips_closed_by_clv():
    """El SettlementPoller NO procesa una pata cerrada por CLV (anti doble-conteo)."""
    from src.storage.models import PortfolioPosition

    _seed_buy(count=10, fill_price=40)
    ex = _exec_with_fill([["0.60", "100"]], fill_count=10)
    await ex.exit_position(PortfolioPosition(ticker="KXC", side="yes", count=10))

    # Fuente que SÍ resolvería el mercado — pero la pata ya está closed_by_clv → se saltea.
    source = MagicMock()
    source.get_resolution = AsyncMock()
    settled = await SettlementPoller(source).settle_once()
    assert settled == 0  # nada que settlear
    source.get_resolution.assert_not_called()  # ni siquiera consultó la resolución


@pytest.mark.asyncio
async def test_partial_exit_splits_original():
    """Exit parcial (vende 4 de 10) → original queda con 6 'filled'; hija settled de 4."""
    from src.storage.models import PortfolioPosition

    _seed_buy(count=10, fill_price=40)
    ex = _exec_with_fill([["0.60", "100"]], fill_count=4)  # llena solo 4
    out = await ex.exit_position(PortfolioPosition(ticker="KXC", side="yes", count=10))
    assert out.filled and out.filled_count == 4

    with get_session() as s:
        rows = list(s.exec(select(Trade).where(Trade.ticker == "KXC", Trade.action == "buy")))
    open_rows = [r for r in rows if r.status == "filled"]
    closed_rows = [r for r in rows if r.status == "settled" and r.closed_by_clv]
    assert len(open_rows) == 1 and open_rows[0].count == 6  # remanente abierto
    assert len(closed_rows) == 1 and closed_rows[0].count == 4  # porción cerrada
    assert closed_rows[0].pnl_cents is not None and closed_rows[0].pnl_cents > 0
