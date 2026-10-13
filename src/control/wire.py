"""Protocolo de control puro stdlib; importable por el panel aislado."""

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field

SCHEMA = "motor-command-v1"
SCOPES = frozenset(
    {
        "m1.entries",
        "m2.entries",
        "m2.exits",
        "m2.fair",
        "m3.exits",
        "m5.quotes",
        "rest.entries",
        "m6.observe",
        "m8.observe",
        "m9.observe",
    }
)
EXITS = frozenset({"m2.exits", "m3.exits"})
MAX_BYTES = 8192
MAX_TTL_MS = 60000
MAX_AGE_MS = 30000


class ControlError(ValueError):
    """Sólo códigos estáticos; nunca payloads, claves o errores del proveedor."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ControlError(code)


def _integer(value, low=0, high=2**53 - 1):
    return type(value) is int and low <= value <= high


def _hex(value, length):
    return type(value) is str and re.fullmatch(f"[0-9a-f]{{{length}}}", value) is not None


def _canonical(value) -> str:
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ControlError("INVALID_JSON") from None
    _require(len(result.encode()) <= MAX_BYTES, "PAYLOAD_TOO_LARGE")
    return result


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "DUPLICATE_FIELD")
        result[key] = value
    return result


@dataclass(frozen=True)
class Authenticator:
    key: bytes = field(repr=False)

    def __post_init__(self):
        _require(type(self.key) is bytes and len(self.key) >= 32, "INVALID_CHANNEL_KEY")

    def mac(self, domain: str, value: dict) -> str:
        return hmac.new(
            self.key, (SCHEMA + ":" + domain + ":" + _canonical(value)).encode(), hashlib.sha256
        ).hexdigest()

    def seal(self, domain: str, value: dict) -> bytes:
        return _canonical({"body": value, "mac": self.mac(domain, value)}).encode()

    def open(self, domain: str, wire: bytes) -> dict:
        _require(type(wire) is bytes and len(wire) <= MAX_BYTES, "INVALID_ENVELOPE")
        try:
            envelope = json.loads(wire, object_pairs_hook=_unique)
        except (ValueError, UnicodeError, RecursionError):
            raise ControlError("INVALID_ENVELOPE") from None
        _require(type(envelope) is dict and set(envelope) == {"body", "mac"}, "INVALID_ENVELOPE")
        body, signature = envelope["body"], envelope["mac"]
        _require(type(body) is dict and _hex(signature, 64), "INVALID_ENVELOPE")
        _require(hmac.compare_digest(signature, self.mac(domain, body)), "UNAUTHENTICATED")
        return body


@dataclass(frozen=True)
class Intent:
    command_id: str
    actor_id: int
    chat_id: int
    environment: str
    source_sha: str
    instance_id: str
    limits_sha256: str
    scope: str
    enabled: bool
    expected_revision: int
    issued_at_ms: int
    expires_at_ms: int

    @classmethod
    def parse(cls, value: dict):
        _require(
            type(value) is dict and set(value) == set(cls.__dataclass_fields__), "INVALID_INTENT"
        )
        _require(
            _hex(value["command_id"], 32)
            and _hex(value["source_sha"], 40)
            and _hex(value["instance_id"], 32)
            and _hex(value["limits_sha256"], 64),
            "INVALID_IDENTITY",
        )
        _require(_integer(value["actor_id"], 1) and _integer(value["chat_id"], 1), "INVALID_OWNER")
        _require(
            value["environment"] in ("demo", "production")
            and type(value["scope"]) is str
            and value["scope"] in SCOPES
            and type(value["enabled"]) is bool,
            "INVALID_ACTION",
        )
        _require(_integer(value["expected_revision"]), "INVALID_REVISION")
        start, end = value["issued_at_ms"], value["expires_at_ms"]
        _require(
            _integer(start, 946684800000)
            and _integer(end, 946684800000)
            and 0 < end - start <= MAX_TTL_MS,
            "INVALID_EXPIRY",
        )
        return cls(**value)
