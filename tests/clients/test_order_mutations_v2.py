"""Contrato V2 offline: cancelar exige evidencia observada, nunca fills inferidos."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.clients.kalshi_rest as rest


@pytest.fixture
def client():
    with (
        patch.object(rest, "get_settings", return_value=MagicMock(TRADING_ENABLED=True)),
        patch.object(rest, "KalshiSigner"),
    ):
        value = rest.KalshiRestClient()
    value._request = AsyncMock()
    value._write_throttle.acquire = AsyncMock()
    return value


def order(oid="a", **changes):
    result = {
        "order_id": oid,
        "client_order_id": f"coid-{oid}",
        "ticker": f"T-{oid}",
        "subaccount_number": 2,
        "exchange_index": 3,
        "status": "resting",
        "fill_count_fp": "0.00",
        "remaining_count_fp": "10.00",
        "initial_count_fp": "10.00",
    }
    result.update(changes)
    return result


def terminal(oid="a", **changes):
    result = order(oid, status="canceled", remaining_count_fp="0.00")
    result.update(changes)
    return result


def ack(oid="a", **changes):
    result = {"order_id": oid, "client_order_id": f"coid-{oid}", "reduced_by": "10.00"}
    result.update(changes)
    return result


async def test_get_order_uses_current_read_path(client):
    observed = {"order": order()}
    client._request.return_value = observed
    assert await client.get_order("a") == observed
    client._request.assert_awaited_once_with("GET", "/portfolio/orders/a")


@pytest.mark.parametrize("response", [{}, {"order": None}, {"order": order("wrong")}])
async def test_get_order_rejects_unidentified_response(client, response):
    client._request.return_value = response
    with pytest.raises(RuntimeError):
        await client.get_order("a")


@pytest.mark.parametrize("oid", ["", "../a", "a?x=1", None, 1])
async def test_invalid_id_never_reaches_network(client, oid):
    with pytest.raises(ValueError):
        await client.cancel_order(oid)
    client._request.assert_not_awaited()


async def test_single_cancel_routes_from_read_and_returns_actual_fractional_fill(client):
    after = terminal(fill_count_fp="0.40")
    client.settings.TRADING_ENABLED = False
    client._request.side_effect = [{"order": order()}, ack(reduced_by="9.60"), {"order": after}]
    assert await client.cancel_order("a") == {"order": after}
    calls = client._request.await_args_list
    assert [call.args[0] for call in calls] == ["GET", "DELETE", "GET"]
    assert calls[1].args == ("DELETE", "/portfolio/events/orders/a")
    assert calls[1].kwargs == {
        "params": {"market_ticker": "T-a", "subaccount": 2, "exchange_index": 3}
    }
    assert "fill_count" not in after


@pytest.mark.parametrize(
    "changes",
    [
        {"ticker": ""},
        {"subaccount_number": None},
        {"subaccount_number": True},
        {"exchange_index": -1},
        {"exchange_index": "3"},
        {"client_order_id": ""},
    ],
)
async def test_invalid_observed_routing_never_cancels(client, changes):
    client._request.return_value = {"order": order(**changes)}
    with pytest.raises(RuntimeError):
        await client.cancel_order("a")
    assert client._request.await_count == 1


@pytest.mark.parametrize(
    "response",
    [
        ack("wrong"),
        ack(client_order_id="wrong"),
        {},
        ack(reduced_by="NaN"),
        ack(reduced_by="-1"),
        ack(error={"code": "oops"}),
    ],
)
async def test_bad_ack_is_not_confirmation(client, response):
    client._request.side_effect = [{"order": order()}, response]
    with pytest.raises(RuntimeError):
        await client.cancel_order("a")
    assert client._request.await_count == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"order_id": "wrong"},
        {"client_order_id": "wrong"},
        {"ticker": "wrong"},
        {"subaccount_number": 0},
        {"exchange_index": 0},
        {"status": "resting"},
        {"fill_count_fp": None},
        {"fill_count_fp": "NaN"},
        {"fill_count_fp": "Infinity"},
        {"fill_count_fp": "-1"},
        {"fill_count_fp": "11"},
        {"remaining_count_fp": "1"},
        {"initial_count_fp": "11"},
        {"fill_count": 1},
    ],
)
async def test_post_cancel_read_must_be_terminal_consistent_and_identical(client, changes):
    client._request.side_effect = [{"order": order()}, ack(), {"order": terminal(**changes)}]
    with pytest.raises(RuntimeError):
        await client.cancel_order("a")


async def test_final_fill_cannot_go_backwards(client):
    client._request.side_effect = [
        {"order": order(fill_count_fp="2", remaining_count_fp="8")},
        ack(reduced_by="8"),
        {"order": terminal(fill_count_fp="1")},
    ]
    with pytest.raises(RuntimeError):
        await client.cancel_order("a")


async def test_already_terminal_order_needs_no_write(client):
    client._request.return_value = {"order": terminal(fill_count_fp="2")}
    assert await client.cancel_order("a") == client._request.return_value
    assert client._request.await_count == 1


async def test_post_cancel_read_failure_never_returns_success(client):
    client._request.side_effect = [{"order": order()}, ack(), OSError("offline")]
    with pytest.raises(OSError):
        await client.cancel_order("a")


async def test_batch_uses_v2_body_and_returns_only_confirmed_orders(client):
    client._request.side_effect = [
        {"order": order("a")},
        {"order": order("b")},
        {"orders": [ack("b"), ack("a")]},
        {"order": terminal("a", fill_count_fp="2")},
        {"order": terminal("b")},
    ]
    response = await client.batch_cancel_orders(["a", "b"])
    write = client._request.await_args_list[2]
    assert write.args == ("DELETE", "/portfolio/events/orders/batched")
    assert write.kwargs == {
        "json": {
            "orders": [
                {"order_id": oid, "market_ticker": f"T-{oid}", "subaccount": 2, "exchange_index": 3}
                for oid in ("a", "b")
            ]
        }
    }
    assert response == {
        "orders": [
            {"order_id": "a", "order": terminal("a", fill_count_fp="2")},
            {"order_id": "b", "order": terminal("b")},
        ]
    }


@pytest.mark.parametrize(
    "items",
    [
        [ack("a")],
        [ack("a"), ack("b"), ack("b")],
        [ack("a"), ack("b", error={"code": "failed"})],
        [ack("a"), ack("b", client_order_id="wrong")],
    ],
)
async def test_batch_partial_missing_duplicate_error_do_not_confirm(client, items):
    client._request.side_effect = [
        {"order": order("a")},
        {"order": order("b")},
        {"orders": items},
        {"order": terminal("a")},
    ]
    response = await client.batch_cancel_orders(["a", "b"])
    assert response["orders"][0]["order"] == terminal("a")
    assert "error" in response["orders"][1]
    assert "order" not in response["orders"][1]
    assert client._request.await_count == 4


async def test_batch_keeps_other_orders_when_one_route_is_unknown(client):
    client._request.side_effect = [
        OSError("offline"),
        {"order": order("b")},
        {"orders": [ack("b")]},
        {"order": terminal("b")},
    ]
    response = await client.batch_cancel_orders(["a", "b"])
    assert "error" in response["orders"][0]
    assert response["orders"][1]["order"] == terminal("b")
    assert len(client._request.await_args_list[2].kwargs["json"]["orders"]) == 1


@pytest.mark.parametrize("ids", [["a", "a"], [str(i) for i in range(21)]])
async def test_batch_input_is_bounded_and_unique(client, ids):
    with pytest.raises(ValueError):
        await client.batch_cancel_orders(ids)
    client._request.assert_not_awaited()


@pytest.mark.parametrize("method", ["cancel_order", "batch_cancel_orders"])
async def test_total_cancel_deadline_includes_initial_read(client, monkeypatch, method):
    monkeypatch.setattr(rest, "_ORDER_MUTATION_TIMEOUT_SECONDS", 0.01)

    async def slow(*args, **kwargs):
        await asyncio.sleep(60)

    client._request.side_effect = slow
    with pytest.raises(TimeoutError):
        await getattr(client, method)("a" if method == "cancel_order" else ["a"])


async def test_batch_create_uses_v2_and_conservative_local_size_cap(client):
    bodies = [rest.build_order_body(ticker="T", side="yes", action="buy", count=1, yes_price=40)]
    client._request.return_value = {"orders": []}
    assert await client.batch_create_orders(bodies) == {"orders": []}
    client._request.assert_awaited_once_with(
        "POST", "/portfolio/events/orders/batched", json={"orders": bodies}
    )
    client._request.reset_mock()
    with pytest.raises(ValueError):
        await client.batch_create_orders(bodies * 11)
    client._request.assert_not_awaited()


@pytest.mark.parametrize("method", ["cancel_order", "batch_cancel_orders"])
async def test_outer_cleanup_cancellation_propagates_after_delete(client, method):
    reading_after_delete = asyncio.Event()

    async def respond(verb, path, **kwargs):
        if verb == "DELETE":
            return {"orders": [ack()]} if method == "batch_cancel_orders" else ack()
        if client._request.await_count == 1:
            return {"order": order()}
        reading_after_delete.set()
        await asyncio.Future()

    client._request.side_effect = respond
    task = asyncio.create_task(getattr(client, method)("a" if method == "cancel_order" else ["a"]))
    await asyncio.wait_for(reading_after_delete.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert client._request.await_count == 3


@pytest.mark.parametrize("method", ["cancel_order", "batch_cancel_orders"])
async def test_total_deadline_also_covers_post_delete_read(client, monkeypatch, method):
    monkeypatch.setattr(rest, "_ORDER_MUTATION_TIMEOUT_SECONDS", 0.01)

    async def respond(verb, path, **kwargs):
        if verb == "DELETE":
            return {"orders": [ack()]} if method == "batch_cancel_orders" else ack()
        if client._request.await_count == 1:
            return {"order": order()}
        await asyncio.Future()

    client._request.side_effect = respond
    with pytest.raises(TimeoutError):
        await getattr(client, method)("a" if method == "cancel_order" else ["a"])
    assert client._request.await_count == 3


@pytest.mark.parametrize("items", [[ack("unknown")], [None], [ack()] * 21])
async def test_unknown_or_unbounded_batch_response_is_never_confirmed(client, items):
    client._request.side_effect = [{"order": order()}, {"orders": items}]
    with pytest.raises(rest.KalshiOrderConfirmationError):
        await client.batch_cancel_orders(["a"])
    assert client._request.await_count == 2


@pytest.mark.parametrize("already_terminal", [False, True])
@pytest.mark.parametrize(
    "counts", [{"fill_count_fp": "11.00"}, {"fill_count_fp": "0.00", "fill_count": 1}]
)
async def test_single_preserves_correlated_invalid_fill_as_evidence(
    client, already_terminal, counts
):
    observed = terminal(**counts, private_marker="NEVER-LOG-THIS-PAYLOAD")
    client._request.side_effect = (
        [{"order": observed}]
        if already_terminal
        else [{"order": order()}, ack(), {"order": observed}]
    )
    with pytest.raises(rest.KalshiOrderConfirmationError) as caught:
        await client.cancel_order("a")
    assert caught.value.observed_order == observed
    assert "NEVER-LOG-THIS-PAYLOAD" not in str(caught.value)
    assert "fill_count" not in str(caught.value)
    assert "NEVER-LOG-THIS-PAYLOAD" not in repr(caught.value)
    assert client._request.await_count == (1 if already_terminal else 3)


@pytest.mark.parametrize("already_terminal", [False, True])
@pytest.mark.parametrize(
    "counts", [{"fill_count_fp": "11.00"}, {"fill_count_fp": "0.00", "fill_count": 1}]
)
async def test_batch_error_preserves_evidence_without_success_or_payload_logs(
    client, already_terminal, counts
):
    observed = terminal(**counts, private_marker="NEVER-LOG-THIS-PAYLOAD")
    client._request.side_effect = (
        [{"order": observed}]
        if already_terminal
        else [{"order": order()}, {"orders": [ack()]}, {"order": observed}]
    )
    messages = []
    sink = rest.logger.add(messages.append, format="{message}")
    try:
        response = await client.batch_cancel_orders(["a"])
    finally:
        rest.logger.remove(sink)
    assert response == {
        "orders": [
            {
                "order_id": "a",
                "error": {"code": "cancel_unconfirmed"},
                "observed_order": observed,
                "observed_orders": [observed if already_terminal else order(), observed],
            }
        ]
    }
    assert "NEVER-LOG-THIS-PAYLOAD" not in "".join(messages)
    assert "fill_count" not in "".join(messages)


@pytest.mark.parametrize(
    "changes", [{"order_id": "wrong"}, {"client_order_id": "wrong"}, {"ticker": "wrong"}]
)
async def test_unrelated_post_read_never_becomes_observed_evidence(client, changes):
    client._request.side_effect = [
        {"order": order()},
        ack(),
        {"order": terminal(fill_count_fp="11", **changes)},
    ]
    with pytest.raises(rest.KalshiOrderConfirmationError) as caught:
        await client.cancel_order("a")
    assert caught.value.observed_order is None


@pytest.mark.parametrize(
    "items", [[], [ack("a", client_order_id="wrong")], [ack("a", reduced_by=None)]]
)
async def test_missing_or_invalid_batch_ack_does_not_manufacture_observed_order(client, items):
    client._request.side_effect = [{"order": order()}, {"orders": items}]
    response = await client.batch_cancel_orders(["a"])
    assert response == {
        "orders": [
            {"order_id": "a", "error": {"code": "cancel_unconfirmed"}, "observed_orders": [order()]}
        ]
    }
    assert client._request.await_count == 2


@pytest.mark.parametrize("failure_type", [OSError, TimeoutError, asyncio.CancelledError])
async def test_batch_keeps_earlier_invalid_order_on_same_escaping_exception(client, failure_type):
    observed = terminal("a", fill_count_fp="11", private_marker="NEVER-LOG-THIS-PAYLOAD")
    original = failure_type("batch interrupted")
    client._request.side_effect = [{"order": observed}, {"order": order("b")}, original]
    messages = []
    sink = rest.logger.add(messages.append, format="{message}")
    try:
        with pytest.raises(failure_type) as caught:
            await client.batch_cancel_orders(["a", "b"])
    finally:
        rest.logger.remove(sink)
    assert caught.value is original
    assert observed in caught.value.observed_orders
    assert order("b") in caught.value.observed_orders
    assert len(caught.value.observed_orders) == 2
    assert "NEVER-LOG-THIS-PAYLOAD" not in str(caught.value)
    assert "NEVER-LOG-THIS-PAYLOAD" not in repr(caught.value)
    assert "NEVER-LOG-THIS-PAYLOAD" not in "".join(messages)


@pytest.mark.parametrize("interrupt", ["deadline", "cancel"])
async def test_batch_preserves_evidence_through_real_deadline_or_external_cancel(
    client, monkeypatch, interrupt
):
    observed = terminal("a", fill_count_fp="11")
    delete_started = asyncio.Event()

    async def respond(verb, path, **kwargs):
        if verb == "DELETE":
            delete_started.set()
            await asyncio.Future()
        return {"order": observed if path.endswith("/a") else order("b")}

    client._request.side_effect = respond
    if interrupt == "deadline":
        monkeypatch.setattr(rest, "_ORDER_MUTATION_TIMEOUT_SECONDS", 0.01)
        with pytest.raises(TimeoutError) as caught:
            await client.batch_cancel_orders(["a", "b"])
    else:
        task = asyncio.create_task(client.batch_cancel_orders(["a", "b"]))
        await asyncio.wait_for(delete_started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
    assert observed in caught.value.observed_orders
    assert order("b") in caught.value.observed_orders
    assert len(caught.value.observed_orders) == 2
    assert client._request.await_count == 3


async def test_batch_does_not_attach_unrelated_or_ack_only_data_to_escaping_error(client):
    original = OSError("batch interrupted")
    client._request.side_effect = [
        {"order": terminal("foreign", fill_count_fp="11")},
        {"order": order("b")},
        original,
    ]
    with pytest.raises(OSError) as caught:
        await client.batch_cancel_orders(["a", "b"])
    assert caught.value is original
    assert caught.value.observed_orders == [order("b")]


@pytest.mark.parametrize("stage", ["delete", "ack", "read"])
async def test_single_cancel_keeps_positive_floor_when_later_step_fails(client, stage):
    before = order(fill_count_fp="4", remaining_count_fp="6")
    original = OSError("offline")
    client._request.side_effect = {
        "delete": [{"order": before}, original],
        "ack": [{"order": before}, ack("wrong")],
        "read": [{"order": before}, ack(reduced_by="6"), original],
    }[stage]
    with pytest.raises((OSError, rest.KalshiOrderConfirmationError)) as caught:
        await client.cancel_order("a")
    assert caught.value.observed_orders == [before]
    if stage != "ack":
        assert caught.value is original


async def test_single_cancel_keeps_both_positive_before_and_regressing_after(client):
    before = order(fill_count_fp="4", remaining_count_fp="6")
    after = terminal(fill_count_fp="0")
    client._request.side_effect = [{"order": before}, ack(reduced_by="6"), {"order": after}]
    with pytest.raises(rest.KalshiOrderConfirmationError) as caught:
        await client.cancel_order("a")
    assert caught.value.observed_orders == [before, after]
    assert caught.value.observed_order == after


async def test_batch_keeps_valid_early_terminal_fill_when_later_write_fails(client):
    before = terminal("a", fill_count_fp="4")
    later = order("b")
    original = OSError("offline")
    client._request.side_effect = [{"order": before}, {"order": later}, original]
    with pytest.raises(OSError) as caught:
        await client.batch_cancel_orders(["a", "b"])
    assert caught.value is original
    assert before in caught.value.observed_orders
    assert later in caught.value.observed_orders
    assert len(caught.value.observed_orders) <= 60


async def test_batch_normal_error_keeps_both_positive_floor_and_regression(client):
    before = order(fill_count_fp="4", remaining_count_fp="6")
    after = terminal(fill_count_fp="0")
    client._request.side_effect = [
        {"order": before},
        {"orders": [ack(reduced_by="6")]},
        {"order": after},
    ]
    response = await client.batch_cancel_orders(["a"])
    item = response["orders"][0]
    assert "error" in item and "order" not in item
    assert item["observed_orders"] == [before, after]
    assert item["observed_order"] == after


@pytest.mark.parametrize("method", ["cancel_order", "batch_cancel_orders"])
@pytest.mark.parametrize(
    "route", [{"exchange_index": None}, {"subaccount_number": None}, {"ticker": ""}]
)
async def test_positive_floor_survives_invalid_routing_without_write(client, method, route):
    before = order(fill_count_fp="4", remaining_count_fp="6", **route)
    client._request.return_value = {"order": before}
    if method == "cancel_order":
        with pytest.raises(rest.KalshiOrderConfirmationError) as caught:
            await client.cancel_order("a")
        assert caught.value.observed_orders == [before]
    else:
        response = await client.batch_cancel_orders(["a"])
        item = response["orders"][0]
        assert item["observed_orders"] == [before]
        assert "error" in item and "order" not in item
    assert client._request.await_count == 1
    client._request.assert_awaited_once_with("GET", "/portfolio/orders/a")
