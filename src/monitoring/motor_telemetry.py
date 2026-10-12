"""Observaciones acotadas por instancia para el contrato runtime-motor-telemetry-v1.

Compatible con el parser del panel US500 22522c3. El registro no decide permisos:
configuración, objeto, tarea y retorno se observan por separado. Sin DB ni red.
Un retorno incluye no-op o errores capturados por el propio motor; no acredita fills.
"""

from __future__ import annotations

import asyncio
import inspect
import re
import threading
import time
import weakref
from dataclasses import dataclass, field
from functools import wraps
from uuid import uuid4

SCHEMA = "runtime-motor-telemetry-v1"
MAX_AGE_MS = 300_000
TASK_MAX_AGE_MS = 30_000
MAX_FUTURE_MS = 5_000
MAX_GENERATION = 2**53 - 1
MOTOR_FLAGS = {
    "m1": ("MOTOR_1_ARBITRAGE_ENABLED", "MOTOR_1_EXECUTION_ENABLED", None),
    "m2": (
        "MOTOR_2_SPORTSBOOK_ENABLED",
        "MOTOR_2_ENTRY_EXECUTION_ENABLED",
        "MOTOR_2_EXECUTION_ENABLED",
    ),
    "m3": ("MOTOR_3_CLV_ENABLED", None, "MOTOR_3_EXECUTION_ENABLED"),
    "m5": (
        "MOTOR_MM_ENABLED",
        "MOTOR_MM_EXECUTION_ENABLED",
        "MOTOR_MM_EXECUTION_ENABLED",
    ),
    "m6": ("MOTOR_6_LINEMOVE_ENABLED", None, None),
    "rest": ("MOTOR_REST_ENABLED", "MOTOR_REST_EXECUTION_ENABLED", None),
    "m8": ("MOTOR_8_OFI_ENABLED", None, None),
    "m9": ("MOTOR_9_SPILLOVER_ENABLED", None, None),
}
# These are wiring facts from the reviewed source, never runtime enabled values.
COMPONENTS = {
    "capture": (None, "loop", ()),
    "m1_loop": ("m1", "loop", ("capture", "orderbook_v2")),
    "m2_poll": ("m2", "loop", ("capture", "odds_live")),
    "m2_exit": ("m2", "loop", ()),
    "m3_loop": ("m3", "loop", ()),
    "m5_loop": ("m5", "loop", ("m2_poll",)),
    "m6_observer": ("m6", "callback", ("m2_poll", "odds_live")),
    "rest_observer": ("rest", "callback", ("capture", "ticker_handler")),
    "m8_observer": ("m8", "callback", ("capture", "orderbook_v2", "delta_handler")),
    "m9_observer": ("m9", "callback", ("capture", "orderbook_v2", "delta_handler")),
}
FLAGS = frozenset(
    {"TRADING_ENABLED"}
    | {name for names in MOTOR_FLAGS.values() for name in names if name is not None}
)


def _time(value):
    return type(value) is int and 946_684_800_000 <= value <= 4_102_444_800_000


def _fresh(value, observed_at_ms, now_ms, ttl=MAX_AGE_MS):
    if observed_at_ms is None:
        return {"value": None, "observed_at_ms": None, "freshness": "UNKNOWN"}
    age = now_ms - observed_at_ms
    if age < 0:
        reason = "CLOCK_REGRESSION"
    elif age > ttl:
        reason = "STALE"
    else:
        reason = "FRESH"
    return {
        "value": value if reason == "FRESH" else None,
        "observed_at_ms": observed_at_ms,
        "freshness": reason,
    }


@dataclass
class _Component:
    generation: int = 0
    last_observation_ms: int | None = None
    observations: dict = field(default_factory=dict)
    dependencies: dict = field(default_factory=dict)


