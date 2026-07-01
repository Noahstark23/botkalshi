"""
Contabilidad exacta de fees en los exits (deudas auditoría Motor 3, 2026-07-01).

(a) Split parcial: el remanente conservaba fees_cents del count ORIGINAL → cuando
settleaba por resolución, _leg_pnl_cents restaba las fees de la porción cerrada DOS
veces. Ahora el remanente queda con fees prorrateadas y la hija lleva las suyas.
(b) Fee del exit por tramo: sumar ceil(fee(closed)) por pata sobre-cobraba hasta
(n_patas−1)¢ y no cuadraba con la fila SELL. Ahora se asigna acumulativo
(cumfee(k)−cumfee(k_prev)) → la suma es EXACTA al fee real del fill.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import select

from src.math.fees import kalshi_fee_cents
from src.storage.models import PortfolioPosition, Trade, get_session
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

TICKER = "KXACCT"


def _client(*, fill_count: int) -> MagicMock:
    c = MagicMock()
    c.get_orderbook = AsyncMock(return_value={"orderbook": {"yes": [["0.60", "100"]], "no": []}})
    c.place_order = AsyncMock(return_value={"order": {"order_id": "o", "fill_count": fill_count}})
    c.get_orders = AsyncMock(return_value={"orders": []})
    return c


def _seed_buy(coid: str, count: int, *, fees: int | None) -> None:
    with get_session() as s:
        s.add(
            Trade(
                client_order_id=coid,
                ticker=TICKER,
                side="yes",
                action="buy",
                count=count,
                price_cents=45,
                fill_price_cents=45,
                fees_cents=fees,
                strategy="motor_2_consensus",
                status="filled",
            )
        )
        s.commit()


def _rows() -> list[Trade]:
    with get_session() as s:
        return list(s.exec(select(Trade).where(Trade.action == "buy")))


@pytest.mark.asyncio
async def test_partial_split_prorates_entry_fees():
    """BUY 10 @45 con fees pagadas fee(10,45); exit llena 4 → remanente queda con
    fee(10,45) − fee(4,45) y la hija con fee(4,45): el total pagado se conserva y la
    resolución del remanente ya no resta fees de contratos que no tiene."""
    paid = kalshi_fee_cents(10, 45)
    _seed_buy("b1", 10, fees=paid)
    ex = Motor3ExitExecutor(_client(fill_count=4), entry_origin=("motor_2_consensus",))

    out = await ex.exit_position(PortfolioPosition(ticker=TICKER, side="yes", count=10))
    assert out.filled and out.filled_count == 4

    rows = {("rem" if r.status == "filled" else "child"): r for r in _rows()}
    tramo = kalshi_fee_cents(4, 45)
    assert rows["rem"].count == 6 and rows["rem"].fees_cents == paid - tramo
    assert rows["child"].count == 4 and rows["child"].fees_cents == tramo
    assert rows["rem"].fees_cents + rows["child"].fees_cents == paid


@pytest.mark.asyncio
async def test_exit_fee_allocated_cumulatively_across_legs():
    """Exit de 10 que cierra dos patas de 5+5: la suma de PnL usa EXACTAMENTE
    fee(10, exit) — no 2×ceil(fee(5, exit))."""
    _seed_buy("b1", 5, fees=None)
    _seed_buy("b2", 5, fees=None)
    ex = Motor3ExitExecutor(_client(fill_count=10), entry_origin=("motor_2_consensus",))

    out = await ex.exit_position(PortfolioPosition(ticker=TICKER, side="yes", count=10))
    assert out.filled and out.filled_count == 10

    rows = _rows()
    assert all(r.status == "settled" and r.closed_by_clv for r in rows)
    total_pnl = sum(r.pnl_cents for r in rows)
    expected = (
        10 * (60 - 45)
        - kalshi_fee_cents(10, 60)  # fee del exit EXACTA (no suma de ceils por pata)
        - 2 * kalshi_fee_cents(5, 45)  # fees de entrada por pata (órdenes separadas)
    )
    assert total_pnl == expected
