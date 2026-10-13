"""Parada monotónica de nuevas admisiones; nunca autoriza órdenes ni reanuda.

El transporte entrega un sobre autenticado al proceso que posee el ledger. Este
módulo no abre red, no consulta Telegram y no aprovisiona tablas o secretos.
"""

import hashlib
from dataclasses import asdict, dataclass

from src.control.wire import (
    Authenticator,
    ControlError,
    Intent,
    _canonical,
    _hex,
    _integer,
    _require,
)

BINDING_FIELDS = (
    "actor_id",
    "chat_id",
    "environment",
    "source_sha",
    "instance_id",
    "limits_sha256",
)


@dataclass(frozen=True)
class StopRequest:
    request_id: str
    actor_id: int
    chat_id: int
    environment: str
    source_sha: str
    instance_id: str
    limits_sha256: str
    issued_at_ms: int
    expires_at_ms: int
    operation: str = "stop"

    @classmethod
    def parse(cls, value):
        _require(
            type(value) is dict and set(value) == set(cls.__dataclass_fields__),
            "INVALID_STOP_REQUEST",
        )
        _require(value["operation"] in ("stop", "status"), "INVALID_STOP_OPERATION")
        # Comparte la validación estricta de identidad/TTL, no la política CAS.
        Intent.parse(
            {
                **{key: value[key] for key in BINDING_FIELDS},
                "command_id": value["request_id"],
                "scope": "m1.entries",
                "enabled": False,
                "expected_revision": 0,
                "issued_at_ms": value["issued_at_ms"],
                "expires_at_ms": value["expires_at_ms"],
            }
        )
        return cls(**value)

    def authorize(self, binding, now):
        self.parse(asdict(self))
        _require(_integer(now, 946684800000), "INVALID_TIME")
        for key in BINDING_FIELDS:
            _require(getattr(self, key) == getattr(binding, key), "PERMISSION_BINDING_CHANGED")
        _require(self.issued_at_ms <= now < self.expires_at_ms, "EXPIRED_OR_FUTURE")

    def digest(self):
        return hashlib.sha256(_canonical(asdict(self)).encode()).hexdigest()


@dataclass(frozen=True)
class EntryStopStatus:
    latched: bool
    first_request_id: str | None
    first_request_digest: str | None
    latched_at_ms: int | None

    def validate(self):
        _require(type(self.latched) is bool, "INVALID_ENTRY_STOP")
        if self.latched:
            _require(
                _hex(self.first_request_id, 32)
                and _hex(self.first_request_digest, 64)
                and _integer(self.latched_at_ms, 946684800000),
                "INVALID_ENTRY_STOP",
            )
        else:
            _require(
                self.first_request_id is self.first_request_digest is self.latched_at_ms is None,
                "INVALID_ENTRY_STOP",
            )
        return self


class EntryStopChannel:
    """Vía independiente del journal de permisos. Respuesta sólo tras commit.

    Un error de persistencia/lectura nunca produce un recibo de parada confirmada.
    El cliente debe tratar falta de recibo como UNKNOWN y consultar con un pedido
    nuevo de estado; no deducir resultado del envío ni reenviar una orden.
    """

    def __init__(self, adapter, auth: Authenticator):
        self.adapter, self.auth = adapter, auth

    def handle(self, wire: bytes) -> bytes:
        request = StopRequest.parse(self.auth.open("entry-stop-request-v1", wire))
        try:
            status = self.adapter.entry_stop(request)
        except Exception:
            # No filtrar SQL, rutas, texto arbitrario o errores de proveedores.
            raise ControlError("ENTRY_STOP_UNCONFIRMED") from None
        status.validate()
        return self.auth.seal(
            "entry-stop-receipt-v1",
            {
                "request_digest": request.digest(),
                "status": asdict(status),
            },
        )


def verify_receipt(auth: Authenticator, request: StopRequest, wire: bytes, *, now_ms: int):
    """El recibo queda ligado al pedido fresco completo, incluida su identidad."""
    request.authorize(request, now_ms)
    value = auth.open("entry-stop-receipt-v1", wire)
    _require(
        set(value) == {"request_digest", "status"} and value["request_digest"] == request.digest(),
        "WRONG_ENTRY_STOP_RECEIPT",
    )
    raw = value["status"]
    _require(
        type(raw) is dict and set(raw) == set(EntryStopStatus.__dataclass_fields__),
        "INVALID_ENTRY_STOP",
    )
    status = EntryStopStatus(**raw).validate()
    _require(status.latched_at_ms is None or status.latched_at_ms <= now_ms, "FUTURE_ENTRY_STOP")
    if request.operation == "stop":
        _require(status.latched, "ENTRY_STOP_UNCONFIRMED")
    return status
