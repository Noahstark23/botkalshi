"""
Cliente REST asíncrono para Kalshi API v2.

Features:
    - Auth automática RSA-PSS por request
    - Retries con backoff exponencial (tenacity)
    - Distinción de errores retryables vs no-retryables
    - Logging estructurado de cada request
    - Async context manager para session management
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.auth.signer import KalshiSigner
from src.utils.config import get_settings


class KalshiAPIError(Exception):
    """Error genérico de Kalshi API."""

    def __init__(self, status_code: int, message: str, response_body: str = ""):
        self.status_code = status_code
        self.message = message
        self.response_body = response_body
        super().__init__(f"[{status_code}] {message}")


class KalshiAuthError(KalshiAPIError):
    """401/403 - no retry."""


class KalshiRateLimitError(KalshiAPIError):
    """429 - retry con backoff."""


class KalshiServerError(KalshiAPIError):
    """5xx - retry."""


class KalshiClientError(KalshiAPIError):
    """4xx (excepto 401/403/429) - no retry, bug nuestro."""


# Excepciones retryables
_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    KalshiRateLimitError,
    KalshiServerError,
)


class KalshiRestClient:
    """
    Cliente REST asíncrono para Kalshi.

    Uso:
        async with KalshiRestClient() as client:
            balance = await client.get_balance()
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.signer = KalshiSigner(
            private_key_path=self.settings.KALSHI_PRIVATE_KEY_PATH,
            api_key_id=self.settings.KALSHI_API_KEY_ID,
        )
        self.base_url = self.settings.rest_url
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> KalshiRestClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            http2=False,  # Kalshi no parece soportar h2 todavía
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _build_sign_path(self, path: str, params: dict | None) -> str:
        """
        Construye el path para signing.
        Kalshi requiere que el query string se incluya en el mensaje firmado.
        """
        sign_path = f"/trade-api/v2{path}"
        if params:
            qs = urlencode(sorted(params.items()))  # ordenado para reproducibilidad
            sign_path = f"{sign_path}?{qs}"
        return sign_path

    @staticmethod
    def _classify_error(status_code: int, response_text: str) -> KalshiAPIError:
        """Mapea status code → excepción específica."""
        if status_code in (401, 403):
            return KalshiAuthError(status_code, "Auth failed", response_text)
        if status_code == 429:
            return KalshiRateLimitError(status_code, "Rate limited", response_text)
        if 500 <= status_code < 600:
            return KalshiServerError(status_code, "Server error", response_text)
        return KalshiClientError(status_code, "Client error", response_text)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
    ) -> dict[str, Any]:
        """Request firmado con retries automáticos."""
        if self._client is None:
            raise RuntimeError("Cliente no inicializado. Usar `async with KalshiRestClient()`")

        # Solo GET puede llevar query string en signing
        sign_params = params if method.upper() == "GET" else None
        sign_path = self._build_sign_path(path, sign_params)

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
            reraise=True,
        ):
            with attempt:
                # Re-firma cada attempt (timestamp cambia)
                headers = self.signer.sign(method, sign_path)

                logger.debug(f"{method} {path} attempt={attempt.retry_state.attempt_number}")

                resp = await self._client.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    headers=headers,
                )

                if resp.status_code >= 400:
                    err = self._classify_error(resp.status_code, resp.text[:500])
                    logger.warning(
                        f"{method} {path} → {resp.status_code}: {resp.text[:200]}"
                    )
                    raise err

                return resp.json()

        # Inalcanzable, pero mypy quiere return
        raise RuntimeError("Retries inesperadamente agotados")

    # =====================================================
    # Account
    # =====================================================
    async def get_balance(self) -> dict:
        """Balance de la cuenta. Retorna `{'balance': cents}`."""
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self, *, limit: int = 100, cursor: str | None = None) -> dict:
        """Posiciones abiertas, paginadas."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/portfolio/positions", params=params)

    async def get_fills(self, *, limit: int = 100, ticker: str | None = None) -> dict:
        """Fills recientes (trades ejecutados de tu cuenta)."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return await self._request("GET", "/portfolio/fills", params=params)

    # =====================================================
    # Markets (público, no necesita auth técnicamente, pero firmamos igual)
    # =====================================================
    async def list_events(
        self,
        *,
        status: str = "open",
        series_ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        """Lista de eventos disponibles."""
        params: dict[str, Any] = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/events", params=params)

    async def get_event(self, event_ticker: str) -> dict:
        """Detalle de un evento con sus markets."""
        return await self._request("GET", f"/events/{event_ticker}")

    async def list_markets(
        self,
        *,
        event_ticker: str | None = None,
        status: str = "open",
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        """Lista de markets."""
        params: dict[str, Any] = {"status": status, "limit": limit}
        if event_ticker:
            params["event_ticker"] = event_ticker
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> dict:
        """Detalle de un market individual."""
        return await self._request("GET", f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, *, depth: int = 10) -> dict:
        """
        Orderbook de un market.
        CRÍTICO para detección de arbitraje y sizing.
        """
        return await self._request(
            "GET",
            f"/markets/{ticker}/orderbook",
            params={"depth": depth},
        )

    # =====================================================
    # Orders - SOLO USAR DESPUÉS DE VALIDACIÓN
    # =====================================================
    async def place_order(
        self,
        *,
        ticker: str,
        side: str,  # "yes" or "no"
        action: str,  # "buy" or "sell"
        count: int,
        order_type: str = "limit",
        yes_price: int | None = None,
        no_price: int | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        """
        Coloca una orden.

        IMPORTANTE: Antes de llamar este método:
        1. Verifica TRADING_ENABLED=true
        2. Valida con risk manager
        3. Considera idempotency con client_order_id
        """
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": order_type,
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if client_order_id:
            body["client_order_id"] = client_order_id

        logger.info(
            f"Placing order: {action} {count} {side} {ticker} @ "
            f"yes={yes_price} no={no_price}"
        )
        return await self._request("POST", "/portfolio/orders", json=body)

    async def cancel_order(self, order_id: str) -> dict:
        """Cancela una orden por ID."""
        logger.info(f"Cancelling order: {order_id}")
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def get_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> dict:
        """Lista de órdenes."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return await self._request("GET", "/portfolio/orders", params=params)
