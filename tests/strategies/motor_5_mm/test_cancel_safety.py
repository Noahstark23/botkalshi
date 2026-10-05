"""Cancelaciones M5: la incertidumbre conserva reservas y dueño de la quote."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.strategies.motor_5_mm.order_state import (
    COUNT_QUARANTINE_NOTE,
    filled_count,
    parse_count,
    terminal_fill_count,
)
from src.strategies.motor_5_mm.quoter import QuoteSet


@pytest.fixture(autouse=True)
def _clear_locks():
    Motor5Executor._locks.clear()
    yield
    Motor5Executor._locks.clear()


class FakeClient:
    def __init__(self):
        self.placed = []
        self.cancelled = []
        self.cancel_responses = {}
        self.batch_response = {"orders": []}
        self.batch_error = None
        self.cancel_error = None

    async def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"order": {"order_id": f"K{len(self.placed)}"}}

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        if self.cancel_error:
            raise self.cancel_error
        return self.cancel_responses.get(order_id, {"order": {"order_id": order_id}})

    async def batch_cancel_orders(self, ids):
        if self.batch_error:
            raise self.batch_error
        return self.batch_response


def quote(*, bid=47, ask=53):
    return QuoteSet(ticker="T", fair_prob=0.5, bid_cents=bid, ask_cents=ask, size=10)


def rows():
    with get_session() as session:
        return list(session.exec(select(Trade)))


async def test_failed_batch_and_individual_cancel_preserve_pending_and_ownership():
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote())
    coids = executor.live_coids()
    client.batch_error = TimeoutError("batch timeout")
    client.cancel_error = TimeoutError("individual timeout")

    assert await executor.cancel_all("kill_switch") == 0
    assert executor.live_coids() == coids
    assert all(row.status == "pending" for row in rows())
    assert executor.corrupted == {"T"}


async def test_cancel_with_missing_count_does_not_release_or_replace():
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote())
    coids = executor.live_coids()
    client.cancel_responses["K1"] = {"order": {"order_id": "K1", "status": "canceled"}}

    assert await executor.sync_quotes(quote(bid=46, ask=54)) == "corrupted"
    assert executor.live_coids() == coids
    assert len(client.placed) == 2
    assert client.cancelled == ["K1"]
    assert all(row.status == "pending" for row in rows())


async def test_cancel_failure_while_removing_side_stops_other_side_placement():
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    client.cancel_error = TimeoutError("cancel timeout")

    assert await executor.sync_quotes(quote(bid=None, ask=54)) == "corrupted"
    assert len(client.placed) == 1
    assert executor.live_order_ids() == ["K1"]


async def test_ambiguous_first_placement_does_not_place_other_side():
    client = FakeClient()
    attempts = []

    async def place_timeout(**kwargs):
        attempts.append(kwargs)
        raise TimeoutError("unknown acceptance")

    client.place_order = place_timeout
    executor = Motor5Executor(client)
    assert await executor.sync_quotes(quote()) == "corrupted"
    assert len(attempts) == 1
    assert len(rows()) == 1


def terminal(order_id="K1", *, filled="0.00", status="canceled", **kwargs):
    return {"order_id": order_id, "status": status, "fill_count_fp": filled, **kwargs}


@pytest.mark.parametrize("invalid", ["0.005", "11.00", None])
async def test_terminal_unrepresentable_fill_survives_later_zero(invalid):
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    client.cancel_responses["K1"] = {"order": terminal(filled=invalid)}
    await executor.retire_ticker("T")
    assert rows()[0].notes.startswith(COUNT_QUARANTINE_NOTE)
    client.cancel_responses["K1"] = {"order": terminal(filled="0.00")}
    await executor.retire_ticker("T")
    assert rows()[0].status == "pending"
    assert executor.live_order_ids() == ["K1"]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("before_fill,after_fill", [("0.00", "11.00"), ("4.00", "0.00")])
async def test_real_client_preserves_rejected_count_before_retry_zero(
    batch, before_fill, after_fill
):
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    coid = rows()[0].client_order_id
    before = {
        "order_id": "K1",
        "client_order_id": coid,
        "ticker": "T",
        "subaccount_number": 0,
        "exchange_index": 0,
        "status": "resting",
        "fill_count_fp": before_fill,
        "initial_count_fp": "10.00",
        "remaining_count_fp": "10.00",
    }
    after = {
        **before,
        "status": "canceled",
        "remaining_count_fp": "0.00",
        "fill_count_fp": after_fill,
    }
    zero = {**after, "fill_count_fp": "0.00"}
    ack = {"order_id": "K1", "client_order_id": coid, "reduced_by": "10.00"}
    # Cliente auténtico, transporte falso: no constructor/signer/entorno ni cuenta.
    rest = object.__new__(KalshiRestClient)
    rest._write_throttle = MagicMock(acquire=AsyncMock())
    rest._request = AsyncMock(
        side_effect=[
            {"order": before},
            {"orders": [ack]} if batch else ack,
            {"order": after},
            {"order": zero},
        ]
    )
    executor._client = rest
    if batch:
        assert await executor.cancel_all("test") == 0
    else:
        await executor.retire_ticker("T")
        await executor.retire_ticker("T")
    assert rest._request.await_count == 4
    if after_fill == "11.00":
        assert rows()[0].notes.startswith(COUNT_QUARANTINE_NOTE)
    else:
        assert rows()[0].filled_count == 4
    assert rows()[0].status == "pending"
    assert executor.live_order_ids() == ["K1"]


@pytest.mark.parametrize("fill", ["0.50", "4.00"])
async def test_batch_quarantine_write_failure_aborts_before_fallback(fill):
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    client.batch_response = {
        "orders": [
            {
                "order_id": "K1",
                "error": {"code": "unconfirmed"},
                "observed_order": terminal(filled=fill),
            }
        ]
    }
    with (
        patch("src.strategies.motor_5_mm.executor.get_session", side_effect=OSError("disk")),
        pytest.raises(OSError, match="disk"),
    ):
        await executor.cancel_all("test")
    assert client.cancelled == []
    assert rows()[0].status == "pending"


@pytest.mark.parametrize("fill", ["11.00", "4.00"])
async def test_real_batch_keeps_early_bad_evidence_when_later_write_fails(fill):
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote())
    by_id = {row.kalshi_order_id: row for row in rows()}

    def observed(oid, *, status="resting", fill="0.00"):
        return {
            "order_id": oid,
            "client_order_id": by_id[oid].client_order_id,
            "ticker": "T",
            "subaccount_number": 0,
            "exchange_index": 0,
            "status": status,
            "fill_count_fp": fill,
            "initial_count_fp": "10.00",
            "remaining_count_fp": "10.00" if status == "resting" else "0.00",
        }

    rest = object.__new__(KalshiRestClient)
    rest._write_throttle = MagicMock(acquire=AsyncMock())
    rest._request = AsyncMock(
        side_effect=[
            {"order": observed("K1", status="canceled", fill=fill)},
            {"order": observed("K2")},
            OSError("later batch write failed"),
            {"order": observed("K1", status="canceled")},
            {"order": observed("K2", status="canceled")},
        ]
    )
    executor._client = rest
    assert await executor.cancel_all("test") == 1
    by_id = {row.kalshi_order_id: row for row in rows()}
    assert by_id["K1"].status == "pending"
    if fill == "11.00":
        assert by_id["K1"].notes.startswith(COUNT_QUARANTINE_NOTE)
    else:
        assert by_id["K1"].filled_count == 4
    assert by_id["K2"].status == "cancelled"
    assert executor.live_order_ids() == ["K1"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        terminal(status="resting"),
        terminal(status=[]),
        terminal(order_id="wrong-order"),
        terminal(client_order_id="wrong-client"),
        terminal(filled="1.005"),
        terminal(filled="-1.00"),
        terminal(filled="NaN"),
        terminal(filled="Infinity"),
        terminal(filled=True),
        terminal(filled="11.00"),
        terminal(filled="3.00", fill_count="0"),
        terminal(filled="0.00", status="executed"),
        terminal(filled="3.00", status="executed"),
        terminal(remaining_count_fp="1.00"),
        terminal(remaining_count_fp="0.00", remaining_count=1),
        terminal(initial_count_fp="9.00"),
    ],
)
async def test_invalid_cancel_payload_keeps_pending_and_live(payload):
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    original_coids = executor.live_coids()
    client.cancel_responses["K1"] = {"order": payload}

    assert await executor.sync_quotes(quote(bid=46, ask=None)) == "corrupted"
    assert executor.live_coids() == original_coids
    assert len(client.placed) == 1
    assert rows()[0].status == "pending"


@pytest.mark.parametrize(
    "filled,status", [("0.00", "canceled"), ("4.00", "canceled"), ("10.00", "executed")]
)
async def test_exact_terminal_fill_is_persisted_before_forgetting(filled, status):
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    client.cancel_responses["K1"] = {
        "order": terminal(filled=filled, status=status, remaining_count_fp="0.00")
    }

    assert await executor.sync_quotes(quote(bid=None, ask=None)) == "synced"
    assert executor.live_coids() == set()
    row = rows()[0]
    assert row.status == ("filled" if int(float(filled)) else "cancelled")
    assert row.filled_count == int(float(filled))


async def test_partial_batch_accounts_only_confirmed_order_and_its_fill():
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote())
    client.batch_response = {
        "orders": [
            {"order_id": "K1", "order": terminal(filled="4.00")},
            {"order_id": "K2", "error": {"code": "unavailable"}},
        ]
    }
    client.cancel_error = TimeoutError("fallback unavailable")

    assert await executor.cancel_all("shutdown") == 1
    assert executor.live_order_ids() == ["K2"]
    assert client.cancelled == ["K2"]
    by_id = {row.kalshi_order_id: row for row in rows()}
    assert by_id["K1"].status == "filled" and by_id["K1"].filled_count == 4
    assert by_id["K2"].status == "pending"


@pytest.mark.parametrize(
    "batch",
    [
        {"ids": ["K1"]},
        {"orders": [terminal(), terminal(filled="4.00")]},
        {"orders": [{"order_id": "K2", "order": terminal()}]},
        {"orders": [{"order_id": "K1", "order": terminal(), "error": {}}]},
    ],
)
async def test_ambiguous_batch_requires_independent_confirmation(batch):
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    client.batch_response = batch
    client.cancel_error = TimeoutError("no independent confirmation")

    assert await executor.cancel_all("kill_switch") == 0
    assert executor.live_order_ids() == ["K1"]
    assert rows()[0].status == "pending"


async def test_batch_timeout_can_be_confirmed_by_individual_response():
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    client.batch_error = TimeoutError("batch failed")
    client.cancel_responses["K1"] = {"order": terminal()}

    assert await executor.cancel_all("kill_switch") == 1
    assert executor.live_coids() == set()
    assert rows()[0].status == "cancelled"


async def test_db_failure_preserves_pending_and_live_even_after_terminal_response(monkeypatch):
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    coids = executor.live_coids()
    client.cancel_responses["K1"] = {"order": terminal()}

    def unavailable_db():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("src.strategies.motor_5_mm.executor.get_session", unavailable_db)
    assert await executor.sync_quotes(quote(bid=None, ask=None)) == "corrupted"
    assert executor.live_coids() == coids
    assert rows()[0].status == "pending"


async def test_forget_confirmed_only_removes_named_orders_without_mutating_ledger():
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote())
    buy_coid = executor._live["T"]["buy"].client_order_id
    executor.forget_confirmed({buy_coid, "m5mm-unknown"})
    assert executor.live_order_ids() == ["K2"]
    assert all(row.status == "pending" for row in rows())
    executor.forget_confirmed(executor.live_coids())
    assert executor._live == {}


async def test_managed_coids_excludes_corrupted_quotes_without_forgetting_ownership():
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote())
    await executor.sync_quotes(
        QuoteSet(ticker="HEALTHY", fair_prob=0.5, bid_cents=47, ask_cents=53, size=10)
    )
    owned = executor.live_coids()
    healthy = {live.client_order_id for live in executor._live["HEALTHY"].values()}
    assert executor.managed_coids() == owned

    client.cancel_error = TimeoutError("retire cannot confirm")
    await executor.retire_ticker("T")

    assert executor.corrupted == {"T"}
    assert executor.managed_coids() == healthy
    assert executor.live_coids() == owned
    assert all(row.status == "pending" for row in rows())


async def test_missing_order_id_retains_live_and_pending_without_network_cancel():
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    executor._live["T"]["buy"].kalshi_order_id = None
    assert await executor.cancel_all("shutdown") == 0
    assert len(executor.live_coids()) == 1
    assert executor.corrupted == {"T"}
    assert client.cancelled == []
    assert rows()[0].status == "pending"


async def test_stale_cancel_does_not_regress_a_previously_persisted_fill():
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    with get_session() as session:
        row = session.exec(select(Trade)).one()
        row.status = "filled"
        row.filled_count = 4
        session.add(row)
        session.commit()
    client.cancel_responses["K1"] = {"order": terminal()}

    assert await executor.sync_quotes(quote(bid=None, ask=None)) == "corrupted"
    assert executor.live_order_ids() == ["K1"]
    assert rows()[0].filled_count == 4
    assert rows()[0].status == "filled"


@pytest.mark.parametrize(
    "value", [None, True, "1.005", "-1", "NaN", "Infinity", "1e10000", "1e-1000000", [], {}]
)
def test_count_parser_never_truncates_or_accepts_unbounded_values(value):
    assert parse_count(value) is None


def test_count_parser_preserves_integer_precision_and_supports_legacy():
    assert parse_count("9007199254740993.00") == 9007199254740993
    assert filled_count({"fill_count": "4.00"}) == 4
    assert filled_count({"fill_count_fp": "4.00", "fill_count": 4}) == 4
    assert filled_count({"fill_count_fp": None, "fill_count": 4}) is None
    assert terminal_fill_count(terminal(filled="4.00"), order_id="K1", count=10) == 4


@pytest.mark.parametrize("filled", ["0.01", "0.50", "1.23"])
async def test_fractional_cancel_fill_prevents_replacement_from_old_inventory(filled):
    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    client.cancel_responses["K1"] = {"order": terminal(filled=filled)}
    assert await executor.sync_quotes(quote(bid=46, ask=54)) == "risk_blocked"
    assert len(client.placed) == 1
    assert executor.live_coids() == set()
    row = rows()[0]
    assert row.status == "filled" and row.filled_count_fp == filled
    assert row.filled_count is None


@pytest.mark.parametrize("filled", ["0.01", "0.50", "1.23"])
async def test_fractional_evidence_from_failed_cancel_remains_pending_and_exact(filled):
    from src.clients.kalshi_rest import KalshiOrderConfirmationError

    client = FakeClient()
    executor = Motor5Executor(client)
    await executor.sync_quotes(quote(ask=None))
    client.cancel_error = KalshiOrderConfirmationError(
        "unconfirmed", observed_order=terminal(filled=filled)
    )
    await executor.retire_ticker("T")
    row = rows()[0]
    assert row.status == "pending" and row.filled_count_fp == filled
    assert row.filled_count is None
    assert COUNT_QUARANTINE_NOTE not in row.notes
    assert executor.live_order_ids() == ["K1"]
    assert executor.corrupted == {"T"}
    assert executor._mm_open_cost_usd() == 4.70
