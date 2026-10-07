"""Sonda IOC: exclusivamente MockTransport, diario privado y entorno temporal."""

import asyncio
import json
import os
import subprocess
import sys
import time
from argparse import Namespace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest

from scripts import demo_ioc_partial_probe as probe


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    denied = Mock(side_effect=AssertionError("network forbidden"))
    for target in (
        "socket.socket.connect",
        "socket.socket.connect_ex",
        "socket.create_connection",
        "socket.getaddrinfo",
    ):
        monkeypatch.setattr(target, denied)
    yield
    denied.assert_not_called()


def settings():
    return SimpleNamespace(
        KALSHI_ENV="demo",
        rest_url=probe.DEMO_ROOT,
        TRADING_ENABLED=True,
        KALSHI_PRIVATE_KEY_PATH="not-a-real-key",
        KALSHI_API_KEY_ID="fake-key",
    )


def args(path, *, execute=True, recover=False):
    return Namespace(state=path, execute=execute, recover=recover)


class Venue:
    def __init__(self, path, **changes):
        self.path = path
        self.calls = []
        self.order = None
        self.quantity = "1.00"
        self.book_quantity = "1.00"
        self.fee = "0.020000"
        self.debit = "0.33"
        self.missing = False
        self.timeout = False
        self.resting = False
        self.regressive = False
        self.foreign_position = False
        self.foreign_resting = False
        self.fills_late = False
        self.fee_override = None
        self.override_type = None
        self.__dict__.update(changes)

    def factory(self, state, path, *, settings, allow_create):
        return probe.DemoIOCClient(
            state,
            path,
            settings=settings,
            allow_create=allow_create,
            signer=SimpleNamespace(sign=lambda *_: {}),
            transport=httpx.MockTransport(self.handle),
        )

    def observed(self):
        fill = "0.00" if self.regressive else self.quantity
        return {
            **self.order,
            "initial_count_fp": "2.00",
            "fill_count_fp": fill,
            "remaining_count_fp": str(2 - Decimal(fill)) if self.resting else "0.00",
            "status": "resting" if self.resting else "canceled",
        }

    def handle(self, request):
        path = request.url.path.removeprefix("/trade-api/v2")
        self.calls.append((request.method, path))
        if path.startswith("/portfolio/") and request.method == "GET":
            assert request.url.params["subaccount"] == "0"
        if request.method == "POST":
            saved = json.loads(self.path.read_text())
            assert saved["create_consumed"] and saved["status"] == "INTENT_BEFORE_NETWORK"
            assert self.path.stat().st_mode & 0o777 == 0o600
            body = json.loads(request.content)
            assert body == probe.order_body(saved)
            self.order = {
                "order_id": "own",
                "client_order_id": body["client_order_id"],
                "ticker": probe.TICKER,
                "outcome_side": "yes",
                "book_side": "bid",
                "subaccount_number": 0,
                "exchange_index": 0,
            }
            if self.timeout:
                raise httpx.ReadTimeout("fake-key NEVER print secret body")
            return httpx.Response(
                201,
                json={
                    "order_id": "own",
                    "fill_count_fp": self.quantity,
                    "remaining_count_fp": "0.00",
                },
            )
        if request.method == "DELETE":
            assert path == "/portfolio/events/orders/own"
            assert request.url.params["subaccount"] == "0"
            self.resting = False
            return httpx.Response(200, json={"order_id": "own", "reduced_by": "1.00"})
        if path == "/portfolio/orders":
            orders = [] if not self.order or self.missing else [self.observed()]
            if not self.order and self.foreign_resting:
                orders = [{"order_id": "foreign", "status": "resting"}]
            return httpx.Response(200, json={"orders": orders, "cursor": ""})
        if path == "/portfolio/orders/own":
            return httpx.Response(200, json={"order": self.observed()})
        if path == "/portfolio/balance":
            balance = Decimal(200) - (Decimal(self.debit) if self.order else 0)
            return httpx.Response(200, json={"balance_dollars": format(balance, ".4f")})
        if path == "/portfolio/positions":
            rows = (
                [{"ticker": probe.TICKER, "position_fp": self.quantity}]
                if self.order and Decimal(self.quantity)
                else []
            )
            if self.foreign_position:
                rows.append({"ticker": "FOREIGN", "position_fp": "1.00"})
            return httpx.Response(200, json={"market_positions": rows, "cursor": ""})
        if path == "/portfolio/fills":
            assert request.url.params["order_id"] == "own"
            rows = (
                []
                if self.fills_late or not Decimal(self.quantity)
                else [
                    {
                        "fill_id": "fill1",
                        "order_id": "own",
                        "ticker": probe.TICKER,
                        "outcome_side": "yes",
                        "book_side": "bid",
                        "side": "yes",
                        "action": "buy",
                        "count_fp": self.quantity,
                        "yes_price_dollars": "0.3100",
                        "fee_cost": self.fee,
                        "subaccount_number": 0,
                    }
                ]
            )
            return httpx.Response(200, json={"fills": rows, "cursor": ""})
        if path.endswith("/orderbook"):
            return httpx.Response(
                200,
                json={
                    "orderbook_fp": {
                        "yes_dollars": [["0.29", "96.00"]],
                        "no_dollars": [["0.63", "20.00"], ["0.69", self.book_quantity]],
                    }
                },
            )
        if path == f"/markets/{probe.TICKER}":
            return httpx.Response(
                200,
                json={
                    "market": {
                        "ticker": probe.TICKER,
                        "event_ticker": probe.EVENT,
                        "status": "active",
                    }
                },
            )
        if path == f"/series/{probe.SERIES}":
            return httpx.Response(
                200,
                json={
                    "series": {"ticker": probe.SERIES, "fee_type": "quadratic", "fee_multiplier": 1}
                },
            )
        if path == f"/events/{probe.EVENT}":
            return httpx.Response(
                200,
                json={
                    "event": {
                        "event_ticker": probe.EVENT,
                        "series_ticker": probe.SERIES,
                        "fee_type_override": self.override_type,
                        "fee_multiplier_override": self.fee_override,
                    }
                },
            )
        raise AssertionError("unexpected fake route")


