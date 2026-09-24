"""ASTRA-DEPLOY-SUPERVISION-20260924 — deployable S1/S2/S3 with every boundary pinned.

Pinned:
  - units: non-root user, NoNewPrivileges, ProtectSystem=strict, PrivateTmp,
    PrivateNetwork, AF_UNIX only, writes only under /var/lib/botkalshi-supervision,
    bounded resources and logs; they run ONLY the collector/audit/reader scripts;
  - nothing (units, installer, collector) starts/stops/pauses the bot, sets flags,
    touches risk, set_pause, /admin/*, clear_kill_switch, activation scripts, executors;
  - the installer verifies the exact release, runs the focal tests first, backs up,
    records SHA / previous, never overwrites the env, never enables a timer, and
    supports dry-run, rollback and a read-only verify; 0700 dirs / 0600 files;
  - the collector reads the live sources read-only and fails closed;
  - same-host end to end: live dir byte-identical after collect → audit → reader;
  - the production audit never uses the fictional simulation bank.
Everything runs under a temporary root with a fake command runner: no root, no
systemd, no network, no real data. Stdlib only (runs under the system python3).
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parents[1]
sys.path.insert(0, str(BASE))

import production_audit  # noqa: E402
import supervision_collect as collect  # noqa: E402
import supervision_deploy as deploy  # noqa: E402
import supervision_reader  # noqa: E402

from src.monitoring import supervision_snapshot as producer  # noqa: E402

DDL = (Path(__file__).parent / "fixtures" / "production_schema.sql").read_text()
SHA1 = "1" * 40
SHA2 = "2" * 40
FORBIDDEN = (
    "set_pause",
    "/admin",
    "clear_kill_switch",
    "engage_kill_switch",
    "ACTIVAR",
    "enable-live-odds",
    "botkalshi-live",
    "src.runner",
    "place_order",
    "cancel_order",
    "TRADING_ENABLED=true",
    "EXECUTION_ENABLED=true",
    "StrictHostKeyChecking=no",
)


def unit_directives(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "[")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out.setdefault(key, []).append(value)
    return out


class UnitBoundaryTests(unittest.TestCase):
    SERVICES = ("botkalshi-supervision-audit.service", "botkalshi-supervision-reader.service")

    def test_services_are_confined_and_bounded(self):
        for name in self.SERVICES:
            d = unit_directives((BASE / name).read_text())
            with self.subTest(unit=name):
                self.assertEqual(d["User"], ["botkalshi-supervisor"])
                self.assertEqual(d["Group"], ["botkalshi-supervisor"])
                for key, value in (
                    ("NoNewPrivileges", "true"),
                    ("PrivateTmp", "true"),
                    ("PrivateNetwork", "true"),
                    ("ProtectSystem", "strict"),
                    ("ProtectHome", "true"),
                    ("RestrictAddressFamilies", "AF_UNIX"),
                    ("UMask", "0077"),
                    ("CapabilityBoundingSet", ""),
                ):
                    self.assertEqual(d[key], [value], key)
                self.assertEqual(d["ReadWritePaths"], ["/var/lib/botkalshi-supervision"])
                for key in (
                    "MemoryMax",
                    "CPUQuota",
                    "TasksMax",
                    "TimeoutStartSec",
                    "LogRateLimitIntervalSec",
                    "LogRateLimitBurst",
                ):
                    self.assertIn(key, d)
                self.assertIn("KALSHI_PRIVATE_KEY", d["UnsetEnvironment"][0])

    def test_services_run_only_the_supervision_scripts(self):
        allowed = {
            "ExecStart": ("production_audit.py", "supervision_reader.py"),
            "ExecStartPre": ("supervision_collect.py",),
        }
        for name in self.SERVICES:
            d = unit_directives((BASE / name).read_text())
            for key, scripts in allowed.items():
                for command in d.get(key, []):
                    argv = command.lstrip("+-").split()
                    self.assertEqual(argv[0], "/usr/bin/python3", command)
                    script = next(a for a in argv if a.endswith(".py"))
                    self.assertTrue(script.endswith(scripts), command)
            self.assertNotIn("ExecStartPost", d)
            self.assertNotIn("ExecStop", d)

    def test_no_control_surface_anywhere(self):
        files = [
            *(BASE / u for u in deploy.UNITS),
            BASE / "install-supervision.sh",
            BASE / "supervision_deploy.py",
            BASE / "supervision_collect.py",
        ]
        for path in files:
            text = path.read_text()
            code = "\n".join(
                line for line in text.splitlines() if not line.lstrip().startswith("#")
            )
            if path.suffix == ".py":  # docstrings may NAME what is forbidden; code may not
                tree = ast.parse(text)
                code = "\n".join(
                    ast.get_source_segment(text, node) or ""
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.Call, ast.Assign, ast.Constant))
                    and not (
                        isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and "\n" in node.value
                    )
                )
            for forbidden in FORBIDDEN:
                self.assertNotIn(forbidden, code, f"{path.name}: {forbidden}")

    def test_systemctl_verbs_are_only_reload_and_query(self):
        self.assertEqual(deploy.ALLOWED_SYSTEMCTL, frozenset({"daemon-reload", "is-enabled"}))
        source = (BASE / "supervision_deploy.py").read_text()
        verbs = set(re.findall(r'"systemctl",\s*"([a-z-]+)"', source))
        self.assertLessEqual(verbs, {"daemon-reload", "is-enabled"}, verbs)

    def test_installer_wrapper_is_valid_bash(self):
        done = subprocess.run(
            ["bash", "-n", str(BASE / "install-supervision.sh")], capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 0, done.stderr)


class FakeRunner:
    def __init__(self, *, heads=None, dirty="", tests_rc=0, user_exists=False):
        self.calls: list[list[str]] = []
        self.heads = heads or {}
        self.dirty = dirty
        self.tests_rc = tests_rc
        self.user_exists = user_exists

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] == "git" and argv[3] == "rev-parse":
            sha = Path(argv[2]).name
            return subprocess.CompletedProcess(argv, 0, self.heads.get(sha, sha) + "\n", "")
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, self.dirty, "")
        if argv[:3] == ["/usr/bin/python3", "-m", "unittest"]:
            return subprocess.CompletedProcess(argv, self.tests_rc, "", "")
        if argv[:2] == ["id", "-u"]:
            return subprocess.CompletedProcess(argv, 0 if self.user_exists else 1, "", "")
        if argv[:2] == ["systemctl", "is-enabled"]:
            return subprocess.CompletedProcess(argv, 1, "disabled\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")


class DeployTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for sha in (SHA1, SHA2):
            release = self.root / "opt/botkalshi-research/releases" / sha
            (release / ".git").mkdir(parents=True)
            target = release / "infra/digitalocean-shadow"
            target.mkdir(parents=True)
            for unit in deploy.UNITS:
                shutil.copy(BASE / unit, target / unit)

    def deployer(self, runner=None, dry_run=False):
        runner = runner or FakeRunner()
        return deploy.Deployer(
            runner=runner, root=self.root, dry_run=dry_run, out=io.StringIO()
        ), runner

    def unit_text(self, name="botkalshi-supervision-audit.service"):
        return (self.root / "etc/systemd/system" / name).read_text()


class InstallTests(DeployTestCase):
    def test_install_renders_records_and_never_enables_timers(self):
        d, runner = self.deployer()
        result = d.install(SHA1)
        self.assertEqual(
            result, {"installed": SHA1, "previous": None, "dry_run": False, "timers_enabled": False}
        )
        self.assertIn(
            f"/opt/botkalshi-research/releases/{SHA1}/infra/digitalocean-shadow/"
            "production_audit.py",
            self.unit_text(),
        )
        self.assertNotIn("/opt/botkalshi/", self.unit_text())
        state = self.root / "var/lib/botkalshi-supervision"
        for directory in (state, *(state / s for s in deploy.STATE_SUBDIRS)):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        self.assertEqual(json.loads((state / "RELEASE").read_text())["sha"], SHA1)
        self.assertEqual(stat.S_IMODE((state / "RELEASE").stat().st_mode), 0o600)
        env = self.root / "etc/botkalshi-supervision.env"
        self.assertEqual(stat.S_IMODE(env.stat().st_mode), 0o600)
        self.assertTrue(all(line.startswith("#") for line in env.read_text().splitlines() if line))
        systemctl = [c for c in runner.calls if c[0] == "systemctl"]
        self.assertEqual(systemctl, [["systemctl", "daemon-reload"]])
        self.assertIn(
            [
                "useradd",
                "--system",
                "--home",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                "botkalshi-supervisor",
            ],
            runner.calls,
        )
        # The focal tests ran BEFORE anything was written, with the system python.
        test_call = next(c for c in runner.calls if c[:3] == ["/usr/bin/python3", "-m", "unittest"])
        self.assertEqual(test_call[3:], list(deploy.FOCAL_TESTS))

    def test_failing_focal_tests_install_nothing(self):
        d, _ = self.deployer(FakeRunner(tests_rc=1))
        with self.assertRaisesRegex(deploy.DeployError, "focal tests failed"):
            d.install(SHA1)
        self.assertFalse((self.root / "etc/systemd/system").exists())
        self.assertFalse((self.root / "var/lib/botkalshi-supervision/RELEASE").exists())

    def test_wrong_head_dirty_tree_or_bad_sha_is_refused(self):
        for runner, sha, message in (
            (FakeRunner(heads={SHA1: SHA2}), SHA1, "HEAD does not match"),
            (FakeRunner(dirty=" M x.py"), SHA1, "not clean"),
            (FakeRunner(), "abc", "40-hex"),
            (FakeRunner(), "3" * 40, "checkout missing"),
        ):
            d, _ = self.deployer(runner)
            with self.subTest(message=message), self.assertRaisesRegex(deploy.DeployError, message):
                d.install(sha)

    def test_upgrade_backs_up_preserves_data_and_records_previous(self):
        d, _ = self.deployer()
        d.install(SHA1)
        audit_db = self.root / "var/lib/botkalshi-supervision/audit/audit.sqlite3"
        audit_db.write_bytes(b"audit state bytes")
        env = self.root / "etc/botkalshi-supervision.env"
        env.write_text("BOTKALSHI_LIVE_DB=/srv/live/trades.db\n")
        d2, _ = self.deployer()
        result = d2.install(SHA2)
        self.assertEqual(result["previous"], SHA1)
        self.assertEqual(audit_db.read_bytes(), b"audit state bytes")  # data preserved
        self.assertEqual(env.read_text(), "BOTKALSHI_LIVE_DB=/srv/live/trades.db\n")  # kept
        state = self.root / "var/lib/botkalshi-supervision"
        self.assertEqual(json.loads((state / "RELEASE.previous").read_text())["sha"], SHA1)
        self.assertIn(f"releases/{SHA2}/", self.unit_text())
        [backup] = list((self.root / "var/backups/botkalshi-supervision").glob("*.tar.gz"))
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        with tarfile.open(backup) as tar:
            names = tar.getnames()
        self.assertIn("var/lib/botkalshi-supervision/audit/audit.sqlite3", names)
        self.assertIn("etc/systemd/system/botkalshi-supervision-audit.service", names)

    def test_rollback_restores_previous_units_and_keeps_state(self):
        d, _ = self.deployer()
        d.install(SHA1)
        self.deployer()[0].install(SHA2)
        marker = self.root / "var/lib/botkalshi-supervision/alerts/state.json"
        marker.write_text("{}")
        result = self.deployer()[0].rollback()
        self.assertEqual((result["installed"], result["previous"]), (SHA1, SHA2))
        self.assertIn(f"releases/{SHA1}/", self.unit_text())
        self.assertEqual(marker.read_text(), "{}")

    def test_dry_run_changes_nothing(self):
        d, runner = self.deployer(dry_run=True)
        d.install(SHA1)
        self.assertFalse((self.root / "etc").exists())
        self.assertFalse((self.root / "var").exists())
        mutating = [c for c in runner.calls if c[0] in ("useradd", "systemctl")]
        self.assertEqual(mutating, [])
        self.assertTrue(any("DRY-RUN would run: systemctl daemon-reload" in s for s in d.plan))
        # A dry run never claims to have done something.
        self.assertFalse(any(s.startswith(("installed", "env TEMPLATE written")) for s in d.plan))
        self.assertEqual(d.plan[-1], "DRY-RUN complete: nothing was written or run")

    def test_verify_is_read_only_and_catches_loose_permissions(self):
        d, runner = self.deployer()
        d.install(SHA1)
        report = self.deployer()[0].verify()
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["units_sha"], SHA1)
        self.assertEqual(set(report["timers"].values()), {"disabled"})
        loose = self.root / "var/lib/botkalshi-supervision/reader/latest.json"
        loose.write_text("{}")
        os.chmod(loose, 0o644)
        report = self.deployer()[0].verify()
        self.assertIn("FILE_MODE:latest.json", report["problems"])

    def test_verify_catches_a_loosened_state_directory(self):
        self.deployer()[0].install(SHA1)
        os.chmod(self.root / "var/lib/botkalshi-supervision/audit", 0o755)
        self.assertIn("DIR_MODE:audit", self.deployer()[0].verify()["problems"])

    def test_disallowed_systemctl_verb_is_refused(self):
        d, _ = self.deployer()
        for verb in ("start", "restart", "enable", "stop", "disable"):
            with self.subTest(verb=verb), self.assertRaises(deploy.DeployError):
                d.run(["systemctl", verb, "botkalshi-supervision-audit.timer"], mutating=True)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.live = self.dir / "live"
        self.live.mkdir(mode=0o700)
        self.db = self.live / "trades.db"
        with sqlite3.connect(self.db) as con:
            con.executescript(DDL)
            con.execute("CREATE TABLE orderbook_events (id INTEGER PRIMARY KEY, blob TEXT)")
            con.execute("INSERT INTO orderbook_events (blob) VALUES ('huge diagnostics')")
            con.execute(
                "INSERT INTO trades (id, client_order_id, ticker, side, action, count, price_cents,"
                " strategy, status, fill_price_cents, fees_cents, pnl_cents, closed_by_clv,"
                " placed_at, settled_at) VALUES (1,'a','X','yes','buy',1,45,'m1','settled',45,1,"
                "10,0,'2026-09-23 13:00:00','2026-09-23 13:30:00')"
            )
        self.snapshot = self.live / "latest.json"
        self.env = self.dir / "supervision.env"
        self.write_env(f"BOTKALSHI_LIVE_DB={self.db}\nBOTKALSHI_LIVE_SNAPSHOT={self.snapshot}\n")
        self.inbox = self.dir / "inbox"

    def write_env(self, text, mode=0o600):
        self.env.write_text(text)
        os.chmod(self.env, mode)

    def run_collect(self):
        return collect.collect(self.env, self.inbox, None, require_root_owner=False)

    def test_env_accepts_only_the_two_absolute_paths(self):
        for text, reason in (
            (f"BOTKALSHI_LIVE_DB={self.db}\nKALSHI_PRIVATE_KEY=x\n", "ENV_UNEXPECTED_KEY"),
            (
                "BOTKALSHI_LIVE_DB=relative.db\nBOTKALSHI_LIVE_SNAPSHOT=/x\n",
                "ENV_PATH_NOT_ABSOLUTE",
            ),
            (f"BOTKALSHI_LIVE_DB={self.db}\n", "ENV_INCOMPLETE"),
            ("BOTKALSHI_LIVE_DB=/a/../b\nBOTKALSHI_LIVE_SNAPSHOT=/x\n", "ENV_PATH_NOT_ABSOLUTE"),
        ):
            self.write_env(text)
            with self.subTest(reason=reason), self.assertRaisesRegex(collect.CollectError, reason):
                self.run_collect()
        self.write_env(
            f"BOTKALSHI_LIVE_DB={self.db}\nBOTKALSHI_LIVE_SNAPSHOT={self.snapshot}\n", mode=0o666
        )
        with self.assertRaisesRegex(collect.CollectError, "ENV_WRITABLE_BY_OTHERS"):
            self.run_collect()

    def test_env_must_be_root_owned(self):
        if os.geteuid() == 0:
            os.chown(self.env, 65534, 65534)
        with self.assertRaisesRegex(collect.CollectError, "ENV_NOT_ROOT_OWNED"):
            collect.read_env(self.env)

    def test_exports_only_trades_read_only_and_private(self):
        self.snapshot.write_text('{"x": 1}')
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.live.iterdir()}
        status = self.run_collect()
        self.assertEqual((status["snapshot"], status["trades"]), ("COPIED", "EXPORTED"))
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.live.iterdir()}
        self.assertEqual(before, after)  # the live side is untouched
        with sqlite3.connect(self.inbox / "trades.db") as con:
            tables = {
                r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            self.assertEqual(tables, {"trades"})  # never the diagnostics or anything else
            self.assertEqual(con.execute("SELECT pnl_cents FROM trades").fetchall(), [(10,)])
        for name in ("snapshot.json", "trades.db", "collect-status.json"):
            self.assertEqual(stat.S_IMODE((self.inbox / name).stat().st_mode), 0o600, name)
        self.assertEqual(stat.S_IMODE(self.inbox.stat().st_mode), 0o700)

    def test_the_live_db_is_opened_mode_ro(self):
        from unittest.mock import patch

        real = sqlite3.connect
        with patch.object(collect.sqlite3, "connect", side_effect=real) as connect:
            self.run_collect()
        live_calls = [c for c in connect.call_args_list if str(self.db) in str(c.args[0])]
        self.assertEqual(len(live_calls), 1)
        self.assertEqual(live_calls[0].args[0], f"file:{self.db}?mode=ro")
        self.assertIs(live_calls[0].kwargs.get("uri"), True)

    def test_missing_or_symlinked_snapshot_fails_closed(self):
        self.snapshot.write_text('{"x": 1}')
        self.run_collect()
        self.snapshot.unlink()
        self.assertEqual(self.run_collect()["snapshot"], "MISSING")
        self.assertFalse((self.inbox / "snapshot.json").exists())  # stale copy removed
        self.snapshot.symlink_to(self.db)
        with self.assertRaisesRegex(collect.CollectError, "SOURCE_NOT_REGULAR"):
            self.run_collect()

    def test_main_failure_removes_stale_copies(self):
        self.snapshot.write_text('{"x": 1}')
        self.run_collect()
        self.write_env("GARBAGE=1\n")
        code = collect.main(["--env", str(self.env), "--inbox", str(self.inbox)])
        self.assertEqual(code, 3)
        self.assertFalse((self.inbox / "snapshot.json").exists())
        self.assertFalse((self.inbox / "trades.db").exists())


class SameHostEndToEndTests(CollectorTests):
    def publish(self, at):
        doc = producer.build_snapshot(
            now=at,
            runtime={
                "started_at": at - timedelta(hours=1),
                "pid": 1,
                "db_initialized": True,
                "capture_running": True,
                "last_ws_message": at - timedelta(seconds=2),
                "is_paused": False,
                "pause_reason": None,
                "motor1_local_pause": None,
                "last_error": None,
                "last_error_at": None,
                "books": {"tracked": 2, "initialized": 2, "gaps_last_60s": 0, "sids_disabled": 0},
                "motor5": {},
            },
            config={
                "kalshi_env": "production",
                "trading_enabled": False,
                "balance_refresh_seconds": 300,
                "motors": {},
            },
            capital={"raw_balance_usd": 50.0, "balance_at": at - timedelta(minutes=1)},
            ledger=producer.read_ledger(self.db, now=at),
            release="a" * 40,
        )
        producer.export_snapshot(doc, self.live / "supervision", history_max=5)
        return self.live / "supervision" / "latest.json"

    def test_collect_audit_read_with_live_untouched(self):
        self.snapshot = self.publish(datetime.now(UTC))
        self.write_env(f"BOTKALSHI_LIVE_DB={self.db}\nBOTKALSHI_LIVE_SNAPSHOT={self.snapshot}\n")
        with sqlite3.connect(self.db) as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        live_before = {
            p: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.live.rglob("*")
            if p.is_file()
        }
        state = self.dir / "state"
        self.run_collect()
        self.assertEqual(
            production_audit.main(
                [
                    "--source",
                    str(self.inbox / "trades.db"),
                    "--state",
                    str(state / "audit.sqlite3"),
                    "--report",
                    str(state / "audit.json"),
                ]
            ),
            0,
        )
        self.assertEqual(
            supervision_reader.main(
                [
                    "--snapshot",
                    str(self.inbox / "snapshot.json"),
                    "--audit",
                    str(state / "audit.json"),
                    "--state",
                    str(state / "alerts.json"),
                    "--outbox",
                    str(state / "outbox.jsonl"),
                    "--report",
                    str(state / "reader.json"),
                ]
            ),
            0,
        )
        report = json.loads((state / "reader.json").read_text())
        self.assertEqual(report["view_state"], "OK", report)
        self.assertEqual((report["authority"], report["commands_executed"]), ("NONE", []))
        live_after = {
            p: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in self.live.rglob("*")
            if p.is_file()
        }
        self.assertEqual(live_before, live_after)
        # The producer stops: the copy is removed, the reader reports it.
        self.snapshot.unlink()
        self.run_collect()
        supervision_reader.main(
            [
                "--snapshot",
                str(self.inbox / "snapshot.json"),
                "--state",
                str(state / "alerts.json"),
                "--outbox",
                str(state / "outbox.jsonl"),
                "--report",
                str(state / "reader.json"),
            ]
        )
        outbox = (state / "outbox.jsonl").read_text()
        self.assertIn("SERVICE_DOWN", outbox)
        self.assertEqual(stat.S_IMODE((state / "outbox.jsonl").stat().st_mode), 0o600)


class AuditNeverUsesTheSimulationBankTests(unittest.TestCase):
    def test_audit_module_does_not_import_or_call_the_bank(self):
        tree = ast.parse((BASE / "production_audit.py").read_text())
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertFalse({"simulation_bank", "init_sim_bank", "m5_research_sim"} & imported)
        called = {
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        self.assertNotIn("init_bank", called)

    def test_a_simulation_bank_file_is_not_a_valid_source(self):
        import simulation_bank

        with tempfile.TemporaryDirectory() as tmp:
            bank_db = Path(tmp) / "simulation-bank.sqlite3"
            simulation_bank.init_bank(bank_db, initial_capital_usd="200.00")
            with self.assertRaisesRegex(production_audit.AuditError, "SOURCE_SCHEMA_INCOMPATIBLE"):
                production_audit.audit_pass(bank_db, Path(tmp) / "audit.sqlite3")
            state = Path(tmp) / "audit.sqlite3"
            if state.exists():
                with sqlite3.connect(state) as con:
                    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
                self.assertFalse([t for t in tables if t.startswith("simulation")])


if __name__ == "__main__":
    unittest.main()
