"""Runner DEMO: aislamiento, cero escrituras y lifecycle real con transporte simulado."""

import asyncio
import contextlib
import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts import demo_runner_readiness as smoke


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    from src.monitoring.health import BotState
    from src.risk.manager import RiskManager
    from src.storage import models
    from src.utils.config import Settings, reset_settings_for_testing

    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (credentials / "key_id.txt").write_text("demo-fake-key-id-only")
    (credentials / "kalshi_private_key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    for file in credentials.iterdir():
        file.chmod(0o600)
    workdir = tmp_path / "work"
    workdir.mkdir()
    # Deliberadamente hostil: ni dotenv ni entorno heredado deben ganar.
    (workdir / ".env").write_text(
        "TRADING_ENABLED=true\nKALSHI_ENV=production\nODDS_API_KEY=SECRET\n"
    )
    previous_env = dict(os.environ)
    previous_config = Settings.model_config.get("env_file")
    monkeypatch.chdir(workdir)
    monkeypatch.setattr(models, "_engine", None)
    for name, value in {
        "db_initialized": False,
        "capture_running": False,
        "tracked_markets_count": 0,
        "v2_manager": None,
        "last_error": None,
        "is_paused": False,
        "pause_reason": None,
        "motor1_local_pause": None,
        "ws_connected": False,
    }.items():
        monkeypatch.setattr(BotState, name, value)
    for name in ("_cached_capital_usd", "_last_balance_at", "_last_raw_balance_usd"):
        monkeypatch.setattr(RiskManager, name, None)
    try:
        yield credentials, workdir
    finally:
        if models._engine is not None:
            models._engine.dispose()
        Settings.model_config["env_file"] = previous_config
        os.environ.clear()
        os.environ.update(previous_env)
        reset_settings_for_testing()


def test_configuration_ignores_prod_env_and_dotenv(isolated_settings):
    credentials, workdir = isolated_settings
    os.environ.update(
        TRADING_ENABLED="true",
        KALSHI_ENV="production",
        ODDS_API_KEY="SECRET",
        HTTP_PROXY="https://invalid-proxy",
        TELEGRAM_BOT_TOKEN="SECRET",
    )
    cfg = smoke.configure(workdir, credentials, 18765)
    assert cfg.KALSHI_ENV == "demo" and cfg.rest_url == smoke.REST and cfg.ws_url == smoke.WS
    assert f"sqlite:///{workdir / 'readiness.db'}" == cfg.DATABASE_URL
    assert cfg.MOTOR_1_ARBITRAGE_ENABLED and cfg.USE_ORDERBOOK_MANAGER_V2
    assert cfg.DYNAMIC_CAPITAL_ENABLED
    for name in (
        "TRADING_ENABLED",
        "MOTOR_1_EXECUTION_ENABLED",
        "MOTOR_2_SPORTSBOOK_ENABLED",
        "MOTOR_2_ENTRY_EXECUTION_ENABLED",
        "MOTOR_2_EXECUTION_ENABLED",
        "MOTOR_3_CLV_ENABLED",
        "MOTOR_3_EXECUTION_ENABLED",
        "MOTOR_MM_ENABLED",
        "MOTOR_MM_EXECUTION_ENABLED",
        "MOTOR_REST_ENABLED",
        "MOTOR_REST_EXECUTION_ENABLED",
        "TELEGRAM_DASHBOARD_ENABLED",
        "ANALYST_LOOP_ENABLED",
        "DAILY_PNL_REPORT_ENABLED",
    ):
        assert getattr(cfg, name) is False
    assert not cfg.ODDS_API_KEY and not cfg.TELEGRAM_BOT_TOKEN and not cfg.clave_privada_env()
    assert "HTTP_PROXY" not in os.environ


@pytest.mark.parametrize("bad", ["permissions", "symlink", "repository"])
def test_credentials_refuse_unsafe_paths(isolated_settings, monkeypatch, bad):
    credentials, workdir = isolated_settings
    path = credentials / "key_id.txt"
    if bad == "permissions":
        path.chmod(0o644)
    elif bad == "symlink":
        other = credentials / "actual-id"
        path.rename(other)
        path.symlink_to(other)
    else:
        monkeypatch.setattr(smoke, "ROOT", credentials.parent)
    with pytest.raises(PermissionError):
        smoke.configure(workdir, credentials, 18765)


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CONNECT", "TRACE"]
)
@pytest.mark.parametrize("asynchronous", [True, False])
async def test_non_get_blocked_before_transport(method, asynchronous):
    requests = []
    transport = httpx.MockTransport(lambda request: requests.append(request) or httpx.Response(200))
    evidence = smoke.Evidence()
    with smoke.network_guards(evidence, allowed_ips={"192.0.2.1"}), pytest.raises(PermissionError):
        if asynchronous:
            async with httpx.AsyncClient(transport=transport) as client:
                await client.request(method, smoke.REST + "/portfolio/orders")
        else:
            with httpx.Client(transport=transport) as client:
                client.request(method, smoke.REST + "/portfolio/orders")
    assert requests == [] and evidence.blocked == {"HTTP_MUTATION_BLOCKED": 1}


