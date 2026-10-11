"""Fixed loopback runtime status, tested with fake HTTP and denied sockets."""

import copy
import http.client
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from motor_status_readonly import (
    ACTIVITY_KINDS,
    MAX_ACTIVITY_COUNT,
    MAX_BYTES,
    MOTOR_KEYS,
    RuntimeStatusReader,
    activity_from_response,
    sanitize_activity,
    sanitize_snapshot,
    status_from_responses,
)

NOW = 1791744840000


def health(at=NOW):
    return {
        "status": "healthy",
        "timestamp": datetime.fromtimestamp(at / 1000, timezone.utc).isoformat(),
    }


def status():
    return {
        "bot": {
            "is_paused": False,
            "capture_running": True,
            "ws_connected": True,
            "motor5": {"enabled": False, "task_running": False},
        },
        "config": {
            "environment": "production",
            "trading_enabled": False,
            "motors_enabled": {
                "motor_1_arbitrage": True,
                "motor_2_sportsbook": False,
                "motor_2_execution": False,
                "motor_3_clv": False,
                "motor_3_execution": False,
                "motor_5_mm": False,
                "motor_5_execution": False,
            },
        },
        "capital": {"is_paused": True},
    }


def edges(at=NOW, by_kind=None):
    generated = datetime.fromtimestamp(at / 1000, timezone.utc)
    return {
        "days_requested": 1,
        "cutoff": (generated - timedelta(days=1)).strftime("%Y-%m-%d"),
        "generated_at": generated.isoformat(),
        "by_kind": by_kind if by_kind is not None else {},
    }


class Response:
    def __init__(self, raw, status_code=200):
        self.raw, self.status = raw, status_code
        self.read_sizes = []

    def read1(self, size):
        self.read_sizes.append(size)
        chunk, self.raw = self.raw[:size], self.raw[size:]
        return chunk


