"""Read-only doubles exercise shared marks without constructing a live client."""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time

from src.monitoring.position_marks import PositionMarkRefresher
from src.risk.manager import RiskManager

NOW = datetime(2026, 10, 8, 18, tzinfo=UTC)
TICKER = "KXMLBGAME-26OCT08AAABBB-AAA"
EVENT = "KXMLBGAME-26OCT08AAABBB"


@pytest.fixture
def evidence(monkeypatch):
    monkeypatch.setattr(RiskManager, "_marks", {})
    client = SimpleNamespace(
        get_market=AsyncMock(return_value={"market": {"ticker": TICKER, "event_ticker": EVENT}}),
        get_orderbook=AsyncMock(
            return_value={
                "orderbook_fp": {
                    "yes_dollars": [["0.40", "0.25"]],
                    "no_dollars": [["0.50", "2.00"]],
                }
            }
        ),
    )
    fee = SimpleNamespace(
        series_ticker="KXMLBGAME",
        event_ticker=EVENT,
        observed_at=NOW,
        multiplier=Fraction(1, 2),
    )
    policy = SimpleNamespace(observe=AsyncMock(return_value=fee))
    worker = PositionMarkRefresher(
        client, ticker_provider=lambda: [TICKER], fee_policy=policy, clock=lambda: NOW
    )
    with freeze_time(NOW, real_asyncio=True):
        yield worker, client, policy, fee


async def test_records_bid_ask_and_fee_for_both_sides_even_fractional_depth(evidence):
    worker, client, policy, fee = evidence
    assert await worker.refresh() == {"observed": [TICKER], "unavailable": []}
    yes, no = (RiskManager._marks[(TICKER, side)] for side in ("yes", "no"))
    assert (yes.bid_cents, yes.ask_cents) == (40, 50)
    assert (no.bid_cents, no.ask_cents) == (50, 60)
    assert yes.fee_multiplier == Fraction(1, 2)
    assert yes.observed_at == NOW.replace(tzinfo=None)
    policy.observe.assert_awaited_once_with(TICKER, event_ticker=EVENT)


@pytest.mark.parametrize(
    "fault",
    ["book", "fee", "identity", "crossed", "missing_side", "nan", "stale_fee", "future_fee"],
)
async def test_missing_or_contradictory_evidence_invalidates_previous_marks(evidence, fault):
    worker, client, policy, fee = evidence
    await worker.refresh()
    if fault == "book":
        client.get_orderbook.side_effect = RuntimeError("offline injected failure")
    elif fault == "fee":
        policy.observe.side_effect = RuntimeError("offline injected failure")
    elif fault == "identity":
        client.get_market.return_value["market"]["ticker"] = "another"
    elif fault == "crossed":
        client.get_orderbook.return_value["orderbook_fp"]["no_dollars"] = [["0.70", "1"]]
    elif fault == "missing_side":
        client.get_orderbook.return_value["orderbook_fp"]["no_dollars"] = []
    elif fault == "nan":
        client.get_orderbook.return_value["orderbook_fp"]["yes_dollars"] = [["0.40", "NaN"]]
    else:
        fee.observed_at = NOW + timedelta(seconds=1 if fault == "future_fee" else -31)
    result = await worker.refresh()
    assert result == {"observed": [], "unavailable": [TICKER]}
    assert not RiskManager._marks


async def test_inventory_failure_clears_marks_and_fails_required_service(evidence):
    worker, *_ = evidence
    await worker.refresh()

    def unreadable():
        raise OSError("offline database failure")

    worker._tickers = unreadable
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        await worker.refresh()
    assert not RiskManager._marks


@pytest.mark.parametrize("tickers", [[f"KXMLBGAME-E-{i}" for i in range(9)], ["OTHER-E-X"]])
async def test_unsupported_inventory_does_not_issue_reads(evidence, tickers):
    worker, client, *_ = evidence
    worker._tickers = lambda: tickers
    with pytest.raises(RuntimeError, match="inventory unavailable"):
        await worker.refresh()
    client.get_market.assert_not_awaited()
    client.get_orderbook.assert_not_awaited()


async def test_resolved_inventory_drops_marks(evidence):
    worker, *_ = evidence
    await worker.refresh()
    worker._tickers = lambda: []
    assert await worker.refresh() == {"observed": [], "unavailable": []}
    assert not RiskManager._marks


async def test_fee_io_does_not_rejuvenate_old_book(evidence):
    worker, client, policy, fee = evidence
    moments = iter([NOW - timedelta(seconds=31), NOW])
    worker._clock = lambda: next(moments)
    assert (await worker.refresh())["unavailable"] == [TICKER]
    assert not RiskManager._marks
