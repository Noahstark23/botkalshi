"""Validación Demo shadow con datos SINTÉTICOS y transports locales; nunca red."""

import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from scripts import demo_multi_readiness as smoke
from tests.scripts import test_demo_runner_readiness as baseline_tests

isolated_settings = baseline_tests.isolated_settings


def document(*, mismatch=False, books=3, age=60):
    now = datetime.now(UTC)
    kickoff = now + timedelta(hours=3)
    teams = (
        ("Boston Red Sox", "Baltimore Orioles")
        if mismatch
        else ("Philadelphia Phillies", "New York Mets")
    )
    return {
        "fetched_at": (now - timedelta(seconds=age)).isoformat(),
        "sport": "baseball_mlb",
        "params": {"regions": "us", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"},
        "events": [
            {
                "id": "synthetic-not-provider-data",
                "sport_key": "baseball_mlb",
                "commence_time": kickoff.isoformat(),
                "home_team": teams[0],
                "away_team": teams[1],
                "bookmakers": [
                    {
                        "key": f"synthetic_book_{i}",
                        "last_update": (now - timedelta(seconds=age)).isoformat(),
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": team, "price": 1.9, "point": None} for team in teams
                                ],
                            }
                        ],
                    }
                    for i in range(books)
                ],
            }
        ],
    }


def source_file(tmp_path, payload):
    directory = tmp_path.resolve() / "source"
    directory.mkdir()
    (directory / "odds-latest.json").write_text(json.dumps(payload))
    return directory


def configured(isolated_settings):
    credentials, workdir = isolated_settings
    return smoke.configure(workdir, credentials, 18765, workdir.resolve())


def test_opt_in_validates_real_settings_and_does_not_inherit_execution(isolated_settings):
    from src.utils.config import Settings

    os.environ.update(
        TRADING_ENABLED="true", MOTOR_2_ENTRY_EXECUTION_ENABLED="true", ODDS_API_KEY="never-read"
    )
    cfg = configured(isolated_settings)
    assert cfg.KALSHI_ENV == "demo" and cfg.MOTOR_MM_DEMO_READONLY_ENABLED
    assert cfg.EXPERIMENT_DYNAMIC_MULTI_ENABLED and cfg.MOTOR_1_ARBITRAGE_ENABLED
    assert cfg.MOTOR_2_SPORTSBOOK_ENABLED and cfg.MOTOR_MM_ENABLED
    assert not cfg.TRADING_ENABLED and not cfg.MOTOR_2_ENTRY_EXECUTION_ENABLED
    assert not cfg.ODDS_API_KEY and cfg.MOTOR_MM_FAIR_TTL_SEC == 360
    assert Settings.model_fields["MOTOR_MM_DEMO_READONLY_ENABLED"].default is False


@pytest.mark.parametrize(
    "flag",
    [
        "TRADING_ENABLED",
        "MOTOR_1_EXECUTION_ENABLED",
        "MOTOR_2_ENTRY_EXECUTION_ENABLED",
        "MOTOR_2_EXECUTION_ENABLED",
        "MOTOR_3_EXECUTION_ENABLED",
        "MOTOR_REST_EXECUTION_ENABLED",
        "MOTOR_MM_EXECUTION_ENABLED",
    ],
)
def test_readonly_flag_rejects_every_execution_independent_of_multi_profile(
    isolated_settings, flag
):
    from src.utils.config import Settings

    fields = configured(isolated_settings).model_dump()
    fields.update(EXPERIMENT_DYNAMIC_MULTI_ENABLED=False)
    fields[flag] = True
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"KALSHI_ENV": "production"},
        {"MOTOR_MM_DEMO_READONLY_ENABLED": False},
        {"MOTOR_MM_FEES_AS_MAKER": False},
        {"MOTOR_2_MIN_BOOKS": 2},
        {"MOTOR_2_MAX_BOOK_AGE_MIN": 16},
        {"MOTOR_MM_QUOTE_SIZE_CONTRACTS": 2},
        {"MOTOR_MM_REQUIRE_PREGAME": False},
        {"MOTOR_2_SPORTSBOOK_ENABLED": False},
    ],
)
def test_demo_opt_in_does_not_relax_f1_data_gates(isolated_settings, changes):
    from src.utils.config import Settings

    fields = configured(isolated_settings).model_dump()
    fields.update(changes)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **fields)


