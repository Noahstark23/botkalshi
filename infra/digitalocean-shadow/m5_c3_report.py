"""C3 report for the research service: what the PUBLIC cycles actually showed. Read-only.

Usage (on the research droplet):

    python3 m5_c3_report.py --data /var/lib/botkalshi-research [--cycles 10]
    python3 m5_c3_report.py --data DIR --save-restart-mark mark.json     # before restart
    python3 m5_c3_report.py --data DIR --check-restart-mark mark.json    # after restart

Keeps the five areas SEPARATE, never merged into one verdict:
  - m5_local_tested   — not measurable here: it is the test suite's evidence (CIERRE);
  - m5_public         — what the recorded public cycles show (blocked states included);
  - m1_observer       — the M1 book review, diagnostic only (proposes no risk);
  - radar             — NOT_CONNECTED (no producer in the code);
  - exits_settlements — PENDING (no simulated exit or settlement of M5 positions yet).

Two different counts, never confused (review of da71325):
  - RECORDED cycles: every report with a cycle id, whatever its status. A cycle blocked
    by BLOCKED_NO_FAIR is recorded honestly — but it proves nothing about the circuit.
  - QUALIFYING cycles (what C3 accredits): status OK, no errors, no inputs problem, and
    evidence that the circuit was USABLE in that cycle — a proposal that reached the
    bank's decision (so fair, fee and book were all valid then), or an EVALUATED live
    quote whose fee was verified for that observation. Fills are NOT required: a usable
    cycle that crossed nothing still qualifies.
C3 needs >= 10 distinct qualifying cycle ids in the analysed window.
The restart mark hashes every stored event and admission up to the last `seq` before
the restart; after it, the same prefix must hash identically (nothing lost or rewritten).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any

SCHEMA = "botkalshi-m5-c3-report-v1"
BANK_FILE = "simulation-bank.sqlite3"


def _cycles(data: Path, limit: int | None) -> list[dict]:
    path = data / "m5" / "cycles.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            rows.append({"status": "UNREADABLE_LINE"})
    return rows[-limit:] if limit else rows


def _ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _bank_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"status": "NO_BANK"}
    con = _ro(path)
    try:
        capital = con.execute(
            "SELECT initial_capital_cents FROM simulation_bank WHERE id = 1"
        ).fetchone()
        reserved = con.execute(
            "SELECT COALESCE(SUM(amount_cents), 0) FROM simulation_reservations WHERE status='ACTIVE'"
        ).fetchone()[0]
        events = con.execute(
            "SELECT COUNT(*), COALESCE(MAX(seq), 0) FROM simulation_events"
        ).fetchone()
        breaches = con.execute(
            "SELECT breach, COUNT(*) FROM simulation_events WHERE breach IS NOT NULL GROUP BY breach"
        ).fetchall()
        unresolved = con.execute(
            "SELECT COUNT(*) FROM simulation_quote_observations "
            "WHERE outcome='UNVERIFIED' AND resolved_at IS NULL"
        ).fetchone()[0]
    finally:
        con.close()
    return {
        "status": "OK",
        "initial_capital_cents": capital[0] if capital else None,
        "reserved_cents": reserved,
        "events": events[0],
        "max_seq": events[1],
        "breaches": dict(breaches),
        "unresolved_quote_observations": unresolved,
        "note": "Realized P&L is derived by the bank on read; see simulation_bank.get_snapshot.",
    }


def restart_mark(path: Path) -> dict[str, Any]:
    con = _ro(path)
    try:
        max_seq = con.execute("SELECT COALESCE(MAX(seq), 0) FROM simulation_events").fetchone()[0]
        digest = hashlib.sha256()
        for row in con.execute(
            "SELECT * FROM simulation_events WHERE seq <= ? ORDER BY seq", (max_seq,)
        ):
            digest.update(json.dumps(row, default=str).encode())
        keys = [
            r[0]
            for r in con.execute(
                "SELECT admission_key FROM simulation_admissions ORDER BY admission_key"
            )
        ]
        adm = hashlib.sha256()
        for (row,) in con.execute(
            "SELECT json_array(admission_key, origin, position_key, risk_cents, decision, "
            "reservation_key, evidence_json) FROM simulation_admissions ORDER BY admission_key"
        ):
            adm.update(row.encode())
    finally:
        con.close()
    return {
        "max_seq": max_seq,
        "events_sha256": digest.hexdigest(),
        "admission_keys": keys,
        "admissions_sha256": adm.hexdigest(),
    }


def check_restart(path: Path, mark: dict[str, Any]) -> dict[str, Any]:
    con = _ro(path)
    try:
        digest = hashlib.sha256()
        for row in con.execute(
            "SELECT * FROM simulation_events WHERE seq <= ? ORDER BY seq", (mark["max_seq"],)
        ):
            digest.update(json.dumps(row, default=str).encode())
        adm = hashlib.sha256()
        marks = ",".join("?" for _ in mark["admission_keys"]) or "''"
        for (row,) in con.execute(
            "SELECT json_array(admission_key, origin, position_key, risk_cents, decision, "
            f"reservation_key, evidence_json) FROM simulation_admissions WHERE admission_key IN ({marks}) "
            "ORDER BY admission_key",
            mark["admission_keys"],
        ):
            adm.update(row.encode())
    finally:
        con.close()
    events_ok = digest.hexdigest() == mark["events_sha256"]
    adm_ok = adm.hexdigest() == mark["admissions_sha256"]
    return {
        "events_prefix_identical": events_ok,
        "admissions_identical": adm_ok,
        "verdict": "PRESERVED" if events_ok and adm_ok else "CHANGED",
    }


C3_MIN_QUALIFYING_CYCLES = 10
_DECIDED = frozenset({"ADMITTED", "REJECTED"})
_VERIFIED_FEE = frozenset({"OK", "CHANGED"})


def qualification(row: dict) -> str | None:
    """None if this cycle qualifies for C3; otherwise the first reason it does not."""
    if not row.get("cycle_id"):
        return "NO_CYCLE_ID"
    if row.get("status") != "OK":
        return f"STATUS_{row.get('status')}"
    if row.get("errors"):
        return "ERRORS"
    if row.get("inputs_problem"):
        return f"INPUTS_{row['inputs_problem']}"
    if any(p.get("decision") in _DECIDED for p in row.get("proposals") or []):
        return None
    if any(
        e.get("status") == "EVALUATED" and e.get("fee_state") in _VERIFIED_FEE
        for e in row.get("evaluations") or []
    ):
        return None
    blocked = [reasons[0] for reasons in (row.get("blocked") or {}).values() if reasons]
    if blocked:
        return f"ALL_BLOCKED_{Counter(blocked).most_common(1)[0][0]}"
    return "NO_USABLE_CIRCUIT"


def build_report(data: Path, *, cycles: int | None = None) -> dict[str, Any]:
    rows = _cycles(data, cycles)
    statuses = Counter(r.get("status") for r in rows)
    blocked = Counter()
    withdrawals = Counter()
    proposals = Counter()
    evaluations = Counter()
    fills = 0
    for r in rows:
        for reasons in (r.get("blocked") or {}).values():
            blocked[reasons[0] if reasons else "?"] += 1
        for w in r.get("withdrawals") or []:
            withdrawals[w.get("reason")] += 1
        for p in r.get("proposals") or []:
            proposals["ACTIVE" if p.get("active") else p.get("decision", "?")] += 1
        for e in r.get("evaluations") or []:
            evaluations[e.get("status")] += 1
            fills += len(e.get("fills") or []) if e.get("status") == "EVALUATED" else 0
    cycle_ids = [r.get("cycle_id") for r in rows if r.get("cycle_id")]
    not_qualifying = Counter()
    qualifying_ids = set()
    for r in rows:
        reason = qualification(r)
        if reason is None:
            qualifying_ids.add(r["cycle_id"])
        else:
            not_qualifying[reason] += 1
    m1 = None
    m1_path = data / "m1" / "latest.json"
    if m1_path.exists():
        try:
            review = json.loads(m1_path.read_text(encoding="utf-8"))
            m1 = {
                "cycle_id": review.get("cycle_id"),
                "statuses": dict(Counter(x.get("status") for x in review.get("reviews") or [])),
            }
        except ValueError:
            m1 = {"status": "UNREADABLE"}
    producer = rows[-1].get("inputs_producer") if rows else None
    return {
        "schema_version": SCHEMA,
        "mode": "SIMULATION_ONLY",
        "execution_authorized": False,
        "m5_local_tested": "NOT_MEASURED_HERE — evidence is the test suite recorded in CIERRE",
        "m5_public": {
            "cycles_recorded": len(rows),
            "distinct_cycle_ids_recorded": len(set(cycle_ids)),
            "qualifying_cycles": len(qualifying_ids),
            "not_qualifying_reasons": dict(not_qualifying),
            "c3_min_qualifying_cycles": C3_MIN_QUALIFYING_CYCLES,
            "c3_cycles_met": len(qualifying_ids) >= C3_MIN_QUALIFYING_CYCLES,
            "fills_required_for_c3": False,
            "statuses": dict(statuses),
            "blocked_first_reason": dict(blocked),
            "proposals": dict(proposals),
            "evaluations": dict(evaluations),
            "fills_booked": fills,
            "withdrawals": dict(withdrawals),
            "last_inputs_producer": producer,
        },
        "m1_observer": m1 or {"status": "NO_REVIEW_FILE"},
        "radar": "NOT_CONNECTED",
        "exits_settlements": "PENDING",
        "bank": _bank_summary(data / BANK_FILE),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--cycles", type=int, default=None, help="window (default: all recorded)")
    parser.add_argument("--save-restart-mark", type=Path)
    parser.add_argument("--check-restart-mark", type=Path)
    args = parser.parse_args(argv)
    bank_path = args.data / BANK_FILE
    if args.save_restart_mark:
        args.save_restart_mark.write_text(json.dumps(restart_mark(bank_path), indent=2))
        print(f"mark saved: {args.save_restart_mark}")
        return 0
    if args.check_restart_mark:
        result = check_restart(bank_path, json.loads(args.check_restart_mark.read_text()))
        print(json.dumps(result, indent=2))
        return 0 if result["verdict"] == "PRESERVED" else 3
    print(json.dumps(build_report(args.data, cycles=args.cycles), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
