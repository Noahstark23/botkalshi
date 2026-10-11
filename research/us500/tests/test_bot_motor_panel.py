"""Pruebas del panel completo con red bloqueada y bases temporales."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_us500_control import CHAT, NOW, OWNER, confirmation, update

from bot_motor_panel import MOTORS, disconnected_detail, overview
from us500_control import PaperControl
from us500_telegram import _payload


class MotorPanelTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch("socket.socket", side_effect=AssertionError("NO_NETWORK"))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "control.sqlite"
        self.control = PaperControl(self.path, owner_user_id=OWNER, owner_chat_id=CHAT)
        self.number = 0

    def send(self, **kwargs):
        self.number += 1
        result = self.control.handle_update(
            update(self.number, **kwargs), now_ms=NOW + self.number
        )
        for response in result:
            _payload(response["method"], response["payload"], CHAT)
        return result

    def test_all_entry_commands_show_catalog_without_claiming_live_status(self):
        for command in ("/start", "/menu", "/motores"):
            with self.subTest(command=command):
                payload = self.send(text=command)[-1]["payload"]
                text = payload["text"]
                self.assertIn("no consulta su estado", text)
                self.assertIn("permiso de simulación pausado", text)
                buttons = [
                    b for row in payload["reply_markup"]["inline_keyboard"] for b in row
                ]
                targets = [b["callback_data"] for b in buttons]
                for key in ("m1", "m2", "m3", "m5", "m6", "rest", "m8", "m9", "all"):
                    self.assertIn("u5:engine:" + key, targets)
                self.assertIn("u5:menu", targets)
                self.assertEqual(len(targets), 11)
        self.assertFalse(self.control.snapshot()["production_control_available"])

    def test_each_disconnected_motor_has_explicit_locked_controls(self):
        for key in [m.key for m in MOTORS] + ["all"]:
            with self.subTest(key=key):
                payload = self.send(action="u5:engine:" + key)[-1]["payload"]
                self.assertIn("Estado del proceso: no consultado", payload["text"])
                buttons = payload["reply_markup"]["inline_keyboard"][0]
                self.assertEqual(
                    [b["callback_data"] for b in buttons],
                    [f"u5:blocked:{key}:enable", f"u5:blocked:{key}:pause"],
                )
                self.assertTrue(all("sin conexión" in b["text"] for b in buttons))

    def test_disconnected_actions_never_change_us500_permission_or_pending_nonce(self):
        action = confirmation(self.send(action="u5:enable"))
        before = self.control.snapshot()
        for key in [m.key for m in MOTORS] + ["all"]:
            for verb in ("enable", "pause"):
                with self.subTest(key=key, verb=verb):
                    payload = self.send(action=f"u5:blocked:{key}:{verb}")[-1][
                        "payload"
                    ]
                    self.assertIn("No se aplicó ningún cambio", payload["text"])
                    state = self.control.snapshot()
                    for field in (
                        "revision",
                        "paper_enabled",
                        "reason",
                        "updated_at_ms",
                    ):
                        self.assertEqual(state[field], before[field])
        self.send(action=action)
        self.assertTrue(self.control.snapshot()["paper_enabled"])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM paper_control_audit WHERE action='REJECT_DISCONNECTED'"
                ).fetchone()[0],
                18,
            )

    def test_general_pause_is_not_misrepresented_as_paper_pause(self):
        action = confirmation(self.send(action="u5:enable"))
        self.send(action=action)
        self.send(action="u5:blocked:all:pause")
        self.assertTrue(self.control.snapshot()["paper_enabled"])
        self.send(action="u5:pause")
        self.assertFalse(self.control.snapshot()["paper_enabled"])

    def test_us500_roundtrip_and_restart_keep_exact_control_identity(self):
        before = self.control.snapshot()
        menu = self.send(action="u5:menu")[-1]["payload"]
        self.assertIn("No inicia una estrategia automática", menu["text"])
        action = confirmation(self.send(action="u5:enable"))
        self.send(action=action)
        payload = self.send(text="/motores")[-1]["payload"]
        self.assertIn("permiso de simulación habilitado", payload["text"])
        reopened = PaperControl(self.path, owner_user_id=OWNER, owner_chat_id=CHAT)
        self.assertTrue(reopened.snapshot()["paper_enabled"])
        self.assertEqual(reopened.snapshot()["control_id"], before["control_id"])
        self.send(action="u5:pause")
        self.assertFalse(reopened.snapshot()["paper_enabled"])

    def test_unauthorized_catalog_and_callbacks_are_silent_and_do_not_change_state(
        self,
    ):
        before = self.control.snapshot()
        for key in [m.key for m in MOTORS] + ["all"]:
            for action in (
                "u5:panel",
                f"u5:engine:{key}",
                f"u5:blocked:{key}:enable",
                f"u5:blocked:{key}:pause",
            ):
                for override in (
                    {"actor": OWNER + 1},
                    {"chat": CHAT + 1},
                    {"kind": "group"},
                ):
                    self.assertEqual(self.send(action=action, **override), [])
        self.assertEqual(self.control.snapshot(), before)

    def test_unknown_actions_cannot_address_production_or_echo_untrusted_data(self):
        before = self.control.snapshot()
        for action in (
            "u5:engine:PRODUCTION_PRIVATE",
            "u5:blocked:us500:enable",
            "u5:blocked:m2:live",
            "u5:blocked:m2:enable:extra",
            "u5:blocked:all:resume",
            "u5:blocked:__dict__:enable",
        ):
            payload = self.send(action=action)[-1]["payload"]
            self.assertIn("Acción desconocida", payload["text"])
            self.assertNotIn("PRODUCTION_PRIVATE", json.dumps(payload))
        self.assertEqual(self.control.snapshot()["revision"], before["revision"])
        self.assertFalse(self.control.snapshot()["paper_enabled"])

    def test_duplicate_disconnected_update_has_one_audit_record(self):
        item = update(1, action="u5:blocked:m5:pause")
        first = self.control.handle_update(item, now_ms=NOW)
        self.assertEqual(self.control.handle_update(item, now_ms=NOW + 1), first)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM paper_control_audit").fetchone()[0], 1
            )

    def test_pending_confirmation_does_not_become_global_after_navigation(self):
        action = confirmation(self.send(action="u5:enable"))
        self.send(action="u5:engine:all")
        self.send(action=action)
        payload = self.send(action="u5:panel")[-1]["payload"]
        self.assertIn("conexión pendiente", payload["text"])
        self.assertFalse(self.control.snapshot()["production_control_available"])

    def test_descriptions_preserve_dependency_and_exit_distinctions(self):
        self.assertIn(
            "salidas y los datos que consume M5", disconnected_detail("m2")[0]
        )
        self.assertIn("no es un motor de nuevas compras", disconnected_detail("m3")[0])
        self.assertIn("canceladas sin confirmación", disconnected_detail("m5")[0])
        self.assertIn("liquidaciones", disconnected_detail("rest")[0])
        for key in ("m6", "m8", "m9"):
            self.assertIn("No tiene ejecutor", disconnected_detail(key)[0])
        self.assertIsNone(disconnected_detail("m4"))
        self.assertIn("no consulta su estado", overview(False)[0])


if __name__ == "__main__":
    unittest.main()
