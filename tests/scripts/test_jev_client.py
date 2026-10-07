"""Contrato Jev con transporte simulado: ninguna llamada real ni import del bot."""

import ast
import hashlib
import json
import socket
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from scripts.jev_client import (
    JEV_ENDPOINT,
    JEV_MODEL,
    MAX_ELAPSED_SECONDS,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    JevClient,
    JevError,
)

KEY = "fake-key-never-send"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*_args, **_kwargs):
        pytest.fail("Esta prueba no permite conexiones reales")

    monkeypatch.setattr(socket.socket, "connect", blocked)


def valid_response(probability=0.75):
    return {
        "model": JEV_MODEL,
        "answers": {"supports_trade": {"type": "noul", "noul": probability}},
        "usage": {"input_tokens": 42, "output_tokens": 5},
    }


def test_pinned_request_result_hash_and_secure_transport(monkeypatch):
    seen = []
    monkeypatch.setenv("HTTP_PROXY", "http://untrusted.invalid:3128")
    monkeypatch.setenv("TYPESAFE_BASE_URL", "https://untrusted.invalid")

    def handler(request):
        seen.append(request)
        assert str(request.url) == JEV_ENDPOINT
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["accept-encoding"] == "identity"
        assert set(request.extensions["timeout"].values()) == {10.0}
        assert json.loads(request.content) == {
            "model": JEV_MODEL,
            "state": {"signal": 2, "verified": False},
            "questions": {"supports_trade": {"type": "noul", "instructions": "¿Apoya?"}},
        }
        return httpx.Response(200, json=valid_response())

    with JevClient(KEY, transport=httpx.MockTransport(handler)) as client:
        assert not client._client.trust_env
        assert not client._client.follow_redirects
        result = client.evaluate({"verified": False, "signal": 2}, "¿Apoya?")
    assert result.probability_yes == 0.75
    assert result.model == JEV_MODEL
    assert (result.input_tokens, result.output_tokens) == (42, 5)
    assert result.request_sha256 == hashlib.sha256(seen[0].content).hexdigest()
    assert result.latency_ms >= 0
    assert KEY not in repr(client) + repr(result)


@pytest.mark.parametrize("probability", [0, 1, 0.0, 1.0])
def test_probability_boundaries(probability):
    with JevClient(
        KEY,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=valid_response(probability))
        ),
    ) as client:
        assert client.evaluate({}, "Pregunta").probability_yes == probability


