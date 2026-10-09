"""GETs ficticios -> parser exacto -> cierre contable real en SQLite temporal.

Integra collect_exit_execution, ExitFillSummary y finalizadores M3/reduction.
No ejecuta runner, clientes autenticados, órdenes ni conexiones demo. El estado
terminal lo suministra la fixture; aquí no se acredita terminalidad del exchange.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from unittest.mock import Mock

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
from src.storage.exit_capture import collect_exit_execution
from src.storage.exit_reconciliation import reconcile_known_exit
from src.storage.quantities import confirmed_fill, filled_fields, open_quantity, quantity_fields
from src.storage.reductions import finalize_reduction, persist_reduction
from src.strategies.motor_3_clv.executor import Motor3ExitExecutor

NOW = datetime(2026, 10, 8, 16, tzinfo=UTC)
ORDER = "fixture-exit-order"
TICKER = "FIXTURE-EXACT-EXIT"


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'exit-capture.db'}")
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    denied = Mock(side_effect=AssertionError("OFFLINE_EXIT_ACCOUNTING_ONLY"))
    for target in (
        "socket.socket.connect",
        "socket.socket.connect_ex",
        "socket.create_connection",
        "socket.getaddrinfo",
        "src.clients.kalshi_rest.KalshiRestClient.__init__",
    ):
        monkeypatch.setattr(target, denied)
    yield
    engine.dispose()
    denied.assert_not_called()


class GetOnlyClient:
    """Sólo las dos lecturas permitidas; no existe interfaz de mutación."""

    def __init__(self, live, historical):
        self.pages = {"live": list(live), "historical": list(historical)}
        self.calls = []

    async def _page(self, source, params):
        self.calls.append((source, dict(params)))
        assert params["ticker"] == TICKER and params["subaccount"] == 0
        assert params["limit"] == 100
        if source == "live":
            assert params["order_id"] == ORDER
        else:
            assert "order_id" not in params
        result = self.pages[source].pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def get_fills(self, **params):
        return await self._page("live", params)

    async def get_historical_fills(self, **params):
        return await self._page("historical", params)


class KnownOrderGetClient(GetOnlyClient):
    def __init__(self, order, live, historical):
        super().__init__(live, historical)
        self.order = order
        self.order_calls = []

    async def get_order(self, order_id):
        self.order_calls.append(order_id)
        assert order_id == ORDER
        return {"order": dict(self.order)}


def terminal_order(*, side="yes", **changes):
    row = {
        "order_id": ORDER,
        "client_order_id": "sell",
        "ticker": TICKER,
        "side": side,
        "action": "sell",
        "outcome_side": "no" if side == "yes" else "yes",
        "book_side": "ask" if side == "yes" else "bid",
        "subaccount_number": 0,
        "status": "canceled",
        "initial_count_fp": "1.00",
        "fill_count_fp": "0.25",
        "remaining_count_fp": "0.00",
    }
    row.update(changes)
    return row


def page(*fills, cursor=""):
    return {"fills": list(fills), "cursor": cursor}


def provider_fill(
    *, side="yes", price="0.700100", count="0.25", fee="0.000123", fid="f1", **changes
):
    # Conversión sólo para la fixture: todas sus cotizaciones tienen 6dp exactos.
    opposite_units = 1_000_000 - int(Fraction(price) * 1_000_000)
    complement = f"{opposite_units // 1_000_000}.{opposite_units % 1_000_000:06d}"
    row = {
        "fill_id": fid,
        "trade_id": fid,
        "order_id": ORDER,
        "ticker": TICKER,
        "market_ticker": TICKER,
        "side": side,
        "action": "sell",
        "outcome_side": "no" if side == "yes" else "yes",
        "book_side": "ask" if side == "yes" else "bid",
        f"{side}_price_dollars": price,
        f"{'no' if side == 'yes' else 'yes'}_price_dollars": complement,
        "count_fp": count,
        "fee_cost": fee,
        "subaccount_number": 0,
        "exchange_index": 0,
    }
    row.update(changes)
    return row


def seed_buy(
    coid="buy", *, quantity=Fraction(1), side="yes", gross="0.400000", fee="0.002000", age=0
):
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id=coid,
                ticker=TICKER,
                side=side,
                action="buy",
                strategy="motor_2_consensus",
                price_cents=40,
                status="filled",
                fill_cost_usd_fp=gross,
                fees_usd_fp=fee,
                fees_cents=1,
                commitment_origin_id=f"origin-{coid}",
                placed_at=NOW + timedelta(seconds=age),
                **filled_fields(quantity),
            )
        )
        session.commit()


def seed_sell(mode, *, side="yes", limit=60):
    if mode == "reduction":
        persist_reduction("buy", "sell", 1, limit)
    else:
        with models.get_session() as session:
            session.add(
                models.Trade(
                    client_order_id="sell",
                    ticker=TICKER,
                    side=side,
                    action="sell",
                    strategy="motor_3_clv",
                    price_cents=limit,
                    status="pending",
                    notes="clv_exit_intent",
                    **quantity_fields(1),
                )
            )
            session.commit()


def prepare(mode, *, side="yes", limit=60):
    seed_buy(side=side)
    seed_sell(mode, side=side, limit=limit)


def rows():
    with models.get_session() as session:
        return list(session.exec(select(models.Trade).order_by(models.Trade.id)))


def snapshot():
    return [row.model_dump() for row in rows()]


def update_sell(**changes):
    with models.get_session() as session:
        row = session.exec(select(models.Trade).where(models.Trade.client_order_id == "sell")).one()
        for field, value in changes.items():
            setattr(row, field, value)
        session.add(row)
        session.commit()


def finish(mode, quantity, execution, *, side="yes", limit=60):
    if mode == "reduction":
        finalize_reduction("sell", quantity, ORDER, execution=execution)
    else:
        Motor3ExitExecutor(object())._finalize_fill(
            "sell", TICKER, side, quantity, limit, ORDER, execution=execution
        )


async def collect(client, *, quantity=Fraction(1, 4), side="yes"):
    return await collect_exit_execution(
        client, order_id=ORDER, ticker=TICKER, side=side, quantity=quantity, subaccount=0
    )


@pytest.mark.parametrize("mode", ["m3", "reduction"])
@pytest.mark.parametrize("side", ["yes", "no"])
async def test_collect_finalize_partial_exact_price_and_fee_replay(mode, side):
    prepare(mode, side=side)
    row = provider_fill(side=side)
    unrelated = provider_fill(side=side, order_id="unrelated-order", fid="unrelated")
    client = GetOnlyClient([page(row, cursor="live-next"), page()], [page(unrelated, dict(row))])
    execution = await collect(client, side=side)
    assert client.calls[1][1]["cursor"] == "live-next"
    assert [source for source, _ in client.calls] == ["live", "live", "historical"]
    assert execution.count == Fraction(1, 4)
    assert execution.gross_usd == Fraction("0.175025")
    finish(mode, Fraction(1, 4), execution, side=side)
    sell = next(row for row in rows() if row.action == "sell")
    closed = next(row for row in rows() if row.action == "buy" and row.status == "settled")
    remaining = next(row for row in rows() if row.status == "filled")
    # Precio .7001 observado frente a límite .60; no sustituir por 60 ni truncar a70c.
    assert closed.pnl_usd_fp == "0.074402"
    assert sell.sale_proceeds_usd_fp == "0.175025"
    assert sell.fees_usd_fp == "0.000123" and sell.fees_cents == 1
    assert sell.fill_price_cents is None and sell.fill_cost_usd_fp is None
    assert sell.exit_fill_evidence_sha256 == execution.evidence_sha256
    assert sell.kalshi_order_id == ORDER and sell.status == "settled"
    assert confirmed_fill(sell) == confirmed_fill(closed) == Fraction(1, 4)
    assert closed.commitment_origin_id == remaining.commitment_origin_id == "origin-buy"
    assert remaining.fill_cost_usd_fp == "0.300000"
    assert remaining.fees_usd_fp == "0.001500" and open_quantity(remaining) == Fraction(3, 4)
    assert "exit_execution_observed" in closed.notes and "pnl_basis=observed" in closed.notes
    assert "exit_accounting_estimated" not in closed.notes
    before = snapshot()
    finish(mode, Fraction(1, 4), execution, side=side)
    assert snapshot() == before
    conflict = await collect(
        GetOnlyClient([page(provider_fill(side=side, fee="0.000124"))], [page()]), side=side
    )
    with pytest.raises(ValueError, match="REPLAY_CONFLICT"):
        finish(mode, Fraction(1, 4), conflict, side=side)
    assert snapshot() == before


@pytest.mark.parametrize("side", ["yes", "no"])
async def test_m3_fifo_two_origins_partial_remainder_conserves_income_and_fees(side):
    seed_buy("first", quantity=Fraction(1, 4), side=side, gross="0.100000", fee="0.000500")
    seed_buy("second", side=side, age=1)
    seed_sell("m3", side=side)
    fills = [
        provider_fill(side=side, price="0.650100"),
        provider_fill(side=side, price="0.700200", count="0.50", fee="0.000878", fid="f2"),
    ]
    execution = await collect(
        GetOnlyClient([page(fills[0])], [page(fills[1])]), quantity=Fraction(3, 4), side=side
    )
    finish("m3", Fraction(3, 4), execution, side=side)
    closed = [row for row in rows() if row.action == "buy" and row.status == "settled"]
    first = next(row for row in closed if row.commitment_origin_id == "origin-first")
    second = next(row for row in closed if row.commitment_origin_id == "origin-second")
    assert first.pnl_usd_fp == "0.070042" and second.pnl_usd_fp == "0.140082"
    # .512625 ingresos - .300000 coste BUY - .001500 fee BUY - .001001 fee SELL.
    assert sum(Fraction(row.pnl_usd_fp) for row in closed) == Fraction("0.210124")
    assert confirmed_fill(first) == Fraction(1, 4) and confirmed_fill(second) == Fraction(1, 2)
    remaining = next(row for row in rows() if row.status == "filled")
    assert (
        remaining.client_order_id == "second" and remaining.commitment_origin_id == "origin-second"
    )
    assert remaining.fill_cost_usd_fp == "0.200000" and remaining.fees_usd_fp == "0.001000"
    assert open_quantity(remaining) == Fraction(1, 2)
    sell = next(row for row in rows() if row.action == "sell")
    assert sell.sale_proceeds_usd_fp == "0.512625" and sell.fees_usd_fp == "0.001001"
    before = snapshot()
    finish("m3", Fraction(3, 4), execution, side=side)
    assert snapshot() == before


@pytest.mark.parametrize("mode", ["m3", "reduction"])
@pytest.mark.parametrize(
    "failure", [RuntimeError("fake-read-failed"), {"fills": [], "cursor": None}]
)
async def test_second_page_failure_does_not_finalize_or_write_ledger(mode, failure):
    prepare(mode)
    before = snapshot()
    client = GetOnlyClient([page(provider_fill(), cursor="next"), failure], [page()])
    with pytest.raises((RuntimeError, ValueError)):
        execution = await collect(client)
        finish(mode, Fraction(1, 4), execution)
    assert snapshot() == before
    assert len(client.calls) == 2 and client.pages["historical"] == [page()]


@pytest.mark.parametrize("mode", ["m3", "reduction"])
@pytest.mark.parametrize(
    "change",
    [
        {"fees_usd_fp": "0.000124", "fees_cents": 1},
        {"fees_usd_fp": "0.000123", "fees_cents": 2},
        {"fees_usd_fp": "NaN", "fees_cents": 1},
    ],
)
async def test_conflicting_known_fee_reverts_all_metadata_and_buy_writes(mode, change):
    prepare(mode)
    update_sell(filled_count_fp="0.25", **change)
    before = snapshot()
    execution = await collect(GetOnlyClient([page(provider_fill())], [page()]))
    with pytest.raises(ValueError):
        finish(mode, Fraction(1, 4), execution)
    assert snapshot() == before
    if mode == "reduction":
        assert next(row for row in rows() if row.action == "sell").notes == "reduction_for=buy"


@pytest.mark.parametrize("mode", ["m3", "reduction"])
@pytest.mark.parametrize(
    "change",
    [
        {"order_id": "wrong"},
        {"ticker": "wrong"},
        {"side": "no"},
        {"count": Fraction(1, 2)},
        {"subaccount": 1},
    ],
)
async def test_summary_must_match_persisted_sell_before_any_writes(mode, change):
    prepare(mode)
    execution = await collect(GetOnlyClient([page(provider_fill())], [page()]))
    before = snapshot()
    with pytest.raises(ValueError, match="IDENTITY_INVALID"):
        finish(mode, Fraction(1, 4), replace(execution, **change))
    assert snapshot() == before


@pytest.mark.parametrize("mode", ["m3", "reduction"])
async def test_settled_estimated_history_is_not_automatically_rewritten(mode):
    prepare(mode)
    finish(mode, Fraction(1, 4), None)
    before = snapshot()
    assert all(
        "exit_accounting_estimated" in row.notes for row in rows() if row.status == "settled"
    )
    execution = await collect(GetOnlyClient([page(provider_fill())], [page()]))
    with pytest.raises(ValueError, match="REPLAY_CONFLICT"):
        finish(mode, Fraction(1, 4), execution)
    assert snapshot() == before


@pytest.mark.parametrize("mode", ["m3", "reduction"])
@pytest.mark.parametrize("side", ["yes", "no"])
async def test_observed_zero_gross_is_valid_and_never_replaced_by_limit(mode, side):
    prepare(mode, side=side, limit=0)
    execution = await collect(
        GetOnlyClient([page(provider_fill(side=side, price="0", fee="0"))], [page()]), side=side
    )
    finish(mode, Fraction(1, 4), execution, side=side, limit=0)
    sell = next(row for row in rows() if row.action == "sell")
    closed = next(row for row in rows() if row.action == "buy" and row.status == "settled")
    assert sell.sale_proceeds_usd_fp == "0.000000" and sell.fill_price_cents == 0
    assert sell.fees_usd_fp == "0.000000" and closed.pnl_usd_fp == "-0.100500"


@pytest.mark.parametrize("source", ["live", "historical"])
async def test_collector_page_limit_and_cursor_loops_never_claim_complete(source):
    prepare("m3")
    before = snapshot()
    for bad_pages in (
        [page(cursor=f"next-{i}") for i in range(5)],
        [page(cursor="repeat"), page(cursor="repeat")],
        [{"fills": []}],
    ):
        client = GetOnlyClient(
            bad_pages if source == "live" else [page(provider_fill())],
            bad_pages if source == "historical" else [page()],
        )
        with pytest.raises(ValueError):
            await collect(client)
        assert len(client.calls) <= (5 if source == "live" else 6)
        assert snapshot() == before


async def test_historical_scope_and_duplicate_conflicts_reject_complete_collection():
    for row in (
        provider_fill(order_id="another-order", ticker="OTHER"),
        provider_fill(order_id="another-order", subaccount_number=1),
        provider_fill(fee="0.000124"),
    ):
        with pytest.raises(ValueError):
            await collect(GetOnlyClient([page(provider_fill())], [page(row)]))


async def test_late_fifo_failure_rolls_back_sell_and_already_processed_first_buy():
    seed_buy("first", quantity=Fraction(1, 4), gross="0.100000", fee="0.000500")
    seed_buy("second", gross="NaN", age=1)
    seed_sell("m3")
    execution = await collect(
        GetOnlyClient([page(provider_fill(count="0.75", price="0.70"))], [page()]),
        quantity=Fraction(3, 4),
    )
    before = snapshot()
    with pytest.raises(ValueError):
        finish("m3", Fraction(3, 4), execution)
    assert snapshot() == before


@pytest.mark.parametrize("mode", ["m3", "reduction"])
@pytest.mark.parametrize("side", ["yes", "no"])
async def test_known_exit_get_reconciliation_preserves_pause_and_closes_once(mode, side):
    prepare(mode, side=side)
    update_sell(kalshi_order_id=ORDER)
    models.engage_kill_switch("fixture: revisar después del cierre")
    pause_before = models.kill_switch_engaged()
    client = KnownOrderGetClient(
        terminal_order(side=side), [page(provider_fill(side=side))], [page()]
    )
    execution = await reconcile_known_exit(client, "sell")
    assert client.order_calls == [ORDER]
    assert [source for source, _ in client.calls] == ["live", "historical"]
    assert execution.gross_usd == Fraction("0.175025")
    sell = next(row for row in rows() if row.action == "sell")
    closed = next(row for row in rows() if row.action == "buy" and row.status == "settled")
    assert sell.sale_proceeds_usd_fp == "0.175025" and sell.status == "settled"
    assert closed.pnl_usd_fp == "0.074402"
    assert models.kill_switch_engaged() == pause_before
    before = snapshot()
    with pytest.raises(ValueError, match="REQUIRES_KNOWN_PENDING_SELL"):
        await reconcile_known_exit(client, "sell")
    assert snapshot() == before and client.order_calls == [ORDER]
    assert len(client.calls) == 2 and models.kill_switch_engaged() == pause_before


async def test_unidentified_ioc_intent_is_rejected_before_any_get():
    prepare("m3")
    before = snapshot()
    client = KnownOrderGetClient(terminal_order(), [page(provider_fill())], [page()])
    with pytest.raises(ValueError, match="REQUIRES_KNOWN_PENDING_SELL"):
        await reconcile_known_exit(client, "sell")
    assert snapshot() == before and client.order_calls == [] and client.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "resting", "remaining_count_fp": "0.75"},
        {"subaccount_number": 1},
        {"client_order_id": "another-intent"},
        {"side": "no"},
        {"outcome_side": "yes"},
        {"initial_count_fp": "2.00"},
    ],
)
async def test_known_exit_rejects_nonterminal_or_wrong_identity_before_fill_reads(changes):
    prepare("m3")
    update_sell(kalshi_order_id=ORDER)
    before = snapshot()
    client = KnownOrderGetClient(terminal_order(**changes), [page(provider_fill())], [page()])
    with pytest.raises(ValueError):
        await reconcile_known_exit(client, "sell")
    assert snapshot() == before and client.order_calls == [ORDER] and client.calls == []


async def test_known_exit_historical_failure_retains_pending_quantity_and_evidence():
    prepare("reduction")
    update_sell(kalshi_order_id=ORDER, filled_count_fp="0.25")
    before = snapshot()
    client = KnownOrderGetClient(
        terminal_order(), [page(provider_fill())], [page(cursor="more"), RuntimeError("fixture")]
    )
    with pytest.raises(RuntimeError):
        await reconcile_known_exit(client, "sell")
    assert snapshot() == before
    assert client.order_calls == [ORDER]
    assert [source for source, _ in client.calls] == ["live", "historical", "historical"]
