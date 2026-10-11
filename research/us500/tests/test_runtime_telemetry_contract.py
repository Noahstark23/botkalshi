import copy
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from runtime_telemetry_contract import (
    COMPONENTS,
    FLAGS,
    MAX_AGE_MS,
    MAX_GENERATION,
    MOTOR_FLAGS,
    TASK_MAX_AGE_MS,
    RuntimeTelemetry,
    best_effort,
    parse_runtime_telemetry,
)

NOW = 1_791_756_000_000
SHA = "a" * 40
INSTANCE = "b" * 32


def registry():
    return RuntimeTelemetry(source_sha=SHA, instance_id=INSTANCE, started_at_ms=NOW)


class RuntimeTelemetryTests(unittest.TestCase):
    def test_source_identity_is_strict_and_does_not_echo_bad_input(self):
        for bad in ("83075", "token-secret", None, 1, "A" * 40):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "^INVALID_SOURCE_IDENTITY$"):
                    RuntimeTelemetry(
                        source_sha=bad, instance_id=INSTANCE, started_at_ms=NOW
                    )
        with self.assertRaises(ValueError):
            RuntimeTelemetry(source_sha=SHA, instance_id="not-uuid", started_at_ms=NOW)

    def test_default_fields_unknown_not_disabled(self):
        snapshot = registry().snapshot(now_ms=NOW)
        for block in snapshot["motors"].values():
            self.assertIsNone(block["configured"])
            self.assertIsNone(block["execution_entry_configured"])
            self.assertIsNone(block["execution_sell_configured"])
        self.assertEqual(snapshot["motors"]["m6"]["execution_implementation"], "ABSENT")
        for block in snapshot["components"].values():
            self.assertIsNone(block["task_running"]["value"])
            self.assertIsNone(block["instantiated"]["value"])

    def test_configuration_allowlist_exact_booleans_and_m2_entry_sell(self):
        state = registry()
        self.assertTrue(
            state.configure(
                {
                    "TRADING_ENABLED": False,
                    "MOTOR_2_SPORTSBOOK_ENABLED": True,
                    "MOTOR_2_EXECUTION_ENABLED": True,
                    "MOTOR_2_ENTRY_EXECUTION_ENABLED": False,
                    "MOTOR_6_LINEMOVE_ENABLED": 1,
                    "ODDS_API_KEY": "secret",
                },
                now_ms=NOW,
            )
        )
        snapshot = state.snapshot(now_ms=NOW)
        self.assertFalse(snapshot["global_trading_configured"])
        self.assertTrue(snapshot["motors"]["m2"]["execution_sell_configured"])
        self.assertFalse(snapshot["motors"]["m2"]["execution_entry_configured"])
        self.assertIsNone(snapshot["motors"]["m6"]["configured"])
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertIsNone(snapshot["components"]["m2_poll"]["task_running"]["value"])

    def test_missing_new_configuration_clears_old_values(self):
        state = registry()
        state.configure({"MOTOR_1_ARBITRAGE_ENABLED": True}, now_ms=NOW)
        state.configure({}, now_ms=NOW + 1)
        self.assertIsNone(state.snapshot(now_ms=NOW + 1)["motors"]["m1"]["configured"])
        self.assertFalse(state.configure({}, now_ms=NOW))

    def test_explicit_task_probe_is_independent_of_tick(self):
        state = registry()
        generation = state.begin_component("m1_loop", now_ms=NOW)
        state.observe("m1_loop", generation, "task_running", True, now_ms=NOW)
        block = state.snapshot(now_ms=NOW)["components"]["m1_loop"]
        self.assertTrue(block["task_running"]["value"])
        self.assertIsNone(block["instantiated"]["value"])
        self.assertIsNone(block["tick_started"]["value"])
        state.observe("m1_loop", generation, "task_running", False, now_ms=NOW + 1)
        self.assertFalse(
            state.snapshot(now_ms=NOW + 1)["components"]["m1_loop"]["task_running"][
                "value"
            ]
        )

    def test_task_and_progress_expire_independently_without_inventing_off(self):
        state = registry()
        generation = state.begin_component("m1_loop", now_ms=NOW, instantiated=True)
        state.observe("m1_loop", generation, "task_running", True, now_ms=NOW)
        state.observe("m1_loop", generation, "tick_started", True, now_ms=NOW)
        state.observe("m1_loop", generation, "tick_returned", False, now_ms=NOW)
        block = state.snapshot(now_ms=NOW + TASK_MAX_AGE_MS + 1)["components"][
            "m1_loop"
        ]
        self.assertIsNone(block["task_running"]["value"])
        self.assertEqual(block["task_running"]["freshness"], "STALE")
        self.assertTrue(block["tick_started"]["value"])
        self.assertFalse(block["tick_returned"]["value"])
        block = state.snapshot(now_ms=NOW + MAX_AGE_MS + 1)["components"]["m1_loop"]
        self.assertIsNone(block["instantiated"]["value"])
        self.assertIsNone(block["tick_started"]["value"])

    def test_callbacks_never_claim_own_task_and_capture_never_updates_passengers(self):
        state = registry()
        for name in ("m6_observer", "rest_observer", "m8_observer", "m9_observer"):
            generation = state.begin_component(name, now_ms=NOW, instantiated=True)
            self.assertFalse(
                state.observe(name, generation, "task_running", True, now_ms=NOW)
            )
            self.assertTrue(
                state.observe(name, generation, "tick_started", True, now_ms=NOW)
            )
        generation = state.begin_component("capture", now_ms=NOW)
        state.observe("capture", generation, "task_running", True, now_ms=NOW)
        snapshot = state.snapshot(now_ms=NOW)
        for name in ("m6_observer", "rest_observer", "m8_observer", "m9_observer"):
            self.assertEqual(
                snapshot["components"][name]["task_running"]["freshness"],
                "NOT_APPLICABLE",
            )
        self.assertIsNone(snapshot["components"]["m1_loop"]["task_running"]["value"])

    def test_generation_blocks_retired_instance_and_resets_old_progress(self):
        state = registry()
        old = state.begin_component("m8_observer", now_ms=NOW, instantiated=True)
        state.observe("m8_observer", old, "tick_started", True, now_ms=NOW)
        new = state.begin_component("m8_observer", now_ms=NOW + 1)
        self.assertEqual(new, old + 1)
        self.assertFalse(
            state.observe("m8_observer", old, "tick_returned", True, now_ms=NOW + 2)
        )
        self.assertIsNone(
            state.snapshot(now_ms=NOW + 2)["components"]["m8_observer"]["tick_started"][
                "value"
            ]
        )

    def test_out_of_order_and_invalid_hooks_do_not_mutate(self):
        state = registry()
        generation = state.begin_component("m6_observer", now_ms=NOW + 2)
        original = state.snapshot(now_ms=NOW + 3)
        for name, token, field, value, when in (
            ("m6_observer", generation, "tick_started", True, NOW),
            ("m6_observer", True, "tick_started", True, NOW + 3),
            ("m6_observer", generation, "tick_started", False, NOW + 3),
            ("m6_observer", generation, "tick_returned", "secret", NOW + 3),
            ("m6_observer", generation, "arbitrary", True, NOW + 3),
            ("missing", generation, "tick_returned", True, NOW + 3),
        ):
            self.assertFalse(state.observe(name, token, field, value, now_ms=when))
        self.assertEqual(original, state.snapshot(now_ms=NOW + 3))

    def test_dependencies_are_independent_explicit_observations(self):
        state = registry()
        token = state.begin_component("m6_observer", now_ms=NOW)
        self.assertTrue(
            state.dependency("m6_observer", token, "odds_live", False, now_ms=NOW)
        )
        self.assertFalse(
            state.dependency("m6_observer", token, "credentials", True, now_ms=NOW)
        )
        dependencies = state.snapshot(now_ms=NOW)["components"]["m6_observer"][
            "dependencies"
        ]
        self.assertFalse(dependencies["odds_live"]["value"])
        self.assertIsNone(dependencies["m2_poll"]["value"])

    def test_snapshot_is_detached_and_does_not_refresh_hooks(self):
        state = registry()
        token = state.begin_component("m1_loop", now_ms=NOW, instantiated=True)
        state.observe("m1_loop", token, "task_running", True, now_ms=NOW)
        output = state.snapshot(now_ms=NOW)
        output["components"]["m1_loop"]["task_running"]["value"] = False
        self.assertTrue(
            state.snapshot(now_ms=NOW)["components"]["m1_loop"]["task_running"]["value"]
        )
        self.assertIsNone(
            state.snapshot(now_ms=NOW + TASK_MAX_AGE_MS + 1)["components"]["m1_loop"][
                "task_running"
            ]["value"]
        )

    def test_process_restart_does_not_inherit_runtime_state(self):
        state = registry()
        state.configure({key: True for key in FLAGS}, now_ms=NOW)
        state.begin_component("m1_loop", now_ms=NOW, instantiated=True)
        fresh = RuntimeTelemetry(
            source_sha=SHA, instance_id="c" * 32, started_at_ms=NOW + 100
        )
        self.assertNotEqual(
            state.snapshot(now_ms=NOW + 100)["source"]["instance_id"],
            fresh.snapshot(now_ms=NOW + 100)["source"]["instance_id"],
        )
        self.assertIsNone(
            fresh.snapshot(now_ms=NOW + 100)["motors"]["m1"]["configured"]
        )

    def test_bounded_concurrent_updates(self):
        state = registry()
        token = state.begin_component("m1_loop", now_ms=NOW)

        def update(_):
            return state.observe("m1_loop", token, "tick_started", True, now_ms=NOW)

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(pool.map(update, range(100))))
        self.assertEqual(len(state._components), len(COMPONENTS))
        self.assertLessEqual(len(state._components["m1_loop"].observations), 4)
        state._components["m1_loop"].generation = MAX_GENERATION
        self.assertIsNone(state.begin_component("m1_loop", now_ms=NOW))

    def test_telemetry_failure_boundary_no_raw_error_or_network(self):
        with patch("socket.socket", side_effect=AssertionError("network forbidden")):
            state = registry()
            self.assertTrue(
                parse_runtime_telemetry(state.snapshot(now_ms=NOW), now_ms=NOW)[
                    "available"
                ]
            )

            def failing_hook():
                raise RuntimeError("secret raw payload")

            self.assertIsNone(best_effort(failing_hook))


