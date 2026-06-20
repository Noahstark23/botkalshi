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

import json
import uuid
from decimal import Decimal
from typing import Any

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

    def __init__(
        self, status_code: int, message: str, response_body: str = "", error_code: str | None = None
    ):
        self.status_code = status_code
        self.message = message
        self.response_body = response_body
        # error.code del body JSON de Kalshi (p.ej. "fill_or_kill_insufficient_resting_volume").
        # None si el body no traía un code parseable. Lo usan los callers para distinguir
        # causas determinísticas (KILL de FOK) de errores genéricos.
        self.error_code = error_code
        super().__init__(
            f"[{status_code}] {message}" + (f" code={error_code}" if error_code else "")
        )


class KalshiAuthError(KalshiAPIError):
    """401/403 - no retry."""


class KalshiRateLimitError(KalshiAPIError):
    """429 - retry con backoff."""


class KalshiServerError(KalshiAPIError):
    """5xx - retry."""


class KalshiClientError(KalshiAPIError):
    """4xx (excepto 401/403/429) - no retry, bug nuestro."""


class TradingDisabledError(RuntimeError):
    """
    Capa C del muro: se intentó colocar una orden de ENTRADA con TRADING_ENABLED=false.

    Defensa final de bajo nivel — independiente de cualquier guard aguas arriba.
    NO es un error de la API de Kalshi (no tiene status_code): la orden nunca salió.
    """


# Excepciones retryables
_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    KalshiRateLimitError,
    KalshiServerError,
)


def _record_api_error(method: str, path: str, exc: Exception) -> None:
    """Record API error in BotState. Lazy import avoids potential circular deps."""
    try:
        from src.monitoring.health import BotState  # noqa: PLC0415

        BotState.record_error(f"Kalshi {method} {path}: {type(exc).__name__}: {str(exc)[:200]}")
    except Exception:
        pass  # best-effort; never let logging crash a request


