"""El probe demo no puede redirigirse a producción ni operar órdenes ajenas."""

import asyncio
import json
import os
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from scripts import demo_execution_probe as probe


def client():
    obj = object.__new__(probe.DemoOnlyClient)
    obj.base_url = probe.DEMO_ROOT
    obj.settings = SimpleNamespace(KALSHI_ENV="demo", rest_url=probe.DEMO_ROOT)
    obj._client = SimpleNamespace(base_url=probe.DEMO_ROOT)
    obj.state = {"ticker": "KXTEST-DEMO", "client_order_id": "demo-readiness-test"}
    obj.allow_create = True
    obj.create_attempted = False
    obj.owned_order_ids = {"owned"}
    return obj


def body():
    return {
        "ticker": "KXTEST-DEMO",
        "client_order_id": "demo-readiness-test",
        "count": "1",
        "price": "0.01",
        "side": "bid",
        "post_only": True,
        "time_in_force": "good_till_canceled",
    }


def full_order(state, **overrides):
    return {
        "order_id": "own-order",
        "client_order_id": state["client_order_id"],
        "ticker": state["ticker"],
        "outcome_side": "yes",
        "book_side": "bid",
        "initial_count_fp": "1.00",
        "fill_count_fp": "0.00",
        "remaining_count_fp": "0.00",
        "status": "canceled",
        **overrides,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["base_url", "settings", "transport"])
async def test_production_host_rejected_before_transport(target):
    obj = client()
    production = "https://external-api.kalshi.com/trade-api/v2"
    if target == "base_url":
        obj.base_url = production
    elif target == "settings":
        obj.settings.KALSHI_ENV = "production"
    else:
        obj._client.base_url = production
    with patch.object(probe.KalshiRestClient, "_request", new_callable=AsyncMock) as network:
        with pytest.raises(PermissionError):
            await obj._request("GET", "/portfolio/balance")
        network.assert_not_called()


def test_constructor_refuses_production_before_reading_secret():
    settings = SimpleNamespace(KALSHI_ENV="production", rest_url=probe.DEMO_ROOT)
    with (
        patch.object(probe, "get_settings", return_value=settings),
        patch.object(probe.KalshiRestClient, "__init__") as parent,
        pytest.raises(PermissionError),
    ):
        probe.DemoOnlyClient({}, allow_create=True)
    parent.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [429, 500, "timeout"])
async def test_ambiguous_create_consumes_budget_and_cannot_repeat(failure):
    obj = client()
    obj.signer = SimpleNamespace(sign=lambda *_: {})
    requests = []

    def transport(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("respuesta perdida")
        return httpx.Response(failure, json={"error": "fixture"})

    async with httpx.AsyncClient(
        base_url=probe.DEMO_ROOT, transport=httpx.MockTransport(transport)
    ) as network:
        obj._client = network
        with pytest.raises((RuntimeError, httpx.ReadTimeout)):
            await obj._request("POST", "/portfolio/events/orders", json=body())
        with pytest.raises(PermissionError):
            await obj._request("POST", "/portfolio/events/orders", json=body())
    assert len(requests) == 1
    assert requests[0].url.host == "external-api.demo.kalshi.co"


@pytest.mark.asyncio
async def test_create_stream_has_total_deadline_and_no_retry():
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            await asyncio.sleep(10)
            yield b"}"

    obj = client()
    obj.signer = SimpleNamespace(sign=lambda *_: {})
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(200, stream=SlowStream())

    async with httpx.AsyncClient(
        base_url=probe.DEMO_ROOT, transport=httpx.MockTransport(transport)
    ) as network:
        obj._client = network
        with patch.object(probe, "CREATE_TIMEOUT_SECONDS", 0.01), pytest.raises(TimeoutError):
            await obj._request("POST", "/portfolio/events/orders", json=body())
    assert len(requests) == 1
    assert obj.create_attempted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("count", "2"), ("price", "0.02"), ("side", "ask"), ("post_only", False)],
)
async def test_order_cannot_exceed_fixed_budget(field, value):
    payload = body()
    payload[field] = value
    with (
        patch.object(probe.KalshiRestClient, "_request", new_callable=AsyncMock) as network,
        pytest.raises(PermissionError),
    ):
        await client()._request("POST", "/portfolio/events/orders", json=payload)
    network.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_only_owned_order_and_no_batch_or_arbitrary_endpoint():
    obj = client()
    with patch.object(probe.KalshiRestClient, "_request", new_callable=AsyncMock) as network:
        for method, path in (
            ("DELETE", "/portfolio/events/orders/foreign"),
            ("DELETE", "/portfolio/events/orders/batched"),
            ("POST", "/withdrawals"),
            ("GET", "/portfolio/orders/foreign"),
        ):
            with pytest.raises(PermissionError):
                await obj._request(method, path)
        network.assert_not_called()
        await obj._request("DELETE", "/portfolio/events/orders/owned")
        assert network.await_count == 1


