"""Fixed loopback runtime status, tested with fake HTTP and denied sockets."""

import copy
import http.client
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from motor_status_readonly import (
    MAX_BYTES,
    MOTOR_KEYS,
    RuntimeStatusReader,
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

    def test_only_two_fixed_anonymous_gets_are_available(self):
        reader, connections, hosts = self.reader(
            [
                Response(json.dumps(health()).encode()),
                Response(json.dumps(status()).encode()),
            ]
        )
        self.assertEqual(hosts, [])
        result = reader(now_ms=NOW)
        self.assertTrue(result["fresh"])
        self.assertEqual(hosts, [("127.0.0.1", 18080, 2)] * 2)
        for connection, resource in zip(connections, ("/health", "/status")):
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


if __name__ == "__main__":
    unittest.main()