@pytest.mark.parametrize("probability", [True, False, "0.8", None, -0.01, 1.01, [], {}])
def test_invalid_probability_fails_closed(probability):
    with (
        JevClient(
            KEY,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json=valid_response(probability))
            ),
        ) as client,
        pytest.raises(JevError, match="contrato"),
    ):
        client.evaluate({}, "Pregunta")


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
@pytest.mark.parametrize("value", [True, False, "5", 5.0, None, -1])
def test_usage_requires_nonnegative_integer(field, value):
    response = valid_response()
    response["usage"][field] = value
    with (
        JevClient(
            KEY, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as client,
        pytest.raises(JevError, match="contrato"),
    ):
        client.evaluate({}, "Pregunta")


@pytest.mark.parametrize(
    "response",
    [
        {},
        [],
        None,
        {**valid_response(), "model": "jev-latest"},
        {**valid_response(), "model": "jev-1.14.0"},
        {**valid_response(), "answers": {}},
        {**valid_response(), "answers": {"supports_trade": {"type": "choice", "noul": 0.5}}},
        {**valid_response(), "answers": {"supports_trade": {"type": "noul"}}},
        {**valid_response(), "usage": {}},
        {**valid_response(), "usage": None},
        {key: value for key, value in valid_response().items() if key != "model"},
        {key: value for key, value in valid_response().items() if key != "answers"},
        {key: value for key, value in valid_response().items() if key != "usage"},
    ],
)
def test_missing_or_incompatible_contract(response):
    with (
        JevClient(
            KEY, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
        ) as client,
        pytest.raises(JevError, match="contrato"),
    ):
        client.evaluate({}, "Pregunta")


@pytest.mark.parametrize("raw", [b"not JSON", b"\xff", b"{", b'{"model":1,"model":2}'])
def test_malformed_json_is_sanitized(raw):
    with (
        JevClient(
            KEY, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw))
        ) as client,
        pytest.raises(JevError, match="contrato"),
    ):
        client.evaluate({}, "Pregunta")


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_nonfinite_probability_is_rejected(value):
    raw = json.dumps(valid_response()).replace("0.75", value)
    with (
        JevClient(
            KEY, transport=httpx.MockTransport(lambda _: httpx.Response(200, content=raw))
        ) as client,
        pytest.raises(JevError, match="contrato"),
    ):
        client.evaluate({}, "Pregunta")


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 422, 429, 500, 529])
def test_http_failures_do_not_retry_redirect_or_echo_response(status, caplog, capsys):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text=KEY, headers={"Location": f"https://evil.invalid/{KEY}"})

    with JevClient(KEY, max_calls=1, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(JevError, match=f"HTTP {status}") as error:
            client.evaluate({"private": "SENSITIVE-STATE"}, "Pregunta")
        with pytest.raises(JevError, match="presupuesto"):
            client.evaluate({}, "Pregunta")
    assert len(requests) == 1
    assert KEY not in str(error.value) + caplog.text + str(capsys.readouterr())
    assert "SENSITIVE-STATE" not in str(error.value) + caplog.text


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError, RuntimeError])
def test_transport_error_is_sanitized_and_consumes_budget(failure):
    seen = []

    def handler(request):
        seen.append(request)
        raise failure(KEY)

    with JevClient(KEY, max_calls=1, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(JevError) as error:
            client.evaluate({}, "Pregunta")
        assert KEY not in str(error.value)
        assert error.value.__suppress_context__
        with pytest.raises(JevError, match="presupuesto"):
            client.evaluate({}, "Pregunta")
    assert len(seen) == 1


@pytest.mark.parametrize(
    "state", [[], "state", None, {1: "key"}, {"x": float("nan")}, {"x": object()}]
)
def test_invalid_state_never_requests(state):
    def handler(_request):
        pytest.fail("Solicitud inválida no debe salir")

    with JevClient(KEY, transport=httpx.MockTransport(handler)) as client, pytest.raises(JevError):
        client.evaluate(state, "Pregunta")


@pytest.mark.parametrize("instructions", [None, True, "", " \n "])
def test_invalid_instructions_never_requests(instructions):
    with (
        JevClient(
            KEY, transport=httpx.MockTransport(lambda _: pytest.fail("No debe salir"))
        ) as client,
        pytest.raises(JevError),
    ):
        client.evaluate({}, instructions)


def test_payload_limit_and_invalid_payload_do_not_spend_call_budget():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=valid_response())

    with JevClient(KEY, max_calls=1, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(JevError, match="16 KiB"):
            client.evaluate({"too_large": "é" * MAX_REQUEST_BYTES}, "Pregunta")
        assert client.evaluate({}, "Pregunta").probability_yes == 0.75
        with pytest.raises(JevError, match="presupuesto"):
            client.evaluate({}, "Pregunta")
    assert len(calls) == 1


def test_response_stream_is_bounded_and_closed():
    class LargeStream(httpx.SyncByteStream):
        closed = False
        yielded = 0

        def __iter__(self):
            for _ in range(1000):
                self.yielded += 4096
                yield b"x" * 4096

        def close(self):
            self.closed = True

    stream = LargeStream()
    with (
        JevClient(
            KEY,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=stream)),
        ) as client,
        pytest.raises(JevError, match="64 KiB"),
    ):
        client.evaluate({}, "Pregunta")
    assert stream.closed
    assert stream.yielded <= MAX_RESPONSE_BYTES + 4096