@pytest.mark.parametrize(
    "raw", [None, True, "NaN", "Infinity", "-1", "2", "0.001", "1e-2", " 0.25", "０.２５"]
)
def test_invalid_quantity_not_reported_as_zero(raw):
    with pytest.raises(ValueError):
        probe.count({"fill_count_fp": raw}, "fill_count")


def test_fraction_and_nullable_legacy_preserved():
    assert str(probe.count({"fill_count_fp": "0.25", "fill_count": None}, "fill_count")) == "0.25"
    with pytest.raises(ValueError):
        probe.count({"fill_count_fp": "0.25", "fill_count": 0}, "fill_count")


@pytest.mark.asyncio
async def test_absent_order_after_ambiguous_create_stays_unconfirmed(tmp_path):
    fake = SimpleNamespace(get_all_orders=AsyncMock(return_value=[]))
    state = {"client_order_id": "demo-readiness-test", "ticker": "KXTEST-DEMO"}
    path = tmp_path / "state.json"
    assert await probe.reconcile(fake, state, path) is None
    assert state["status"] == "UNCONFIRMED_NO_RETRY"
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
@pytest.mark.parametrize("filled,remaining", [("0.00", "0.00"), ("0.50", "0.75")])
async def test_regressive_or_excessive_counts_never_certify_empty_position(
    tmp_path, filled, remaining
):
    state = {
        "client_order_id": "demo-readiness-test",
        "ticker": "KXTEST-DEMO",
        "filled_count": "0.25",
        "order_id": "own-order",
    }
    order = full_order(state, fill_count_fp=filled, remaining_count_fp=remaining)
    fake = SimpleNamespace(get_all_orders=AsyncMock(return_value=[order]), owned_order_ids=set())
    assert await probe.reconcile(fake, state, tmp_path / "state.json") is None
    assert state["status"] == "UNCONFIRMED_NO_RETRY"
    assert state["position_may_remain"]
    assert probe.Decimal(state["filled_count"]) >= probe.Decimal("0.25")


@pytest.mark.asyncio
async def test_existing_intent_requires_explicit_recovery_without_network(tmp_path):
    path = tmp_path / "state.json"
    probe.save_state(path, {"status": "INTENT_BEFORE_NETWORK"})
    args = Namespace(ticker="KXTEST-DEMO", state=path, recover=False, execute=True)
    with patch.object(probe, "DemoOnlyClient") as cls, pytest.raises(ValueError):
        await probe.run_probe(args)
    cls.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("fill", ["0.00", "0.25"])
