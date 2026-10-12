"""Bounded local supervisor for a dedicated US500 paper Telegram bot.

No service installation, Kalshi access, production launcher, or paper runner is
included. A dedicated Telegram bot or a single existing router is required.
Only explicit CLI execution reads US500_PAPER_TELEGRAM_TOKEN; importing the module
does not read credentials, create files, poll, or change control state.
"""

from __future__ import annotations

import argparse
import fcntl
import itertools
import json
import math
import os
import re
import signal
import stat
import tempfile
import threading
import time
from pathlib import Path

from us500_control import PaperControl
from us500_telegram import PaperTelegram, TelegramTransport, _safe_error

SCHEMA = "us500-paper-telegram-service-v1"
MAX_CYCLES = 10_000
MAX_STATUS_BYTES = 32_768
_CONTROL_ID = re.compile(r"[a-f0-9]{32}\Z")
_REASON = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_OUTBOX_STATES = {"PENDING", "ATTEMPTED", "SENT", "FAILED", "UNKNOWN"}
_TRANSIENT_POLL_ERRORS = {
    "TELEGRAM_RESPONSE_UNKNOWN",
    "TELEGRAM_RESPONSE_TIMEOUT",
    "TELEGRAM_RATE_LIMITED",
    "CONTROL_CLOCK_REGRESSION",
}


class ServiceError(ValueError):
    """Stable supervisor error without raw exception, URL, or credential text."""


def _integer(value, low=0, high=2**63 - 1):
    return type(value) is int and low <= value <= high