@pytest.mark.parametrize(
    "url",
    [
        "https://external-api.kalshi.com/trade-api/v2/portfolio/balance",
        "https://api.the-odds-api.com/v4/sports",
        "https://api.telegram.org/bot/sendMessage",
        "https://api.typesafe.ai/v1/systemone",
        smoke.REST + "/portfolio/orders",
        f"http://{smoke.HOST}/trade-api/v2/events",
        f"https://secret@{smoke.HOST}/trade-api/v2/events",
        f"https://{smoke.WS_HOST}/trade-api/v2/events",
    ],
)
async def test_non_demo_and_non_allowlisted_reads_blocked(url):
    transport = AsyncMock()
    with (
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", transport),
        smoke.network_guards(smoke.Evidence(), allowed_ips={"192.0.2.1"}),
        pytest.raises(PermissionError),
    ):
        async with httpx.AsyncClient() as client:
            await client.get(url)
    transport.assert_not_awaited()


async def test_redirect_rechecked_and_budget_enforced_before_transport(monkeypatch):
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(
            302, headers={"location": "https://external-api.kalshi.com/trade-api/v2/events"}
        )

    evidence = smoke.Evidence()
    with smoke.network_guards(evidence, allowed_ips={"192.0.2.1"}):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(redirect), follow_redirects=True
        ) as client:
            with pytest.raises(PermissionError):
                await client.get(smoke.REST + "/events")
    assert len(requests) == 1 and evidence.blocked == {"HTTP_HOST_BLOCKED": 1}
    monkeypatch.setattr(smoke, "MAX_HTTP", 1)
    evidence = smoke.Evidence()
    requests.clear()
    with smoke.network_guards(evidence, allowed_ips={"192.0.2.1"}):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: requests.append(r) or httpx.Response(200, json={})
            )
        ) as client:
            await client.get(smoke.REST + "/events")
            with pytest.raises(PermissionError):
                await client.get(smoke.REST + "/events")
    assert len(requests) == 1 and evidence.http_attempts == 1


def test_dns_and_socket_other_destinations_denied():
    evidence = smoke.Evidence()
    with smoke.network_guards(evidence, allowed_ips={"192.0.2.1"}):
        with pytest.raises(PermissionError):
            socket.getaddrinfo("external-api.kalshi.com", 443)
        with socket.socket() as sock:
            with pytest.raises(PermissionError):
                sock.connect(("192.0.2.2", 443))
            with pytest.raises(PermissionError):
                sock.connect_ex(("192.0.2.1", 80))
    assert evidence.blocked == {"DNS_DESTINATION_BLOCKED": 1, "TCP_DESTINATION_BLOCKED": 2}


def subscription():
    return {
        "id": 1,
        "cmd": "subscribe",
        "params": {"channels": ["orderbook_delta", "ticker"], "market_tickers": ["KXTEST-A"]},
    }


@pytest.mark.parametrize(
    "mutation",
    [
        {"cmd": "place_order", "params": {}},
        {"cmd": "subscribe", "params": {"channels": ["fill"], "market_tickers": ["KXTEST-A"]}},
        {
            "cmd": "update_subscription",
            "params": {"action": "delete_markets", "sids": [1], "market_tickers": ["KXTEST-A"]},
        },
    ],
)
async def test_ws_commands_blocked_before_underlying_send(mutation):
    raw = SimpleNamespace(send=AsyncMock())
    ws = smoke.ReadOnlyWS(raw, smoke.Evidence())
    await ws.send(json.dumps(subscription()))
    with pytest.raises(PermissionError):
        await ws.send(json.dumps(mutation))
    assert raw.send.await_count == 1


async def test_ws_host_and_transport_override_blocked_before_connect():
    import websockets

    original = AsyncMock()
    with (
        patch.object(websockets, "connect", original),
        smoke.network_guards(smoke.Evidence(), allowed_ips={"192.0.2.1"}),
    ):
        with pytest.raises(PermissionError):
            async with websockets.connect("wss://external-api.kalshi.com/trade-api/ws/v2"):
                pass
        with pytest.raises(PermissionError):
            async with websockets.connect(f"wss://{smoke.HOST}/trade-api/ws/v2"):
                pass
        with pytest.raises(PermissionError):
            async with websockets.connect(smoke.WS, host="evil"):
                pass
    original.assert_not_called()