@pytest.mark.parametrize("cancel_race", [False, "response", "exception"])
async def test_lifecycle_restarts_and_reports_partial_fill_without_erasing_it(
    tmp_path, fill, cancel_race
):
    orders = []
    clients = []

    class FakeClient:
        def __init__(self, state, *, allow_create):
            self.state = state
            self.allow_create = allow_create
            self.owned_order_ids = set()
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def get_balance(self, **_):
            return {"balance": 10000}

        async def get_all_orders(self, **_):
            return [dict(o) for o in orders]

        async def get_market(self, _):
            return {"market": {"status": "active"}}

        async def place_order(self, **kwargs):
            assert self.allow_create
            # La intención está en disco antes del envío.
            import json

            saved = json.loads((tmp_path / "state.json").read_text())
            assert saved["status"] == "INTENT_BEFORE_NETWORK"
            orders.append(
                {
                    "order_id": "own-order",
                    "client_order_id": kwargs["client_order_id"],
                    "ticker": kwargs["ticker"],
                    "outcome_side": "yes",
                    "book_side": "bid",
                    "initial_count_fp": "1.00",
                    "status": "resting",
                    "fill_count_fp": fill,
                    "remaining_count_fp": str(1 - probe.Decimal(fill)),
                }
            )
            return {"order": dict(orders[0])}

        async def cancel_order(self, oid):
            assert oid in self.owned_order_ids
            orders[0].update(status="canceled", remaining_count_fp="0.00")
            observed = dict(orders[0])
            if cancel_race:
                observed["fill_count_fp"] = "0.50"
            if cancel_race == "exception":
                error = RuntimeError("cancel incierto")
                error.observed_orders = [observed]
                raise error
            return {"order": observed}

    args = Namespace(
        ticker="KXTEST-DEMO", state=tmp_path / "state.json", execute=True, recover=False
    )
    with patch.object(probe, "DemoOnlyClient", FakeClient):
        result = await probe.run_probe(args)
    assert len(clients) == 2
    assert not clients[1].allow_create
    if cancel_race:
        assert result["status"] == "UNCONFIRMED_NO_RETRY"
        assert result["filled_count"] == "0.50"
        assert result["position_may_remain"] is True
        return
    assert result["status"] == "TERMINAL_CONFIRMED"
    assert result["filled_count"] == fill
    assert result["position_may_remain"] is (fill != "0.00")
    assert result["partial_fill_observed"] is (fill != "0.00")
    assert result["runner_integration_validated"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "executed"},
        {"status": "executed", "fill_count_fp": "0.25"},
        {"outcome_side": "no"},
        {"book_side": "ask"},
        {"outcome_side": None},
        {"action": "sell"},
        {"side": "no"},
        {"initial_count_fp": "0.25"},
        {"initial_count_fp": None},
        {"initial_count": 0},
        {"subaccount_number": 1},
        {"status": "resting", "remaining_count_fp": "0.00"},
    ],
)
async def test_read_order_contract_never_certifies_inconsistent_terminal(tmp_path, overrides):
    state = {"client_order_id": "demo-readiness-test", "ticker": "KXTEST-DEMO"}
    fake = SimpleNamespace(
        get_all_orders=AsyncMock(return_value=[full_order(state, **overrides)]),
        owned_order_ids=set(),
    )
    assert await probe.reconcile(fake, state, tmp_path / "state.json") is None
    assert state["status"] == "UNCONFIRMED_NO_RETRY"
    assert state["position_may_remain"] is True
    # No se borra una contradicción si luego un GET parece sano.
    fake.get_all_orders.return_value = [full_order(state)]
    assert await probe.reconcile(fake, state, tmp_path / "state.json") is None


@pytest.mark.asyncio
async def test_executed_one_contract_is_confirmable(tmp_path):
    state = {"client_order_id": "demo-readiness-test", "ticker": "KXTEST-DEMO"}
    order = full_order(state, status="executed", fill_count_fp="1.00")
    fake = SimpleNamespace(get_all_orders=AsyncMock(return_value=[order]), owned_order_ids=set())
    assert await probe.reconcile(fake, state, tmp_path / "state.json") == order
    assert state["filled_count"] == "1.00" and state["position_may_remain"]


@pytest.mark.asyncio
async def test_duplicate_snapshot_preserves_correlated_fill_floor(tmp_path):
    state = {"client_order_id": "demo-readiness-test", "ticker": "KXTEST-DEMO"}
    order = full_order(state, fill_count_fp="0.25")
    fake = SimpleNamespace(
        get_all_orders=AsyncMock(return_value=[order, order]), owned_order_ids=set()
    )
    assert await probe.reconcile(fake, state, tmp_path / "state.json") is None
    assert state["filled_count"] == "0.25"
    assert state["position_may_remain"] and state["invalid_order_observed"]


@pytest.mark.asyncio
async def test_flat_v2_create_fill_is_a_floor_for_following_get(tmp_path):
    state = {"client_order_id": "demo-readiness-test", "ticker": "KXTEST-DEMO"}
    probe.preserve_create_response(
        {"order_id": "own-order", "fill_count": "0.25", "remaining_count": "0.75", "ts_ms": 1},
        state,
    )
    assert state["filled_count"] == "0.25"
    fake = SimpleNamespace(
        get_all_orders=AsyncMock(return_value=[full_order(state)]), owned_order_ids=set()
    )
    assert await probe.reconcile(fake, state, tmp_path / "state.json") is None
    assert state["filled_count"] == "0.25"
    assert state["position_may_remain"]


def test_invalid_remaining_in_create_does_not_erase_known_fill():
    state = {"client_order_id": "demo-readiness-test", "ticker": "KXTEST-DEMO"}
    with pytest.raises(ValueError):
        probe.preserve_create_response(
            {"order_id": "own-order", "fill_count": "0.25", "remaining_count": None}, state
        )
    assert state["filled_count"] == "0.25" and state["position_may_remain"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("filled_count", "NaN"),
        ("filled_count", None),
        ("filled_count", "2"),
        ("environment", "production"),
        ("count_contradiction", "false"),
    ],
)
async def test_corrupt_journal_blocks_before_constructing_client(tmp_path, field, value):
    state = {
        "schema": "kalshi-demo-probe-v1",
        "environment": "demo",
        "ticker": "KXTEST-DEMO",
        "client_order_id": "demo-readiness-12345678-1234-4234-8234-123456789abc",
        "status": "UNCONFIRMED_NO_RETRY",
        "real_money_orders": 0,
        field: value,
    }
    path = tmp_path / "state.json"
    probe.save_state(path, state)
    args = Namespace(ticker=state["ticker"], state=path, recover=True, execute=False)
    with patch.object(probe, "DemoOnlyClient") as cls, pytest.raises(ValueError):
        await probe.run_probe(args)
    cls.assert_not_called()


