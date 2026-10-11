"""Explicit, bounded Telegram transport for the isolated US500 paper control.

Use a dedicated Telegram bot, or integrate PaperControl into an existing router.
Never install another getUpdates poller for a bot already consumed elsewhere.
No polling loop, service installation, production imports, or background startup.
Only this module's explicit CLI invocation reads US500_PAPER_TELEGRAM_TOKEN.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from us500_control import ControlError, PaperControl

HOST = "api.telegram.org"
MAX_BYTES = 1_000_000
MAX_BATCH = 10
MAX_SENDS = 10
MAX_UPDATES = 2_000
MAX_OUTBOX = 4_000
HTTP_TIMEOUT_SECONDS = 5
HTTP_DEADLINE_SECONDS = 7
POLL_DEADLINE_SECONDS = 20
_METHODS = {"getUpdates", "sendMessage", "answerCallbackQuery"}
_TOKEN = re.compile(r"([1-9][0-9]{0,19}):[A-Za-z0-9_-]{20,100}\Z")
_CALLBACK = re.compile(r"[!-~]{1,128}\Z")
_TABLES = {"telegram_meta", "telegram_updates", "telegram_outbox"}
_SAFE_ERRORS = {
    "INVALID_JSON_VALUE",
    "DUPLICATE_RESPONSE_KEY",
    "INVALID_JSON_CONSTANT",
    "METHOD_OR_PAYLOAD_BLOCKED",
    "INVALID_GET_UPDATES_PAYLOAD",
    "INVALID_CALLBACK_RESPONSE",
    "RECIPIENT_OR_MESSAGE_BLOCKED",
    "INVALID_MESSAGE_TEXT",
    "INVALID_KEYBOARD",
    "OUTBOUND_PAYLOAD_TOO_LARGE",
    "INVALID_TELEGRAM_TOKEN",
    "INVALID_OWNER_CHAT",
    "TELEGRAM_REQUEST_REJECTED",
    "TELEGRAM_RESPONSE_UNKNOWN",
    "TELEGRAM_RESPONSE_TIMEOUT",
    "TELEGRAM_RESPONSE_TOO_LARGE",
    "INVALID_CONTROL_SCOPE",
    "INVALID_BOT_IDENTITY",
    "INVALID_OUTBOX_DATABASE",
    "FOREIGN_OR_INCOMPLETE_OUTBOX_DATABASE",
    "OUTBOX_BINDING_CONFLICT",
    "OUTBOX_DATABASE_REPLACED",
    "INVALID_UPDATE_ID",
    "UPDATE_TOO_LARGE",
    "UPDATE_ID_CONFLICT",
    "UPDATE_CAPACITY_EXHAUSTED",
    "OUTBOX_CAPACITY_EXHAUSTED",
    "INVALID_CONTROL_RESPONSE",
    "INVALID_POLL_TIMESTAMP",
    "INVALID_UPDATES_RESPONSE",
    "TELEGRAM_TOKEN_REQUIRED",
    "GET_UPDATES_FAILED",
    "OUTBOX_DELIVERY_FAILED",
    "INVALID_RETENTION_LIMIT",
    "OUTBOX_RETENTION_FAILED",
}


class TelegramError(ValueError):
    """Stable error without credentials, URLs, or raw provider payloads."""


class TelegramRejected(TelegramError):
    """An explicit provider rejection. Never retried automatically."""


def _safe_error(exc):
    code = str(exc)
    return code if code in _SAFE_ERRORS else "CONTROL_OR_TRANSPORT_FAILED"


def _integer(value, *, minimum=0, maximum=2**63 - 2):
    return type(value) is int and minimum <= value <= maximum


def _canonical(value):
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise TelegramError("INVALID_JSON_VALUE") from None


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TelegramError("DUPLICATE_RESPONSE_KEY")
        result[key] = value
    return result


def _reject_constant(_):
    raise TelegramError("INVALID_JSON_CONSTANT")


def _payload(method, payload, owner_chat_id):
    if method not in _METHODS or type(payload) is not dict:
        raise TelegramError("METHOD_OR_PAYLOAD_BLOCKED")
    if method == "getUpdates":
        if set(payload) != {"offset", "limit", "timeout", "allowed_updates"}:
            raise TelegramError("INVALID_GET_UPDATES_PAYLOAD")
        if (
            not _integer(payload["offset"])
            or not _integer(payload["limit"], minimum=1, maximum=MAX_BATCH)
            or payload["timeout"] != 0
            or type(payload["timeout"]) is not int
            or payload["allowed_updates"] != ["message", "callback_query"]
        ):
            raise TelegramError("INVALID_GET_UPDATES_PAYLOAD")
    elif method == "answerCallbackQuery":
        if (
            set(payload) != {"callback_query_id"}
            or not isinstance(payload["callback_query_id"], str)
            or not _CALLBACK.fullmatch(payload["callback_query_id"])
        ):
            raise TelegramError("INVALID_CALLBACK_RESPONSE")
    else:
        if (
            not {"chat_id", "text"}
            <= set(payload)
            <= {"chat_id", "text", "reply_markup"}
            or type(payload["chat_id"]) is not int
            or payload["chat_id"] != owner_chat_id
        ):
            raise TelegramError("RECIPIENT_OR_MESSAGE_BLOCKED")
        if (
            not isinstance(payload["text"], str)
            or not 1 <= len(payload["text"]) <= 4096
        ):
            raise TelegramError("INVALID_MESSAGE_TEXT")
        if "reply_markup" in payload:
            markup = payload["reply_markup"]
            if type(markup) is not dict or set(markup) != {"inline_keyboard"}:
                raise TelegramError("INVALID_KEYBOARD")
            rows = markup["inline_keyboard"]
            if type(rows) is not list or not 1 <= len(rows) <= 10:
                raise TelegramError("INVALID_KEYBOARD")
            for row in rows:
                if type(row) is not list or not 1 <= len(row) <= 10:
                    raise TelegramError("INVALID_KEYBOARD")
                for button in row:
                    if type(button) is not dict or set(button) != {
                        "text",
                        "callback_data",
                    }:
                        raise TelegramError("INVALID_KEYBOARD")
                    text, data = button["text"], button["callback_data"]
                    if (
                        not isinstance(text, str)
                        or not 1 <= len(text) <= 100
                        or not isinstance(data, str)
                        or not data.startswith("u5:")
                        or not 1 <= len(data.encode("utf-8")) <= 64
                    ):
                        raise TelegramError("INVALID_KEYBOARD")
    if len(_canonical(payload).encode("utf-8")) > 32768:
        raise TelegramError("OUTBOUND_PAYLOAD_TOO_LARGE")


class TelegramTransport:
    """Fixed-host HTTPS POST only; no redirects, proxies, retries, or logging.

    Constructor injection is for fake transports/tests. It does not read the
    environment. A network outcome without an explicit acknowledgement is unknown.
    """

    def __init__(self, token, *, owner_chat_id, connection_factory=None):
        if not isinstance(token, str) or not (match := _TOKEN.fullmatch(token)):
            raise TelegramError("INVALID_TELEGRAM_TOKEN")
        if not _integer(owner_chat_id, minimum=1):
            raise TelegramError("INVALID_OWNER_CHAT")
        self._token = token
        self.bot_id = int(match.group(1))
        self.owner_chat_id = owner_chat_id
        self._connection_factory = connection_factory or http.client.HTTPSConnection

    def call(self, method, payload):
        _payload(method, payload, self.owner_chat_id)
        body = _canonical(payload).encode("utf-8")
        connection = None
        deadline = time.monotonic() + HTTP_DEADLINE_SECONDS
        try:
            connection = self._connection_factory(HOST, timeout=HTTP_TIMEOUT_SECONDS)
            connection.request(
                "POST",
                f"/bot{self._token}/{method}",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                if 400 <= response.status < 500:
                    raise TelegramRejected("TELEGRAM_REQUEST_REJECTED")
                raise TelegramError("TELEGRAM_RESPONSE_UNKNOWN")
            raw = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TelegramError("TELEGRAM_RESPONSE_TIMEOUT")
                if getattr(connection, "sock", None) is not None:
                    connection.sock.settimeout(min(HTTP_TIMEOUT_SECONDS, remaining))
                chunk = response.read1(min(65536, MAX_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise TelegramError("TELEGRAM_RESPONSE_TOO_LARGE")
            parsed = json.loads(
                raw, object_pairs_hook=_unique, parse_constant=_reject_constant
            )
            if type(parsed) is not dict or type(parsed.get("ok")) is not bool:
                raise TelegramError("TELEGRAM_RESPONSE_UNKNOWN")
            if not parsed["ok"]:
                raise TelegramRejected("TELEGRAM_REQUEST_REJECTED")
            return parsed
        except TelegramError:
            raise
        except Exception:
            # Network errors commonly contain the token-bearing URL. Never copy.
            raise TelegramError("TELEGRAM_RESPONSE_UNKNOWN") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


class PaperTelegram:
    """Durable cursor/outbox; consumes only a dedicated bot's owner commands.

    Responses commit before the cursor advances, in the same SQLite transaction.
    A crash after control.handle_update is safe: PaperControl caches its response.
    ATTEMPTED is committed before HTTP. Ambiguous/recovered attempts become UNKNOWN
    and are never retried; a missing acknowledgement cannot toggle control twice.
    Use one poller per bot. No raw incoming updates or API token enter this DB.
    """

    def __init__(self, path, *, control, transport):
        snapshot = control.snapshot()
        if (
            snapshot.get("mode") != "paper"
            or snapshot.get("scope") != "US500"
            or snapshot.get("production_control_available") is not False
        ):
            raise TelegramError("INVALID_CONTROL_SCOPE")
        if not _integer(getattr(transport, "bot_id", None), minimum=1):
            raise TelegramError("INVALID_BOT_IDENTITY")
        self.control, self.transport = control, transport
        self.path = Path(path)
        if str(path) == ":memory:" or self.path.is_symlink():
            raise TelegramError("INVALID_OUTBOX_DATABASE")
        created = False
        try:
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            if not self.path.is_file() or self.path.is_symlink():
                raise TelegramError("INVALID_OUTBOX_DATABASE") from None
        else:
            os.close(descriptor)
            created = True
        stat = self.path.stat()
        self._identity = (stat.st_dev, stat.st_ino)
        binding = _canonical(
            {
                "control_id": snapshot["control_id"],
                "owner_user_id": control.owner_user_id,
                "owner_chat_id": control.owner_chat_id,
                "bot_id": transport.bot_id,
            }
        )
        self._binding = binding
        self._db = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        self._db.execute("PRAGMA synchronous=FULL")
        try:
            self._check_file()
            self._db.execute("BEGIN IMMEDIATE")
            tables = {
                r[0]
                for r in self._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not created and not _TABLES.issubset(tables):
                raise TelegramError("FOREIGN_OR_INCOMPLETE_OUTBOX_DATABASE")
            if created:
                self._db.execute(
                    "CREATE TABLE telegram_meta (id INTEGER PRIMARY KEY CHECK(id=1), binding TEXT NOT NULL, next_offset INTEGER NOT NULL)"
                )
                self._db.execute(
                    "CREATE TABLE telegram_updates (update_id INTEGER PRIMARY KEY, digest TEXT NOT NULL)"
                )
                self._db.execute(
                    "CREATE TABLE telegram_outbox (id TEXT PRIMARY KEY, update_id INTEGER NOT NULL, ordinal INTEGER NOT NULL, method TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, error TEXT)"
                )
                self._db.execute(
                    "INSERT INTO telegram_meta VALUES (1, ?, 0)", (binding,)
                )
            elif self._db.execute(
                "SELECT binding FROM telegram_meta WHERE id=1"
            ).fetchone() != (binding,):
                raise TelegramError("OUTBOX_BINDING_CONFLICT")
            self._db.execute(
                "UPDATE telegram_outbox SET status='UNKNOWN', error='INTERRUPTED_ATTEMPT' WHERE status='ATTEMPTED'"
            )
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            self._db.close()
            raise

    def _check_file(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise TelegramError("OUTBOX_DATABASE_REPLACED")
        stat = self.path.stat()
        if (stat.st_dev, stat.st_ino) != self._identity:
            raise TelegramError("OUTBOX_DATABASE_REPLACED")

    def close(self):
        self._db.close()

    def snapshot(self):
        self._check_file()
        counts = dict(
            self._db.execute(
                "SELECT status, count(*) FROM telegram_outbox GROUP BY status"
            )
        )
        return {
            "schema": "us500-paper-telegram-v1",
            "mode": "paper",
            "scope": "US500",
            "next_offset": self._db.execute(
                "SELECT next_offset FROM telegram_meta WHERE id=1"
            ).fetchone()[0],
            "outbox_counts": counts,
            "saved_updates": self._db.execute(
                "SELECT count(*) FROM telegram_updates"
            ).fetchone()[0],
            "production_control_available": False,
            "automatic_unknown_retry": False,
        }

    def prune_delivered(self, *, keep_updates=500):
        """Explicit, transactional retention; never sends or changes control.

        Keep the most recent update IDs regardless of delivery state. Older IDs
        strictly behind the cursor may be removed only when all their responses
        are SENT (including ignored updates that have no response). Any other
        status protects the entire update and all its outbox rows. Cursor and
        control binding survive intact: a forgotten ID cannot be executed again.
        Protected rows may still exhaust capacity; they are never auto-retried
        or erased to keep a receiver running.
        """
        if not _integer(keep_updates, minimum=1, maximum=MAX_UPDATES):
            raise TelegramError("INVALID_RETENTION_LIMIT")
        try:
            self._check_file()
            current = self.control.snapshot()
            if (
                type(current) is not dict
                or current.get("mode") != "paper"
                or current.get("scope") != "US500"
                or current.get("production_control_available") is not False
            ):
                raise TelegramError("INVALID_CONTROL_SCOPE")
            binding = _canonical(
                {
                    "control_id": current.get("control_id"),
                    "owner_user_id": self.control.owner_user_id,
                    "owner_chat_id": self.control.owner_chat_id,
                    "bot_id": self.transport.bot_id,
                }
            )
            if binding != self._binding:
                raise TelegramError("OUTBOX_BINDING_CONFLICT")
            self._db.execute("BEGIN IMMEDIATE")
            metadata = self._db.execute(
                "SELECT binding, next_offset FROM telegram_meta WHERE id=1"
            ).fetchone()
            if metadata is None or metadata[0] != self._binding:
                raise TelegramError("OUTBOX_BINDING_CONFLICT")
            offset = metadata[1]
            if not _integer(offset):
                raise TelegramError("OUTBOX_RETENTION_FAILED")
            cutoff = self._db.execute(
                "SELECT update_id FROM telegram_updates ORDER BY update_id DESC LIMIT 1 OFFSET ?",
                (keep_updates - 1,),
            ).fetchone()
            eligible = []
            if cutoff is not None:
                eligible = self._db.execute(
                    "SELECT u.update_id FROM telegram_updates u "
                    "WHERE u.update_id < ? AND u.update_id < ? "
                    "AND NOT EXISTS (SELECT 1 FROM telegram_outbox o "
                    "WHERE o.update_id=u.update_id AND o.status IS NOT 'SENT') "
                    "ORDER BY u.update_id",
                    (cutoff[0], offset),
                ).fetchall()
            deleted_outbox = self._db.executemany(
                "DELETE FROM telegram_outbox WHERE update_id=? AND status='SENT'",
                eligible,
            ).rowcount
            deleted_updates = self._db.executemany(
                "DELETE FROM telegram_updates WHERE update_id=?", eligible
            ).rowcount
            retained_updates = self._db.execute(
                "SELECT count(*) FROM telegram_updates"
            ).fetchone()[0]
            self._check_file()
            self._db.execute("COMMIT")
            return {
                "deleted_updates": deleted_updates,
                "deleted_outbox": deleted_outbox,
                "retained_updates": retained_updates,
                "next_offset": offset,
            }
        except BaseException as exc:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            if isinstance(exc, TelegramError) or not isinstance(exc, Exception):
                raise
            raise TelegramError("OUTBOX_RETENTION_FAILED") from None

    def _persist_update(self, update, now_ms):
        if type(update) is not dict or not _integer(update.get("update_id")):
            raise TelegramError("INVALID_UPDATE_ID")
        canonical = _canonical(update).encode("utf-8")
        if len(canonical) > 32768:
            raise TelegramError("UPDATE_TOO_LARGE")
        update_id = update["update_id"]
        digest = hashlib.sha256(canonical).hexdigest()
        try:
            self._db.execute("BEGIN IMMEDIATE")
            prior = self._db.execute(
                "SELECT digest FROM telegram_updates WHERE update_id=?", (update_id,)
            ).fetchone()
            if prior:
                if prior != (digest,):
                    raise TelegramError("UPDATE_ID_CONFLICT")
                self._db.execute("COMMIT")
                return
            offset = self._db.execute(
                "SELECT next_offset FROM telegram_meta WHERE id=1"
            ).fetchone()[0]
            if update_id < offset:
                self._db.execute("COMMIT")
                return
            if (
                self._db.execute("SELECT count(*) FROM telegram_updates").fetchone()[0]
                >= MAX_UPDATES
            ):
                raise TelegramError("UPDATE_CAPACITY_EXHAUSTED")
            if (
                self._db.execute("SELECT count(*) FROM telegram_outbox").fetchone()[0]
                + 2
                > MAX_OUTBOX
            ):
                raise TelegramError("OUTBOX_CAPACITY_EXHAUSTED")
            responses = self.control.handle_update(update, now_ms=now_ms)
            if type(responses) is not list or len(responses) > 2:
                raise TelegramError("INVALID_CONTROL_RESPONSE")
            for ordinal, response in enumerate(responses):
                if (
                    type(response) is not dict
                    or set(response) != {"method", "payload"}
                    or response["method"] not in {"sendMessage", "answerCallbackQuery"}
                ):
                    raise TelegramError("INVALID_CONTROL_RESPONSE")
                _payload(
                    response["method"], response["payload"], self.control.owner_chat_id
                )
                self._db.execute(
                    "INSERT INTO telegram_outbox VALUES (?, ?, ?, ?, ?, 'PENDING', NULL)",
                    (
                        f"{update_id}:{ordinal}",
                        update_id,
                        ordinal,
                        response["method"],
                        _canonical(response["payload"]),
                    ),
                )
            self._db.execute(
                "INSERT INTO telegram_updates VALUES (?, ?)", (update_id, digest)
            )
            self._db.execute(
                "UPDATE telegram_meta SET next_offset=? WHERE id=1", (update_id + 1,)
            )
            self._db.execute("COMMIT")
        except Exception:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise

    @staticmethod
    def _acknowledged(method, payload, response):
        if type(response) is not dict or response.get("ok") is not True:
            return False
        result = response.get("result")
        if method == "answerCallbackQuery":
            return result is True
        return (
            type(result) is dict
            and _integer(result.get("message_id"), minimum=1)
            and type(result.get("chat")) is dict
            and type(result["chat"].get("id")) is int
            and result["chat"]["id"] == payload["chat_id"]
        )

    def _deliver(self, deadline):
        for _ in range(MAX_SENDS):
            if time.monotonic() >= deadline:
                break
            self._check_file()
            try:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT id, method, payload FROM telegram_outbox WHERE status='PENDING' ORDER BY update_id, ordinal LIMIT 1"
                ).fetchone()
                if row is None:
                    self._db.execute("COMMIT")
                    break
                outbox_id, method, raw_payload = row
                self._db.execute(
                    "UPDATE telegram_outbox SET status='ATTEMPTED' WHERE id=? AND status='PENDING'",
                    (outbox_id,),
                )
                self._db.execute("COMMIT")
            except Exception:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            payload = json.loads(raw_payload)
            status, error = "UNKNOWN", "ACKNOWLEDGEMENT_MISSING"
            try:
                response = self.transport.call(method, payload)
                if self._acknowledged(method, payload, response):
                    status, error = "SENT", None
                elif type(response) is dict and response.get("ok") is False:
                    status, error = "FAILED", "TELEGRAM_REQUEST_REJECTED"
            except TelegramRejected:
                status, error = "FAILED", "TELEGRAM_REQUEST_REJECTED"
            except Exception:
                # Includes token-bearing exceptions from an injected transport.
                status, error = "UNKNOWN", "TELEGRAM_RESPONSE_UNKNOWN"
            self._check_file()
            self._db.execute(
                "UPDATE telegram_outbox SET status=?, error=? WHERE id=? AND status='ATTEMPTED'",
                (status, error, outbox_id),
            )

    def _stopped(self, code, now_ms):
        # A full/unavailable transport must not leave paper enabled while its
        # owner cannot pause it. Record a local fault, never a fabricated update.
        reason = (
            "TELEGRAM_CAPACITY"
            if code in {"UPDATE_CAPACITY_EXHAUSTED", "OUTBOX_CAPACITY_EXHAUSTED"}
            else "TELEGRAM_UNAVAILABLE"
        )
        confirmed = False
        try:
            state = self.control.pause_for_fault(reason, now_ms=now_ms)
            confirmed = type(state) is dict and state.get("paper_enabled") is False
        except Exception:
            pass
        try:
            result = self.snapshot()
        except Exception:
            result = {
                "schema": "us500-paper-telegram-v1",
                "mode": "paper",
                "scope": "US500",
                "production_control_available": False,
                "outbox_state_available": False,
            }
        result.update(
            poll_status="STOPPED",
            error=code,
            paper_pause_confirmed=confirmed,
            pause_error=None if confirmed else "PAUSE_UNCONFIRMED",
        )
        return result

    def poll_once(self, *, now_ms):
        if not _integer(now_ms, minimum=946684800000, maximum=4102444800000):
            raise TelegramError("INVALID_POLL_TIMESTAMP")
        deadline = time.monotonic() + POLL_DEADLINE_SECONDS
        try:
            self._check_file()
            offset = self.snapshot()["next_offset"]
            response = self.transport.call(
                "getUpdates",
                {
                    "offset": offset,
                    "limit": MAX_BATCH,
                    "timeout": 0,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            if (
                type(response) is not dict
                or response.get("ok") is not True
                or type(response.get("result")) is not list
                or len(response["result"]) > MAX_BATCH
            ):
                raise TelegramError("INVALID_UPDATES_RESPONSE")
            updates = response["result"]
            if any(
                type(update) is not dict or not _integer(update.get("update_id"))
                for update in updates
            ):
                raise TelegramError("INVALID_UPDATE_ID")
            # Never advance past an earlier update merely because the server's
            # batch arrives unsorted. Conflicting duplicate IDs stop processing.
            seen = {}
            for update in updates:
                identifier = update["update_id"]
                canonical = _canonical(update)
                if identifier in seen and seen[identifier] != canonical:
                    raise TelegramError("UPDATE_ID_CONFLICT")
                seen[identifier] = canonical
            for update in sorted(updates, key=lambda item: item["update_id"]):
                if time.monotonic() >= deadline:
                    break
                self._persist_update(update, now_ms)
        except (TelegramError, ControlError) as exc:
            return self._stopped(_safe_error(exc), now_ms)
        except Exception:
            return self._stopped("GET_UPDATES_FAILED", now_ms)
        try:
            self._deliver(deadline)
        except Exception:
            return self._stopped("OUTBOX_DELIVERY_FAILED", now_ms)
        result = self.snapshot()
        result.update(poll_status="COMPLETE", error=None)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control-db", type=Path, required=True)
    parser.add_argument("--outbox-db", type=Path, required=True)
    parser.add_argument("--owner-user-id", type=int, required=True)
    parser.add_argument("--owner-chat-id", type=int, required=True)
    args = parser.parse_args(argv)
    adapter = None
    try:
        token = os.environ.get("US500_PAPER_TELEGRAM_TOKEN")
        if token is None:
            raise TelegramError("TELEGRAM_TOKEN_REQUIRED")
        transport = TelegramTransport(token, owner_chat_id=args.owner_chat_id)
        control = PaperControl(
            args.control_db,
            owner_user_id=args.owner_user_id,
            owner_chat_id=args.owner_chat_id,
        )
        adapter = PaperTelegram(args.outbox_db, control=control, transport=transport)
        result = adapter.poll_once(now_ms=time.time_ns() // 1_000_000)
        print(_canonical(result))
        return 0 if result["poll_status"] == "COMPLETE" else 2
    except (TelegramError, ControlError) as exc:
        print(
            _canonical(
                {"error": _safe_error(exc), "production_control_available": False}
            )
        )
        return 2
    except Exception:
        print(
            _canonical(
                {
                    "error": "LOCAL_CONTROL_OR_IO_ERROR",
                    "production_control_available": False,
                }
            )
        )
        return 2
    finally:
        if adapter is not None:
            adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
