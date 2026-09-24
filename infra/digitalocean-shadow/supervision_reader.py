"""Read-only supervision consumer of the PRODUCTION snapshot (S3) + technical alerts.

Encargo ASTRA-SUPERVISION-20260923. Reads the S1 snapshot the production runtime
writes (`src/monitoring/supervision_snapshot.py`, schema
`botkalshi-production-snapshot-v1`) from a private FILE — fetched by the operator's
already-approved channel, or through `fetch_snapshot_via_ssh`, which is only an
adapter here and is never run from tests. It never exposes an API.

It is read-only by construction. A suggested decision here never calls
`assistant_bridge.set_pause`, `/admin/resume`, activation scripts or any executor, and
it never uses `_assess_locked` (whose PAUSE_RESEARCH branch writes control state). The
bot's runtime does not read anything this module writes, so a supervisor, AI or
reader outage cannot change a trading decision (test-guarded).

The contract is the producer's own `validate_snapshot`: one definition, no copy that
could drift. A snapshot from the SIMULATION domain, from another schema, corrupt,
expired or missing is never read as healthy.

Technical alerts: SERVICE_DOWN (no snapshot, or expired), SNAPSHOT_CORRUPT,
WRONG_DOMAIN, FEED_STALE, BALANCE_EVIDENCE_LOST (possible auth loss: the balance read
is failing while the runtime writes), LEDGER_INCONSISTENT, KILL_SWITCH_ENGAGED and
AUDIT_DIAGNOSTICS (from the S2 auditor report).
  - Dedup: a firing alert is sent once, then only after its backoff expires
    (5 min × 3ⁿ, capped at 6 h) — a flapping source cannot multiply messages.
  - Recovery requires NEW evidence: an alert resolves only on a snapshot newer than the
    one it fired on. The same file read again proves nothing.
  - Redaction: messages are built only from codes, counts and timestamps; a final
    secret scan drops any message that still matches a secret pattern.
Stdlib only; no network except the optional transport adapter.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import repo_root  # noqa: F401 — repo root on sys.path (system python3)

from src.monitoring.supervision_snapshot import (
    LEDGER_DOMAIN,
    SCHEMA_VERSION,
    contains_secret,
    validate_snapshot,
)

SCHEMA_VIEW = "botkalshi-supervision-view-v1"
SCHEMA_ALERT_STATE = "botkalshi-supervision-alert-state-v1"
MAX_SNAPSHOT_BYTES = 1_000_000
BACKOFF_BASE = timedelta(minutes=5)
BACKOFF_FACTOR = 3
BACKOFF_CAP = timedelta(hours=6)
_HOST_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")
_REMOTE_PATH_RE = re.compile(r"/[A-Za-z0-9_./-]{1,200}/supervision/latest\.json")

SEVERITY = {
    "SERVICE_DOWN": "critical",
    "SNAPSHOT_CORRUPT": "critical",
    "WRONG_DOMAIN": "critical",
    "FEED_STALE": "warning",
    "BALANCE_EVIDENCE_LOST": "warning",
    "LEDGER_INCONSISTENT": "warning",
    "KILL_SWITCH_ENGAGED": "critical",
    "AUDIT_DIAGNOSTICS": "warning",
}


def _aware(text: object) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.astimezone(UTC) if moment.tzinfo else None


# --------------------------------------------------------------------------------------
# Reading the snapshot.
# --------------------------------------------------------------------------------------


def read_snapshot(path: Path, *, now: datetime) -> dict[str, Any]:
    """The supervisor's view of one snapshot file. Never raises for bad input: the
    problem IS the answer (MISSING / CORRUPT / WRONG_DOMAIN / EXPIRED / …)."""
    now = now.astimezone(UTC)
    view: dict[str, Any] = {
        "schema_version": SCHEMA_VIEW,
        "read_at": now.isoformat(),
        "source_file": path.name,
        "state": None,
        "problems": [],
        "snapshot": None,
        "authority": "NONE",
    }
    try:
        if path.is_symlink():
            return {**view, "state": "CORRUPT", "problems": ["SYMLINK"]}
        data = path.read_bytes()
    except FileNotFoundError:
        return {**view, "state": "MISSING"}
    except OSError as exc:
        return {**view, "state": "UNREADABLE", "problems": [type(exc).__name__]}
    if len(data) > MAX_SNAPSHOT_BYTES:
        return {**view, "state": "CORRUPT", "problems": ["TOO_LARGE"]}
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {**view, "state": "CORRUPT", "problems": ["NOT_JSON"]}
    if isinstance(doc, dict) and (
        doc.get("schema_version") != SCHEMA_VERSION or doc.get("ledger_domain") != LEDGER_DOMAIN
    ):
        # A simulation artifact (or another schema) in the production slot.
        return {**view, "state": "WRONG_DOMAIN", "problems": ["NOT_PRODUCTION_SNAPSHOT"]}
    problems = validate_snapshot(doc)
    if problems:
        return {**view, "state": "CORRUPT", "problems": problems[:10]}
    valid_until = _aware(doc["valid_until"])
    captured = _aware(doc["captured_at"])
    if captured is None or valid_until is None or captured > now + timedelta(seconds=60):
        return {**view, "state": "CORRUPT", "problems": ["BAD_TIMESTAMPS"]}
    view["snapshot"] = doc
    if now > valid_until:
        view["state"] = "EXPIRED"
    else:
        view["state"] = doc["health"]["status"]  # OK | DEGRADED | UNKNOWN, from the producer
    return view


def fetch_snapshot_via_ssh(
    host_alias: str,
    remote_path: str,
    destination: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> Path:
    """ADAPTER for the operator's already-approved SSH channel. NOT run by tests or by
    this session. `host_alias` is an ~/.ssh/config Host entry: keys, users and host
    names live there, never in arguments. StrictHostKeyChecking stays `yes`; BatchMode
    forbids prompts. The remote path must be a supervision/latest.json."""
    if not _HOST_ALIAS_RE.fullmatch(host_alias):
        raise ValueError("host alias must be a plain ssh config alias")
    if not _REMOTE_PATH_RE.fullmatch(remote_path) or ".." in remote_path:
        raise ValueError("remote path must be a supervision/latest.json")
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=10",
        host_alias,
        "cat",
        "--",
        remote_path,
    ]
    done = runner(argv, capture_output=True, timeout=30, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"ssh fetch failed with exit {done.returncode}")
    body = done.stdout
    if not isinstance(body, bytes) or len(body) > MAX_SNAPSHOT_BYTES:
        raise RuntimeError("ssh fetch returned an invalid body")
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_name(f".{destination.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, destination)
    return destination


# --------------------------------------------------------------------------------------
# Alert conditions (pure).
# --------------------------------------------------------------------------------------


def conditions(view: dict[str, Any], audit_report: dict[str, Any] | None = None) -> dict[str, str]:
    """{alert_code: short allowlisted detail} currently true for this view."""
    out: dict[str, str] = {}
    state = view.get("state")
    if state in ("MISSING", "UNREADABLE"):
        out["SERVICE_DOWN"] = f"snapshot {state.lower()}"
    elif state == "EXPIRED":
        out["SERVICE_DOWN"] = "snapshot expired (producer not writing)"
    elif state == "CORRUPT":
        out["SNAPSHOT_CORRUPT"] = ",".join(str(p)[:40] for p in view.get("problems", [])[:3])
    elif state == "WRONG_DOMAIN":
        out["WRONG_DOMAIN"] = "non-production snapshot in the production slot"
    doc = view.get("snapshot")
    if isinstance(doc, dict) and state not in ("EXPIRED",):
        if doc["feed"]["status"] != "OK":
            out["FEED_STALE"] = f"feed {doc['feed']['status']}"
        if doc["balance"]["status"] in ("UNKNOWN", "STALE", "ERROR"):
            out["BALANCE_EVIDENCE_LOST"] = f"balance {doc['balance']['status']}"
        if doc["coherence"]["status"] != "OK":
            bad = sorted(k for k, v in doc["coherence"]["checks"].items() if v)
            out["LEDGER_INCONSISTENT"] = (
                f"coherence {doc['coherence']['status']} {','.join(bad)[:80]}".strip()
            )
        if doc["controls"]["kill_switch"]["state"] == "ENGAGED":
            out["KILL_SWITCH_ENGAGED"] = "kill switch engaged"
    if isinstance(audit_report, dict) and audit_report.get("status") == "DIAGNOSTICS":
        kinds = audit_report.get("diagnostics") or {}
        serious = sorted(k for k in kinds if k != "PARTIAL_PASS")
        if serious:
            out["AUDIT_DIAGNOSTICS"] = ",".join(serious)[:80]
    return out


# --------------------------------------------------------------------------------------
# Dedup / backoff / recovery, persisted.
# --------------------------------------------------------------------------------------


class Transport(Protocol):
    def send(self, text: str) -> bool: ...


@dataclass
class FakeTransport:
    """For tests and dry runs: records, never sends."""

    sent: list[str]
    fail: bool = False

    def send(self, text: str) -> bool:
        if self.fail:
            return False
        self.sent.append(text)
        return True


class OutboxTransport:
    """Default in deployment: 'delivers' to a bounded local JSONL outbox (0600) that the
    operator reads. No network, no credentials — alerts are never lost, never sent out."""

    def __init__(self, path: Path, *, max_lines: int = 500) -> None:
        self.path = path
        self.max_lines = max_lines

    def send(self, text: str) -> bool:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lines = self.path.read_text(encoding="utf-8").splitlines() if self.path.exists() else []
        entry = json.dumps({"at": datetime.now(UTC).isoformat(), "text": text})
        lines = [*lines[-(self.max_lines - 1) :], entry]
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)
        return True


class TelegramTransport:
    """Production adapter over the existing research notifier's credential mechanism
    (systemd credentials; never arguments). Not exercised by tests."""

    def send(self, text: str) -> bool:
        import telegram_notifier

        token, chat_id = telegram_notifier._safe_environment()
        return telegram_notifier._send_message(token, chat_id, text) == 200


def _load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": SCHEMA_ALERT_STATE, "alerts": {}}
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_ALERT_STATE:
        return {"schema_version": SCHEMA_ALERT_STATE, "alerts": {}}
    return state


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _message(code: str, detail: str, kind: str, release: str) -> str | None:
    text = f"[botkalshi supervision] {kind} {SEVERITY.get(code, 'warning')} {code}: {detail} (release {release[:12]})"
    return None if contains_secret(text) else text[:500]


def process_alerts(
    view: dict[str, Any],
    *,
    state_path: Path,
    transport: Transport,
    now: datetime,
    audit_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate conditions against the persisted alert state; send what dedup/backoff
    allow; resolve only on NEW evidence. Returns what happened (for logs/reports)."""
    now = now.astimezone(UTC)
    state = _load_state(state_path)
    alerts: dict[str, Any] = state["alerts"]
    doc = view.get("snapshot") if isinstance(view.get("snapshot"), dict) else {}
    evidence_id = doc.get("snapshot_id")
    evidence_at = doc.get("captured_at")
    release = str(doc.get("release_sha") or "UNKNOWN")
    active = conditions(view, audit_report)
    outcome = {"sent": [], "suppressed": [], "resolved": [], "held": [], "dropped": []}

    for code, detail in sorted(active.items()):
        entry = alerts.get(code)
        if entry is None or entry["status"] == "RESOLVED":
            entry = {
                "status": "FIRING",
                "first_seen": now.isoformat(),
                "sends": 0,
                "next_allowed": now.isoformat(),
                "evidence_id": evidence_id,
                "evidence_at": evidence_at,
            }
            alerts[code] = entry
        entry["last_seen"] = now.isoformat()
        entry["evidence_id"] = evidence_id or entry.get("evidence_id")
        entry["evidence_at"] = evidence_at or entry.get("evidence_at")
        if now < _aware(entry["next_allowed"]):
            outcome["suppressed"].append(code)
            continue
        text = _message(code, detail, "FIRING", release)
        if text is None:
            outcome["dropped"].append(code)
            continue
        if transport.send(text):
            entry["sends"] += 1
            delay = min(BACKOFF_BASE * (BACKOFF_FACTOR ** (entry["sends"] - 1)), BACKOFF_CAP)
            entry["next_allowed"] = (now + delay).isoformat()
            outcome["sent"].append(code)
        else:
            outcome["suppressed"].append(code)  # transport down: retry next run, no backoff

    for code, entry in sorted(alerts.items()):
        if code in active or entry["status"] != "FIRING":
            continue
        fired_at = _aware(entry.get("evidence_at"))
        new_at = _aware(evidence_at)
        newer = (
            evidence_id is not None
            and evidence_id != entry.get("evidence_id")
            and new_at is not None
            and (fired_at is None or new_at > fired_at)
        )
        if not newer:
            outcome["held"].append(code)  # no new evidence: recovery not proven
            continue
        entry.update(status="RESOLVED", resolved_at=now.isoformat(), resolved_by=evidence_id)
        text = _message(code, "recovered on newer snapshot", "RESOLVED", release)
        if text is not None:
            transport.send(text)  # best-effort notice; the state change stands either way
        outcome["resolved"].append(code)
    _save_state(state_path, state)
    return outcome