def test_engine_contract_and_metric_never_relabel_demo_as_production():
    from src.strategies.motor_5_mm.engine import Motor5Engine

    shared = {"fees_as_maker": True, "series_csv": smoke.SERIES}
    production = Motor5Engine(**shared, exchange_environment="production")
    demo = Motor5Engine(**shared, exchange_environment="demo", demo_readonly_validation=True)
    assert production.F1_METRIC_VERSION == "f1-v2-bbo-depth"
    assert demo.F1_METRIC_VERSION == smoke.METRIC
    assert demo._experiment_id != production._experiment_id
    assert demo._executor is None and not demo._trading_enabled
    for kwargs in (
        {"exchange_environment": "production", "demo_readonly_validation": True},
        {"exchange_environment": "demo", "demo_readonly_validation": True, "trading_enabled": True},
        {"exchange_environment": "demo"},
    ):
        with pytest.raises(ValueError):
            Motor5Engine(**shared, **kwargs)


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH", "HEAD", "OPTIONS"])
async def test_added_series_route_never_allows_mutation_before_transport(method):
    evidence = smoke.Evidence()
    calls = []
    with smoke.base.network_guards(evidence, allowed_ips={"192.0.2.1"}):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: calls.append(request))
        ) as client:
            with pytest.raises(PermissionError, match="HTTP_MUTATION_BLOCKED"):
                await client.request(method, smoke.base.REST + "/series/KXMLBGAME")
    assert calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://api.elections.kalshi.com/trade-api/v2/series/KXMLBGAME",
        "https://api.the-odds-api.com/v4/sports",
        "https://api.typesafe.ai/v1/systemone",
    ],
)
def test_exact_demo_host_still_required(url):
    with pytest.raises(PermissionError, match="HTTP_HOST_BLOCKED"):
        smoke.Evidence().http("GET", url)


async def test_snapshot_copy_preserves_bytes_and_original_dates(tmp_path):
    doc = document()
    source = source_file(tmp_path, doc)
    work = tmp_path.resolve() / "work"
    work.mkdir()
    evidence = smoke.Evidence()
    destination = await smoke.prepare_snapshot(source, work, evidence)
    raw = (source / "odds-latest.json").read_bytes()
    assert (destination / "odds-latest.json").read_bytes() == raw
    assert evidence.snapshot_sha256 == hashlib.sha256(raw).hexdigest()
    assert evidence.snapshot_fetched_at == doc["fetched_at"]
    assert evidence.snapshot_age_seconds >= 60
    assert evidence.snapshot_checked


@pytest.mark.parametrize("age", [601, -60])
async def test_expired_or_future_snapshot_fails_before_credentials_or_network(tmp_path, age):
    source = source_file(tmp_path, document(age=age))
    work = tmp_path.resolve() / "work"
    work.mkdir()
    evidence = smoke.Evidence()
    with pytest.raises(ValueError):
        await smoke.prepare_snapshot(source, work, evidence)
    assert not evidence.snapshot_checked and not evidence.http_attempts


