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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from fractions import Fraction
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
from src.math.balance import available_cash_usd
from src.math.quantities import contract_count, fixed_count
from src.utils.config import get_settings


class _WriteThrottle:
    """
    Token bucket para WRITES de órdenes (place/cancel/batch) — Motor 5 F2.

    Techo interno conservador de 5 operaciones/seg, no una copia del tier de Kalshi.
    Kalshi usa presupuestos de tokens: create=10, cancel=2 por orden, incluso en batch.
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


class KalshiOrdersPaginationError(RuntimeError):
    """La colección de órdenes no pudo verificarse completa; no es una lista vacía."""


class KalshiOrderConfirmationError(RuntimeError):
    """La API no aportó evidencia suficiente: el caller debe conservar exposición."""

    def __init__(
        self,
        message: str,
        *,
        observed_order: dict | None = None,
        observed_orders: list[dict] | None = None,
    ) -> None:
        # Evidencia para piso/cuarentena, nunca una confirmación. No entra en args,
        # str ni repr: un log de excepción no debe volcar el payload de la cuenta.
        self.observed_order = dict(observed_order) if observed_order is not None else None
        self.observed_orders = [dict(order) for order in observed_orders or []]
        super().__init__(message)


@asynccontextmanager
async def _preserve_order_observations(results: dict, before_by_id: dict) -> AsyncIterator[None]:
    """Propaga la MISMA excepción con observed_orders: list[dict], máximo 60.

    Incluye TimeoutError y CancelledError: un cleanup interrumpido no puede borrar
    evidencia correlacionada que se obtuvo antes de fallar otra orden del batch.
    """
    try:
        yield
    except BaseException as exc:
        observed_orders: list[dict] = []

        def remember(oid: str, order: object) -> None:
            if not isinstance(order, dict) or order.get("order_id") != oid:
                return
            before = before_by_id.get(oid)
            if before is not None and any(
                order.get(key) != before.get(key)
                for key in ("client_order_id", "ticker", "subaccount_number", "exchange_index")
            ):
                return
            # Dos GET por orden, hasta 20 órdenes: como máximo 40 lecturas distintas.
            # El techo defensivo de 60 también acota metadatos de excepciones anómalas.
            if len(observed_orders) < 60 and order not in observed_orders:
                observed_orders.append(dict(order))

        for oid, before in before_by_id.items():
            remember(oid, before)
        for oid, item in results.items():
            remember(oid, item.get("order"))
            remember(oid, item.get("observed_order"))
            for order in item.get("observed_orders", [])[:60]:
                remember(oid, order)
        extra = getattr(exc, "observed_orders", None)
        if isinstance(extra, list):
            for order in extra[:60]:
                if isinstance(order, dict) and order.get("order_id") in before_by_id:
                    remember(order["order_id"], order)
        if observed_orders:
            # Atributo aparte: no cambia args/str/repr, ni envuelve la cancelación.
            exc.observed_orders = observed_orders
        raise


# La reconciliación es parte del tick: ni una API cíclica ni sus retries pueden
# bloquear indefinidamente la gestión de exposición pendiente.
_ORDERS_MAX_PAGES = 100
_ORDERS_MAX_CURSOR_LENGTH = 4096
_ORDERS_PAGINATION_TIMEOUT_SECONDS = 30.0
# Límites LOCALES: el límite del exchange depende del tier y del costo por ítem.
# Basic admite 100 tokens de write: 10 creates (10 c/u); 20 cancels usan 40 tokens.
_BATCH_CREATE_MAX_ORDERS = 10
_BATCH_CANCEL_MAX_ORDERS = 20
_ORDER_MUTATION_TIMEOUT_SECONDS = 30.0
_TERMINAL_ORDER_STATUSES = frozenset({"canceled", "cancelled", "executed"})


def _validate_order_id(order_id: object) -> None:
    if (
        not isinstance(order_id, str)
        or not 1 <= len(order_id) <= 128
        or not all(c.isascii() and (c.isalnum() or c in "-_") for c in order_id)
    ):
        raise ValueError("order_id inválido")


def _observed_count(value: object) -> Decimal:
    """Conservar precisión/fracciones; el ledger decide si puede representarlas."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise KalshiOrderConfirmationError("cantidad ausente o inválida")
    if len(str(value)) > 64:
        raise KalshiOrderConfirmationError("cantidad demasiado larga")
    try:
        count = Decimal(value)
    except InvalidOperation as exc:
        raise KalshiOrderConfirmationError("cantidad inválida") from exc
    if not count.is_finite() or count < 0:
        raise KalshiOrderConfirmationError("cantidad no finita o negativa")
    return count


def _order_count(order: dict, name: str) -> Decimal:
    fixed = f"{name}_fp"
    value = _observed_count(order[fixed] if fixed in order else order.get(name))
    if fixed in order and order.get(name) is not None and _observed_count(order[name]) != value:
        raise KalshiOrderConfirmationError("cantidades contradictorias")
    return value


def _cancel_route(order: dict) -> dict:
    """Nunca cancelar a ciegas en shard/subcuenta 0 una orden listada en otro."""
    if not all(
        isinstance(order.get(key), str) and order[key] for key in ("ticker", "client_order_id")
    ):
        raise KalshiOrderConfirmationError("identidad incompleta para cancelar")
    subaccount = order.get("subaccount_number")
    exchange = order.get("exchange_index")
    if type(subaccount) is not int or not 0 <= subaccount <= 63:
        raise KalshiOrderConfirmationError("subcuenta de la orden inválida")
    if type(exchange) is not int or exchange < 0:
        raise KalshiOrderConfirmationError("exchange de la orden inválido")
    return {"market_ticker": order["ticker"], "subaccount": subaccount, "exchange_index": exchange}


def _validate_cancel_ack(ack: object, before: dict) -> None:
    if (
        not isinstance(ack, dict)
        or ack.get("error") is not None
        or ack.get("order_id") != before["order_id"]
        or ("client_order_id" in ack and ack["client_order_id"] != before["client_order_id"])
    ):
        raise KalshiOrderConfirmationError("ack de cancelación no correlacionado")
    # reduced_by sólo valida el ack: JAMÁS se usa para reconstruir fill_count.
    _observed_count(ack.get("reduced_by"))


