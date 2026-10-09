"""Smoke acotado de ProductionRunner, exclusivamente DEMO y sólo lectura.

Sin --run imprime el plan y no lee claves ni abre red. --run crea un proceso -I,
cwd/SQLite temporales y un informe de contadores sin payloads, claves ni saldos.
No valida ejecución, liquidez, rentabilidad ni motores dependientes de Odds/Jev.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib
import json
import logging
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HOST = "external-api.demo.kalshi.co"
WS_HOST = "external-api-ws.demo.kalshi.co"
REST = f"https://{HOST}/trade-api/v2"
WS = f"wss://{WS_HOST}/trade-api/ws/v2"
MAX_HTTP = 60
MAX_WS_COMMANDS = 30
MAX_WS_FRAMES = 5000
MAX_TICKERS = 20
SHUTDOWN_SECONDS = 15
TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
REQUIRED_SERVICES = {"capture", "settlement", "motor1_arb"}
ERROR_TYPES = {
    "ValueError",
    "PermissionError",
    "FileNotFoundError",
    "OSError",
    "gaierror",
    "TimeoutError",
    "RuntimeError",
    "ValidationError",
    "TypeError",
    "OtherError",
    "ReportWriteError",
    "TimeoutExpired",
    "HTTPError",
    "ConnectError",
    "ReadTimeout",
}
PHASES = {"configure", "guards", "runner", "worker", "parent", "report"}
WS_ERROR_TYPES = {
    "TypeError",
    "ValueError",
    "PermissionError",
    "RuntimeError",
    "OtherError",
    "SSLCertVerificationError",
    "SSLError",
    "InvalidStatus",
    "InvalidHandshake",
    "InvalidMessage",
    "InvalidURI",
    "TimeoutError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "ConnectionClosedError",
    "ConnectionClosedOK",
    "gaierror",
    "OSError",
}
WS_FAILURE_PHASES = {"handshake", "session", "close"}
BLOCK_CODES = {
    "HTTP_MUTATION_BLOCKED",
    "HTTP_HOST_BLOCKED",
    "HTTP_ROUTE_BLOCKED",
    "HTTP_BUDGET_EXHAUSTED",
    "WS_HOST_BLOCKED",
    "WS_CONNECTION_BUDGET_EXHAUSTED",
    "WS_COMMAND_BLOCKED",
    "WS_COMMAND_BUDGET_EXHAUSTED",
    "WS_FRAME_BUDGET_EXHAUSTED",
    "DEMO_DNS_UNAVAILABLE",
    "DNS_DESTINATION_BLOCKED",
    "TCP_DESTINATION_BLOCKED",
    "WS_TRANSPORT_OVERRIDE_BLOCKED",
    "DISCOVERY_EVENT_BOUND_EXCEEDED",
    "DISCOVERY_MARKET_BOUND_EXCEEDED",
    "DISCOVERY_EVENT_IDENTITY_MISMATCH",
    "EXECUTOR_CONSTRUCTED",
    "UNEXPECTED_TRADE_ROW",
    "EXTERNAL_PRODUCER_CONSTRUCTED",
}


@dataclass
class Evidence:
    http_attempts: int = 0
    http_ok: Counter = field(default_factory=Counter)
    http_statuses: Counter = field(default_factory=Counter)
    ws_connections: int = 0
    ws_commands: int = 0
    ws_frames: int = 0
    ws_error_types: Counter = field(default_factory=Counter)
    ws_handshake_statuses: Counter = field(default_factory=Counter)
    ws_failure_phases: Counter = field(default_factory=Counter)
    discoveries: int = 0
    ticks: int = 0
    initialized_books_peak: int = 0
    tracked_markets_peak: int = 0
    health_started: bool = False
    capture_seen: bool = False
    db_initialized: bool = False
    shutdown_clean: bool = False
    runner_exit: int | None = None
    error_seen: bool = False
    blocked: Counter = field(default_factory=Counter)
    services_started: set[str] = field(default_factory=set)

    def deny(self, reason: str):
        self.blocked[reason] += 1
        raise PermissionError(reason)  # Sólo códigos fijos; jamás URL/headers/payload.

    def http(self, method: str, url) -> str:
        parsed = urlsplit(str(url))
        if method != "GET":
            self.deny("HTTP_MUTATION_BLOCKED")
        if (
            parsed.scheme != "https"
            or parsed.hostname != HOST
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            self.deny("HTTP_HOST_BLOCKED")
        path = parsed.path
        if path == "/trade-api/v2/portfolio/balance":
            route = "balance"
        elif path == "/trade-api/v2/events":
            route = "events"
        elif re.fullmatch(r"/trade-api/v2/events/[A-Za-z0-9_.-]{1,100}", path):
            route = "event"
        elif re.fullmatch(r"/trade-api/v2/markets/[A-Za-z0-9_.-]{1,100}/orderbook", path):
            route = "book"
        elif re.fullmatch(r"/trade-api/v2/markets/[A-Za-z0-9_.-]{1,100}", path):
            route = "market"
        else:
            self.deny("HTTP_ROUTE_BLOCKED")
        if self.http_attempts >= MAX_HTTP:
            self.deny("HTTP_BUDGET_EXHAUSTED")
        self.http_attempts += 1
        return route

    def ws_url(self, url: str):
        if url != WS:
            self.deny("WS_HOST_BLOCKED")
        if self.ws_connections >= 3:
            self.deny("WS_CONNECTION_BUDGET_EXHAUSTED")
        self.ws_connections += 1

    def ws_message(self, raw):
        try:
            if not isinstance(raw, str) or len(raw) > 16384:
                raise ValueError
            message = json.loads(raw)
            if not isinstance(message, dict) or set(message) - {"id", "cmd", "params"}:
                raise ValueError
            params = message.get("params", {})
            if not isinstance(params, dict):
                raise ValueError
            if message.get("cmd") == "subscribe":
                if set(params) - {"channels", "market_tickers"}:
                    raise ValueError
                channels = params.get("channels")
                if (
                    not isinstance(channels, list)
                    or not channels
                    or not set(channels) <= {"ticker", "orderbook_delta"}
                ):
                    raise ValueError
            elif message.get("cmd") == "update_subscription":
                if (
                    set(params) - {"sids", "market_tickers", "action"}
                    or params.get("action") != "get_snapshot"
                ):
                    raise ValueError
                if (
                    not isinstance(params.get("sids"), list)
                    or not params["sids"]
                    or any(type(s) is not int or s < 0 for s in params["sids"])
                ):
                    raise ValueError
            else:
                raise ValueError
            tickers = params.get("market_tickers")
            if (
                not isinstance(tickers, list)
                or not 1 <= len(tickers) <= MAX_TICKERS
                or any(not isinstance(t, str) or not TOKEN.fullmatch(t) for t in tickers)
            ):
                raise ValueError
        except (ValueError, TypeError):
            self.deny("WS_COMMAND_BLOCKED")
        if self.ws_commands >= MAX_WS_COMMANDS:
            self.deny("WS_COMMAND_BUDGET_EXHAUSTED")
        self.ws_commands += 1

    def report(self) -> dict:
        checks = {
            "boot": self.db_initialized and self.health_started,
            "supervised_services": self.services_started >= REQUIRED_SERVICES,
            "capture_running": self.capture_seen,
            "discovery": self.discoveries > 0 and self.tracked_markets_peak > 0,
            "balance_read": self.http_ok["balance"] > 0,
            "books": self.ws_frames > 0 and self.initialized_books_peak > 0,
            "shadow_ticks": self.ticks >= 2,
            "shutdown": self.shutdown_clean and self.runner_exit == 0,
            "no_guard_violation": not self.blocked,
            "no_observed_error": not self.error_seen,
        }
        return {
            "schema": "demo-runner-readiness-v1",
            "environment": "demo",
            "status": "PASS_READ_ONLY" if all(checks.values()) else "INCOMPLETE",
            "checks": checks,
            "counts": {
                "http_attempts": self.http_attempts,
                "http_successes": dict(self.http_ok),
                "ws_connections": self.ws_connections,
                "ws_commands": self.ws_commands,
                "ws_frames": self.ws_frames,
                "discoveries": self.discoveries,
                "shadow_ticks": self.ticks,
                "initialized_books_peak": self.initialized_books_peak,
                "tracked_markets_peak": self.tracked_markets_peak,
            },
            "blocked": dict(self.blocked),
            "http_statuses": dict(self.http_statuses),
            "ws_error_types": dict(self.ws_error_types),
            "ws_handshake_statuses": dict(self.ws_handshake_statuses),
            "ws_failure_phases": dict(self.ws_failure_phases),
            "real_money_orders": 0,
            "services_started": sorted(self.services_started),
            "failed_gates": [name for name, passed in checks.items() if not passed],
            "scope": "bounded_M1_shadow_runner_only",
        }


class ReadOnlyWS:
    def __init__(self, connection, evidence: Evidence):
        self.connection, self.evidence = connection, evidence

    def __getattr__(self, name):
        return getattr(self.connection, name)

    async def send(self, message):
        self.evidence.ws_message(message)
        return await self.connection.send(message)

    async def __aiter__(self):
        async for message in self.connection:
            if self.evidence.ws_frames >= MAX_WS_FRAMES:
                self.evidence.deny("WS_FRAME_BUDGET_EXHAUSTED")
            self.evidence.ws_frames += 1
            yield message


@contextlib.contextmanager
def network_guards(evidence: Evidence, *, allowed_ips: set[str] | None = None):
    """Guard por request/redirect y frame, más destino TCP/DNS; proxies deshabilitados."""
    import httpx
    import websockets

    lookup, connect, connect_ex = (
        socket.getaddrinfo,
        socket.socket.connect,
        socket.socket.connect_ex,
    )
    # Dos destinos oficiales diferentes: REST y WS; ambos resueltos antes del guard.
    ips = (
        allowed_ips
        if allowed_ips is not None
        else {
            r[4][0] for host in (HOST, WS_HOST) for r in lookup(host, 443, type=socket.SOCK_STREAM)
        }
    )
    if not ips:
        evidence.deny("DEMO_DNS_UNAVAILABLE")
    original_async = httpx.AsyncClient._send_single_request
    original_sync = httpx.Client._send_single_request
    original_ws = websockets.connect

    async def send_async(client, request):
        route = evidence.http(request.method, request.url)
        try:
            response = await original_async(client, request)
        except Exception:
            evidence.error_seen = True
            raise
        if response.is_success:
            evidence.http_ok[route] += 1
        elif not response.is_redirect:
            evidence.error_seen = True
        evidence.http_statuses[str(response.status_code)] += 1
        return response

    def send_sync(client, request):
        route = evidence.http(request.method, request.url)
        try:
            response = original_sync(client, request)
        except Exception:
            evidence.error_seen = True
            raise
        if response.is_success:
            evidence.http_ok[route] += 1
        elif not response.is_redirect:
            evidence.error_seen = True
        evidence.http_statuses[str(response.status_code)] += 1
        return response

    def dns(host, port, *args, **kwargs):
        name = host.decode("ascii") if isinstance(host, bytes) else host
        if name not in ips | {HOST, WS_HOST} or port not in (443, "443"):
            evidence.deny("DNS_DESTINATION_BLOCKED")
        return lookup(host, port, *args, **kwargs)

    def destination(address):
        if (
            not isinstance(address, tuple)
            or len(address) < 2
            or address[0] not in ips
            or address[1] != 443
        ):
            evidence.deny("TCP_DESTINATION_BLOCKED")

    def guarded_connect(sock, address):
        destination(address)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        destination(address)
        return connect_ex(sock, address)

    @contextlib.asynccontextmanager
    async def ws_connect(url, **kwargs):
        evidence.ws_url(url)
        if any(key in kwargs for key in ("sock", "host", "port", "create_connection")):
            evidence.deny("WS_TRANSPORT_OVERRIDE_BLOCKED")
        kwargs.update(proxy=None, open_timeout=10, close_timeout=3, max_size=2**20)
        phase = "handshake"
        try:
            async with original_ws(url, **kwargs) as connection:
                phase = "session"
                yield ReadOnlyWS(connection, evidence)
                phase = "close"
        except Exception as exc:
            # Sin texto, traceback, URL, headers ni body. El nombre de una clase
            # desconocida tampoco sale: cae al bucket estático OtherError.
            kind = type(exc).__name__
            evidence.ws_error_types[kind if kind in WS_ERROR_TYPES else "OtherError"] += 1
            evidence.ws_failure_phases[phase] += 1
            evidence.error_seen = True
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if phase == "handshake" and type(status) is int and 100 <= status <= 599:
                evidence.ws_handshake_statuses[str(status)] += 1
            raise

    with contextlib.ExitStack() as stack:
        for target, replacement in (
            ("httpx.AsyncClient._send_single_request", send_async),
            ("httpx.Client._send_single_request", send_sync),
            ("websockets.connect", ws_connect),
            ("socket.getaddrinfo", dns),
            ("socket.socket.connect", guarded_connect),
            ("socket.socket.connect_ex", guarded_connect_ex),
        ):
            stack.enter_context(patch(target, replacement))
        yield


def configure(workdir: Path, credentials: Path, health_port: int):
    """No dotenv, env heredada, PEM dentro del repo ni rutas ambiguas/symlinks."""
    from src.utils.config import Settings, get_settings, reset_settings_for_testing

    credentials = credentials.resolve()
    if credentials.is_relative_to(ROOT):
        raise PermissionError("CREDENTIALS_IN_REPOSITORY")
    key_file, pem = credentials / "key_id.txt", credentials / "kalshi_private_key.pem"
    for file in (key_file, pem):
        info = file.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 32768:
            raise PermissionError("CREDENTIAL_FILE_INVALID")
    key_id = key_file.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{10,128}", key_id):
        raise ValueError("DEMO_KEY_ID_INVALID")
    os.environ.clear()
    os.environ.update(
        KALSHI_ENV="demo",
        KALSHI_API_KEY_ID=key_id,
        KALSHI_PRIVATE_KEY_PATH=str(pem),
        KALSHI_DEMO_REST_URL=REST,
        KALSHI_DEMO_WS_URL=WS,
        DATABASE_URL=f"sqlite:///{workdir / 'readiness.db'}",
        TRADING_ENABLED="false",
        MOTOR_1_ARBITRAGE_ENABLED="true",
        MOTOR_1_EXECUTION_ENABLED="false",
        USE_ORDERBOOK_MANAGER_V2="true",
        MOTOR_2_SPORTSBOOK_ENABLED="false",
        MOTOR_2_ENTRY_EXECUTION_ENABLED="false",
        MOTOR_2_EXECUTION_ENABLED="false",
        MOTOR_3_CLV_ENABLED="false",
        MOTOR_3_EXECUTION_ENABLED="false",
        MOTOR_REST_ENABLED="false",
        MOTOR_REST_EXECUTION_ENABLED="false",
        MOTOR_MM_ENABLED="false",
        MOTOR_MM_EXECUTION_ENABLED="false",
        MOTOR_6_LINEMOVE_ENABLED="false",
        MOTOR_8_OFI_ENABLED="false",
        MOTOR_9_SPILLOVER_ENABLED="false",
        ANALYST_LOOP_ENABLED="false",
        DAILY_PNL_REPORT_ENABLED="false",
        MEMORY_MONITOR_ENABLED="false",
        DB_MAINTENANCE_ENABLED="false",
        DISK_GUARD_ENABLED="false",
        TELEGRAM_DASHBOARD_ENABLED="false",
        BALANCE_REFRESH_SECONDS="30",
        DYNAMIC_CAPITAL_ENABLED="true",
        HEALTH_HOST="127.0.0.1",
        HEALTH_PORT=str(health_port),
    )
    Settings.model_config["env_file"] = None
    reset_settings_for_testing()
    return get_settings()


@contextlib.contextmanager
def runner_instrumentation(evidence: Evidence, series: str, *, event_ticker: str | None = None):
    from src.clients.kalshi_rest import KalshiRestClient
    from src.clients.odds_api import OddsAPIClient
    from src.runner import ProductionRunner
    from src.strategies import data_capture
    from src.strategies.motor_1_arbitrage.engine import Motor1Engine

    list_events = KalshiRestClient.list_events
    discover = data_capture.DataCaptureService._discover_markets
    tick = Motor1Engine._tick
    start_service = ProductionRunner._start_required_service

    if event_ticker is not None and (
        not TOKEN.fullmatch(event_ticker) or not event_ticker.startswith(series + "-")
    ):
        raise ValueError("INVALID_TARGET_EVENT")

    def supervised(runner, name, service):
        async def observed():
            if name not in REQUIRED_SERVICES:
                evidence.deny("EXTERNAL_PRODUCER_CONSTRUCTED")
            evidence.services_started.add(name)
            await service()

        return start_service(runner, name, observed)

    async def bounded_events(client, **kwargs):
        if event_ticker is not None:
            # Selección explícita para Demo: identidad devuelta por GET, jamás
            # metadata fabricada ni listado de producción. No acredita discovery amplio.
            payload = await client.get_event(event_ticker)
            event = payload.get("event") if isinstance(payload, dict) else None
            if (
                kwargs.get("series_ticker") != series
                or not isinstance(event, dict)
                or event.get("event_ticker") != event_ticker
                or event.get("series_ticker") != series
            ):
                evidence.deny("DISCOVERY_EVENT_IDENTITY_MISMATCH")
            return {"events": [event], "cursor": ""}
        kwargs["limit"] = 2
        result = await list_events(client, **kwargs)
        if not isinstance(result.get("events"), list) or len(result["events"]) > 2:
            evidence.deny("DISCOVERY_EVENT_BOUND_EXCEEDED")
        return result

    async def discovered(service):
        result = await discover(service)
        if len(service._tracked_tickers) > MAX_TICKERS:
            evidence.deny("DISCOVERY_MARKET_BOUND_EXCEEDED")
        evidence.discoveries += 1
        return result

    async def ticked(engine):
        if engine._executor is not None:
            evidence.deny("EXECUTOR_CONSTRUCTED")
        result = await tick(engine)
        evidence.ticks += 1
        return result

    def no_executor(*args, **kwargs):
        evidence.deny("EXECUTOR_CONSTRUCTED")

    def no_producer(*args, **kwargs):
        evidence.deny("EXTERNAL_PRODUCER_CONSTRUCTED")

    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(data_capture, "TARGET_SERIES_PREFIXES", [series]))
        stack.enter_context(patch.object(KalshiRestClient, "list_events", bounded_events))
        stack.enter_context(
            patch.object(data_capture.DataCaptureService, "_discover_markets", discovered)
        )
        stack.enter_context(patch.object(Motor1Engine, "_tick", ticked))
        stack.enter_context(patch.object(ProductionRunner, "_start_required_service", supervised))
        stack.enter_context(patch.object(OddsAPIClient, "__init__", no_producer))
        for module, name in (
            ("motor_1_arbitrage", "ArbitrageExecutor"),
            ("motor_2_consensus", "Motor2Executor"),
            ("motor_3_clv", "Motor3ExitExecutor"),
            ("motor_rest_arb", "RestExecutor"),
            ("motor_5_mm", "Motor5Executor"),
        ):
            cls = getattr(importlib.import_module(f"src.strategies.{module}.executor"), name)
            stack.enter_context(patch.object(cls, "__init__", no_executor))
        yield


async def run_runner(seconds: float, evidence: Evidence) -> dict:
    from sqlalchemy import func
    from sqlmodel import select

    from src.monitoring.health import BotState
    from src.runner import ProductionRunner
    from src.storage.models import Trade, get_session

    baseline = set(asyncio.all_tasks())
    runner = ProductionRunner()
    task = asyncio.create_task(runner.run(), name="bounded-production-runner")
    deadline = time.monotonic() + seconds
    try:
        while not task.done() and time.monotonic() < deadline and not evidence.blocked:
            evidence.db_initialized |= BotState.db_initialized
            evidence.capture_seen |= BotState.capture_running
            evidence.error_seen |= BotState.last_error is not None
            evidence.tracked_markets_peak = max(
                evidence.tracked_markets_peak, BotState.tracked_markets_count
            )
            evidence.health_started |= bool(
                runner._uvicorn_server and runner._uvicorn_server.started
            )
            if BotState.v2_manager is not None:
                evidence.initialized_books_peak = max(
                    evidence.initialized_books_peak,
                    BotState.v2_manager.stats()["initialized_tickers"],
                )
            await asyncio.sleep(0.25)
        await asyncio.wait_for(runner.shutdown("bounded_demo_readiness"), SHUTDOWN_SECONDS)
        evidence.runner_exit = await asyncio.wait_for(asyncio.shield(task), SHUTDOWN_SECONDS)
        evidence.shutdown_clean = True
    except Exception:
        evidence.error_seen = True
    finally:
        # runner.run puede esperar FIRST_EXCEPTION. Cancelar TODAS sus hermanas al
        # agotar plazo; este worker dedicado no comparte tasks con otra aplicación.
        pending = [t for t in asyncio.all_tasks() - baseline if not t.done()]
        for child in pending:
            child.cancel()
        if pending:
            _, unfinished = await asyncio.wait(pending, timeout=3)
            if unfinished:
                evidence.shutdown_clean = False
    evidence.error_seen |= BotState.last_error is not None
    if evidence.db_initialized:
        with get_session() as session:
            if session.exec(select(func.count()).select_from(Trade)).one() != 0:
                evidence.blocked["UNEXPECTED_TRADE_ROW"] += 1
    return evidence.report()


def _worker(seconds: int, series: str) -> dict:
    # Antes de importar el proyecto: neutralizar entorno, dotenv y cualquier log.
    credentials = Path.home() / ".config/botkalshi/demo"
    os.environ.clear()
    logging.disable(logging.CRITICAL)
    from loguru import logger

    logger.remove()
    evidence = Evidence()
    logger.add(lambda _message: setattr(evidence, "error_seen", True), level="ERROR")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="kalshi-demo-runner-") as directory:
        workdir = Path(directory)
        os.chdir(workdir)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        phase = "configure"
        try:
            configure(workdir, credentials, port)
            phase = "guards"
            with network_guards(evidence), runner_instrumentation(evidence, series):
                phase = "runner"
                return asyncio.run(run_runner(seconds, evidence))
        except Exception as exc:
            return blocked_report(exc, phase)


def blocked_report(exc: Exception, phase: str) -> dict:
    kind = type(exc).__name__
    return {
        "status": "BLOCKED",
        "environment": "demo",
        "real_money_orders": 0,
        "error_type": kind if kind in ERROR_TYPES else "OtherError",
        "phase": phase,
    }


def worker(seconds: int, series: str) -> dict:
    # Ni prints de librerías ni logs nativos pueden escapar por stdout. Sólo main
    # emite el objeto sanitizado al terminar; stderr se descarta en el padre.
    stdout_fd, stderr_fd = os.dup(1), os.dup(2)
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            return _worker(seconds, series)
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        os.close(stdout_fd)
        os.close(stderr_fd)


def checked_report(raw: bytes) -> dict:
    """Rechazar campos/texto arbitrarios del hijo antes de persistir o mostrar."""
    if len(raw) > 16384:
        raise ValueError("INVALID_WORKER_REPORT")
    value = json.loads(raw)
    if isinstance(value, dict) and value.get("status") == "BLOCKED":
        if (
            set(value) == {"status", "environment", "real_money_orders", "error_type", "phase"}
            and value["environment"] == "demo"
            and value["real_money_orders"] == 0
            and value["error_type"] in ERROR_TYPES
            and value["phase"] in PHASES
        ):
            return value
        raise ValueError("INVALID_WORKER_REPORT")
    template = Evidence().report()
    if not isinstance(value, dict) or set(value) != set(template):
        raise ValueError("INVALID_WORKER_REPORT")
    for key in ("schema", "environment", "scope", "real_money_orders"):
        if value[key] != template[key]:
            raise ValueError("INVALID_WORKER_REPORT")
    checks = value["checks"]
    if (
        not isinstance(checks, dict)
        or set(checks) != set(template["checks"])
        or any(type(v) is not bool for v in checks.values())
    ):
        raise ValueError("INVALID_WORKER_REPORT")
    if value["status"] != ("PASS_READ_ONLY" if all(checks.values()) else "INCOMPLETE"):
        raise ValueError("INVALID_WORKER_REPORT")
    if value["failed_gates"] != [key for key, passed in checks.items() if not passed]:
        raise ValueError("INVALID_WORKER_REPORT")
    services = value["services_started"]
    if not isinstance(services, list) or any(s not in REQUIRED_SERVICES for s in services):
        raise ValueError("INVALID_WORKER_REPORT")
    counts = value["counts"]
    if not isinstance(counts, dict) or set(counts) != set(template["counts"]):
        raise ValueError("INVALID_WORKER_REPORT")
    for key, count in counts.items():
        if key == "http_successes":
            if not isinstance(count, dict) or not set(count) <= {
                "balance",
                "events",
                "event",
                "book",
                "market",
            }:
                raise ValueError("INVALID_WORKER_REPORT")
            numbers = count.values()
        else:
            numbers = [count]
        if any(type(n) is not int or not 0 <= n <= 100000 for n in numbers):
            raise ValueError("INVALID_WORKER_REPORT")
    blocked = value["blocked"]
    if (
        not isinstance(blocked, dict)
        or not set(blocked) <= BLOCK_CODES
        or any(type(n) is not int or not 1 <= n <= 100000 for n in blocked.values())
    ):
        raise ValueError("INVALID_WORKER_REPORT")
    statuses = value["http_statuses"]
    if not isinstance(statuses, dict) or any(
        not re.fullmatch(r"[1-5][0-9]{2}", key) or type(n) is not int or not 1 <= n <= MAX_HTTP
        for key, n in statuses.items()
    ):
        raise ValueError("INVALID_WORKER_REPORT")
    for field_name, allowed in (
        ("ws_error_types", WS_ERROR_TYPES),
        ("ws_failure_phases", WS_FAILURE_PHASES),
    ):
        counter = value[field_name]
        if (
            not isinstance(counter, dict)
            or not set(counter) <= allowed
            or any(type(n) is not int or not 1 <= n <= 3 for n in counter.values())
        ):
            raise ValueError("INVALID_WORKER_REPORT")
    statuses = value["ws_handshake_statuses"]
    if not isinstance(statuses, dict) or any(
        not re.fullmatch(r"[1-5][0-9]{2}", key) or type(n) is not int or not 1 <= n <= 3
        for key, n in statuses.items()
    ):
        raise ValueError("INVALID_WORKER_REPORT")
    return value


def save_report(path: Path, result: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, indent=2)


def isolated_command(seconds: int, series: str) -> tuple[list[str], dict[str, str]]:
    return (
        [
            sys.executable,
            "-I",
            str(Path(__file__).resolve()),
            "--worker",
            "--seconds",
            str(seconds),
            "--series",
            series,
        ],
        {"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1", "TZ": "UTC"},
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--series", default="KXELONMARS")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not 60 <= args.seconds <= 90 or not re.fullmatch(r"KX[A-Z0-9]{1,30}", args.series):
        parser.error("seconds debe estar entre 60 y 90; serie Kalshi exacta requerida")
    if not args.run and not args.worker:
        print(
            json.dumps(
                {
                    "status": "PLAN_ONLY",
                    "seconds": args.seconds,
                    "series": args.series,
                    "environment": "demo",
                    "http_methods": ["GET"],
                    "max_http": MAX_HTTP,
                    "max_events": 2,
                    "max_markets": MAX_TICKERS,
                    "credentials_read": False,
                }
            )
        )
        return 0
    result = {"status": "BLOCKED", "environment": "demo", "real_money_orders": 0}
    try:
        if args.worker:
            result = worker(args.seconds, args.series)
        else:
            if args.report is None or args.report.exists():
                raise ValueError("FRESH_REPORT_PATH_REQUIRED")
            command, env = isolated_command(args.seconds, args.series)
            completed = subprocess.run(
                command,
                env=env,
                cwd=tempfile.gettempdir(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=args.seconds + 2 * SHUTDOWN_SECONDS + 15,
                check=False,
            )
            result = checked_report(completed.stdout)
            if completed.returncode != (0 if result["status"] == "PASS_READ_ONLY" else 1):
                raise ValueError("WORKER_EXIT_MISMATCH")
    except Exception as exc:
        result = blocked_report(exc, "worker" if args.worker else "parent")
    if not args.worker and args.report is not None:
        try:
            save_report(args.report, result)
        except (OSError, ValueError):
            result = {
                "status": "BLOCKED",
                "environment": "demo",
                "error_type": "ReportWriteError",
                "real_money_orders": 0,
                "phase": "report",
            }
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS_READ_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
