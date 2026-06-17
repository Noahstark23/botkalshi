"""
Diagnóstico standalone de conectividad con Kalshi API.

Ejecutar directamente: python scripts/diagnose_kalshi.py

No importa ningún módulo del bot — signing inline para máximo aislamiento.
Útil para diagnosticar 429s, auth errors, y disponibilidad de endpoints.
"""

from __future__ import annotations

import asyncio
import base64
import os
import time
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv()

BASE_URL = os.getenv(
    "KALSHI_API_BASE_URL",
    "https://api.elections.kalshi.com/trade-api/v2",
)
PAUSE_BETWEEN_REQUESTS = 3.0  # segundos entre requests para no generar 429s


def _load_private_key(path: str):
    key_bytes = Path(path).read_bytes()
    return serialization.load_pem_private_key(key_bytes, password=None)


def _sign_request(method: str, path: str, api_key_id: str, private_key) -> dict[str, str]:
    timestamp_ms = str(int(time.time() * 1000))
    msg = f"{timestamp_ms}{method.upper()}{path}"
    sig_bytes = private_key.sign(
        msg.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    sig_b64 = base64.b64encode(sig_bytes).decode("ascii")
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
    }


async def probe(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    private_key,
    api_key_id: str,
    *,
    params: dict | None = None,
) -> None:
    # Kalshi firma timestamp+method+path SIN querystring; los params van en la URL del
    # request (abajo). Incluir el ?qs en la firma da 401 INCORRECT_API_KEY_SIGNATURE
    # (mismo bug que se corrigió en kalshi_rest.py el 2026-06-17).
    sign_path = f"/trade-api/v2{path}"

    headers = _sign_request(method, sign_path, api_key_id, private_key)
    url = f"{BASE_URL}{path}"

    print(f"\n{'=' * 60}")
    print(f"→ {method} {path}")
    if params:
        print(f"  params: {params}")

    try:
        resp = await client.request(method, url, params=params, headers=headers)
        status = resp.status_code
        icon = "✅" if status < 400 else ("⚠️" if status == 429 else "❌")
        print(f"  {icon} Status: {status}")

        # Rate limit headers
        rl_headers = {
            k: v
            for k, v in resp.headers.items()
            if any(x in k.lower() for x in ["retry", "ratelimit", "x-rate", "limit", "remaining"])
        }
        if rl_headers:
            print(f"  Rate-limit headers: {rl_headers}")
        else:
            print("  Rate-limit headers: (ninguno)")

        body = resp.text[:400]
        print(f"  Body: {body}")

    except Exception as e:
        print(f"  ❌ Exception: {type(e).__name__}: {e}")


async def main() -> None:
    key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
    api_key_id = os.getenv("KALSHI_API_KEY_ID", "")

    if not key_path or not api_key_id:
        print("❌ KALSHI_PRIVATE_KEY_PATH y KALSHI_API_KEY_ID deben estar en .env")
        return

    if not Path(key_path).exists():  # noqa: ASYNC240 — chequeo sync de arranque, no I/O en loop
        print(f"❌ Clave privada no encontrada: {key_path}")
        return

    print(f"=== Kalshi diagnostic - target host: {BASE_URL} ===")
    print()
    print(f"API Key ID: {api_key_id[:12]}...")

    try:
        private_key = _load_private_key(key_path)
    except Exception as e:
        print(f"❌ No se pudo cargar clave privada: {e}")
        return

    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
        endpoints = [
            ("GET", "/portfolio/balance", None),
            # positions/fills con params: ahora firman bien (path sin qs) → prueban el fix.
            ("GET", "/portfolio/positions", {"limit": 200}),
            ("GET", "/portfolio/fills", {"limit": 50}),
            ("GET", "/account/limits", None),
            ("GET", "/exchange/status", None),
            ("GET", "/events", {"limit": 5, "status": "open"}),
        ]

        for method, path, params in endpoints:
            await probe(client, method, path, private_key, api_key_id, params=params)
            await asyncio.sleep(PAUSE_BETWEEN_REQUESTS)

    print(f"\n{'=' * 60}")
    print("Diagnóstico completo.")


if __name__ == "__main__":
    asyncio.run(main())
