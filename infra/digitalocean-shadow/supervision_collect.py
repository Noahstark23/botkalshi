"""Root pre-step of the supervision units: copy the production evidence into the
supervisor's private inbox. READ-ONLY on everything it does not own.

Encargo ASTRA-DEPLOY-SUPERVISION-20260924. The live runtime keeps its snapshot
directory 0700 and its DB private; granting the supervisor ACLs on live data would
change the live host's permissions (and `export_snapshot` re-chmods its directory,
which would silently drop an ACL mask anyway). So a root `ExecStartPre=+` step reads
the two sources and hands the supervisor COPIES it owns:

  - the S1 snapshot (`BOTKALSHI_LIVE_SNAPSHOT`): copied byte for byte if it is a regular
    file of bounded size; if it disappeared, the stale copy is REMOVED so the reader
    reports MISSING instead of an old state;
  - the `trades` table only (`BOTKALSHI_LIVE_DB`): read through sqlite `mode=ro` and
    written to a fresh SQLite in the inbox — never the rest of the DB, never a write to
    the source.

The paths come from an operator-owned env file (root-owned, not group/other writable)
that may contain ONLY those two keys: no secret can ride along. Nothing here starts,
stops, pauses or configures any service. Stdlib only.

Usage (as the unit's root pre-step):
    python3 -I supervision_collect.py --env /etc/botkalshi-supervision.env \
        --inbox /var/lib/botkalshi-supervision/inbox --owner botkalshi-supervisor
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import sqlite3
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

ALLOWED_KEYS = ("BOTKALSHI_LIVE_DB", "BOTKALSHI_LIVE_SNAPSHOT")
MAX_SNAPSHOT_BYTES = 1_000_000
MAX_TRADES_ROWS = 2_000_000


class CollectError(Exception):
    pass


def read_env(path: Path, *, require_root_owner: bool = True) -> dict[str, str]:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise CollectError("ENV_MISSING") from exc
    if not stat.S_ISREG(info.st_mode):
        raise CollectError("ENV_NOT_REGULAR")
    if require_root_owner and info.st_uid != 0:
        raise CollectError("ENV_NOT_ROOT_OWNED")
    if info.st_mode & 0o022:
        raise CollectError("ENV_WRITABLE_BY_OTHERS")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if not sep or key not in ALLOWED_KEYS:
            raise CollectError("ENV_UNEXPECTED_KEY")  # the key name is not echoed
        if not value.startswith("/") or ".." in value.split("/"):
            raise CollectError(f"ENV_PATH_NOT_ABSOLUTE:{key}")
        values[key] = value
    missing = [k for k in ALLOWED_KEYS if k not in values]
    if missing:
        raise CollectError(f"ENV_INCOMPLETE:{','.join(missing)}")
    return values


def _regular(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise CollectError("SOURCE_NOT_REGULAR")
    return info


def _atomic_bytes(path: Path, data: bytes, uid: int | None, gid: int | None) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if uid is not None:
        os.chown(tmp, uid, gid)
    os.replace(tmp, path)


def copy_snapshot(source: Path, inbox: Path, uid: int | None, gid: int | None) -> str:
    target = inbox / "snapshot.json"
    info = _regular(source)
    if info is None:
        target.unlink(missing_ok=True)  # never leave an old state looking current
        return "MISSING"
    if info.st_size > MAX_SNAPSHOT_BYTES:
        target.unlink(missing_ok=True)
        return "TOO_LARGE"
    _atomic_bytes(target, source.read_bytes(), uid, gid)
    return "COPIED"


def export_trades(source: Path, inbox: Path, uid: int | None, gid: int | None) -> str:
    target = inbox / "trades.db"
    if _regular(source) is None:
        target.unlink(missing_ok=True)
        return "MISSING"
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    try:
        row = src.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
        ).fetchone()
        if row is None:
            raise CollectError("SOURCE_HAS_NO_TRADES")
        tmp = target.with_name(".trades.db.tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        dst = sqlite3.connect(tmp)
        try:
            dst.execute(row[0])
            cursor = src.execute("SELECT * FROM trades ORDER BY id")
            columns = len(cursor.description)
            copied = 0
            while batch := cursor.fetchmany(5000):
                copied += len(batch)
                if copied > MAX_TRADES_ROWS:
                    raise CollectError("TRADES_TOO_MANY_ROWS")
                dst.executemany(f"INSERT INTO trades VALUES ({','.join('?' * columns)})", batch)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    if uid is not None:
        os.chown(tmp, uid, gid)
    os.replace(tmp, target)
    return "EXPORTED"


def collect(env: Path, inbox: Path, owner: str | None, *, require_root_owner: bool = True) -> dict:
    values = read_env(env, require_root_owner=require_root_owner)
    uid = gid = None
    if owner is not None:
        entry = pwd.getpwnam(owner)
        uid, gid = entry.pw_uid, entry.pw_gid
    inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(inbox, 0o700)
    if uid is not None:
        os.chown(inbox, uid, gid)
    status = {
        "schema_version": "botkalshi-supervision-collect-v1",
        "collected_at": datetime.now(UTC).isoformat(),
        "snapshot": copy_snapshot(Path(values["BOTKALSHI_LIVE_SNAPSHOT"]), inbox, uid, gid),
        "trades": export_trades(Path(values["BOTKALSHI_LIVE_DB"]), inbox, uid, gid),
        "authority": "NONE",
    }
    _atomic_bytes(inbox / "collect-status.json", json.dumps(status).encode(), uid, gid)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="copy production evidence to the inbox")
    parser.add_argument("--env", required=True, type=Path)
    parser.add_argument("--inbox", required=True, type=Path)
    parser.add_argument("--owner")
    args = parser.parse_args(argv)
    try:
        status = collect(args.env, args.inbox, args.owner)
    except (CollectError, sqlite3.Error, KeyError, OSError) as exc:
        # Fail closed: no stale copy may pass for current evidence.
        for name in ("snapshot.json", "trades.db"):
            (args.inbox / name).unlink(missing_ok=True)
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)[:120]}), file=sys.stderr)
        return 3
    print(json.dumps(status, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
