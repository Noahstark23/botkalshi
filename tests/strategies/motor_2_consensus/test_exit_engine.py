"""
Tests del Motor2ExitEngine (cierre de posiciones de Motor 2).

Cubre: take-profit dispara al alcanzar el umbral, NO dispara por debajo, el net= descuenta
fees reales, agregación de patas, YES+NO del mismo market como posiciones independientes,
shadow no cierra nada, y el path de ejecución marca closed_by_clv=1 + pnl_cents correcto.

DB: la fixture autouse de conftest.py monta un SQLite temporal aislado por test.
"""

from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger
from sqlmodel import select

from src.math.fees import kalshi_fee_cents
from src.storage.models import Trade, get_session
from src.strategies.motor_2_consensus.exit_engine import Motor2ExitEngine
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor


def _orderbook(side: str, bid_cents: int) -> dict:
    """Orderbook mínimo con un bid en el lado dado (formato [["0.95","100"], ...])."""
    return {"orderbook": {side: [[f"0.{bid_cents:02d}", "100.00"]]}}


def _orderbook_both(yes_bid: int, no_bid: int) -> dict:
    return {
        "orderbook": {
            "yes": [[f"0.{yes_bid:02d}", "100.00"]],
            "no": [[f"0.{no_bid:02d}", "100.00"]],
        }
    }


def _seed_buy(
    ticker: str,
    *,
    side: str = "yes",
    count: int = 10,
    price: int = 54,
    coid: str | None = None,
    status: str = "filled",
    closed: bool = False,
) -> None:
    with get_session() as s:
        s.add(
            Trade(
                client_order_id=coid or f"{ticker}-{side}-{price}",
                ticker=ticker,
                side=side,
                action="buy",
                count=count,
                price_cents=price,
                fill_price_cents=price,
                strategy="motor_2_consensus",
                status=status,
                closed_by_clv=closed,
            )
        )
        s.commit()


@contextlib.contextmanager
def _capture_logs():
    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        yield captured
    finally:
        logger.remove(sink)


def _find(lines: list[str], *needles: str) -> str:
    return next(m for m in lines if all(n in m for n in needles))


def _engine(**kw) -> Motor2ExitEngine:
    eng = Motor2ExitEngine(**kw)
    eng._client = MagicMock()
    return eng


@pytest.mark.asyncio
async def test_take_profit_fires_at_threshold():
    """bid == umbral → [MOTOR 2 TP SHADOW] con net de fees."""
    eng = _engine(take_profit_enabled=True, tp_threshold=62)
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 62))
    _seed_buy("KXMLB", side="yes", count=10, price=54)

    with _capture_logs() as cap:
        await eng._tick()

    line = _find(cap, "[MOTOR 2 TP SHADOW]", "KXMLB")
    assert "10c side=yes" in line
    assert "bid=62c >= 62c" in line
    assert "entry=54c" in line
    assert "net=$" in line


@pytest.mark.asyncio
async def test_take_profit_does_not_fire_below_threshold():
    """bid < umbral → no dispara (no se loguea TP SHADOW)."""
    eng = _engine(take_profit_enabled=True, tp_threshold=62)
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 61))
    _seed_buy("KXMLB", side="yes", count=10, price=54)

    with _capture_logs() as cap:
        await eng._tick()

    assert not any("[MOTOR 2 TP SHADOW]" in m for m in cap)


@pytest.mark.asyncio
async def test_net_pnl_deducts_real_fees():
    """net = gross − fees(ambos lados) con kalshi_fee_cents — el net del shadow es el realizado."""
    bid, entry, count = 62, 54, 10
    eng = _engine(take_profit_enabled=True, tp_threshold=62)
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", bid))
    _seed_buy("KXFEE", side="yes", count=count, price=entry)

    with _capture_logs() as cap:
        await eng._tick()

    fees = kalshi_fee_cents(count, bid) + kalshi_fee_cents(count, entry)
    net = count * (bid - entry) - fees
    assert fees > 0  # sanity: las fees efectivamente se descuentan
    line = _find(cap, "[MOTOR 2 TP SHADOW]", "KXFEE")
    assert f"net=${net / 100:+.2f}" in line


