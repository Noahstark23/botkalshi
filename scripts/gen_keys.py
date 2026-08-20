"""
Genera el par de claves RSA para la API de Kalshi — en secrets/, JAMÁS en config/.

POR QUÉ ESTE SCRIPT ESCRIBE DONDE ESCRIBE (incidente 2026-08-19): la versión original
escribía en config/, y el commit 0706226 (may-2026) borró dos líneas del .gitignore para
"facilitar el deploy" embebiendo la clave privada en la imagen Docker — con el repo
PÚBLICO. Resultado: la clave de la cuenta de dinero real estuvo 102 días expuesta en
GitHub y hubo que revocarla. secrets/ está en .gitignore desde el día uno y la clave se
monta al container por Coolify (Storages → file mount), nunca via COPY.

USO (local, una vez por rotación):
    python3 scripts/gen_keys.py
    # → secrets/kalshi_private_key.pem (0600) + secrets/kalshi_public_key.pem
    # Subir la PÚBLICA a Kalshi (web → API keys), montar la PRIVADA en Coolify.
"""

import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PRIVATE_PATH = "secrets/kalshi_private_key.pem"
PUBLIC_PATH = "secrets/kalshi_public_key.pem"


def generate_keys() -> None:
    os.makedirs("secrets", exist_ok=True)

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # 0600 desde el nacimiento: se crea el fd con permisos correctos, no se corrige después.
    fd = os.open(PRIVATE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    with open(PUBLIC_PATH, "wb") as f:
        f.write(
            private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

    print(f"Llaves generadas: {PRIVATE_PATH} (0600) + {PUBLIC_PATH}")
    print("La PRIVADA no sale de esta máquina salvo el mount de Coolify. La PÚBLICA va a Kalshi.")


if __name__ == "__main__":
    generate_keys()