@pytest.mark.asyncio
async def test_inherited_pagination_pins_primary_subaccount_in_every_request():
    obj = client()
    obj.signer = SimpleNamespace(sign=lambda *_: {})
    requests = []

    def transport(request):
        requests.append(request)
        return httpx.Response(
            200, json={"orders": [], "cursor": "next" if len(requests) == 1 else ""}
        )

    async with httpx.AsyncClient(
        base_url=probe.DEMO_ROOT, transport=httpx.MockTransport(transport)
    ) as network:
        obj._client = network
        assert await obj.get_all_orders(ticker=obj.state["ticker"]) == []
    assert len(requests) == 2
    assert all(request.url.params["subaccount"] == "0" for request in requests)


@pytest.mark.parametrize("ticker", ["KXTEST-DEMO", "KXTEMPCHIHS-26OCT0702-T65.99"])
def test_main_real_client_contract_with_temporary_rsa_and_mock_transport(
    tmp_path, monkeypatch, capsys, ticker
):
    """CLI completa y cliente heredado: las firmas/métodos reales nunca salen a red."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from src.utils import config

    credentials = tmp_path / ".config/botkalshi/demo"
    credentials.mkdir(parents=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (credentials / "key_id.txt").write_text("offline-demo-key")
    (credentials / "kalshi_private_key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setattr(
        probe.Settings, "model_config", {**probe.Settings.model_config, "env_file": None}
    )
    monkeypatch.setattr(probe.logger, "remove", lambda: None)
    requests = []
    orders = []

    def transport(request):
        requests.append(request)
        assert request.url.host == "external-api.demo.kalshi.co"
        assert request.headers.get("KALSHI-ACCESS-SIGNATURE")
        path = request.url.path.removeprefix("/trade-api/v2")
        if path == "/portfolio/balance":
            return httpx.Response(200, json={"balance": 10000})
        if path == "/portfolio/orders":
            assert request.url.params["subaccount"] == "0"
            return httpx.Response(200, json={"orders": orders, "cursor": ""})
        if path == f"/markets/{ticker}":
            return httpx.Response(200, json={"market": {"ticker": ticker, "status": "active"}})
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["count"] == "1" and payload["price"] == "0.01"
            orders.append(
                full_order(
                    payload,
                    status="resting",
                    remaining_count_fp="1.00",
                    subaccount_number=0,
                    exchange_index=0,
                    fill_count=None,
                    remaining_count=None,
                    initial_count=None,
                )
            )
            return httpx.Response(
                201,
                json={
                    "order_id": "own-order",
                    "fill_count": "0.00",
                    "remaining_count": "1.00",
                    "ts_ms": 1,
                },
            )
        if request.method == "DELETE":
            assert path == "/portfolio/events/orders/own-order"
            orders[0].update(status="canceled", remaining_count_fp="0.00")
            return httpx.Response(200, json={"order_id": "own-order", "reduced_by": "1.00"})
        if path == "/portfolio/orders/own-order":
            return httpx.Response(200, json={"order": dict(orders[0])})
        raise AssertionError("Request inesperada")

    original_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return original_client(*args, **kwargs, transport=httpx.MockTransport(transport))

    monkeypatch.setattr(probe.httpx, "AsyncClient", factory)
    state_path = tmp_path / "state.json"
    base_args = ["probe", "--ticker", ticker, "--state", str(state_path)]
    with patch.dict(os.environ, {}, clear=True):
        monkeypatch.setattr(sys, "argv", [*base_args, "--execute"])
        assert probe.main() == 0
        result = json.loads(capsys.readouterr().out)
        assert result["status"] == "TERMINAL_CONFIRMED"
        assert result["filled_count"] == "0.00"
        assert not result["position_may_remain"]
        monkeypatch.setattr(sys, "argv", [*base_args, "--recover"])
        assert probe.main() == 0
    assert sum(request.method == "POST" for request in requests) == 1
    assert sum(request.method == "DELETE" for request in requests) == 1
