"""Recuperación nocturna offline: runner, ledger y conciliación reales.

Sólo HTTP, balance y servicios ajenos se sustituyen. Los reinicios son nuevas
instancias con conexión SQLite reabierta y estado volátil descartado, no procesos
del SO ni un kill -9. Nunca usa credenciales, cuenta, Odds API ni el probe demo.
"""

from __future__ import annotations

import asyncio
import copy
import json
import socket
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
import src.strategies.motor_5_mm.incidents as incidents
from src.clients.kalshi_rest import KalshiRestClient, _WriteThrottle
from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.runner import ProductionRunner
from src.storage.quantities import quantity_fields
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.engine import Motor5Engine
from src.strategies.motor_5_mm.executor import Motor5Executor
from src.utils.config import Settings

TICKER = "KXMLBGAME-26OCT07PHINYM-PHI"


def _rows():
    # Otra sesión demuestra que el intent ya se confirmó, no sólo se añadió al ORM.
    with models.get_session() as session:
        return list(session.exec(select(models.Trade)))


def _entry():
    return ArbOpportunity(
        legs=(ArbLeg("KXMLBGAME-OTHER-NYM", "yes", 45, 1, 1),),
        count=1,
        gross_profit_cents=0,
        fees_cents=0,
        net_profit_cents=0,
        edge_pct=0.0,
    )