async def test_default_plan_does_not_construct_client_or_touch_files(tmp_path):
    factory = Mock(side_effect=AssertionError("no client in plan"))
    result = await probe.run_probe(args(tmp_path / "journal", execute=False), factory=factory)
    assert result["status"] == "PLAN_ONLY" and result["budget_cents"] == 66
    assert not list(tmp_path.iterdir())
    factory.assert_not_called()


@pytest.mark.parametrize(
    "quantity,fee,debit,partial",
    [
        ("0.00", "0.00", "0.00", False),
        ("0.25", "0.010000", "0.087500", True),
        ("1.00", "0.020000", "0.33", True),
        ("2.00", "0.030000", "0.65", False),
    ],
)
async def test_exact_fill_cost_and_restart_never_reposts(tmp_path, quantity, fee, debit, partial):
    path = tmp_path / "journal.json"
    venue = Venue(path, quantity=quantity, fee=fee, debit=debit)
    result = await probe.run_probe(args(path), settings=settings(), factory=venue.factory)
    assert result["status"] == "TERMINAL_CONFIRMED" and result["account_reconciled"]
    assert result["terminal_confirmed"] and result["partial_fill_observed"] is partial
    assert Decimal(result["filled_count"]) == Decimal(quantity)
    assert Decimal(result["fills_gross_usd"]) + Decimal(result["fills_fees_usd"]) == Decimal(debit)
    recovered = await probe.run_probe(
        args(path, execute=False, recover=True), settings=settings(), factory=venue.factory
    )
    assert recovered["status"] == "TERMINAL_CONFIRMED"
    assert sum(method == "POST" for method, _ in venue.calls) == 1
    with pytest.raises(ValueError):
        await probe.run_probe(args(path), settings=settings(), factory=venue.factory)


async def test_missing_after_timeout_recovers_without_new_post(tmp_path):
    path = tmp_path / "journal"
    venue = Venue(path, timeout=True, missing=True)
    first = await probe.run_probe(args(path), settings=settings(), factory=venue.factory)
    assert first["status"] == "UNCONFIRMED_NO_RETRY" and first["position_may_remain"]
    assert first["filled_count_is_floor"]
    assert first["pending_reason"] == "ORDER_NOT_OBSERVED_AFTER_CONSUMED_CREATE"
    assert first["last_run_error_type"] is None  # No ValueError secundario por ID ausente.
    assert "fake-key" not in json.dumps(first)
    venue.missing = False
    recovered = await probe.run_probe(
        args(path, execute=False, recover=True), settings=settings(), factory=venue.factory
    )
    assert recovered["status"] == "TERMINAL_CONFIRMED"
    assert sum(method == "POST" for method, _ in venue.calls) == 1