def main(argv: list[str] | None = None) -> int:
    """One read-only supervision run: read the snapshot (and the audit report), evaluate
    alerts with dedup/backoff, deliver to the LOCAL outbox, write a bounded report."""
    import argparse

    parser = argparse.ArgumentParser(description="read-only production supervision run")
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--outbox", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    view = read_snapshot(args.snapshot, now=now)
    audit = None
    if args.audit is not None:
        try:
            audit = json.loads(args.audit.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            audit = {"status": "DIAGNOSTICS", "diagnostics": {"AUDIT_REPORT_UNREADABLE": 1}}
    outcome = process_alerts(
        view,
        state_path=args.state,
        transport=OutboxTransport(args.outbox),
        now=now,
        audit_report=audit,
    )
    doc = view.get("snapshot") or {}
    report = {
        "schema_version": SCHEMA_VIEW + "+run",
        "read_at": view["read_at"],
        "view_state": view["state"],
        "problems": view["problems"],
        "snapshot_id": doc.get("snapshot_id"),
        "release_sha": doc.get("release_sha"),
        "alert_conditions": conditions(view, audit),
        "alerts": outcome,
        "authority": "NONE",
        "commands_executed": [],
    }
    args.report.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = args.report.with_name(f".{args.report.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, args.report)
    print(json.dumps({k: report[k] for k in ("view_state", "alert_conditions")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
