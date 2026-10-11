"""Fixtures fabricadas para validar formato observado, nunca observaciones reales."""

import copy
import fcntl
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import example_session
import us500_session as session
from us500_engine import DataError, body_digest
from us500_runner import build_plan, replay, validate_session


def observed_shape_fixture(count=4):
    """Datos de test inventados con claim observed; origen explícitamente no autenticado."""
    example = example_session.session(environment="public")
    snapshots, costs, previous = [], [], None
    for frame in example["frames"][:count]:
        snapshot = copy.deepcopy(frame["snapshot"])
        snapshot["synthetic"] = False
        snapshot["source_authenticity_verified"] = False
        market = snapshot["observations"]["market"]["body"]["market"]
        market.update(
            market_id="test-fixture-us500",
            market_version=1,
            exchange_index=1,
            asset_class="Indices",
        )
        snapshot["observations"]["market"]["canonical_body_sha256"] = body_digest(
            {"market": market}
        )
        snapshots.append(snapshot)
        at = frame["at_ms"]
        costs.append(
            {
                "from_ms": previous if previous is not None else at,
                "at_ms": at,
                "funding_cashflow_usd": "0",
                "infrastructure_cost_usd": "0.001",
                "provenance_note": "Supuesto explícito de test inventado; no extracto de cuenta.",
            }
        )
        previous = at
    return {
        "snapshots": snapshots,
        "ledger_config": example["ledger"],
        "strategy": example["strategy"],
        "interval_costs": costs,
        "run_id": "observed-shape-fixture",
    }


def change_market(snapshot, **changes):
    observation = snapshot["observations"]["market"]
    observation["body"]["market"].update(changes)
    observation["canonical_body_sha256"] = body_digest(observation["body"])


def shift_snapshot(snapshot, shift):
    for observation in snapshot["observations"].values():
        for key in ("requested_at_ms", "received_at_ms"):
            observation[key] += shift
    market = snapshot["observations"]["market"]["body"]["market"]
    for name in ("reference_price", "liquidation_mark_price"):
        market[name]["ts_ms"] += shift
    change_market(snapshot)


class Response:
    def __init__(self, body, status=200):
        self.body, self.status = json.dumps(body).encode(), status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size):
        return self.body[:size]


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError("REQUEST_NOT_PLANNED")
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def transport_and_clock(snapshots):
    responses, times = [], []
    for snapshot in snapshots:
        for name in ("market", "orderbook"):
            observation = snapshot["observations"][name]
            responses.append(Response(observation["body"]))
            times.extend(
                [observation["requested_at_ms"], observation["received_at_ms"]]
            )
    clock = iter(times)
    return Transport(responses), lambda: next(clock)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        guard = patch("socket.socket", side_effect=AssertionError("NETWORK_FORBIDDEN"))
        guard.start()
        self.addCleanup(guard.stop)