def _validate_terminal_order(order: dict, before: dict) -> None:
    for key in ("order_id", "client_order_id", "ticker", "subaccount_number", "exchange_index"):
        if order.get(key) != before.get(key):
            raise KalshiOrderConfirmationError("la lectura posterior cambió la identidad")
    _cancel_route(order)
    try:
        if (
            not isinstance(order.get("status"), str)
            or order["status"] not in _TERMINAL_ORDER_STATUSES
        ):
            raise KalshiOrderConfirmationError("la orden todavía no es terminal")
        filled = _order_count(order, "fill_count")
        initial = _order_count(order, "initial_count")
        if (
            _order_count(order, "remaining_count") != 0
            or initial != _order_count(before, "initial_count")
            or not _order_count(before, "fill_count") <= filled <= initial
            or (order["status"] == "executed" and (filled == 0 or filled != initial))
        ):
            raise KalshiOrderConfirmationError("cantidades terminales no confirmadas")
    except KalshiOrderConfirmationError as exc:
        # La identidad ya se correlacionó. Preservar incluso un fill imposible evita
        # que una lectura posterior con cero borre la evidencia que exige cuarentena.
        raise KalshiOrderConfirmationError(
            str(exc), observed_order=order, observed_orders=[before, order]
        ) from exc


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
    count: int | Fraction,
    yes_price: int | None = None,
    no_price: int | None = None,
    client_order_id: str | None = None,
    time_in_force: str = "gtc",
    post_only: bool = False,
    reduce_only: bool = False,
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
    if action not in {"buy", "sell"}:
        raise ValueError("build_order_body: action debe ser buy o sell")
    parsed_count = contract_count(count)
    if parsed_count is None or parsed_count <= 0:
        raise ValueError("build_order_body: cantidad positiva en centésimas requerida")
    if type(reduce_only) is not bool:
        raise ValueError("build_order_body: reduce_only debe ser bool")
    if reduce_only and (action != "sell" or time_in_force != "immediate_or_cancel" or post_only):
        raise ValueError("build_order_body: salida reduce_only requiere SELL IOC sin post_only")
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
        "count": str(int(parsed_count))
        if parsed_count.denominator == 1
        else fixed_count(parsed_count),
        "price": f"{Decimal(price_cents) / 100:.2f}",
        "time_in_force": tif,
        # Cancela la pata taker si se auto-cruzaría (lo ya matcheado igual ejecuta).
        "self_trade_prevention_type": "taker_at_cross",
    }
    if post_only:
        body["post_only"] = True
    if reduce_only:
        body["reduce_only"] = True
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
        # un MM que cotiza N tickers por tick puede rafaguear writes. Techo interno
        # conservador de 5 operaciones/s; no sustituye el presupuesto tokenizado del tier.
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
    async def get_balance(self, *, subaccount: int | None = None) -> dict:
        """Balance de la cuenta. Retorna `{'balance': cents}`."""
        if subaccount is None:
            return await self._request("GET", "/portfolio/balance")
        return await self._request("GET", "/portfolio/balance", params={"subaccount": subaccount})

    async def get_available_balance_usd(self) -> float:
        """Cash disponible en USD (Bug 1, incidente 2026-07-07: pre-check antes de colocar
        patas de arb — el balance REAL de Kalshi es lo único que decide si acepta la orden).
        Lanza ValueError ante efectivo ausente, inválido o contradictorio."""
        return float(available_cash_usd(await self.get_balance()))

    async def get_positions(
        self, *, limit: int = 100, cursor: str | None = None, subaccount: int | None = None
    ) -> dict:
        """Posiciones abiertas, paginadas."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        if subaccount is not None:
            params["subaccount"] = subaccount
        return await self._request("GET", "/portfolio/positions", params=params)

    async def get_fills(
        self,
        *,
        limit: int = 100,
        ticker: str | None = None,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        subaccount: int | None = None,
    ) -> dict:
        """Fills recientes (trades ejecutados de tu cuenta)."""
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        if subaccount is not None:
            params["subaccount"] = subaccount
        return await self._request("GET", "/portfolio/fills", params=params)

    async def get_settlements(
        self,
        *,
        limit: int = 200,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        subaccount: int | None = None,
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
        if subaccount is not None:
            params["subaccount"] = subaccount
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
        reduce_only: bool = False,
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
        # SELL también puede abrir exposición contraria. Sólo una salida IOC
        # con reduce_only validado por el exchange conserva la excepción al OFF.
        # La atribución al motor sigue siendo responsabilidad de cada executor.
        self._guard_order_submission(action=action, reduce_only=reduce_only)

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
            reduce_only=reduce_only,
        )
        logger.info(
            f"Placing order (V2): {action} {count} {side} {ticker} "
            f"→ side={body['side']} price=${body['price']} tif={body['time_in_force']}"
            f"{' post_only' if post_only else ''}"
        )
        await self._write_throttle.acquire()
        self._guard_order_submission(action=action, reduce_only=reduce_only)
        return await self._request("POST", "/portfolio/events/orders", json=body)

    def _guard_order_submission(self, *, action: str, reduce_only: bool) -> None:
        if self.settings.TRADING_ENABLED is not True and not (
            action == "sell" and reduce_only is True
        ):
            raise TradingDisabledError("Trading apagado: sólo se permiten salidas SELL reduce_only")

    async def cancel_order(self, order_id: str) -> dict:
        """V2 ack + GET terminal; no libera exposición usando sólo reduced_by.

        El GET previo conserva la firma histórica (sólo ID), pero resuelve shard y
        subcuenta desde la orden observada. Todo el flujo, incluidos retries, tiene 30s.
        """
        _validate_order_id(order_id)
        before_by_id: dict[str, dict] = {}
        async with (
            _preserve_order_observations({}, before_by_id),
            asyncio.timeout(_ORDER_MUTATION_TIMEOUT_SECONDS),
        ):
            before = (await self.get_order(order_id))["order"]
            # GET correlacionó el ID. Un routing incompleto prohíbe el DELETE, pero
            # no borra fills observados: el consumidor revalida la identidad local.
            before_by_id[order_id] = before
            route = _cancel_route(before)
            if (
                isinstance(before.get("status"), str)
                and before["status"] in _TERMINAL_ORDER_STATUSES
            ):
                _validate_terminal_order(before, before)
                return {"order": before}
            await self._write_throttle.acquire()
            ack = await self._request(
                "DELETE", f"/portfolio/events/orders/{order_id}", params=route
            )
            _validate_cancel_ack(ack, before)
            after = (await self.get_order(order_id))["order"]
            _validate_terminal_order(after, before)
            return {"order": after}

    async def get_order(self, order_id: str) -> dict:
        """La lectura sigue en /portfolio/orders; V2 sólo migró las mutaciones."""
        _validate_order_id(order_id)
        async with asyncio.timeout(_ORDER_MUTATION_TIMEOUT_SECONDS):
            response = await self._request("GET", f"/portfolio/orders/{order_id}")
        order = response.get("order") if isinstance(response, dict) else None
        if (
            not isinstance(order, dict)
            or response.get("error") is not None
            or order.get("error") is not None
            or order.get("order_id") != order_id
        ):
            raise KalshiOrderConfirmationError("GET de orden sin identidad confirmada")
        return response

    async def batch_create_orders(self, orders: list[dict]) -> dict:
        """
        Crea órdenes en batch (POST /portfolio/events/orders/batched) — Motor 5 F2.

        `orders` = bodies V2 construidos con build_order_body (misma traducción que
        place_order, una sola fuente de verdad). Hasta 10 órdenes por llamada, límite
        local compatible con los 100 tokens del bucket Basic (10 tokens/create).

        GUARD (más estricto que la Capa C de place_order): con TRADING_ENABLED=false
        se bloquea TODO el batch, incluidos asks. Un ask del MM NO es un sell protector:
        vender YES sin posición ABRE una posición NO (identidad V2) — es una ENTRADA.
        Ningún flujo de rollback usa batch, así que no hay sell legítimo que frenar.
        """
        if self.settings.TRADING_ENABLED is not True:
            raise TradingDisabledError(
                f"batch_create_orders bloqueado: TRADING_ENABLED=false (n={len(orders)})"
            )
        if not orders:
            return {"orders": []}
        if len(orders) > _BATCH_CREATE_MAX_ORDERS:
            raise ValueError("batch_create_orders: límite local de 10 órdenes")
        async with asyncio.timeout(_ORDER_MUTATION_TIMEOUT_SECONDS):
            await self._write_throttle.acquire(len(orders))
            self._guard_order_submission(action="buy", reduce_only=False)
            resp = await self._request(
                "POST", "/portfolio/events/orders/batched", json={"orders": orders}
            )
        logger.info(f"batch_create_orders n={len(orders)}")
        return resp

    async def batch_cancel_orders(self, order_ids: list[str]) -> dict:
        """
        Cancelación V2, máximo local de 20 órdenes y 30s incluyendo lecturas.

        Un ack no es un Order. Cada resultado exitoso devuelve únicamente el Order
        terminal leído después; ausentes/duplicados/errores quedan sin confirmar.
        Cancelar sigue permitido con trading apagado. Costo oficial: 2 tokens/ítem.
        Si el batch sale por excepción, observed_orders contiene las evidencias
        correlacionadas ya recogidas (máximo 60 Orders; piso de fill/cuarentena,
        nunca confirmación de cierre). Los errores por ítem preservan la misma lista.
        """
        if not order_ids:
            return {"orders": []}
        if len(order_ids) > _BATCH_CANCEL_MAX_ORDERS:
            raise ValueError("batch_cancel_orders: límite local de 20 órdenes")
        for order_id in order_ids:
            _validate_order_id(order_id)
        if len(set(order_ids)) != len(order_ids):
            raise ValueError("batch_cancel_orders: IDs duplicados")
        results = {
            oid: {"order_id": oid, "error": {"code": "cancel_unconfirmed"}} for oid in order_ids
        }
        before_by_id: dict[str, dict] = {}
        requests = []
        async with (
            _preserve_order_observations(results, before_by_id),
            asyncio.timeout(_ORDER_MUTATION_TIMEOUT_SECONDS),
        ):
            for oid in order_ids:
                try:
                    before = (await self.get_order(oid))["order"]
                    before_by_id[oid] = before
                    results[oid]["observed_orders"] = [before]
                    route = _cancel_route(before)
                    if (
                        isinstance(before.get("status"), str)
                        and before["status"] in _TERMINAL_ORDER_STATUSES
                    ):
                        _validate_terminal_order(before, before)
                        results[oid] = {"order_id": oid, "order": before}
                        continue
                    requests.append({"order_id": oid, **route})
                except Exception as exc:
                    if (
                        isinstance(exc, KalshiOrderConfirmationError)
                        and exc.observed_order is not None
                        and exc.observed_order.get("order_id") == oid
                    ):
                        results[oid]["observed_order"] = exc.observed_order
                        results[oid]["observed_orders"] = exc.observed_orders
                    logger.warning(
                        f"batch_cancel_orders route_unconfirmed type={type(exc).__name__}"
                    )
            if requests:
                await self._write_throttle.acquire(len(requests))
                response = await self._request(
                    "DELETE", "/portfolio/events/orders/batched", json={"orders": requests}
                )
                items = response.get("orders") if isinstance(response, dict) else None
                if (
                    not isinstance(items, list)
                    or len(items) > _BATCH_CANCEL_MAX_ORDERS
                    or response.get("error") is not None
                ):
                    raise KalshiOrderConfirmationError(
                        "batch cancel sin resultados correlacionables"
                    )
                by_id: dict[str, dict] = {}
                duplicates = set()
                requested_ids = {request["order_id"] for request in requests}
                for item in items:
                    oid = item.get("order_id") if isinstance(item, dict) else None
                    if not isinstance(oid, str) or oid not in requested_ids:
                        raise KalshiOrderConfirmationError("batch cancel con identidad desconocida")
                    if oid in by_id:
                        duplicates.add(oid)
                    by_id[oid] = item
                for oid, before in before_by_id.items():
                    if oid in duplicates or oid not in by_id:
                        continue
                    try:
                        _validate_cancel_ack(by_id[oid], before)
                        after = (await self.get_order(oid))["order"]
                        _validate_terminal_order(after, before)
                        results[oid] = {"order_id": oid, "order": after}
                    except Exception as exc:
                        if (
                            isinstance(exc, KalshiOrderConfirmationError)
                            and exc.observed_order is not None
                            and exc.observed_order.get("order_id") == oid
                        ):
                            results[oid]["observed_order"] = exc.observed_order
                            results[oid]["observed_orders"] = exc.observed_orders
                        logger.warning(
                            f"batch_cancel_orders confirmation_missing type={type(exc).__name__}"
                        )
        logger.info(f"batch_cancel_orders n={len(order_ids)}")
        return {"orders": [results[oid] for oid in order_ids]}

    async def get_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        subaccount: int | None = None,
    ) -> dict:
        """Una página de órdenes; no implica que la colección esté completa."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("get_orders: limit debe ser un entero entre 1 y 1000")
        if cursor is not None and (
            not isinstance(cursor, str) or len(cursor) > _ORDERS_MAX_CURSOR_LENGTH
        ):
            raise ValueError("get_orders: cursor inválido o demasiado largo")
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        if cursor:
            params["cursor"] = cursor
        if subaccount is not None:
            params["subaccount"] = subaccount
        return await self._request("GET", "/portfolio/orders", params=params)

    async def get_all_orders(
        self,
        *,
        ticker: str | None = None,
        status: str | None = None,
        limit: int = 200,
        max_pages: int = 100,
    ) -> list[dict]:
        """Colección completa o excepción: nunca devuelve un prefijo parcial.

        El contrato de Kalshi termina con cursor="", no con una página corta/vacía.
        Cursor ausente, repetido o mal formado deja la exposición como incierta.
        Tope duro: 100 páginas de hasta 1000 órdenes y 30s para toda la lectura.
        Las identidades/estados de cada orden los valida el reconciliador.
        """
        if type(max_pages) is not int or not 1 <= max_pages <= _ORDERS_MAX_PAGES:
            raise ValueError("get_all_orders: max_pages debe ser un entero entre 1 y 100")
        # Validar también aquí evita arrancar una colección con presupuesto inválido,
        # incluso si una implementación de prueba sustituye get_orders.
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("get_all_orders: limit debe ser un entero entre 1 y 1000")
        orders: list[dict] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        async with asyncio.timeout(_ORDERS_PAGINATION_TIMEOUT_SECONDS):
            for _ in range(max_pages):
                page = await self.get_orders(
                    ticker=ticker, status=status, limit=limit, cursor=cursor
                )
                if not isinstance(page, dict) or not isinstance(page.get("orders"), list):
                    raise KalshiOrdersPaginationError("get_all_orders: página de orders inválida")
                page_orders = page["orders"]
                if len(page_orders) > limit:
                    raise KalshiOrdersPaginationError("get_all_orders: página excede limit")
                if any(not isinstance(order, dict) for order in page_orders):
                    raise KalshiOrdersPaginationError("get_all_orders: entrada de orders inválida")
                next_cursor = page.get("cursor")
                if not isinstance(next_cursor, str) or len(next_cursor) > _ORDERS_MAX_CURSOR_LENGTH:
                    raise KalshiOrdersPaginationError("get_all_orders: cursor ausente o inválido")
                orders.extend(page_orders)
                if not next_cursor:
                    return orders
                if next_cursor in seen_cursors:
                    raise KalshiOrdersPaginationError("get_all_orders: ciclo de cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
        raise KalshiOrdersPaginationError("get_all_orders: max_pages agotado sin cursor final")