def test_default_plan_does_not_load_snapshots_or_credentials(monkeypatch, capsys):
    monkeypatch.setattr(smoke, "worker", lambda *args: pytest.fail("worker not allowed"))
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("subprocess not allowed")
    )
    assert smoke.main([]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "PLAN_ONLY" and not plan["credentials_read"]
    assert not plan["execution_validated"]


def test_report_rejects_secret_text_in_any_extended_field():
    report = smoke.Evidence().report()
    assert smoke.checked_report(json.dumps(report).encode()) == report
    for key, replacement in (
        ("error_type", "secret"),
        ("services_started", ["secret"]),
        ("snapshot", {**report["snapshot"], "sha256": "secret"}),
        ("counts", {**report["counts"], "m2_cycles": "secret"}),
    ):
        with pytest.raises(ValueError):
            smoke.checked_report(json.dumps({**report, key: replacement}).encode())


@pytest.mark.parametrize(
    "case,accept",
    [("matching", True), ("mismatch", False), ("insufficient_books", False), ("bad_fee", False)],
)
async def test_real_runner_demo_snapshot_to_m2_fair_to_m5_shadow(
    isolated_settings, monkeypatch, tmp_path, case, accept
):
    """ProductionRunner.run real + HTTP/WS falsos; no es una observación conectada."""
    import websockets
    from websockets.connection import State

    from src.monitoring.health import BotState
    from src.runner import ProductionRunner
    from src.strategies.motor_1_arbitrage.engine import Motor1Engine
    from src.strategies.motor_5_mm.engine import Motor5Engine

    payload = document(mismatch=case == "mismatch", books=2 if case == "insufficient_books" else 3)
    source = source_file(tmp_path, payload)
    credentials, work = isolated_settings
    evidence = smoke.Evidence()
    snapshot = await smoke.prepare_snapshot(source, work.resolve(), evidence)
    smoke.configure(work, credentials, 18765, snapshot)
    kickoff = datetime.fromisoformat(payload["events"][0]["commence_time"])
    event_id = f"KXMLBGAME-{kickoff:%y%b%d}PHINYM".upper()
    event = {"event_ticker": event_id, "series_ticker": smoke.SERIES}
    markets = [
        {
            "ticker": f"{event_id}-{suffix}",
            "status": "active",
            "event_ticker": event_id,
            "yes_sub_title": team,
            "close_time": (kickoff + timedelta(hours=6)).isoformat(),
            "open_time": "2020-01-01T00:00:00Z",
            "yes_bid": 20,
            "no_bid": 20,
            "yes_ask": 80,
            "no_ask": 80,
        }
        for suffix, team in (("PHI", "Philadelphia Phillies"), ("NYM", "New York Mets"))
    ]
    requests = []

    async def transport(_self, request):
        requests.append(request)
        path = request.url.path
        if path.endswith("/balance"):
            body = {"balance": 20000, "portfolio_value": 0}
        elif path.endswith("/events"):
            body = {"events": [event]}
        elif "/events/" in path:
            body = {"event": event, "markets": markets}
        elif "/series/" in path:
            body = {
                "series": {
                    "fee_type": "quadratic_with_maker_fees",
                    "fee_multiplier": 2 if case == "bad_fee" else 0.5,
                }
            }
        elif path.endswith("/orderbook"):
            body = {
                "orderbook_fp": {
                    "yes_dollars": [["0.2000", "100.00"]],
                    "no_dollars": [["0.2000", "100.00"]],
                }
            }
        else:
            body = {"market": next(m for m in markets if path.endswith(m["ticker"]))}
        return httpx.Response(200, json=body)

    class FakeSocket:
        state = State.OPEN

        async def send(self, message):
            assert json.loads(message)["cmd"] in {"subscribe", "update_subscription"}

        async def close(self, **kwargs):
            self.state = State.CLOSED

        async def __aiter__(self):
            yield json.dumps(
                {"type": "subscribed", "id": 1, "msg": {"channel": "orderbook_delta", "sid": 1}}
            )
            for index, market in enumerate(markets, 1):
                yield json.dumps(
                    {
                        "type": "orderbook_snapshot",
                        "sid": 1,
                        "seq": index,
                        "msg": {
                            "market_ticker": market["ticker"],
                            "yes_dollars_fp": [["0.2000", "100.00"]],
                            "no_dollars_fp": [["0.2000", "100.00"]],
                        },
                    }
                )
            await asyncio.Event().wait()

    @contextlib.asynccontextmanager
    async def fake_connect(url, **kwargs):
        assert url == smoke.base.WS and kwargs["proxy"] is None
        yield FakeSocket()

    real_sleep = asyncio.sleep

    async def accelerated_sleep(seconds):
        if seconds == 30:
            # El reloj acelerado del test no debe adelantar el arranque M2 al discovery.
            for _ in range(500):
                if BotState.tracked_markets_count >= 2:
                    break
                await real_sleep(0.003)
        await real_sleep(min(seconds, 0.003))

    async def local_health(runner):
        runner._uvicorn_server = SimpleNamespace(started=True, should_exit=False)
        await runner._stop_event.wait()

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", transport)
    monkeypatch.setattr(websockets, "connect", fake_connect)
    monkeypatch.setattr(asyncio, "sleep", accelerated_sleep)
    monkeypatch.setattr(Motor1Engine, "LOOP_INTERVAL_SEC", 0.005)
    monkeypatch.setattr(Motor5Engine, "LOOP_INTERVAL_SEC", 0.12)
    monkeypatch.setattr(ProductionRunner, "_run_health_server", local_health)
    with (
        smoke.base.network_guards(evidence, allowed_ips={"192.0.2.1"}),
        smoke.instrumentation(evidence),
    ):
        result = await smoke.run_runner(1.5, evidence)
    assert result["services_started"] == sorted(smoke.SERVICES), result
    assert result["counts"]["m2_cycles"] == 1
    assert result["counts"]["m2_exit_cycles"] > 0
    assert result["counts"]["m5_cycles"] >= 2
    assert result["counts"]["financial_rows"] == 0
    assert result["checks"]["financial_ledger_empty"]
    assert (result["counts"]["fair_items"] > 0) == (case in {"matching", "bad_fee"})
    assert (result["counts"]["shadow_quotes"] > 0) == accept
    assert result["status"] == ("HYBRID_READ_ONLY" if accept else "INCOMPLETE"), result
    assert not result["execution_validated"] and not result["source_authenticity_verified"]
    assert result["mode"] == "HYBRID_READ_ONLY" and result["fee_metric"] == smoke.METRIC
    assert all(
        request.method == "GET" and request.url.host == smoke.base.HOST for request in requests
    )
    assert smoke.checked_report(json.dumps(result).encode()) == result
