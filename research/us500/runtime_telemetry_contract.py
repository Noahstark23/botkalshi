"""Bounded in-memory observability contract; not installed in the financial runtime.

Only explicit hooks provide facts. Configuration never grants permission, task
liveness never proves progress, and a missing callback never means disabled.
This module has no network, database, environment, logging, or trading imports.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field

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
        self._flags = {name: None for name in FLAGS}
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
                key: flags.get(key) if type(flags.get(key)) is bool else None
                for key in FLAGS
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
                or (
                    old.last_observation_ms is not None
                    and now_ms < old.last_observation_ms
                )
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
            or field_name
            not in {"instantiated", "task_running", "tick_started", "tick_returned"}
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


def _unknown(reason):
    return {
        "available": False,
        "reason": reason,
        "source": None,
        "configuration_at_ms": None,
        "global_trading_configured": None,
        "motors": {
            motor: {
                "configured": None,
                "execution_entry_configured": None,
                "execution_sell_configured": None,
                "execution_implementation": "UNKNOWN",
            }
            for motor in MOTOR_FLAGS
        },
        "components": {},
    }


def _bounded_tree(value):
    # Reject user-defined objects before encoding; bound width, depth, text, nodes.
    pending = [(value, 0)]
    nodes = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > 2048 or depth > 12:
            return False
        if type(current) is dict:
            if len(current) > 64:
                return False
            if any(type(key) is not str or len(key) > 64 for key in current):
                return False
            pending.extend((item, depth + 1) for item in current.values())
        elif type(current) is str:
            if len(current) > 256:
                return False
        elif type(current) is int:
            if not 0 <= current <= MAX_GENERATION:
                return False
        elif current is not None and type(current) is not bool:
            return False
    return True


def _keys(value, expected):
    return type(value) is dict and set(value) == set(expected)


def _tri(value):
    return value is None or type(value) is bool


def _parse_observation(
    value, *, now_ms, sampled_at_ms, started_at_ms, ttl, callback_task=False
):
    if not _keys(value, ("value", "observed_at_ms", "freshness")) or not _tri(
        value["value"]
    ):
        raise ValueError("INVALID_OBSERVATION")
    observed = value["observed_at_ms"]
    freshness = value["freshness"]
    if callback_task:
        if value != {
            "value": None,
            "observed_at_ms": None,
            "freshness": "NOT_APPLICABLE",
        }:
            raise ValueError("INVALID_CALLBACK_TASK")
        return dict(value)
    if observed is None:
        if value["value"] is not None or freshness != "UNKNOWN":
            raise ValueError("INVALID_UNKNOWN")
        return dict(value)
    if not _time(observed) or not started_at_ms <= observed <= sampled_at_ms:
        raise ValueError("INVALID_OBSERVATION_TIME")
    expected = _fresh(value["value"], observed, sampled_at_ms, ttl)
    if freshness != expected["freshness"]:
        raise ValueError("INVALID_FRESHNESS")
    if freshness == "FRESH" and type(value["value"]) is not bool:
        raise ValueError("INVALID_FRESH_VALUE")
    if freshness != "FRESH" and value["value"] is not None:
        raise ValueError("INVALID_STALE_VALUE")
    return _fresh(value["value"], observed, max(now_ms, sampled_at_ms), ttl)


def parse_runtime_telemetry(value, *, now_ms):
    """Pure strict parser for optional /status['motor_telemetry'].

    Missing, malformed, oversized, or stale telemetry yields unknown fields. It
    must not invalidate the separate legacy /status fields. No previous snapshot
    is accepted, stored, or revived. Returned dictionaries are detached.
    """
    if value is None:
        return _unknown("NOT_INSTALLED")
    if not _time(now_ms) or not _bounded_tree(value):
        return _unknown("INVALID")
    # JSON sizing is safe only after the bounded primitive-only traversal above.
    import json

    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=True)) > 65_536:
        return _unknown("INVALID")
    try:
        if (
            not _keys(
                value,
                (
                    "schema",
                    "source",
                    "configuration_at_ms",
                    "global_trading_configured",
                    "motors",
                    "components",
                ),
            )
            or value["schema"] != SCHEMA
        ):
            raise ValueError("INVALID_SCHEMA")
        source = value["source"]
        if (
            not _keys(
                source, ("commit_sha", "instance_id", "started_at_ms", "sampled_at_ms")
            )
            or type(source["commit_sha"]) is not str
            or re.fullmatch(r"[0-9a-f]{40}", source["commit_sha"]) is None
            or type(source["instance_id"]) is not str
            or re.fullmatch(r"[0-9a-f]{32}", source["instance_id"]) is None
            or not _time(source["started_at_ms"])
            or not _time(source["sampled_at_ms"])
            or source["started_at_ms"] > source["sampled_at_ms"]
        ):
            raise ValueError("INVALID_IDENTITY")
        sampled = source["sampled_at_ms"]
        if now_ms - sampled > TASK_MAX_AGE_MS:
            return _unknown("STALE")
        if sampled - now_ms > MAX_FUTURE_MS:
            raise ValueError("FUTURE_SOURCE")
        config_at = value["configuration_at_ms"]
        if config_at is not None and (
            not _time(config_at) or not source["started_at_ms"] <= config_at <= sampled
        ):
            raise ValueError("INVALID_CONFIG_TIME")
        if not _tri(value["global_trading_configured"]):
            raise ValueError("INVALID_FLAG")
        if not _keys(value["motors"], MOTOR_FLAGS) or not _keys(
            value["components"], COMPONENTS
        ):
            raise ValueError("INVALID_MEMBERS")
        motors = {}
        for motor in MOTOR_FLAGS:
            block = value["motors"][motor]
            boolean_keys = (
                "configured",
                "execution_entry_configured",
                "execution_sell_configured",
            )
            if (
                not _keys(block, (*boolean_keys, "execution_implementation"))
                or any(not _tri(block[key]) for key in boolean_keys)
                or block["execution_implementation"]
                not in {"PRESENT", "ABSENT", "UNKNOWN"}
            ):
                raise ValueError("INVALID_MOTOR")
            if motor in {"m6", "m8", "m9"} and (
                block["execution_implementation"] != "ABSENT"
                or block["execution_entry_configured"] is not None
                or block["execution_sell_configured"] is not None
            ):
                raise ValueError("INVALID_CALLBACK_EXECUTION")
            if (
                MOTOR_FLAGS[motor][1] is None
                and block["execution_entry_configured"] is not None
            ) or (
                MOTOR_FLAGS[motor][2] is None
                and block["execution_sell_configured"] is not None
            ):
                raise ValueError("NONEXISTENT_EXECUTION_FLAG")
            if config_at is None and any(
                block[key] is not None for key in boolean_keys
            ):
                raise ValueError("MISSING_CONFIG_TIME")
            motors[motor] = dict(block)
        if config_at is None and value["global_trading_configured"] is not None:
            raise ValueError("MISSING_GLOBAL_CONFIG_TIME")
        components = {}
        for name, (motor, model, dependencies) in COMPONENTS.items():
            block = value["components"][name]
            fields = ("instantiated", "task_running", "tick_started", "tick_returned")
            if (
                not _keys(
                    block,
                    ("motor", "task_model", "generation", *fields, "dependencies"),
                )
                or block["motor"] != motor
                or block["task_model"] != model
                or type(block["generation"]) is not int
                or not 0 <= block["generation"] <= MAX_GENERATION
                or not _keys(block["dependencies"], dependencies)
            ):
                raise ValueError("INVALID_COMPONENT")
            parsed = {key: block[key] for key in ("motor", "task_model", "generation")}
            for key in fields:
                parsed[key] = _parse_observation(
                    block[key],
                    now_ms=now_ms,
                    sampled_at_ms=sampled,
                    started_at_ms=source["started_at_ms"],
                    ttl=TASK_MAX_AGE_MS if key == "task_running" else MAX_AGE_MS,
                    callback_task=model == "callback" and key == "task_running",
                )
            if parsed["tick_started"]["value"] is False:
                raise ValueError("INVALID_TICK_START")
            parsed["dependencies"] = {
                key: _parse_observation(
                    block["dependencies"][key],
                    now_ms=now_ms,
                    sampled_at_ms=sampled,
                    started_at_ms=source["started_at_ms"],
                    ttl=MAX_AGE_MS,
                )
                for key in dependencies
            }
            if block["generation"] == 0 and any(
                item["observed_at_ms"] is not None
                for item in [
                    *(parsed[key] for key in fields),
                    *parsed["dependencies"].values(),
                ]
            ):
                raise ValueError("MISSING_GENERATION")
            components[name] = parsed
        return {
            "available": True,
            "reason": "FRESH",
            "source": dict(source),
            "configuration_at_ms": config_at,
            "global_trading_configured": value["global_trading_configured"],
            "motors": motors,
            "components": components,
        }
    except (ValueError, KeyError, TypeError):
        return _unknown("INVALID")