class _Exchange:
    """Kalshi ficticio: aceptar POST no obliga a que el listado ya lo vea."""

    def __init__(self):
        self.phase = "resting"
        self.orders = {}
        self.requests = []
        self.pre_post = []
        self.errors = []

    def order(self, oid):
        value = dict(self.orders[oid])
        if self.phase == "terminal":
            value.update(status="canceled", remaining_count_fp="0.00")
        return value

    def handle(self, request):
        try:
            return self._handle(request)
        except BaseException as exc:
            # Un executor trata errores de transporte como incertidumbre. Una
            # aserción del fixture nunca debe convertirse en un test verde por eso.
            self.errors.append(exc)
            raise

    def _handle(self, request):
        method, path = request.method, request.url.path.removeprefix("/trade-api/v2")
        self.requests.append((method, path))
        if method == "POST" and path == "/portfolio/events/orders":
            body = json.loads(request.content)
            rows = _rows()
            matching = [r for r in rows if r.client_order_id == body["client_order_id"]]
            assert len(matching) == 1 and matching[0].status == "pending"
            assert matching[0].kalshi_order_id is None
            assert RiskManager()._get_current_exposure_usd() == pytest.approx(0.45)
            self.pre_post.append(matching[0].client_order_id)
            oid = f"offline-{len(self.orders) + 1}"
            assert not self.orders, "no se permite un POST duplicado ni una segunda quote"
            self.orders[oid] = {
                "order_id": oid,
                "client_order_id": body["client_order_id"],
                "ticker": body["ticker"],
                "status": "resting",
                "side": "yes",
                "action": "buy",
                "outcome_side": "yes",
                "book_side": "bid",
                "initial_count_fp": "1.00",
                "fill_count_fp": "0.00",
                "remaining_count_fp": "1.00",
                "exchange_index": 0,
                "subaccount_number": 0,
            }
            assert body["side"] == "bid" and body["count"] == "1"
            assert body["price"] == "0.45" and body["post_only"] is True
            return httpx.Response(201, json=self.order(oid))
        if method == "GET" and path == "/portfolio/orders":
            orders = [] if self.phase == "absent" else [self.order(k) for k in self.orders]
            return httpx.Response(200, json={"orders": orders, "cursor": ""})
        if method == "GET" and path.startswith("/portfolio/orders/"):
            # El GET individual también queda resting después del ACK de DELETE.
            return httpx.Response(200, json={"order": self.order(path.rsplit("/", 1)[1])})
        if method == "DELETE" and path.startswith("/portfolio/events/orders/"):
            if path.endswith("/batched"):
                ids = [item["order_id"] for item in json.loads(request.content)["orders"]]
                return httpx.Response(200, json={"orders": [self.ack(oid) for oid in ids]})
            return httpx.Response(200, json=self.ack(path.rsplit("/", 1)[1]))
        if method == "GET" and path.endswith("/orderbook"):
            return httpx.Response(200, json={"orderbook": {"yes": [[40, 10]], "no": [[40, 10]]}})
        raise AssertionError(f"HTTP no previsto en la simulación: {method} {path}")

    def ack(self, oid):
        return {
            "order_id": oid,
            "client_order_id": self.orders[oid]["client_order_id"],
            "reduced_by": "1.00",
        }

    @property
    def posts(self):
        return sum(method == "POST" for method, _ in self.requests)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """Fronteras cerradas: SQLite local, configuración explícita y sockets prohibidos."""

    def no_network(*args, **kwargs):
        raise AssertionError("la prueba nocturna prohíbe red real")

    for attr in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, attr, no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    settings = Settings.model_construct(
        KALSHI_ENV="demo",
        KALSHI_API_KEY_ID="offline-only",
        DATABASE_URL=f"sqlite:///{tmp_path}/night.db",
        TRADING_ENABLED=True,
        MOTOR_1_ARBITRAGE_ENABLED=False,
        MOTOR_2_SPORTSBOOK_ENABLED=False,
        MOTOR_REST_ENABLED=False,
        MOTOR_3_CLV_ENABLED=False,
        MOTOR_MM_ENABLED=True,
        MOTOR_MM_EXECUTION_ENABLED=True,
        MOTOR_MM_SERIES="KXMLBGAME",
        MOTOR_MM_REQUIRE_PREGAME=False,
        MOTOR_MM_FEES_AS_MAKER=False,
        MOTOR_MM_HALF_SPREAD_CENTS=5,
        MOTOR_MM_EDGE_SKEW_CENTS=0,
        MOTOR_MM_QUOTE_SIZE_CONTRACTS=1,
        # El canary real admite sólo BUY 45c; la segunda pata no cabe.
        MOTOR_MM_MAX_EXPOSURE_USD=0.45,
        ACTIVE_CAPITAL_USD=1000,
        CAPITAL_CAP_USD=1000,
        CAPITAL_FLOOR_USD=100,
        CAPITAL_SAFETY_FACTOR_PCT=90,
        UNREALIZED_STOP_ENABLED=False,
        ANALYST_LOOP_ENABLED=False,
        DAILY_PNL_REPORT_ENABLED=False,
    )
    for module in ("src.runner", "src.risk.manager", "src.storage.models"):
        monkeypatch.setattr(f"{module}.get_settings", lambda: settings)
    for name, value in vars(BotState).copy().items():
        if not name.startswith("_") and not callable(value) and not isinstance(value, classmethod):
            monkeypatch.setattr(BotState, name, copy.deepcopy(value))
    monkeypatch.setattr(incidents, "_current_execution_owner", None)
    monkeypatch.setattr(Motor5Executor, "_locks", {})
    monkeypatch.setattr(RiskManager, "_check_lock", asyncio.Lock())
    monkeypatch.setattr(RiskManager, "_marks", {})
    monkeypatch.setattr(RiskManager, "_cached_capital_usd", 1000.0)
    monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 1000.0)
    monkeypatch.setattr(RiskManager, "_last_balance_at", datetime.now(UTC).replace(tzinfo=None))
    monkeypatch.setattr(models, "_engine", create_engine(settings.DATABASE_URL))
    models.init_db()
    FairValueBook.clear()
    backend = _Exchange()

    class OfflineClient(KalshiRestClient):
        """Conserva place/cancel/list/request reales; sustituye auth y transporte."""

        def __init__(self):
            self.settings = settings
            self.base_url = "https://offline.invalid/trade-api/v2"
            self.signer = SimpleNamespace(sign=lambda *args: {})
            self._client = None
            self._write_throttle = _WriteThrottle(rate_per_sec=10000, burst=10000)

        async def __aenter__(self):
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                transport=httpx.MockTransport(backend.handle),
                trust_env=False,
                follow_redirects=False,
            )
            return self

    monkeypatch.setattr("src.runner.KalshiRestClient", OfflineClient)
    for name in ("alert_startup", "alert_shutdown", "send_alert"):
        monkeypatch.setattr(f"src.runner.{name}", AsyncMock())
    monkeypatch.setattr(RiskManager, "refresh_capital_from_balance", AsyncMock(return_value=None))
    original_sleep = asyncio.sleep

    async def skip_startup_delay(*args, **kwargs):
        await original_sleep(0)

    monkeypatch.setattr("src.runner.asyncio.sleep", skip_startup_delay)
    monkeypatch.setattr(Motor5Engine, "LOOP_INTERVAL_SEC", 0.001)
    state = SimpleNamespace(settings=settings, exchange=backend, client=OfflineClient)

    def restart():
        models.get_engine().dispose()
        monkeypatch.setattr(models, "_engine", create_engine(settings.DATABASE_URL))
        BotState.is_paused = False
        BotState.pause_reason = None
        incidents._current_execution_owner = None
        Motor5Executor._locks = {}
        RiskManager._check_lock = asyncio.Lock()
        FairValueBook.clear()

    state.restart = restart
    restart()
    yield state
    FairValueBook.clear()
    models.get_engine().dispose()


