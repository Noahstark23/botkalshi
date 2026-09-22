"""M5 research inputs producer — fee from Kalshi's public API, fair from a budgeted pilot.

Pinned (CTO, 2026-09-22):
  - fee = series + event + scheduled changes, each entry with its ORIGINAL read time and
    the official schedule's window; unreadable or inconsistent sources fail closed;
  - fair = M2's computation, dated by the oldest accepted book's `last_update` — never
    the download or export time; stale or future-dated lines are not evidence;
  - odds pilot: off by default; the owner's key FILE (0600) and a budget ≤ 10 credits;
    one request shape (MLB, h2h, one region); the credit reserved BEFORE the request;
    an unexpected cost halts; the key never lands in any output.
All payloads are synthetic fixtures (not market data). Stdlib only: this suite also runs
under the droplet's system python3. No network.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import collector  # noqa: E402
import m5_inputs as producer  # noqa: E402
import m5_research_sim as sim  # noqa: E402
import research_runner  # noqa: E402
import simulation_bank as bank  # noqa: E402
from m1_observation import review_m1_book  # noqa: E402

NOW = datetime(2026, 9, 22, 19, 0, tzinfo=UTC)
SERIES = "KXMLBGAME"
EVENT = "KXMLBGAME-26SEP221905NYYBOS"  # 19:05 ET on 22-sep = 23:05 UTC
KICKOFF = datetime(2026, 9, 22, 23, 5, tzinfo=UTC)
KEY = "fixturekey0123456789"


def series_body(mult=0.5, fee_type="quadratic_with_maker_fees"):
    return {"series": {"ticker": SERIES, "fee_type": fee_type, "fee_multiplier": mult}}


def changes_body(*rows):
    return {
        "series_fee_change_arr": [
            {
                "id": f"c{i}",
                "series_ticker": SERIES,
                "fee_type": "quadratic_with_maker_fees",
                "fee_multiplier": m,
                "scheduled_ts": ts,
            }
            for i, (m, ts) in enumerate(rows)
        ]
    }


PAST_CHANGE = (0.5, "2026-08-07T04:59:45Z")


def event_body(event=EVENT, **override):
    return {"event": {"event_ticker": event, "series_ticker": SERIES, **override}, "markets": []}


class FakeReader:
    """Answers by path; records every call. It never opens a socket."""

    def __init__(self, responses, headers=None, on_call=None):
        self.responses = responses
        self.headers = headers or {}
        self.calls = []
        self.on_call = on_call

    def _answer(self, origin, path, params):
        self.calls.append((origin, path, dict(params or {})))
        if self.on_call:
            self.on_call(path)
        value = self.responses[path]
        if isinstance(value, Exception):
            raise value
        return value

    def get_json(self, origin, path, params=None):
        return self._answer(origin, path, params)

    def get_json_with_headers(self, origin, path, params=None, header_names=()):
        return self._answer(origin, path, params), {
            k: v for k, v in self.headers.items() if k in header_names
        }


def kalshi_responses(**overrides):
    base = {
        f"/series/{SERIES}": series_body(),
        "/series/fee_changes": changes_body(PAST_CHANGE),
        f"/events/{EVENT}": event_body(),
    }
    base.update(overrides)
    return base


def market(ticker, name, *, yes=(("0.4500", "10"),), no=(("0.5000", "10"),)):
    return {
        "ticker": ticker,
        "event_ticker": EVENT,
        "status": "open",
        "yes_sub_title": name,
        "close_time": (NOW + timedelta(hours=6)).isoformat(),
        "book_observed_at": (NOW - timedelta(seconds=2)).isoformat(),
        "levels": {},
        "m1_book": {"yes_bids": [list(x) for x in yes], "no_bids": [list(x) for x in no]},
    }


MARKETS = [
    market(f"{EVENT}-NYY", "New York Y"),
    market(f"{EVENT}-BOS", "Boston", yes=(("0.4900", "10"),), no=(("0.5400", "10"),)),
]


def odds_event(*, commence=KICKOFF, books=None):
    books = (
        books
        if books is not None
        else [
            ("b1", 1.95, 2.00, 4),
            ("b2", 1.90, 2.05, 2),
            ("b3", 1.92, 2.02, 1),
        ]
    )
    return {
        "id": "oe1",
        "sport_key": "baseball_mlb",
        "commence_time": commence.isoformat(),
        "home_team": "Boston Red Sox",
        "away_team": "New York Yankees",
        "bookmakers": [
            {
                "key": k,
                "last_update": (NOW - timedelta(minutes=age)).isoformat(),
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "New York Yankees", "price": a},
                            {"name": "Boston Red Sox", "price": b},
                        ],
                    }
                ],
            }
            for k, a, b, age in books
        ],
    }


class ProducerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        for name in ("connect", "connect_ex"):
            blocker = patch.object(
                socket.socket, name, side_effect=AssertionError("NETWORK FORBIDDEN")
            )
            blocker.start()
            self.addCleanup(blocker.stop)

    def key_file(self, mode=0o600, content=KEY):
        path = self.data / "odds.key"
        path.write_text(content)
        path.chmod(mode)
        return path

    def pilot_env(self, key_file, budget="10", interval="1800"):
        return {
            "BOTKALSHI_ODDS_PILOT_ENABLED": "true",
            "BOTKALSHI_ODDS_PILOT_KEY_FILE": str(key_file),
            "BOTKALSHI_ODDS_PILOT_BUDGET": budget,
            "BOTKALSHI_ODDS_PILOT_MIN_INTERVAL_SEC": interval,
        }


class FeeTests(ProducerTestCase):
    def fees(self, responses, markets=MARKETS, previous=(), now=NOW):
        reader = FakeReader(responses)
        entries, status = producer.produce_fees(
            reader, markets=list(markets), previous=list(previous), now=now, clock=lambda: now
        )
        return entries, status, reader

    def test_series_schedule_with_its_official_start(self):
        [entry], status, _ = self.fees(kalshi_responses())
        self.assertEqual(status["events"][EVENT], "OK")
        self.assertEqual(entry["fee_multiplier"], "1/2")
        self.assertEqual(entry["source"], "series")
        self.assertEqual(entry["observed_at"], NOW.isoformat())
        self.assertEqual(entry["effective_from"], "2026-08-07T04:59:45+00:00")
        self.assertIsNone(entry["effective_until"])
        self.assertEqual(status["fee_changes_wire"], "DOCUMENTED_NOT_OBSERVED_IN_SESSION")

    def test_scheduled_change_splits_the_windows(self):
        future = (1, (NOW + timedelta(minutes=30)).isoformat())
        entries, _, _ = self.fees(
            kalshi_responses(**{"/series/fee_changes": changes_body(PAST_CHANGE, future)})
        )
        self.assertEqual([e["fee_multiplier"] for e in entries], ["1/2", "1"])
        market = {"ticker": f"{EVENT}-NYY", "event_ticker": EVENT}
        before = sim._resolve_fee(entries, market=market, now=NOW, p=sim.DEFAULT_PARAMS)
        after = sim._resolve_fee(
            entries, market=market, now=NOW + timedelta(minutes=31), p=sim.DEFAULT_PARAMS
        )
        self.assertEqual((before["fee_multiplier"], after["fee_multiplier"]), ("1/2", "1"))

    def test_event_override_wins_and_proves_no_past(self):
        [entry], _, _ = self.fees(
            kalshi_responses(
                **{
                    f"/events/{EVENT}": event_body(
                        fee_type_override="quadratic_with_maker_fees", fee_multiplier_override=1
                    )
                }
            )
        )
        self.assertEqual((entry["source"], entry["fee_multiplier"]), ("event_override", "1"))
        self.assertIsNone(entry["effective_from"])

    def test_partial_override_blocks_the_event(self):
        entries, status, _ = self.fees(
            kalshi_responses(**{f"/events/{EVENT}": event_body(fee_multiplier_override=1)})
        )
        self.assertEqual((entries, status["events"][EVENT]), ([], "PARTIAL_OVERRIDE"))

    def test_unsupported_fee_type_blocks(self):
        entries, status, _ = self.fees(
            kalshi_responses(**{f"/series/{SERIES}": series_body(fee_type="flat")})
        )
        self.assertEqual(entries, [])
        self.assertEqual(status["series"][SERIES], "UNSUPPORTED_FEE_TYPE")

    def test_unreadable_fee_changes_fail_closed(self):
        for body in (
            {"changes": []},
            {"series_fee_change_arr": [{"series_ticker": SERIES}]},
            collector.ResearchError("provider HTTP 404"),
        ):
            with self.subTest(body=body):
                entries, status, _ = self.fees(kalshi_responses(**{"/series/fee_changes": body}))
                self.assertEqual(entries, [])
                self.assertNotEqual(status["series"][SERIES], "OK")

    def test_scheduled_change_of_another_series_is_refused(self):
        body = changes_body(PAST_CHANGE)
        body["series_fee_change_arr"][0]["series_ticker"] = "KXNFLGAME"
        entries, status, _ = self.fees(kalshi_responses(**{"/series/fee_changes": body}))
        self.assertEqual((entries, status["series"][SERIES]), ([], "FEE_CHANGES_UNREADABLE"))

    def test_series_disagreeing_with_its_own_schedule_blocks(self):
        entries, status, _ = self.fees(kalshi_responses(**{f"/series/{SERIES}": series_body(1)}))
        self.assertEqual((entries, status["events"][EVENT]), ([], "SCHEDULE_DISAGREES_WITH_SERIES"))

    def test_event_identity_mismatch_blocks(self):
        entries, status, _ = self.fees(
            kalshi_responses(**{f"/events/{EVENT}": event_body(event="KXMLBGAME-OTHER")})
        )
        self.assertEqual((entries, status["events"][EVENT]), ([], "EVENT_IDENTITY_MISMATCH"))

    def test_fresh_evidence_is_not_refetched_and_old_evidence_expires(self):
        first, _, _ = self.fees(kalshi_responses())
        again, status, reader = self.fees(
            kalshi_responses(), previous=first, now=NOW + timedelta(minutes=5)
        )
        self.assertEqual(reader.calls, [])
        self.assertEqual(again, first)  # the original read time is kept, not re-dated
        later, _, _ = self.fees(kalshi_responses(), previous=first, now=NOW + timedelta(hours=3))
        self.assertTrue(all(e["observed_at"] != NOW.isoformat() for e in later))


class PilotTests(ProducerTestCase):
    def fetch(self, reader, env, now=NOW):
        config, config_status = producer.pilot_config(env)
        return producer.maybe_fetch_odds(
            reader,
            data_dir=self.data,
            config=config,
            config_status=config_status,
            wanted=True,
            now=now,
        )

    def odds_reader(self, **kw):
        return FakeReader({"/sports/baseball_mlb/odds": [odds_event()]}, **kw)

    def test_off_by_default(self):
        reader = self.odds_reader()
        self.assertEqual(self.fetch(reader, {})["status"], "PILOT_DISABLED")
        self.assertEqual(reader.calls, [])

    def test_owner_key_file_is_required(self):
        reader = self.odds_reader()
        env = {"BOTKALSHI_ODDS_PILOT_ENABLED": "true", "BOTKALSHI_ODDS_PILOT_BUDGET": "10"}
        self.assertEqual(self.fetch(reader, env)["status"], "OWNER_KEY_FILE_NOT_PROVISIONED")
        self.assertEqual(reader.calls, [])

    def test_interval_below_thirty_minutes_is_refused(self):
        """The published pilot limit is one call every >= 30 min; 10 min is not allowed."""
        reader = self.odds_reader()
        result = self.fetch(reader, self.pilot_env(self.key_file(), interval="1799"))
        self.assertEqual(result["status"], "PILOT_INTERVAL_TOO_SHORT")
        self.assertEqual(reader.calls, [])

    def test_budget_above_ten_is_refused(self):
        self.assertEqual(
            self.fetch(self.odds_reader(), self.pilot_env(self.key_file(), budget="11"))["status"],
            "PILOT_BUDGET_OUT_OF_RANGE",
        )

    def test_world_readable_key_file_is_refused_before_any_reservation(self):
        reader = self.odds_reader()
        with self.assertRaisesRegex(producer.InputsSourceError, "KEY_FILE_PERMISSIONS"):
            self.fetch(reader, self.pilot_env(self.key_file(mode=0o644)))
        self.assertEqual(reader.calls, [])
        ledger = (
            json.loads((self.data / "m5" / "odds-pilot-ledger.json").read_text())
            if (self.data / "m5" / "odds-pilot-ledger.json").exists()
            else {"reserved_credits": 0}
        )
        self.assertEqual(ledger["reserved_credits"], 0)

    def test_one_request_shape_and_the_credit_is_reserved_before_it(self):
        ledger_path = self.data / "m5" / "odds-pilot-ledger.json"
        seen = []

        def during_call(path):
            seen.append(json.loads(ledger_path.read_text())["reserved_credits"])

        reader = self.odds_reader(
            headers={"x-requests-last": "1", "x-requests-remaining": "499"}, on_call=during_call
        )
        result = self.fetch(reader, self.pilot_env(self.key_file()))
        self.assertEqual(result["status"], "FETCHED")
        self.assertEqual(seen, [1])  # already persisted while the request was in flight
        [(origin, path, params)] = reader.calls
        self.assertEqual(origin, "https://api.the-odds-api.com/v4")
        self.assertEqual(path, "/sports/baseball_mlb/odds")
        self.assertEqual(
            {k: v for k, v in params.items() if k != "apiKey"},
            {"regions": "us", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"},
        )

    def test_budget_of_ten_is_never_exceeded(self):
        env = self.pilot_env(self.key_file(), interval="1800")
        reader = self.odds_reader(headers={"x-requests-last": "1"})
        outcomes = [
            self.fetch(reader, env, now=NOW + timedelta(minutes=30 * i))["status"]
            for i in range(12)
        ]
        self.assertEqual(outcomes.count("FETCHED"), 10)
        self.assertEqual(outcomes[10:], ["BUDGET_EXHAUSTED", "BUDGET_EXHAUSTED"])
        self.assertEqual(len(reader.calls), 10)

    def test_raising_the_env_budget_does_not_reopen_a_spent_ledger(self):
        env = self.pilot_env(self.key_file(), budget="3", interval="1800")
        reader = self.odds_reader(headers={"x-requests-last": "1"})
        for i in range(4):
            self.fetch(reader, env, now=NOW + timedelta(minutes=30 * i))
        self.assertEqual(len(reader.calls), 3)
        env["BOTKALSHI_ODDS_PILOT_BUDGET"] = "10"
        self.assertEqual(
            self.fetch(reader, env, now=NOW + timedelta(hours=5))["status"], "BUDGET_EXHAUSTED"
        )
        self.assertEqual(len(reader.calls), 3)

    def test_failed_request_still_counts(self):
        reader = FakeReader(
            {"/sports/baseball_mlb/odds": collector.ResearchError("provider HTTP 401")}
        )
        result = self.fetch(reader, self.pilot_env(self.key_file()))
        self.assertEqual((result["status"], result["reserved_credits"]), ("ERROR", 1))

    def test_interval_between_calls(self):
        env = self.pilot_env(self.key_file())
        reader = self.odds_reader()
        self.fetch(reader, env)
        self.assertEqual(
            self.fetch(reader, env, now=NOW + timedelta(minutes=5))["status"], "WAITING_INTERVAL"
        )

    def test_unexpected_cost_halts_the_pilot(self):
        env = self.pilot_env(self.key_file(), interval="1800")
        reader = self.odds_reader(headers={"x-requests-last": "2"})
        self.fetch(reader, env)
        self.assertEqual(self.fetch(reader, env, now=NOW + timedelta(hours=1))["status"], "HALTED")
        self.assertEqual(len(reader.calls), 1)

    def test_the_key_never_lands_in_any_output(self):
        self.fetch(
            self.odds_reader(headers={"x-requests-last": "1"}), self.pilot_env(self.key_file())
        )
        for path in self.data.rglob("*"):
            if path.is_file() and path.name != "odds.key":
                self.assertNotIn(KEY, path.read_text(errors="ignore"), path)


class FairTests(ProducerTestCase):
    def doc(self, events, fetched=NOW):
        return {
            "fetched_at": fetched.isoformat(),
            "events": [collector.sanitize_odds_event(e) for e in events],
        }

    def test_fair_is_m2_consensus_dated_by_the_oldest_book(self):
        fairs, diag = producer.produce_fairs(self.doc([odds_event()]), markets=MARKETS, now=NOW)
        self.assertEqual(diag["events"][EVENT], "FAIR:3_books")
        by_ticker = {f["ticker"]: f for f in fairs}
        nyy = by_ticker[f"{EVENT}-NYY"]
        self.assertEqual(nyy["source"], "m2-consensus-v1")
        self.assertEqual(nyy["observed_at"], (NOW - timedelta(minutes=4)).isoformat())
        self.assertEqual(nyy["commence_time"], KICKOFF.isoformat())
        total = sum(float(f["fair_prob"]) for f in fairs)
        self.assertAlmostEqual(total, 1.0, places=3)  # no-vig

    def test_too_few_fresh_books_gives_no_fair(self):
        stale = odds_event(
            books=[("b1", 1.95, 2.0, 4), ("b2", 1.9, 2.05, 2), ("b3", 1.92, 2.02, 40)]
        )
        fairs, diag = producer.produce_fairs(self.doc([stale]), markets=MARKETS, now=NOW)
        self.assertEqual((fairs, diag["events"][EVENT]), ([], "NO_FAIR:too_few_books"))

    def test_line_dated_after_its_own_download_is_not_evidence(self):
        future = odds_event(
            books=[("b1", 1.95, 2.0, 4), ("b2", 1.9, 2.05, 2), ("b3", 1.92, 2.02, -10)]
        )
        fairs, _ = producer.produce_fairs(self.doc([future]), markets=MARKETS, now=NOW)
        self.assertEqual(fairs, [])

    def test_future_dated_line_is_excluded_even_when_evaluated_later(self):
        """Dated 10 min after its own download: by the time the clock passes that date,
        M2's age filter alone would accept it. It is not evidence either way."""
        future = odds_event(
            books=[("b1", 1.95, 2.0, 4), ("b2", 1.9, 2.05, 2), ("b3", 1.92, 2.02, -10)]
        )
        fairs, diag = producer.produce_fairs(
            self.doc([future]), markets=MARKETS, now=NOW + timedelta(minutes=11)
        )
        self.assertEqual(fairs, [])
        self.assertEqual(diag["events"][EVENT], "NO_FAIR:too_few_books")

    def test_export_time_never_rejuvenates_the_fair(self):
        """Evaluating an old download later: the books age with the clock, not with it."""
        fairs, diag = producer.produce_fairs(
            self.doc([odds_event()]), markets=MARKETS, now=NOW + timedelta(minutes=20)
        )
        self.assertEqual(fairs, [])
        self.assertIn("NO_FAIR", diag["events"][EVENT])

    def test_started_game_or_other_date_does_not_match(self):
        started = odds_event(commence=NOW - timedelta(minutes=1))
        _, diag = producer.produce_fairs(self.doc([started]), markets=MARKETS, now=NOW)
        self.assertEqual(diag["events"][EVENT], "NO_MATCH:started")
        other_day = odds_event(commence=KICKOFF + timedelta(days=1))
        _, diag = producer.produce_fairs(self.doc([other_day]), markets=MARKETS, now=NOW)
        self.assertEqual(diag["events"][EVENT], "NO_MATCH:date")

    def test_market_without_outcome_name_is_not_guessed(self):
        unnamed = [dict(m, yes_sub_title=None) for m in MARKETS]
        _, diag = producer.produce_fairs(self.doc([odds_event()]), markets=unnamed, now=NOW)
        self.assertEqual(diag["events"][EVENT], "NO_OUTCOME_NAMES")


