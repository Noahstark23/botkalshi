"""
Autenticación RSA-PSS para Kalshi API v2.

Cada request requiere 3 headers:
    KALSHI-ACCESS-KEY:       UUID del API key (público)
    KALSHI-ACCESS-TIMESTAMP: timestamp en milisegundos
    KALSHI-ACCESS-SIGNATURE: firma RSA-PSS de "{timestamp}{METHOD}{path}"

Reference: https://trading-api.readme.io/reference/api-authentication-key-management
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiSigner:
    """
    Firma requests para Kalshi API.

    Thread-safe: la private key se carga una vez en __init__.
    """

    def __init__(self, private_key_path: Path, api_key_id: str) -> None:
        if not api_key_id:
            raise ValueError("api_key_id no puede estar vacío")
        self.api_key_id = api_key_id
        self._private_key = self._load_private_key(private_key_path)

    @staticmethod
    def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
        """Cargar y validar private key desde archivo PEM."""
        if not path.exists():
            raise FileNotFoundError(f"Private key no encontrada: {path}")

        with open(path, "rb") as f:
            try:
                key = serialization.load_pem_private_key(
                    f.read(),
                    password=None,
                )
            except Exception as e:
                raise ValueError(
                    f"No se pudo cargar key desde {path}. "
                    f"Verifica formato PEM y que no esté encriptada. Error: {e}"
                ) from e

        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError(f"Key en {path} no es RSA (tipo: {type(key).__name__})")

        if key.key_size < 2048:
            raise ValueError(f"Key size {key.key_size} muy pequeño. Mínimo 2048 (recomendado 4096)")

        return key

    def sign(self, method: str, path: str) -> dict[str, str]:
        """
        Genera headers de autenticación para un request.

        Args:
            method: HTTP method en mayúsculas (GET, POST, DELETE, etc.)
            path: Path completo desde dominio, incluyendo query string si la hay
                  Ejemplo: "/trade-api/v2/portfolio/balance"
                  Ejemplo: "/trade-api/v2/markets?status=open&limit=100"

        Returns:
            Dict con los 3 headers KALSHI-ACCESS-* listos para incluir en request.

        Raises:
            ValueError: si method o path están vacíos.
        """
        if not method:
            raise ValueError("method requerido")
        if not path:
            raise ValueError("path requerido")

        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode()

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }
