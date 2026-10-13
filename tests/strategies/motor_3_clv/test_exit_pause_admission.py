"""Una pausa recibida durante un await debe impedir una NUEVA reserva SELL.

Cliente simulado y SQLite temporal del paquete: ninguna petición de mercado.
M2 salidas y M3 comparten executor y deben conservar la misma barrera.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlmodel import Session, select

from src.monitoring.health import BotState
from src.storage.models import OperationalState, PortfolioPosition, Trade, get_session
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

TICKER = "KXPAUSE-TEST"
BOOK = {"orderbook": {"yes": [["0.60", "100"]], "no": []}}


def _seed_buy():
    with get_session() as session:
        session.add(
            Trade(
                client_order_id="pause-test-buy",
                ticker=TICKER,
                side="yes",
                action="buy",
                count=2,
                price_cents=45,
                fill_price_cents=45,
                strategy="motor_2_consensus",
                status="filled",
            )
        )
        session.commit()


def _client():
    client = MagicMock()
    client.get_orderbook = AsyncMock(return_value=BOOK)
    # Una admisión válida conserva la incertidumbre del POST ficticio; no concilia
    # ni altera las BUY para conseguir un resultado favorable en el control.
    client.place_order = AsyncMock(side_effect=httpx.ReadTimeout("simulated_ack_loss"))
    return client


def _block(kind, monkeypatch):
    if kind == "runtime":
        BotState.is_paused = True
        BotState.pause_reason = "test_pause_during_await"
    elif kind in {"durable", "invalid"}:
        with get_session() as session:
            session.add(
                OperationalState(
                    key="kill_switch",
                    value="engaged" if kind == "durable" else "unrecognized",
                    reason="test_pause_during_await",
                )
            )
            session.commit()
    else:
        original = Session.get

        def unreadable(session, entity, *args, **kwargs):
            if entity is OperationalState:
                raise RuntimeError("simulated_pause_read_failure")
            return original(session, entity, *args, **kwargs)

        monkeypatch.setattr(Session, "get", unreadable)


@pytest.mark.parametrize("strategy", ["motor_3_clv", "motor_2_consensus"])
@pytest.mark.parametrize("boundary", ["reconcile", "orderbook"])
@pytest.mark.parametrize("kind", ["runtime", "durable", "invalid", "unreadable"])
async def test_pause_during_await_blocks_sell_admission(strategy, boundary, kind, monkeypatch):
    _seed_buy()
    client = _client()
    executor = Motor3ExitExecutor(client, strategy=strategy)

    if boundary == "reconcile":

        async def reconcile(*_):
            _block(kind, monkeypatch)
            return True

        monkeypatch.setattr(executor, "_reconcile_pending_sells", reconcile)
    else:

        async def orderbook(*_):
            _block(kind, monkeypatch)
            return BOOK

        client.get_orderbook.side_effect = orderbook

    outcome = await executor.exit_position(PortfolioPosition(ticker=TICKER, side="yes", count=2))

    assert not outcome.placed
    client.place_order.assert_not_awaited()
    with get_session() as session:
        trades = list(session.exec(select(Trade)))
        assert len(trades) == 1
        assert trades[0].status == "filled" and not trades[0].closed_by_clv
        if kind in {"durable", "invalid"}:
            row = session.get(OperationalState, "kill_switch")
            assert row.value == ("engaged" if kind == "durable" else "unrecognized")
            assert row.reason == "test_pause_during_await"


@pytest.mark.parametrize("strategy", ["motor_3_clv", "motor_2_consensus"])
@pytest.mark.parametrize("state", [None, "clear"])
async def test_verified_unpaused_admission_keeps_lost_ack_pending(strategy, state):
    _seed_buy()
    if state:
        with get_session() as session:
            session.add(OperationalState(key="kill_switch", value=state))
            session.commit()
    client = _client()
    outcome = await Motor3ExitExecutor(client, strategy=strategy).exit_position(
        PortfolioPosition(ticker=TICKER, side="yes", count=2)
    )
    assert outcome.placed and outcome.reason == "error_red"
    client.place_order.assert_awaited_once()
    with get_session() as session:
        rows = list(session.exec(select(Trade)))
        sells = [row for row in rows if row.action == "sell"]
        assert len(sells) == 1 and sells[0].status == "pending"
        assert len(rows) == 2