async def test_resting_cancel_and_regression_keep_monotonic_floor(tmp_path):
    path = tmp_path / "journal"
    venue = Venue(path, timeout=True, resting=True)
    first = await probe.run_probe(args(path), settings=settings(), factory=venue.factory)
    assert first["status"] == "TERMINAL_CONFIRMED"
    assert ("DELETE", "/portfolio/events/orders/own") in venue.calls
    venue.regressive = True
    result = await probe.run_probe(
        args(path, execute=False, recover=True), settings=settings(), factory=venue.factory
    )
    assert result["status"] == "UNCONFIRMED_NO_RETRY"
    assert result["filled_count"] == "1.00" and "fill_regression" in result["uncertainty"]
    assert result["position_may_remain"]
    assert not result["partial_fill_observed"] and not result["terminal_confirmed"]


@pytest.mark.parametrize(
    "changes",
    [
        {"book_quantity": "0.00"},
        {"book_quantity": "2.00"},
        {"foreign_position": True},
        {"foreign_resting": True},
        {"fee_override": 2, "override_type": "quadratic"},
        {"fee_override": None, "override_type": "quadratic"},
    ],
)
async def test_preflight_rejects_without_any_post(tmp_path, changes):
    path = tmp_path / "journal"
    venue = Venue(path, **changes)
    result = await probe.run_probe(args(path), settings=settings(), factory=venue.factory)
    assert result["status"] == "SKIPPED" and not result["create_consumed"]
    assert not any(method in {"POST", "DELETE"} for method, _ in venue.calls)


@pytest.mark.parametrize(
    "changes", [{"debit": "0"}, {"debit": "0.67"}, {"fee": "0.000000"}, {"fills_late": True}]
)
async def test_terminal_is_not_account_reconciled_without_exact_cash_and_fills(tmp_path, changes):
    path = tmp_path / "journal"
    venue = Venue(path, **changes)
    result = await probe.run_probe(args(path), settings=settings(), factory=venue.factory)
    assert result["status"] == "UNCONFIRMED_NO_RETRY" and result["terminal_confirmed"]
    assert not result["account_reconciled"]
    assert result["filled_count"] == "1.00"


@pytest.mark.parametrize(
    "value", [None, True, "NaN", "Infinity", "-1", "2.01", "0.001", "1e-2", "１"]
)
def test_quantity_rejects_unknown_or_out_of_contract(value):
    with pytest.raises(ValueError):
        probe.count({"fill_count_fp": value}, "fill_count")


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/withdrawals"),
        ("POST", "/portfolio/events/orders/batched"),
        ("DELETE", "/portfolio/events/orders/foreign"),
        ("GET", "https://external-api.kalshi.com/trade-api/v2/portfolio/orders"),
        ("PATCH", "/portfolio/events/orders/own"),
    ],
)
async def test_method_path_guard_before_transport(tmp_path, method, path):
    state = {**probe.plan(), "client_order_id": "unused"}
    network = Mock(side_effect=AssertionError("must not reach transport"))
    async with probe.DemoIOCClient(
        state,
        tmp_path / "journal",
        settings=settings(),
        allow_create=False,
        signer=SimpleNamespace(sign=lambda *_: {}),
        transport=httpx.MockTransport(network),
    ) as client:
        with pytest.raises(PermissionError):
            await client.request(method, path)
    network.assert_not_called()


def test_constructor_checks_demo_before_key_and_real_signer_import(tmp_path):
    state = {**probe.plan(), "client_order_id": "unused"}
    with patch("src.auth.signer.KalshiSigner") as signer:
        client = probe.DemoIOCClient(
            state, tmp_path / "state", settings=settings(), allow_create=False
        )
        assert client.signer is signer.return_value
        signer.assert_called_once()
        wrong = settings()
        wrong.KALSHI_ENV = "production"
        with pytest.raises(PermissionError):
            probe.DemoIOCClient(state, tmp_path / "state", settings=wrong, allow_create=True)
        signer.assert_called_once()


