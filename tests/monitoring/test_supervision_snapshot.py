"""S1 — snapshot de supervisión read-only del runtime principal (ASTRA-SUPERVISION-20260923).

La DB es la del conftest: SQLite temporal con el esquema REAL de `src/storage/models.py`
(contrato real, no tablas a mano). Se pinea:
  - producción y simulación imposibles de confundir;
  - fuente faltante / vencida / parcial / servicio caído ⇒ nunca "sano";
  - ningún secreto llega al JSON (allowlist + detector);
  - observar no modifica la DB fuente (mode=ro, bytes idénticos);
  - exportación atómica, privada y acotada; un fallo de escritura queda explícito;
  - la task del runner nunca propaga (corre bajo FIRST_EXCEPTION).
Sin red, sin credenciales reales.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.monitoring.supervision_snapshot as snap
from src.storage.models import OperationalState, PortfolioPosition, Trade, get_session

NOW = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)
NAIVE_NOW = NOW.replace(tzinfo=None)
SHA = "11c215138d9c6a81322b6cc970643a9a703e27d2"
# Synthetic PEM-looking marker for the redaction tests, assembled at runtime so no real-
# looking key header is ever committed (tests/test_no_claves_trackeadas.py guards that).
FAKE_PEM = "-" * 5 + "BEGIN " + "PRIVATE" + " KEY" + "-" * 5


@pytest.fixture
def db_path(_tmp_db_engine) -> Path:
    return Path(_tmp_db_engine.url.database)


def _trade(**kw) -> Trade:
    base = {
        "client_order_id": f"coid-{kw.get('ticker', 'T')}-{kw.get('status', 'x')}-{id(kw)}",
        "ticker": "KXMLBGAME-26SEP23-NYY",
        "side": "yes",
        "action": "buy",
        "count": 2,
        "price_cents": 45,
        "strategy": "motor_1_arbitrage",
        "status": "filled",
        "placed_at": NOW - timedelta(minutes=10),
    }
    base.update(kw)
    return Trade(**base)


def _seed(*rows) -> None:
    with get_session() as s:
        for row in rows:
            s.add(row)
        s.commit()


def _runtime(**kw):
    base = {
        "started_at": NOW - timedelta(hours=2),
        "pid": 4242,
        "db_initialized": True,
        "capture_running": True,
        "last_ws_message": NOW - timedelta(seconds=5),
        "is_paused": False,
        "pause_reason": None,
        "motor1_local_pause": None,
        "last_error": None,
        "last_error_at": None,
        "books": {"tracked": 10, "initialized": 9, "gaps_last_60s": 0, "sids_disabled": 0},
        "motor5": {},
    }
    base.update(kw)
    return base


CONFIG = {
    "kalshi_env": "production",
    "trading_enabled": False,
    "balance_refresh_seconds": 300,
    "motors": {"motor_1_arbitrage": {"enabled": True, "execution": False}},
}
FRESH_CAPITAL = {
    "raw_balance_usd": 99.37,
    "balance_at": NOW - timedelta(minutes=2),
    "is_paused": True,
}


def build(db_path, *, runtime=None, config=CONFIG, capital=FRESH_CAPITAL, release=SHA, ledger=None):
    return snap.build_snapshot(
        now=NOW,
        runtime=runtime or _runtime(),
        config=config,
        capital=capital,
        ledger=ledger if ledger is not None else snap.read_ledger(db_path, now=NOW),
        release=release,
        snapshot_id="fixed",
    )


class TestContractAgainstRealSchema:
    def test_fresh_coherent_runtime_is_ok_and_valid(self, db_path):
        _seed(
            _trade(
                status="settled",
                pnl_cents=37,
                fill_price_cents=45,
                fees_cents=1,
                settled_at=NAIVE_NOW - timedelta(hours=1),
            ),
            _trade(
                ticker="KXMLBGAME-26SEP23-BOS",
                status="filled",
                fill_price_cents=44,
                fees_cents=1,
                filled_count=2,
            ),
        )
        doc = build(db_path)
        assert snap.validate_snapshot(doc) == []
        assert doc["health"] == {"status": "OK", "reasons": [], "unknown": []}
        assert doc["schema_version"] == "botkalshi-production-snapshot-v1"
        assert doc["ledger_domain"] == "PRODUCTION"
        # Exact units: integer cents, counts; provenance and date on every figure.
        assert doc["balance"] == {
            "value": 9937,
            "unit": "cents",
            "as_of": (NOW - timedelta(minutes=2)).isoformat(),
            "source": "kalshi.get_balance via RiskManager",
            "status": "OK",
            "note": None,
        }
        acc = doc["accounting"]
        assert acc["realized_pnl_today"]["value"] == 37
        assert acc["open_positions_filled"]["value"] == 1
        assert acc["open_cost_basis"]["value"] == 88  # 2 × 44
        assert acc["realized_pnl_today"]["source"] == "trades.db (sqlite mode=ro)"
        assert doc["controls"]["kill_switch"]["state"] == "CLEAR"

    def test_mode_is_configuration_never_live_active(self, db_path):
        config = dict(
            CONFIG,
            trading_enabled=True,
            motors={"motor_5_mm": {"enabled": True, "execution": True}},
        )
        doc = build(db_path, config=config)
        assert doc["mode"]["classification"] == "EXECUTION_CONFIGURED"
        assert "LIVE_ACTIVE" not in json.dumps(doc)
        assert doc["mode"]["note"] == "configuration only; not evidence of live activity"


class TestNeverReportsHealthyWithoutEvidence:
    def test_balance_never_read_is_unknown_not_zero(self, db_path):
        doc = build(db_path, capital={"raw_balance_usd": None, "balance_at": None})
        assert doc["balance"]["status"] == "UNKNOWN"
        assert doc["balance"]["value"] is None
        assert doc["health"]["status"] == "UNKNOWN"
        assert "BALANCE_UNKNOWN" in doc["health"]["unknown"]

    def test_missing_db_leaves_every_figure_unknown_not_zero(self, tmp_path):
        doc = build(None, ledger=snap.read_ledger(tmp_path / "nope.db", now=NOW))
        assert snap.validate_snapshot(doc) == []
        for key, value in doc["accounting"].items():
            if key not in ("last_trade_at", "trades_by_status"):
                assert (value["status"], value["value"]) == ("UNKNOWN", None), key
        assert doc["controls"]["kill_switch"]["state"] == "UNKNOWN"
        assert doc["health"]["status"] == "UNKNOWN"

    def test_unreadable_db_is_an_error_not_a_clean_ledger(self, tmp_path):
        bogus = tmp_path / "corrupt.db"
        bogus.write_bytes(b"not a sqlite file at all" * 100)
        ledger = snap.read_ledger(bogus, now=NOW)
        assert ledger["status"] == "ERROR"
        doc = build(None, ledger=ledger)
        assert doc["health"]["status"] == "UNKNOWN"
        assert doc["coherence"]["status"] == "UNKNOWN"

    def test_stale_balance_degrades(self, db_path):
        doc = build(
            db_path, capital={"raw_balance_usd": 99.0, "balance_at": NOW - timedelta(minutes=16)}
        )
        assert doc["balance"]["status"] == "STALE"
        assert doc["health"]["status"] == "DEGRADED"
        assert "BALANCE_STALE" in doc["health"]["reasons"]

    def test_silent_feed_and_blind_books_are_not_healthy(self, db_path):
        silent = build(db_path, runtime=_runtime(last_ws_message=NOW - timedelta(seconds=61)))
        assert silent["feed"]["status"] == "STALE"
        assert silent["health"]["status"] == "DEGRADED"
        blind = build(db_path, runtime=_runtime(books={"tracked": 10, "initialized": 4}))
        assert blind["feed"]["status"] == "STALE"
        never = build(db_path, runtime=_runtime(last_ws_message=None))
        assert never["feed"]["status"] == "UNKNOWN"
        assert never["health"]["status"] == "UNKNOWN"

    def test_service_down_capture_not_running_is_not_healthy(self, db_path):
        doc = build(db_path, runtime=_runtime(capture_running=False))
        assert doc["feed"]["status"] == "STALE"
        assert doc["health"]["status"] != "OK"

    def test_unknown_release_is_not_ok(self, db_path):
        assert build(db_path, release="UNKNOWN")["health"]["status"] == "UNKNOWN"

    def test_kill_switch_engaged_is_visible_and_degrades(self, db_path):
        _seed(OperationalState(key="kill_switch", value="engaged", reason="weekly stop 12%"))
        doc = build(db_path)
        assert doc["controls"]["kill_switch"]["state"] == "ENGAGED"
        assert doc["controls"]["kill_switch"]["reason"] == "weekly stop 12%"
        assert "KILL_SWITCH_ENGAGED" in doc["health"]["reasons"]

    def test_ledger_incoherence_is_diagnosed_not_corrected(self, db_path):
        _seed(
            _trade(
                status="settled",
                pnl_cents=None,
                fill_price_cents=45,
                fees_cents=None,
                settled_at=NAIVE_NOW,
            ),
            _trade(ticker="X-OLD", status="pending", placed_at=NOW - timedelta(hours=3)),
        )
        doc = build(db_path)
        assert doc["coherence"]["status"] == "INCONSISTENT"
        checks = doc["coherence"]["checks"]
        assert (
            checks["settled_without_pnl"],
            checks["fee_unknown_on_filled"],
            checks["stale_pending"],
        ) == (1, 1, 1)
        assert doc["health"]["status"] == "DEGRADED"

    def test_empty_positions_table_is_unknown_not_zero(self, db_path):
        figure = build(db_path)["accounting"]["portfolio_positions"]
        assert (figure["status"], figure["value"], figure["note"]) == (
            "UNKNOWN",
            None,
            "NO_SYNC_ROWS",
        )

    def test_positions_synced_long_ago_are_stale(self, db_path):
        _seed(
            PortfolioPosition(
                ticker="KXA-1",
                side="yes",
                count=1,
                synced_at=NAIVE_NOW - timedelta(hours=2),
            )
        )
        figure = build(db_path)["accounting"]["portfolio_positions"]
        assert (figure["status"], figure["value"]) == ("STALE", 1)

    def test_positions_recently_synced_are_current(self, db_path):
        _seed(
            PortfolioPosition(
                ticker="KXA-1",
                side="yes",
                count=1,
                synced_at=NAIVE_NOW - timedelta(minutes=5),
            )
        )
        figure = build(db_path)["accounting"]["portfolio_positions"]
        assert (figure["status"], figure["value"]) == ("OK", 1)


class TestNoSecretsReachTheSnapshot:
    def test_secret_like_reasons_are_redacted_and_errors_reduced_to_class(self, db_path):
        doc = build(
            db_path,
            runtime=_runtime(
                pause_reason="manual: Bearer eyJhbGciOiJ",
                last_error="provider read failed https://x/y?apiKey=abc123secret",
                last_error_at=NOW,
            ),
        )
        assert doc["controls"]["pause_reason"] == "[REDACTED]"
        assert doc["process"]["last_error_class"] in ("provider read failed https", "UNCLASSIFIED")
        text = json.dumps(doc)
        assert "abc123secret" not in text and "eyJhbGciOiJ" not in text
        assert snap.validate_snapshot(doc) == []

    def test_contract_rejects_extra_fields_and_secret_patterns(self, db_path):
        doc = build(db_path)
        extra = dict(doc, environment={"KALSHI_API_KEY_ID": "x"})
        assert any(p.startswith("TOP_KEYS") for p in snap.validate_snapshot(extra))
        leaked = json.loads(json.dumps(doc))
        leaked["mode"]["note"] = FAKE_PEM
        assert "SECRET_PATTERN" in snap.validate_snapshot(leaked)
        lying = json.loads(json.dumps(doc))
        lying["accounting"]["realized_pnl_today"].update(status="UNKNOWN", value=0)
        assert "UNKNOWN_WITH_VALUE:realized_pnl_today" in snap.validate_snapshot(lying)

    def test_release_sha_reads_only_the_allowlisted_names(self):
        env = {
            "KALSHI_PRIVATE_KEY": FAKE_PEM,
            "SOURCE_COMMIT": SHA.upper(),
            "GIT_SHA": "f" * 40,
        }
        assert snap.release_sha(env) == SHA
        assert snap.release_sha({"GIT_SHA": "f" * 40}) == "UNKNOWN"
        assert snap.release_sha({"SOURCE_COMMIT": "not-a-sha"}) == "UNKNOWN"


class TestObservingNeverMutates:
    def test_reading_leaves_the_source_db_byte_identical(self, db_path):
        _seed(
            _trade(
                status="settled",
                pnl_cents=5,
                fill_price_cents=45,
                fees_cents=1,
                settled_at=NAIVE_NOW,
            )
        )
        with sqlite3.connect(db_path) as con:  # checkpoint so the main file holds the data
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = hashlib.sha256(db_path.read_bytes()).hexdigest()
        for _ in range(3):
            build(db_path)
        assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before

    def test_read_ledger_opens_the_source_read_only(self, db_path):
        """The reader's OWN connection must be mode=ro: a write through it is impossible."""
        opened = []
        real_connect = sqlite3.connect

        def spy(*args, **kwargs):
            con = real_connect(*args, **kwargs)
            opened.append(con)
            return con

        with patch.object(snap.sqlite3, "connect", side_effect=spy) as connect:
            snap.read_ledger(db_path, now=NOW)
        [(args, kwargs)] = [(c.args, c.kwargs) for c in connect.call_args_list]
        assert args[0].startswith("file:") and args[0].endswith("?mode=ro")
        assert kwargs.get("uri") is True

    def test_the_reader_connection_cannot_write(self, db_path):
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM trades")
        con.close()


