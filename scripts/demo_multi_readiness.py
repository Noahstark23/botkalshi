"""M1/M2/M5 Demo GET/WS + snapshot local inmutable; nunca órdenes ni Odds/Jev HTTP.

Plan por defecto. --run usa un worker -I, SQLite temporal y credenciales Demo
externas. HYBRID_READ_ONLY significa transporte Demo y evaluación shadow con
archivo local cuya autenticidad no se acredita. No valida ejecución ni beneficio.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # El worker -I no hereda PYTHONPATH.
    sys.path.insert(0, str(ROOT))

from scripts import demo_runner_readiness as base  # noqa: E402

SERVICES = base.REQUIRED_SERVICES | {"motor2_shadow", "motor2_exits", "motor5_mm", "position_marks"}
SERIES = "KXMLBGAME"
METRIC = "demo-readonly-v1-bbo-depth"
EXTRA_BLOCKS = {"SNAPSHOT_PREFLIGHT", "WORKER_ERROR", "UNEXPECTED_FINANCIAL_LEDGER"}


@dataclass
class Evidence(base.Evidence):
    m2_cycles: int = 0
    m2_exit_cycles: int = 0
    m5_cycles: int = 0
    fair_publications: int = 0
    fair_items: int = 0
    shadow_quotes: int = 0
    shadow_fills: int = 0
    financial_rows: int = 0
    ledger_checked: bool = False
    snapshot_sha256: str | None = None
    snapshot_fetched_at: str | None = None
    snapshot_age_seconds: int | None = None
    snapshot_events: int = 0
    snapshot_checked: bool = False
    error_type: str | None = None

    def http(self, method, url):
        parsed = urlsplit(str(url))
        if parsed.path == f"/trade-api/v2/series/{SERIES}":
            # Reutiliza TODOS los guards host/método/presupuesto, sin ampliar otros paths.
            super().http(method, parsed._replace(path=f"/trade-api/v2/markets/{SERIES}").geturl())
            return "series"
        return super().http(method, url)

    def report(self):
        value = super().report()
        checks = value["checks"]
        checks.update(
            supervised_services=self.services_started >= SERVICES,
            snapshot_preflight=self.snapshot_checked and self.snapshot_events > 0,
            m2_cycle=self.m2_cycles > 0,
            m2_exit_cycle=self.m2_exit_cycles > 0,
            m5_cycles=self.m5_cycles >= 2,
            m2_fair_published=self.fair_items > 0,
            m5_shadow_quotes=self.shadow_quotes > 0,
            financial_ledger_empty=self.ledger_checked and self.financial_rows == 0,
        )
        value.update(
            schema="demo-multi-readiness-v1",
            scope="bounded_M1_M2_M5_shadow_with_local_snapshot",
            mode="HYBRID_READ_ONLY",
            status="HYBRID_READ_ONLY" if all(checks.values()) else "INCOMPLETE",
            failed_gates=[key for key, passed in checks.items() if not passed],
            source_authenticity_verified=False,
            execution_validated=False,
            fee_metric=METRIC,
            error_type=self.error_type,
            snapshot={
                "kind": "LOCAL_SNAPSHOT_UNVERIFIED",
                "sha256": self.snapshot_sha256,
                "fetched_at": self.snapshot_fetched_at,
                "age_seconds_at_preflight": self.snapshot_age_seconds,
                "max_age_seconds": 600,
                "fair_ttl_seconds": 360,
                "events": self.snapshot_events,
                "checked": self.snapshot_checked,
            },
        )
        value["counts"].update(
            m2_cycles=self.m2_cycles,
            m2_exit_cycles=self.m2_exit_cycles,
            m5_cycles=self.m5_cycles,
            fair_publications=self.fair_publications,
            fair_items=self.fair_items,
            shadow_quotes=self.shadow_quotes,
            shadow_fills=self.shadow_fills,
            financial_rows=self.financial_rows,
        )
        return value


async def prepare_snapshot(source: Path, workdir: Path, evidence: Evidence) -> Path:
    """Copia exactamente una versión acotada, sin alterar ninguna fecha del payload."""
    from src.clients.odds_snapshot import OddsSnapshotClient, _timestamp

    client = OddsSnapshotClient(source)
    raw = client._read("odds-latest.json")
    evidence.snapshot_sha256 = hashlib.sha256(raw).hexdigest()
    # Sólo metadata tipada. El parser completo sigue siendo obligatorio después;
    # conservar hash/edad permite explicar rechazo sin publicar el contenido.
    metadata = json.loads(raw)
    if isinstance(metadata, dict):
        fetched = _timestamp(metadata.get("fetched_at"))
        evidence.snapshot_fetched_at = fetched.isoformat()
        evidence.snapshot_age_seconds = math.ceil((datetime.now(UTC) - fetched).total_seconds())
    destination = workdir / "snapshot"
    destination.mkdir(mode=0o700)
    path = destination / "odds-latest.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    snapshot = await OddsSnapshotClient(destination).read_snapshot("baseball_mlb")
    if not snapshot.events:
        raise ValueError("SNAPSHOT_WITHOUT_USABLE_EVENTS")
    evidence.snapshot_fetched_at = snapshot.fetched_at.isoformat()
    evidence.snapshot_age_seconds = math.ceil(
        (datetime.now(UTC) - snapshot.fetched_at).total_seconds()
    )
    evidence.snapshot_events = len(snapshot.events)
    evidence.snapshot_checked = True
    return destination


def configure(workdir: Path, credentials: Path, health_port: int, snapshot: Path):
    from src.utils.config import get_settings, reset_settings_for_testing

    base.configure(workdir, credentials, health_port)
    os.environ.update(
        EXPERIMENT_DYNAMIC_MULTI_ENABLED="true",
        EXPERIMENT_START_AT="2000-01-01T00:00:00Z",
        CAPITAL_SAFETY_FACTOR_PCT="90",
        CAPITAL_SMOOTHING_PCT="0",
        CAPITAL_FLOOR_USD="40.5",
        CAPITAL_CAP_USD="5000",
        BALANCE_MAX_AGE_SECONDS="60",
        EXPERIMENT_BALANCE_MAX_AGE_SEC="60",
        MAX_TRADE_SIZE_USD="1",
        MAX_TRADE_SIZE_PCT="3",
        MAX_SIMULTANEOUS_EXPOSURE_PCT="10",
        MAX_EVENT_DIRECTIONAL_EXPOSURE_USD="1",
        MAX_DAILY_LOSS_FLOOR_USD="0",
        MAX_WEEKLY_LOSS_FLOOR_USD="0",
        MAX_MONTHLY_LOSS_FLOOR_USD="0",
        DAILY_STOP_ENTRIES_ONLY="false",
        UNREALIZED_STOP_ENABLED="true",
        ODDS_SHARED_SNAPSHOT_DIR=str(snapshot),
        ODDS_SHARED_SNAPSHOT_MAX_AGE_SEC="600",
        ODDS_API_SPORT_KEYS="baseball_mlb",
        ODDS_API_REGIONS="us",
        MOTOR_2_SPORTSBOOK_ENABLED="true",
        MOTOR2_SERIES=SERIES,
        MOTOR_2_MIN_BOOKS="3",
        MOTOR_2_MAX_BOOK_AGE_MIN="15",
        MOTOR_2_BURST_INTERVAL_SEC="0",
        MOTOR_2_ONE_BET_PER_EVENT="true",
        MOTOR_MM_ENABLED="true",
        MOTOR_MM_DEMO_READONLY_ENABLED="true",
        MOTOR_MM_SERIES=SERIES,
        MOTOR_MM_MAX_TICKERS="2",
        MOTOR_MM_EXPERIMENT_ID="demo-readonly-hybrid",
        MOTOR_MM_FAIR_TTL_SEC="360",
        MOTOR_MM_FEES_AS_MAKER="true",
        MOTOR_MM_REQUIRE_PREGAME="true",
        MOTOR_MM_KICKOFF_BUFFER_SEC="120",
        MOTOR_MM_QUOTE_SIZE_CONTRACTS="1",
        MOTOR_MM_MAX_EXPOSURE_USD="3",
    )
    reset_settings_for_testing()
    return get_settings()  # Validación Settings REAL; ningún model_construct/copy.


@contextlib.contextmanager
def instrumentation(evidence: Evidence):
    from scripts.jev_client import JevClient
    from src.strategies.fair_value_book import FairValueBook
    from src.strategies.motor_2_consensus.exit_engine import Motor2ExitEngine
    from src.strategies.motor_2_consensus.poller import Motor2ShadowPoller
    from src.strategies.motor_5_mm.engine import Motor5Engine

    originals = [Motor2ShadowPoller.poll_once, Motor2ExitEngine._tick, Motor5Engine._tick]
    publish = FairValueBook.publish

    def wrapper(original, counter):
        async def observed(engine):
            if engine._executor is not None:
                evidence.deny("EXECUTOR_CONSTRUCTED")
            result = await original(engine)
            setattr(evidence, counter, getattr(evidence, counter) + 1)
            return result

        return observed

    def published(cls, fairs, **kwargs):
        publish(fairs, **kwargs)
        evidence.fair_publications += 1
        evidence.fair_items += len(fairs)

    def deny_producer(*args, **kwargs):
        evidence.deny("EXTERNAL_PRODUCER_CONSTRUCTED")

    FairValueBook.clear()
    with (
        patch.object(base, "REQUIRED_SERVICES", SERVICES),
        base.runner_instrumentation(evidence, SERIES),
        patch.object(Motor2ShadowPoller, "poll_once", wrapper(originals[0], "m2_cycles")),
        patch.object(Motor2ExitEngine, "_tick", wrapper(originals[1], "m2_exit_cycles")),
        patch.object(Motor5Engine, "_tick", wrapper(originals[2], "m5_cycles")),
        patch.object(FairValueBook, "publish", classmethod(published)),
        patch.object(JevClient, "__init__", deny_producer),
    ):
        try:
            yield
        finally:
            FairValueBook.clear()


async def run_runner(seconds: float, evidence: Evidence):
    from sqlmodel import func, select

    from src.storage.models import (
        MMQuote,
        MMShadowFill,
        PortfolioPosition,
        RiskCommitment,
        Trade,
        get_session,
    )

    await base.run_runner(seconds, evidence)
    if evidence.db_initialized:
        with get_session() as session:
            evidence.financial_rows = sum(
                session.exec(select(func.count()).select_from(model)).one()
                for model in (Trade, PortfolioPosition, RiskCommitment)
            )
            evidence.shadow_quotes = session.exec(select(func.count()).select_from(MMQuote)).one()
            evidence.shadow_fills = session.exec(
                select(func.count()).select_from(MMShadowFill)
            ).one()
        evidence.ledger_checked = True
        if evidence.financial_rows:
            evidence.blocked["UNEXPECTED_FINANCIAL_LEDGER"] += 1
    return evidence.report()


def worker(seconds: int, snapshot_dir: Path):
    # Aislamiento previo a imports del bot y a cualquier lectura de credenciales.
    credentials = Path.home() / ".config/botkalshi/demo"
    os.environ.clear()
    logging.disable(logging.CRITICAL)
    from loguru import logger

    from src.utils.config import Settings

    Settings.model_config["env_file"] = None
    logger.remove()
    evidence = Evidence()
    logger.add(lambda _message: setattr(evidence, "error_seen", True), level="ERROR")
    stdout_fd, stderr_fd = os.dup(1), os.dup(2)
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        with tempfile.TemporaryDirectory(prefix="kalshi-demo-multi-") as directory:
            workdir = Path(directory).resolve()
            os.chdir(workdir)
            try:
                snapshot = asyncio.run(prepare_snapshot(snapshot_dir, workdir, evidence))
            except Exception as exc:
                evidence.blocked["SNAPSHOT_PREFLIGHT"] += 1
                evidence.error_type = (
                    type(exc).__name__ if type(exc).__name__ in base.ERROR_TYPES else "OtherError"
                )
                return evidence.report()
            try:
                with socket.socket() as listener:
                    listener.bind(("127.0.0.1", 0))
                    port = listener.getsockname()[1]
                configure(workdir, credentials, port, snapshot)
                with base.network_guards(evidence), instrumentation(evidence):
                    return asyncio.run(run_runner(seconds, evidence))
            except Exception as exc:
                evidence.blocked["WORKER_ERROR"] += 1
                evidence.error_type = (
                    type(exc).__name__ if type(exc).__name__ in base.ERROR_TYPES else "OtherError"
                )
                return evidence.report()
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(stdout_fd, 1)
        os.dup2(stderr_fd, 2)
        os.close(stdout_fd)
        os.close(stderr_fd)


def checked_report(raw: bytes):
    """Sólo enums/counters/hash/fecha conocidos; ningún texto o payload del worker."""
    if len(raw) > 16384:
        raise ValueError("INVALID_WORKER_REPORT")
    value = json.loads(raw)
    template = Evidence().report()
    if type(value) is not dict or set(value) != set(template):
        raise ValueError("INVALID_WORKER_REPORT")
    for key in (
        "schema",
        "scope",
        "environment",
        "mode",
        "real_money_orders",
        "source_authenticity_verified",
        "execution_validated",
        "fee_metric",
    ):
        if type(value[key]) is not type(template[key]) or value[key] != template[key]:
            raise ValueError("INVALID_WORKER_REPORT")
    if value["error_type"] is not None and value["error_type"] not in base.ERROR_TYPES:
        raise ValueError("INVALID_WORKER_REPORT")
    checks = value["checks"]
    if (
        type(checks) is not dict
        or set(checks) != set(template["checks"])
        or any(type(v) is not bool for v in checks.values())
    ):
        raise ValueError("INVALID_WORKER_REPORT")
    if value["status"] != ("HYBRID_READ_ONLY" if all(checks.values()) else "INCOMPLETE") or value[
        "failed_gates"
    ] != [k for k, v in checks.items() if not v]:
        raise ValueError("INVALID_WORKER_REPORT")
    if type(value["services_started"]) is not list or any(
        type(s) is not str or s not in SERVICES for s in value["services_started"]
    ):
        raise ValueError("INVALID_WORKER_REPORT")
    counts = value["counts"]
    if type(counts) is not dict or set(counts) != set(template["counts"]):
        raise ValueError("INVALID_WORKER_REPORT")
    groups = [
        (counts["http_successes"], {"balance", "event", "events", "book", "market", "series"}),
        (value["blocked"], base.BLOCK_CODES | EXTRA_BLOCKS),
        (value["ws_error_types"], base.WS_ERROR_TYPES),
        (value["ws_failure_phases"], base.WS_FAILURE_PHASES),
    ]
    for field in ("http_statuses", "ws_handshake_statuses"):
        groups.append((value[field], {str(n) for n in range(100, 600)}))
    for group, allowed in groups:
        if (
            type(group) is not dict
            or not set(group) <= allowed
            or any(type(n) is not int or not 0 <= n <= 100000 for n in group.values())
        ):
            raise ValueError("INVALID_WORKER_REPORT")
    if any(
        type(n) is not int or not 0 <= n <= 100000
        for k, n in counts.items()
        if k != "http_successes"
    ):
        raise ValueError("INVALID_WORKER_REPORT")
    snapshot = value["snapshot"]
    if type(snapshot) is not dict or set(snapshot) != set(template["snapshot"]):
        raise ValueError("INVALID_WORKER_REPORT")
    for key in ("kind", "max_age_seconds", "fair_ttl_seconds"):
        if snapshot[key] != template["snapshot"][key]:
            raise ValueError("INVALID_WORKER_REPORT")
    if (
        type(snapshot["checked"]) is not bool
        or type(snapshot["events"]) is not int
        or not 0 <= snapshot["events"] <= 256
    ):
        raise ValueError("INVALID_WORKER_REPORT")
    for key in ("sha256", "fetched_at", "age_seconds_at_preflight"):
        item = snapshot[key]
        if item is None and not snapshot["checked"]:
            continue
        if key == "sha256":
            valid = isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item)
        elif key == "fetched_at":
            valid = isinstance(item, str) and re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?\+00:00", item
            )
        else:
            valid = type(item) is int and -315576000000 <= item <= 315576000000
        if not valid:
            raise ValueError("INVALID_WORKER_REPORT")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--snapshot-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if not 60 <= args.seconds <= 90:
        parser.error("seconds debe estar entre 60 y 90")
    if not args.run and not args.worker:
        print(
            json.dumps(
                {
                    "status": "PLAN_ONLY",
                    "environment": "demo",
                    "mode": "HYBRID_READ_ONLY",
                    "http_methods": ["GET"],
                    "max_http": base.MAX_HTTP,
                    "max_markets": base.MAX_TICKERS,
                    "seconds": args.seconds,
                    "credentials_read": False,
                    "snapshot_required": True,
                    "execution_validated": False,
                }
            )
        )
        return 0
    evidence = Evidence()
    try:
        if args.snapshot_dir is None or not args.snapshot_dir.is_absolute():
            raise ValueError("ABSOLUTE_SNAPSHOT_REQUIRED")
        if args.worker:
            result = worker(args.seconds, args.snapshot_dir)
        else:
            if args.report is None or args.report.exists():
                raise ValueError("FRESH_REPORT_PATH_REQUIRED")
            command = [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "--worker",
                "--seconds",
                str(args.seconds),
                "--snapshot-dir",
                str(args.snapshot_dir),
            ]
            completed = subprocess.run(
                command,
                env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "TZ": "UTC"},
                cwd=tempfile.gettempdir(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=args.seconds + 45,
                check=False,
            )
            result = checked_report(completed.stdout)
            if completed.returncode != (0 if result["status"] == "HYBRID_READ_ONLY" else 1):
                raise ValueError("WORKER_EXIT_MISMATCH")
    except Exception as exc:
        evidence.blocked["WORKER_ERROR"] += 1
        evidence.error_type = (
            type(exc).__name__ if type(exc).__name__ in base.ERROR_TYPES else "OtherError"
        )
        result = evidence.report()
    if not args.worker and args.report is not None:
        try:
            base.save_report(args.report, result)
        except (OSError, ValueError):
            evidence.blocked["WORKER_ERROR"] += 1
            evidence.error_type = "ReportWriteError"
            result = evidence.report()
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "HYBRID_READ_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
