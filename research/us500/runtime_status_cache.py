"""Copia saneada del runtime: productor sin token y lector sin red al runtime."""

from __future__ import annotations

import argparse
import json
import os
import signal
import stat
import tempfile
import threading
import time
from pathlib import Path

from motor_status_readonly import (
    MAX_BYTES,
    RuntimeStatusReader,
    _constant,
    _unique,
    sanitize_snapshot,
    unknown_status,
)

CACHE_PATH = Path("/var/lib/botkalshi-status/status.json")


class SnapshotStatusReader:
    def __init__(self, path=CACHE_PATH, *, trusted_uid=None):
        self.path = Path(path)
        self.trusted_uid = trusted_uid

    def __call__(self, *, now_ms):
        try:
            parent = os.open(
                self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                info = os.fstat(parent)
                owner = (
                    self.trusted_uid if self.trusted_uid is not None else info.st_uid
                )
                # El consumidor no puede escribir el directorio del productor.
                if (
                    info.st_uid != owner
                    or info.st_mode & 0o022
                    or (self.trusted_uid is None and owner == os.getuid())
                ):
                    return unknown_status(now_ms, "INVALID")
                fd = os.open(
                    self.path.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=parent,
                )
                with os.fdopen(fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != owner
                        or info.st_mode & 0o022
                    ):
                        return unknown_status(now_ms, "INVALID")
                    raw = source.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    return unknown_status(now_ms, "INVALID")
            finally:
                os.close(parent)
            return sanitize_snapshot(
                json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant),
                now_ms=now_ms,
            )
        except Exception:
            return unknown_status(now_ms, "UNAVAILABLE")


def publish(path, snapshot, *, now_ms):
    path = Path(path)
    data = json.dumps(
        sanitize_snapshot(snapshot, now_ms=now_ms), sort_keys=True, allow_nan=False
    ).encode()
    if len(data) > MAX_BYTES or path.is_symlink() or path.parent.is_symlink():
        raise ValueError("INVALID_STATUS_CACHE")
    fd, temporary = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=CACHE_PATH)
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    reader = RuntimeStatusReader()
    while not stop.is_set():
        now = time.time_ns() // 1_000_000
        publish(args.output, reader(now_ms=now), now_ms=now)
        stop.wait(15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