class TestExport:
    def test_atomic_private_bounded(self, db_path, tmp_path):
        out = tmp_path / "supervision"
        for i in range(5):
            doc = build(db_path)
            doc["snapshot_id"] = f"s{i}"
            snap.export_snapshot(doc, out, history_max=3)
        assert stat.S_IMODE(out.stat().st_mode) == 0o700
        for name in ("latest.json", "history.jsonl"):
            assert stat.S_IMODE((out / name).stat().st_mode) == 0o600
        assert json.loads((out / "latest.json").read_text())["snapshot_id"] == "s4"
        lines = (out / "history.jsonl").read_text().splitlines()
        assert [json.loads(x)["snapshot_id"] for x in lines] == ["s2", "s3", "s4"]
        assert not list(out.glob(".*.tmp"))

    def test_invalid_document_is_never_written(self, db_path, tmp_path):
        out = tmp_path / "supervision"
        good = build(db_path)
        snap.export_snapshot(good, out, history_max=10)
        bad = dict(good, raw_log="line")
        with pytest.raises(ValueError, match="rejected by contract"):
            snap.export_snapshot(bad, out, history_max=10)
        assert json.loads((out / "latest.json").read_text()) == good

    def test_write_failure_is_explicit_and_keeps_the_previous_snapshot(self, db_path, tmp_path):
        out = tmp_path / "supervision"
        good = build(db_path)
        snap.export_snapshot(good, out, history_max=10)
        newer = dict(good, snapshot_id="newer")
        with (
            patch.object(snap.os, "replace", side_effect=OSError("disk full")),
            pytest.raises(OSError, match="disk full"),
        ):
            snap.export_snapshot(newer, out, history_max=10)
        assert json.loads((out / "latest.json").read_text())["snapshot_id"] == "fixed"
        assert not list(out.glob(".*.tmp"))


class TestProductionAndSimulationCannotBeConfused:
    def test_research_artifacts_fail_the_production_contract(self):
        sim_report = {
            "schema_version": "botkalshi-m5-research-cycle-v1",
            "mode": "SIMULATION_ONLY",
            "cohort": "m5-research-rest-v1",
        }
        sim_bank = {
            "schema_version": "botkalshi-simulation-bank-v1",
            "mode": "SIMULATION_ONLY",
            "capital_source": "FICTIONAL_TEST_CAPITAL",
        }
        for doc in (sim_report, sim_bank):
            problems = snap.validate_snapshot(doc)
            assert "SCHEMA_VERSION" in problems and "LEDGER_DOMAIN" in problems

    def test_a_relabelled_simulation_is_rejected(self, db_path):
        doc = build(db_path)
        doc["ledger_domain"] = "SIMULATION"
        assert "LEDGER_DOMAIN" in snap.validate_snapshot(doc)


class TestRuntimeCollection:
    def _settings(self, db_path, tmp_path, **kw):
        settings = MagicMock()
        settings.DATABASE_URL = f"sqlite:///{db_path}"
        settings.KALSHI_ENV = "production"
        settings.TRADING_ENABLED = False
        settings.BALANCE_REFRESH_SECONDS = 300
        settings.SUPERVISION_SNAPSHOT_INTERVAL_SEC = 60
        for name in (
            "MOTOR_1_ARBITRAGE_ENABLED",
            "MOTOR_1_EXECUTION_ENABLED",
            "MOTOR_2_SPORTSBOOK_ENABLED",
            "MOTOR_2_EXECUTION_ENABLED",
            "MOTOR_2_ENTRY_EXECUTION_ENABLED",
            "MOTOR_3_CLV_ENABLED",
            "MOTOR_3_EXECUTION_ENABLED",
            "MOTOR_MM_ENABLED",
            "MOTOR_MM_EXECUTION_ENABLED",
            "MOTOR_REST_ENABLED",
            "MOTOR_REST_EXECUTION_ENABLED",
        ):
            setattr(settings, name, False)
        for k, v in kw.items():
            setattr(settings, k, v)
        return settings

    def test_collect_reads_runtime_without_secrets(self, db_path, tmp_path, monkeypatch):
        from src.monitoring.health import BotState
        from src.risk.manager import RiskManager

        monkeypatch.setenv("KALSHI_PRIVATE_KEY", FAKE_PEM)
        monkeypatch.setenv("SOURCE_COMMIT", SHA)
        monkeypatch.setattr(BotState, "last_error", "GET /x?token=zzz failed")
        monkeypatch.setattr(RiskManager, "_last_raw_balance_usd", 12.34)
        monkeypatch.setattr(RiskManager, "_last_balance_at", NAIVE_NOW)
        settings = self._settings(db_path, tmp_path)
        with (
            patch("src.utils.config.get_settings", return_value=settings),
            patch("src.risk.manager.get_settings", return_value=settings),
        ):
            doc = snap.collect_and_build(now=NOW)
        assert snap.validate_snapshot(doc) == []
        text = json.dumps(doc)
        assert "BEGIN" not in text and "zzz" not in text
        assert doc["release_sha"] == SHA
        assert doc["balance"]["value"] == 1234