class RuntimeTelemetry:
    """One registry per process. Invalid/out-of-order hooks return False/None.

    Call begin_component for a new component generation. Only pass instantiated=True
    AFTER construction succeeds. All subsequent hooks
    require its generation token, preventing a retired instance from overwriting
    the replacement. A task probe must inspect that exact task, not its parent.
    """

    def __init__(self, *, source_sha, instance_id, started_at_ms):
        if (
            type(source_sha) is not str
            or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
            or type(instance_id) is not str
            or re.fullmatch(r"[0-9a-f]{32}", instance_id) is None
            or not _time(started_at_ms)
        ):
            raise ValueError("INVALID_SOURCE_IDENTITY")
        self._source_sha = source_sha
        self._instance_id = instance_id
        self._started_at_ms = started_at_ms
        self._lock = threading.Lock()
        self._flags = dict.fromkeys(FLAGS)
        self._config_at_ms = None
        self._components = {name: _Component() for name in COMPONENTS}

    def configure(self, flags, *, now_ms):
        """Only explicit booleans survive. Absent/invalid flags remain unknown.

        Caller provides an allowlisted projection, never vars(settings). The
        immutable process configuration need not be refreshed on every GET.
        """
        if type(flags) is not dict or not _time(now_ms):
            return False
        with self._lock:
            if now_ms < self._started_at_ms or (
                self._config_at_ms is not None and now_ms < self._config_at_ms
            ):
                return False
            self._flags = {
                key: flags.get(key) if type(flags.get(key)) is bool else None for key in FLAGS
            }
            self._config_at_ms = now_ms
        return True

    def begin_component(self, name, *, now_ms, instantiated=None):
        if (
            type(name) is not str
            or name not in COMPONENTS
            or not _time(now_ms)
            or not _tri(instantiated)
        ):
            return None
        with self._lock:
            old = self._components[name]
            if (
                now_ms < self._started_at_ms
                or (old.last_observation_ms is not None and now_ms < old.last_observation_ms)
                or old.generation >= MAX_GENERATION
            ):
                return None
            state = _Component(old.generation + 1, now_ms)
            if instantiated is not None:
                state.observations["instantiated"] = (instantiated, now_ms)
            self._components[name] = state
            return state.generation

    def observe(self, name, generation, field_name, value, *, now_ms):
        """Record instantiated/task_running or tick_started/tick_returned.

        tick_returned=True only says the call returned; some engines catch their
        own errors, so it does NOT establish successful evaluation or a fill.
        tick_returned=False means an exception escaped that particular call.
        """
        if (
            type(name) is not str
            or name not in COMPONENTS
            or type(generation) is not int
            or type(field_name) is not str
            or field_name not in {"instantiated", "task_running", "tick_started", "tick_returned"}
            or type(value) is not bool
            or not _time(now_ms)
            or (field_name == "task_running" and COMPONENTS[name][1] != "loop")
            or (field_name == "tick_started" and value is not True)
        ):
            return False
        with self._lock:
            state = self._components[name]
            if not self._valid_generation(state, generation, now_ms):
                return False
            state.observations[field_name] = (value, now_ms)
            state.last_observation_ms = now_ms
        return True

    def dependency(self, name, generation, dependency, available, *, now_ms):
        """Availability is an explicit technical observation, never readiness."""
        if (
            type(name) is not str
            or name not in COMPONENTS
            or type(dependency) is not str
            or dependency not in COMPONENTS[name][2]
            or type(generation) is not int
            or type(available) is not bool
            or not _time(now_ms)
        ):
            return False
        with self._lock:
            state = self._components[name]
            if not self._valid_generation(state, generation, now_ms):
                return False
            state.dependencies[dependency] = (available, now_ms)
            state.last_observation_ms = now_ms
        return True

    @staticmethod
    def _valid_generation(state, generation, now_ms):
        return (
            generation > 0
            and generation == state.generation
            and state.last_observation_ms is not None
            and now_ms >= state.last_observation_ms
        )

    def snapshot(self, *, now_ms):
        """Detached JSON primitives. Reading never advances any component hook."""
        if not _time(now_ms) or now_ms < self._started_at_ms:
            raise ValueError("INVALID_SNAPSHOT_TIME")
        with self._lock:
            motors = {}
            for motor, (configured, entry, sell) in MOTOR_FLAGS.items():
                motors[motor] = {
                    "configured": self._flags[configured],
                    "execution_entry_configured": self._flags.get(entry),
                    "execution_sell_configured": self._flags.get(sell),
                    "execution_implementation": (
                        "ABSENT" if motor in {"m6", "m8", "m9"} else "PRESENT"
                    ),
                }
            components = {}
            for name, (motor, task_model, dependencies) in COMPONENTS.items():
                state = self._components[name]
                fields = {}
                for key in (
                    "instantiated",
                    "task_running",
                    "tick_started",
                    "tick_returned",
                ):
                    value, timestamp = state.observations.get(key, (None, None))
                    ttl = TASK_MAX_AGE_MS if key == "task_running" else MAX_AGE_MS
                    fields[key] = _fresh(value, timestamp, now_ms, ttl)
                if task_model == "callback":
                    fields["task_running"]["freshness"] = "NOT_APPLICABLE"
                components[name] = {
                    "motor": motor,
                    "task_model": task_model,
                    "generation": state.generation,
                    **fields,
                    "dependencies": {
                        key: _fresh(*state.dependencies.get(key, (None, None)), now_ms)
                        for key in dependencies
                    },
                }
            return {
                "schema": SCHEMA,
                "source": {
                    "commit_sha": self._source_sha,
                    "instance_id": self._instance_id,
                    "started_at_ms": self._started_at_ms,
                    "sampled_at_ms": now_ms,
                },
                "configuration_at_ms": self._config_at_ms,
                "global_trading_configured": self._flags["TRADING_ENABLED"],
                "motors": motors,
                "components": components,
            }