class Connection:
    sock = None

    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, method, path, **kwargs):
        self.requests.append((method, path, kwargs))

    def getresponse(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def close(self):
        self.closed = True


class RuntimeStatusTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN"))
        guard.start()
        self.addCleanup(guard.stop)

    def reader(self, responses):
        connections = [Connection(response) for response in responses]
        hosts = []

        def factory(host, port, *, timeout):
            hosts.append((host, port, timeout))
            return connections[len(hosts) - 1]

        return RuntimeStatusReader(connection_factory=factory), connections, hosts

    def test_only_three_fixed_anonymous_gets_are_available(self):
        reader, connections, hosts = self.reader(
            [
                Response(json.dumps(health()).encode()),
                Response(json.dumps(status()).encode()),
                Response(json.dumps(edges()).encode()),
            ]
        )
        self.assertEqual(hosts, [])
        result = reader(now_ms=NOW)
        self.assertTrue(result["fresh"])
        self.assertEqual(hosts, [("127.0.0.1", 18080, 2)] * 3)
        for connection, resource in zip(
            connections, ("/health", "/status", "/stats/edges?days=1")
        ):
            self.assertEqual(
                connection.requests,
                [("GET", resource, {"headers": {"Accept": "application/json"}})],
            )
            self.assertTrue(connection.closed)

    def test_configuration_and_running_are_separate(self):
        result = status_from_responses(health(), status(), now_ms=NOW)
        self.assertIs(result["motors"]["m1"]["configured"], True)
        self.assertIsNone(result["motors"]["m1"]["running"])
        self.assertIsNone(result["motors"]["m1"]["execution_configured"])
        for key in ("m2", "m3", "m5"):
            self.assertIs(result["motors"][key]["configured"], False)
            self.assertIs(result["motors"][key]["execution_configured"], False)
        self.assertIs(result["motors"]["m5"]["running"], False)
        for key in ("m2", "m3", "m6", "m8", "m9", "rest"):
            self.assertIsNone(result["motors"][key]["running"])
        self.assertIs(result["flags"]["capital_paused"], True)
        self.assertIs(result["flags"]["trading_enabled"], False)
        self.assertIs(result["flags"]["capture_running"], True)

    def test_strict_booleans_do_not_treat_strings_numbers_or_objects_as_flags(self):
        for raw in ("true", "false", 0, 1, None, {}, []):
            data = status()
            data["config"]["trading_enabled"] = raw
            data["bot"]["motor5"]["task_running"] = raw
            data["config"]["motors_enabled"]["motor_2_execution"] = raw
            result = status_from_responses(health(), data, now_ms=NOW)
            with self.subTest(value=raw):
                self.assertIsNone(result["flags"]["trading_enabled"])
                self.assertIsNone(result["motors"]["m5"]["running"])
                self.assertIsNone(result["motors"]["m2"]["execution_configured"])

    def test_sensitive_extra_fields_and_arbitrary_labels_never_escape(self):
        data = status()
        data.update(token="SECRET_MARKER", balance=10000, errors=["SECRET_MARKER"])
        data["config"].update(api_key="SECRET_MARKER", environment="SECRET_MARKER")
        data["capital"].update(cash_usd="SECRET_MARKER")
        result = status_from_responses(
            {**health(), "details": "SECRET_MARKER"}, data, now_ms=NOW
        )
        serialized = json.dumps(result)
        for forbidden in ("SECRET_MARKER", "balance", "cash_usd", "api_key"):
            self.assertNotIn(forbidden, serialized)
        self.assertIsNone(result["environment"])

    def test_stale_or_future_health_discards_all_flags(self):
        for at, reason in ((NOW - 30001, "STALE"), (NOW + 5001, "INVALID")):
            result = status_from_responses(health(at), status(), now_ms=NOW)
            self.assertFalse(result["fresh"])
            self.assertEqual(result["reason"], reason)
            self.assertTrue(all(value is None for value in result["flags"].values()))
            self.assertTrue(
                all(
                    value is None
                    for motor in result["motors"].values()
                    for value in motor.values()
                )
            )
        self.assertTrue(
            status_from_responses(health(NOW - 30000), status(), now_ms=NOW)["fresh"]
        )
        self.assertTrue(
            status_from_responses(health(NOW + 1000), status(), now_ms=NOW)["fresh"]
        )

    def test_missing_invalid_or_unhealthy_health_is_unknown(self):
        for item in (
            None,
            [],
            {},
            {"status": "unhealthy", "timestamp": health()["timestamp"]},
            {"status": "healthy", "timestamp": "2026-10-11T00:00:00"},
            {"status": "healthy", "timestamp": "SECRET_MARKER"},
        ):
            result = status_from_responses(item, status(), now_ms=NOW)
            self.assertFalse(result["fresh"])
            self.assertIsNone(result["flags"]["trading_enabled"])
            self.assertNotIn("SECRET_MARKER", json.dumps(result))

    def test_partial_status_keeps_absent_signals_unknown(self):
        for item in ({}, {"bot": []}, {"config": "SECRET_MARKER"}, {"capital": 12}):
            result = status_from_responses(health(), item, now_ms=NOW)
            self.assertTrue(result["fresh"])
            self.assertTrue(all(value is None for value in result["flags"].values()))
            self.assertTrue(
                all(
                    value is None
                    for motor in result["motors"].values()
                    for value in motor.values()
                )
            )

    def test_injected_snapshot_cannot_fabricate_unexposed_task_signals(self):
        source = status_from_responses(health(), status(), now_ms=NOW)
        for key in MOTOR_KEYS:
            source["motors"][key]["running"] = True
        source["real_controls_available"] = True
        result = sanitize_snapshot(source, now_ms=NOW)
        self.assertIs(result["motors"]["m5"]["running"], True)
        self.assertTrue(
            all(
                result["motors"][key]["running"] is None
                for key in MOTOR_KEYS
                if key != "m5"
            )
        )
        self.assertFalse(result["real_controls_available"])

    def test_url_and_resource_overrides_are_not_supported(self):
        with self.assertRaises(TypeError):
            RuntimeStatusReader(url="https://example.com")
        reader, _, hosts = self.reader([])
        for resource in (
            "http://example.com",
            "/orders",
            "/status?token=secret",
            "//example.com",
            "/stats/edges?days=30",
            "/stats/daily?days=1",
        ):
            with self.assertRaisesRegex(ValueError, "RUNTIME_RESOURCE_BLOCKED"):
                reader._get(resource)
        self.assertEqual(hosts, [])

    def test_non_200_and_redirects_are_unknown_without_following(self):
        for code in (301, 302, 404, 500):
            reader, connections, hosts = self.reader([Response(b"SECRET_MARKER", code)])
            result = reader(now_ms=NOW)
            self.assertFalse(result["fresh"])
            self.assertEqual(len(hosts), 1)
            self.assertTrue(connections[0].closed)
            self.assertNotIn("SECRET_MARKER", json.dumps(result))

    def test_response_size_json_and_protocol_errors_are_sanitized(self):
        for raw in (
            b"x" * (MAX_BYTES + 1),
            b'{"status":"healthy","status":"other"}',
            b'{"status":NaN}',
            b"not json",
            b"\xff",
        ):
            response = Response(raw)
            reader, connections, _ = self.reader([response])
            result = reader(now_ms=NOW)
            self.assertFalse(result["fresh"])
            self.assertTrue(connections[0].closed)
            self.assertTrue(all(size <= 16384 for size in response.read_sizes))
        reader, _, _ = self.reader([http.client.BadStatusLine("SECRET_MARKER")])
        self.assertNotIn("SECRET_MARKER", json.dumps(reader(now_ms=NOW)))

    def test_body_deadline_stops_slow_response(self):
        reader, connections, _ = self.reader([Response(b"{}")])
        with patch("motor_status_readonly.time.monotonic", side_effect=[0, 3]):
            result = reader(now_ms=NOW)
        self.assertFalse(result["fresh"])
        self.assertEqual(connections[0].response.read_sizes, [])
        self.assertTrue(connections[0].closed)

    def test_new_failure_cannot_reuse_previous_fresh_snapshot(self):
        reader, _, _ = self.reader(
            [
                Response(json.dumps(health()).encode()),
                Response(json.dumps(status()).encode()),
                OSError("SECRET_MARKER"),
            ]
        )
        self.assertTrue(reader(now_ms=NOW)["fresh"])
        result = reader(now_ms=NOW + 1000)
        self.assertFalse(result["fresh"])
        self.assertIsNone(result["motors"]["m1"]["configured"])

    def test_inputs_are_not_mutated(self):
        raw_health, raw_status = health(), status()
        before = copy.deepcopy((raw_health, raw_status))
        status_from_responses(raw_health, raw_status, now_ms=NOW)
        self.assertEqual((raw_health, raw_status), before)

    def test_complete_empty_activity_response_means_zero_rows_not_disabled(self):
        result = activity_from_response(edges(), now_ms=NOW)
        self.assertTrue(result["available"])
        self.assertEqual(result["counts"], {key: 0 for key in ACTIVITY_KINDS})
        source = status_from_responses(health(), status(), now_ms=NOW)
        source["activity"] = result
        cleaned = sanitize_snapshot(source, now_ms=NOW)
        self.assertEqual(cleaned["activity"], result)
        self.assertIsNone(cleaned["motors"]["m6"]["configured"])
        self.assertIsNone(cleaned["motors"]["m8"]["running"])

    def test_activity_only_counts_fixed_signal_kinds_and_checks_units(self):
        kinds = {
            kind: {
                "unit": unit,
                "rows_total": index + 1,
                "value_avg": "PRIVATE_SECRET",
                "value_max": "PRIVATE_SECRET",
            }
            for index, (kind, unit) in enumerate(ACTIVITY_KINDS.values())
        }
        kinds["spillover_exec"] = {"unit": "cents", "rows_total": 999}
        kinds["unrecognized"] = {"unit": "PRIVATE_SECRET", "rows_total": 999}
        result = activity_from_response(edges(by_kind=kinds), now_ms=NOW)
        self.assertEqual(result["counts"], {"m6": 1, "rest": 2, "m8": 3, "m9": 4})
        self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
        for kind, _ in ACTIVITY_KINDS.values():
            invalid = copy.deepcopy(kinds)
            invalid[kind]["unit"] = "wrong"
            self.assertFalse(
                activity_from_response(edges(by_kind=invalid), now_ms=NOW)["available"]
            )

    def test_activity_count_types_bounds_and_incomplete_shapes_fail_closed(self):
        for count in (True, False, -1, 1.0, "12", None, MAX_ACTIVITY_COUNT + 1, []):
            result = activity_from_response(
                edges(by_kind={"ofi": {"unit": "zscore", "rows_total": count}}),
                now_ms=NOW,
            )
            self.assertFalse(result["available"])
            self.assertIsNone(result["counts"]["m8"])
        for value in (
            None,
            {},
            {**edges(), "by_kind": None},
            edges(by_kind={"ofi": {"rows_total": 4}}),
            edges(by_kind={"ofi": {"unit": "zscore"}}),
        ):
            self.assertFalse(activity_from_response(value, now_ms=NOW)["available"])

    def test_activity_window_uses_previous_utc_date_not_rolling_24_hours(self):
        result = activity_from_response(edges(), now_ms=NOW)
        self.assertEqual(result["cutoff_utc"], "2026-10-10")
        self.assertNotEqual(
            NOW - 24 * 60 * 60 * 1000,
            int(
                datetime.fromisoformat(
                    result["cutoff_utc"] + "T00:00:00+00:00"
                ).timestamp()
                * 1000
            ),
        )
        for field, value in (
            ("days_requested", True),
            ("days_requested", 2),
            ("cutoff", "2026-10-11"),
            ("cutoff", "20261010"),
            ("cutoff", "PRIVATE_SECRET"),
            ("generated_at", "2026-10-11T00:00:00"),
        ):
            payload = edges()
            payload[field] = value
            self.assertFalse(activity_from_response(payload, now_ms=NOW)["available"])

    def test_activity_freshness_future_and_missing_counts_revalidated(self):
        value = activity_from_response(edges(), now_ms=NOW)
        self.assertTrue(sanitize_activity(value, now_ms=NOW + 300000)["available"])
        stale = sanitize_activity(value, now_ms=NOW + 300001)
        self.assertFalse(stale["available"])
        self.assertEqual(stale["reason"], "STALE")
        self.assertFalse(
            activity_from_response(edges(NOW + 5001), now_ms=NOW)["available"]
        )
        del value["counts"]["m6"]
        self.assertFalse(sanitize_activity(value, now_ms=NOW)["available"])

    def test_bad_activity_never_erases_valid_runtime_status(self):
        reader, _, hosts = self.reader(
            [
                Response(json.dumps(health()).encode()),
                Response(json.dumps(status()).encode()),
                Response(json.dumps({"days_requested": 1}).encode()),
            ]
        )
        result = reader(now_ms=NOW)
        self.assertTrue(result["fresh"])
        self.assertIs(result["motors"]["m1"]["configured"], True)
        self.assertFalse(result["activity"]["available"])
        self.assertEqual(len(hosts), 3)

    def test_activity_is_not_queried_when_status_health_is_invalid(self):
        reader, _, hosts = self.reader(
            [
                Response(json.dumps(health(NOW - 30001)).encode()),
                Response(json.dumps(status()).encode()),
            ]
        )
        self.assertFalse(reader(now_ms=NOW)["fresh"])
        self.assertEqual(len(hosts), 2)

    def test_activity_success_cache_and_failed_refresh_have_300_second_budget(self):
        reader = RuntimeStatusReader()
        current = {"at": NOW, "fail": False}
        activity_requests = []

        def get(path):
            if path == "/health":
                return health(current["at"])
            if path == "/status":
                return status()
            self.assertEqual(path, "/stats/edges?days=1")
            activity_requests.append(current["at"])
            if current["fail"]:
                raise OSError("PRIVATE_SECRET")
            return edges(current["at"], {"ofi": {"unit": "zscore", "rows_total": 7}})

        with patch.object(reader, "_get", side_effect=get):
            with patch("motor_status_readonly.time.monotonic", return_value=100):
                first = reader(now_ms=NOW)
            self.assertEqual(first["activity"]["counts"]["m8"], 7)
            current["at"] = NOW + 299000
            with patch("motor_status_readonly.time.monotonic", return_value=399):
                cached = reader(now_ms=current["at"])
            self.assertEqual(cached["activity"]["age_ms"], 299000)
            self.assertEqual(len(activity_requests), 1)
            current.update(at=NOW + 300000, fail=True)
            with patch("motor_status_readonly.time.monotonic", return_value=400):
                failed = reader(now_ms=current["at"])
            self.assertTrue(failed["fresh"])
            self.assertFalse(failed["activity"]["available"])
            self.assertIsNone(failed["activity"]["counts"]["m8"])
            current.update(at=NOW + 599000, fail=False)
            with patch("motor_status_readonly.time.monotonic", return_value=699):
                cooldown = reader(now_ms=current["at"])
            self.assertFalse(cooldown["activity"]["available"])
            self.assertEqual(len(activity_requests), 2)
            current["at"] = NOW + 600000
            with patch("motor_status_readonly.time.monotonic", return_value=700):
                recovered = reader(now_ms=current["at"])
            self.assertTrue(recovered["activity"]["available"])
            self.assertEqual(len(activity_requests), 3)

    def test_wall_clock_jump_cannot_show_old_counts_inside_monotonic_cache(self):
        reader = RuntimeStatusReader()
        with patch.object(reader, "_get", return_value=edges()) as get:
            with patch("motor_status_readonly.time.monotonic", return_value=1):
                self.assertTrue(reader._activity(NOW)["available"])
            with patch("motor_status_readonly.time.monotonic", return_value=2):
                self.assertFalse(reader._activity(NOW + 300001)["available"])
            self.assertEqual(get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