class TestRunnerTask:
    def _runner(self, **settings):
        from src.runner import ProductionRunner

        runner = ProductionRunner.__new__(ProductionRunner)
        runner.settings = MagicMock(**settings)
        runner._stop_event = asyncio.Event()
        return runner

    def test_disabled_by_default_does_nothing(self):
        from src.utils.config import Settings

        assert Settings.model_fields["SUPERVISION_SNAPSHOT_ENABLED"].default is False
        runner = self._runner(SUPERVISION_SNAPSHOT_ENABLED=False)
        with patch("src.monitoring.supervision_snapshot.collect_and_build") as collect:
            asyncio.run(runner._run_supervision_snapshot())
        collect.assert_not_called()

    def test_a_failing_snapshot_never_escapes_the_task(self, tmp_path):
        runner = self._runner(
            SUPERVISION_SNAPSHOT_ENABLED=True,
            SUPERVISION_SNAPSHOT_DIR=str(tmp_path),
            SUPERVISION_SNAPSHOT_INTERVAL_SEC=60,
            SUPERVISION_SNAPSHOT_HISTORY_MAX=10,
        )
        calls = []

        def boom():
            calls.append(1)
            runner._stop_event.set()
            raise RuntimeError("collector exploded")

        from src.monitoring.health import BotState

        with (
            patch("src.monitoring.supervision_snapshot.collect_and_build", side_effect=boom),
            patch("src.runner.asyncio.sleep", return_value=None),
        ):
            asyncio.run(runner._run_supervision_snapshot())  # must return, not raise
        assert calls == [1]
        assert BotState.last_error.startswith("supervision_snapshot: RuntimeError")


def test_module_has_no_control_or_network_surface():
    source = Path(snap.__file__).read_text()
    for forbidden in (
        "set_pause",
        "/admin",
        "place_order",
        "cancel_order",
        "clear_kill_switch",
        "engage_kill_switch",
        "httpx",
        "aiohttp",
        "urllib",
        "socket",
        "FastAPI",
    ):
        assert forbidden not in source, forbidden
    assert os.environ.get("SUPERVISION_SNAPSHOT_ENABLED") in (None, "", "false")