class AssemblyTests(OfflineTests):
    def test_observed_shape_fixture_builds_compatible_paper_session_without_validation_claim(
        self,
    ):
        result = session.build_session(**observed_shape_fixture())
        validate_session(result)
        _, report = build_plan(result)
        self.assertFalse(result["source_authenticity_verified"])
        self.assertFalse(result["strategy_validated"])
        self.assertFalse(result["assembly"]["synthetic_input"])
        self.assertEqual(
            result["assembly"]["observation_claim"], "observed_unverified_origin"
        )
        self.assertFalse(report["strategy_validated"])
        self.assertEqual(report["actual_orders"], 0)
        self.assertEqual(len(report["frames"]), 4)
        self.assertTrue(all(not f["snapshot"]["synthetic"] for f in result["frames"]))

    def test_inputs_are_copied_and_immutable_metadata_is_pinned(self):
        package = observed_shape_fixture()
        built = session.build_session(**package)
        before = body_digest(built)
        package["strategy"]["quantity"] = "999"
        package["ledger_config"]["max_notional_usd"] = "999"
        package["interval_costs"][0]["funding_cashflow_usd"] = "99"
        change_market(package["snapshots"][0], market_version=99)
        self.assertEqual(body_digest(built), before)
        metadata = built["assembly"]["instrument_metadata"]
        self.assertEqual(
            body_digest(metadata), built["assembly"]["instrument_metadata_sha256"]
        )

    def test_unverified_snapshot_cannot_claim_authenticity_or_strategy_validation(self):
        for key in (
            "source_authenticity_verified",
            "strategy_validated",
            "execution_available",
        ):
            package = observed_shape_fixture(1)
            package["snapshots"][0][key] = True
            with (
                self.subTest(key=key),
                self.assertRaisesRegex(
                    DataError, "UNSUPPORTED_SNAPSHOT_AUTHORITY_CLAIM"
                ),
            ):
                session.build_session(**package)

    def test_explicit_interval_cost_evidence_survives_in_each_frame(self):
        package = observed_shape_fixture()
        package["interval_costs"][2]["funding_cashflow_usd"] = "-0.004"
        result = session.build_session(**package)
        frame = result["frames"][2]
        self.assertEqual(frame["funding_cashflow_usd"], "-0.004")
        self.assertEqual(
            frame["snapshot"]["session_interval_cost_evidence"],
            package["interval_costs"][2],
        )

    def test_missing_zero_cost_or_provenance_is_never_inferred(self):
        for key in session.COST_FIELDS:
            package = observed_shape_fixture()
            del package["interval_costs"][0][key]
            with self.subTest(key=key), self.assertRaises(DataError):
                session.build_session(**package)
        package = observed_shape_fixture()
        package["interval_costs"] = package["interval_costs"][:-1]
        with self.assertRaises(DataError):
            session.build_session(**package)

    def test_cost_interval_time_and_money_validation(self):
        for changes in (
            {"from_ms": example_session.NOW - 1},
            {"at_ms": example_session.NOW + 1},
            {"funding_cashflow_usd": "1"},
            {"infrastructure_cost_usd": "-1"},
            {"funding_cashflow_usd": 0.0},
            {"provenance_note": ""},
        ):
            package = observed_shape_fixture()
            package["interval_costs"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(DataError):
                session.build_session(**package)

    def test_long_gap_is_rejected_before_paper_execution(self):
        package = observed_shape_fixture(2)
        shift_snapshot(package["snapshots"][1], 10000)
        package["interval_costs"][1]["at_ms"] += 10000
        with self.assertRaisesRegex(DataError, "SESSION_OBSERVATION_GAP"):
            session.build_session(**package)

    def test_duplicate_reversed_and_overlapping_capture_times_are_rejected(self):
        for shift in (-1000, -2000, -990):
            package = observed_shape_fixture(2)
            shift_snapshot(package["snapshots"][1], shift)
            package["interval_costs"][1]["at_ms"] += shift
            with self.subTest(shift=shift), self.assertRaises(DataError):
                session.build_session(**package)

    def test_synthetic_and_observed_claims_must_not_mix(self):
        package = observed_shape_fixture(2)
        package["snapshots"][1]["synthetic"] = True
        with self.assertRaisesRegex(DataError, "MIXED_SYNTHETIC"):
            session.build_session(**package)
        for snapshot in package["snapshots"]:
            snapshot["synthetic"] = True
        self.assertTrue(session.build_session(**package)["assembly"]["synthetic_input"])

    def test_changing_market_identity_or_version_is_rejected(self):
        for field, value in (
            ("market_id", "another-id"),
            ("market_version", 2),
            ("exchange_index", 2),
            ("contract_size", "0.01"),
            ("underlying_multiplier", "2"),
        ):
            package = observed_shape_fixture(2)
            change_market(package["snapshots"][1], **{field: value})
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(DataError, "INSTRUMENT_METADATA_CHANGED"),
            ):
                session.build_session(**package)

    def test_missing_mark_metadata_hash_or_book_is_rejected(self):
        for missing in (
            "market_id",
            "market_version",
            "exchange_index",
            "asset_class",
            "liquidation_mark_price",
        ):
            package = observed_shape_fixture(1)
            del package["snapshots"][0]["observations"]["market"]["body"]["market"][
                missing
            ]
            change_market(package["snapshots"][0])
            with self.subTest(missing=missing), self.assertRaises(DataError):
                session.build_session(**package)
        package = observed_shape_fixture(1)
        del package["snapshots"][0]["observations"]["market"]["canonical_body_sha256"]
        with self.assertRaises(DataError):
            session.build_session(**package)
        package = observed_shape_fixture(1)
        del package["snapshots"][0]["observations"]["orderbook"]
        package["snapshots"][0]["observations"]["candles"] = {"price": "13.5"}
        with self.assertRaises(DataError):
            session.build_session(**package)

    def test_stale_future_or_extreme_mark_is_rejected(self):
        for mark in (
            {"price": "13", "ts_ms": example_session.NOW - 5001},
            {"price": "13", "ts_ms": example_session.NOW + 1},
            {"price": "922337203685477.5807", "ts_ms": example_session.NOW},
        ):
            package = observed_shape_fixture(1)
            change_market(package["snapshots"][0], liquidation_mark_price=mark)
            with self.subTest(mark=mark), self.assertRaises(DataError):
                session.build_session(**package)

    def test_closed_capture_is_rejected_without_fabricating_liquidity(self):
        package = observed_shape_fixture(1)
        change_market(package["snapshots"][0], schedule={"is_open": False})
        with self.assertRaises(DataError):
            session.build_session(**package)

    def test_assembled_prefix_can_resume_runner_without_duplicating_cash(self):
        full_package = observed_shape_fixture()
        prefix_package = copy.deepcopy(full_package)
        prefix_package["snapshots"] = prefix_package["snapshots"][:2]
        prefix_package["interval_costs"] = prefix_package["interval_costs"][:2]
        prefix = session.build_session(**prefix_package)
        full = session.build_session(**full_package)
        with tempfile.TemporaryDirectory() as directory:
            resumed = Path(directory) / "resumed.sqlite"
            fresh = Path(directory) / "fresh.sqlite"
            replay(prefix, resumed)
            later = replay(full, resumed)
            repeated = replay(full, resumed)
            expected = replay(full, fresh)
        self.assertEqual(later["final_state"], expected["final_state"])
        self.assertEqual(repeated["final_state"], expected["final_state"])

    def test_assemble_cli_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "existing.json"
            target.write_text("unchanged")
            with (
                patch(
                    "sys.argv",
                    [
                        "session",
                        "assemble",
                        "--input",
                        "nonexistent",
                        "--output",
                        str(target),
                    ],
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(session.main(), 2)
            self.assertEqual(target.read_text(), "unchanged")

    def test_assemble_cli_and_json_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = (
                Path(directory) / "input.json",
                Path(directory) / "output.json",
            )
            source.write_text(json.dumps(observed_shape_fixture(2)))
            with (
                patch(
                    "sys.argv",
                    [
                        "session",
                        "assemble",
                        "--input",
                        str(source),
                        "--output",
                        str(target),
                    ],
                ),
                patch("builtins.print"),
            ):
                self.assertEqual(session.main(), 0)
            built = session.load_package(target)
            self.assertEqual(len(built["frames"]), 2)
            self.assertFalse(built["execution_available"])
            source.write_text('{"a":1,"a":2}')
            with self.assertRaises(DataError):
                session.load_package(source)


class CollectorTests(OfflineTests):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "collection.sqlite"

    def collector(
        self, transport, clock=lambda: example_session.NOW, maximum=8, **extra
    ):
        return session.SessionCollector(
            self.path,
            collection_id="fixture-collection",
            max_gets=maximum,
            transport=transport,
            clock_ms=clock,
            **extra,
        )

    def test_two_allowlisted_gets_with_persistent_budget_and_observed_claim(self):
        transport, clock = transport_and_clock(observed_shape_fixture(1)["snapshots"])
        collector = self.collector(transport, clock)
        captured = collector.capture_next("sample-1")
        report = collector.export()
        self.assertEqual(report["reserved_gets"], 2)
        self.assertEqual(report["remaining_gets"], 6)
        self.assertFalse(captured["synthetic"])
        self.assertFalse(captured["source_authenticity_verified"])
        self.assertFalse(report["strategy_validated"])
        self.assertEqual(
            [r[0].full_url for r in transport.calls],
            [
                "https://external-api.kalshi.com/trade-api/v2/margin/markets/KXUS500PERP",
                "https://external-api.kalshi.com/trade-api/v2/margin/markets/KXUS500PERP/orderbook?depth=20",
            ],
        )
        for request, timeout in transport.calls:
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(
                dict(request.header_items()), {"Accept": "application/json"}
            )
            self.assertIsNone(request.data)
            self.assertEqual(timeout, 5)

    def test_restart_same_observation_id_returns_saved_snapshot_without_gets(self):
        transport, clock = transport_and_clock(observed_shape_fixture(1)["snapshots"])
        first = self.collector(transport, clock).capture_next("one")
        idle_transport = Transport([])
        resumed = self.collector(idle_transport)
        self.assertEqual(resumed.capture_next("one"), first)
        self.assertEqual(resumed.export()["reserved_gets"], 2)
        self.assertEqual(idle_transport.calls, [])

    def test_restart_new_observation_uses_same_global_budget(self):
        snapshots = observed_shape_fixture(2)["snapshots"]
        transport, clock = transport_and_clock(snapshots[:1])
        self.collector(transport, clock, maximum=4).capture_next("one")
        transport, clock = transport_and_clock(snapshots[1:])
        resumed = self.collector(transport, clock, maximum=4)
        resumed.capture_next("two")
        with self.assertRaisesRegex(DataError, "INSUFFICIENT_BUDGET"):
            resumed.capture_next("three")
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(resumed.export()["reserved_gets"], 4)
        self.assertEqual(resumed.export()["status"], "EXHAUSTED")

    def test_budget_cannot_increase_on_restart_or_buy_half_a_snapshot(self):
        idle = Transport([])
        collector = self.collector(idle, maximum=1)
        with self.assertRaises(DataError):
            collector.capture_next("one")
        with self.assertRaisesRegex(DataError, "COLLECTION_CONFIG_CHANGED"):
            self.collector(idle, maximum=2)
        self.assertEqual(idle.calls, [])
        self.assertEqual(collector.export()["reserved_gets"], 0)

    def test_market_closed_stops_before_orderbook_and_remains_paused_on_restart(self):
        snapshot = observed_shape_fixture(1)["snapshots"][0]
        change_market(snapshot, schedule={"is_open": False})
        transport, clock = transport_and_clock([snapshot])
        collector = self.collector(transport, clock)
        with self.assertRaisesRegex(DataError, "COLLECTION_MARKET_CLOSED"):
            collector.capture_next("closed")
        self.assertEqual(len(transport.calls), 1)
        idle = Transport([])
        resumed = self.collector(idle)
        with self.assertRaisesRegex(DataError, "COLLECTION_STOPPED"):
            resumed.capture_next("later")
        report = resumed.export()
        self.assertEqual(report["reserved_gets"], 1)
        self.assertEqual(report["status"], "PAUSED")
        self.assertEqual(idle.calls, [])

    def test_network_error_consumes_reserved_get_once_without_retry_or_details(self):
        transport = Transport([urllib.error.URLError("private-error-never-output")])
        collector = self.collector(transport)
        with self.assertRaisesRegex(DataError, "COLLECTION_NETWORK_ERROR"):
            collector.capture_next("failed")
        report = collector.export()
        self.assertEqual(report["reserved_gets"], 1)
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("private-error", json.dumps(report))
        self.assertEqual(report["requests"][0]["status"], "FAILED")

    def test_crash_after_reservation_is_unknown_and_never_retried_after_restart(self):
        transport = Transport([SystemExit("injected crash")])
        collector = self.collector(transport)
        with self.assertRaises(SystemExit):
            collector.capture_next("unknown")
        idle = Transport([])
        resumed = self.collector(idle)
        with self.assertRaisesRegex(DataError, "COLLECTION_STOPPED"):
            resumed.capture_next("unknown")
        report = resumed.export()
        self.assertEqual(report["reserved_gets"], 1)
        self.assertEqual(report["captures"][0]["status"], "UNKNOWN")
        self.assertEqual(report["requests"][0]["status"], "RESERVED")
        self.assertEqual(report["stop_reason"], "INTERRUPTED_CAPTURE_NO_RETRY")
        self.assertEqual(idle.calls, [])

    def test_crash_after_both_responses_does_not_duplicate_gets_or_observation(self):
        transport, clock = transport_and_clock(observed_shape_fixture(1)["snapshots"])
        collector = self.collector(transport, clock)
        with patch.object(
            session, "_valid_snapshot", side_effect=KeyboardInterrupt("injected crash")
        ):
            with self.assertRaises(KeyboardInterrupt):
                collector.capture_next("unknown")
        idle = Transport([])
        resumed = self.collector(idle)
        report = resumed.export()
        self.assertEqual(report["reserved_gets"], 2)
        self.assertEqual(len(report["captures"]), 1)
        self.assertTrue(
            all(r["status"] == "RESPONSE_SAVED" for r in report["requests"])
        )
        self.assertEqual(report["status"], "PAUSED")
        self.assertEqual(idle.calls, [])

    def test_conflicting_identity_during_collection_pauses_without_new_order_paths(
        self,
    ):
        snapshots = observed_shape_fixture(2)["snapshots"]
        change_market(snapshots[1], market_version=2)
        transport, clock = transport_and_clock(snapshots)
        collector = self.collector(transport, clock)
        collector.capture_next("first")
        with self.assertRaisesRegex(DataError, "INSTRUMENT_METADATA_CHANGED"):
            collector.capture_next("second")
        self.assertEqual(collector.export()["status"], "PAUSED")
        self.assertEqual(collector.export()["reserved_gets"], 4)

    def test_overlapping_new_capture_is_not_saved_as_independent_sample(self):
        snapshot = observed_shape_fixture(1)["snapshots"][0]
        transport, clock = transport_and_clock([snapshot, snapshot])
        collector = self.collector(transport, clock)
        collector.capture_next("one")
        with self.assertRaisesRegex(DataError, "REUSED_OR_OVERLAPPING_CAPTURE"):
            collector.capture_next("two")
        report = collector.export()
        self.assertEqual(sum(c["status"] == "COMPLETE" for c in report["captures"]), 1)

    def test_tampered_saved_snapshot_is_rejected_on_restart(self):
        transport, clock = transport_and_clock(observed_shape_fixture(1)["snapshots"])
        collector = self.collector(transport, clock)
        collector.capture_next("one")
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE us500_capture SET snapshot_hash=?", ("0" * 64,))
        with self.assertRaisesRegex(DataError, "SAVED_SNAPSHOT_DIGEST_MISMATCH"):
            self.collector(Transport([])).capture_next("one")

    def test_durable_budget_accounting_conflict_prevents_resume_and_export(self):
        collector = self.collector(Transport([]))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE us500_collection_meta SET reserved_gets=99")
        with self.assertRaisesRegex(DataError, "GLOBAL_GET_ACCOUNTING_CONFLICT"):
            collector.export()
        with self.assertRaisesRegex(DataError, "GLOBAL_GET_ACCOUNTING_CONFLICT"):
            self.collector(Transport([]))

    def test_foreign_journal_and_symlink_are_preserved(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("CREATE TABLE foreign_data(value)")
            connection.execute("INSERT INTO foreign_data VALUES(42)")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(DataError, "JOURNAL_NOT_OWNED"):
            self.collector(Transport([]))
        self.assertEqual(self.path.read_bytes(), before)
        target = self.path.parent / "link.sqlite"
        target.symlink_to(self.path)
        with self.assertRaises(DataError):
            session.SessionCollector(
                target, collection_id="fixture", transport=Transport([])
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_file_lock_stops_concurrent_capture_before_network(self):
        collector = self.collector(Transport([]))
        with Path(str(self.path) + ".lock").open("rb") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(DataError, "COLLECTION_BUSY"):
                collector.capture_next("one")
        self.assertEqual(collector.export()["reserved_gets"], 0)

    def test_default_transport_disables_proxies_and_redirects(self):
        with patch.object(session.urllib.request, "build_opener") as build:
            session.SessionCollector(self.path, collection_id="test")
        handlers = build.call_args.args
        self.assertEqual(handlers[0].proxies, {})
        with self.assertRaises(DataError):
            handlers[1].redirect_request(None, None, 302, "", {}, "https://example.com")


if __name__ == "__main__":
    unittest.main()