class EndToEndTests(ProducerTestCase):
    def test_produced_inputs_feed_an_admitted_m5_quote(self):
        bank_db = self.data / "simulation-bank.sqlite3"
        bank.init_bank(bank_db, initial_capital_usd="200.00")
        reader = FakeReader(
            {**kalshi_responses(), "/sports/baseball_mlb/odds": [odds_event()]},
            headers={"x-requests-last": "1"},
        )
        packet = {
            "packet_id": "c1",
            "generated_at": NOW.isoformat(),
            "kalshi": {"markets": MARKETS},
        }
        status = producer.produce(
            reader,
            data_dir=self.data,
            packet=packet,
            now=NOW,
            env=self.pilot_env(self.key_file()),
            clock=lambda: NOW,
        )
        self.assertEqual(status["odds_pilot"]["status"], "FETCHED")
        inputs, problem = sim.load_inputs(self.data / "m5" / "inputs.json")
        self.assertIsNone(problem)
        m1 = {
            "cycle_id": "c1",
            "reviews": [
                review_m1_book(research_runner._m1_book_input("c1", m), now=NOW) for m in MARKETS
            ],
        }
        report = sim.run_cycle(bank_db=bank_db, packet=packet, m1_review=m1, inputs=inputs, now=NOW)
        active = [p for p in report["proposals"] if p["active"]]
        self.assertTrue(active, report["blocked"])
        [q] = [
            a
            for a in bank.list_admissions(bank_db, origin="M5")
            if a["admission_key"] == active[0]["admission_key"]
        ]
        self.assertEqual(q["evidence"]["fair"]["source"], "m2-consensus-v1")
        self.assertEqual(q["evidence"]["fee"]["effective_from"], "2026-08-07T04:59:45+00:00")

    def test_without_the_pilot_m5_is_honestly_blocked_no_fair(self):
        bank_db = self.data / "simulation-bank.sqlite3"
        bank.init_bank(bank_db, initial_capital_usd="200.00")
        reader = FakeReader(kalshi_responses())
        packet = {
            "packet_id": "c1",
            "generated_at": NOW.isoformat(),
            "kalshi": {"markets": MARKETS},
        }
        status = producer.produce(
            reader, data_dir=self.data, packet=packet, now=NOW, env={}, clock=lambda: NOW
        )
        self.assertEqual(status["odds_pilot"]["status"], "PILOT_DISABLED")
        inputs, _ = sim.load_inputs(self.data / "m5" / "inputs.json")
        m1 = {
            "cycle_id": "c1",
            "reviews": [
                review_m1_book(research_runner._m1_book_input("c1", m), now=NOW) for m in MARKETS
            ],
        }
        report = sim.run_cycle(bank_db=bank_db, packet=packet, m1_review=m1, inputs=inputs, now=NOW)
        self.assertEqual(report["proposals"], [])
        self.assertTrue(all(r[0] == "BLOCKED_NO_FAIR" for r in report["blocked"].values()))