def best_effort(hook, *args, **kwargs):
    """Runtime integration boundary: telemetry failures must not stop an engine.

    Pass only this registry's hook methods, never the financial call itself.
    Raw exceptions are deliberately neither returned nor logged.
    """
    try:
        return hook(*args, **kwargs)
    except Exception:
        return None


def _tri(value):
    return value is None or type(value) is bool


def _now_ms():
    return time.time_ns() // 1_000_000


class ComponentBinding:
    """Token de una generación; un objeto retirado nunca actualiza su reemplazo."""

    def __init__(self, owner, name, generation, component, task):
        self.owner = owner
        self.name = name
        self.generation = generation
        self.component = weakref.ref(component)
        self.task = weakref.ref(task) if task is not None else None

    def mark(self, field_name, value):
        best_effort(
            self.owner.registry.observe,
            self.name,
            self.generation,
            field_name,
            value,
            now_ms=_now_ms(),
        )

    def probe(self):
        # El sondeo comprueba referencias concretas; no deriva actividad de flags.
        self.mark("instantiated", self.component() is not None)
        if self.task is not None:
            task = self.task()
            self.mark("task_running", task is not None and not task.done())


class MotorTelemetry:
    """Registro por runner con como máximo diez bindings de referencias débiles."""

    def __init__(self, settings):
        self.registry = RuntimeTelemetry(
            source_sha=settings.SOURCE_COMMIT,
            instance_id=uuid4().hex,
            started_at_ms=_now_ms(),
        )
        self.registry.configure(
            {key: getattr(settings, key, None) for key in FLAGS},
            now_ms=_now_ms(),
        )
        self.bindings = {}

    def bind(self, name, component):
        if name not in COMPONENTS or component is None:
            return
        task = asyncio.current_task() if COMPONENTS[name][1] == "loop" else None
        generation = self.registry.begin_component(name, now_ms=_now_ms(), instantiated=True)
        if generation is None:
            return
        binding = ComponentBinding(self, name, generation, component, task)
        component._runtime_telemetry_binding = binding
        self.bindings[name] = binding
        binding.probe()

    def snapshot(self):
        # GET puede sondear la tarea exacta; nunca refresca tick_started/returned.
        for binding in tuple(self.bindings.values()):
            best_effort(binding.probe)
        return self.registry.snapshot(now_ms=_now_ms())


def create_telemetry(settings):
    # Sin SHA completo validable no se inventa identidad. El arranque continúa;
    # /status publica null y el panel mantiene NOT_INSTALLED/desconocido.
    return best_effort(MotorTelemetry, settings)


def bind_component(owner, name, component):
    if isinstance(owner, MotorTelemetry):
        best_effort(owner.bind, name, component)


def _binding(component):
    try:
        binding = getattr(component, "_runtime_telemetry_binding", None)
        return binding if isinstance(binding, ComponentBinding) else None
    except Exception:
        return None


def bind_child(parent, name, component):
    binding = _binding(parent)
    if binding is not None:
        bind_component(binding.owner, name, component)


def telemetry_snapshot(owner):
    return best_effort(owner.snapshot) if isinstance(owner, MotorTelemetry) else None


def observed_tick(method):
    """Envuelve sólo el método llamado: no lo repite ni modifica sus errores.

    La cancelación también se propaga; los fallos de instrumentación se descartan
    sin serializar payloads, excepciones, credenciales ni argumentos del motor.
    """

    def mark(component, name, value):
        binding = _binding(component)
        if binding is not None:
            best_effort(binding.mark, name, value)

    if inspect.iscoroutinefunction(method):

        @wraps(method)
        async def asynchronous(self, *args, **kwargs):
            mark(self, "tick_started", True)
            try:
                result = await method(self, *args, **kwargs)
            except BaseException:
                mark(self, "tick_returned", False)
                raise
            mark(self, "tick_returned", True)
            return result

        return asynchronous

    @wraps(method)
    def synchronous(self, *args, **kwargs):
        mark(self, "tick_started", True)
        try:
            result = method(self, *args, **kwargs)
        except BaseException:
            mark(self, "tick_returned", False)
            raise
        mark(self, "tick_returned", True)
        return result

    return synchronous