@pytest.mark.parametrize("failure", ["type", "tls", "http", "unknown"])
async def test_ws_handshake_diagnostics_are_static_and_sanitized(failure):
    import ssl

    import websockets
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    secret = "FORBIDDEN-HEADER-PAYLOAD"
    error = {
        "type": TypeError(secret),
        "tls": ssl.SSLCertVerificationError(secret),
        "http": InvalidStatus(
            Response(404, secret, Headers({"Authorization": secret}), bytearray(secret.encode()))
        ),
        "unknown": type(secret, (Exception,), {})(secret),
    }[failure]

    @contextlib.asynccontextmanager
    async def failing_connect(url, **kwargs):
        assert url == smoke.WS and kwargs["proxy"] is None
        raise error
        yield  # pragma: no cover -- firma async context manager

    evidence = smoke.Evidence()
    with (
        patch.object(websockets, "connect", failing_connect),
        smoke.network_guards(evidence, allowed_ips={"192.0.2.1"}),
        pytest.raises(type(error)),
    ):
        async with websockets.connect(smoke.WS):
            pass
    expected_type = {
        "type": "TypeError",
        "tls": "SSLCertVerificationError",
        "http": "InvalidStatus",
        "unknown": "OtherError",
    }[failure]
    assert evidence.ws_error_types == {expected_type: 1}
    assert evidence.ws_failure_phases == {"handshake": 1}
    assert evidence.ws_handshake_statuses == ({"404": 1} if failure == "http" else {})
    report = evidence.report()
    assert secret not in json.dumps(report)
    assert smoke.checked_report(json.dumps(report).encode()) == report


def test_ws_report_diagnostic_unknown_values_are_rejected():
    report = smoke.Evidence().report()
    report["ws_error_types"] = {"SECRET-DYNAMIC-CLASS": 1}
    with pytest.raises(ValueError):
        smoke.checked_report(json.dumps(report).encode())


def test_executor_and_odds_construction_blocked(isolated_settings):
    from src.clients.odds_api import OddsAPIClient
    from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor

    smoke.configure(isolated_settings[1], isolated_settings[0], 18765)
    evidence = smoke.Evidence()
    with smoke.runner_instrumentation(evidence, "KXTEST"):
        with pytest.raises(PermissionError):
            ArbitrageExecutor(None)
        with pytest.raises(PermissionError):
            OddsAPIClient()
    assert evidence.blocked == {"EXECUTOR_CONSTRUCTED": 1, "EXTERNAL_PRODUCER_CONSTRUCTED": 1}


def test_plan_isolated_cli_does_not_read_credentials_or_spawn(tmp_path, capsys):
    with patch.object(smoke, "worker") as worker, patch.object(subprocess, "run") as run:
        assert smoke.main([]) == 0
    worker.assert_not_called()
    run.assert_not_called()
    assert json.loads(capsys.readouterr().out)["credentials_read"] is False
    command, env = smoke.isolated_command(60, "KXTEST")
    assert command[1] == "-I" and "--worker" in command
    assert not {"HOME", "PYTHONPATH", "HTTP_PROXY", "KALSHI_API_KEY_ID", "DATABASE_URL"} & set(env)
    # Proceso real, pero PLAN_ONLY: sin dependencias del repo, .env ni red.
    completed = subprocess.run(
        [command[0], "-I", str(Path(smoke.__file__).resolve())],
        env={"KALSHI_ENV": "production", "TRADING_ENABLED": "true"},
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout)["status"] == "PLAN_ONLY"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["secret_extra_field", "secret_error", "timeout"])
def test_parent_never_publishes_child_payloads(tmp_path, capsys, failure):
    secret = "NEVER-PUBLISH-THIS-KEY"
    result = smoke.Evidence().report()
    if failure == "secret_extra_field":
        result["key"] = secret
    elif failure == "secret_error":
        result["blocked"] = {secret: 1}
    completed = SimpleNamespace(stdout=json.dumps(result).encode(), returncode=1)
    side_effect = (
        subprocess.TimeoutExpired("worker", 1, output=secret.encode(), stderr=secret.encode())
        if failure == "timeout"
        else None
    )
    path = tmp_path / "report.json"
    with patch.object(subprocess, "run", return_value=completed, side_effect=side_effect):
        assert smoke.main(["--run", "--report", str(path)]) == 1
    assert secret not in capsys.readouterr().out
    assert secret not in path.read_text()
    assert json.loads(path.read_text())["status"] == "BLOCKED"
    assert path.stat().st_mode & 0o077 == 0