class OperatorScriptTests(ProducerTestCase):
    def test_init_bank_is_explicit_idempotent_and_never_redefines_capital(self):
        import init_sim_bank

        with patch("sys.stdout"):
            self.assertEqual(
                init_sim_bank.main(["--data", str(self.data), "--initial-capital-usd", "200.00"]), 0
            )
            self.assertEqual(
                init_sim_bank.main(["--data", str(self.data), "--initial-capital-usd", "200.00"]), 0
            )
        with patch("sys.stderr"):
            self.assertEqual(
                init_sim_bank.main(["--data", str(self.data), "--initial-capital-usd", "500.00"]), 2
            )

    def c3(self, rows):
        import m5_c3_report

        for row in rows:
            research_runner._append_bounded(self.data / "m5" / "cycles.jsonl", row)
        return m5_c3_report.build_report(self.data)

    @staticmethod
    def cycle(i, **kw):
        row = {
            "cycle_id": f"c{i}",
            "status": "OK",
            "errors": [],
            "inputs_problem": None,
            "blocked": {},
            "proposals": [],
            "evaluations": [],
            "withdrawals": [],
        }
        row.update(kw)
        return row

    def test_ten_blocked_cycles_are_recorded_but_do_not_qualify(self):
        """Review of da71325: ten BLOCKED_NO_FAIR cycles are honest, not C3 evidence."""
        report = self.c3([self.cycle(i, blocked={"T": ["BLOCKED_NO_FAIR"]}) for i in range(10)])
        public = report["m5_public"]
        self.assertEqual(public["distinct_cycle_ids_recorded"], 10)
        self.assertEqual(public["qualifying_cycles"], 0)
        self.assertIs(public["c3_cycles_met"], False)
        self.assertEqual(public["not_qualifying_reasons"], {"ALL_BLOCKED_BLOCKED_NO_FAIR": 10})

    def test_errors_inputs_problems_and_unverified_fees_do_not_qualify(self):
        decided = [{"decision": "ADMITTED", "active": True}]  # would qualify on its own
        rows = [
            self.cycle(0, errors=[{"error": "x"}], proposals=decided),
            self.cycle(1, inputs_problem="NO_INPUTS_FILE", proposals=decided),
            self.cycle(2, status="OK_WITH_ERRORS", proposals=decided),
            self.cycle(3, evaluations=[{"status": "UNVERIFIED"}]),
            self.cycle(4, evaluations=[{"status": "EVALUATED", "fee_state": "UNVERIFIED"}]),
            self.cycle(5, evaluations=[{"status": "GAP"}]),
            self.cycle(6, evaluations=[{"status": "REPLAY"}]),
        ]
        self.assertEqual(self.c3(rows)["m5_public"]["qualifying_cycles"], 0)

    def test_usable_cycles_qualify_without_fills(self):
        rows = [
            self.cycle(i, proposals=[{"decision": "ADMITTED", "active": True}]) for i in range(5)
        ]
        rows += [self.cycle(i, proposals=[{"decision": "REJECTED"}]) for i in range(5, 8)]
        rows += [
            self.cycle(i, evaluations=[{"status": "EVALUATED", "fee_state": "OK", "fills": []}])
            for i in range(8, 10)
        ]
        report = self.c3(rows)
        public = report["m5_public"]
        self.assertEqual(public["qualifying_cycles"], 10)
        self.assertIs(public["c3_cycles_met"], True)
        self.assertEqual(public["fills_booked"], 0)
        self.assertIs(public["fills_required_for_c3"], False)
        self.assertEqual(report["radar"], "NOT_CONNECTED")
        self.assertEqual(report["exits_settlements"], "PENDING")
        self.assertIn("NOT_MEASURED_HERE", report["m5_local_tested"])

    def test_a_repeated_cycle_id_counts_once(self):
        rows = [self.cycle(0, proposals=[{"decision": "ADMITTED"}]) for _ in range(10)]
        self.assertEqual(self.c3(rows)["m5_public"]["qualifying_cycles"], 1)

    def test_restart_mark_detects_a_rewritten_history(self):
        import sqlite3

        import m5_c3_report

        db = self.data / "simulation-bank.sqlite3"
        bank.init_bank(db, initial_capital_usd="200.00")
        bank.record_fill(
            db,
            event_key="f1",
            origin="M5",
            position_key="m5r1:X",
            side="buy",
            price_cents=45,
            count=1,
            fee_cents=1,
            occurred_at=NOW,
            now=NOW,
        )
        mark = m5_c3_report.restart_mark(db)
        # New activity after the restart does not break the mark...
        bank.record_fill(
            db,
            event_key="f2",
            origin="M5",
            position_key="m5r1:X",
            side="sell",
            price_cents=50,
            count=1,
            fee_cents=1,
            occurred_at=NOW,
            now=NOW,
        )
        self.assertEqual(m5_c3_report.check_restart(db, mark)["verdict"], "PRESERVED")
        # ...a rewritten past does.
        con = sqlite3.connect(db)
        con.execute("UPDATE simulation_events SET fee_cents = 0 WHERE event_key = 'f1'")
        con.commit()
        con.close()
        self.assertEqual(m5_c3_report.check_restart(db, mark)["verdict"], "CHANGED")

    def test_cycle_history_is_bounded(self):
        path = self.data / "m5" / "cycles.jsonl"
        for i in range(30):
            research_runner._append_bounded(path, {"cycle_id": i}, max_lines=10)
        lines = path.read_text().splitlines()
        self.assertEqual([json.loads(x)["cycle_id"] for x in lines], list(range(20, 30)))


if __name__ == "__main__":
    unittest.main()
