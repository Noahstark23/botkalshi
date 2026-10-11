"""Owner-only Telegram protocol for the local US500 paper runner.

This module has no network transport, token, launcher, or production imports.
It returns outgoing Telegram method descriptions for a separate transport.
Enabling paper execution never clears a risk pause in the paper ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Callable

from bot_motor_panel import BACK, disconnected_detail, overview


class ControlError(ValueError):
    """Stable errors without input payloads or credential values."""


_SCHEMA = "us500-paper-control-v1"
_MAX_UPDATE_BYTES = 32768
_CALLBACK_ID = re.compile(r"[!-~]{1,128}\Z")
_NONCE = re.compile(r"[A-Za-z0-9_-]{24}\Z")
_FAULT_REASONS = {"TELEGRAM_CAPACITY", "TELEGRAM_UNAVAILABLE"}
_TABLES = {
    "paper_control_meta",
    "paper_control_pending",
    "paper_control_updates",
    "paper_control_audit",
}
_FORWARD_FIELDS = {
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_date",
    "forward_sender_name",
    "forward_signature",
    "sender_chat",
    "via_bot",
}


def _integer(value, low=1, high=2**63 - 1):
    return type(value) is int and low <= value <= high


def _time(value):
    if not _integer(value, 946684800000, 4102444800000):
        raise ControlError("INVALID_CONTROL_TIMESTAMP")
    return value


def _canonical(value):
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class PaperControl:
    """Bounded SQLite state, default disabled, with exact actor/chat ownership.

    handle_update(update, now_ms=...) returns sanitized sendMessage /
    answerCallbackQuery descriptions. Input Telegram payloads are not retained.
    with_permit(callback) invokes callback(snapshot) while holding the same
    SQLite write lock as pause. A pause acknowledged after that frame commits
    excludes all later permits. Keep callbacks short; do not reenter this class.
    """

    def __init__(
        self,
        path,
        *,
        owner_user_id,
        owner_chat_id,
        confirmation_ttl_ms=60000,
        max_updates=2000,
        max_audit=4000,
    ):
        if not _integer(owner_user_id) or not _integer(owner_chat_id):
            raise ControlError("INVALID_OWNER_CONFIGURATION")
        if not _integer(confirmation_ttl_ms, 1000, 600000):
            raise ControlError("INVALID_CONFIRMATION_TTL")
        if not _integer(max_updates, 1, 10000) or not _integer(max_audit, 1, 20000):
            raise ControlError("INVALID_CONTROL_BUDGET")
        self.path = Path(path)
        if str(path) == ":memory:" or self.path.is_symlink():
            raise ControlError("INVALID_CONTROL_DATABASE")
        self.owner_user_id = owner_user_id
        self.owner_chat_id = owner_chat_id
        self.ttl = confirmation_ttl_ms
        self.max_updates = max_updates
        self.max_audit = max_audit
        self._config = _canonical(
            {
                "owner_user_id": owner_user_id,
                "owner_chat_id": owner_chat_id,
                "confirmation_ttl_ms": confirmation_ttl_ms,
                "max_updates": max_updates,
                "max_audit": max_audit,
            }
        )
        created = False
        try:
            fd = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            if not self.path.is_file() or self.path.is_symlink():
                raise ControlError("INVALID_CONTROL_DATABASE") from None
        else:
            os.close(fd)
            created = True
        self._identity = self._file_identity()
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            tables = {
                r[0]
                for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not created and not _TABLES.issubset(tables):
                raise ControlError("FOREIGN_OR_INCOMPLETE_CONTROL_DATABASE")
            if created:
                db.execute(
                    "CREATE TABLE paper_control_meta (id INTEGER PRIMARY KEY CHECK(id=1), schema TEXT NOT NULL, config TEXT NOT NULL, control_id TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), revision INTEGER NOT NULL, reason TEXT NOT NULL, updated_ms INTEGER NOT NULL, last_update_id INTEGER NOT NULL, last_seen_ms INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE paper_control_pending (id INTEGER PRIMARY KEY CHECK(id=1), nonce_hash TEXT NOT NULL, actor_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, revision INTEGER NOT NULL, expires_ms INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE paper_control_updates (update_id INTEGER PRIMARY KEY, payload_hash TEXT NOT NULL, response TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE paper_control_audit (seq INTEGER PRIMARY KEY AUTOINCREMENT, at_ms INTEGER NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, revision INTEGER NOT NULL, actor_id INTEGER NOT NULL, chat_id INTEGER NOT NULL)"
                )
                db.execute(
                    "INSERT INTO paper_control_meta VALUES(1,?,?,?,?,?,?,?,?,?)",
                    (
                        _SCHEMA,
                        self._config,
                        secrets.token_hex(16),
                        0,
                        0,
                        "DEFAULT_DISABLED",
                        0,
                        -1,
                        0,
                    ),
                )
            self._read(db)
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _file_identity(self):
        try:
            stat = self.path.stat(follow_symlinks=False)
            if self.path.is_symlink() or not self.path.is_file():
                raise ControlError("INVALID_CONTROL_DATABASE")
            return stat.st_dev, stat.st_ino
        except OSError as exc:
            raise ControlError("CONTROL_DATABASE_UNAVAILABLE") from exc

    def _connect(self):
        if self._file_identity() != self._identity:
            raise ControlError("CONTROL_DATABASE_REPLACED")
        try:
            db = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=rw",
                uri=True,
                timeout=5,
                isolation_level=None,
            )
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=5000")
            return db
        except sqlite3.Error as exc:
            raise ControlError("CONTROL_DATABASE_UNAVAILABLE") from exc

    def _read(self, db):
        row = db.execute("SELECT * FROM paper_control_meta WHERE id=1").fetchone()
        if row is None or row["schema"] != _SCHEMA or row["config"] != self._config:
            raise ControlError("CONTROL_CONFIG_OR_SCHEMA_CONFLICT")
        if row["enabled"] not in (0, 1) or not _integer(row["revision"], 0):
            raise ControlError("CORRUPT_CONTROL_STATE")
        return row

    @staticmethod
    def _snapshot(row):
        return {
            "schema": _SCHEMA,
            "scope": "US500",
            "mode": "paper",
            "paper_enabled": bool(row["enabled"]),
            "revision": row["revision"],
            "reason": row["reason"],
            "updated_at_ms": row["updated_ms"],
            "last_update_id": row["last_update_id"],
            "control_id": row["control_id"],
            "production_control_available": False,
            "ledger_risk_pause_cleared": False,
        }

    def snapshot(self):
        db = self._connect()
        try:
            return self._snapshot(self._read(db))
        finally:
            db.close()

    def pause_for_fault(self, reason, *, now_ms):
        """Durably fail closed for an approved local transport fault.

        This is a local call, never a fabricated owner update. Audit actor/chat
        zero identify the local system. A clock rollback cannot prevent a pause;
        the stored logical timestamp never moves backwards. Repeated faults are
        idempotent unless a new confirmation must be revoked.
        """
        if not isinstance(reason, str) or reason not in _FAULT_REASONS:
            raise ControlError("INVALID_LOCAL_FAULT_REASON")
        now = _time(now_ms)
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            state = self._read(db)
            pending = db.execute(
                "SELECT 1 FROM paper_control_pending WHERE id=1"
            ).fetchone()
            if state["enabled"] or state["reason"] != reason or pending:
                at = max(now, state["last_seen_ms"], state["updated_ms"])
                revision = state["revision"] + 1
                db.execute(
                    "UPDATE paper_control_meta SET enabled=0,revision=?,reason=?,updated_ms=?,last_seen_ms=? WHERE id=1",
                    (revision, reason, at, at),
                )
                db.execute("DELETE FROM paper_control_pending")
                self._audit(db, at, "LOCAL_FAULT_PAUSE", reason, revision, local=True)
            result = self._snapshot(self._read(db))
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def with_permit(self, callback: Callable[[dict], object]):
        """Serialize one short paper frame against pause; never clear ledger risk."""
        if not callable(callback):
            raise ControlError("INVALID_PERMIT_CALLBACK")
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            state = self._snapshot(self._read(db))
            if not state["paper_enabled"]:
                result = {
                    "permitted": False,
                    "reason": "PAPER_DISABLED",
                    "revision": state["revision"],
                    "result": None,
                }
            else:
                result = {
                    "permitted": True,
                    "reason": "PAPER_CONTROL_ENABLED",
                    "revision": state["revision"],
                    "result": callback(state),
                }
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _authorized(self, actor, chat, message):
        return (
            isinstance(actor, dict)
            and type(actor.get("id")) is int
            and actor["id"] == self.owner_user_id
            and actor.get("is_bot") is False
            and isinstance(chat, dict)
            and type(chat.get("id")) is int
            and chat["id"] == self.owner_chat_id
            and chat.get("type") == "private"
            and not any(k in message for k in _FORWARD_FIELDS)
            and message.get("is_automatic_forward") is not True
        )

    def _parse(self, update):
        if not isinstance(update, dict) or not _integer(update.get("update_id"), 0):
            return None
        try:
            serialized = _canonical(update)
            encoded_size = len(serialized.encode("utf-8"))
        except (ValueError, TypeError, UnicodeError, RecursionError):
            return None
        if encoded_size > _MAX_UPDATE_BYTES:
            return None
        if ("message" in update) == ("callback_query" in update):
            return None
        query = update.get("callback_query")
        if query is not None:
            if not isinstance(query, dict) or "inline_message_id" in query:
                return None
            message = query.get("message")
            if not isinstance(message, dict) or message.get("date") == 0:
                return None
            actor, chat = query.get("from"), message.get("chat")
            callback_id = query.get("id")
            if not isinstance(callback_id, str) or not _CALLBACK_ID.fullmatch(
                callback_id
            ):
                return None
            action = query.get("data")
            if not isinstance(action, str) or len(action.encode("utf-8")) > 64:
                return None
        else:
            message = update.get("message")
            if not isinstance(message, dict):
                return None
            actor, chat = message.get("from"), message.get("chat")
            if message.get("text") not in ("/start", "/menu", "/motores", "/us500"):
                return None
            callback_id = None
            action = "u5:menu" if message["text"] == "/us500" else "u5:panel"
        if not self._authorized(actor, chat, message):
            return None
        return {
            "update_id": update["update_id"],
            "payload_hash": _digest(serialized),
            "callback_id": callback_id,
            "action": action,
        }

    def _message(self, text, keyboard=None):
        payload = {"chat_id": self.owner_chat_id, "text": text}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        return {"method": "sendMessage", "payload": payload}

    def _menu(self, state):
        return self._message(
            "US500 — simulación local\nPermiso de replay: "
            + ("habilitado" if state["enabled"] else "pausado")
            + ".\nNo inicia una estrategia automática. El control no reactiva una pausa de riesgo ni cierra posiciones.",
            [
                [{"text": "Activar simulación US500", "callback_data": "u5:enable"}],
                [
                    {"text": "Pausar US500", "callback_data": "u5:pause"},
                    {"text": "Estado", "callback_data": "u5:status"},
                ],
                BACK,
            ],
        )

    def _audit(self, db, now, action, reason, revision, *, local=False):
        db.execute(
            "INSERT INTO paper_control_audit(at_ms,action,reason,revision,actor_id,chat_id) VALUES(?,?,?,?,?,?)",
            (
                now,
                action,
                reason,
                revision,
                0 if local else self.owner_user_id,
                0 if local else self.owner_chat_id,
            ),
        )
        db.execute(
            "DELETE FROM paper_control_audit WHERE seq NOT IN (SELECT seq FROM paper_control_audit ORDER BY seq DESC LIMIT ?)",
            (self.max_audit,),
        )

    def _action(self, db, parsed, now, state):
        action = parsed["action"]
        if action == "u5:panel":
            return [self._message(*overview(bool(state["enabled"])))]
        if action.startswith("u5:engine:"):
            detail = disconnected_detail(action[len("u5:engine:") :])
            if detail is not None:
                return [self._message(*detail)]
        if action.startswith("u5:blocked:"):
            parts = action.split(":")
            if len(parts) == 4 and parts[3] in ("enable", "pause"):
                detail = disconnected_detail(parts[2], requested_action=parts[3])
                if detail is not None:
                    self._audit(
                        db,
                        now,
                        "REJECT_DISCONNECTED",
                        "NO_ADAPTER_" + parts[2].upper(),
                        state["revision"],
                    )
                    return [self._message(*detail)]
        if action in ("u5:menu", "u5:status"):
            return [self._menu(state)]
        if action == "u5:pause":
            revision = state["revision"] + 1
            db.execute(
                "UPDATE paper_control_meta SET enabled=0,revision=?,reason='OWNER_PAUSED',updated_ms=? WHERE id=1",
                (revision, now),
            )
            db.execute("DELETE FROM paper_control_pending")
            self._audit(db, now, "PAUSE", "OWNER_PAUSED", revision)
            return [
                self._message(
                    "Permiso de simulación US500 pausado. Se conservan reservas y posiciones; las pausas de riesgo permanecen.",
                    [[{"text": "Ver US500", "callback_data": "u5:menu"}], BACK],
                )
            ]
        if action == "u5:enable":
            if state["enabled"]:
                return [
                    self._message(
                        "La simulación US500 ya está activa. Las pausas de riesgo siguen vigentes."
                    )
                ]
            nonce = secrets.token_urlsafe(18)
            db.execute(
                "INSERT OR REPLACE INTO paper_control_pending VALUES(1,?,?,?,?,?)",
                (
                    _digest(nonce),
                    self.owner_user_id,
                    self.owner_chat_id,
                    state["revision"],
                    now + self.ttl,
                ),
            )
            self._audit(
                db, now, "REQUEST_ENABLE", "CONFIRMATION_REQUIRED", state["revision"]
            )
            return [
                self._message(
                    "Confirmá activar únicamente la simulación local US500. La confirmación vence pronto y se usa una vez.",
                    [
                        [
                            {
                                "text": "Confirmar simulación US500",
                                "callback_data": "u5:confirm:" + nonce,
                            }
                        ],
                        [{"text": "Cancelar y pausar", "callback_data": "u5:pause"}],
                    ],
                )
            ]
        if action.startswith("u5:confirm:"):
            nonce = action[len("u5:confirm:") :]
            pending = db.execute(
                "SELECT * FROM paper_control_pending WHERE id=1"
            ).fetchone()
            valid = (
                bool(_NONCE.fullmatch(nonce))
                and pending is not None
                and secrets.compare_digest(pending["nonce_hash"], _digest(nonce))
                and pending["actor_id"] == self.owner_user_id
                and pending["chat_id"] == self.owner_chat_id
                and pending["revision"] == state["revision"]
                and now < pending["expires_ms"]
                and not state["enabled"]
            )
            if not valid:
                self._audit(
                    db,
                    now,
                    "REJECT_CONFIRMATION",
                    "INVALID_OR_EXPIRED_CONFIRMATION",
                    state["revision"],
                )
                return [
                    self._message(
                        "Confirmación inválida, vencida o ya usada. Abrí /us500 para volver a solicitarla."
                    )
                ]
            revision = state["revision"] + 1
            db.execute(
                "UPDATE paper_control_meta SET enabled=1,revision=?,reason='OWNER_CONFIRMED_PAPER',updated_ms=? WHERE id=1",
                (revision, now),
            )
            db.execute("DELETE FROM paper_control_pending")
            self._audit(db, now, "ENABLE", "OWNER_CONFIRMED_PAPER", revision)
            return [
                self._message(
                    "Permiso de simulación US500 habilitado. No inicia una estrategia automática. El motor conserva todos sus controles y pausas de riesgo.",
                    [
                        [
                            {
                                "text": "Pausar simulación US500",
                                "callback_data": "u5:pause",
                            }
                        ],
                        BACK,
                    ],
                )
            ]
        return [self._message("Acción desconocida. Usá /us500.")]

    def handle_update(self, update, *, now_ms):
        """Pure Telegram adapter plus durable control; no external side effects."""
        now = _time(now_ms)
        parsed = self._parse(update)
        if parsed is None:
            return []
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            state = self._read(db)
            cached = db.execute(
                "SELECT payload_hash,response FROM paper_control_updates WHERE update_id=?",
                (parsed["update_id"],),
            ).fetchone()
            if cached:
                if cached["payload_hash"] != parsed["payload_hash"]:
                    raise ControlError("UPDATE_ID_CONFLICT")
                result = json.loads(cached["response"])
                db.commit()
                return result
            if parsed["update_id"] <= state["last_update_id"]:
                db.commit()
                return []
            if now < state["last_seen_ms"]:
                raise ControlError("CONTROL_CLOCK_REGRESSION")
            responses = []
            if parsed["callback_id"] is not None:
                responses.append(
                    {
                        "method": "answerCallbackQuery",
                        "payload": {"callback_query_id": parsed["callback_id"]},
                    }
                )
            responses.extend(self._action(db, parsed, now, state))
            db.execute(
                "UPDATE paper_control_meta SET last_update_id=?,last_seen_ms=? WHERE id=1",
                (parsed["update_id"], now),
            )
            db.execute(
                "INSERT INTO paper_control_updates VALUES(?,?,?)",
                (parsed["update_id"], parsed["payload_hash"], _canonical(responses)),
            )
            db.execute(
                "DELETE FROM paper_control_updates WHERE update_id NOT IN (SELECT update_id FROM paper_control_updates ORDER BY update_id DESC LIMIT ?)",
                (self.max_updates,),
            )
            db.commit()
            return responses
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
