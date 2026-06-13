"""Tests del signer RSA-PSS."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.auth.signer import KalshiSigner


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