def test_isolated_settings_ignore_production_env_and_require_private_demo_files(
    tmp_path, monkeypatch
):
    import src.utils.config as config

    directory = tmp_path / ".config/botkalshi/demo"
    directory.mkdir(parents=True)
    for name, content in (
        ("key_id.txt", "offline-key"),
        ("kalshi_private_key.pem", "offline-test-pem"),
    ):
        path = directory / name
        path.write_text(content)
        path.chmod(0o600)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(
        os,
        "environ",
        {
            "KALSHI_ENV": "production",
            "KALSHI_PRIVATE_KEY": "never-reuse",
            "KALSHI_API_KEY_ID": "prod-id",
        },
    )
    monkeypatch.setattr(config, "_settings", config._settings)
    monkeypatch.setitem(
        config.Settings.model_config, "env_file", config.Settings.model_config.get("env_file")
    )
    isolated = probe.isolated_demo_settings(True)
    assert isolated.KALSHI_ENV == "demo" and isolated.rest_url == probe.DEMO_ROOT
    assert isolated.KALSHI_API_KEY_ID == "offline-key" and not isolated.clave_privada_env()
    assert directory / "kalshi_private_key.pem" == Path(isolated.KALSHI_PRIVATE_KEY_PATH)
    assert os.environ == {}
    (directory / "kalshi_private_key.pem").chmod(0o644)
    with pytest.raises(PermissionError):
        probe.isolated_demo_settings(True)


async def test_stale_book_and_second_post_fail_before_network(tmp_path):
    path = tmp_path / "journal"
    venue = Venue(path)
    state = {
        **probe.plan(),
        "client_order_id": probe.PREFIX + str(probe.uuid.uuid4()),
        "status": "INTENT_BEFORE_NETWORK",
        "create_consumed": True,
        "filled_count": "0.00",
        "uncertainty": [],
    }
    probe.save_state(path, state)
    async with venue.factory(state, path, settings=settings(), allow_create=True) as client:
        client.book_at = time.monotonic() - 3
        with pytest.raises(PermissionError):
            await client.request("POST", "/portfolio/events/orders", body=probe.order_body(state))
        client.book_at = time.monotonic()
        await client.request("POST", "/portfolio/events/orders", body=probe.order_body(state))
        with pytest.raises(PermissionError):
            await client.request("POST", "/portfolio/events/orders", body=probe.order_body(state))
    assert len(venue.calls) == 1


def test_cli_plan_under_isolated_python_never_reads_dotenv_or_keys(tmp_path):
    root = Path(__file__).resolve().parents[2]
    bootstrap = "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('scripts.demo_ioc_partial_probe',run_name='__main__')"
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            bootstrap,
            str(root),
            "--state",
            str(tmp_path / "never-created"),
        ],
        cwd=tmp_path,
        env={
            "KALSHI_ENV": "production",
            "KALSHI_API_KEY_ID": "never-display-me",
            "PATH": os.defpath,
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0 and json.loads(result.stdout)["status"] == "PLAN_ONLY"
    assert "never-display-me" not in result.stdout + result.stderr
    assert not list(tmp_path.iterdir())


async def test_read_stream_is_bounded_and_has_deadline(tmp_path, monkeypatch):
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"{"
            await asyncio.sleep(1)

    monkeypatch.setattr(probe, "REQUEST_TIMEOUT", 0.01)
    state = {**probe.plan(), "client_order_id": "unused"}
    async with probe.DemoIOCClient(
        state,
        tmp_path / "state",
        settings=settings(),
        allow_create=False,
        signer=SimpleNamespace(sign=lambda *_: {}),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowStream())),
    ) as client:
        with pytest.raises(TimeoutError):
            await client.request("GET", "/portfolio/balance")


@pytest.mark.parametrize("change", ["settings", "base_url", "transport"])
async def test_host_mutation_never_reaches_transport(tmp_path, change):
    network = Mock(side_effect=AssertionError("transport forbidden"))
    state = {**probe.plan(), "client_order_id": "unused"}
    async with probe.DemoIOCClient(
        state,
        tmp_path / "state",
        settings=settings(),
        allow_create=False,
        signer=SimpleNamespace(sign=lambda *_: {}),
        transport=httpx.MockTransport(network),
    ) as client:
        if change == "settings":
            client.settings.KALSHI_ENV = "production"
        elif change == "base_url":
            client.base_url = "https://external-api.kalshi.com/trade-api/v2"
        else:
            client.http.base_url = "https://external-api.kalshi.com/trade-api/v2"
        with pytest.raises(PermissionError):
            await client.request("GET", "/portfolio/balance")
    network.assert_not_called()


