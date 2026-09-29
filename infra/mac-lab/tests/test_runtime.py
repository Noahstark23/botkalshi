"""Offline acceptance tests. Network I/O is blocked, not merely avoided."""
from __future__ import annotations
import contextlib
from datetime import UTC, datetime, timedelta
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location("lab_runtime", Path(__file__).resolve().parents[1] / "runtime.py")
lab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lab)


class LabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.net = patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden"))
        self.net.start()
        self.addCleanup(self.net.stop)
        self.net_ex = patch.object(socket.socket, "connect_ex", side_effect=AssertionError("network forbidden"))
        self.net_ex.start()
        self.addCleanup(self.net_ex.stop)

    def test_no_credentials(self):
        for name in lab.SECRET_NAMES:
            with self.subTest(name=name), self.assertRaises(lab.LabError):
                lab.safety_check({name: "sensitive-value-not-to-log"})

    def test_flags_fail_closed(self):
        for name in ("TRADING_ENABLED", "MOTOR_MM_EXECUTION_ENABLED", "MOTOR_3_EXECUTION_ENABLED"):
            for value in ("true", "yes", "1", "typo"):
                with self.subTest(name=name, value=value), self.assertRaises(lab.LabError):
                    lab.safety_check({name: value})
        lab.safety_check({"TRADING_ENABLED": "false"})

    def test_synthetic_distinct_and_no_bank(self):
        report = lab.offline_once(self.data)
        self.assertTrue(report["synthetic"])
        self.assertFalse(report["execution_enabled"])
        self.assertFalse(report["legacy_motors_running"])
        self.assertNotIn("balance", report)
        self.assertEqual(report["result"]["economic_gate"], "NOT_EVALUATED")
        self.assertEqual(json.loads((self.data / "reports/latest.json").read_text()), report)

    def test_missing_snapshot_never_created(self):
        path = self.data / "missing.db"
        with self.assertRaises(lab.LabError):
            lab.inspect_snapshot(path)
        self.assertFalse(path.exists())

    def test_snapshot_is_unchanged(self):
        path = lab.make_fixture(self.data)
        before = path.read_bytes()
        self.assertEqual(lab.inspect_snapshot(path)["tables"]["mm_shadow_fills"]["rows"], 1)
        self.assertEqual(before, path.read_bytes())

    def test_source_connection_is_readonly(self):
        path = lab.make_fixture(self.data)
        with contextlib.closing(lab.read_db(path)) as con, self.assertRaises(sqlite3.OperationalError):
            con.execute("DELETE FROM mm_shadow_fills")

    def test_wal_snapshot_rejected(self):
        path = lab.make_fixture(self.data)
        Path(str(path) + "-wal").touch()
        with self.assertRaises(lab.LabError):
            lab.inspect_snapshot(path)

    def test_missing_tables_unknown_not_zero(self):
        path = self.data / "other.db"
        with contextlib.closing(sqlite3.connect(path)) as con:
            con.execute("CREATE TABLE secrets (value TEXT)")
            con.execute("INSERT INTO secrets VALUES ('must-not-export')")
            con.commit()
        report = lab.inspect_snapshot(path)
        self.assertIsNone(report["tables"]["mm_shadow_fills"]["rows"])
        self.assertNotIn("must-not-export", json.dumps(report))
        self.assertNotIn("secrets", json.dumps(report))

    def test_corrupt_snapshot_fails(self):
        path = self.data / "corrupt.db"
        path.write_bytes(b"not a database")
        with self.assertRaises(sqlite3.Error):
            lab.inspect_snapshot(path)

    def test_snapshot_symlink_rejected(self):
        path = lab.make_fixture(self.data)
        link = self.data / "link.db"
        link.symlink_to(path)
        with self.assertRaises(lab.LabError):
            lab.inspect_snapshot(link)

    def test_writer_lock_excludes_second(self):
        with lab.writer_lock(self.data), self.assertRaises(lab.LabError):
            with lab.writer_lock(self.data):
                pass

    def test_backup_restore_integrity(self):
        lab.offline_once(self.data)
        source = self.data / "bootstrap.sqlite3"
        destination = self.data / "backup.sqlite3"
        outcome = lab.backup_database(source, destination)
        self.assertEqual(outcome["status"], "VERIFIED_SQLITE_COPY")
        self.assertEqual(len(outcome["sha256"]), 64)
        with contextlib.closing(lab.read_db(source)) as a, contextlib.closing(lab.read_db(destination)) as b:
            self.assertEqual(a.execute("SELECT * FROM reports").fetchall(), b.execute("SELECT * FROM reports").fetchall())
            self.assertEqual(b.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_backup_no_overwrite_or_missing(self):
        lab.offline_once(self.data)
        dest = self.data / "existing.db"
        dest.write_bytes(b"preserve")
        with self.assertRaises(lab.LabError):
            lab.backup_database(self.data / "bootstrap.sqlite3", dest)
        self.assertEqual(dest.read_bytes(), b"preserve")
        missing_dest = self.data / "not-created.db"
        with self.assertRaises(lab.LabError):
            lab.backup_database(self.data / "missing.db", missing_dest)
        self.assertFalse(missing_dest.exists())

    def test_public_routes_and_methods_restricted(self):
        allowed = lab.PublicReader.validated_url("GET", "/series/KXMLBGAME")
        self.assertTrue(allowed.startswith(lab.ORIGIN))
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD"):
            with self.subTest(method=method), self.assertRaises(lab.LabError):
                lab.PublicReader.validated_url(method, "/markets")
        for path in ("/portfolio/orders", "/markets/../portfolio/orders", "https://evil.example", "//localhost", "/markets/X%2Forderbook/orderbook"):
            with self.subTest(path=path), self.assertRaises(lab.LabError):
                lab.PublicReader.validated_url("GET", path)

    def test_public_parameters_restricted(self):
        for params in ({"limit": 1000}, {"apiKey": "secret"}, {"cursor": "unbounded"}):
            with self.assertRaises(lab.LabError):
                lab.PublicReader.validated_url("GET", "/markets", params)

    def test_rejection_precedes_network(self):
        reader = lab.PublicReader()
        reader.opener = Mock()
        with self.assertRaises(lab.LabError):
            reader.get("/portfolio/orders")
        reader.opener.open.assert_not_called()

    def test_public_call_cap(self):
        reader = lab.PublicReader()
        reader.opener = Mock()
        reader.calls = 7
        with self.assertRaises(lab.LabError):
            reader.get("/series/KXMLBGAME")
        reader.opener.open.assert_not_called()

    def test_redirect_not_followed(self):
        with self.assertRaises(lab.LabError):
            lab.NoRedirect().redirect_request(None, None, 302, "x", {}, "http://127.0.0.1")

    def test_public_precision_preserved(self):
        reader = lab.PublicReader()
        fake = io.BytesIO(b'{"value": 0.5100, "quantity": "5.50"}')
        reader.opener.open = Mock(return_value=fake)
        body = reader.get("/series/KXMLBGAME")
        self.assertEqual(body["value"], "0.5100")
        self.assertEqual(body["quantity"], "5.50")

    def test_public_oversized_body(self):
        reader = lab.PublicReader()
        reader.opener.open = Mock(return_value=io.BytesIO(b" " * (lab.MAX_BODY + 1)))
        with self.assertRaises(lab.LabError):
            reader.get("/series/KXMLBGAME")

    def test_public_nan_rejected(self):
        reader = lab.PublicReader()
        reader.opener.open = Mock(return_value=io.BytesIO(b'{"value": NaN}'))
        with self.assertRaises(lab.LabError):
            reader.get("/series/KXMLBGAME")

    def test_public_capture_clearly_partial(self):
        with patch.object(lab.PublicReader, "get", side_effect=[
            {"series": {"fee_multiplier": "1"}},
            {"markets": [{"ticker": "KXMLBGAME-SYNTHETIC"}], "cursor": "more"},
            {"orderbook_fp": {"yes_dollars": [["0.5100", "5.50"]], "no_dollars": []}},
        ]):
            report = lab.capture_public(self.data)
        self.assertFalse(report["synthetic"])
        self.assertFalse(report["execution_enabled"])
        self.assertTrue(report["result"]["more_pages"])
        self.assertIn("PARTIAL", report["result"]["coverage"])
        self.assertIsNone(report["result"]["recommendation"])

    def test_public_failure_persists_incomplete(self):
        with patch.object(lab.PublicReader, "get", side_effect=lab.LabError("HTTP blocked")), self.assertRaises(lab.LabError):
            lab.capture_public(self.data)
        report = json.loads((self.data / "reports/latest.json").read_text())
        self.assertEqual(report["result"]["status"], "INCOMPLETE")

    def test_heartbeat_and_clean_shutdown(self):
        lab.serve(self.data, max_ticks=1, interval=0)
        with self.assertRaises(lab.LabError):
            lab.health(self.data)
        state = json.loads((self.data / "health.json").read_text())
        self.assertFalse(state["running"])

    def test_health_stale_rejected(self):
        lab.atomic_text(self.data / "health.json", lab.json_text({
            "running": True, "updated_at": (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        }))
        with self.assertRaises(lab.LabError):
            lab.health(self.data)

    def test_cli_public_optin_required(self):
        with patch.object(sys, "argv", ["runtime", "public-once", "--data", str(self.data)]), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(lab.main(), 2)
        self.assertFalse((self.data / "reports/latest.json").exists())

    def test_cli_secrets_not_logged(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"KALSHI_API_KEY_ID": "dont-print-me"}), patch.object(sys, "argv", ["runtime", "offline-once"]), contextlib.redirect_stderr(output):
            self.assertEqual(lab.main(), 2)
        self.assertNotIn("dont-print-me", output.getvalue())


if __name__ == "__main__":
    unittest.main()
