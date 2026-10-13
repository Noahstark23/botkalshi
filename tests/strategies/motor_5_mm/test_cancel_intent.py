"""Intents pre-DELETE con SQLite temporal y cliente ficticio, sin cuenta."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, select

from src.storage.models import Trade, get_session
from src.strategies.motor_5_mm.cancel_intent import record_cancel_requests
from src.strategies.motor_5_mm.executor import LiveOrder, Motor5Executor
from src.strategies.motor_5_mm.order_state import COUNT_QUARANTINE_NOTE
from src.strategies.motor_5_mm.reconciler import MMReconciler


def seed(coid="m5mm-a", oid="K1", **changes):
    with get_session() as session:
        row = Trade(
            **(
                {
                    "client_order_id": coid,
                    "kalshi_order_id": oid,
                    "ticker": "T",
                    "side": "yes",
                    "action": "buy",
                    "count": 10,
                    "price_cents": 47,
                    "strategy": "motor_5_mm",
                    "status": "pending",
                }
                | changes
            )
        )
        session.add(row)
        session.commit()


def rows():
    with get_session() as session:
        return {row.client_order_id: row for row in session.exec(select(Trade))}


def order(coid="m5mm-a", oid="K1", **changes):
    return {
        "client_order_id": coid,
        "order_id": oid,
        "ticker": "T",
        "status": "canceled",
        "fill_count_fp": "0.00",
    } | changes


def executor(client):
    ex = Motor5Executor(client)
    ex._live = {"T": {"buy": LiveOrder("m5mm-a", "K1", "buy", 47, 10)}}
    return ex


@pytest.mark.parametrize("error", [TimeoutError, asyncio.CancelledError])
async def test_request_survives_lost_response_and_restart(error):
    seed()
    client = AsyncMock()

    async def lost_response(_oid):
        row = rows()["m5mm-a"]  # Otra Session ve commit antes de red.
        assert row.cancel_requested_at_ms and row.status == "pending"
        raise error()

    client.cancel_order.side_effect = lost_response
    ex = executor(client)
    if error is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await ex._cancel("T", ex._live["T"]["buy"])
    else:
        assert not await ex._cancel("T", ex._live["T"]["buy"])
    stamp = rows()["m5mm-a"].cancel_requested_at_ms
    assert rows()["m5mm-a"].status == "pending" and ex.live_coids() == {"m5mm-a"}
    # Otra instancia no usa un resting posterior para borrar el DELETE incierto.
    client.get_all_orders.return_value = [order(status="resting")]
    report = await MMReconciler(client).reconcile(live_coids={"m5mm-a"})
    assert not report.complete and report.still_resting == 0
    assert rows()["m5mm-a"].cancel_requested_at_ms == stamp
    assert client.cancel_order.await_count == 1
    # Ausencia tampoco resuelve; sólo el terminal confirmado cambia status.
    client.get_all_orders.return_value = []
    assert not (await MMReconciler(client).reconcile()).complete
    assert rows()["m5mm-a"].status == "pending"
    client.get_all_orders.return_value = [order()]
    assert (await MMReconciler(client).reconcile()).complete
    assert rows()["m5mm-a"].status == "cancelled"
    assert rows()["m5mm-a"].cancel_requested_at_ms == stamp


async def test_batch_targets_are_marked_before_network_and_keep_history():
    seed()
    seed("m5mm-b", "K2", action="sell")
    client = AsyncMock()
    ex = executor(client)
    ex._live["T"]["sell"] = LiveOrder("m5mm-b", "K2", "sell", 47, 10)
    stamps = {}

    async def batch(ids):
        assert ids == ["K1", "K2"]
        for coid, row in rows().items():
            assert row.status == "pending" and row.cancel_requested_at_ms
            stamps[coid] = row.cancel_requested_at_ms
        return {"orders": [order(), order("m5mm-b", "K2")]}

    client.batch_cancel_orders.side_effect = batch
    assert await ex.cancel_all("simulated") == 2
    assert ex.live_coids() == set()
    assert {coid: row.cancel_requested_at_ms for coid, row in rows().items()} == stamps
    assert all(row.status == "cancelled" for row in rows().values())
    client.cancel_order.assert_not_awaited()


async def test_orphan_request_is_durable_before_network():
    seed()
    client = AsyncMock()
    client.get_all_orders.return_value = [order(status="resting")]

    async def cancel(_oid):
        assert rows()["m5mm-a"].cancel_requested_at_ms
        return {"order": order()}

    client.cancel_order.side_effect = cancel
    report = await MMReconciler(client).reconcile(live_coids=set())
    assert report.complete and report.orphan_cancelled == 1
    assert rows()["m5mm-a"].status == "cancelled"


@pytest.mark.parametrize("route", ["single", "batch", "orphan"])
async def test_failed_intent_commit_blocks_replacement_but_attempts_protective_delete(
    monkeypatch, route
):
    seed()
    client = AsyncMock()
    client.get_all_orders.return_value = [order(status="resting")]
    client.batch_cancel_orders.side_effect = TimeoutError("lost")
    client.cancel_order.side_effect = TimeoutError("lost")
    real_commit = Session.commit

    def failing_commit(session):
        if any(isinstance(row, Trade) and row.cancel_requested_at_ms for row in session.dirty):
            raise RuntimeError("simulated commit failure")
        return real_commit(session)

    monkeypatch.setattr(Session, "commit", failing_commit)
    ex = executor(client)
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        if route == "single":
            await ex._cancel("T", ex._live["T"]["buy"])
        elif route == "batch":
            await ex.cancel_all("simulated")
        else:
            await MMReconciler(client).reconcile(live_coids=set())
    if route == "single":
        client.cancel_order.assert_not_awaited()
        client.batch_cancel_orders.assert_not_awaited()
    else:
        client.cancel_order.assert_awaited_once_with("K1")
        if route == "batch":
            client.batch_cancel_orders.assert_awaited_once_with(["K1"])
    from src.strategies.motor_5_mm.incidents import incident_status

    assert incident_status()[0]
    assert rows()["m5mm-a"].cancel_requested_at_ms is None
    assert rows()["m5mm-a"].status == "pending"


def test_batch_identity_failure_rolls_back_all_and_retry_preserves_original_time():
    seed()
    seed("m5mm-b", "K2")
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        record_cancel_requests([("m5mm-a", "K1"), ("m5mm-b", "wrong")], now_ms=100)
    assert all(row.cancel_requested_at_ms is None for row in rows().values())
    record_cancel_requests([("m5mm-a", "K1")], now_ms=100)
    record_cancel_requests([("m5mm-a", "K1")], now_ms=200)
    assert rows()["m5mm-a"].cancel_requested_at_ms == 100
    with pytest.raises(ValueError, match="CLOCK"):
        record_cancel_requests([("m5mm-a", "K1")], now_ms=99)
    assert rows()["m5mm-a"].cancel_requested_at_ms == 100


@pytest.mark.parametrize("changes", [{"strategy": "motor_2_consensus"}, {"status": "filled"}])
def test_other_strategy_or_terminal_is_not_a_cancel_target(changes):
    seed(**changes)
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        record_cancel_requests([("m5mm-a", "K1")])
    assert rows()["m5mm-a"].cancel_requested_at_ms is None


@pytest.mark.parametrize("targets", [[], [("", "K1")], [("m5mm-a", "")], [("m5mm-a", "K1")] * 2])
def test_invalid_or_duplicate_targets_do_not_write(targets):
    seed()
    with pytest.raises(ValueError):
        record_cancel_requests(targets)
    assert rows()["m5mm-a"].cancel_requested_at_ms is None


async def test_one_terminal_target_does_not_prevent_retiring_other_quote():
    seed(status="cancelled")
    seed("m5mm-b", "K2", action="sell")
    client = AsyncMock()
    ex = executor(client)
    ex._live["T"]["sell"] = LiveOrder("m5mm-b", "K2", "sell", 47, 10)
    client.batch_cancel_orders.return_value = {"orders": [order("m5mm-b", "K2")]}
    assert await ex.cancel_all("kill_switch") == 1
    client.batch_cancel_orders.assert_awaited_once_with(["K2"])
    client.cancel_order.assert_not_awaited()
    assert ex.live_coids() == {"m5mm-a"}
    assert rows()["m5mm-a"].cancel_requested_at_ms is None
    assert rows()["m5mm-b"].status == "cancelled"
    assert rows()["m5mm-b"].cancel_requested_at_ms


async def test_clock_regression_does_not_block_protective_delete(monkeypatch):
    from src.strategies.motor_5_mm import cancel_intent

    seed(cancel_requested_at_ms=200)
    monkeypatch.setattr(cancel_intent.time, "time_ns", lambda: 100_000_000)
    client = AsyncMock()
    client.batch_cancel_orders.return_value = {"orders": [order()]}
    with pytest.raises(ValueError, match="CLOCK"):
        await executor(client).cancel_all("shutdown")
    client.batch_cancel_orders.assert_awaited_once_with(["K1"])
    assert rows()["m5mm-a"].cancel_requested_at_ms == 200
    assert rows()["m5mm-a"].status == "cancelled"


async def test_dead_database_still_attempts_all_known_cancels_and_blocks_restart(monkeypatch):
    from src.strategies.motor_5_mm import incidents

    owner = incidents.begin_execution_session()
    seed()
    seed("m5mm-b", "K2", action="sell")
    client = AsyncMock()
    client.batch_cancel_orders.side_effect = TimeoutError("lost batch")
    client.cancel_order.side_effect = TimeoutError("lost individual")
    ex = executor(client)
    ex._live["T"]["sell"] = LiveOrder("m5mm-b", "K2", "sell", 47, 10)
    with monkeypatch.context() as failure:

        def fail_commit(_session):
            raise RuntimeError("simulated database unavailable")

        failure.setattr(Session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="database unavailable"):
            await ex.cancel_all("shutdown")
        client.batch_cancel_orders.assert_awaited_once_with(["K1", "K2"])
        assert [call.args for call in client.cancel_order.await_args_list] == [("K1",), ("K2",)]
        assert incidents.incident_status()[0]
    assert all(row.status == "pending" for row in rows().values())
    assert ex.live_coids() == {"m5mm-a", "m5mm-b"}
    # Recuperar DB / otra instancia no convierte la sesión abandonada en sana.
    monkeypatch.setattr(incidents, "_current_execution_owner", None)
    assert incidents.incident_status()[0]
    with pytest.raises(RuntimeError, match="pending"):
        incidents.finish_execution_session(owner)


async def test_orphan_intent_failure_does_not_skip_other_orphan(monkeypatch):
    seed()
    seed("m5mm-b", "K2", action="sell")
    client = AsyncMock()
    client.get_all_orders.return_value = [
        order(status="resting"),
        order("m5mm-b", "K2", status="resting"),
    ]
    client.cancel_order.side_effect = TimeoutError("lost")
    real_commit = Session.commit

    def fail_intent_commit(session):
        if any(isinstance(row, Trade) and row.cancel_requested_at_ms for row in session.dirty):
            raise RuntimeError("simulated intent write failure")
        return real_commit(session)

    monkeypatch.setattr(Session, "commit", fail_intent_commit)
    with pytest.raises(RuntimeError, match="intent write failure"):
        await MMReconciler(client).reconcile(live_coids=set())
    assert [call.args for call in client.cancel_order.await_args_list] == [("K1",), ("K2",)]
    assert all(row.status == "pending" for row in rows().values())


@pytest.mark.parametrize("observed_count", ["3.00", "10.50"])
@pytest.mark.parametrize("batch_error", [True, False])
async def test_failed_batch_observation_write_cannot_release_reservation_with_later_zero(
    monkeypatch, observed_count, batch_error
):
    seed()
    client = AsyncMock()
    error = RuntimeError("lost batch")
    error.observed_orders = [order(status="resting", fill_count_fp=observed_count)]
    if batch_error:
        client.batch_cancel_orders.side_effect = error
    else:
        client.batch_cancel_orders.return_value = {
            "orders": [
                {"order_id": "K1", "error": "unconfirmed", "observed_orders": error.observed_orders}
            ]
        }
    client.cancel_order.return_value = {"order": order()}
    real_commit = Session.commit
    failed = False

    def fail_observation_once(session):
        nonlocal failed
        if not failed and any(
            isinstance(row, Trade)
            and (row.filled_count == 3 or COUNT_QUARANTINE_NOTE in (row.notes or ""))
            for row in session.dirty
        ):
            failed = True
            raise RuntimeError("simulated lost observed fill")
        return real_commit(session)

    monkeypatch.setattr(Session, "commit", fail_observation_once)
    ex = executor(client)
    with pytest.raises(RuntimeError, match="simulated lost observed fill"):
        await ex.cancel_all("shutdown")
    assert failed
    client.cancel_order.assert_awaited_once_with("K1")
    assert rows()["m5mm-a"].status == "pending"
    assert ex.live_coids() == {"m5mm-a"}


async def test_deferred_orphan_error_does_not_skip_phantom_retirement(monkeypatch):
    seed()
    client = AsyncMock()
    client.get_all_orders.return_value = [
        order(status="resting"),
        order("m5mm-ghost", "G1", status="resting"),
    ]

    async def terminal_response(oid):
        return {"order": order("m5mm-a" if oid == "K1" else "m5mm-ghost", oid)}

    client.cancel_order.side_effect = terminal_response
    real_commit = Session.commit

    def fail_intent(session):
        if any(isinstance(row, Trade) and row.cancel_requested_at_ms for row in session.dirty):
            raise RuntimeError("simulated intent failure")
        return real_commit(session)

    monkeypatch.setattr(Session, "commit", fail_intent)
    with pytest.raises(RuntimeError, match="intent failure"):
        await MMReconciler(client).reconcile(live_coids=set())
    assert [call.args for call in client.cancel_order.await_args_list] == [("K1",), ("G1",)]
    assert set(rows()) == {"m5mm-a"}


async def test_failed_phantom_latch_does_not_prevent_retiring_all_phantoms(monkeypatch):
    from src.strategies.motor_5_mm import incidents

    incidents.begin_execution_session()
    client = AsyncMock()
    client.get_all_orders.return_value = [
        order("m5mm-ghost1", "G1", status="resting"),
        order("m5mm-ghost2", "G2", status="resting"),
    ]

    async def terminal_response(oid):
        return {"order": order("m5mm-ghost" + oid[-1], oid)}

    client.cancel_order.side_effect = terminal_response
    with monkeypatch.context() as failure:

        def fail_commit(_session):
            raise RuntimeError("simulated DB failure")

        failure.setattr(Session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="DB failure"):
            await MMReconciler(client).reconcile(live_coids=set())
    assert [call.args for call in client.cancel_order.await_args_list] == [("G1",), ("G2",)]
    assert rows() == {}
    assert incidents.incident_status()[0]
