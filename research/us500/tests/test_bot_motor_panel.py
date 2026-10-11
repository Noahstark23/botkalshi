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
from motor_status_readonly import activity_from_response, status_from_responses
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
                self.assertIn("fuente sin conectar", text)
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

    def test_each_motor_has_only_readonly_refresh_and_back(self):
        for key in [m.key for m in MOTORS] + ["all"]:
            with self.subTest(key=key):
                payload = self.send(action="u5:engine:" + key)[-1]["payload"]
                self.assertIn("Estados desconocidos", payload["text"])
                buttons = [
                    button
                    for row in payload["reply_markup"]["inline_keyboard"]
                    for button in row
                ]
                self.assertEqual(
                    [b["callback_data"] for b in buttons],
                    [f"u5:engine:{key}", "u5:panel"],
                )
                self.assertTrue(
                    all(
                        "Activar" not in b["text"] and "Desactivar" not in b["text"]
                        for b in buttons
                    )
                )

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
        self.assertIn("fuente sin conectar", payload["text"])
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
        self.assertIn("fuente sin conectar", overview(False)[0])

    def runtime(self, now, *, m5_running=False):
        from datetime import datetime, timezone

        return status_from_responses(
            {
                "status": "healthy",
                "timestamp": datetime.fromtimestamp(
                    now / 1000, timezone.utc
                ).isoformat(),
            },
            {
                "bot": {
                    "capture_running": True,
                    "ws_connected": True,
                    "is_paused": False,
                    "motor5": {"enabled": False, "task_running": m5_running},
                },
                "config": {
                    "environment": "production",
                    "trading_enabled": False,
                    "motors_enabled": {
                        "motor_1_arbitrage": True,
                        "motor_2_sportsbook": False,
                        "motor_2_execution": False,
                        "motor_5_mm": False,
                        "motor_5_execution": False,
                    },
                },
                "capital": {"is_paused": True},
            },
            now_ms=now,
        )

    def with_provider(self, provider):
        self.control = PaperControl(
            self.path, owner_user_id=OWNER, owner_chat_id=CHAT, status_provider=provider
        )

    def test_fresh_runtime_separates_configured_detection_and_task(self):
        calls = []

        def provider(*, now_ms):
            calls.append(now_ms)
            return self.runtime(now_ms, m5_running=True)

        self.with_provider(provider)
        m1 = self.send(action="u5:engine:m1")[-1]["payload"]["text"]
        self.assertIn("Detección configurada: sí", m1)
        self.assertIn("Ejecución configurada: desconocido", m1)
        self.assertIn("Tarea reportada: desconocida", m1)
        m5 = self.send(action="u5:engine:m5")[-1]["payload"]["text"]
        self.assertIn("Configurado: no", m5)
        self.assertIn("Tarea reportada: en marcha", m5)
        self.assertIn("M5 habilitado según runtime: no", m5)
        self.assertEqual(len(calls), 2)

    def test_runtime_provider_only_runs_for_authorized_real_status_views(self):
        calls = []

        def provider(*, now_ms):
            calls.append(now_ms)
            return self.runtime(now_ms)

        self.with_provider(provider)
        for action in ("u5:panel", "u5:engine:m1", "u5:blocked:m5:enable"):
            self.assertEqual(self.send(action=action, actor=999), [])
            self.assertEqual(self.send(action=action, kind="group"), [])
        for action in ("u5:menu", "u5:pause", "u5:enable", "u5:engine:private-url"):
            self.send(action=action)
        self.assertEqual(calls, [])
        self.send(text="/motores")
        self.assertEqual(len(calls), 1)

    def test_readonly_views_never_change_permission_or_confirmation(self):
        self.with_provider(lambda *, now_ms: self.runtime(now_ms))
        action = confirmation(self.send(action="u5:enable"))
        before = self.control.snapshot()
        for key in [m.key for m in MOTORS] + ["all"]:
            self.send(action="u5:engine:" + key)
        after = self.control.snapshot()
        for field in ("paper_enabled", "revision", "reason", "updated_at_ms"):
            self.assertEqual(after[field], before[field])
        self.send(action=action)
        self.assertTrue(self.control.snapshot()["paper_enabled"])

    def test_fault_after_fresh_status_never_displays_previous_enabled_state(self):
        answers = [self.runtime(NOW, m5_running=True), RuntimeError("PRIVATE_SECRET")]

        def provider(*, now_ms):
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

        self.with_provider(provider)
        first = self.send(action="u5:engine:m5")[-1]["payload"]["text"]
        second = self.send(action="u5:engine:m5")[-1]["payload"]["text"]
        self.assertIn("Tarea reportada: en marcha", first)
        self.assertIn("consulta fallida", second)
        self.assertIn("Tarea reportada: desconocida", second)
        self.assertNotIn("PRIVATE_SECRET", second)
        self.assertNotIn(b"PRIVATE_SECRET", self.path.read_bytes())

    def test_stale_provider_unknown_even_if_provider_claims_fresh(self):
        self.with_provider(
            lambda *, now_ms: self.runtime(now_ms - 30001, m5_running=True)
        )
        text = self.send(action="u5:engine:m5")[-1]["payload"]["text"]
        self.assertIn("dato vencido", text)
        self.assertIn("Configurado: desconocido", text)
        self.assertIn("Tarea reportada: desconocida", text)

    def test_runtime_source_cannot_inject_sensitive_text_or_unsupported_running(self):
        def provider(*, now_ms):
            value = self.runtime(now_ms)
            value.update(
                source="PRIVATE_TOKEN",
                environment="PRIVATE_TOKEN",
                health="PRIVATE_TOKEN",
            )
            value["flags"]["trading_enabled"] = "PRIVATE_TOKEN"
            value["motors"]["m6"].update(running=True, configured=True)
            return value

        self.with_provider(provider)
        overview_text = self.send(text="/motores")[-1]["payload"]["text"]
        self.assertNotIn("PRIVATE_TOKEN", overview_text)
        self.assertIn("Trading configurado: desconocido", overview_text)
        text = self.send(action="u5:engine:m6")[-1]["payload"]["text"]
        self.assertIn("Configurado: desconocido", text)
        self.assertIn("Tarea reportada: desconocida", text)
        self.assertNotIn(b"PRIVATE_TOKEN", self.path.read_bytes())

    def test_rest_does_not_inherit_general_capture_running(self):
        self.with_provider(lambda *, now_ms: self.runtime(now_ms))
        text = self.send(action="u5:engine:rest")[-1]["payload"]["text"]
        self.assertIn("Tarea reportada: desconocida", text)
        self.assertIn("captura general no demuestra", text)

    def test_m2_execution_flag_describes_only_exits_not_entries(self):
        self.with_provider(lambda *, now_ms: self.runtime(now_ms))
        text = self.send(action="u5:engine:m2")[-1]["payload"]["text"]
        self.assertIn("Salidas configuradas: no", text)
        self.assertIn("Entradas: sin dato expuesto", text)
        self.assertNotIn("Ejecución configurada", text)

    def test_runtime_fetch_is_outside_control_lock(self):
        def provider(*, now_ms):
            with closing(sqlite3.connect(self.path, timeout=0)) as db:
                db.execute("BEGIN IMMEDIATE")
                db.rollback()
            return self.runtime(now_ms)

        self.with_provider(provider)
        text = self.send(text="/motores")[-1]["payload"]["text"]
        self.assertIn("Actualizado:", text)

    def test_recorded_activity_renders_window_without_claiming_running(self):
        from test_motor_status_readonly import edges

        def provider(*, now_ms):
            value = self.runtime(now_ms)
            value["activity"] = activity_from_response(
                edges(now_ms, {"ofi": {"unit": "zscore", "rows_total": 12}}),
                now_ms=now_ms,
            )
            return value

        self.with_provider(provider)
        overview_text = self.send(text="/motores")[-1]["payload"]["text"]
        self.assertIn(
            "Registros de señales: M6: 0 · REST: 0 · M8: 12 · M9: 0", overview_text
        )
        self.assertIn("desde 2026-10-10 00:00 UTC hasta", overview_text)
        self.assertNotIn("24 horas", overview_text)
        self.assertIn("Cero registros no significa apagado", overview_text)
        for key, count in (("m6", 0), ("rest", 0), ("m8", 12), ("m9", 0)):
            text = self.send(action="u5:engine:" + key)[-1]["payload"]["text"]
            self.assertIn(f"Registros de señales: {key.upper()}: {count}", text)
            self.assertIn("Configurado: desconocido", text)
            self.assertIn("Tarea reportada: desconocida", text)

    def test_activity_failure_does_not_hide_current_configuration(self):
        self.with_provider(lambda *, now_ms: self.runtime(now_ms))
        text = self.send(text="/motores")[-1]["payload"]["text"]
        self.assertIn("M1: detección configurada sí", text)
        self.assertIn("Actividad registrada: desconocida", text)


if __name__ == "__main__":
    unittest.main()
