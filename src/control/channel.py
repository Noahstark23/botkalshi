"""Comandos autenticados, confirmados y durables antes de conectar Telegram.

No abre sockets, importa ejecutores ni cambia flags. El transporte entrega bytes;
un consumidor del runtime llama consume con su adaptador CAS. APPLIED exige una
observación posterior atribuible, nunca sólo el retorno del adaptador.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy import MetaData, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool
from sqlmodel import Field, Session, SQLModel, create_engine, select

from src.control.wire import (
    EXITS as EXITS,
)
from src.control.wire import (
    MAX_AGE_MS as MAX_AGE_MS,
)
from src.control.wire import (
    MAX_BYTES as MAX_BYTES,
)
from src.control.wire import (
    MAX_TTL_MS as MAX_TTL_MS,
)
from src.control.wire import (
    SCHEMA as SCHEMA,
)
from src.control.wire import (
    SCOPES as SCOPES,
)
from src.control.wire import (
    Authenticator as Authenticator,
)
from src.control.wire import (
    ControlError as ControlError,
)
from src.control.wire import (
    Intent as Intent,
)
from src.control.wire import (
    _canonical as _canonical,
)
from src.control.wire import (
    _hex as _hex,
)
from src.control.wire import (
    _integer as _integer,
)
from src.control.wire import (
    _require as _require,
)


@dataclass(frozen=True)
class Observation:
    """Lectura del adaptador del runtime, específica del permiso solicitado.

    gates_clear incluye kill-switch, capital, conciliación y dependencias del
    permiso. boot_allowed es el techo inmutable provisionado, no un flag editable
    por chat. El adaptador debe revalidar estos hechos en apply_if_current.
    """

    environment: str
    source_sha: str
    instance_id: str
    limits_sha256: str
    scope: str
    revision: int
    observed_at_ms: int
    boot_allowed: bool
    available: bool
    enabled: bool
    gates_clear: bool
    open_positions: int
    resting_orders: int
    uncertain_orders: int
    dependents_active: bool
    applied_command_id: str | None = None


class RuntimeAdapter(Protocol):
    def observe(self, scope: str) -> Observation: ...

    def apply_if_current(self, intent: Intent) -> None:
        """CAS durable: revalidar binding/revisión/gates y cambiar sólo el permiso.

        No debe levantar pausas, aumentar límites ni enviar órdenes al habilitar.
        Debe persistir command_id con la revisión efectiva en la misma transacción.
        Deshabilitar quotes sólo se confirma tras retiro atribuible de las resting.
        """
        ...


class _Model(SQLModel):
    metadata = MetaData()


class _Meta(_Model, table=True):
    __tablename__ = "motor_channel_meta"
    id: int = Field(default=1, primary_key=True)
    schema_name: str
    config_digest: str
    last_seen_ms: int = 0


class _Command(_Model, table=True):
    __tablename__ = "motor_channel_commands"
    command_id: str = Field(primary_key=True)
    intent_json: str
    state: str
    reason: str
    updated_at_ms: int


class CommandChannel:
    """Journal acotado, aislado del ledger financiero, sin poda automática.

    PROPOSED -> CONFIRMED -> CLAIMED -> APPLIED o UNKNOWN. CLAIMED jamás se
    reutiliza; tras perder proceso/ACK queda incierto hasta observar su efecto.
    La capacidad agotada rechaza propuestas nuevas y conserva consultas/pausas
    ya confirmadas. El transporte y los adaptadores reales se conectan aparte.
    """

    def __init__(
        self,
        path,
        *,
        auth: Authenticator,
        actor_id: int,
        chat_id: int,
        environment: str,
        max_commands: int = 1000,
        clock: Callable[[], int] | None = None,
    ):
        _require(_integer(actor_id, 1) and _integer(chat_id, 1), "INVALID_OWNER")
        _require(
            environment in ("demo", "production") and _integer(max_commands, 1, 10000),
            "INVALID_CONFIGURATION",
        )
        self.path = Path(path)
        self.auth, self.actor_id, self.chat_id = auth, actor_id, chat_id
        self.environment, self.max_commands = environment, max_commands
        self.clock = clock if clock is not None else lambda: time.time_ns() // 1000000
        _require(str(path) != ":memory:" and not self.path.is_symlink(), "INVALID_DATABASE")
        parent = self.path.absolute().parent.stat()
        _require(
            parent.st_uid == os.getuid() and parent.st_mode & 0o022 == 0,
            "UNSAFE_DATABASE_DIRECTORY",
        )
        created = False
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            _require(self.path.is_file(), "INVALID_DATABASE")
        else:
            os.close(fd)
            created = True
        self.identity = self._identity()
        self.engine = create_engine(
            URL.create("sqlite", database=str(self.path.absolute())),
            connect_args={"timeout": 5},
            poolclass=NullPool,
        )
        config = auth.mac(
            "configuration",
            {
                "actor": actor_id,
                "chat": chat_id,
                "environment": environment,
                "max_commands": max_commands,
            },
        )
        tables = set(inspect(self.engine).get_table_names())
        if created:
            _require(not tables, "FOREIGN_DATABASE")
            _Model.metadata.create_all(self.engine)
            with Session(self.engine) as session:
                session.add(_Meta(schema_name=SCHEMA, config_digest=config))
                session.commit()
        with Session(self.engine) as session:
            _require(
                set(inspect(self.engine).get_table_names()) == set(_Model.metadata.tables),
                "FOREIGN_DATABASE",
            )
            meta = session.get(_Meta, 1)
            _require(
                meta is not None
                and meta.schema_name == SCHEMA
                and hmac.compare_digest(meta.config_digest, config),
                "CONFIGURATION_MISMATCH",
            )

    def _identity(self):
        stat = self.path.stat(follow_symlinks=False)
        _require(
            not self.path.is_symlink()
            and self.path.is_file()
            and stat.st_uid == os.getuid()
            and stat.st_mode & 0o077 == 0,
            "UNSAFE_DATABASE",
        )
        return stat.st_dev, stat.st_ino

    @contextmanager
    def _transaction(self, now_ms):
        _require(_integer(now_ms, 946684800000), "INVALID_TIME")
        _require(self._identity() == self.identity, "DATABASE_REPLACED")
        with Session(self.engine) as session:
            session.exec(text("BEGIN IMMEDIATE"))
            meta = session.get(_Meta, 1)
            _require(meta is not None and now_ms >= meta.last_seen_ms, "CLOCK_REGRESSION")
            meta.last_seen_ms = now_ms
            session.add(meta)
            yield session
            session.commit()

    def _authorize(self, intent: Intent, now_ms: int):
        _require((intent.actor_id, intent.chat_id) == (self.actor_id, self.chat_id), "WRONG_OWNER")
        _require(intent.environment == self.environment, "WRONG_ENVIRONMENT")
        _require(intent.issued_at_ms <= now_ms < intent.expires_at_ms, "EXPIRED_OR_FUTURE")

    def _observe(self, intent: Intent, adapter: RuntimeAdapter, now_ms: int) -> Observation:
        try:
            value = adapter.observe(intent.scope)
        except Exception:
            raise ControlError("OBSERVATION_UNAVAILABLE") from None
        after_ms = self.clock()
        _require(_integer(after_ms) and after_ms >= now_ms, "CLOCK_REGRESSION")
        now_ms = after_ms
        _require(type(value) is Observation, "UNKNOWN_OBSERVATION")
        for name in ("environment", "source_sha", "instance_id", "limits_sha256", "scope"):
            _require(getattr(value, name) == getattr(intent, name), "BINDING_CHANGED")
        _require(
            _integer(value.observed_at_ms) and 0 <= now_ms - value.observed_at_ms <= MAX_AGE_MS,
            "STALE_OBSERVATION",
        )
        _require(
            _integer(value.revision)
            and all(
                _integer(getattr(value, f))
                for f in ("open_positions", "resting_orders", "uncertain_orders")
            ),
            "UNKNOWN_OBSERVATION",
        )
        _require(
            all(
                type(getattr(value, f)) is bool
                for f in (
                    "boot_allowed",
                    "available",
                    "enabled",
                    "gates_clear",
                    "dependents_active",
                )
            ),
            "UNKNOWN_OBSERVATION",
        )
        return value

    def _preflight(self, intent, adapter, now_ms):
        self._authorize(intent, now_ms)
        value = self._observe(intent, adapter, now_ms)
        _require(value.revision == intent.expected_revision, "REVISION_CHANGED")
        _require(value.available, "ADAPTER_UNAVAILABLE")
        if intent.enabled:
            _require(
                value.boot_allowed and value.gates_clear and value.uncertain_orders == 0,
                "ENABLE_BLOCKED",
            )
        elif intent.scope in EXITS:
            _require(
                value.open_positions == value.resting_orders == value.uncertain_orders == 0,
                "EXIT_PROTECTION_REQUIRED",
            )
        elif intent.scope == "m2.fair":
            _require(not value.dependents_active, "FAIR_DEPENDENCY_ACTIVE")
        # Observar puede tardar; el plazo no se congela al entrar a la función.
        self._authorize(intent, self.clock())

    def _receipt(self, row):
        body = {
            "schema": SCHEMA,
            "command_id": row.command_id,
            "state": row.state,
            "reason": row.reason,
            "updated_at_ms": row.updated_at_ms,
            "intent_sha256": hashlib.sha256(row.intent_json.encode()).hexdigest(),
        }
        if row.state == "PROPOSED":
            body["confirmation"] = self.auth.mac("confirmation", json.loads(row.intent_json))
        return body

    def handle(self, wire: bytes, *, adapter: RuntimeAdapter) -> bytes:
        """Sólo propose/confirm/status; consumir nunca es una operación remota."""
        request = self.auth.open("request", wire)
        now_ms = self.clock()
        _require(set(request) == {"operation", "intent", "confirmation"}, "INVALID_REQUEST")
        operation = request["operation"]
        _require(operation in ("propose", "confirm", "status"), "INVALID_OPERATION")
        intent = Intent.parse(request["intent"])
        _require(
            (intent.actor_id, intent.chat_id) == (self.actor_id, self.chat_id)
            and intent.environment == self.environment,
            "WRONG_OWNER_OR_ENVIRONMENT",
        )
        if operation != "confirm":
            _require(request["confirmation"] is None, "UNEXPECTED_CONFIRMATION")
        encoded = _canonical(asdict(intent))
        with self._transaction(now_ms) as session:
            row = session.get(_Command, intent.command_id)
            if row is not None:
                _require(row.intent_json == encoded, "COMMAND_ID_CONFLICT")
            if operation == "status":
                _require(row is not None, "UNKNOWN_COMMAND")
            elif operation == "propose" and row is None:
                self._preflight(intent, adapter, now_ms)
                count = len(
                    session.exec(select(_Command.command_id).limit(self.max_commands)).all()
                )
                _require(count < self.max_commands, "JOURNAL_FULL")
                row = _Command(
                    command_id=intent.command_id,
                    intent_json=encoded,
                    state="PROPOSED",
                    reason="CONFIRMATION_REQUIRED",
                    updated_at_ms=now_ms,
                )
                session.add(row)
            elif operation == "confirm":
                _require(row is not None, "UNKNOWN_COMMAND")
                token = request["confirmation"]
                _require(
                    _hex(token, 64)
                    and hmac.compare_digest(token, self.auth.mac("confirmation", asdict(intent))),
                    "INVALID_CONFIRMATION",
                )
                if row.state == "PROPOSED":
                    self._preflight(intent, adapter, now_ms)
                    row.state, row.reason, row.updated_at_ms = (
                        "CONFIRMED",
                        "AWAITING_RUNTIME",
                        now_ms,
                    )
                    session.add(row)
            response = self._receipt(row)
        return self.auth.seal("response", response)

    def consume(self, command_id: str, *, adapter: RuntimeAdapter) -> dict:
        """Un claim durable antes del efecto. Cualquier ambigüedad impide repetir."""
        now_ms = self.clock()
        with self._transaction(now_ms) as session:
            row = session.get(_Command, command_id)
            _require(row is not None, "UNKNOWN_COMMAND")
            if row.state != "CONFIRMED":
                return self._receipt(row)
            intent = Intent.parse(json.loads(row.intent_json))
            try:
                self._preflight(intent, adapter, now_ms)
                self._authorize(intent, self.clock())
            except ControlError as error:
                row.state, row.reason = "BLOCKED", str(error)
                row.updated_at_ms = now_ms
                session.add(row)
                return self._receipt(row)
            row.state, row.reason, row.updated_at_ms = "CLAIMED", "EFFECT_NOT_YET_VERIFIED", now_ms
            session.add(row)
        # El adaptador vuelve a comprobar CAS y gates en su propia transacción.
        # Si muere el proceso, CLAIMED queda durable y no se vuelve a ejecutar.
        try:
            adapter.apply_if_current(intent)
        except Exception:
            return self._finish(command_id, "UNKNOWN", "APPLY_UNCERTAIN", self.clock())
        return self.reconcile(command_id, adapter=adapter)

    def _finish(self, command_id, state, reason, now_ms):
        with self._transaction(now_ms) as session:
            row = session.get(_Command, command_id)
            _require(row is not None, "UNKNOWN_COMMAND")
            if row.state in {"CLAIMED", "UNKNOWN"}:
                row.state, row.reason, row.updated_at_ms = state, reason, now_ms
                session.add(row)
            return self._receipt(row)

    def reconcile(self, command_id: str, *, adapter: RuntimeAdapter) -> dict:
        """Consulta atribuible, sin llamar apply ni liberar incertidumbre por ausencia."""
        now_ms = self.clock()
        with self._transaction(now_ms) as session:
            row = session.get(_Command, command_id)
            _require(row is not None, "UNKNOWN_COMMAND")
            if row.state not in {"CLAIMED", "UNKNOWN"}:
                return self._receipt(row)
            intent = Intent.parse(json.loads(row.intent_json))
        try:
            value = self._observe(intent, adapter, now_ms)
            _require(
                value.available
                and value.applied_command_id == command_id
                and value.revision == intent.expected_revision + 1
                and value.enabled is intent.enabled,
                "EFFECT_NOT_ATTRIBUTABLE",
            )
            if intent.scope == "m5.quotes" and not intent.enabled:
                _require(
                    value.resting_orders == value.uncertain_orders == 0,
                    "QUOTE_WITHDRAWAL_UNCERTAIN",
                )
        except Exception:
            return self._finish(command_id, "UNKNOWN", "EFFECT_NOT_ATTRIBUTABLE", now_ms)
        return self._finish(command_id, "APPLIED", "OBSERVED_EFFECT", now_ms)
