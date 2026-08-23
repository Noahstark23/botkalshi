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

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
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


class _WriteThrottle:
    """
    Token bucket para WRITES de órdenes (place/cancel/batch) — Motor 5 F2.

    Kalshi limita ~20 writes/seg; el techo interno es 5/seg (25%) con burst = capacidad.
    acquire(n) espera lo necesario (n>capacidad se degrada a esperar n/rate). Es
    PREVENTIVO: el retry del 429 sigue existiendo como red reactiva aguas abajo.
    time_fn/sleep_fn inyectables para tests determinísticos.
    """

    def __init__(
        self,
        rate_per_sec: float = 5.0,
        burst: float = 5.0,
        *,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._rate = rate_per_sec
        self._capacity = burst
        self._tokens = burst
        self._last: float | None = None
        self._time = time_fn
        self._sleep = sleep_fn or asyncio.sleep
        self._lock = asyncio.Lock()

    async def acquire(self, n: int = 1) -> None:
        async with self._lock:
            now = self._time()
            if self._last is not None:
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= n:
                self._tokens -= n
                return
            wait = (n - self._tokens) / self._rate
            self._tokens = 0.0
            await self._sleep(wait)
            self._last = self._time()


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


def build_order_body(
    *,
    ticker: str,
    side: str,  # "yes" | "no"
    action: str,  # "buy" | "sell"
    count: int,
    yes_price: int | None = None,
    no_price: int | None = None,
    client_order_id: str | None = None,
    time_in_force: str = "gtc",
    post_only: bool = False,
) -> dict[str, Any]:
    """
    Traducción (side yes/no, action buy/sell, precio en ¢) → body del contrato V2.

    Extraída de place_order (Motor 5 F2) para que el batch create use EXACTAMENTE la
    misma semántica de mapeo — dos traducciones divergentes serían un bug de plata real.

    Kalshi deprecó el endpoint legacy /portfolio/orders (410 deprecated_v1_order_endpoint).
    V2 cotiza TODO desde el libro YES: `side` ∈ {bid = comprar YES, ask = vender YES},
    un ÚNICO `price` en DÓLARES (string fixed-point, ej. "0.52"), SIN campos `action`
    ni `type`. Mapeo con la identidad comprar-NO @ P¢ ≡ vender-YES @ (100−P)¢:
      yes + buy  → bid,  precio = yes_price
      yes + sell → ask,  precio = yes_price
      no  + buy  → ask,  precio = 100 − no_price   (comprar NO = vender YES al complemento)
      no  + sell → bid,  precio = 100 − no_price   (vender  NO = comprar YES al complemento)

    post_only=True agrega el campo al body (solo cuando se pide: los callers existentes
    no cambian ni un byte de su payload). Semántica a validar contra demo (plan §1.4).
    """
    if side == "yes":
        if yes_price is None:
            raise ValueError("build_order_body: side='yes' requiere yes_price")
        price_cents = yes_price
        book_side = "bid" if action == "buy" else "ask"
    elif side == "no":
        if no_price is None:
            raise ValueError("build_order_body: side='no' requiere no_price")
        price_cents = 100 - no_price
        book_side = "ask" if action == "buy" else "bid"
    else:
        raise ValueError(f"build_order_body: side inválido {side!r} (esperado 'yes'|'no')")
    if not (1 <= price_cents <= 99):
        raise ValueError(f"build_order_body: precio fuera de rango [1,99]: {price_cents}c")

    # V2 solo acepta los TIF completos; normalizar el atajo histórico 'gtc'. order_type
    # ('limit'/'market') NO existe en V2: la inmediatez la da el time_in_force (FOK/IOC).
    tif = "good_till_canceled" if time_in_force == "gtc" else time_in_force
    # FixedPointDollars: dólares como string (Decimal exacto, NUNCA float), 2 decimales
    # (el precio es en ¢ enteros → exacto). 52¢ → "0.52", 40¢ → "0.40".
    body: dict[str, Any] = {
        "ticker": ticker,
        # client_order_id es REQUERIDO en V2 (idempotencia); los callers siempre lo pasan.
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "side": book_side,
        "count": str(count),  # FixedPointCount: acepta "10"
        "price": f"{Decimal(price_cents) / 100:.2f}",
        "time_in_force": tif,
        # Cancela la pata taker si se auto-cruzaría (lo ya matcheado igual ejecuta).
        "self_trade_prevention_type": "taker_at_cross",
    }
    if post_only:
        body["post_only"] = True
    return body


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
            private_key_pem=self.settings.clave_privada_env(),
        )
        self.base_url = self.settings.rest_url
        self._client: httpx.AsyncClient | None = None
        # Throttle preventivo de WRITES (Motor 5 F2): hoy solo hay retry REACTIVO al 429;
        # un MM que cotiza N tickers por tick puede rafaguear writes. Techo interno ~25%
        # del límite de Kalshi (20 writes/s) = 5/s. Los READS no se limitan.
        self._write_throttle = _WriteThrottle()

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

    async def get_available_balance_usd(self) -> float:
        """Cash disponible en USD (Bug 1, incidente 2026-07-07: pre-check antes de colocar
        patas de arb — el balance REAL de Kalshi es lo único que decide si acepta la orden).
        Lanza ValueError si la respuesta no trae 'balance' (el caller decide fail-open)."""
        data = await self.get_balance()
        cents = data.get("balance") if isinstance(data, dict) else None
        if cents is None:
            raise ValueError(f"get_balance sin campo 'balance': {data!r}")
        return float(cents) / 100.0

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

    async def get_series(self, series_ticker: str) -> dict:
        """Detalle público de una serie, incluida su política de fees vigente."""
        return await self._request("GET", f"/series/{series_ticker}")

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
        post_only: bool = False,
    ) -> dict:
        """
        Coloca una orden vía el endpoint V2 (POST /portfolio/events/orders).

        post_only (Motor 5 F2): la orden solo entra si NO cruza el spread (maker puro);
        si cruzaría, la API la rechaza en vez de ejecutarla como taker. ⚠️ Semántica a
        VALIDAR contra demo (plan motor_5 §1.4 residual) — si la API no soporta el campo,
        el fallback documentado es el chequeo pre-envío del book (el quoter ya lo emula).

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
        # guard. Invariante: en los productos BINARIOS que este bot opera, buy =
        # abrir posición y sell = cerrar. Reconcile usa get_orders/get_positions,
        # no place_order, así que no se ve afectado.
        #
        # ⚠️ ALCANCE DEL INVARIANTE (acotado 2026-08-15): "no hay short" describe
        # nuestro UNIVERSO (binarios de eventos), NO a Kalshi entero — desde el
        # 3-jun-2026 el exchange lista PERPETUOS apalancados, donde un `sell`
        # ABRE un corto. Hoy el bot no tiene una sola línea de perps, así que la
        # excepción sigue siendo segura. PRE-CONDICIÓN para el futuro: si alguna
        # vez entra CUALQUIER producto con posición corta (perps, márgenes), este
        # guard se cierra ANTES de que exista su executor — no después. De lo
        # contrario la Capa C, que es la defensa de último recurso, se convierte
        # en el agujero por donde sale un corto apalancado con el trading
        # supuestamente apagado.
        if action != "sell" and not self.settings.TRADING_ENABLED:
            raise TradingDisabledError(
                f"place_order bloqueado: TRADING_ENABLED=false "
                f"(action={action} ticker={ticker} count={count})"
            )

        body = build_order_body(
            ticker=ticker,
            side=side,
            action=action,
            count=count,
            yes_price=yes_price,
            no_price=no_price,
            client_order_id=client_order_id,
            time_in_force=time_in_force,
            post_only=post_only,
        )
        logger.info(
            f"Placing order (V2): {action} {count} {side} {ticker} "
            f"→ side={body['side']} price=${body['price']} tif={body['time_in_force']}"
            f"{' post_only' if post_only else ''}"
        )
        await self._write_throttle.acquire()
        return await self._request("POST", "/portfolio/events/orders", json=body)

    async def cancel_order(self, order_id: str) -> dict:
        """Cancela una orden por ID."""
        logger.info(f"Cancelling order: {order_id}")
        await self._write_throttle.acquire()
        return await self._request("DELETE", f"/portfolio/orders/{order_id}")

    async def batch_create_orders(self, orders: list[dict]) -> dict:
        """
        Crea órdenes en batch (POST /portfolio/orders/batched) — Motor 5 F2.

        `orders` = bodies V2 construidos con build_order_body (misma traducción que
        place_order, una sola fuente de verdad). ⚠️ Semántica del endpoint a VALIDAR
        contra demo con respuestas crudas (plan §4 gate: matriz de validación API) —
        por eso se loguea el response completo (truncado).

        GUARD (más estricto que la Capa C de place_order): con TRADING_ENABLED=false
        se bloquea TODO el batch, incluidos asks. Un ask del MM NO es un sell protector:
        vender YES sin posición ABRE una posición NO (identidad V2) — es una ENTRADA.
        Ningún flujo de rollback usa batch, así que no hay sell legítimo que frenar.
        """
        if not self.settings.TRADING_ENABLED:
            raise TradingDisabledError(
                f"batch_create_orders bloqueado: TRADING_ENABLED=false (n={len(orders)})"
            )
        if not orders:
            return {"orders": []}
        await self._write_throttle.acquire(len(orders))
        resp = await self._request("POST", "/portfolio/orders/batched", json={"orders": orders})
        logger.info(f"batch_create_orders n={len(orders)} → {str(resp)[:400]}")
        return resp

    async def batch_cancel_orders(self, order_ids: list[str]) -> dict:
        """
        Cancela órdenes en batch (DELETE /portfolio/orders/batched) — Motor 5 F2.

        Cancelar es SIEMPRE protector (reduce exposición pendiente) → sin gate de
        trading. Es la pieza del cancel-all <5s del kill-switch del MM. ⚠️ Semántica a
        validar contra demo (response crudo logueado).
        """
        if not order_ids:
            return {"ids": []}
        await self._write_throttle.acquire(len(order_ids))
        resp = await self._request("DELETE", "/portfolio/orders/batched", json={"ids": order_ids})
        logger.info(f"batch_cancel_orders n={len(order_ids)} → {str(resp)[:400]}")
        return resp

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