def _extract_error_code(response_text: str) -> str | None:
    """
    Extrae error.code del body JSON de un error de Kalshi.

    Shape esperado: {"error": {"code": "...", "message": "..."}}. Algunos errores
    traen "code" en la raíz. Devuelve None si el body no es JSON o no trae code
    (best-effort: nunca lanza).
    """
    try:
        body = json.loads(response_text)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        code = err.get("code")
        if isinstance(code, str):
            return code
    root_code = body.get("code")
    if isinstance(root_code, str):
        return root_code
    return None


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

    def _build_sign_path(self, path: str) -> str:
        """
        Path para el signing. Kalshi firma `timestamp + method + path` SIN el query string.

        FIX 2026-06-17 (bug de plata real): incluir el ?qs en la firma daba 401
        INCORRECT_API_KEY_SIGNATURE en TODO endpoint autenticado con params
        (portfolio/fills?limit=, portfolio/positions?limit=) → el bot quedaba CIEGO a su
        cartera mientras place_order (POST sin query) sí firmaba bien. Evidencia: balance
        (sin params) firmaba OK. El querystring va en la URL del request, NUNCA en la firma.
        """
        return f"/trade-api/v2{path}"

    @staticmethod
    def _classify_error(status_code: int, response_text: str) -> KalshiAPIError:
        """Mapea status code → excepción específica, con error.code del body si lo hay."""
        error_code = _extract_error_code(response_text)
        if status_code in (401, 403):
            return KalshiAuthError(status_code, "Auth failed", response_text, error_code)
        if status_code == 429:
            return KalshiRateLimitError(status_code, "Rate limited", response_text, error_code)
        if 500 <= status_code < 600:
            return KalshiServerError(status_code, "Server error", response_text, error_code)
        return KalshiClientError(status_code, "Client error", response_text, error_code)

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

        # La firma va SOBRE EL PATH BASE (sin querystring); los params van en la URL.
        sign_path = self._build_sign_path(path)

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(4),
                # Kalshi no envía Retry-After; backoff puro: 1s, 2s, 4s... cap 60s
                wait=wait_exponential(multiplier=1, min=1, max=60),
                retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
                reraise=True,
            ):
                with attempt:
                    # Re-firma cada attempt (timestamp cambia)
                    headers = self.signer.sign(method, sign_path)
                    attempt_num = attempt.retry_state.attempt_number

                    logger.debug(f"{method} {path} attempt={attempt_num}")

                    resp = await self._client.request(
                        method,
                        path,
                        params=params,
                        json=json,
                        headers=headers,
                    )

                    if resp.status_code >= 400:
                        err = self._classify_error(resp.status_code, resp.text[:500])
                        if isinstance(err, KalshiRateLimitError):
                            logger.warning(
                                f"{method} {path} → 429 (attempt {attempt_num}/4): "
                                f"{resp.text[:200]}"
                            )
                        else:
                            logger.warning(
                                f"{method} {path} → {resp.status_code}: {resp.text[:200]}"
                            )
                        raise err

                    return resp.json()

        except Exception as exc:
            _record_api_error(method, path, exc)
            raise

        # Inalcanzable (always return or raise above), but mypy requires a return path
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

    async def get_settlements(
        self,
        *,
        limit: int = 200,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> dict:
        """
        Posiciones resueltas por el exchange. `revenue` (¢) = lo recibido al settlement.
        `min_ts`/`max_ts` son Unix en SEGUNDOS. Read-only.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return await self._request("GET", "/portfolio/settlements", params=params)

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
        time_in_force: str = "gtc",
    ) -> dict:
        """
        Coloca una orden vía el endpoint V2 (POST /portfolio/events/orders).

        La firma sigue siendo (side yes/no, action buy/sell, yes_price/no_price en ¢):
        internamente se traduce al contrato V2 (side bid/ask, price único en dólares). Ver
        el bloque de traducción abajo. `order_type` se ignora (V2 no tiene campo type).

        IMPORTANTE: Antes de llamar este método:
        1. Verifica TRADING_ENABLED=true
        2. Valida con risk manager
        3. Considera idempotency con client_order_id

        time_in_force:
            Default "gtc" (good-till-canceled) — replica el comportamiento histórico
            de Kalshi (que asume gtc si se omite), por lo que NO cambia el
            comportamiento de ningún caller existente.

            ⚠️ ADVERTENCIA: "gtc" deja la orden RESTING viva si no cruza — es el
            modo asociado al bug del Issue #14 (pata que queda abierta sin
            detectarse → exposición direccional silenciosa). Para arbitraje el
            caller DEBE pasar explícitamente time_in_force="fill_or_kill"
            (FOK: se ejecuta completa o se cancela, cero resting). NUNCA confiar
            en el default para órdenes de arbitraje.
        """
        # ── Capa C del muro (defensa final de bajo nivel) ──────────────────────
        # Con TRADING_ENABLED=false NINGUNA orden de ENTRADA puede salir, sin
        # importar bugs aguas arriba. Default-deny: se bloquea todo SALVO
        # action="sell", que es una SALIDA PROTECTORA (rollback cerrando una pata
        # expuesta). Si el flag se apagara a mitad de una ejecución, bloquear el
        # sell dejaría exposición abierta sin poder cerrarla — peor que no tener
        # guard. Invariante de Kalshi (verificado en todo el codebase, no hay
        # short): buy = abrir posición, sell = cerrar. Reconcile usa get_orders/
        # get_positions, no place_order, así que no se ve afectado.
        if action != "sell" and not self.settings.TRADING_ENABLED:
            raise TradingDisabledError(
                f"place_order bloqueado: TRADING_ENABLED=false "
                f"(action={action} ticker={ticker} count={count})"
            )

        # ── Traducción al contrato V2 (POST /portfolio/events/orders) ──────────────
        # Kalshi deprecó el endpoint legacy /portfolio/orders (410 deprecated_v1_order_endpoint).
        # V2 cotiza TODO desde el libro YES: `side` ∈ {bid = comprar YES, ask = vender YES},
        # un ÚNICO `price` en DÓLARES (string fixed-point, ej. "0.52"), SIN campos `action`
        # ni `type`. El bot razona en (side yes/no, action buy/sell, precio en ¢ del lado);
        # se mapea con la identidad comprar-NO @ P¢ ≡ vender-YES @ (100−P)¢:
        #   yes + buy  → bid,  precio = yes_price
        #   yes + sell → ask,  precio = yes_price
        #   no  + buy  → ask,  precio = 100 − no_price   (comprar NO = vender YES al complemento)
        #   no  + sell → bid,  precio = 100 − no_price   (vender  NO = comprar YES al complemento)
        if side == "yes":
            if yes_price is None:
                raise ValueError("place_order: side='yes' requiere yes_price")
            price_cents = yes_price
            book_side = "bid" if action == "buy" else "ask"
        elif side == "no":
            if no_price is None:
                raise ValueError("place_order: side='no' requiere no_price")
            price_cents = 100 - no_price
            book_side = "ask" if action == "buy" else "bid"
        else:
            raise ValueError(f"place_order: side inválido {side!r} (esperado 'yes'|'no')")
        if not (1 <= price_cents <= 99):
            raise ValueError(f"place_order: precio fuera de rango [1,99]: {price_cents}c")

        # V2 solo acepta los TIF completos; normalizar el atajo histórico 'gtc'. order_type
        # ('limit'/'market') NO existe en V2: la inmediatez la da el time_in_force (FOK/IOC).
        tif = "good_till_canceled" if time_in_force == "gtc" else time_in_force
        # FixedPointDollars: dólares como string (Decimal exacto, NUNCA float), 2 decimales
        # (el precio es en ¢ enteros → exacto). 52¢ → "0.52", 40¢ → "0.40".
        price_dollars = f"{Decimal(price_cents) / 100:.2f}"

        body: dict[str, Any] = {
            "ticker": ticker,
            # client_order_id es REQUERIDO en V2 (idempotencia); los callers siempre lo pasan.
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": book_side,
            "count": str(count),  # FixedPointCount: acepta "10"
            "price": price_dollars,
            "time_in_force": tif,
            # Cancela la pata taker si se auto-cruzaría (lo ya matcheado igual ejecuta).
            "self_trade_prevention_type": "taker_at_cross",
        }

        logger.info(
            f"Placing order (V2): {action} {count} {side} {ticker} @ {price_cents}c "
            f"→ side={book_side} price=${price_dollars} tif={tif}"
        )
        return await self._request("POST", "/portfolio/events/orders", json=body)

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
