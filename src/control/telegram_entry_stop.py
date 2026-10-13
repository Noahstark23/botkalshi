"""Router opcional para el consumidor Telegram existente; sólo parada/consulta.

Stdlib, sin token, getUpdates, credenciales financieras ni import del runtime.
No se instala ni se añade a ningún runner/servicio automáticamente. El control
US500 conserva su DB, cursor y outbox; esta extensión tiene una DB propia.
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from types import MappingProxyType

from src.control.entry_stop import BINDING_FIELDS, StopRequest, verify_receipt
from src.control.wire import _canonical, _integer, _require

FORWARDED = {
    "forward_origin",
    "forward_from",
    "forward_from_chat",
    "forward_date",
    "forward_sender_name",
    "forward_signature",
    "sender_chat",
    "via_bot",
}
PREFIX = "u5:entries:"
MAX_STOP_REQUESTS = 2000
TABLES = {
    "entry_stop_panel_meta",
    "entry_stop_panel_state",
    "entry_stop_panel_updates",
    "entry_stop_panel_history",
}
LIMITATIONS = (
    "Sólo bloquea nuevas entradas y cotizaciones en rutas conectadas. "
    "Las órdenes previas y UNKNOWN se conservan; no confirma cancelaciones. "
    "Las salidas y la conciliación mantienen sus controles."
)


class TelegramEntryStopRouter:
    def __init__(self, path, *, control, identity, auth, requests, receipts):
        _require(type(identity) is dict and set(identity) == set(BINDING_FIELDS), "INVALID_BINDING")
        _require(identity["environment"] == "demo", "PRODUCTION_BRIDGE_NOT_SUPPORTED")
        self.identity = MappingProxyType(dict(identity))
        self.auth, self.requests, self.receipts = auth, requests, receipts
        self.control = control
        self.owner_user_id, self.owner_chat_id = identity["actor_id"], identity["chat_id"]
        self._request("status", "0" * 32, 946684800000)
        _require(
            control.owner_user_id == self.owner_user_id
            and control.owner_chat_id == self.owner_chat_id,
            "PANEL_OWNER_MISMATCH",
        )
        self.config = auth.mac(
            "entry-stop-panel-config-v1",
            {"identity": identity, "max_stop_requests": MAX_STOP_REQUESTS},
        )
        self.path = Path(path).absolute()
        parent = self.path.parent.stat()
        _require(
            parent.st_uid == os.geteuid() and parent.st_mode & 0o022 == 0,
            "INSECURE_PANEL_DIRECTORY",
        )
        created = False
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            created = True
        except FileExistsError:
            pass
        self.file_identity = self._identity()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if created:
                db.execute(
                    "CREATE TABLE entry_stop_panel_meta (id INTEGER PRIMARY KEY CHECK(id=1), config TEXT NOT NULL, last_seen INTEGER NOT NULL, last_update INTEGER NOT NULL, stop_count INTEGER NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE entry_stop_panel_state (id INTEGER PRIMARY KEY CHECK(id=1), nonce_hash TEXT, expires INTEGER, stop_request TEXT, status_request TEXT, stop_publication TEXT NOT NULL, status_publication TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE entry_stop_panel_updates (update_id INTEGER PRIMARY KEY, digest TEXT NOT NULL, response TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE entry_stop_panel_history (request_id TEXT PRIMARY KEY, request TEXT NOT NULL, publication TEXT NOT NULL)"
                )
                db.execute("INSERT INTO entry_stop_panel_meta VALUES(1,?,0,-1,0)", (self.config,))
                db.execute(
                    "INSERT INTO entry_stop_panel_state VALUES(1,NULL,NULL,NULL,NULL,'NOT_REQUESTED','NOT_REQUESTED')"
                )
            self._meta(db)

    def _identity(self):
        info = self.path.lstat()
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_nlink == 1
            and info.st_uid == os.geteuid()
            and info.st_mode & 0o077 == 0,
            "INSECURE_PANEL_DATABASE",
        )
        return info.st_dev, info.st_ino

    @contextmanager
    def _connect(self):
        _require(self._identity() == self.file_identity, "PANEL_DATABASE_REPLACED")
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _meta(self, db):
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        _require(tables == TABLES, "INVALID_PANEL_DATABASE")
        meta = db.execute("SELECT * FROM entry_stop_panel_meta WHERE id=1").fetchone()
        state = db.execute("SELECT * FROM entry_stop_panel_state WHERE id=1").fetchone()
        _require(
            meta is not None and state is not None and meta["config"] == self.config,
            "PANEL_CONFIG_CHANGED",
        )
        _require(
            all(
                state[key] in {"NOT_REQUESTED", "PUBLISH_UNCONFIRMED", "PUBLISHED_AWAITING_RECEIPT"}
                for key in ("stop_publication", "status_publication")
            ),
            "INVALID_PUBLICATION_STATE",
        )
        count = db.execute("SELECT count(*) FROM entry_stop_panel_history").fetchone()[0]
        _require(
            _integer(meta["stop_count"], 0, MAX_STOP_REQUESTS)
            and count == meta["stop_count"]
            and (count > 0) == (state["stop_request"] is not None),
            "STOP_HISTORY_MISMATCH",
        )
        if state["stop_request"] is not None:
            stored = StopRequest.parse(json.loads(state["stop_request"]))
            history = db.execute(
                "SELECT request,publication FROM entry_stop_panel_history WHERE request_id=?",
                (stored.request_id,),
            ).fetchone()
            _require(
                history is not None
                and history["request"] == state["stop_request"]
                and history["publication"] == state["stop_publication"],
                "STOP_HISTORY_MISMATCH",
            )
        return meta, state

    def snapshot(self):
        # Es el permiso de US500, no el estado del latch del runtime.
        with self._connect() as db:
            _, state = self._meta(db)
            return {
                **self.control.snapshot(),
                "entry_stop_environment": "demo",
                "entry_stop_bridge_configured": True,
                "entry_stop_control_available": None,
                "entry_stop_publication": state["stop_publication"],
                "entry_stop_status_publication": state["status_publication"],
            }

    def pause_for_fault(self, *args, **kwargs):
        return self.control.pause_for_fault(*args, **kwargs)

    def set_transport_available(self, available):
        self.control.set_transport_available(available)

    def _request(self, operation, request_id, now):
        return StopRequest.parse(
            {
                **self.identity,
                "operation": operation,
                "request_id": request_id,
                "issued_at_ms": now,
                "expires_at_ms": now + 60000,
            }
        )

    def _parse(self, update):
        if type(update) is not dict or not _integer(update.get("update_id")):
            return None
        if ("message" in update) == ("callback_query" in update):
            return None
        callback_id = None
        if "callback_query" in update:
            query = update["callback_query"]
            if type(query) is not dict or "inline_message_id" in query:
                return None
            actor, message = query.get("from"), query.get("message")
            action, callback_id = query.get("data"), query.get("id")
            if not isinstance(callback_id, str) or not re.fullmatch(r"[!-~]{1,128}", callback_id):
                return None
            if type(action) is not str or len(action.encode()) > 64:
                return None
        else:
            message = update["message"]
            if type(message) is not dict:
                return None
            actor = message.get("from")
            action = {
                "/parar_entradas": PREFIX + "stop",
                "/estado_entradas": PREFIX + "status",
                "/motores": "u5:panel",
                "/start": "u5:panel",
                "/menu": "u5:panel",
            }.get(message.get("text"))
        if type(message) is not dict or message.get("date") == 0:
            return None
        chat = message.get("chat")
        if (
            type(actor) is not dict
            or type(chat) is not dict
            or type(actor.get("id")) is not int
            or actor["id"] != self.owner_user_id
            or actor.get("is_bot") is not False
            or type(chat.get("id")) is not int
            or chat["id"] != self.owner_chat_id
            or chat.get("type") != "private"
            or FORWARDED.intersection(message)
            or message.get("is_automatic_forward") is True
        ):
            return None
        if action != "u5:panel" and not (type(action) is str and action.startswith(PREFIX)):
            return None
        return action, callback_id

    def _message(self, text, buttons=None):
        return {
            "method": "sendMessage",
            "payload": {
                "chat_id": self.owner_chat_id,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": buttons
                    or [
                        [
                            {
                                "text": "Consultar parada de entradas",
                                "callback_data": PREFIX + "status",
                            }
                        ],
                        [{"text": "Ver motores", "callback_data": "u5:panel"}],
                    ]
                },
            },
        }

    def _result(self, state, operation, now):
        # Aun con publicación incierta puede existir recibo: comprobarlo primero.
        try:
            raw = state[operation + "_request"]
            _require(raw is not None, "NO_STOP_REQUEST")
            request = StopRequest.parse(json.loads(raw))
            wire = self.receipts.read(operation)
            status = verify_receipt(self.auth, request, wire, now_ms=now)
        except Exception:
            detail = (
                " La publicación al buzón quedó sin confirmar; requiere diagnóstico local."
                if state[operation + "_publication"] == "PUBLISH_UNCONFIRMED"
                else ""
            )
            return self._message(
                "Resultado UNKNOWN: falta un recibo válido y vigente."
                + detail
                + " Consultá el estado; no se reenvía la parada."
            )
        if status.latched:
            message = "Parada registrada en el ledger. Nuevas entradas bloqueadas. " + LIMITATIONS
        else:
            message = "No se observó una parada de entradas en esta consulta. Esto no habilita motores ni acredita preparación financiera."
        return self._message(message)

    def handle_update(self, update, *, now_ms):
        parsed = self._parse(update)
        if parsed is None:
            return self.control.handle_update(update, now_ms=now_ms)
        _require(_integer(now_ms, 946684800000), "INVALID_TIME")
        action, callback_id = parsed
        digest = hashlib.sha256(_canonical(update).encode()).hexdigest()
        publication = None
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            meta, state = self._meta(db)
            prior = db.execute(
                "SELECT digest,response FROM entry_stop_panel_updates WHERE update_id=?",
                (update["update_id"],),
            ).fetchone()
            if prior:
                _require(prior["digest"] == digest, "UPDATE_ID_CONFLICT")
                return json.loads(prior["response"])
            if update["update_id"] <= meta["last_update"]:
                return []
            _require(now_ms >= meta["last_seen"], "CLOCK_REGRESSION")
            ack = (
                [{"method": "answerCallbackQuery", "payload": {"callback_query_id": callback_id}}]
                if callback_id
                else []
            )
            if action == "u5:panel":
                responses = self.control.handle_update(update, now_ms=now_ms)
                for response in responses:
                    if response.get("method") == "sendMessage":
                        keyboard = (
                            response["payload"]
                            .setdefault("reply_markup", {})
                            .setdefault("inline_keyboard", [])
                        )
                        keyboard.append(
                            [{"text": "Parar nuevas entradas", "callback_data": PREFIX + "stop"}]
                        )
            elif action == PREFIX + "stop":
                nonce = secrets.token_hex(16)
                db.execute(
                    "UPDATE entry_stop_panel_state SET nonce_hash=?,expires=? WHERE id=1",
                    (hashlib.sha256(nonce.encode()).hexdigest(), now_ms + 60000),
                )
                responses = ack + [
                    self._message(
                        "Confirmá la parada de nuevas entradas. " + LIMITATIONS,
                        [
                            [
                                {
                                    "text": "Confirmar parada de entradas",
                                    "callback_data": PREFIX + "confirm:" + nonce,
                                }
                            ],
                            [{"text": "Volver", "callback_data": "u5:panel"}],
                        ],
                    )
                ]
            elif action.startswith(PREFIX + "result:"):
                operation = action[len(PREFIX + "result:") :]
                responses = (
                    ack + [self._result(state, operation, now_ms)]
                    if operation in ("stop", "status")
                    else ack
                )
            else:
                confirmed = action.startswith(PREFIX + "confirm:")
                nonce = action[len(PREFIX + "confirm:") :] if confirmed else ""
                valid = (
                    confirmed
                    and re.fullmatch(r"[0-9a-f]{32}", nonce)
                    and state["nonce_hash"] is not None
                    and _integer(state["expires"])
                    and now_ms < state["expires"]
                    and secrets.compare_digest(
                        state["nonce_hash"], hashlib.sha256(nonce.encode()).hexdigest()
                    )
                )
                previous = (
                    StopRequest.parse(json.loads(state["stop_request"]))
                    if state["stop_request"] is not None
                    else None
                )
                full = (
                    db.execute("SELECT count(*) FROM entry_stop_panel_history").fetchone()[0]
                    >= MAX_STOP_REQUESTS
                )
                if valid and (full or (previous is not None and now_ms < previous.expires_at_ms)):
                    db.execute(
                        "UPDATE entry_stop_panel_state SET nonce_hash=NULL,expires=NULL WHERE id=1"
                    )
                    responses = ack + [
                        self._message(
                            "Historial de paradas lleno: se conserva toda incertidumbre; requiere diagnóstico local."
                            if full
                            else "Ya existe una solicitud de parada vigente. Consultá su estado; no se reenvía."
                        )
                    ]
                elif action == PREFIX + "status" or valid:
                    operation = "stop" if valid else "status"
                    request_id = self.auth.mac(
                        "entry-stop-update-v1",
                        {"update_id": update["update_id"], "config": self.config},
                    )[:32]
                    request = self._request(operation, request_id, now_ms)
                    column = "stop_request" if valid else "status_request"
                    db.execute(
                        f"UPDATE entry_stop_panel_state SET {column}=?,{operation}_publication='PUBLISH_UNCONFIRMED' WHERE id=1",
                        (_canonical(asdict(request)),),
                    )
                    if valid:
                        db.execute(
                            "UPDATE entry_stop_panel_meta SET stop_count=stop_count+1 WHERE id=1"
                        )
                        db.execute(
                            "INSERT INTO entry_stop_panel_history VALUES(?,?,'PUBLISH_UNCONFIRMED')",
                            (request.request_id, _canonical(asdict(request))),
                        )
                        db.execute(
                            "UPDATE entry_stop_panel_state SET nonce_hash=NULL,expires=NULL WHERE id=1"
                        )
                    publication = (
                        operation,
                        self.auth.seal("entry-stop-request-v1", asdict(request)),
                    )
                    responses = ack + [
                        self._message(
                            "Solicitud registrada; resultado pendiente de recibo del runtime. No implica ejecución ni cancelación de órdenes.",
                            [
                                [
                                    {
                                        "text": "Ver recibo",
                                        "callback_data": PREFIX + "result:" + operation,
                                    }
                                ],
                                [
                                    {
                                        "text": "Consultar estado nuevo",
                                        "callback_data": PREFIX + "status",
                                    }
                                ],
                            ],
                        )
                    ]
                else:
                    responses = ack + [
                        self._message(
                            "Confirmación inválida, vencida o ya usada. Abrí de nuevo la parada de entradas."
                        )
                    ]
            db.execute(
                "INSERT INTO entry_stop_panel_updates VALUES(?,?,?)",
                (update["update_id"], digest, _canonical(responses)),
            )
            db.execute(
                "UPDATE entry_stop_panel_meta SET last_seen=?,last_update=? WHERE id=1",
                (now_ms, update["update_id"]),
            )
            db.execute(
                "DELETE FROM entry_stop_panel_updates WHERE update_id NOT IN (SELECT update_id FROM entry_stop_panel_updates ORDER BY update_id DESC LIMIT 2000)"
            )
        # Antes del I/O queda registrado el intento. Un crash o falta de recibo
        # conserva UNKNOWN; reentregar el update sólo devuelve la respuesta guardada.
        if publication is not None:
            operation, wire = publication
            outcome = "PUBLISH_UNCONFIRMED"
            try:
                self.requests.publish(operation, wire)
                outcome = "PUBLISHED_AWAITING_RECEIPT"
            except Exception:
                pass  # Se conserva el intento incierto; jamás copiar texto del fallo.
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._meta(db)
                db.execute(
                    f"UPDATE entry_stop_panel_state SET {operation}_publication=? WHERE id=1 AND {operation}_request=?",
                    (outcome, _canonical(asdict(request))),
                )
                if operation == "stop":
                    db.execute(
                        "UPDATE entry_stop_panel_history SET publication=? WHERE request_id=? AND request=?",
                        (outcome, request.request_id, _canonical(asdict(request))),
                    )
        return responses