@pytest.mark.parametrize("delay_stage", ["between_chunks", "after_read"])
def test_elapsed_budget_stops_trickle_and_post_read_without_retry_or_leak(
    monkeypatch, delay_stage, caplog, capsys
):
    now = [100.0]
    monkeypatch.setattr("scripts.jev_client.time.monotonic", lambda: now[0])
    calls = []

    class DelayedStream(httpx.SyncByteStream):
        closed = False
        yielded = 0

        def __iter__(self):
            if delay_stage == "after_read":
                self.yielded += 1
                yield json.dumps(valid_response()).encode()
                now[0] += MAX_ELAPSED_SECONDS + 1
                return
            # Cada lectura tarda menos de 10 s y entrega menos de 4096 bytes.
            for _ in range(1000):
                now[0] += 7
                self.yielded += 1
                yield KEY.encode()

        def close(self):
            self.closed = True

    stream = DelayedStream()

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=stream)

    with JevClient(KEY, max_calls=1, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(JevError, match="presupuesto de tiempo") as error:
            client.evaluate({"private": "SENSITIVE-STATE"}, "Pregunta")
        with pytest.raises(JevError, match="presupuesto de llamadas"):
            client.evaluate({}, "Pregunta")
    assert len(calls) == 1
    assert stream.closed
    assert stream.yielded == (5 if delay_stage == "between_chunks" else 1)
    output = str(error.value) + caplog.text + str(capsys.readouterr())
    assert KEY not in output
    assert "SENSITIVE-STATE" not in output


def test_compressed_response_is_rejected_without_reading_stream():
    class UnreadStream(httpx.SyncByteStream):
        closed = False

        def __iter__(self):
            pytest.fail("No descomprimir respuesta fuera del contrato")
            yield b""  # pragma: no cover - sólo define el protocolo de generador

        def close(self):
            self.closed = True

    stream = UnreadStream()
    with (
        JevClient(
            KEY,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)
            ),
        ) as client,
        pytest.raises(JevError, match="codificación"),
    ):
        client.evaluate({}, "Pregunta")
    assert stream.closed


def test_canonical_digest_is_independent_of_object_key_order_and_zero_usage_is_valid():
    response = valid_response()
    response["usage"] = {"input_tokens": 0, "output_tokens": 0}
    with JevClient(
        KEY, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as client:
        first = client.evaluate({"b": 2, "a": 1}, "Pregunta")
        second = client.evaluate({"a": 1, "b": 2}, "Pregunta")
    assert first.request_sha256 == second.request_sha256
    assert (first.input_tokens, first.output_tokens) == (0, 0)


def test_context_closes_client_even_on_failure():
    client = JevClient(KEY, transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    with pytest.raises(JevError), client:
        client.evaluate({}, "Pregunta")
    assert client._client.is_closed
    client.close()
    with pytest.raises(JevError, match="cerrado"):
        client.evaluate({}, "Pregunta")


@pytest.mark.parametrize("key", [None, "", " ", "x\nsecret", "x\rsecret", "ñ"])
def test_reject_invalid_explicit_key(key):
    with pytest.raises(JevError, match="clave"):
        JevClient(key)


@pytest.mark.parametrize("budget", [True, False, 0, -1, "30", 1.5])
def test_reject_invalid_budget(budget):
    with pytest.raises(JevError, match="max_calls"):
        JevClient(KEY, max_calls=budget)


@pytest.mark.parametrize("script_name", ["jev_client.py", "jev_simulation.py"])
def test_module_has_no_bot_environment_order_or_database_imports(script_name):
    source = Path(__file__).resolve().parents[2] / "scripts" / script_name
    tree = ast.parse(source.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(("src", "dotenv", "sqlmodel"))
        elif isinstance(node, ast.Import):
            assert all(
                not alias.name.startswith(("src", "dotenv", "sqlmodel")) for alias in node.names
            )


def test_close_error_does_not_leak_secret():
    client = JevClient(KEY, transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    with (
        patch.object(client._client, "close", side_effect=RuntimeError(KEY)),
        pytest.raises(JevError) as error,
    ):
        client.close()
    assert KEY not in str(error.value)