async def _run(lab, monkeypatch, *, after_tick=None, after_boot=None):
    """Ejecuta el supervisor y boot reales; acelera sólo calendario y servicios ajenos."""
    runner = ProductionRunner()
    engines, callback_errors = [], []
    completed_ticks = []

    async def idle():
        await runner._stop_event.wait()

    async def capture():
        try:
            if after_boot is not None:
                await after_boot()
            if not lab.settings.MOTOR_MM_ENABLED:
                runner._stop_event.set()
        except BaseException as exc:
            callback_errors.append(exc)
            runner._stop_event.set()
            raise
        await runner._stop_event.wait()

    original_tick = Motor5Engine._tick

    def engine_factory(**kwargs):
        engine = Motor5Engine(**kwargs, client_factory=lab.client)
        engines.append(engine)
        completed_ticks.append(0)
        ticks = 0

        async def tick():
            nonlocal ticks
            await original_tick(engine)
            ticks += 1
            completed_ticks[-1] = ticks
            try:
                stop = True if after_tick is None else await after_tick(engine, ticks)
            except BaseException as exc:
                callback_errors.append(exc)
                raise
            if stop:
                runner._stop_event.set()

        engine._tick = tick
        return engine

    with monkeypatch.context() as scoped:
        scoped.setattr("src.strategies.motor_5_mm.engine.Motor5Engine", engine_factory)
        scoped.setattr(runner, "_run_data_capture", capture)
        for name in (
            "_run_health_server",
            "_run_settlement",
            "_run_telegram_commands",
            "_run_balance_refresh",
            "_run_memory_monitor",
            "_run_db_maintenance",
            "_run_disk_guard",
            "_run_motor1_arb",
            "_run_motor2_shadow",
            "_run_motor2_exits",
        ):
            scoped.setattr(runner, name, idle)
        try:
            result = await asyncio.wait_for(runner.run(), timeout=5)
        finally:
            runner._stop_event.set()
    if callback_errors:
        raise callback_errors[0]
    if lab.exchange.errors:
        raise lab.exchange.errors[0]
    assert all(ticks > 0 for ticks in completed_ticks), BotState.last_error
    return result, engines


async def test_runner_eventual_cancel_preserves_reserve_across_instances(lab, monkeypatch):
    """Aceptada → ausente → resting tras cancel → nuevo dueño → terminal; un POST."""
    FairValueBook.publish({TICKER: 0.50})
    snapshots = []

    async def first_process(engine, tick):
        snapshots.append((_rows()[0].status, RiskManager()._get_current_exposure_usd()))
        assert lab.exchange.posts == 1
        if tick == 1:
            assert len(engine._executor.live_coids()) == 1
            lab.exchange.phase = "absent"
            return False
        assert incidents.incident_status()[0]
        assert len(engine._executor.live_coids()) == 1
        assert not (await RiskManager().check_pre_trade(_entry())).approved
        return True

    exit_code, first = await _run(lab, monkeypatch, after_tick=first_process)
    assert exit_code == 1  # El shutdown no presenta el cancel inconcluso como limpio.
    assert snapshots == [("pending", 0.45), ("pending", 0.45)]
    assert len(lab.exchange.pre_post) == 1
    coid = _rows()[0].client_order_id
    assert first[0]._executor.live_coids() == {coid}
    assert BotState.is_paused and incidents.incident_status()[0]
    assert any(method == "DELETE" for method, _ in lab.exchange.requests)

    lab.restart()
    lab.exchange.phase = "resting"
    FairValueBook.publish({TICKER: 0.50})
    cancel_count = sum(method == "DELETE" for method, _ in lab.exchange.requests)

    async def recovering(engine, tick):
        assert engine is not first[0]
        assert BotState.motor5_experiment_invalid
        assert "1 run(s) no-clean: invalid" in BotState.motor5_experiment_invalid_reason
        assert _rows()[0].status == "pending"
        assert RiskManager()._get_current_exposure_usd() == pytest.approx(0.45)
        assert incidents.incident_status()[0]
        assert not (await RiskManager().check_pre_trade(_entry())).approved
        return True

    exit_code, second = await _run(lab, monkeypatch, after_tick=recovering)
    assert exit_code == 1 and second[0]._executor is not first[0]._executor
    assert sum(method == "DELETE" for method, _ in lab.exchange.requests) > cancel_count
    assert _rows()[0].status == "pending" and lab.exchange.posts == 1

    lab.restart()
    lab.exchange.phase = "terminal"
    FairValueBook.publish({TICKER: 0.50})

    async def confirmed(engine, tick):
        row = _rows()[0]
        assert row.status == "cancelled" and row.filled_count_fp == "0.00"
        assert RiskManager()._get_current_exposure_usd() == 0
        assert not engine._executor.live_coids()
        assert incidents.incident_status()[0]  # Cierre no equivale a permiso manual.
        assert not (await RiskManager().check_pre_trade(_entry())).approved
        return True

    exit_code, third = await _run(lab, monkeypatch, after_tick=confirmed)
    assert exit_code == 0 and third[0]._executor is not second[0]._executor
    assert lab.exchange.posts == 1 and len(_rows()) == 1
    with models.get_session() as session:
        assert session.get(models.OperationalState, incidents.SESSION_KEY).value == "clean"
        assert len(list(session.exec(select(models.BotRun)))) == 3
        runs = list(
            session.exec(select(models.MMExperimentRun).order_by(models.MMExperimentRun.id))
        )
        # Una cohorte ya inválida puede dejar su siguiente época como running;
        # ambas representaciones siguen siendo no-clean y jamás se blanquean.
        assert len(runs) == 3
        assert runs[0].status == "invalid" and runs[1].status != "clean"
        assert runs[2].status == "clean"
    assert BotState.motor5_experiment_invalid
    assert "2 run(s) no-clean: invalid" in BotState.motor5_experiment_invalid_reason


