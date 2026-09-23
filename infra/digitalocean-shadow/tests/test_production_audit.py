"""S2 — read-only audit of the REAL production trade history (ASTRA-SUPERVISION-20260923).

The source DB is built from `fixtures/production_schema.sql`, the DDL compiled from
`src/storage/models.py` (a main-suite test fails if it drifts). Pinned:
  - the source is never modified (mode=ro; bytes identical after passes);
  - replay and restart keep identities, sums and cursors — no double cost or result;
  - unknown fees, lost/ahead cursors, contradictory data, gaps, partial passes and
    state-write failures are explicit diagnostics, never silent corrections;
  - unmappable rows are reported specifically, not invented;
  - the audit is separate from the simulation bank and carries no authority.
Stdlib only. No network.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

import production_audit as audit  # noqa: E402

DDL = (Path(__file__).parent / "fixtures" / "production_schema.sql").read_text()
NOW = datetime(2026, 9, 23, 14, 0, tzinfo=UTC)


class AuditTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.source = self.dir / "trades.db"
        self.state = self.dir / "audit.sqlite3"
        with sqlite3.connect(self.source) as con:
            con.executescript(DDL)
        self.next_id = 1

    def add(
        self,
        coid,
        *,
        strategy="motor_1_arbitrage",
        status="filled",
        fill=45,
        fee=1,
        pnl=None,
        settled=None,
        id=None,
        count=2,
        price=45,
        filled_count=None,
    ):
        tid = id or self.next_id
        self.next_id = tid + 1
        with sqlite3.connect(self.source) as con:
            con.execute(
                "INSERT INTO trades (id, client_order_id, ticker, side, action, count, price_cents, "
                "strategy, status, fill_price_cents, fees_cents, pnl_cents, closed_by_clv, "
                "filled_count, placed_at, filled_at, settled_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                (
                    tid,
                    coid,
                    "KXMLBGAME-26SEP23-NYY",
                    "yes",
                    "buy",
                    count,
                    price,
                    strategy,
                    status,
                    fill,
                    fee,
                    pnl,
                    filled_count,
                    "2026-09-23 13:00:00",
                    "2026-09-23 13:00:05",
                    settled,
                ),
            )

    def update(self, coid, **fields):
        sets = ", ".join(f"{k} = ?" for k in fields)
        with sqlite3.connect(self.source) as con:
            con.execute(
                f"UPDATE trades SET {sets} WHERE client_order_id = ?", (*fields.values(), coid)
            )

    def run_pass(self, **kw):
        return audit.audit_pass(self.source, self.state, now=NOW, **kw)

    def events(self):
        with sqlite3.connect(self.state) as con:
            return con.execute(
                "SELECT client_order_id, transition, payload_json FROM audit_events ORDER BY 1, 2"
            ).fetchall()


class ProjectionTests(AuditTestCase):
    def test_transitions_are_projected_with_exact_cents(self):
        self.add("a", status="settled", pnl=37, settled="2026-09-23 13:30:00")
        self.add("b", status="filled", fill=44, filled_count=1)
        self.add("c", status="cancelled", fill=None, fee=None)
        report = self.run_pass()
        self.assertEqual(report["status"], "CLEAN", report["diagnostics"])
        self.assertEqual(report["authority"], "NONE")
        self.assertEqual(report["domain"], "PRODUCTION_REAL_READONLY")
        transitions = [(c, t) for c, t, _ in self.events()]
        self.assertEqual(
            transitions,
            [
                ("a", "FILLED"),
                ("a", "PLACED"),
                ("a", "SETTLED"),
                ("b", "FILLED"),
                ("b", "PLACED"),
                ("c", "CANCELLED"),
                ("c", "PLACED"),
            ],
        )
        cohort = report["cohorts"]["motor_1_arbitrage"]
        self.assertEqual(
            (cohort["realized_pnl_cents_audit"], cohort["realized_pnl_cents_source"]), (37, 37)
        )

    def test_lifecycle_across_passes_adds_only_the_new_transition(self):
        self.add("a", status="pending", fill=None, fee=None)
        self.assertEqual(self.run_pass()["new_events"], 1)  # PLACED
        self.update("a", status="filled", fill_price_cents=45, fees_cents=1)
        self.assertEqual(self.run_pass()["new_events"], 1)  # FILLED
        self.update("a", status="settled", pnl_cents=-46, settled_at="2026-09-23 14:00:00")
        report = self.run_pass()
        self.assertEqual(report["new_events"], 1)  # SETTLED
        self.assertEqual(report["cohorts"]["motor_1_arbitrage"]["realized_pnl_cents_audit"], -46)


class ReplayAndRestartTests(AuditTestCase):
    def test_replay_and_restart_never_duplicate_events_or_sums(self):
        self.add("a", status="settled", pnl=10, settled="2026-09-23 13:30:00")
        self.add("b", status="settled", pnl=-4, settled="2026-09-23 13:31:00")
        first = self.run_pass()
        before = self.events()
        again = self.run_pass()  # replay: same source
        self.assertEqual(again["new_events"], 0)
        self.assertEqual(again["unchanged_rows"], 2)
        # "restart": a new process, same state file
        import importlib

        importlib.reload(audit)
        third = audit.audit_pass(self.source, self.state, now=NOW)
        self.assertEqual(self.events(), before)
        for report in (again, third):
            self.assertEqual(report["cohorts"], first["cohorts"])

    def test_lost_cursor_rebuilds_idempotently(self):
        self.add("a", status="settled", pnl=10, settled="2026-09-23 13:30:00")
        self.run_pass()
        before = self.events()
        with sqlite3.connect(self.state) as con:
            con.execute("DELETE FROM audit_cursor")
            con.execute("DELETE FROM audit_trade_state")
        report = self.run_pass()
        self.assertEqual(report["new_events"], 0)  # stable identities: nothing doubled
        self.assertEqual(self.events(), before)
        self.assertEqual(report["cohorts"]["motor_1_arbitrage"]["cursor_last_id"], 1)

    def test_cursors_are_isolated_by_cohort(self):
        self.add(
            "a",
            strategy="motor_1_arbitrage",
            status="settled",
            pnl=5,
            settled="2026-09-23 13:00:00",
        )
        self.add(
            "b", strategy="motor_5_mm", status="settled", pnl=-2, settled="2026-09-23 13:00:00"
        )
        report = self.run_pass()
        self.assertEqual(report["cohorts"]["motor_1_arbitrage"]["cursor_last_id"], 1)
        self.assertEqual(report["cohorts"]["motor_5_mm"]["cursor_last_id"], 2)
        self.assertEqual(report["cohorts"]["motor_5_mm"]["realized_pnl_cents_audit"], -2)


class PaginationTests(AuditTestCase):
    """Review of 130bb6b: every pass used to re-read the FIRST page, so the tail beyond
    max_rows was never projected and PARTIAL_PASS stayed forever."""

    def settle_rows(self, n, start=1):
        for i in range(start, start + n):
            self.add(f"t{i}", status="settled", pnl=10, settled="2026-09-23 13:30:00")

    def distinct_events(self):
        return len({(c, t) for c, t, _ in self.events()})

    def test_backlog_is_consumed_across_pages_without_losing_the_tail(self):
        self.settle_rows(7)
        first = self.run_pass(max_rows=3)
        self.assertEqual(
            (first["partial"], first["backlog_rows"], first["scan_cursor"]), (True, 4, 3)
        )
        self.assertEqual(first["diagnostics"].get("SUM_PENDING_BACKLOG"), 1)
        self.assertNotIn("SUM_MISMATCH", first["diagnostics"])
        second = self.run_pass(max_rows=3)
        self.assertEqual(
            (second["partial"], second["backlog_rows"], second["scan_cursor"]), (True, 1, 6)
        )
        third = self.run_pass(max_rows=3)
        self.assertEqual(
            (third["partial"], third["backlog_rows"], third["scan_cursor"]), (False, 0, 7)
        )
        cohort = third["cohorts"]["motor_1_arbitrage"]
        self.assertEqual(
            (cohort["realized_pnl_cents_audit"], cohort["realized_pnl_cents_source"]), (70, 70)
        )
        self.assertEqual(third["status"], "CLEAN", third["diagnostics"])
        self.assertEqual(len(self.events()), 21)  # 7 trades × PLACED/FILLED/SETTLED
        self.assertEqual(self.distinct_events(), 21)  # no duplicate identity

    def test_restart_mid_backlog_equals_an_uninterrupted_run(self):
        self.settle_rows(7)
        self.run_pass(max_rows=3)
        import importlib

        importlib.reload(audit)  # restart between pages
        for _ in range(3):
            last = audit.audit_pass(self.source, self.state, now=NOW, max_rows=3)
        interrupted = self.events()
        # Same source audited in one uninterrupted, unpaged pass into a fresh state.
        fresh = self.dir / "fresh.sqlite3"
        whole = audit.audit_pass(self.source, fresh, now=NOW, max_rows=100)
        with sqlite3.connect(fresh) as con:
            uninterrupted = con.execute(
                "SELECT client_order_id, transition, payload_json FROM audit_events ORDER BY 1, 2"
            ).fetchall()
        self.assertEqual(interrupted, uninterrupted)
        self.assertEqual(last["cohorts"], whole["cohorts"])

    def test_rows_appended_after_the_limit_are_projected(self):
        self.settle_rows(3)
        self.run_pass(max_rows=3)
        self.settle_rows(2, start=4)
        report = self.run_pass(max_rows=3)
        self.assertEqual(report["scan_cursor"], 5)
        self.assertEqual(report["cohorts"]["motor_1_arbitrage"]["realized_pnl_cents_audit"], 50)

    def test_open_rows_are_revisited_while_a_backlog_is_pending(self):
        self.add("p1", status="pending", fill=None, fee=None)
        self.settle_rows(6, start=2)
        self.run_pass(max_rows=3)  # sees p1 pending + t2, t3
        self.update(
            "p1",
            status="settled",
            fill_price_cents=45,
            fees_cents=1,
            pnl_cents=5,
            settled_at="2026-09-23 13:40:00",
        )
        self.run_pass(max_rows=3)  # scan moves on; p1 comes back through the open page
        settled = {c for c, t, _ in self.events() if t == "SETTLED"}
        self.assertIn("p1", settled)

    def test_sweep_eventually_reverifies_terminal_rows(self):
        self.settle_rows(6)
        for _ in range(2):
            self.run_pass(max_rows=3)  # backlog consumed; t1 was projected long ago
        # A terminal row on the SECOND page, rewritten behind the scan cursor: only a sweep
        # that rotates past page 1 can ever come back to it.
        self.update("t5", pnl_cents=999)
        seen = []
        for _ in range(3):  # at most ceil(6/3)+1 passes for the sweep to come around
            seen.append(self.run_pass(max_rows=3)["diagnostics"].get("CONTRADICTION", 0))
        self.assertGreaterEqual(sum(seen), 1)
        [(_, _, payload)] = [e for e in self.events() if e[0] == "t5" and e[1] == "SETTLED"]
        self.assertIn('"pnl_cents": 10', payload)  # first observation kept

    def test_lost_cursor_mid_backlog_rebuilds_without_duplicates(self):
        self.settle_rows(7)
        self.run_pass(max_rows=3)
        with sqlite3.connect(self.state) as con:
            con.execute("DELETE FROM audit_cursor")
        for _ in range(4):
            report = self.run_pass(max_rows=3)
        self.assertEqual(self.distinct_events(), len(self.events()))
        self.assertEqual(len(self.events()), 21)
        self.assertEqual(report["cohorts"]["motor_1_arbitrage"]["realized_pnl_cents_audit"], 70)


class DiagnosticsTests(AuditTestCase):
    def test_unknown_fee_is_explicit_and_never_recomputed(self):
        self.add("a", status="filled", fee=None)
        report = self.run_pass()
        self.assertEqual(report["diagnostics"].get("FEE_UNKNOWN"), 1)
        [(_, _, payload)] = [e for e in self.events() if e[1] == "FILLED"]
        self.assertIn('"fee_state": "UNKNOWN"', payload)
        self.assertIn('"fees_cents": null', payload)

    def test_contradictory_update_keeps_the_first_observation(self):
        self.add("a", status="settled", pnl=10, settled="2026-09-23 13:30:00")
        self.run_pass()
        self.update("a", pnl_cents=999)  # the past rewritten
        report = self.run_pass()
        self.assertGreaterEqual(report["diagnostics"].get("CONTRADICTION", 0), 1)
        [(_, _, payload)] = [e for e in self.events() if e[1] == "SETTLED"]
        self.assertIn('"pnl_cents": 10', payload)
        self.assertEqual(report["diagnostics"].get("SUM_MISMATCH"), 1)

    def test_rewritten_fill_on_an_open_trade_is_a_contradiction(self):
        """Non-terminal row, so only the EVENT-level comparison can catch it."""
        self.add("a", status="filled", fill=45)
        self.run_pass()
        self.update("a", fill_price_cents=50)
        report = self.run_pass()
        self.assertEqual(report["diagnostics"].get("CONTRADICTION"), 1)
        [(_, _, payload)] = [e for e in self.events() if e[1] == "FILLED"]
        self.assertIn('"fill_price_cents": 45', payload)

    def test_status_regression_is_reported(self):
        self.add("a", status="settled", pnl=10, settled="2026-09-23 13:30:00")
        self.run_pass()
        self.update("a", status="filled", pnl_cents=None, settled_at=None)
        self.assertEqual(self.run_pass()["diagnostics"].get("STATUS_REGRESSION"), 1)

    def test_unmappable_rows_are_specific_not_invented(self):
        self.add("a", status="settled", pnl=None, settled="2026-09-23 13:30:00")
        self.add("b", status="teleported", fill=None, fee=None)
        self.add("c", status="filled", fill=None)
        report = self.run_pass()
        self.assertEqual(report["diagnostics"].get("INCOMPATIBLE_ROW"), 2)  # a: pnl, c: fill
        self.assertEqual(report["diagnostics"].get("INCOMPATIBLE_STATUS"), 1)
        self.assertFalse([e for e in self.events() if e[1] == "SETTLED"])

    def test_id_gap_and_source_replacement_are_diagnosed(self):
        self.add("a", id=1)
        self.add("b", id=4)
        report = self.run_pass()
        self.assertEqual(report["diagnostics"].get("ID_GAP"), 1)
        # The source is replaced by an older/other copy: cursor kept, flagged.
        with sqlite3.connect(self.source) as con:
            con.execute("DELETE FROM trades")
        self.next_id = 1
        self.add("z", id=1)
        report = self.run_pass()
        self.assertEqual(report["diagnostics"].get("SOURCE_CHANGED"), 1)
        # Two distinct facts: the global scan cursor AND the cohort cursor are ahead.
        self.assertEqual(report["diagnostics"].get("CURSOR_AHEAD_OF_SOURCE"), 2)
        self.assertEqual(report["cohorts"]["motor_1_arbitrage"]["cursor_last_id"], 4)

    def test_partial_pass_is_explicit(self):
        for i in range(5):
            self.add(f"t{i}")
        report = self.run_pass(max_rows=3)
        self.assertTrue(report["partial"])
        self.assertEqual(report["diagnostics"].get("PARTIAL_PASS"), 1)

    def test_state_write_failure_commits_nothing(self):
        self.add("a", status="settled", pnl=10, settled="2026-09-23 13:30:00")
        self.add("b", status="settled", pnl=5, settled="2026-09-23 13:31:00")
        real = audit._fingerprint
        calls = []

        def flaky(payload):
            calls.append(1)
            if len(calls) == 5:  # after the first trade's events were inserted
                raise RuntimeError("disk")
            return real(payload)

        with (
            patch.object(audit, "_fingerprint", side_effect=flaky),
            self.assertRaisesRegex(audit.AuditError, "STATE_WRITE_FAILED"),
        ):
            self.run_pass()
        with sqlite3.connect(self.state) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM audit_cursor").fetchone()[0], 0)

    def test_missing_or_incompatible_source_is_an_error(self):
        with self.assertRaisesRegex(audit.AuditError, "SOURCE_MISSING"):
            audit.audit_pass(self.dir / "nope.db", self.state, now=NOW)
        other = self.dir / "other.db"
        with sqlite3.connect(other) as con:
            con.execute("CREATE TABLE trades (id INTEGER, client_order_id TEXT)")
        with self.assertRaisesRegex(audit.AuditError, "SOURCE_SCHEMA_INCOMPATIBLE"):
            audit.audit_pass(other, self.state, now=NOW)


class NoAuthorityTests(AuditTestCase):
    def test_the_source_is_never_modified(self):
        self.add("a", status="settled", pnl=10, settled="2026-09-23 13:30:00")
        with sqlite3.connect(self.source) as con:
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        for _ in range(3):
            self.run_pass()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_the_source_is_opened_mode_ro(self):
        self.add("a")
        real = sqlite3.connect
        with patch.object(audit.sqlite3, "connect", side_effect=real) as connect:
            self.run_pass()
        source_calls = [c for c in connect.call_args_list if str(c.args[0]).startswith("file:")]
        self.assertEqual(len(source_calls), 1)
        self.assertTrue(source_calls[0].args[0].endswith("?mode=ro"))
        self.assertIs(source_calls[0].kwargs.get("uri"), True)
        self.assertFalse([c for c in connect.call_args_list if str(c.args[0]) == str(self.source)])

    def test_read_only_source_file_is_enough(self):
        self.add("a", status="settled", pnl=10, settled="2026-09-23 13:30:00")
        ro_dir = self.dir / "ro"
        ro_dir.mkdir()
        copy = ro_dir / "trades.db"
        shutil.copy(self.source, copy)
        copy.chmod(0o444)
        ro_dir.chmod(0o555)
        self.addCleanup(ro_dir.chmod, 0o755)
        report = audit.audit_pass(copy, self.state, now=NOW)
        self.assertEqual(report["cohorts"]["motor_1_arbitrage"]["realized_pnl_cents_audit"], 10)

    def test_module_has_no_authority_or_network(self):
        source = (BASE / "production_audit.py").read_text()
        for forbidden in (
            "init_bank",
            "simulation_bank",
            "place_order",
            "set_pause",
            "urllib",
            "socket",
            "requests",
            "httpx",
            "UPDATE trades",
            "DELETE FROM trades",
            "INSERT INTO trades",
        ):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