async def test_real_runner_with_fake_demo_transports_and_supervised_shutdown(
    isolated_settings, monkeypatch
):
    import websockets
    from websockets.connection import State

    from src.monitoring.health import BotState
    from src.runner import ProductionRunner
    from src.strategies.motor_1_arbitrage.engine import Motor1Engine

    smoke.configure(isolated_settings[1], isolated_settings[0], 18765)
    evidence = smoke.Evidence()
    requests = []
    market = {
        "ticker": "KXTEST-E-A",
        "status": "active",
        "close_time": "2099-01-01T00:00:00Z",
        "open_time": "2020-01-01T00:00:00Z",
        "yes_bid": 40,
        "no_bid": 50,
        "yes_ask": 50,
        "no_ask": 60,
    }

    async def transport(_self, request):
        requests.append(request)
        path = request.url.path
        payload = (
            {"balance": 20000, "portfolio_value": 0}
            if path.endswith("/balance")
            else (
                {"events": [{"event_ticker": "KXTEST-E"}]}
                if path.endswith("/events")
                else (
                    {"event": {"event_ticker": "KXTEST-E"}, "markets": [market]}
                    if "/events/" in path
                    else {"market": market}
                )
            )
        )
        return httpx.Response(200, json=payload)

    class FakeSocket:
        state = State.OPEN

        async def send(self, message):
            assert json.loads(message)["cmd"] in {"subscribe", "update_subscription"}

        async def close(self, **kwargs):
            self.state = State.CLOSED

        async def __aiter__(self):
            for frame in [
                {"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}},
                {
                    "type": "orderbook_snapshot",
                    "sid": 1,
                    "seq": 1,
                    "msg": {
                        "market_ticker": market["ticker"],
                        "yes_dollars_fp": [["0.4000", "10.00"]],
                        "no_dollars_fp": [["0.5000", "10.00"]],
                    },
                },
            ]:
                yield json.dumps(frame)
            await asyncio.Event().wait()

    @contextlib.asynccontextmanager
    async def fake_connect(url, **kwargs):
        assert url == smoke.WS and kwargs["proxy"] is None
        yield FakeSocket()

    real_sleep = asyncio.sleep

    async def accelerated_sleep(seconds):
        await real_sleep(min(seconds, 0.001))

    async def local_health(runner):
        runner._uvicorn_server = SimpleNamespace(started=True, should_exit=False)
        await runner._stop_event.wait()

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(asyncio, "sleep", accelerated_sleep)
    monkeypatch.setattr(Motor1Engine, "LOOP_INTERVAL_SEC", 0.002)
    monkeypatch.setattr(ProductionRunner, "_run_health_server", local_health)
    # El resto es ProductionRunner.run REAL, clientes con firma RSA falsa local,
    # discovery/parser/BookV2/engine/settlement reales; nunca sockets externos.
    with (
        smoke.network_guards(evidence, allowed_ips={"192.0.2.1"}),
        smoke.runner_instrumentation(evidence, "KXTEST"),
    ):
        report = await smoke.run_runner(2, evidence)
    assert report["status"] == "PASS_READ_ONLY", report
    assert report["services_started"] == ["capture", "motor1_arb", "settlement"]
    assert report["counts"]["initialized_books_peak"] == 1
    assert report["counts"]["shadow_ticks"] >= 2
    assert all(r.method == "GET" and r.url.host == smoke.HOST for r in requests)
    assert BotState.capture_running is False
    assert smoke.checked_report(json.dumps(report).encode()) == report


async def test_real_supervisor_failure_cannot_pass(isolated_settings, monkeypatch):
    from src.risk.manager import RiskManager
    from src.runner import ProductionRunner

    smoke.configure(isolated_settings[1], isolated_settings[0], 18765)

    async def crashing_capture(_runner):
        raise RuntimeError("SECRET-PAYLOAD-FROM-UPSTREAM")

    async def waiting_service(runner):
        await runner._stop_event.wait()

    monkeypatch.setattr(ProductionRunner, "_run_data_capture", crashing_capture)
    for name in (
        "_run_health_server",
        "_run_settlement",
        "_run_balance_refresh",
        "_run_motor1_arb",
    ):
        monkeypatch.setattr(ProductionRunner, name, waiting_service)
    monkeypatch.setattr(RiskManager, "refresh_capital_from_balance", AsyncMock(return_value=None))
    evidence = smoke.Evidence()
    with (
        smoke.network_guards(evidence, allowed_ips={"192.0.2.1"}),
        smoke.runner_instrumentation(evidence, "KXTEST"),
    ):
        report = await smoke.run_runner(0.05, evidence)
    assert report["status"] == "INCOMPLETE" and evidence.runner_exit == 1
    assert "shutdown" in report["failed_gates"] and "no_observed_error" in report["failed_gates"]
    assert "SECRET" not in json.dumps(report)
