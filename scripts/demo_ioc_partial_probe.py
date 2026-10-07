"""Sonda de protocolo IOC DEMO, independiente de cualquier motor.

Plan offline por defecto. --execute permite UNA intención BUY YES IOC 2@31c
del ticker fijado; presupuesto ficticio máximo 66c. --recover sólo lee/cancela
esa orden: nunca vuelve a crearla ni vende la posición. No demuestra alpha/M5.
Uso: python -m scripts.demo_ioc_partial_probe --state /ruta/privada/ioc.json
     agregar exclusivamente --execute o --recover para contactar DEMO.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import stat
import time
import uuid
from decimal import Decimal
from pathlib import Path

import httpx

DEMO_ROOT = "https://external-api.demo.kalshi.co/trade-api/v2"
TICKER = "KXAAAGASD-26OCT08-4.4000"
EVENT = "KXAAAGASD-26OCT08"
SERIES = "KXAAAGASD"
PREFIX = "demo-ioc-protocol-"
COUNT = 2
LIMIT_CENTS = 31
BUDGET_CENTS = 66
TOTAL_TIMEOUT = 27.0
REQUEST_TIMEOUT = 5.0
BOOK_MAX_AGE = 2.0
SCHEMA = "kalshi-demo-ioc-protocol-v1"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]{1,6})?\Z")
# Vocabulario cerrado de diagnóstico; jamás reflejar cadenas arbitrarias del
# servidor, aunque vengan en un campo llamado code. No es una clasificación de
# rechazo definitivo ni permiso para volver a intentar una orden.
SAFE_HTTP_CODES = frozenset(
    {
        "bad_request",
        "invalid_request",
        "invalid_parameters",
        "invalid_argument",
        "validation_error",
        "invalid_order",
        "invalid_ticker",
        "invalid_price",
        "invalid_count",
        "invalid_time_in_force",
        "unsupported_time_in_force",
        "insufficient_balance",
        "market_closed",
        "market_not_open",
        "market_not_found",
        "order_not_found",
        "not_found",
        "unauthorized",
        "invalid_api_key",
        "invalid_signature",
        "permission_denied",
        "forbidden",
        "rate_limit_exceeded",
        "too_many_requests",
        "service_unavailable",
        "internal_error",
        "internal_server_error",
        "conflict",
        "duplicate_client_order_id",
        "order_already_exists",
        "self_trade_prevention",
        "fill_or_kill_insufficient_resting_volume",
        "post_only_would_cross",
        "deprecated_v1_order_endpoint",
    }
)


class DemoHTTPError(RuntimeError):
    """Sólo diagnóstico público acotado; no retiene request/response ni secretos."""

    def __init__(self, status_code, error_code=None):
        self.status_code = status_code
        self.error_code = error_code if error_code in SAFE_HTTP_CODES else None
        super().__init__(f"DEMO HTTP {status_code}")


def number(raw, *, maximum=Decimal("1000000000"), signed=False, places=6):
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("Número no verificable")
    value = str(raw)
    if len(value) > 32 or not _NUMBER.fullmatch(value):
        raise ValueError("Número no verificable")
    result = Decimal(value)
    if result.as_tuple().exponent < -places or abs(result) > maximum or (result < 0 and not signed):
        raise ValueError("Número fuera del contrato")
    return result


def count(order, field):
    value = number(order.get(field + "_fp", order.get(field)), maximum=Decimal(COUNT), places=2)
    legacy = order.get(field)
    if legacy is not None and number(legacy, maximum=Decimal(COUNT), places=2) != value:
        raise ValueError("Cantidades contradictorias")
    return value


def save_state(path: Path, state: dict):
    """fsync de archivo Y directorio antes de red; nunca incluye respuestas crudas."""
    temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def plan():
    return {
        "schema": SCHEMA,
        "environment": "demo",
        "ticker": TICKER,
        "subaccount": 0,
        "count": COUNT,
        "limit_cents": LIMIT_CENTS,
        "budget_cents": BUDGET_CENTS,
        "fee_reserve_cents": 4,
        "status": "PLAN_ONLY",
        "real_money_orders": 0,
        "protocol_only": True,
        "motor_m5_validated": False,
    }


def order_body(state):
    return {
        "ticker": TICKER,
        "client_order_id": state["client_order_id"],
        "side": "bid",
        "count": "2",
        "price": "0.31",
        "time_in_force": "immediate_or_cancel",
        "self_trade_prevention_type": "taker_at_cross",
        "subaccount": 0,
    }


def validate_state(state):
    expected = plan()
    for field in expected.keys() - {"status"}:
        if type(state.get(field)) is not type(expected[field]) or state[field] != expected[field]:
            raise ValueError("Diario fuera del contrato")
    coid = state.get("client_order_id", "")
    if not coid.startswith(PREFIX) or str(
        uuid.UUID(coid.removeprefix(PREFIX))
    ) != coid.removeprefix(PREFIX):
        raise ValueError("Identidad de diario inválida")
    if state.get("status") not in {
        "SKIPPED",
        "INTENT_BEFORE_NETWORK",
        "UNCONFIRMED_NO_RETRY",
        "TERMINAL_CONFIRMED",
    }:
        raise ValueError("Estado de diario inválido")
    if type(state.get("create_consumed")) is not bool:
        raise ValueError("Presupuesto de intención inválido")
    if state["status"] != "SKIPPED" and not state["create_consumed"]:
        raise ValueError("Intento sin reserva durable")
    count({"fill_count_fp": state["filled_count"]}, "fill_count")
    if not isinstance(state.get("uncertainty"), list) or any(
        type(v) is not str for v in state["uncertainty"]
    ):
        raise ValueError("Incertidumbre inválida")
    oid = state.get("order_id")
    if oid is not None and (not isinstance(oid, str) or not _ID.fullmatch(oid)):
        raise ValueError("Identidad de orden inválida")


def flag(state, code):
    if code not in state["uncertainty"]:
        state["uncertainty"].append(code)
    state["status"] = "UNCONFIRMED_NO_RETRY"
    state["position_may_remain"] = True


def observe(order, state, *, ack=False):
    """Conserva el máximo fill conocido aunque la evidencia terminal sea inválida."""
    if not isinstance(order, dict):
        flag(state, "invalid_order")
        return False
    oid = order.get("order_id")
    if (
        not isinstance(oid, str)
        or not _ID.fullmatch(oid)
        or state.get("order_id") not in (None, oid)
        or (not ack and order.get("client_order_id") != state["client_order_id"])
        or order.get("client_order_id", state["client_order_id"]) != state["client_order_id"]
        or order.get("ticker", TICKER if ack else None) != TICKER
    ):
        flag(state, "identity_mismatch")
        return False
    state["order_id"] = oid
    try:
        filled = count(order, "fill_count")
        previous = number(state["filled_count"], maximum=Decimal(COUNT), places=2)
        if filled < previous:
            flag(state, "fill_regression")
        state["filled_count"] = format(max(previous, filled), ".2f")
        state["position_may_remain"] = max(previous, filled) > 0 or bool(state["uncertainty"])
        remaining = count(order, "remaining_count")
        if filled + remaining > COUNT:
            raise ValueError("Acumulado inválido")
        if ("initial_count_fp" in order or "initial_count" in order or not ack) and count(
            order, "initial_count"
        ) != COUNT:
            raise ValueError("Cantidad inicial distinta")
        for field, expected in (
            ("outcome_side", "yes"),
            ("book_side", "bid"),
            ("side", "yes"),
            ("action", "buy"),
        ):
            if (
                field in order or (not ack and field in {"outcome_side", "book_side"})
            ) and order.get(field) != expected:
                raise ValueError("Dirección distinta")
        if (not ack or "subaccount_number" in order) and (
            type(order.get("subaccount_number")) is not int or order["subaccount_number"] != 0
        ):
            raise ValueError("Subcuenta distinta")
        if not ack:
            status = order.get("status")
            if not (
                (status in {"canceled", "cancelled"} and remaining == 0)
                or (status == "executed" and filled == COUNT and remaining == 0)
                or (status == "resting" and remaining > 0 and filled + remaining == COUNT)
            ):
                raise ValueError("Estado no verificable")
            state["observed_status"] = status
        state["remaining_count"] = format(remaining, ".2f")
        return True
    except (ValueError, TypeError):
        flag(state, "invalid_counts_or_direction")
        return False


class DemoIOCClient:
    """Una petición por llamada, sin retries ni rutas de venta/batch/redirección."""

    def __init__(self, state, path, *, settings, allow_create, signer=None, transport=None):
        if settings.KALSHI_ENV != "demo" or settings.rest_url != DEMO_ROOT:
            raise PermissionError("Sólo host oficial demo")
        self.settings, self.state, self.path = settings, state, path
        self.allow_create, self.attempted = allow_create, False
        self.base_url = DEMO_ROOT
        self.transport = transport
        self.book_at = None
        if signer is None:
            from src.auth.signer import KalshiSigner

            signer = KalshiSigner(
                private_key_path=settings.KALSHI_PRIVATE_KEY_PATH,
                api_key_id=settings.KALSHI_API_KEY_ID,
            )
        self.signer = signer

    async def __aenter__(self):
        self.http = httpx.AsyncClient(
            base_url=DEMO_ROOT,
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT,
        )
        return self

    async def __aexit__(self, *_):
        await self.http.aclose()

    async def _http_failure(self, response, method):
        operation = {"POST": "create", "DELETE": "cancel", "GET": "read"}[method]
        status = response.status_code
        # La pérdida anterior fue después de recibir un HTTP no2xx, antes de
        # conservar su status. Guardarlo primero aun si el cuerpo no termina.
        self.state[f"{operation}_http_status"] = status
        self.state[f"{operation}_http_error_code"] = None
        save_state(self.path, self.state)
        code = None
        try:
            async with asyncio.timeout(1.0):
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(raw) + len(chunk) > 16384:
                        raise ValueError("Diagnóstico HTTP demasiado grande")
                    raw.extend(chunk)
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    error = payload.get("error")
                    candidate = (
                        error.get("code") if isinstance(error, dict) else payload.get("code")
                    )
                    if isinstance(candidate, str) and candidate in SAFE_HTTP_CODES:
                        code = candidate
        except (ValueError, TimeoutError, httpx.HTTPError):
            # El status observado sigue durable. Ausencia de code no se fabrica.
            code = None
        self.state[f"{operation}_http_error_code"] = code
        save_state(self.path, self.state)
        raise DemoHTTPError(status, code)

    async def request(self, method, path, *, params=None, body=None):
        if (
            self.base_url != DEMO_ROOT
            or self.settings.KALSHI_ENV != "demo"
            or self.settings.rest_url != DEMO_ROOT
            or str(self.http.base_url).rstrip("/") != DEMO_ROOT
        ):
            raise PermissionError("Host de demo alterado")
        params = dict(params or {})
        oid = self.state.get("order_id")
        own_get = f"/portfolio/orders/{oid}" if oid else None
        own_delete = f"/portfolio/events/orders/{oid}" if oid else None
        if method == "GET":
            allowed = {
                "/portfolio/balance",
                "/portfolio/orders",
                "/portfolio/positions",
                "/portfolio/fills",
                f"/markets/{TICKER}",
                f"/markets/{TICKER}/orderbook",
                f"/series/{SERIES}",
                f"/events/{EVENT}",
            }
            if path not in allowed and path != own_get:
                raise PermissionError("GET fuera de alcance")
            if path.startswith("/portfolio/"):
                if params.get("subaccount", 0) != 0:
                    raise PermissionError("Subcuenta distinta")
                params["subaccount"] = 0
        elif method == "POST":
            if (
                not self.allow_create
                or self.attempted
                or self.settings.TRADING_ENABLED is not True
                or path != "/portfolio/events/orders"
                or body != order_body(self.state)
                or type(body.get("subaccount")) is not int
                or params
            ):
                raise PermissionError("POST fuera del presupuesto")
            saved = json.loads(self.path.read_text())
            validate_state(saved)
            if (
                saved != self.state
                or not saved["create_consumed"]
                or saved["status"] != "INTENT_BEFORE_NETWORK"
            ):
                raise PermissionError("Intención no confirmada en disco")
            if self.book_at is None or not 0 <= time.monotonic() - self.book_at <= BOOK_MAX_AGE:
                raise PermissionError("Libro vencido")
            self.attempted = True  # Una respuesta perdida consume el único intento.
        elif method == "DELETE":
            if (
                path != own_delete
                or body is not None
                or params != {"market_ticker": TICKER, "subaccount": 0, "exchange_index": 0}
            ):
                raise PermissionError("Cancelación ajena o fuera de alcance")
        else:
            raise PermissionError("Método fuera de alcance")
        async with asyncio.timeout(REQUEST_TIMEOUT):
            headers = self.signer.sign(method, "/trade-api/v2" + path)
            async with self.http.stream(
                method, path, headers=headers, params=params, json=body
            ) as response:
                if response.status_code not in (200, 201):
                    await self._http_failure(response, method)
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 262144:
                        raise ValueError("Respuesta demasiado grande")
                payload = json.loads(raw)
                if not isinstance(payload, dict) or payload.get("error") is not None:
                    raise ValueError("Respuesta inválida")
                return payload


async def collect(client, path, field, **params):
    rows, seen = [], set()
    for _ in range(5):
        page = await client.request("GET", path, params={"limit": 200, **params})
        batch, cursor = page.get(field), page.get("cursor")
        if (
            not isinstance(batch, list)
            or len(batch) > 200
            or any(not isinstance(row, dict) for row in batch)
            or not isinstance(cursor, str)
            or len(cursor) > 2048
        ):
            raise ValueError("Paginación no verificable")
        rows.extend(batch)
        if not cursor:
            return rows
        if cursor in seen:
            raise ValueError("Cursor repetido")
        seen.add(cursor)
        params["cursor"] = cursor
    raise ValueError("Paginación incompleta")


async def positions(client):
    rows = await collect(client, "/portfolio/positions", "market_positions")
    result = {}
    for row in rows:
        value = number(row.get("position_fp", row.get("position")), signed=True, places=2)
        if (
            row.get("position") is not None
            and number(row["position"], signed=True, places=2) != value
        ):
            raise ValueError("Posición contradictoria")
        if value:
            ticker = row.get("ticker")
            if not isinstance(ticker, str) or ticker in result:
                raise ValueError("Posición sin identidad única")
            result[ticker] = value
    return result


def available_ask_quantity(payload):
    """Suma todo NO bid >=69c; su complemento es YES ask <=31c."""
    fp = payload.get("orderbook_fp")
    book = fp if isinstance(fp, dict) else payload.get("orderbook")
    if not isinstance(book, dict):
        raise ValueError("Libro ausente")
    key = "no_dollars" if "no_dollars" in book else "no"
    levels = book.get(key)
    if not isinstance(levels, list) or len(levels) > 1000:
        raise ValueError("Lado NO ausente")
    total, seen = Decimal(0), set()
    for level in levels:
        if not isinstance(level, list) or len(level) != 2:
            raise ValueError("Nivel inválido")
        price = number(
            level[0],
            maximum=Decimal(1 if key.endswith("dollars") else 100),
            places=4 if key.endswith("dollars") else 0,
        )
        if key.endswith("dollars"):
            price *= 100
        quantity = number(level[1], places=2)
        if price <= 0 or price >= 100 or price in seen:
            raise ValueError("Precio inválido o duplicado")
        seen.add(price)
        if 100 - price <= LIMIT_CENTS:
            total += quantity
    return total


async def preflight(client, state):
    from src.math.balance import available_cash_usd
    from src.math.fees import kalshi_fee_cents

    if await collect(client, "/portfolio/orders", "orders", status="resting") or await positions(
        client
    ):
        return "account_not_empty"
    balance = available_cash_usd(await client.request("GET", "/portfolio/balance"))
    state["balance_before_usd"] = str(balance)
    if balance * 100 < BUDGET_CENTS:
        return "insufficient_demo_balance"
    market = (await client.request("GET", f"/markets/{TICKER}")).get("market", {})
    if (
        market.get("ticker") != TICKER
        or market.get("event_ticker") != EVENT
        or market.get("status") not in {"open", "active"}
    ):
        return "market_unavailable"
    series = (await client.request("GET", f"/series/{SERIES}")).get("series", {})
    event = (await client.request("GET", f"/events/{EVENT}")).get("event", {})
    if (
        series.get("ticker") != SERIES
        or event.get("event_ticker") != EVENT
        or event.get("series_ticker") != SERIES
    ):
        return "fee_identity_unknown"
    fee_type, multiplier = series.get("fee_type"), series.get("fee_multiplier")
    override = (event.get("fee_type_override"), event.get("fee_multiplier_override"))
    if override != (None, None):
        fee_type, multiplier = override
    if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
        return "fee_type_unknown"
    # JSON puede expresar el multiplier como float; conversión explícita sólo aquí.
    if isinstance(multiplier, bool) or not isinstance(multiplier, (str, int, float)):
        return "fee_multiplier_unknown"
    multiplier = number(str(multiplier), maximum=Decimal(1))
    fee = kalshi_fee_cents(COUNT, LIMIT_CENTS, fee_multiplier=str(multiplier))
    if fee > 4 or COUNT * LIMIT_CENTS + fee > BUDGET_CENTS:
        return "fee_budget_exceeded"
    state["fee_multiplier"] = str(multiplier)
    state["fee_upper_cents"] = fee
    # El libro se lee al final. Toda cantidad accesible debe caber estrictamente
    # entre0 y2; si aparecen2 o más ya no es el ensayo parcial solicitado.
    started = time.monotonic()
    book = await client.request("GET", f"/markets/{TICKER}/orderbook", params={"depth": 100})
    client.book_at = started
    quantity = available_ask_quantity(book)
    state["observed_ask_quantity_at_limit"] = str(quantity)
    if not 0 < quantity < COUNT:
        return "partial_liquidity_unavailable"
    if time.monotonic() - started > BOOK_MAX_AGE:
        return "book_stale"
    return None


async def reconcile(client, state, path):
    if state.get("order_id"):
        response = await client.request("GET", f"/portfolio/orders/{state['order_id']}")
        candidates = [response.get("order")]
    else:
        orders = await collect(client, "/portfolio/orders", "orders", ticker=TICKER)
        candidates = [
            row for row in orders if row.get("client_order_id") == state["client_order_id"]
        ]
    valid = False
    for order in candidates:
        valid = observe(order, state) and len(candidates) == 1
    if len(candidates) != 1:
        # Ausencia no prueba cero y puede resolverse en otro --recover.
        state["status"] = "UNCONFIRMED_NO_RETRY"
        state["position_may_remain"] = True
        state["pending_reason"] = (
            "ORDER_NOT_OBSERVED" if not candidates else "ORDER_IDENTITY_AMBIGUOUS"
        )
        if candidates:
            flag(state, "duplicate_order")
    save_state(path, state)
    return candidates[0] if valid else None


async def execution_costs(client, state):
    """Fills oficiales: fee_cost es TOTAL USD por fill, nunca fee por contrato.

    Ver src/analytics/portfolio_report.py y contrato GET /portfolio/fills.
    No convierte un coste ambiguo de SELL; esta sonda sólo compra YES.
    """
    if not state.get("order_id"):
        raise ValueError("Orden desconocida para conciliar fills")
    rows = await collect(client, "/portfolio/fills", "fills", order_id=state["order_id"])
    quantity, gross, fees, ids = Decimal(0), Decimal(0), Decimal(0), set()
    for row in rows:
        identity = row.get("fill_id")
        if not isinstance(identity, str) or not _ID.fullmatch(identity) or identity in ids:
            raise ValueError("Fill sin identidad única")
        ids.add(identity)
        if (
            row.get("order_id") != state["order_id"]
            or row.get("ticker", row.get("market_ticker")) != TICKER
            or row.get("market_ticker", TICKER) != TICKER
        ):
            raise ValueError("Fill ajeno")
        if (
            row.get("outcome_side") != "yes"
            or row.get("book_side") != "bid"
            or row.get("side", "yes") != "yes"
            or row.get("action", "buy") != "buy"
            or type(row.get("subaccount_number")) is not int
            or row["subaccount_number"] != 0
        ):
            raise ValueError("Fill de dirección/subcuenta distinta")
        size = count(row, "count")
        price = number(row.get("yes_price_dollars"), maximum=Decimal("0.31"), places=4)
        fee = number(row.get("fee_cost"), maximum=Decimal("0.04"))
        if size <= 0 or price <= 0:
            raise ValueError("Fill no positivo")
        quantity += size
        gross += size * price
        fees += fee
    if quantity > COUNT or gross + fees > Decimal("0.66") or fees > Decimal("0.04"):
        raise ValueError("Coste o cantidad excede presupuesto")
    return quantity, gross, fees, len(ids)


async def _locked(args, settings, factory):
    path = args.state
    if path.exists():
        if not args.recover or path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError("Diario existente: sólo --recover")
        state = json.loads(path.read_text())
        validate_state(state)
        if not state["create_consumed"]:
            return state
    elif args.recover:
        raise ValueError("Diario ausente")
    else:
        state = {
            **plan(),
            "client_order_id": PREFIX + str(uuid.uuid4()),
            "status": "SKIPPED",
            "create_consumed": False,
            "filled_count": "0.00",
            "uncertainty": [],
            "position_may_remain": False,
        }
    # Un nuevo intento de lectura debe acreditar de nuevo ambas afirmaciones;
    # un fallo posterior no hereda un certificado de una recuperación anterior.
    state.update(
        account_reconciled=False,
        terminal_confirmed=False,
        partial_fill_observed=False,
        filled_count_is_floor=True,
        pending_reason="RECOVERY_EVIDENCE_PENDING",
        last_run_error_type=None,
    )
    try:
        async with (
            asyncio.timeout(TOTAL_TIMEOUT),
            factory(state, path, settings=settings, allow_create=not args.recover) as client,
        ):
            if not args.recover:
                reason = await preflight(client, state)
                if reason:
                    state["skip_reason"] = reason
                    save_state(path, state)
                    return state
                state.update(
                    status="INTENT_BEFORE_NETWORK", create_consumed=True, position_may_remain=True
                )
                save_state(path, state)
                try:
                    response = await client.request(
                        "POST", "/portfolio/events/orders", body=order_body(state)
                    )
                    observe(response.get("order", response), state, ack=True)
                except Exception as exc:
                    state["create_error_type"] = type(exc).__name__
                state["status"] = "UNCONFIRMED_NO_RETRY"
                save_state(path, state)
            order = await reconcile(client, state, path)
            if order is not None and count(order, "remaining_count") > 0:
                if type(order.get("exchange_index")) is not int or order["exchange_index"] != 0:
                    flag(state, "cancel_route_unknown")
                else:
                    try:
                        await client.request(
                            "DELETE",
                            f"/portfolio/events/orders/{state['order_id']}",
                            params={"market_ticker": TICKER, "subaccount": 0, "exchange_index": 0},
                        )
                    except Exception as exc:
                        state["cancel_error_type"] = type(exc).__name__
                    # DELETE ack nunca acredita fill cero o cierre; GET posterior sí.
                    order = await reconcile(client, state, path)
            from src.math.balance import available_cash_usd

            # Terminal de orden y conciliación contable son pruebas separadas.
            terminal = (
                order is not None
                and order.get("status") in {"executed", "canceled", "cancelled"}
                and count(order, "remaining_count") == 0
            )
            state["terminal_confirmed"] = terminal and not state["uncertainty"]
            actual = await positions(client)
            after = available_cash_usd(await client.request("GET", "/portfolio/balance"))
            state["balance_after_usd"] = str(after)
            state["observed_positions"] = {ticker: str(qty) for ticker, qty in actual.items()}
            filled = number(state["filled_count"], maximum=Decimal(COUNT), places=2)
            expected = {TICKER: filled} if filled else {}
            debit = number(state["balance_before_usd"]) - after
            state["balance_debit_usd"] = str(debit)
            if not state.get("order_id"):
                # 0.00 es sólo el piso del diario. La ausencia no permite consultar
                # fills por una identidad supuesta ni acreditar orden rechazada.
                state["pending_reason"] = "ORDER_NOT_OBSERVED_AFTER_CONSUMED_CREATE"
                state["position_may_remain"] = True
                state["status"] = "UNCONFIRMED_NO_RETRY"
                save_state(path, state)
                return state
            quantity, gross, fees, fill_records = await execution_costs(client, state)
            state.update(
                fill_records=fill_records,
                fills_quantity=str(quantity),
                fills_gross_usd=str(gross),
                fills_fees_usd=str(fees),
            )
            state["account_reconciled"] = (
                actual == expected
                and quantity == filled
                and debit == gross + fees
                and 0 <= debit * 100 <= BUDGET_CENTS
            )
            state["partial_fill_observed"] = state["terminal_confirmed"] and 0 < filled < COUNT
            state["filled_count_is_floor"] = not state["terminal_confirmed"]
            state["position_may_remain"] = (
                bool(filled) or not terminal or bool(state["uncertainty"])
            )
            state["status"] = (
                "TERMINAL_CONFIRMED"
                if terminal and state["account_reconciled"] and not state["uncertainty"]
                else "UNCONFIRMED_NO_RETRY"
            )
            state["pending_reason"] = (
                None
                if state["status"] == "TERMINAL_CONFIRMED"
                else "ORDER_EVIDENCE_INCONSISTENT"
                if state["uncertainty"]
                else "ORDER_NOT_TERMINAL"
                if not terminal
                else "ACCOUNT_EVIDENCE_INCOMPLETE"
            )
    except Exception as exc:
        state["error_type"] = type(exc).__name__
        state["last_run_error_type"] = type(exc).__name__
        state["status"] = "UNCONFIRMED_NO_RETRY" if state["create_consumed"] else "SKIPPED"
    save_state(path, state)
    return state


async def run_probe(args, *, settings=None, factory=DemoIOCClient):
    if args.execute and args.recover:
        raise ValueError("Modos excluyentes")
    if not args.execute and not args.recover:
        return plan()  # Sin claves, imports del bot, archivos ni red.
    if args.state.is_symlink():
        raise ValueError("Diario symlink prohibido")
    args.state = args.state.absolute()
    args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(args.state) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return await _locked(args, settings, factory)


def isolated_demo_settings(execute):
    """Claves sólo de la ruta DEMO tras borrar entorno, nunca dotenv/producción."""
    credentials = Path.home() / ".config/botkalshi/demo"
    os.environ.clear()
    from src.utils.config import Settings, reset_settings_for_testing

    Settings.model_config["env_file"] = None
    reset_settings_for_testing()
    paths = [credentials / "key_id.txt", credentials / "kalshi_private_key.pem"]
    for path in paths:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("Clave demo requiere archivo privado del usuario")
    key_id = paths[0].read_text().strip()
    if not _ID.fullmatch(key_id):
        raise ValueError("Identificador demo inválido")
    return Settings(
        _env_file=None,
        KALSHI_ENV="demo",
        KALSHI_API_KEY_ID=key_id,
        KALSHI_PRIVATE_KEY_PATH=str(paths[1]),
        KALSHI_DEMO_REST_URL=DEMO_ROOT,
        TRADING_ENABLED=bool(execute),
        DATABASE_URL="sqlite:///:memory:",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--execute", action="store_true")
    modes.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    try:
        if args.execute or args.recover:
            from loguru import logger

            logger.remove()
            settings = isolated_demo_settings(args.execute)
        else:
            settings = None
        result = asyncio.run(run_probe(args, settings=settings))
    except Exception as exc:
        # Nunca imprimir repr(exc), requests, response bodies, headers ni claves.
        result = {"status": "BLOCKED", "error_type": type(exc).__name__, "real_money_orders": 0}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"PLAN_ONLY", "SKIPPED", "TERMINAL_CONFIRMED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