@pytest.mark.parametrize(
    ("enabled", "trading", "execution"),
    [(False, True, True), (True, False, True), (True, True, False)],
)
async def test_runner_flags_never_construct_execution_when_gate_is_closed(
    lab, monkeypatch, enabled, trading, execution
):
    lab.settings.MOTOR_MM_ENABLED = enabled
    lab.settings.TRADING_ENABLED = trading
    lab.settings.MOTOR_MM_EXECUTION_ENABLED = execution
    FairValueBook.publish({TICKER: 0.50})
    result, engines = await _run(lab, monkeypatch)
    assert result == 0
    assert len(engines) == int(enabled)
    assert all(engine._executor is None and engine._reconciler is None for engine in engines)
    assert _rows() == [] and lab.exchange.posts == 0


@pytest.mark.parametrize(
    ("strategy", "flag"),
    [
        ("motor_1_arbitrage", "MOTOR_1_ARBITRAGE_ENABLED"),
        ("motor_2_consensus", "MOTOR_2_SPORTSBOOK_ENABLED"),
        ("motor_rest_arb", "MOTOR_REST_ENABLED"),
    ],
)
async def test_full_boot_retains_unknown_intent_and_pause_after_terminal(
    lab, monkeypatch, strategy, flag
):
    """Boot GET-only real para motores compartidos; ningún cancel ni POST del hook."""
    lab.settings.MOTOR_MM_ENABLED = False
    setattr(lab.settings, flag, True)
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="prior-process",
                kalshi_order_id="prior-id",
                ticker=TICKER,
                side="yes",
                action="buy",
                price_cents=45,
                status="pending",
                strategy=strategy,
                **quantity_fields(1),
            )
        )
        session.commit()
    lab.exchange.orders["prior-id"] = {
        "order_id": "prior-id",
        "client_order_id": "prior-process",
        "ticker": TICKER,
        "side": "yes",
        "action": "buy",
        "status": "resting",
        "fill_count_fp": "0.00",
        "initial_count_fp": "1.00",
        "remaining_count_fp": "1.00",
    }
    for phase in ("absent", "resting", "terminal"):
        lab.restart()
        lab.exchange.phase = phase

        async def gate(phase=phase):
            terminal = phase == "terminal"
            assert _rows()[0].status == ("cancelled" if terminal else "pending")
            assert RiskManager()._get_current_exposure_usd() == (0 if terminal else 0.45)
            assert BotState.is_paused and models.kill_switch_engaged()[0]
            assert not (await RiskManager().check_pre_trade(_entry())).approved

        result, engines = await _run(lab, monkeypatch, after_boot=gate)
        assert result == 0 and engines == []
    assert lab.exchange.requests == [("GET", "/portfolio/orders")] * 3
    assert len(_rows()) == 1


async def test_full_boot_without_pending_has_real_risk_permission(lab, monkeypatch):
    """Control: el mismo gate puede aprobar con cash ficticio fresco y DB limpia."""
    lab.settings.MOTOR_MM_ENABLED = False
    lab.settings.MOTOR_2_SPORTSBOOK_ENABLED = True

    async def gate():
        assert not BotState.is_paused and not models.kill_switch_engaged()[0]
        assert (await RiskManager().check_pre_trade(_entry())).approved

    result, _ = await _run(lab, monkeypatch, after_boot=gate)
    assert result == 0 and lab.exchange.requests == [] and _rows() == []
