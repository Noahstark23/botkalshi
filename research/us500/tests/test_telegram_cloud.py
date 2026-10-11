"""Cloud bootstrap rejects missing migration, unsafe secrets, and bot mix-ups."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from us500_control import PaperControl
from us500_telegram import PaperTelegram, TelegramTransport
from us500_telegram_cloud import PreflightTransient, load_credential, main

TOKEN = "12345:" + "synthetic-not-a-real-token"


class CloudTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "token"
        self.path.write_text(TOKEN)
        os.chmod(self.path, 0o600)
        self.calls = []
        self.responses = [
            {"ok": True, "result": {"id": 12345, "is_bot": True}},
            {"ok": True, "result": {"url": ""}},
        ]
        self.closed = 0
        test = self

        class Connection:
            def __init__(self, host, **kwargs):
                test.assertEqual(host, "api.telegram.org")

            def request(self, method, path):
                test.calls.append((method, path.rsplit("/", 1)[-1]))

            def getresponse(self):
                self.status = 200
                return self

            def read(self, maximum):
                test.assertEqual(maximum, 65537)
                return json.dumps(test.responses.pop(0)).encode()

            def close(self):
                test.closed += 1

        self.factory = Connection

    def read(self):
        return load_credential(
            self.path, bot_id=12345, owner_chat_id=456, connection_factory=self.factory
        )

    def test_only_identity_and_webhook_reads_with_exact_bot(self):
        self.assertEqual(self.read(), TOKEN)
        self.assertEqual(self.calls, [("GET", "getMe"), ("GET", "getWebhookInfo")])
        self.assertEqual(self.closed, 2)

    def test_unsafe_mode_and_symlink_rejected_before_network(self):
        os.chmod(self.path, 0o644)
        with self.assertRaises(ValueError):
            self.read()
        os.chmod(self.path, 0o600)
        link = self.path.with_suffix(".link")
        link.symlink_to(self.path)
        self.path = link
        with self.assertRaises(OSError):
            self.read()
        self.assertEqual(self.calls, [])

    def test_systemd_root_service_group_readonly_credential(self):
        metadata = SimpleNamespace(
            st_mode=0o100440, st_uid=0, st_gid=os.getgid(), st_size=len(TOKEN)
        )
        with patch("os.fstat", return_value=metadata):
            self.assertEqual(self.read(), TOKEN)

    def test_other_group_or_writable_group_is_rejected(self):
        for mode, uid, gid in (
            (0o100440, 0, os.getgid() + 1),
            (0o100640, 0, os.getgid()),
            (0o100444, 0, os.getgid()),
        ):
            metadata = SimpleNamespace(
                st_mode=mode, st_uid=uid, st_gid=gid, st_size=len(TOKEN)
            )
            with (
                patch("os.fstat", return_value=metadata),
                self.assertRaises(ValueError),
            ):
                self.read()
        self.assertEqual(self.calls, [])

    def test_wrong_bot_and_unknown_or_existing_webhook_rejected(self):
        for result in ({"url": "https://private.invalid"}, {}, None):
            self.responses = [
                {"ok": True, "result": {"id": 12345, "is_bot": True}},
                {"ok": True, "result": result},
            ]
            with self.assertRaises(ValueError):
                self.read()
        self.responses = [{"ok": True, "result": {"id": 12346, "is_bot": True}}]
        with self.assertRaises(ValueError):
            self.read()

    def test_network_failure_preflight_is_retryable_without_leaking_details(self):
        with patch.object(self.factory, "request", side_effect=TimeoutError(TOKEN)):
            with self.assertRaises(PreflightTransient) as caught:
                self.read()
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertEqual(self.closed, 1)

    def test_cloud_start_transient_returns_retryable_exit_and_durable_pause(self):
        root = Path(self.temp.name)
        control = PaperControl(
            root / "control.sqlite", owner_user_id=456, owner_chat_id=456
        )
        adapter = PaperTelegram(
            root / "outbox.sqlite",
            control=control,
            transport=TelegramTransport(TOKEN, owner_chat_id=456),
        )
        adapter.close()
        for error, expected in (
            (PreflightTransient("TEMPORARY"), 75),
            (ValueError("PRIVATE"), 78),
        ):
            output = io.StringIO()
            with (
                patch("signal.signal"),
                patch("us500_telegram_cloud.load_credential", side_effect=error),
                contextlib.redirect_stdout(output),
            ):
                result = main(
                    [
                        "--state-dir",
                        str(root),
                        "--credential-file",
                        str(self.path),
                        "--bot-id",
                        "12345",
                        "--owner-id",
                        "456",
                    ]
                )
            self.assertEqual(result, expected)
            value = json.loads(output.getvalue())
            self.assertTrue(value["paper_pause_confirmed"])
            self.assertFalse(value["control"]["paper_enabled"])
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertNotIn(TOKEN, output.getvalue())

    def test_missing_migrated_state_does_not_open_network_or_create_db(self):
        output = io.StringIO()
        with (
            patch("signal.signal"),
            patch("socket.socket", side_effect=AssertionError("NETWORK")),
            contextlib.redirect_stdout(output),
        ):
            result = main(
                [
                    "--state-dir",
                    str(Path(self.temp.name) / "missing"),
                    "--credential-file",
                    str(self.path),
                    "--bot-id",
                    "12345",
                    "--owner-id",
                    "456",
                ]
            )
        self.assertEqual(result, 78)
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertFalse((Path(self.temp.name) / "missing").exists())


if __name__ == "__main__":
    unittest.main()
