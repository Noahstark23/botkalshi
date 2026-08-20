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

_MARCADOR_PEM = "-----BEGIN"


def normalizar_pem(raw: str) -> bytes:
    """Material PEM utilizable a partir de lo que el panel de env haya guardado.

    POR QUÉ EXISTE (incidente 2026-08-20, bot en crash-loop): tras sacar la clave de la
    imagen (#248) la única vía de entrega en una app compose de Coolify es una env var —
    los file mounts del panel los ignoran las apps compose, y el volumen que la UI dice
    montar no existe en el host. Pero un PEM es MULTILÍNEA y cada panel lo guarda a su
    manera. Las tres formas que aparecen en la práctica se aceptan todas, porque un
    segundo crash-loop por un `\\n` literal cuesta horas de bot caído:

      1. saltos de línea REALES          → se usa tal cual
      2. `\\n` LITERAL (dos caracteres)   → se convierte (el caso más común de un panel web)
      3. el PEM entero en base64         → se decodifica

    La detección es inequívoca: el marcador `-----BEGIN` está presente o no, y si no está
    se prueba base64 exigiendo que el resultado SÍ lo tenga. Nunca se adivina.

    ⚠️ Los errores JAMÁS incluyen el material: un mensaje de error termina en los logs.
    """
    texto = raw.strip()
    if not texto:
        raise ValueError("El material PEM está vacío.")

    # Caso 2: el panel escapó los saltos. Se hace ANTES de decidir sobre base64.
    if _MARCADOR_PEM in texto and "\\n" in texto:
        texto = texto.replace("\\r\\n", "\n").replace("\\n", "\n")

    # Caso 3: sin cabecera visible → único candidato razonable es base64 del PEM entero.
    if _MARCADOR_PEM not in texto:
        compacto = "".join(texto.split())
        try:
            decodificado = base64.b64decode(compacto, validate=True).decode("utf-8")
        except Exception as e:
            raise ValueError(
                "El material PEM no tiene cabecera '-----BEGIN' y tampoco es base64 "
                "válido. Pegá el contenido del .pem completo (con sus saltos de línea) "
                "o su base64."
            ) from e
        if _MARCADOR_PEM not in decodificado:
            raise ValueError(
                "El base64 decodificó, pero el resultado no es un PEM "
                "(falta la cabecera '-----BEGIN')."
            )
        texto = decodificado

    # OpenSSL/cryptography toleran la falta del salto final, pero no todos los parsers.
    if not texto.endswith("\n"):
        texto += "\n"
    return texto.encode("utf-8")


def cargar_clave_pem(raw: str | bytes, *, origen: str) -> rsa.RSAPrivateKey:
    """Normaliza, parsea y VALIDA material PEM → la clave RSA lista para firmar.

    Única puerta de entrada de una clave privada al proceso: la usan el signer y el
    validador de Settings, así el boot rechaza exactamente lo mismo que rechazaría el
    primer request (RSA, ≥2048 bits) en vez de una aproximación — un PEM que pasa el
    boot y explota en el primer request es el peor de los dos mundos.

    `origen` describe DE DÓNDE vino ("env" o la ruta) solo para el mensaje de error;
    el material JAMÁS se incluye: un mensaje de error termina en los logs.
    """
    crudo = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    pem = normalizar_pem(crudo)
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except Exception as e:
        raise ValueError(
            f"No se pudo cargar la key desde {origen}. "
            f"Verifica formato PEM y que no esté encriptada. Error: {e}"
        ) from e

    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(f"Key desde {origen} no es RSA (tipo: {type(key).__name__})")

    if key.key_size < 2048:
        raise ValueError(f"Key size {key.key_size} muy pequeño. Mínimo 2048 (recomendado 4096)")

    return key


class KalshiSigner:
    """
    Firma requests para Kalshi API.

    Thread-safe: la private key se carga una vez en __init__.

    La clave puede venir por CONTENIDO (`private_key_pem`, que gana) o por ARCHIVO
    (`private_key_path`). El contenido tiene prioridad porque es la vía de entrega en
    Coolify compose, donde no hay file mounts (incidente 2026-08-20).
    """

    def __init__(
        self,
        private_key_path: Path | None = None,
        api_key_id: str = "",
        *,
        private_key_pem: str | bytes | None = None,
    ) -> None:
        if not api_key_id:
            raise ValueError("api_key_id no puede estar vacío")
        self.api_key_id = api_key_id
        if private_key_pem is not None:
            self._private_key = cargar_clave_pem(private_key_pem, origen="env")
        elif private_key_path is not None:
            self._private_key = self._load_private_key(private_key_path)
        else:
            raise ValueError(
                "Falta la clave privada: pasá private_key_pem (contenido) o "
                "private_key_path (archivo)."
            )

    @staticmethod
    def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
        """Cargar y validar private key desde archivo PEM."""
        if not path.exists():
            raise FileNotFoundError(f"Private key no encontrada: {path}")

        with open(path, "rb") as f:
            return cargar_clave_pem(f.read(), origen=str(path))

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
