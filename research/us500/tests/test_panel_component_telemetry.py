"""Optional runtime telemetry never grants controls or revives stale facts."""

import copy
import unittest

from test_motor_status_readonly import NOW, health, status

from bot_motor_panel import disconnected_detail, overview
from motor_status_readonly import sanitize_snapshot, status_from_responses
from runtime_telemetry_contract import RuntimeTelemetry


def instrumented():
    registry = RuntimeTelemetry(
        source_sha="a" * 40, instance_id="b" * 32, started_at_ms=NOW - 1000
    )
    registry.configure({"MOTOR_6_LINEMOVE_ENABLED": True}, now_ms=NOW)
    generation = registry.begin_component("m6_observer", now_ms=NOW)
    registry.observe("m6_observer", generation, "tick_returned", True, now_ms=NOW)
    body = status()
    body["motor_telemetry"] = registry.snapshot(now_ms=NOW)
    return body


class ComponentPanelTests(unittest.TestCase):
    def test_old_runtime_remains_usable_and_explicitly_not_instrumented(self):
        snapshot = status_from_responses(health(), status(), now_ms=NOW)
        self.assertTrue(snapshot["fresh"])
        self.assertEqual(snapshot["telemetry_reason"], "NOT_INSTALLED")
        text, _ = disconnected_detail("m6", runtime=snapshot, now_ms=NOW)
        self.assertIn("todavía no publica", text)
        self.assertIn("Tarea reportada: desconocida", text)

    def test_connected_callback_facts_do_not_invent_an_independent_task(self):
        snapshot = status_from_responses(health(), instrumented(), now_ms=NOW)
        self.assertEqual(snapshot["telemetry_reason"], "FRESH")
        text, buttons = disconnected_detail("m6", runtime=snapshot, now_ms=NOW)
        self.assertIn("callback dependiente, sin tarea propia", text)
        self.assertIn("Último retorno: sí", text)
        self.assertIn("no prueban una operación", text)
        self.assertIsNone(snapshot["motors"]["m6"]["running"])
        self.assertFalse(snapshot["real_controls_available"])
        self.assertEqual(len(buttons), 2)

    def test_stale_telemetry_does_not_erase_fresh_legacy_health(self):
        body = instrumented()
        snapshot = status_from_responses(health(NOW + 31000), body, now_ms=NOW + 31000)
        self.assertTrue(snapshot["fresh"])
        self.assertEqual(snapshot["telemetry_reason"], "STALE")
        self.assertIsNone(snapshot["telemetry"])
        text, _ = disconnected_detail("m6", runtime=snapshot, now_ms=NOW + 31000)
        self.assertIn("vencida", text)
        self.assertNotIn("Último retorno: sí", text)

    def test_malformed_optional_block_never_leaks_text_or_disables_legacy(self):
        body = instrumented()
        body["motor_telemetry"]["unexpected"] = "SENSITIVE_SENTINEL"
        snapshot = status_from_responses(health(), body, now_ms=NOW)
        self.assertTrue(snapshot["fresh"])
        self.assertEqual(snapshot["telemetry_reason"], "INVALID")
        text, _ = overview(False, runtime=snapshot, now_ms=NOW)
        self.assertIn("rechazada", text)
        self.assertNotIn("SENSITIVE_SENTINEL", str(snapshot) + text)

    def test_returned_snapshot_is_detached_and_revalidated(self):
        body = instrumented()
        snapshot = status_from_responses(health(), body, now_ms=NOW)
        before = copy.deepcopy(snapshot)
        body["motor_telemetry"]["motors"]["m6"]["configured"] = False
        self.assertEqual(snapshot, before)
        late = sanitize_snapshot(snapshot, now_ms=NOW + 31000)
        self.assertFalse(late["fresh"])
        self.assertIsNone(late["telemetry"])

    def test_invalid_source_or_string_boolean_cannot_claim_running(self):
        for change in ("identity", "boolean"):
            with self.subTest(change=change):
                body = instrumented()
                block = body["motor_telemetry"]
                if change == "identity":
                    block["source"]["commit_sha"] = "short"
                else:
                    block["motors"]["m6"]["configured"] = "true"
                snapshot = status_from_responses(health(), body, now_ms=NOW)
                self.assertEqual(snapshot["telemetry_reason"], "INVALID")
                self.assertIsNone(snapshot["motors"]["m6"]["configured"])


if __name__ == "__main__":
    unittest.main()