class TelegramPaperService:
    """One bounded run, exclusive by directory, control identity and bot identity.

    Existing paper enablement is revoked at startup. Shutdown and failed cycles
    also require a durable local pause. A failed pause is reported explicitly.
    PaperControl's shared TELEGRAM_UNAVAILABLE fault code expresses this fail-
    closed policy at lifecycle boundaries; it does not assert a network outage.
    The supervisor status records the distinct lifecycle cause in pause_cause.
    Telegram buttons may change only the separate paper control; this supervisor
    never invokes a strategy, sends orders, or clears a paper ledger risk pause.
    """

    def __init__(
        self,
        state_dir,
        *,
        control,
        credential_provider,
        transport_factory=TelegramTransport,
        adapter_factory=PaperTelegram,
        clock_ms=None,
        lock_root=None,
    ):
        if (
            not callable(credential_provider)
            or not callable(transport_factory)
            or not callable(adapter_factory)
        ):
            raise ServiceError("INVALID_SERVICE_DEPENDENCY")
        initial = control.snapshot()
        if (
            type(initial) is not dict
            or initial.get("mode") != "paper"
            or initial.get("scope") != "US500"
            or initial.get("production_control_available") is not False
            or not isinstance(initial.get("control_id"), str)
            or not _CONTROL_ID.fullmatch(initial["control_id"])
        ):
            raise ServiceError("INVALID_PAPER_CONTROL")
        self.state_dir = Path(state_dir)
        self.control = control
        self.control_id = initial["control_id"]
        self.credential_provider = credential_provider
        self.transport_factory = transport_factory
        self.adapter_factory = adapter_factory
        self.clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self.lock_root = (
            Path(lock_root)
            if lock_root is not None
            else Path(tempfile.gettempdir())
            / f"us500-paper-telegram-locks-{os.getuid()}"
        )
        self.status_path = self.state_dir / "telegram-service-status.json"
        self._locks = []
        self._adapter = None
        self._directory_identity = None
        self._last_poll_error = None

    def _now(self):
        value = self.clock_ms()
        if not _integer(value, 946684800000, 4102444800000):
            raise ServiceError("INVALID_SERVICE_CLOCK")
        return value

    @staticmethod
    def _directory(path):
        if path.is_symlink():
            raise ServiceError("UNSAFE_SERVICE_DIRECTORY")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o022
        ):
            raise ServiceError("UNSAFE_SERVICE_DIRECTORY")
        return info.st_dev, info.st_ino

    def _lock(self, path):
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ServiceError("UNSAFE_SERVICE_LOCK")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ServiceError("SERVICE_ALREADY_RUNNING") from None
            self._locks.append(descriptor)
        except Exception:
            os.close(descriptor)
            raise

    def _unlock(self):
        for descriptor in reversed(self._locks):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        self._locks.clear()

    def _pause(self):
        try:
            state = self.control.pause_for_fault(
                "TELEGRAM_UNAVAILABLE", now_ms=self._now()
            )
            return (
                type(state) is dict
                and state.get("paper_enabled") is False
                and state.get("control_id") == self.control_id
                and state.get("production_control_available") is False
            )
        except Exception:
            return False

    def _control_status(self):
        raw = self.control.snapshot()
        if (
            type(raw) is not dict
            or raw.get("control_id") != self.control_id
            or raw.get("mode") != "paper"
            or raw.get("scope") != "US500"
            or raw.get("production_control_available") is not False
            or type(raw.get("paper_enabled")) is not bool
            or not _integer(raw.get("revision"))
        ):
            raise ServiceError("CONTROL_IDENTITY_CHANGED")
        reason = raw.get("reason")
        return {
            "control_id": self.control_id,
            "paper_enabled": raw["paper_enabled"],
            "revision": raw["revision"],
            "reason": reason
            if isinstance(reason, str) and _REASON.fullmatch(reason)
            else "UNAVAILABLE",
            "production_control_available": False,
        }

    def _outbox_status(self):
        if self._adapter is None:
            return None
        raw = self._adapter.snapshot()
        if (
            type(raw) is not dict
            or raw.get("scope") != "US500"
            or raw.get("mode") != "paper"
            or raw.get("production_control_available") is not False
        ):
            raise ServiceError("INVALID_OUTBOX_STATUS")
        counts = raw.get("outbox_counts")
        if (
            type(counts) is not dict
            or not set(counts) <= _OUTBOX_STATES
            or any(not _integer(value, 0, 4000) for value in counts.values())
        ):
            raise ServiceError("INVALID_OUTBOX_STATUS")
        if not _integer(raw.get("next_offset")) or not _integer(
            raw.get("saved_updates"), 0, 2000
        ):
            raise ServiceError("INVALID_OUTBOX_STATUS")
        blocking = raw.get("blocking_outbox_counts")
        if blocking is None:
            blocking = {
                key: counts.get(key, 0) for key in ("UNKNOWN", "FAILED", "ATTEMPTED")
            }
        if (
            type(blocking) is not dict
            or not set(blocking) <= {"UNKNOWN", "FAILED", "ATTEMPTED"}
            or any(
                not _integer(value, 0, counts.get(key, 0))
                for key, value in blocking.items()
            )
            or any(
                blocking.get(key, 0) != counts.get(key, 0)
                for key in ("UNKNOWN", "ATTEMPTED")
            )
        ):
            raise ServiceError("INVALID_OUTBOX_STATUS")
        return {
            "next_offset": raw["next_offset"],
            "saved_updates": raw["saved_updates"],
            "outbox_counts": counts,
            "blocking_outbox_counts": blocking,
        }

    def _status(self, phase, completed, maximum, interval, errors, pause_confirmed):
        try:
            control = self._control_status()
        except Exception:
            control = {
                "control_id": self.control_id,
                "paper_enabled": None,
                "available": False,
            }
        try:
            outbox = self._outbox_status()
        except Exception:
            outbox = {"available": False}
        try:
            heartbeat = self._now()
        except Exception:
            heartbeat = None
        return {
            "schema": SCHEMA,
            "pid": os.getpid(),
            "phase": phase,
            "heartbeat_at_ms": heartbeat,
            "completed_cycles": completed,
            "max_cycles": maximum,
            "interval_seconds": interval,
            "control": control,
            "outbox": outbox,
            "errors": list(dict.fromkeys(errors)),
            # Only a closed-set code observed during this run, never historical
            # reconstruction or raw adapter payload/exception text.
            "last_poll_error": self._last_poll_error,
            "paper_pause_confirmed": pause_confirmed,
            "pause_cause": {
                "STARTING": "SERVICE_STARTUP",
                "STOPPED": "SERVICE_SHUTDOWN",
                "FAILED": "SERVICE_FAILURE",
            }.get(phase),
            "production_control_available": False,
            "kalshi_access": False,
            "paper_runner_started": False,
            "status_persisted": False,
        }

    def _write_status(self, status):
        current = self.state_dir.stat()
        if (
            self.state_dir.is_symlink()
            or (current.st_dev, current.st_ino) != self._directory_identity
        ):
            raise ServiceError("SERVICE_DIRECTORY_REPLACED")
        if self.status_path.is_symlink():
            raise ServiceError("UNSAFE_STATUS_PATH")
        if self.status_path.exists():
            descriptor = os.open(self.status_path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as prior:
                raw = prior.read(MAX_STATUS_BYTES + 1)
            if len(raw) > MAX_STATUS_BYTES:
                raise ServiceError("FOREIGN_STATUS_FILE")
            try:
                old = json.loads(raw)
                if (
                    old["schema"] != SCHEMA
                    or old["control"]["control_id"] != self.control_id
                ):
                    raise ValueError
            except (TypeError, KeyError, ValueError):
                raise ServiceError("FOREIGN_STATUS_FILE") from None
        status["status_persisted"] = True
        data = (
            json.dumps(status, sort_keys=True, indent=2, allow_nan=False).encode(
                "utf-8"
            )
            + b"\n"
        )
        if len(data) > MAX_STATUS_BYTES:
            raise ServiceError("STATUS_TOO_LARGE")
        descriptor, filename = tempfile.mkstemp(
            prefix=".us500-telegram-status-", dir=self.state_dir
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(filename, self.status_path)
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except Exception:
            status["status_persisted"] = False
            raise
        finally:
            try:
                os.unlink(filename)
            except FileNotFoundError:
                pass

    def run(self, max_cycles, interval_seconds, stop_event, *, persistent=False):
        if type(persistent) is not bool:
            raise ServiceError("INVALID_PERSISTENT_MODE")
        if not (persistent and max_cycles is None) and not _integer(
            max_cycles, 1, MAX_CYCLES
        ):
            raise ServiceError("INVALID_MAX_CYCLES")
        if (
            type(interval_seconds) not in (int, float)
            or not math.isfinite(interval_seconds)
            or not 0 <= interval_seconds <= 3600
        ):
            raise ServiceError("INVALID_POLL_INTERVAL")
        if not callable(getattr(stop_event, "is_set", None)) or not callable(
            getattr(stop_event, "wait", None)
        ):
            raise ServiceError("INVALID_STOP_EVENT")
        if self._locks:
            raise ServiceError("SERVICE_ALREADY_RUNNING")
        self._last_poll_error = None
        completed, errors, pause_confirmed, owns_control = 0, [], False, False
        phase = "STARTING"
        status = None
        retry_delay = 3
        try:
            self._directory_identity = self._directory(self.state_dir)
            self._lock(self.state_dir / ".telegram-service.lock")
            self._directory(self.lock_root)
            self._lock(self.lock_root / f"control-{self.control_id}.lock")
            owns_control = True
            pause_confirmed = self._pause()
            if not pause_confirmed:
                raise ServiceError("STARTUP_PAUSE_UNCONFIRMED")
            status = self._status(
                phase, completed, max_cycles, interval_seconds, errors, pause_confirmed
            )
            self._write_status(status)
            if not stop_event.is_set():
                try:
                    token = self.credential_provider()
                    transport = self.transport_factory(
                        token, owner_chat_id=self.control.owner_chat_id
                    )
                except Exception:
                    raise ServiceError("CREDENTIAL_OR_TRANSPORT_UNAVAILABLE") from None
                finally:
                    token = None
                if not _integer(getattr(transport, "bot_id", None), 1):
                    raise ServiceError("INVALID_BOT_IDENTITY")
                self._lock(self.lock_root / f"bot-{transport.bot_id}.lock")
                self._adapter = self.adapter_factory(
                    self.state_dir / "outbox.sqlite",
                    control=self.control,
                    transport=transport,
                )
                cycles = itertools.count() if max_cycles is None else range(max_cycles)
                for _ in cycles:
                    if stop_event.is_set():
                        break
                    if persistent:
                        self._adapter.prune_delivered(keep_updates=500)
                        counts = self._outbox_status()["blocking_outbox_counts"]
                        unresolved = any(
                            counts.get(key, 0)
                            for key in ("UNKNOWN", "FAILED", "ATTEMPTED")
                        )
                        self.control.set_transport_available(not unresolved)
                    # Validating identity before each request prevents a replaced
                    # control DB from silently changing the supervised account.
                    self._control_status()
                    status = self._status(
                        "RUNNING", completed, max_cycles, interval_seconds, errors, None
                    )
                    self._write_status(status)
                    try:
                        result = self._adapter.poll_once(now_ms=self._now())
                    except Exception:
                        self._last_poll_error = "POLL_CYCLE_FAILED"
                        raise ServiceError("POLL_CYCLE_FAILED") from None
                    completed += 1
                    if (
                        type(result) is not dict
                        or type(result.get("poll_status")) is not str
                        or result.get("poll_status") != "COMPLETE"
                    ):
                        if (
                            type(result) is dict
                            and type(result.get("poll_status")) is str
                            and result["poll_status"] == "STOPPED"
                        ):
                            code = result.get("error")
                            # The existing sanitizer accepts exception objects;
                            # do not invoke str/repr on an untrusted error value.
                            self._last_poll_error = (
                                _safe_error(code)
                                if type(code) is str
                                else "INVALID_POLL_ERROR"
                            )
                        else:
                            self._last_poll_error = "INVALID_POLL_RESULT"
                        if (
                            persistent
                            and self._last_poll_error in _TRANSIENT_POLL_ERRORS
                        ):
                            if not self._pause():
                                raise ServiceError("RECOVERY_PAUSE_UNCONFIRMED")
                            status = self._status(
                                "RECOVERING",
                                completed,
                                max_cycles,
                                interval_seconds,
                                ["POLL_TEMPORARILY_UNAVAILABLE"],
                                True,
                            )
                            self._write_status(status)
                            delay = result.get("retry_after_seconds", retry_delay)
                            if not _integer(delay, 1, 86400):
                                raise ServiceError("INVALID_RETRY_DELAY")
                            delay = max(retry_delay, delay)
                            # Espera interrumpible, con heartbeat cada 30s para
                            # distinguir backoff de receptor muerto.
                            interrupted = False
                            while delay > 0:
                                chunk = min(delay, 30) if delay > 60 else delay
                                if stop_event.wait(chunk):
                                    interrupted = True
                                    break
                                delay -= chunk
                                if delay:
                                    status = self._status(
                                        "RECOVERING",
                                        completed,
                                        max_cycles,
                                        interval_seconds,
                                        ["POLL_TEMPORARILY_UNAVAILABLE"],
                                        True,
                                    )
                                    self._write_status(status)
                            if interrupted:
                                break
                            retry_delay = min(60, retry_delay * 2)
                            continue
                        raise ServiceError("POLL_CYCLE_STOPPED")
                    retry_delay = 3
                    outbox = self._outbox_status()
                    cycle_phase, cycle_errors = "RUNNING", []
                    if any(
                        outbox[
                            "blocking_outbox_counts" if persistent else "outbox_counts"
                        ].get(key, 0)
                        for key in ("UNKNOWN", "FAILED", "ATTEMPTED")
                    ):
                        if not persistent:
                            raise ServiceError("OUTBOX_DELIVERY_UNCONFIRMED")
                        # Keep receiving new owner commands while preserving every
                        # uncertain response without resending it. Paper stays paused.
                        if not self._pause():
                            raise ServiceError("RECOVERY_PAUSE_UNCONFIRMED")
                        cycle_phase = "DEGRADED"
                        cycle_errors = ["OUTBOX_DELIVERY_UNCONFIRMED"]
                    status = self._status(
                        cycle_phase,
                        completed,
                        max_cycles,
                        interval_seconds,
                        cycle_errors,
                        True if cycle_errors else None,
                    )
                    self._write_status(status)
                    if (
                        max_cycles is None or completed < max_cycles
                    ) and stop_event.wait(interval_seconds):
                        break
            phase = "STOPPED"
        except ServiceError as exc:
            # All ServiceError sites are local constants; other exception text
            # is never included, even if a provider embeds its API token.
            known = str(exc)
            errors.append(known if _REASON.fullmatch(known) else "SERVICE_FAILED")
            phase = "FAILED"
        except KeyboardInterrupt:
            errors.append("INTERRUPTED")
            phase = "STOPPED"
        except Exception:
            errors.append("SERVICE_FAILED")
            phase = "FAILED"
        finally:
            if owns_control:
                pause_confirmed = self._pause()
                if not pause_confirmed:
                    errors.append("SHUTDOWN_PAUSE_UNCONFIRMED")
                    phase = "FAILED"
                status = self._status(
                    phase,
                    completed,
                    max_cycles,
                    interval_seconds,
                    errors,
                    pause_confirmed,
                )
                try:
                    self._write_status(status)
                except Exception:
                    status["status_persisted"] = False
                    status["phase"] = "FAILED"
                    status["errors"] = list(
                        dict.fromkeys(errors + ["STATUS_WRITE_FAILED"])
                    )
            if self._adapter is not None:
                try:
                    self._adapter.close()
                except Exception:
                    pass
                self._adapter = None
            self._unlock()
        if status is None:
            # Lock losers must not pause the active receiver or replace its status.
            status = self._status(
                "FAILED", completed, max_cycles, interval_seconds, errors, False
            )
        return status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--owner-user-id", type=int, required=True)
    parser.add_argument("--owner-chat-id", type=int, required=True)
    parser.add_argument("--max-cycles", type=int, required=True)
    parser.add_argument("--interval-seconds", type=float, default=2)
    args = parser.parse_args(argv)
    stop = threading.Event()
    previous = {}
    try:
        TelegramPaperService._directory(args.state_dir)
        control = PaperControl(
            args.state_dir / "control.sqlite",
            owner_user_id=args.owner_user_id,
            owner_chat_id=args.owner_chat_id,
        )
        supervisor = TelegramPaperService(
            args.state_dir,
            control=control,
            credential_provider=lambda: os.environ.get("US500_PAPER_TELEGRAM_TOKEN"),
        )
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, lambda *_: stop.set())
        status = supervisor.run(args.max_cycles, args.interval_seconds, stop)
        print(json.dumps(status, sort_keys=True, allow_nan=False))
        return 0 if status["phase"] == "STOPPED" else 2
    except Exception:
        print(
            json.dumps(
                {
                    "error": "LOCAL_SUPERVISOR_FAILED",
                    "production_control_available": False,
                }
            )
        )
        return 2
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