@pytest.mark.asyncio
async def test_aggregates_multiple_legs_same_side():
    """Dos patas BUY del mismo (ticker, side) → una posición con count sumado."""
    eng = _engine(take_profit_enabled=True, tp_threshold=90)
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 95))
    _seed_buy("KXAGG", side="yes", count=10, price=50, coid="agg-a")
    _seed_buy("KXAGG", side="yes", count=5, price=60, coid="agg-b")

    with _capture_logs() as cap:
        await eng._tick()

    line = _find(cap, "[MOTOR 2 TP SHADOW]", "KXAGG")
    assert "15c side=yes" in line  # 10 + 5 agregados
    # net per-pata: 10*(95-50)-fees + 5*(95-60)-fees
    net = (
        10 * (95 - 50)
        - kalshi_fee_cents(10, 95)
        - kalshi_fee_cents(10, 50)
        + 5 * (95 - 60)
        - kalshi_fee_cents(5, 95)
        - kalshi_fee_cents(5, 60)
    )
    assert f"net=${net / 100:+.2f}" in line


@pytest.mark.asyncio
async def test_yes_and_no_same_market_are_independent():
    """YES y NO del MISMO market → dos posiciones independientes, cada una con su bid."""
    eng = _engine(take_profit_enabled=True, tp_threshold=90)
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook_both(yes_bid=95, no_bid=92))
    _seed_buy("KXBOTH", side="yes", count=10, price=50, coid="both-yes")
    _seed_buy("KXBOTH", side="no", count=8, price=40, coid="both-no")

    with _capture_logs() as cap:
        await eng._tick()

    yes_line = _find(cap, "[MOTOR 2 TP SHADOW]", "KXBOTH", "side=yes")
    no_line = _find(cap, "[MOTOR 2 TP SHADOW]", "KXBOTH", "side=no")
    assert "bid=95c" in yes_line
    assert "bid=92c" in no_line


@pytest.mark.asyncio
async def test_shadow_never_closes_positions():
    """Sin executor (shadow): aunque dispare el TP, ninguna fila se cierra."""
    eng = _engine(take_profit_enabled=True, tp_threshold=62)
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 95))
    _seed_buy("KXSHA", side="yes", count=10, price=54, coid="sha-buy")

    assert eng._executor is None
    await eng._tick()

    with get_session() as s:
        t = s.exec(select(Trade).where(Trade.client_order_id == "sha-buy")).first()
    assert t.status == "filled"
    assert t.closed_by_clv is False


@pytest.mark.asyncio
async def test_execution_marks_closed_by_clv_and_pnl():
    """Con executor + fill: la pata BUY queda closed_by_clv=1, settled y con pnl_cents realizado."""
    bid, entry, count = 62, 54, 10
    client = MagicMock()
    client.get_orderbook = AsyncMock(return_value=_orderbook("yes", bid))
    client.place_order = AsyncMock(
        return_value={"order": {"order_id": "ord-1", "fill_count": count}}
    )

    eng = Motor2ExitEngine(trading_enabled=True, take_profit_enabled=True, tp_threshold=62)
    eng._client = client
    eng._executor = Motor3ExitExecutor(client, strategy="motor_2_consensus")
    _seed_buy("KXEXE", side="yes", count=count, price=entry, coid="exe-buy")

    await eng._tick()

    with get_session() as s:
        buy = s.exec(select(Trade).where(Trade.client_order_id == "exe-buy")).first()
        sells = list(s.exec(select(Trade).where(Trade.action == "sell")))

    assert buy.closed_by_clv is True
    assert buy.status == "settled"
    expected_pnl = (
        count * (bid - entry) - kalshi_fee_cents(count, bid) - kalshi_fee_cents(count, entry)
    )
    assert buy.pnl_cents == expected_pnl
    # El SELL de auditoría queda atribuido a Motor 2 (no a motor_3_clv).
    assert len(sells) == 1
    assert sells[0].strategy == "motor_2_consensus"