@pytest.mark.parametrize("cursor", [None, 123, "repeated"])
async def test_pagination_must_finish_not_just_return_first_page(cursor):
    from unittest.mock import AsyncMock

    client = SimpleNamespace(request=AsyncMock(return_value={"orders": [], "cursor": cursor}))
    with pytest.raises(ValueError):
        await probe.collect(client, "/portfolio/orders", "orders")
    assert client.request.await_count <= 2


async def test_late_fills_can_resolve_without_new_post(tmp_path):
    path = tmp_path / "journal"
    venue = Venue(path, fills_late=True)
    first = await probe.run_probe(args(path), settings=settings(), factory=venue.factory)
    assert first["terminal_confirmed"] and not first["account_reconciled"]
    venue.fills_late = False
    second = await probe.run_probe(
        args(path, execute=False, recover=True), settings=settings(), factory=venue.factory
    )
    assert second["account_reconciled"] and second["status"] == "TERMINAL_CONFIRMED"
    assert sum(method == "POST" for method, _ in venue.calls) == 1


async def test_duplicate_fill_does_not_double_quantity_or_certify_account(tmp_path):
    path = tmp_path / "journal"
    venue = Venue(path)
    original = venue.handle

    def duplicate(request):
        response = original(request)
        if request.url.path.endswith("/fills"):
            body = response.json()
            body["fills"] *= 2
            return httpx.Response(200, json=body)
        return response

    venue.handle = duplicate
    state = await probe.run_probe(args(path), settings=settings(), factory=venue.factory)
    assert state["status"] == "UNCONFIRMED_NO_RETRY"
    assert not state["account_reconciled"] and state["filled_count"] == "1.00"


def _abrupt_exit_worker(directory):
    """Proceso nuevo; _exit corta el transporte sin finally ni retorno del ACK."""
    import socket

    os.environ.clear()
    os.chdir(directory)

    def denied(*_args, **_kwargs):
        raise AssertionError("child offline")

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = (
        socket.getaddrinfo
    ) = denied
    path = directory / "journal"
    venue = Venue(path)
    original = venue.handle

    def exit_after_accept(request):
        response = original(request)
        if request.method == "POST":
            with (directory / "accepted.json").open("x") as stream:
                json.dump({"order": venue.order, "pid": os.getpid(), "posts": 1}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os._exit(77)
        return response

    venue.handle = exit_after_accept
    asyncio.run(probe.run_probe(args(path), settings=settings(), factory=venue.factory))


def test_abrupt_process_exit_recovers_durable_intent_without_second_post(tmp_path):
    root = Path(__file__).resolve().parents[2]
    bootstrap = "import runpy,sys,pathlib; sys.path.insert(0,sys.argv[1]); scope=runpy.run_path(sys.argv[2]); scope['_abrupt_exit_worker'](pathlib.Path(sys.argv[3]))"
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            bootstrap,
            str(root),
            str(Path(__file__).resolve()),
            str(tmp_path),
        ],
        cwd=tmp_path,
        env={"PATH": os.defpath},
        capture_output=True,
        timeout=5,
    )
    assert process.returncode == 77, process.stderr.decode()
    accepted = json.loads((tmp_path / "accepted.json").read_text())
    assert accepted["pid"] != os.getpid() and accepted["posts"] == 1
    state = json.loads((tmp_path / "journal").read_text())
    assert state["create_consumed"] and state["status"] == "INTENT_BEFORE_NETWORK"
    assert state.get("order_id") is None
    venue = Venue(tmp_path / "journal")
    venue.order = accepted["order"]
    recovered = asyncio.run(
        probe.run_probe(
            args(venue.path, execute=False, recover=True),
            settings=settings(),
            factory=venue.factory,
        )
    )
    assert recovered["status"] == "TERMINAL_CONFIRMED" and recovered["filled_count"] == "1.00"
    assert not any(method == "POST" for method, _ in venue.calls)