class RuntimeTelemetryParserTests(unittest.TestCase):
    def setUp(self):
        self.state = registry()
        self.state.configure({key: False for key in FLAGS}, now_ms=NOW)
        token = self.state.begin_component("m1_loop", now_ms=NOW, instantiated=True)
        self.state.observe("m1_loop", token, "task_running", True, now_ms=NOW)
        self.value = self.state.snapshot(now_ms=NOW)

    def parse(self, value=None, now=NOW):
        return parse_runtime_telemetry(
            self.value if value is None else value, now_ms=now
        )

    def test_round_trip_and_no_aliasing(self):
        result = self.parse(json.loads(json.dumps(self.value)))
        self.assertTrue(result["available"])
        self.assertFalse(result["motors"]["m1"]["configured"])
        self.assertTrue(result["components"]["m1_loop"]["task_running"]["value"])
        result["source"]["commit_sha"] = "c" * 40
        self.assertEqual(self.value["source"]["commit_sha"], SHA)

    def test_missing_legacy_endpoint_is_not_installed(self):
        parsed = parse_runtime_telemetry(None, now_ms=NOW)
        self.assertFalse(parsed["available"])
        self.assertEqual(parsed["reason"], "NOT_INSTALLED")
        self.assertIsNone(parsed["motors"]["m1"]["configured"])

    def test_stale_and_future_payload_clear_every_fact(self):
        self.assertEqual(self.parse(now=NOW + TASK_MAX_AGE_MS + 1)["reason"], "STALE")
        self.assertEqual(self.parse(now=NOW - 5001)["reason"], "INVALID")
        self.assertTrue(self.parse(now=NOW - 5000)["available"])
        stale = self.parse(now=NOW + TASK_MAX_AGE_MS + 1)
        self.assertIsNone(stale["motors"]["m1"]["configured"])
        self.assertIsNone(stale["source"])

    def test_cached_observation_ttl_revalidated(self):
        value = self.state.snapshot(now_ms=NOW + TASK_MAX_AGE_MS)
        result = self.parse(value, now=NOW + TASK_MAX_AGE_MS + 1)
        self.assertTrue(result["available"])
        self.assertIsNone(result["components"]["m1_loop"]["task_running"]["value"])
        self.assertEqual(
            result["components"]["m1_loop"]["task_running"]["freshness"], "STALE"
        )

    def test_source_snapshot_cannot_relabel_stale_true_as_fresh(self):
        value = self.state.snapshot(now_ms=NOW + TASK_MAX_AGE_MS + 1)
        value["components"]["m1_loop"]["task_running"].update(
            value=True, freshness="FRESH"
        )
        self.assertEqual(
            self.parse(value, now=NOW + TASK_MAX_AGE_MS + 1)["reason"], "INVALID"
        )

    def test_malformed_variants(self):
        variants = []
        for field, bad in (
            ("commit_sha", "83075"),
            ("instance_id", "secret"),
            ("sampled_at_ms", True),
            ("started_at_ms", NOW + 1),
        ):
            value = copy.deepcopy(self.value)
            value["source"][field] = bad
            variants.append(value)
        for bad in (0, 1, "false", [], {}, 0.0):
            value = copy.deepcopy(self.value)
            value["motors"]["m2"]["configured"] = bad
            variants.append(value)
        value = copy.deepcopy(self.value)
        value["motors"]["m6"]["execution_entry_configured"] = False
        variants.append(value)
        value = copy.deepcopy(self.value)
        value["components"]["m8_observer"]["task_running"] = {
            "value": False,
            "observed_at_ms": NOW,
            "freshness": "FRESH",
        }
        variants.append(value)
        value = copy.deepcopy(self.value)
        value["components"]["m1_loop"]["generation"] = 0
        variants.append(value)
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                self.assertEqual(self.parse(value)["reason"], "INVALID")

    def test_sensitive_unknowns_and_large_nested_payloads_rejected_without_echo(self):
        value = copy.deepcopy(self.value)
        value["credentials"] = "SECRET"
        result = self.parse(value)
        self.assertEqual(result["reason"], "INVALID")
        self.assertNotIn("SECRET", json.dumps(result))
        for value in (
            {"huge": "x" * 65_537},
            {str(i): None for i in range(65)},
            {"obj": object()},
            {"float": float("nan")},
        ):
            self.assertEqual(self.parse(value)["reason"], "INVALID")
        value = {}
        value["self"] = value
        self.assertEqual(self.parse(value)["reason"], "INVALID")

    def test_unknown_fields_stay_null_and_no_fake_hook(self):
        value = registry().snapshot(now_ms=NOW)
        result = self.parse(value)
        self.assertTrue(result["available"])
        self.assertEqual(set(result["motors"]), set(MOTOR_FLAGS))
        for motor in result["motors"].values():
            self.assertIsNone(motor["configured"])
        value["components"]["m1_loop"]["task_running"]["value"] = True
        self.assertEqual(self.parse(value)["reason"], "INVALID")


if __name__ == "__main__":
    unittest.main()
