"""Puerto M5 Demo: sólo metadatos/libros, host fijo y presupuesto acotado.

Sin herencia del cliente de órdenes, proxies, redirects ni retries. El harness
aplica además un límite global HTTP/WS y de duración. Nunca copia errores crudos.
"""

from __future__ import annotations

import asyncio
import json
import re

import httpx

from src.auth.signer import KalshiSigner
from src.utils.config import get_settings

DEMO_ROOT = "https://external-api.demo.kalshi.co/trade-api/v2"
MAX_REQUESTS = 60
MAX_BYTES = 2_000_000
EXECUTION_FLAGS = (
    "TRADING_ENABLED",
    "MOTOR_1_EXECUTION_ENABLED",
    "MOTOR_2_ENTRY_EXECUTION_ENABLED",
    "MOTOR_2_EXECUTION_ENABLED",
    "MOTOR_3_EXECUTION_ENABLED",
    "MOTOR_REST_EXECUTION_ENABLED",
    "MOTOR_MM_EXECUTION_ENABLED",
)


class DemoReadError(RuntimeError):
    """Código estable sin headers, cuerpos ni excepciones con secretos."""


class KalshiDemoReadOnlyClient:
    def __init__(self):
        settings = get_settings()
        self._check_settings(settings)
        self._settings = settings
        self._http = None
        self._signer = None
        self._requests = 0

    @staticmethod
    def _check_settings(settings):
        if (
            settings.KALSHI_ENV != "demo"
            or settings.rest_url != DEMO_ROOT
            or any(getattr(settings, name) for name in EXECUTION_FLAGS)
        ):
            raise DemoReadError("DEMO_READ_ONLY_REQUIRED")

    async def __aenter__(self):
        self._check_settings(self._settings)  # Antes del signer, aun si cambió el objeto.
        if self._http is not None:
            raise DemoReadError("CLIENT_ALREADY_OPEN")
        try:
            self._signer = KalshiSigner(
                private_key_path=self._settings.KALSHI_PRIVATE_KEY_PATH,
                api_key_id=self._settings.KALSHI_API_KEY_ID,
                private_key_pem=self._settings.clave_privada_env(),
            )
            self._http = httpx.AsyncClient(
                base_url=DEMO_ROOT,
                timeout=httpx.Timeout(5.0),
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            )
        except Exception:
            self._signer = None
            raise DemoReadError("CLIENT_OPEN_FAILED") from None
        return self

    async def __aexit__(self, *args):
        if self._http is not None:
            await self._http.aclose()
            self._http = None
        self._signer = None

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value
        ):
            raise DemoReadError("INVALID_IDENTIFIER")
        return value

    async def _get(self, path, *, params=None):
        if self._http is None or self._signer is None:
            raise DemoReadError("CLIENT_NOT_OPEN")
        if not re.fullmatch(
            r"/(?:series|events|markets)/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}(?:/orderbook)?", path
        ):
            raise DemoReadError("READ_ROUTE_BLOCKED")
        if self._requests >= MAX_REQUESTS:
            raise DemoReadError("READ_BUDGET_EXHAUSTED")
        self._requests += 1
        try:
            headers = self._signer.sign("GET", "/trade-api/v2" + path)
            async with (
                asyncio.timeout(5),
                self._http.stream("GET", path, params=params, headers=headers) as response,
            ):
                if response.status_code != 200:
                    raise DemoReadError(f"READ_HTTP_{response.status_code}")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > MAX_BYTES:
                        raise DemoReadError("READ_RESPONSE_LIMIT")
                    raw.extend(chunk)
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("error") is not None:
                raise DemoReadError("READ_PAYLOAD_INVALID")
            return data
        except DemoReadError:
            raise
        except Exception:
            raise DemoReadError("READ_FAILED") from None

    async def get_series(self, ticker):
        return await self._get("/series/" + self._identifier(ticker))

    async def get_event(self, ticker):
        return await self._get("/events/" + self._identifier(ticker))

    async def get_market(self, ticker):
        return await self._get("/markets/" + self._identifier(ticker))

    async def get_orderbook(self, ticker, *, depth=10):
        if type(depth) is not int or not 1 <= depth <= 100:
            raise DemoReadError("READ_DEPTH_INVALID")
        return await self._get(
            "/markets/" + self._identifier(ticker) + "/orderbook", params={"depth": depth}
        )