@pytest.mark.parametrize(
    "status,code,expected",
    [
        (400, "invalid_request", "invalid_request"),
        (
            409,
            "fill_or_kill_insufficient_resting_volume",
            "fill_or_kill_insufficient_resting_volume",
        ),
        (429, "too_many_requests", "too_many_requests"),
        (503, "internal_error", "internal_error"),
        (400, "SECRET-CODE-never-persist", None),
    ],
)
async def test_http_error_diagnostic_safe_and_no_retry_or_recovery_post(
    tmp_path, status, code, expected
):
    path = tmp_path / "journal"
    venue = Venue(path)
    original = venue.handle

    def reject(request):
        response = original(request)
        if request.method == "POST":
            venue.order = None  # Listado ausente no acredita rechazo, incluso con4xx.
            return httpx.Response(
                status,
                json={"error": {"code": code, "message": "SECRET-BODY-never-persist"}},
                headers={"X-Test-Secret": "SECRET-HEADER"},
            )
        return response

    venue.handle = reject
    state = await probe.run_probe(args(path), settings=settings(), factory=venue.factory)
    assert state["status"] == "UNCONFIRMED_NO_RETRY" and state["create_consumed"]
    assert state["create_error_type"] == "DemoHTTPError"
    assert state["create_http_status"] == status and state["create_http_error_code"] == expected
    assert state["position_may_remain"] and state["filled_count_is_floor"]
    assert state["last_run_error_type"] is None
    assert "SECRET" not in path.read_text() and "SECRET" not in json.dumps(state)
    recovered = await probe.run_probe(
        args(path, execute=False, recover=True), settings=settings(), factory=venue.factory
    )
    assert recovered["create_http_status"] == status and recovered["create_consumed"]
    assert recovered["status"] == "UNCONFIRMED_NO_RETRY"
    assert sum(method == "POST" for method, _ in venue.calls) == 1


async def test_http_headers_status_is_durable_even_if_error_body_never_finishes(
    tmp_path, monkeypatch
):
    class SlowError(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"error":{"message":"SECRET'
            await asyncio.sleep(1)

    monkeypatch.setattr(probe, "REQUEST_TIMEOUT", 0.02)
    state = {**probe.plan(), "client_order_id": "unit"}
    path = tmp_path / "state"
    async with probe.DemoIOCClient(
        state,
        path,
        settings=settings(),
        allow_create=False,
        signer=SimpleNamespace(sign=lambda *_: {}),
        transport=httpx.MockTransport(lambda _: httpx.Response(503, stream=SlowError())),
    ) as client:
        with pytest.raises(TimeoutError):
            await client.request("GET", "/portfolio/balance")
    saved = json.loads(path.read_text())
    assert saved["read_http_status"] == 503 and saved["read_http_error_code"] is None
    assert "SECRET" not in path.read_text()


async def test_legacy_consumed_unknown_journal_cannot_repost_or_invent_old_http_status(tmp_path):
    path = tmp_path / "journal"
    state = {
        **probe.plan(),
        "status": "UNCONFIRMED_NO_RETRY",
        "client_order_id": probe.PREFIX + str(probe.uuid.uuid4()),
        "create_consumed": True,
        "filled_count": "0.00",
        "uncertainty": [],
        "position_may_remain": True,
        "create_error_type": "RuntimeError",
        "error_type": "ValueError",
        "balance_before_usd": "200.0000",
    }
    probe.save_state(path, state)
    venue = Venue(path)
    with pytest.raises(ValueError):
        await probe.run_probe(args(path), settings=settings(), factory=venue.factory)
    assert not venue.calls
    recovered = await probe.run_probe(
        args(path, execute=False, recover=True), settings=settings(), factory=venue.factory
    )
    assert recovered["client_order_id"] == state["client_order_id"] and recovered["create_consumed"]
    assert (
        recovered["create_error_type"] == "RuntimeError" and "create_http_status" not in recovered
    )
    assert recovered["pending_reason"] == "ORDER_NOT_OBSERVED_AFTER_CONSUMED_CREATE"
    assert recovered["filled_count_is_floor"] and not recovered["terminal_confirmed"]
    assert recovered["last_run_error_type"] is None
    assert not any(method != "GET" for method, _ in venue.calls)
