"""Firmas verificables RSA-PSS y Ed25519 con material temporal, sin API."""

from __future__ import annotations

import base64
import traceback
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa, x25519

from src.auth.signer import KalshiSigner, cargar_clave_pem


@pytest.fixture
def temp_key(tmp_path: Path) -> Path:
    """Genera key RSA temporal para tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "test.pem"
    path.write_bytes(pem)
    return path


def test_signer_generates_three_required_headers(temp_key: Path):
    signer = KalshiSigner(temp_key, "test-key-id-12345")
    headers = signer.sign("GET", "/trade-api/v2/portfolio/balance")

    assert "KALSHI-ACCESS-KEY" in headers
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id-12345"
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()


def test_signature_is_cryptographically_valid(temp_key: Path):
    """Verifica que la firma puede verificarse con la public key."""
    signer = KalshiSigner(temp_key, "test-key-id-12345")
    headers = signer.sign("POST", "/trade-api/v2/portfolio/orders")

    # Cargar public key
    with open(temp_key, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    public_key = private_key.public_key()

    # Reconstruir y verificar
    message = (f"{headers['KALSHI-ACCESS-TIMESTAMP']}POST/trade-api/v2/portfolio/orders").encode()
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])

    public_key.verify(
        signature,
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_signer_rejects_non_rsa_key(tmp_path: Path):
    fake = tmp_path / "fake.pem"
    fake.write_text("not a real key")
    with pytest.raises(ValueError):
        KalshiSigner(fake, "test-key-id-12345")


def test_signer_rejects_missing_file(tmp_path: Path):
    nonexistent = tmp_path / "nope.pem"
    with pytest.raises(FileNotFoundError):
        KalshiSigner(nonexistent, "test-key-id-12345")


def test_signer_rejects_empty_api_key(temp_key: Path):
    with pytest.raises(ValueError):
        KalshiSigner(temp_key, "")


def test_signer_rejects_small_key(tmp_path: Path):
    """Keys < 2048 bits deben rechazarse."""
    weak_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    pem = weak_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path = tmp_path / "weak.pem"
    path.write_bytes(pem)

    with pytest.raises(ValueError, match="Key size"):
        KalshiSigner(path, "test-key-id-12345")


def test_sign_requires_method_and_path(temp_key: Path):
    signer = KalshiSigner(temp_key, "test-key-id-12345")
    with pytest.raises(ValueError):
        signer.sign("", "/path")
    with pytest.raises(ValueError):
        signer.sign("GET", "")


def test_signature_changes_per_call(temp_key: Path):
    """Cada call genera firma diferente (timestamp cambia)."""
    import time

    signer = KalshiSigner(temp_key, "test-key-id-12345")
    sig1 = signer.sign("GET", "/foo")
    time.sleep(0.002)  # garantizar timestamp diferente
    sig2 = signer.sign("GET", "/foo")

    assert sig1["KALSHI-ACCESS-TIMESTAMP"] != sig2["KALSHI-ACCESS-TIMESTAMP"]
    assert sig1["KALSHI-ACCESS-SIGNATURE"] != sig2["KALSHI-ACCESS-SIGNATURE"]


@pytest.fixture(params=["rsa", "ed25519"])
def supported_key(request):
    if request.param == "ed25519":
        return ed25519.Ed25519PrivateKey.generate()
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem(key, *, encryption=None):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        encryption or serialization.NoEncryption(),
    )


def _verify(key, signature, message):
    if isinstance(key, ed25519.Ed25519PrivateKey):
        key.public_key().verify(signature, message)
    else:
        key.public_key().verify(
            signature,
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )


@pytest.mark.parametrize("source", ["file", "pem", "escaped", "base64"])
def test_both_algorithms_sign_exact_timestamp_method_and_path(
    supported_key, source, tmp_path, monkeypatch
):
    pem = _pem(supported_key)
    if source == "file":
        path = tmp_path / "temporary.pem"
        path.write_bytes(pem)
        signer = KalshiSigner(path, "offline-key-id")
    else:
        raw = {
            "pem": pem,
            "escaped": pem.decode().replace("\n", "\\n"),
            "base64": base64.b64encode(pem).decode(),
        }[source]
        signer = KalshiSigner(api_key_id="offline-key-id", private_key_pem=raw)
    monkeypatch.setattr("src.auth.signer.time.time", lambda: 1_700_000_000.125)
    headers = signer.sign("get", "/trade-api/v2/portfolio/orders?limit=5")
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000125"
    assert headers["KALSHI-ACCESS-KEY"] == "offline-key-id"
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"], validate=True)
    message = b"1700000000125GET/trade-api/v2/portfolio/orders"
    _verify(supported_key, signature, message)
    for wrong in (message + b"?limit=5", message.replace(b"GET", b"POST"), b"0" + message):
        with pytest.raises(InvalidSignature):
            _verify(supported_key, signature, wrong)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ec.generate_private_key(ec.SECP256R1()),
        ed448.Ed448PrivateKey.generate,
        x25519.X25519PrivateKey.generate,
    ],
)
def test_other_key_algorithms_are_rejected(factory):
    with pytest.raises(ValueError, match="no es RSA ni Ed25519"):
        cargar_clave_pem(_pem(factory()), origen="test temporal")


def test_encrypted_private_keys_are_rejected(supported_key):
    with pytest.raises(ValueError, match="no esté encriptada"):
        cargar_clave_pem(
            _pem(supported_key, encryption=serialization.BestAvailableEncryption(b"test-only")),
            origen="test temporal",
        )


def test_public_key_is_not_a_signing_private_key(supported_key):
    public = supported_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    with pytest.raises(ValueError, match="No se pudo cargar"):
        cargar_clave_pem(public, origen="test temporal")


def test_parser_error_never_echoes_crypto_exception_payload(monkeypatch, supported_key):
    def invalid(*args, **kwargs):
        raise ValueError("sensitive-fixture-never-log")

    monkeypatch.setattr("src.auth.signer.serialization.load_pem_private_key", invalid)
    with pytest.raises(ValueError) as exc:
        cargar_clave_pem(_pem(supported_key), origen="test temporal")
    assert "sensitive-fixture-never-log" not in str(exc.value)
    assert "sensitive-fixture-never-log" not in "".join(traceback.format_exception(exc.value))


def test_non_utf8_pem_has_sanitized_validation_error():
    with pytest.raises(ValueError, match="UTF-8"):
        cargar_clave_pem(b"\xff", origen="test temporal")
