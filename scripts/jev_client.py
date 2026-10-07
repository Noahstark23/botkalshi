"""Cliente Jev para experimentos aislados: no importa config, órdenes ni DB del bot.

Uso desde un harness de simulación, pasando la clave explícitamente:
    with JevClient(api_key, max_calls=30) as client:
        result = client.evaluate(state, instructions)

Contrato HTTP verificado: https://docs.typesafe.ai/api (2026-10-06).
El resultado expresa apoyo a la pregunta, no rentabilidad ni autorización de trading.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass
from types import TracebackType

import httpx

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-1.13.0"
MAX_REQUEST_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_ELAPSED_SECONDS = 30.0


class JevError(Exception):
    """Error público saneado: nunca incorpora claves, cuerpos ni texto del transporte."""


@dataclass(frozen=True, slots=True)
class JevResult:
    probability_yes: float
    model: str
    input_tokens: int
    output_tokens: int
    request_sha256: str
    latency_ms: float


def _elapsed_seconds(started: float) -> float:
    """Presupuesto entre lecturas; una lectura bloqueante conserva su timeout de 10 s."""
    elapsed = time.monotonic() - started
    if elapsed >= MAX_ELAPSED_SECONDS:
        raise JevError("Jev: presupuesto de tiempo agotado; sin reintento")
    return elapsed


def _json_value_valid(value: object, depth: int = 0) -> bool:
    """Evita coerciones silenciosas de claves/tipos y estructuras patológicas."""
    if depth > 32:
        return False
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_value_valid(item, depth + 1) for item in value)
    if type(value) is dict:
        return all(
            type(key) is str and _json_value_valid(item, depth + 1) for key, item in value.items()
        )
    return False


def _request_bytes(state: dict, instructions: str) -> bytes:
    if type(state) is not dict or not _json_value_valid(state):
        raise JevError("Jev: state debe ser un objeto JSON finito")
    if type(instructions) is not str or not instructions.strip():
        raise JevError("Jev: instrucciones no válidas")
    try:
        body = json.dumps(
            {
                "model": JEV_MODEL,
                "state": state,
                "questions": {"supports_trade": {"type": "noul", "instructions": instructions}},
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise JevError("Jev: solicitud JSON no válida") from None
    if len(body) > MAX_REQUEST_BYTES:
        raise JevError("Jev: solicitud supera 16 KiB")
    return body


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON constant")


def _parse_result(body: bytes, request_sha256: str, latency_ms: float) -> JevResult:
    try:
        data = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if type(data) is not dict or data.get("model") != JEV_MODEL:
            raise ValueError("model")
        answers = data["answers"]
        if type(answers) is not dict or set(answers) != {"supports_trade"}:
            raise ValueError("answers")
        answer = answers["supports_trade"]
        if type(answer) is not dict or answer.get("type") != "noul":
            raise ValueError("answer type")
        probability = answer["noul"]
        if type(probability) not in (int, float) or not 0 <= probability <= 1:
            raise ValueError("probability")
        usage = data["usage"]
        if type(usage) is not dict:
            raise ValueError("usage")
        counts = [usage["input_tokens"], usage["output_tokens"]]
        if any(type(count) is not int or count < 0 for count in counts):
            raise ValueError("token count")
        return JevResult(
            probability_yes=float(probability),
            model=JEV_MODEL,
            input_tokens=counts[0],
            output_tokens=counts[1],
            request_sha256=request_sha256,
            latency_ms=latency_ms,
        )
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OverflowError):
        raise JevError("Jev: respuesta incompatible con el contrato fijado") from None


class JevClient:
    """Una llamada HTTP por evaluación; fallos también consumen el presupuesto local."""

    def __init__(
        self,
        api_key: str,
        max_calls: int = 30,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if (
            type(api_key) is not str
            or not api_key
            or not api_key.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in api_key)
        ):
            raise JevError("Jev: clave explícita no válida")
        if type(max_calls) is not int or max_calls < 1:
            raise JevError("Jev: max_calls debe ser un entero positivo")
        self._max_calls = max_calls
        self._calls = 0
        self._closed = False
        self._lock = threading.Lock()
        self._client = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                # No aceptar compresión evita expansión no acotada al limitar el cuerpo.
                "Accept-Encoding": "identity",
            },
            timeout=10.0,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def evaluate(self, state: dict, instructions: str) -> JevResult:
        body = _request_bytes(state, instructions)
        digest = hashlib.sha256(body).hexdigest()
        # Serializa presupuesto y close para que dos callers no superen max_calls.
        with self._lock:
            if self._closed:
                raise JevError("Jev: cliente cerrado")
            if self._calls >= self._max_calls:
                raise JevError("Jev: presupuesto de llamadas agotado")
            self._calls += 1
            started = time.monotonic()
            try:
                with self._client.stream("POST", JEV_ENDPOINT, content=body) as response:
                    _elapsed_seconds(started)
                    if response.status_code != 200:
                        raise JevError(f"Jev: HTTP {response.status_code}; sin reintento")
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding != "identity":
                        raise JevError("Jev: codificación de respuesta no admitida")
                    content = bytearray()
                    # Sin acumular 4096 bytes: observar cada chunk permite cortar un
                    # goteo que no excede el timeout de inactividad de HTTPX.
                    for chunk in response.iter_bytes():
                        _elapsed_seconds(started)
                        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise JevError("Jev: respuesta supera 64 KiB")
                        content.extend(chunk)
            except JevError:
                raise
            except httpx.TimeoutException:
                raise JevError("Jev: tiempo de espera agotado; sin reintento") from None
            except Exception:
                raise JevError("Jev: error de transporte; sin reintento") from None
            # Incluye fin de lectura y cierre. No es un deadline absoluto de 30 s:
            # una lectura en curso puede excederlo hasta su timeout por fase.
            latency_ms = _elapsed_seconds(started) * 1000
            return _parse_result(bytes(content), digest, latency_ms)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self._client.close()
                except Exception:
                    raise JevError("Jev: no se pudo cerrar el transporte") from None

    def __enter__(self) -> JevClient:
        if self._closed:
            raise JevError("Jev: cliente cerrado")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
