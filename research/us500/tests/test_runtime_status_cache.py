"""Caché saneada, caducidad y permisos; ninguna lectura hace red."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_motor_status_readonly import NOW, health, status

from motor_status_readonly import status_from_responses
from runtime_status_cache import SnapshotStatusReader, publish


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "status.json"
        self.reader = SnapshotStatusReader(self.path, trusted_uid=os.getuid())
        self.snapshot = status_from_responses(health(), status(), now_ms=NOW)
        guard = patch("socket.socket", side_effect=AssertionError("NO_NETWORK"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_publish_and_read_sanitized_then_expire(self):
        self.snapshot["secret"] = "PRIVATE"
        publish(self.path, self.snapshot, now_ms=NOW)
        self.assertNotIn("PRIVATE", self.path.read_text())
        self.assertTrue(self.reader(now_ms=NOW)["fresh"])
        self.assertEqual(self.reader(now_ms=NOW + 30001)["reason"], "STALE")
        self.assertIsNone(self.reader(now_ms=NOW + 30001)["flags"]["trading_enabled"])

    def test_missing_symlink_bad_json_and_writable_source_are_unknown(self):
        self.assertFalse(self.reader(now_ms=NOW)["fresh"])
        publish(self.path, self.snapshot, now_ms=NOW)
        os.chmod(self.path, 0o666)
        self.assertFalse(self.reader(now_ms=NOW)["fresh"])
        os.chmod(self.path, 0o644)
        self.path.write_text('{"schema":"bad","schema":"bad"}')
        self.assertFalse(self.reader(now_ms=NOW)["fresh"])
        self.path.unlink()
        self.path.symlink_to(Path(self.tmp.name) / "missing")
        self.assertFalse(self.reader(now_ms=NOW)["fresh"])

    def test_default_reader_rejects_own_writable_source_and_wrong_owner(self):
        publish(self.path, self.snapshot, now_ms=NOW)
        self.assertFalse(SnapshotStatusReader(self.path)(now_ms=NOW)["fresh"])
        self.assertFalse(
            SnapshotStatusReader(self.path, trusted_uid=os.getuid() + 1)(now_ms=NOW)[
                "fresh"
            ]
        )

    def test_no_claims_or_financial_payload_leak(self):
        self.snapshot.update(orders=[1], balance=100, execution_available=True)
        publish(self.path, self.snapshot, now_ms=NOW)
        data = json.loads(self.path.read_text())
        self.assertNotIn("orders", data)
        self.assertNotIn("balance", data)
        self.assertFalse(data["real_controls_available"])


if __name__ == "__main__":
    unittest.main()
