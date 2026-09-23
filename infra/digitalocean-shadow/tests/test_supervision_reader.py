"""S3 — read-only supervision consumer and technical alerts (ASTRA-SUPERVISION-20260923).

Snapshots come from the PRODUCER'S OWN builder/exporter
(`src.monitoring.supervision_snapshot`), i.e. the real contract. A simulated M5 fill
report is the counter-fixture. Pinned:
  - a missing / expired / corrupt / simulation snapshot is never healthy;
  - duplicated alerts do not multiply; recovery requires NEW evidence;
  - no secret reaches an alert;
  - observing never calls set_pause / assess / executors, and the runtime reads
    nothing this module writes (an AI/reader outage cannot change its decisions);
  - the SSH adapter keeps host-key checking, takes no key argument, and is never run.
Transports are fakes: no message leaves this machine. Stdlib only. No network.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import stat
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parents[1]
sys.path.insert(0, str(BASE))

import assistant_bridge  # noqa: E402
import supervision_reader as reader  # noqa: E402

from src.monitoring import supervision_snapshot as producer  # noqa: E402

NOW = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)
SHA = "a4d7b7a" + "0" * 33


def production_doc(*, at=NOW, snapshot_id="s1", **overrides):
    runtime = {
        "started_at": at - timedelta(hours=1),
        "pid": 7,
        "db_initialized": True,
        "capture_running": True,
        "last_ws_message": at - timedelta(seconds=3),
        "is_paused": False,
        "pause_reason": None,
        "motor1_local_pause": None,
        "last_error": None,
        "last_error_at": None,
        "books": {"tracked": 4, "initialized": 4, "gaps_last_60s": 0, "sids_disabled": 0},
        "motor5": {},
    }
    runtime.update(overrides.pop("runtime", {}))
    ledger = {
        "status": "OK",
        "read_at": at,
        "kill_switch": None,
        "mm_quotes_paused": None,
        "trades_by_status": {"settled": 3},
        "open_filled": (0, 0),
        "realized": {"today": (1, 12), "last_7d": (3, 20), "month": (3, 20)},
        "last_trade_at": "2026-09-23 12:00:00",
        "positions": (1, "2026-09-23 13:55:00"),
        "risk_events": [],
        "incoherent": {"settled_without_pnl": 0},
    }
    ledger.update(overrides.pop("ledger", {}))
    capital = {
        "raw_balance_usd": 99.37,
        "balance_at": at - timedelta(minutes=1),
        "is_paused": False,
    }
    capital.update(overrides.pop("capital", {}))
    return producer.build_snapshot(
        now=at,
        runtime=runtime,
        config={
            "kalshi_env": "production",
            "trading_enabled": False,
            "balance_refresh_seconds": 300,
            "motors": {},
        },
        capital=capital,
        ledger=ledger,
        release=SHA,
        snapshot_id=snapshot_id,
    )


SIM_FILL_REPORT = {
    "schema_version": "botkalshi-m5-research-cycle-v1",
    "cohort": "m5-research-rest-v1",
    "mode": "SIMULATION_ONLY",
    "evaluations": [
        {
            "status": "EVALUATED",
            "fills": [{"side": "buy", "price_cents": 45, "count": 1, "fee_cents": 1}],
        }
    ],
}


class ReaderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.out = self.dir / "supervision"
        self.state = self.dir / "alerts" / "state.json"
        for name in ("connect", "connect_ex"):
            blocker = patch.object(
                socket.socket, name, side_effect=AssertionError("NETWORK FORBIDDEN")
            )
            blocker.start()
            self.addCleanup(blocker.stop)

    def publish(self, doc):
        producer.export_snapshot(doc, self.out, history_max=10)
        return self.out / "latest.json"

    def view(self, now=NOW):
        return reader.read_snapshot(self.out / "latest.json", now=now)


class ReadTests(ReaderTestCase):
    def test_real_contract_snapshot_reads_ok(self):
        self.publish(production_doc())
        view = self.view()
        self.assertEqual(view["state"], "OK")
        self.assertEqual(reader.conditions(view), {})
        self.assertEqual(view["authority"], "NONE")

    def test_missing_expired_corrupt_and_simulation_are_never_healthy(self):
        self.assertEqual(self.view()["state"], "MISSING")
        self.publish(production_doc())
        self.assertEqual(self.view(now=NOW + timedelta(minutes=10))["state"], "EXPIRED")
        (self.out / "latest.json").write_text("{not json")
        self.assertEqual(self.view()["state"], "CORRUPT")
        (self.out / "latest.json").write_text(json.dumps(SIM_FILL_REPORT))
        self.assertEqual(self.view()["state"], "WRONG_DOMAIN")
        tampered = production_doc()
        tampered["environment"] = {"X": "y"}
        (self.out / "latest.json").write_text(json.dumps(tampered))
        self.assertEqual(self.view()["state"], "CORRUPT")
        future = production_doc(at=NOW + timedelta(minutes=5))
        (self.out / "latest.json").write_text(json.dumps(future))
        self.assertEqual(self.view()["state"], "CORRUPT")

    def test_relabelled_simulation_is_wrong_domain(self):
        doc = production_doc()
        doc["ledger_domain"] = "SIMULATION"
        (self.out).mkdir(parents=True, exist_ok=True)
        (self.out / "latest.json").write_text(json.dumps(doc))
        self.assertEqual(self.view()["state"], "WRONG_DOMAIN")

    def test_symlinked_snapshot_is_refused(self):
        self.publish(production_doc())
        link = self.dir / "link.json"
        link.symlink_to(self.out / "latest.json")
        self.assertEqual(reader.read_snapshot(link, now=NOW)["state"], "CORRUPT")

    def test_unknown_health_is_reported_as_unknown(self):
        self.publish(production_doc(capital={"raw_balance_usd": None, "balance_at": None}))
        view = self.view()
        self.assertEqual(view["state"], "UNKNOWN")
        self.assertIn("BALANCE_EVIDENCE_LOST", reader.conditions(view))


class ConditionTests(ReaderTestCase):
    def cond(self, doc, audit=None):
        self.publish(doc)
        return reader.conditions(self.view(), audit)

    def test_each_technical_condition(self):
        self.assertIn(
            "FEED_STALE",
            self.cond(production_doc(runtime={"last_ws_message": NOW - timedelta(minutes=5)})),
        )
        self.assertIn(
            "BALANCE_EVIDENCE_LOST",
            self.cond(production_doc(capital={"balance_at": NOW - timedelta(hours=1)})),
        )
        self.assertIn(
            "LEDGER_INCONSISTENT",
            self.cond(production_doc(ledger={"incoherent": {"settled_without_pnl": 2}})),
        )
        self.assertIn(
            "KILL_SWITCH_ENGAGED",
            self.cond(
                production_doc(
                    ledger={"kill_switch": ("engaged", "weekly stop", "2026-09-23 13:00:00")}
                )
            ),
        )
        audit = {"status": "DIAGNOSTICS", "diagnostics": {"SUM_MISMATCH": 1, "PARTIAL_PASS": 1}}
        self.assertEqual(self.cond(production_doc(), audit)["AUDIT_DIAGNOSTICS"], "SUM_MISMATCH")

    def test_service_down_when_the_producer_stops_writing(self):
        self.publish(production_doc())
        view = reader.read_snapshot(self.out / "latest.json", now=NOW + timedelta(minutes=4))
        self.assertIn("SERVICE_DOWN", reader.conditions(view))


class AlertTests(ReaderTestCase):
    def run_alerts(self, now, transport, audit=None):
        return reader.process_alerts(
            self.view(now=now),
            state_path=self.state,
            transport=transport,
            now=now,
            audit_report=audit,
        )

    def test_duplicates_do_not_multiply_and_backoff_grows(self):
        self.publish(production_doc(runtime={"last_ws_message": NOW - timedelta(minutes=5)}))
        fake = reader.FakeTransport(sent=[])
        for seconds in (1, 40, 80, 120):  # all inside the snapshot's validity (180 s)
            self.run_alerts(NOW + timedelta(seconds=seconds), fake)
        self.assertEqual(len(fake.sent), 1)  # four runs, one message
        self.assertIn("FEED_STALE", fake.sent[0])
        # backoff 5 min, then 15 min: a new doc keeps it alive and in the window
        self.publish(
            production_doc(
                at=NOW + timedelta(minutes=5), snapshot_id="s2", runtime={"last_ws_message": NOW}
            )
        )
        self.run_alerts(NOW + timedelta(minutes=5, seconds=2), fake)
        self.assertEqual(len(fake.sent), 2)
        self.publish(
            production_doc(
                at=NOW + timedelta(minutes=10), snapshot_id="s3", runtime={"last_ws_message": NOW}
            )
        )
        self.run_alerts(NOW + timedelta(minutes=10, seconds=2), fake)
        self.assertEqual(len(fake.sent), 2)  # inside the 15-minute backoff

    def test_transport_failure_is_retried_not_counted(self):
        self.publish(production_doc(ledger={"incoherent": {"settled_without_pnl": 1}}))
        down = reader.FakeTransport(sent=[], fail=True)
        self.assertEqual(self.run_alerts(NOW, down)["sent"], [])
        up = reader.FakeTransport(sent=[])
        self.assertEqual(
            self.run_alerts(NOW + timedelta(seconds=30), up)["sent"], ["LEDGER_INCONSISTENT"]
        )

    def test_recovery_requires_new_evidence(self):
        self.publish(production_doc(ledger={"incoherent": {"settled_without_pnl": 1}}))
        fake = reader.FakeTransport(sent=[])
        self.run_alerts(NOW, fake)
        # The condition "disappears" but it is the SAME snapshot id: not proven.
        same = production_doc()  # snapshot_id "s1" again, clean
        self.publish(same)
        outcome = self.run_alerts(NOW + timedelta(seconds=30), fake)
        self.assertEqual(outcome["held"], ["LEDGER_INCONSISTENT"])
        # A newer snapshot that is clean resolves it.
        self.publish(production_doc(at=NOW + timedelta(minutes=1), snapshot_id="s2"))
        outcome = self.run_alerts(NOW + timedelta(minutes=1, seconds=5), fake)
        self.assertEqual(outcome["resolved"], ["LEDGER_INCONSISTENT"])
        self.assertIn("RESOLVED", fake.sent[-1])
        # It fires again, fresh, if the problem comes back.
        self.publish(
            production_doc(
                at=NOW + timedelta(minutes=2),
                snapshot_id="s3",
                ledger={"incoherent": {"settled_without_pnl": 1}},
            )
        )
        self.assertEqual(
            self.run_alerts(NOW + timedelta(minutes=2, seconds=5), fake)["sent"],
            ["LEDGER_INCONSISTENT"],
        )

    def test_alert_state_survives_a_restart(self):
        self.publish(production_doc(ledger={"incoherent": {"settled_without_pnl": 1}}))
        fake = reader.FakeTransport(sent=[])
        self.run_alerts(NOW, fake)
        import importlib

        importlib.reload(reader)
        fake2 = reader.FakeTransport(sent=[])
        reader.process_alerts(
            self.view(), state_path=self.state, transport=fake2, now=NOW + timedelta(seconds=10)
        )
        self.assertEqual(fake2.sent, [])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)

    def test_no_secret_reaches_an_alert(self):
        """A tampered snapshot whose extra KEY looks like a credential: the key name is
        echoed in the contract problems, so only the final scan stands between it and
        the alert text."""
        corrupt = production_doc()
        corrupt["apiKey=abc123secret"] = 1
        (self.out).mkdir(parents=True, exist_ok=True)
        (self.out / "latest.json").write_text(json.dumps(corrupt))
        fake = reader.FakeTransport(sent=[])
        outcome = self.run_alerts(NOW, fake)
        self.assertEqual(outcome["dropped"], ["SNAPSHOT_CORRUPT"])
        for text in fake.sent:
            self.assertFalse(producer.contains_secret(text), text)
            self.assertNotIn("abc123secret", text)


class NoAuthorityTests(ReaderTestCase):
    def test_bridge_production_status_is_read_only(self):
        self.publish(
            production_doc(ledger={"kill_switch": ("engaged", "x", "2026-09-23 13:00:00")})
        )
        with (
            patch.object(assistant_bridge, "set_pause", side_effect=AssertionError("set_pause")),
            patch.object(assistant_bridge, "_assess_locked", side_effect=AssertionError("assess")),
        ):
            result = assistant_bridge.production_status(self.out / "latest.json", now=NOW)
        self.assertIn("KILL_SWITCH_ENGAGED", result["alert_conditions"])
        self.assertEqual((result["authority"], result["commands_executed"]), ("NONE", []))
        self.assertFalse(list(self.dir.rglob("control-state.json")))

    def test_reader_never_references_control_or_execution(self):
        tree = ast.parse((BASE / "supervision_reader.py").read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        for forbidden in (
            "set_pause",
            "_assess_locked",
            "assess",
            "place_order",
            "cancel_order",
            "clear_kill_switch",
            "engage_kill_switch",
        ):
            self.assertNotIn(forbidden, names)

    def test_runtime_never_reads_supervision_output(self):
        """An AI/reader outage cannot change runtime decisions: src/ imports none of it."""
        for path in (ROOT / "src").rglob("*.py"):
            text = path.read_text()
            for module in ("supervision_reader", "assistant_bridge", "production_audit"):
                self.assertNotIn(f"import {module}", text, path)
                self.assertNotIn(f"from {module}", text, path)


class SshAdapterTests(ReaderTestCase):
    def test_adapter_keeps_host_key_checking_and_takes_no_key(self):
        doc = production_doc()
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout=json.dumps(doc).encode())

        dest = reader.fetch_snapshot_via_ssh(
            "botkalshi-supervisor",
            "/var/lib/botkalshi/supervision/latest.json",
            self.dir / "fetched" / "latest.json",
            runner=runner,
        )
        [argv] = calls
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("BatchMode=yes", argv)
        self.assertNotIn("-i", argv)
        self.assertFalse(any(a.split("=")[-1] == "no" for a in argv if "=" in a))
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o600)
        self.assertEqual(reader.read_snapshot(dest, now=NOW)["state"], "OK")

    def test_adapter_rejects_bad_alias_path_and_failures(self):
        ok = lambda argv, **kw: SimpleNamespace(returncode=0, stdout=b"{}")  # noqa: E731
        for alias, path in (
            ("root@1.2.3.4", "/x/supervision/latest.json"),
            ("-oProxyCommand=x", "/x/supervision/latest.json"),
            ("alias", "/etc/shadow"),
            ("alias", "/x/../supervision/latest.json"),
        ):
            with self.subTest(alias=alias, path=path), self.assertRaises(ValueError):
                reader.fetch_snapshot_via_ssh(alias, path, self.dir / "f.json", runner=ok)
        bad = lambda argv, **kw: SimpleNamespace(returncode=255, stdout=b"")  # noqa: E731
        with self.assertRaises(RuntimeError):
            reader.fetch_snapshot_via_ssh(
                "alias", "/x/supervision/latest.json", self.dir / "f.json", runner=bad
            )

    def test_no_ssh_is_executed_by_this_suite(self):
        with patch.object(reader.subprocess, "run", side_effect=AssertionError("ssh run")):
            self.publish(production_doc())
            reader.process_alerts(
                self.view(), state_path=self.state, transport=reader.FakeTransport(sent=[]), now=NOW
            )
        self.assertTrue(os.path.exists(self.state))


if __name__ == "__main__":
    unittest.main()
