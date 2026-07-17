"""
Bug 4 (incidente 2026-07-07): Motor 3 gestiona las HUÉRFANAS de Motor 1 — y SOLO las huérfanas.

Regla de oro: un arb con ambas patas filladas es un hedge de payout garantizado; venderlo suelto
rompe el hedge. El helper motor1_orphan_buys y el cap del engine/executor lo garantizan.
DB: la fixture autouse del conftest monta un SQLite temporal por test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlmodel import select

from src.storage.models import PortfolioPosition, Trade, get_session
from src.strategies.motor_3_clv.engine import Motor3Engine
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor
from src.strategies.motor_3_clv.orphans import arb_id_of, motor1_orphan_buys

TICKER = "KXMLBGAME-26JUL061845HOUWSH-HOU"


def _buy(
    coid: str,
    *,
    side: str = "yes",
    count: int = 7,
    price: int = 18,
    status: str = "filled",
    arb: str = "arb-1",
    strategy: str = "motor_1_arbitrage",
    minutes_ago: int = 60,
) -> Trade:
    return Trade(
        client_order_id=coid,
        ticker=TICKER,
        side=side,
        action="buy",
        count=count,
        price_cents=price,
        fill_price_cents=price,
        strategy=strategy,
        status=status,
        notes=f"arb_id={arb}",
        placed_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )


# =====================================================
# Helper puro: motor1_orphan_buys
# =====================================================


def test_arb_id_parses_notes():
    assert arb_id_of(_buy("a")) == "arb-1"
    t = _buy("b")
    t.notes = "otra cosa"
    assert arb_id_of(t) is None


def test_hedged_pair_is_not_orphan():
    """Ambas patas filladas del mismo arb → hedge → NINGUNA es huérfana."""
    legs = [_buy("y", side="yes", arb="a1"), _buy("n", side="no", price=75, arb="a1")]
    assert motor1_orphan_buys(legs) == []


def test_leg_without_filled_sibling_is_orphan():
    """La pata yes filló; la no del mismo arb NO está en los buys abiertos (rolled_back) →
    la yes es huérfana gestionable."""
    legs = [_buy("y", side="yes", arb="a1")]  # la hermana no aparece (no filled)
    orphans = motor1_orphan_buys(legs)
    assert [o.client_order_id for o in orphans] == ["y"]


def test_no_arb_id_is_conservative_not_orphan():
    t = _buy("x")
    t.notes = None
    assert motor1_orphan_buys([t]) == []


def test_other_strategies_ignored():
    assert motor1_orphan_buys([_buy("m2", strategy="motor_2_consensus")]) == []


# =====================================================
# Engine: atribución con cap al count huérfano
# =====================================================


def _seed(trades: list[Trade]) -> None:
    with get_session() as s:
        for t in trades:
            s.add(t)
        s.commit()


def _position(count: int = 433) -> PortfolioPosition:
    return PortfolioPosition(
        ticker=TICKER,
        side="yes",
        count=count,
        close_time=datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=12),
    )


def test_engine_without_flag_skips_motor1_positions():
    """Default (manages_orphans=False): la posición de Motor 1 sigue siendo ajena → SKIP."""
    _seed([_buy("y1", arb="a1")])  # huérfana real, pero flag off
    with get_session() as s:
        s.add(_position())
        s.commit()
        pos = list(s.exec(select(PortfolioPosition)))
    eng = Motor3Engine(manages_orphans=False)
    assert eng._attributable_positions(pos) == []


def test_engine_with_flag_caps_count_to_orphans():
    """Incidente: posición neta 433 = 412 hedged (2 arbs completos) + 21 huérfanos (3 arbs sin
    hermana). Con el flag, Motor 3 gestiona SOLO 21 — el hedge queda intocable."""
    _seed(
        [
            # 2 arbs completos (hedged): 206 + 206 yes con sus no
            _buy("h1y", side="yes", count=206, arb="h1"),
            _buy("h1n", side="no", count=206, price=75, arb="h1"),
            _buy("h2y", side="yes", count=206, arb="h2"),
            _buy("h2n", side="no", count=206, price=75, arb="h2"),
            # 3 arbs huérfanos (la no no filló): 7 c/u
            _buy("o1", side="yes", count=7, arb="o1", minutes_ago=50),
            _buy("o2", side="yes", count=7, arb="o2", minutes_ago=40),
            _buy("o3", side="yes", count=7, arb="o3", minutes_ago=30),
        ]
    )
    with get_session() as s:
        s.add(_position(count=433))
        s.commit()
        pos = list(s.exec(select(PortfolioPosition)))

    eng = Motor3Engine(manages_orphans=True)
    kept = eng._attributable_positions(pos)

    assert len(kept) == 1
    assert kept[0].count == 21  # SOLO las huérfanas; 412 hedged jamás se venden
    # entry para el trailing: FIFO de las huérfanas (todas a 18c)
    assert eng._orphan_by_pair[(TICKER, "yes")] == (21, 18)
    assert eng._entry_bid_for(kept[0]) == 18


# =====================================================
# Executor: el cap de venta y el settle solo tocan huérfanas
# =====================================================


@pytest.mark.asyncio
async def test_executor_sells_and_settles_only_orphans():
    """exit_position sobre la posición neta (433): con include_motor1_orphans, el cap
    atribuible = 21 → vende 21; el settle marca closed_by_clv SOLO las patas huérfanas."""
    _seed(
        [
            _buy("h1y", side="yes", count=412, arb="h1"),
            _buy("h1n", side="no", count=412, price=75, arb="h1"),
            _buy("o1", side="yes", count=21, arb="o1"),
        ]
    )
    client = MagicMock()
    client.get_orderbook = AsyncMock(return_value={"orderbook": {"yes": [["0.15", "500.00"]]}})
    client.place_order = AsyncMock(return_value={"order": {"order_id": "k1", "fill_count": 21}})

    ex = Motor3ExitExecutor(client, include_motor1_orphans=True)
    pos = _position(count=433)
    outcome = await ex.exit_position(pos)

    assert outcome.filled is True and outcome.filled_count == 21
    # la orden de venta fue por 21 (el cap atribuible), NO por 433
    assert client.place_order.await_args.kwargs["count"] == 21
    with get_session() as s:
        orphan = s.exec(select(Trade).where(Trade.client_order_id == "o1")).first()
        hedged = s.exec(select(Trade).where(Trade.client_order_id == "h1y")).first()
    assert orphan.closed_by_clv is True and orphan.status == "settled"
    assert hedged.closed_by_clv is False and hedged.status == "filled"  # hedge INTACTO


@pytest.mark.asyncio
async def test_executor_without_flag_refuses_motor1_position():
    """Sin el flag: cero BUYs atribuibles → already_closed, no vende nada (sin cambios)."""
    _seed([_buy("o1", side="yes", count=21, arb="o1")])
    client = MagicMock()
    client.get_orderbook = AsyncMock(return_value={"orderbook": {"yes": [["0.15", "500.00"]]}})
    client.place_order = AsyncMock()

    ex = Motor3ExitExecutor(client, include_motor1_orphans=False)
    outcome = await ex.exit_position(_position(count=21))

    assert outcome.placed is False
    client.place_order.assert_not_called()
