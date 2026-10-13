"""Permisos CAS y atribución de admisión en la misma DB que las reservas.

Sin aprovisionamiento implícito, transporte, cliente financiero ni conexión al
runner. El proveedor local de hechos debe leer fuentes autoritativas bajo la
transacción recibida; nunca hace red, commit ni inventa gates por falta de datos.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import MetaData, func, inspect, text
from sqlmodel import Field, Session, SQLModel, select

from src.control.channel import (
    EXITS,
    MAX_AGE_MS,
    ControlError,
    Intent,
    Observation,
    _canonical,
    _hex,
    _integer,
    _require,
)
from src.control.entry_stop import BINDING_FIELDS, EntryStopStatus, StopRequest
from src.storage.models import Trade, kill_switch_engaged

# Sólo estos scopes tienen un punto de admisión de órdenes identificable.
STRATEGIES = {
    "m1.entries": ("motor_1_arbitrage", {"buy"}),
    "m2.entries": ("motor_2_consensus", {"buy"}),
    "m2.exits": ("motor_2_consensus", {"sell"}),
    "m3.exits": ("motor_3_clv", {"sell"}),
    "m5.quotes": ("motor_5_mm", {"buy", "sell"}),
    "rest.entries": ("motor_rest_arb", {"buy"}),
}


MAX_REBINDS = 128


@dataclass(frozen=True)
class PermissionBinding:
    actor_id: int
    chat_id: int
    environment: str
    source_sha: str
    instance_id: str
    limits_sha256: str
    # Registro explícito de scopes cableados. No se deriva de flags ni conteos.
    scopes: frozenset[str]
    boot_allowed: frozenset[str]
    max_commands: int = 1000
    max_admissions: int = 100000

    def canonical(self):
        _require(
            _integer(self.actor_id, 1)
            and _integer(self.chat_id, 1)
            and self.environment in {"demo", "production"}
            and _hex(self.source_sha, 40)
            and _hex(self.instance_id, 32)
            and _hex(self.limits_sha256, 64),
            "INVALID_BINDING",
        )
        _require(
            type(self.scopes) is frozenset
            and type(self.boot_allowed) is frozenset
            and bool(self.scopes)
            and self.scopes <= STRATEGIES.keys()
            and self.boot_allowed <= self.scopes
            and _integer(self.max_commands, 1, 10000)
            and _integer(self.max_admissions, 1, 100000),
            "INVALID_BINDING",
        )
        value = asdict(self)
        value.update(scopes=sorted(self.scopes), boot_allowed=sorted(self.boot_allowed))
        return _canonical(value)

    def digest(self):
        return hashlib.sha256(self.canonical().encode()).hexdigest()

    @classmethod
    def parse(cls, raw):
        _require(type(raw) is str and len(raw.encode()) <= 8192, "INVALID_BINDING_METADATA")
        try:
            value = json.loads(raw)
            _require(
                type(value) is dict and set(value) == set(cls.__dataclass_fields__),
                "INVALID_BINDING_METADATA",
            )
            for name in ("scopes", "boot_allowed"):
                _require(type(value[name]) is list, "INVALID_BINDING_METADATA")
                value[name] = frozenset(value[name])
            binding = cls(**value)
            _require(binding.canonical() == raw, "INVALID_BINDING_METADATA")
            return binding
        except (TypeError, ValueError, RecursionError):
            raise ControlError("INVALID_BINDING_METADATA") from None

    def stable(self):
        return {
            key: value
            for key, value in asdict(self).items()
            if key not in {"source_sha", "instance_id"}
        }


@dataclass(frozen=True)
class GateFacts:
    """Hechos locales; cero filas nunca sustituye conciliación/capital/dependencias."""

    observed_at_ms: int
    available: bool
    gates_clear: bool
    open_positions: int
    resting_orders: int
    uncertain_orders: int
    dependents_active: bool = False

    def validate(self, now):
        _require(
            _integer(self.observed_at_ms) and 0 <= now - self.observed_at_ms <= MAX_AGE_MS,
            "STALE_GATE_FACTS",
        )
        _require(
            all(
                type(getattr(self, f)) is bool
                for f in ("available", "gates_clear", "dependents_active")
            )
            and all(
                _integer(getattr(self, f))
                for f in ("open_positions", "resting_orders", "uncertain_orders")
            ),
            "UNKNOWN_GATE_FACTS",
        )


class _Model(SQLModel):
    metadata = MetaData()


class _Meta(_Model, table=True):
    __tablename__ = "motor_permissions_meta"
    id: int = Field(default=1, primary_key=True)
    binding_digest: str
    binding_json: str
    binding_generation: int = 0
    last_seen_ms: int
    command_count: int = 0
    admission_count: int = 0
    entry_stop_state: str = "clear"
    entry_stop_request_id: str | None = None
    entry_stop_request_digest: str | None = None
    entry_stop_at_ms: int | None = None


class _Permission(_Model, table=True):
    __tablename__ = "motor_permissions"
    scope: str = Field(primary_key=True)
    state: str = "disabled"
    revision: int = 0
    command_id: str | None = None
    transition_generation: int | None = None


class _Applied(_Model, table=True):
    __tablename__ = "motor_permission_commands"
    command_id: str = Field(primary_key=True)
    intent_digest: str
    binding_digest: str
    binding_generation: int
    scope: str
    revision: int
    state: str


class _Transition(_Model, table=True):
    """Auditoría propia: no se hace pasar por comando humano ni consume su cupo."""

    __tablename__ = "motor_permission_transitions"
    generation: int = Field(primary_key=True)
    from_json: str
    to_json: str
    at_ms: int
    resets_json: str


@dataclass(frozen=True)
class HistoricalPermissionWrite:
    command_id: str
    intent_digest: str
    binding_digest: str
    binding_generation: int
    revision: int
    enabled: bool
    fenced: bool
    # Prueba de escritura, nunca recibo de retirada M5 ni autoridad presente.
    current_permission_proven: bool = False
    quotes_retired_proven: bool = False


class Admission(_Model, table=True):
    """Evidencia original inmutable; una fila por intent, no por fill o split."""

    __tablename__ = "motor_permission_admissions"
    client_order_id: str = Field(primary_key=True)
    scope: str
    permission_revision: int
    command_id: str
    source_sha: str
    instance_id: str
    limits_sha256: str
    admitted_at_ms: int
    intent_digest: str


class DurablePermissionAdapter:
    def __init__(self, engine, binding: PermissionBinding, facts, *, clock=None):
        _require(engine.dialect.name == "sqlite", "UNSUPPORTED_DATABASE")
        _require(
            bool(engine.url.database) and engine.url.database != ":memory:", "NON_DURABLE_DATABASE"
        )
        self.path = Path(engine.url.database).absolute()
        self.identity = self._identity()
        self.engine, self.binding, self.facts = engine, binding, facts
        self.digest = binding.digest()
        self.clock = clock or (lambda: time.time_ns() // 1000000)

    def _identity(self):
        _require(not self.path.is_symlink() and self.path.is_file(), "INVALID_LEDGER_FILE")
        st = self.path.stat()
        return st.st_dev, st.st_ino

    def provision(self):
        """Preparación explícita: sólo crea permisos inicialmente deshabilitados.

        No se llama desde constructor, runner o admisión. No resetear una DB
        existente, reparar metadatos ni cambiar identidad/techo automáticamente.
        """
        _require(self._identity() == self.identity, "LEDGER_REPLACED")
        with Session(self.engine) as session:
            session.exec(text("BEGIN IMMEDIATE"))
            expected = set(_Model.metadata.tables)
            present = expected & set(inspect(session.connection()).get_table_names())
            if present:
                # No crear una tabla perdida ni resembrar un meta borrado. DDL y
                # estado inicial sólo nacen juntos en una provisión nueva.
                _require(present == expected, "PARTIAL_PERMISSION_DATABASE")
                existing = session.get(_Meta, 1)
                self._check_meta(existing, self.clock())
                self._check_rows(session, existing)
                return
            _Model.metadata.create_all(session.connection())
            now = self.clock()
            _require(_integer(now, 946684800000), "INVALID_TIME")
            session.add(
                _Meta(
                    binding_digest=self.digest,
                    binding_json=self.binding.canonical(),
                    last_seen_ms=now,
                )
            )
            for scope in sorted(self.binding.scopes):
                session.add(_Permission(scope=scope))
            session.commit()

    @staticmethod
    def _stop_status(meta):
        _require(meta.entry_stop_state in {"clear", "latched"}, "INVALID_ENTRY_STOP")
        return EntryStopStatus(
            meta.entry_stop_state == "latched",
            meta.entry_stop_request_id,
            meta.entry_stop_request_digest,
            meta.entry_stop_at_ms,
        ).validate()

    def _read_entry_stop(self, request):
        # Una consulta no adquiere lock de escritura ni adelanta last_seen_ms.
        # mode=ro impide crear/reparar el ledger por una ruta equivocada.
        with closing(sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA query_only=ON")
            row = db.execute(
                "SELECT binding_digest,binding_json,binding_generation,entry_stop_state,entry_stop_request_id,"
                "entry_stop_request_digest,entry_stop_at_ms FROM motor_permissions_meta WHERE id=1"
            ).fetchone()
            _require(
                row is not None and row["binding_digest"] == self.digest,
                "PERMISSION_BINDING_CHANGED",
            )
            self._stored_binding(SimpleNamespace(**dict(row)))
            request.authorize(self.binding, self.clock())
            return self._stop_status(SimpleNamespace(**dict(row)))

    def entry_stop(self, request: StopRequest):
        """Latch fijo, sin journal, datos de mercado ni límites de admisión.

        El reloj regresivo no impide esta acción unidireccional. Se preserva el
        máximo last_seen_ms; ninguna habilitación ni admisión elude ese bloqueo.
        Lo admitido antes, las salidas, reservas y UNKNOWN no se modifican.
        """
        _require(type(request) is StopRequest, "INVALID_STOP_REQUEST")
        request.authorize(self.binding, self.clock())
        _require(self._identity() == self.identity, "LEDGER_REPLACED")
        if request.operation == "status":
            return self._read_entry_stop(request)
        with Session(self.engine) as session:
            session.exec(text("BEGIN IMMEDIATE"))
            _require(
                session.connection().connection.driver_connection.in_transaction is True,
                "RESERVATION_TRANSACTION_REQUIRED",
            )
            meta = session.get(_Meta, 1)
            _require(
                meta is not None and meta.binding_digest == self.digest,
                "PERMISSION_BINDING_CHANGED",
            )
            self._stored_binding(meta)
            _require(_integer(meta.last_seen_ms, 946684800000), "CLOCK_REGRESSION")
            now = self.clock()
            request.authorize(self.binding, now)
            status = self._stop_status(meta)
            if request.operation == "stop" and not status.latched:
                meta.entry_stop_state = "latched"
                meta.entry_stop_request_id = request.request_id
                meta.entry_stop_request_digest = request.digest()
                meta.entry_stop_at_ms = now
            elif status.first_request_id == request.request_id:
                _require(status.first_request_digest == request.digest(), "STOP_ID_CONFLICT")
            # Revalidar vencimiento inmediatamente antes de persistir.
            after = self._time_after(now)
            request.authorize(self.binding, after)
            meta.last_seen_ms = max(meta.last_seen_ms, after)
            session.add(meta)
            result = self._stop_status(meta)
            session.commit()
            return result

    @staticmethod
    def _stored_binding(meta):
        _require(meta is not None, "PERMISSION_BINDING_CHANGED")
        binding = PermissionBinding.parse(meta.binding_json)
        _require(
            binding.digest() == meta.binding_digest
            and _integer(meta.binding_generation, 0, MAX_REBINDS),
            "INVALID_BINDING_METADATA",
        )
        return binding

    def _check_meta(self, meta, now, *, binding=None):
        binding = binding or self.binding
        stored = self._stored_binding(meta)
        _require(stored == binding, "PERMISSION_BINDING_CHANGED")
        self._stop_status(meta)
        _require(
            _integer(now, 946684800000)
            and _integer(meta.last_seen_ms, 946684800000)
            and now >= meta.last_seen_ms,
            "CLOCK_REGRESSION",
        )
        _require(
            _integer(meta.command_count, 0, binding.max_commands)
            and _integer(meta.admission_count, 0, binding.max_admissions),
            "PERMISSION_COUNTER_INVALID",
        )

    def _history(self, session, meta):
        transitions = session.exec(
            select(_Transition).order_by(_Transition.generation).limit(MAX_REBINDS + 1)
        ).all()
        _require(len(transitions) == meta.binding_generation, "PERMISSION_TRANSITION_MISMATCH")
        bindings, effects = {}, {}
        previous, at_ms, nonces = None, 0, set()
        for generation, transition in enumerate(transitions, 1):
            before = PermissionBinding.parse(transition.from_json)
            after = PermissionBinding.parse(transition.to_json)
            _require(
                transition.generation == generation
                and (previous is None or before == previous)
                and before.stable() == after.stable()
                and before.instance_id != after.instance_id
                and after.instance_id not in nonces
                and _integer(transition.at_ms, 946684800000)
                and at_ms <= transition.at_ms <= meta.last_seen_ms,
                "PERMISSION_TRANSITION_MISMATCH",
            )
            nonces.add(before.instance_id)
            _require(after.instance_id not in nonces, "PERMISSION_TRANSITION_MISMATCH")
            nonces.add(after.instance_id)
            bindings[generation - 1] = before
            bindings[generation] = after
            try:
                resets = json.loads(transition.resets_json)
                _require(
                    type(resets) is dict and _canonical(resets) == transition.resets_json,
                    "PERMISSION_TRANSITION_MISMATCH",
                )
                for scope, revision in resets.items():
                    _require(
                        scope in before.scopes - EXITS and _integer(revision, 2),
                        "PERMISSION_TRANSITION_MISMATCH",
                    )
                    effects[generation, scope] = revision
            except (ValueError, TypeError, RecursionError):
                raise ControlError("PERMISSION_TRANSITION_MISMATCH") from None
            previous, at_ms = after, transition.at_ms
        stored = self._stored_binding(meta)
        _require(previous is None or previous == stored, "PERMISSION_TRANSITION_MISMATCH")
        bindings[meta.binding_generation] = stored
        return bindings, effects

    def _check_rows(self, session, meta, *, binding=None):
        binding = binding or self.binding
        bindings, effects = self._history(session, meta)
        _require(
            session.exec(select(func.count()).select_from(_Meta)).one() == 1
            and session.exec(select(func.count()).select_from(_Applied)).one() == meta.command_count
            and session.exec(select(func.count()).select_from(Admission)).one()
            == meta.admission_count,
            "PERMISSION_HISTORY_MISMATCH",
        )
        rows = session.exec(select(_Permission)).all()
        _require({p.scope for p in rows} == binding.scopes, "PERMISSION_ROWS_CHANGED")
        for row in rows:
            _require(
                row.state in {"enabled", "disabled"} and _integer(row.revision),
                "PERMISSION_STATE_INVALID",
            )
            if row.revision == 0:
                _require(
                    row.command_id is None
                    and row.transition_generation is None
                    and row.state == "disabled",
                    "PERMISSION_STATE_INVALID",
                )
            elif row.transition_generation is not None:
                _require(
                    _integer(row.transition_generation, 1, meta.binding_generation)
                    and row.command_id is None
                    and row.state == "disabled"
                    and effects.get((row.transition_generation, row.scope)) == row.revision,
                    "PERMISSION_ATTRIBUTION_INVALID",
                )
            else:
                _require(_hex(row.command_id, 32), "PERMISSION_STATE_INVALID")
                applied = session.get(_Applied, row.command_id)
                _require(
                    applied is not None
                    and applied.scope == row.scope
                    and applied.revision == row.revision
                    and applied.state == row.state
                    and applied.binding_generation in bindings
                    and applied.binding_digest == bindings[applied.binding_generation].digest(),
                    "PERMISSION_ATTRIBUTION_INVALID",
                )

    def rebind_instance(self, *, expected_previous_digest, expected_generation):
        """Procedimiento local explícito con escritores detenidos, nunca boot/Telegram.

        CAS no equivale a autorización. No necesita el nonce muerto en memoria.
        Entradas habilitadas requieren nueva aprobación; salidas/latch/historia
        se conservan. No migra esquemas legacy ni renueva capacidades.
        """
        _require(
            _hex(expected_previous_digest, 64) and _integer(expected_generation, 0, MAX_REBINDS),
            "INVALID_REBIND_CAS",
        )
        _require(self._identity() == self.identity, "LEDGER_REPLACED")
        with Session(self.engine) as session:
            session.exec(text("BEGIN IMMEDIATE"))
            meta = session.get(_Meta, 1)
            previous = self._stored_binding(meta)
            now = self.clock()
            self._check_meta(meta, now, binding=previous)
            self._check_rows(session, meta, binding=previous)
            _require(
                meta.binding_digest == expected_previous_digest
                and meta.binding_generation == expected_generation,
                "REBIND_CONFLICT",
            )
            _require(previous.stable() == self.binding.stable(), "REBIND_STABLE_IDENTITY_CHANGED")
            bindings, _ = self._history(session, meta)
            _require(
                self.binding.instance_id not in {b.instance_id for b in bindings.values()},
                "REBIND_INSTANCE_REUSED",
            )
            _require(meta.binding_generation < MAX_REBINDS, "REBIND_CAPACITY")
            generation = meta.binding_generation + 1
            resets = {}
            for row in session.exec(select(_Permission)).all():
                if row.scope not in EXITS and row.state == "enabled":
                    _require(_integer(row.revision, 0, 2**53 - 2), "PERMISSION_REVISION_EXHAUSTED")
                    row.state, row.command_id = "disabled", None
                    row.revision += 1
                    row.transition_generation = generation
                    resets[row.scope] = row.revision
                    session.add(row)
            end = self._time_after(now)
            session.add(
                _Transition(
                    generation=generation,
                    from_json=previous.canonical(),
                    to_json=self.binding.canonical(),
                    at_ms=end,
                    resets_json=_canonical(resets),
                )
            )
            meta.binding_digest, meta.binding_json = self.digest, self.binding.canonical()
            meta.binding_generation, meta.last_seen_ms = generation, end
            session.add(meta)
            session.flush()
            self._check_rows(session, meta)
            session.commit()
            return generation

    def recovery_announcement(self, auth, *, generation=None):
        """Atestación de un corte conservado, emitida por el dueño actual.

        Permite alcanzar al ledger sin perder cortes. No prueba que la instancia
        de destino histórica siga viva; adoptar toda la cadena con panel detenido.
        """
        _require(self._identity() == self.identity, "LEDGER_REPLACED")
        with Session(self.engine) as session:
            session.exec(text("BEGIN"))
            meta = session.get(_Meta, 1)
            now = self.clock()
            self._check_meta(meta, now)
            self._check_rows(session, meta)
            generation = meta.binding_generation if generation is None else generation
            _require(_integer(generation, 1, meta.binding_generation), "NO_PERMISSION_TRANSITION")
            transition = session.get(_Transition, generation)
            _require(transition is not None, "NO_PERMISSION_TRANSITION")
            previous = PermissionBinding.parse(transition.from_json)
            target = PermissionBinding.parse(transition.to_json)
            return auth.seal(
                "entry-stop-rebind-v1",
                {
                    "from_identity": {k: getattr(previous, k) for k in BINDING_FIELDS},
                    "to_identity": {k: getattr(target, k) for k in BINDING_FIELDS},
                    "from_permission_digest": previous.digest(),
                    "to_permission_digest": target.digest(),
                    "permission_generation": generation,
                    "issued_at_ms": now,
                    "expires_at_ms": now + 60000,
                },
            )

    def historical_write(self, intent):
        """Prueba histórica exacta, sin apply/conciliación de órdenes ni cierre UNKNOWN."""
        Intent.parse(asdict(intent))
        _require(self._identity() == self.identity, "LEDGER_REPLACED")
        with Session(self.engine) as session:
            session.exec(text("BEGIN"))
            meta = session.get(_Meta, 1)
            self._check_meta(meta, self.clock())
            self._check_rows(session, meta)
            applied = session.get(_Applied, intent.command_id)
            if applied is None:
                return None  # Ausencia de prueba; jamás NOT_APPLIED.
            bindings, _ = self._history(session, meta)
            original = bindings.get(applied.binding_generation)
            digest = hashlib.sha256(_canonical(asdict(intent)).encode()).hexdigest()
            _require(
                original is not None
                and applied.binding_digest == original.digest()
                and all(getattr(intent, k) == getattr(original, k) for k in BINDING_FIELDS)
                and intent.scope in original.scopes
                and applied.intent_digest == digest
                and applied.scope == intent.scope
                and applied.revision == intent.expected_revision + 1
                and applied.state == ("enabled" if intent.enabled else "disabled"),
                "HISTORICAL_PERMISSION_MISMATCH",
            )
            return HistoricalPermissionWrite(
                intent.command_id,
                digest,
                applied.binding_digest,
                applied.binding_generation,
                applied.revision,
                intent.enabled,
                applied.binding_generation < meta.binding_generation,
            )

    def _lock(self, session):
        _require(session.get_bind() is self.engine, "WRONG_RESERVATION_DATABASE")
        _require(self._identity() == self.identity, "LEDGER_REPLACED")
        # El write lock se adquiere ANTES de leer permiso/hechos. No commit aquí:
        # el mismo caller confirma permiso+atribución+Trade o revierte todo.
        session.exec(text("UPDATE motor_permissions_meta SET id=id WHERE id=1"))
        # Session.in_transaction() sólo acredita el estado de SQLAlchemy: con
        # AUTOCOMMIT puede ser True sin lock durable. Comprobar SQLite real.
        _require(
            session.connection().connection.driver_connection.in_transaction is True,
            "RESERVATION_TRANSACTION_REQUIRED",
        )
        now = self.clock()
        meta = session.get(_Meta, 1, populate_existing=True)
        self._check_meta(meta, now)
        session.expire_all()
        self._check_rows(session, meta)
        meta.last_seen_ms = now
        session.add(meta)
        return meta, now

    @contextmanager
    def _transaction(self):
        _require(self._identity() == self.identity, "LEDGER_REPLACED")
        with Session(self.engine) as session:
            session.exec(text("BEGIN IMMEDIATE"))
            meta, now = self._lock(session)
            yield session, meta, now
            session.commit()

    def _value(self, session, scope, now):
        _require(scope in self.binding.scopes, "SCOPE_UNAVAILABLE")
        row = session.get(_Permission, scope, populate_existing=True)
        facts = self.facts(session, scope, now)
        _require(type(facts) is GateFacts, "UNKNOWN_GATE_FACTS")
        # Fallos de lectura del kill-switch impiden también habilitación. Una
        # pausa engaged válida permite deshabilitar entradas, jamás levantarla.
        killed, _ = kill_switch_engaged(session=session)
        after = self._time_after(now)
        facts.validate(after)
        value = Observation(
            environment=self.binding.environment,
            source_sha=self.binding.source_sha,
            instance_id=self.binding.instance_id,
            limits_sha256=self.binding.limits_sha256,
            scope=scope,
            revision=row.revision,
            observed_at_ms=facts.observed_at_ms,
            boot_allowed=scope in self.binding.boot_allowed,
            available=facts.available,
            enabled=row.state == "enabled",
            gates_clear=facts.gates_clear
            and not killed
            and (scope in EXITS or not self._stop_status(session.get(_Meta, 1)).latched),
            open_positions=facts.open_positions,
            resting_orders=facts.resting_orders,
            uncertain_orders=facts.uncertain_orders,
            dependents_active=facts.dependents_active,
            applied_command_id=row.command_id,
        )
        return value, after

    @staticmethod
    def _policy(value, enabled):
        _require(value.available, "ADAPTER_UNAVAILABLE")
        if enabled:
            _require(
                value.boot_allowed and value.gates_clear and value.uncertain_orders == 0,
                "ENABLE_BLOCKED",
            )
        elif value.scope in EXITS:
            _require(
                value.open_positions == value.resting_orders == value.uncertain_orders == 0,
                "EXIT_PROTECTION_REQUIRED",
            )
        # Pausar M5 cierra admisión. El canal sólo emite APPLIED cuando su lectura
        # posterior acredita retirada completa; esta función NO cancela quotes.

    def _time_after(self, now, value=None):
        after = self.clock()
        _require(_integer(after) and after >= now, "CLOCK_REGRESSION")
        if value is not None:
            _require(0 <= after - value.observed_at_ms <= MAX_AGE_MS, "STALE_GATE_FACTS")
        return after

    def observe(self, scope):
        with self._transaction() as (session, meta, now):
            value, checked_at = self._value(session, scope, now)
            meta.last_seen_ms = self._time_after(checked_at, value)
            session.add(meta)
            return value

    def _authorize(self, intent, now):
        Intent.parse(asdict(intent))
        for key in (
            "actor_id",
            "chat_id",
            "environment",
            "source_sha",
            "instance_id",
            "limits_sha256",
        ):
            _require(
                getattr(intent, key) == getattr(self.binding, key), "PERMISSION_BINDING_CHANGED"
            )
        _require(intent.issued_at_ms <= now < intent.expires_at_ms, "EXPIRED_OR_FUTURE")

    def apply_if_current(self, intent: Intent):
        with self._transaction() as (session, meta, now):
            self._authorize(intent, now)
            _require(session.get(_Applied, intent.command_id) is None, "COMMAND_ALREADY_USED")
            _require(meta.command_count < self.binding.max_commands, "PERMISSION_COMMAND_CAPACITY")
            value, checked_at = self._value(session, intent.scope, now)
            end = self._time_after(checked_at, value)
            self._authorize(intent, end)
            _require(value.revision == intent.expected_revision, "REVISION_CHANGED")
            self._policy(value, intent.enabled)
            row = session.get(_Permission, intent.scope)
            row.state = "enabled" if intent.enabled else "disabled"
            row.revision += 1
            row.command_id = intent.command_id
            row.transition_generation = None
            meta.command_count += 1
            meta.last_seen_ms = end
            session.add(meta)
            session.add(row)
            session.add(
                _Applied(
                    command_id=intent.command_id,
                    intent_digest=hashlib.sha256(_canonical(asdict(intent)).encode()).hexdigest(),
                    binding_digest=self.digest,
                    binding_generation=meta.binding_generation,
                    scope=intent.scope,
                    revision=row.revision,
                    state=row.state,
                )
            )

    def admit(self, session: Session, scope: str, trades: list[Trade]):
        """Añade Trade+atribución SIN commit, tras los controles de riesgo/inventario.

        Invocación interna del executor dentro de su transacción de reserva. No
        reemplaza RiskManager, límites, atribución de inventario, Capa A ni Capa C.
        Tras commit las órdenes ya admitidas conservan su política en vuelo.
        """
        meta, now = self._lock(session)
        _require(scope in self.binding.scopes, "SCOPE_UNAVAILABLE")
        _require(scope in EXITS or not self._stop_status(meta).latched, "NEW_ENTRIES_BLOCKED")
        value, checked_at = self._value(session, scope, now)
        self._policy(value, True)
        _require(value.enabled and value.applied_command_id is not None, "PERMISSION_DISABLED")
        _require(bool(trades) and len(trades) <= 32, "INVALID_ADMISSION_GROUP")
        _require(
            meta.admission_count + len(trades) <= self.binding.max_admissions, "ADMISSION_CAPACITY"
        )
        strategy, actions = STRATEGIES[scope]
        coids = [t.client_order_id for t in trades]
        _require(len(set(coids)) == len(coids), "DUPLICATE_ADMISSION")
        for trade in trades:
            _require(
                trade.strategy == strategy
                and trade.action in actions
                and trade.status == "pending",
                "WRONG_ADMISSION_SCOPE",
            )
            _require(session.get(Admission, trade.client_order_id) is None, "DUPLICATE_ADMISSION")
            _require(
                session.exec(
                    select(Trade.id).where(Trade.client_order_id == trade.client_order_id)
                ).first()
                is None,
                "TRADE_ALREADY_EXISTS",
            )
            identity = {
                key: getattr(trade, key)
                for key in (
                    "client_order_id",
                    "ticker",
                    "side",
                    "action",
                    "count",
                    "count_fp",
                    "price_cents",
                    "strategy",
                )
            }
            session.add(
                Admission(
                    client_order_id=trade.client_order_id,
                    scope=scope,
                    permission_revision=value.revision,
                    command_id=value.applied_command_id,
                    source_sha=value.source_sha,
                    instance_id=value.instance_id,
                    limits_sha256=value.limits_sha256,
                    admitted_at_ms=now,
                    intent_digest=hashlib.sha256(_canonical(identity).encode()).hexdigest(),
                )
            )
            session.add(trade)
        meta.admission_count += len(trades)
        meta.last_seen_ms = self._time_after(checked_at, value)
        session.add(meta)
