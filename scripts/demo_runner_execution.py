"""Ensayo de mecánica M5 en DEMO, una intención y hasta 30 centavos ficticios.

Sin --execute/--recover sólo imprime el plan; no lee credenciales ni abre red.
Ejemplo: python scripts/demo_runner_execution.py --workspace /ruta/privada/canary
  --ticker KXHIGHTOKC-26OCT08-B88.5 --price-cents 1 --execute
La recuperación usa la MISMA carpeta y --recover en un proceso nuevo. Nunca crea
otra intención ni liquida fills. QuoteSet es entrada explícita del operador:
prueba gates del runner, executor, Trade y reconciliador, NO fair/alpha ni M2.
Un parcial sólo existe si una contraparte externa lo ejecuta. No fuerza fills.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DEMO_ROOT = "https://external-api.demo.kalshi.co/trade-api/v2"
TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
SCHEMA = "demo-runner-execution-v1"
MAX_HTTP = 80
CAP_CENTS = 30
ERROR_TYPES = {
    "ValueError",
    "PermissionError",
    "TimeoutError",
    "ReadTimeout",
    "ConnectError",
    "KalshiAPIError",
    "KalshiOrderConfirmationError",
    "Motor5DataIntegrityError",
    "ValidationError",
    "FileNotFoundError",
    "OSError",
}


def record_error(state, phase, exc):
    kind = type(exc).__name__
    state.setdefault("errors", []).append(
        {"phase": phase, "type": kind if kind in ERROR_TYPES else "OtherError"}
    )


def save(path: Path, value: dict) -> None:
    """Publicación atómica y durable; no contiene claves ni cuerpos de respuesta."""
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def number(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("INVALID_NUMBER")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("INVALID_NUMBER")
    return result


def plan(args) -> dict:
    if not TOKEN.fullmatch(args.ticker) or not 1 <= args.price_cents <= 29:
        raise ValueError("INVALID_INTENT")
    if not 0 <= args.hold_seconds <= 30:
        raise ValueError("INVALID_HOLD")
    return {
        "schema": SCHEMA,
        "environment": "demo",
        "ticker": args.ticker,
        "price_cents": args.price_cents,
        "contracts": 1,
        "cap_cents": CAP_CENTS,
        "post_only": True,
        "time_in_force": "good_till_canceled",
        "side": "buy_yes",
        "status": "DRY_PLAN",
        "post_attempts": 0,
        "real_money_orders": 0,
        "scope": "runner_gate_executor_reconciler_manual_quote_no_alpha",
        "config_profile": "manual_demo_override_not_F1",
        "autonomous_profile_boot_validated": False,
    }


def configure(workspace: Path, credentials: Path):
    """Credenciales demo explícitas, sin .env ni configuración heredada."""
    from scripts.demo_runner_readiness import configure as read_only_configure
    from src.utils import config

    base = read_only_configure(workspace, credentials, 8087)
    config._settings = manual_demo_settings(base, workspace)
    return config._settings


def manual_demo_settings(base, workspace):
    """Excepción local autorizada: NO acredita el validador del perfil autónomo F1."""
    if (
        base.KALSHI_ENV != "demo"
        or base.rest_url != DEMO_ROOT
        or base.TRADING_ENABLED
        or base.MOTOR_MM_ENABLED
        or base.MOTOR_MM_EXECUTION_ENABLED
    ):
        raise PermissionError("MANUAL_PROFILE_REQUIRES_DEMO_DISABLED_BASE")
    return base.model_copy(
        update={
            "DATABASE_URL": f"sqlite:///{workspace / 'ledger.db'}",
            "TRADING_ENABLED": True,
            "MOTOR_1_ARBITRAGE_ENABLED": False,
            "MOTOR_MM_ENABLED": True,
            "MOTOR_MM_EXECUTION_ENABLED": True,
            "MOTOR_MM_SERIES": "KXHIGHTOKC",
            "MOTOR_MM_QUOTE_SIZE_CONTRACTS": 1,
            "MOTOR_MM_MAX_TICKERS": 1,
            "MOTOR_MM_MAX_EXPOSURE_USD": 0.29,
            "MOTOR_MM_REQUIRE_PREGAME": False,
            "MOTOR_MM_FEES_AS_MAKER": False,
            "MOTOR_MM_EXPERIMENT_ID": "demo-manual-execution-canary",
            "ACTIVE_CAPITAL_USD": 200.0,
            "CAPITAL_CAP_USD": 200.0,
            "CAPITAL_FLOOR_USD": 180.0,
            "UNREALIZED_STOP_ENABLED": False,
        }
    )


class Journal:
    def __init__(self, path: Path, state: dict, *, recover: bool):
        self.path, self.state, self.recover = path, state, recover
        self.http = 0
        self.owned: set[str] = set()  # Se reconstruye del ledger/API, nunca del JSON solo.

    def persist(self):
        self.state["order_ids"] = sorted(self.owned)
        save(self.path, self.state)

    def learn(self, order):
        """Sólo identifica cancelaciones propias; cantidades las valida el conciliador."""
        if not isinstance(order, dict):
            return
        if (
            order.get("client_order_id") == self.state.get("client_order_id")
            and self.state.get("client_order_id")
            and order.get("ticker") == self.state["ticker"]
            and type(order.get("subaccount_number")) is int
            and order["subaccount_number"] == 0
            and order.get("side", order.get("outcome_side")) == "yes"
            and order.get("action", "buy" if order.get("book_side") == "bid" else None) == "buy"
            and order.get("outcome_side", "yes") == "yes"
            and order.get("book_side", "bid") == "bid"
        ):
            oid = order.get("order_id")
            if isinstance(oid, str) and TOKEN.fullmatch(oid):
                if self.owned and oid not in self.owned:
                    raise ValueError("MULTIPLE_ORDER_IDENTITIES")
                self.owned.add(oid)
                self.persist()


def client_factory(journal: Journal, *, transport=None):
    """Cliente real de órdenes, con frontera HTTP demo sin retries ni redirects."""
    import httpx
    from sqlmodel import select

    from src.clients.kalshi_rest import KalshiAPIError, KalshiRestClient
    from src.storage.models import Trade, get_session
    from src.storage.quantities import confirmed_fill, requested_quantity

    class ScopedClient(KalshiRestClient):
        async def __aenter__(self):
            if self.settings.KALSHI_ENV != "demo" or self.base_url != DEMO_ROOT:
                raise PermissionError("DEMO_HOST_REQUIRED")
            self._client = httpx.AsyncClient(
                base_url=DEMO_ROOT,
                transport=transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(10, connect=5),
                headers={"Accept-Encoding": "identity"},
            )
            return self

        async def _request(self, method, path, **kwargs):
            state = journal.state
            if (
                self.base_url != DEMO_ROOT
                or self.settings.rest_url != DEMO_ROOT
                or str(self._client.base_url).rstrip("/") != DEMO_ROOT
            ):
                raise PermissionError("DEMO_HOST_REQUIRED")
            reads = {
                "/portfolio/balance",
                "/portfolio/orders",
                "/portfolio/positions",
                "/portfolio/fills",
                f"/markets/{state['ticker']}",
                f"/markets/{state['ticker']}/orderbook",
            }
            reads |= {f"/portfolio/orders/{oid}" for oid in journal.owned}
            if state.get("event_ticker"):
                reads.add(f"/events/{state['event_ticker']}")
            if state.get("series_ticker"):
                reads.add(f"/series/{state['series_ticker']}")
            if journal.http >= MAX_HTTP:
                raise PermissionError("HTTP_BUDGET_EXHAUSTED")
            if method == "GET" and path in reads:
                if path.startswith("/portfolio/") and path.count("/") == 2:
                    kwargs["params"] = {**(kwargs.get("params") or {}), "subaccount": 0}
            elif method == "POST" and path == "/portfolio/events/orders":
                body = kwargs.get("json") or {}
                with get_session() as session:
                    rows = list(session.exec(select(Trade)))
                    durable = (
                        len(rows) == 1
                        and rows[0].status == "pending"
                        and rows[0].strategy == "motor_5_mm"
                        and rows[0].ticker == state["ticker"]
                        and rows[0].action == "buy"
                        and rows[0].side == "yes"
                        and requested_quantity(rows[0]) == 1
                        and confirmed_fill(rows[0]) in (None, 0)
                        and rows[0].kalshi_order_id is None
                        and rows[0].price_cents == state["price_cents"]
                        and rows[0].client_order_id == body.get("client_order_id")
                    )
                expected = {
                    "ticker": state["ticker"],
                    "client_order_id": body.get("client_order_id"),
                    "count": "1",
                    "side": "bid",
                    "price": f"{state['price_cents'] / 100:.2f}",
                    "time_in_force": "good_till_canceled",
                    "post_only": True,
                    "self_trade_prevention_type": "taker_at_cross",
                }
                if (
                    journal.recover
                    or state["post_attempts"] != 0
                    or not durable
                    or not state.get("preflight_ok")
                    or set(kwargs) != {"json"}
                    or body != expected
                    or body.get("post_only") is not True
                ):
                    raise PermissionError("CREATE_BUDGET_OR_DURABILITY_DENIED")
                # El derecho único se consume ANTES de red; tras crash no se repone.
                state.update(
                    post_attempts=1,
                    client_order_id=body["client_order_id"],
                    status="POST_ATTEMPT_DURABLE",
                    intent_committed_before_post=True,
                )
                journal.persist()
            elif method == "DELETE":
                if path == "/portfolio/events/orders/batched":
                    items = (kwargs.get("json") or {}).get("orders", [])
                    if (
                        len(items) != 1
                        or items[0].get("order_id") not in journal.owned
                        or items[0].get("subaccount") != 0
                        or items[0].get("market_ticker") != state["ticker"]
                    ):
                        raise PermissionError("CANCEL_NOT_OWNED")
                else:
                    oid = path.removeprefix("/portfolio/events/orders/")
                    if (
                        path != f"/portfolio/events/orders/{oid}"
                        or oid not in journal.owned
                        or (kwargs.get("params") or {}).get("subaccount") != 0
                        or (kwargs.get("params") or {}).get("market_ticker") != state["ticker"]
                    ):
                        raise PermissionError("CANCEL_NOT_OWNED")
            else:
                raise PermissionError("HTTP_ROUTE_DENIED")
            journal.http += 1
            headers = self.signer.sign(method, self._build_sign_path(path))
            async with asyncio.timeout(15):
                async with self._client.stream(method, path, headers=headers, **kwargs) as response:
                    if response.status_code not in (200, 201):
                        # Sin eco de respuesta ni excepción HTTP que incluya headers/URL.
                        raise KalshiAPIError(response.status_code, "DEMO_HTTP_ERROR")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 1024 * 1024:
                            raise ValueError("RESPONSE_TOO_LARGE")
                    data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("INVALID_RESPONSE")
            journal.learn(data.get("order", data))
            for order in data.get("orders", []):
                journal.learn(order.get("order", order) if isinstance(order, dict) else order)
            return data

    return ScopedClient


async def positions(client) -> list:
    result, cursor, seen = [], None, set()
    for _ in range(5):
        page = await client.get_positions(limit=100, cursor=cursor, subaccount=0)
        rows = page.get("market_positions")
        if not isinstance(rows, list):
            raise ValueError("POSITIONS_UNKNOWN")
        result.extend(rows)
        cursor = page.get("cursor")
        if cursor == "":
            return result
        if not isinstance(cursor, str) or cursor in seen or len(cursor) > 4096:
            raise ValueError("POSITIONS_CURSOR_INVALID")
        seen.add(cursor)
    raise ValueError("POSITIONS_LIMIT")


def provider_decimal(raw, *, places=6, signed=False, maximum=Decimal("1000000000")):
    """Decimal del proveedor sin float, redondeo, tolerancia ni coerción de bool."""
    if isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("PROVIDER_NUMBER_INVALID")
    value = str(raw)
    if len(value) > 32 or not re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("PROVIDER_NUMBER_INVALID")
    result = Decimal(value)
    if result.as_tuple().exponent < -places or abs(result) > maximum or (result < 0 and not signed):
        raise ValueError("PROVIDER_PRECISION_OR_RANGE_INVALID")
    return result


def provider_quantity(row, field, *, signed=False):
    """El helper core detecta contradicción legacy/_fp y conserva centésimas."""
    from src.math.quantities import api_count, signed_fixed_count

    for name in (field, field + "_fp"):
        if name in row and row[name] is not None:
            provider_decimal(row[name], places=2, signed=signed)
    value = api_count(row, field, signed=signed)
    if value is None:
        raise ValueError("PROVIDER_QUANTITY_UNKNOWN")
    return Decimal(signed_fixed_count(value))


async def own_fills(client, state) -> dict:
    """Suma cantidad, importe BUY y fee_cost TOTAL de cada fill oficial propio."""
    total, gross, fees = Decimal(0), Decimal(0), Decimal(0)
    cursor, seen, identities = None, set(), set()
    for _ in range(5):
        page = await client.get_fills(
            limit=100,
            ticker=state["ticker"],
            cursor=cursor,
            min_ts=int(state["created_at_unix"]),
            subaccount=0,
        )
        if not isinstance(page.get("fills"), list):
            raise ValueError("FILLS_UNKNOWN")
        for fill in page["fills"]:
            if not isinstance(fill, dict):
                raise ValueError("FILLS_UNKNOWN")
            if fill.get("order_id") not in state.get("order_ids", []):
                continue
            identity = fill.get("fill_id", fill.get("trade_id"))
            if (
                not isinstance(identity, str)
                or not TOKEN.fullmatch(identity)
                or identity in identities
                or fill.get("ticker", fill.get("market_ticker")) != state["ticker"]
                or fill.get("market_ticker", state["ticker"]) != state["ticker"]
                or fill.get("client_order_id", state.get("client_order_id"))
                != state.get("client_order_id")
                or fill.get("outcome_side") != "yes"
                or fill.get("book_side") != "bid"
                or fill.get("side", "yes") != "yes"
                or fill.get("action", "buy") != "buy"
                or type(fill.get("subaccount_number")) is not int
                or fill["subaccount_number"] != 0
                or fill.get("is_taker") is not False
            ):
                raise ValueError("FILLS_IDENTITY_UNKNOWN")
            identities.add(identity)
            count = provider_quantity(fill, "count")
            if count <= 0 or count > 1:
                raise ValueError("FILLS_COUNT_UNKNOWN")
            price = provider_decimal(fill.get("yes_price_dollars"), places=4, maximum=Decimal(1))
            if not 0 < price <= Decimal(state["price_cents"]) / 100:
                raise ValueError("FILL_PRICE_OUTSIDE_LIMIT")
            if fill.get("yes_price") is not None:
                legacy_price = provider_decimal(fill["yes_price"], places=0, maximum=Decimal(100))
                if legacy_price / 100 != price:
                    raise ValueError("FILL_PRICE_CONTRADICTORY")
            # fee_cost ya es la comisión del fill; multiplicarla por count sería otro coste.
            fee = provider_decimal(fill.get("fee_cost"), maximum=Decimal("0.01"))
            total += count
            gross += count * price
            fees += fee
        cursor = page.get("cursor")
        if cursor == "":
            if (
                total > 1
                or fees * 100 > state["fee_cap_cents"]
                or (gross + fees) * 100 > state["budget_cents"]
            ):
                raise ValueError("FILLS_BUDGET_EXCEEDED")
            return {
                "quantity": str(total),
                "gross_usd": str(gross),
                "fees_usd": str(fees),
                "records": len(identities),
            }
        if not isinstance(cursor, str) or not cursor or cursor in seen or len(cursor) > 4096:
            raise ValueError("FILLS_CURSOR_INVALID")
        seen.add(cursor)
    raise ValueError("FILLS_LIMIT")


def signed_positions(rows) -> dict:
    result, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("POSITION_UNKNOWN")
        ticker = row.get("ticker")
        if (
            not isinstance(ticker, str)
            or not TOKEN.fullmatch(ticker)
            or ticker in seen
            or (
                "subaccount_number" in row
                and (type(row["subaccount_number"]) is not int or row["subaccount_number"] != 0)
            )
        ):
            raise ValueError("POSITION_IDENTITY_UNKNOWN")
        seen.add(ticker)
        value = provider_quantity(row, "position", signed=True)
        if value:
            result[ticker] = str(value)
    return result


def reconcile_account(state, row, fills, positions_after) -> dict:
    """Certifica sólo igualdades exactas; un saldo menos preciso nunca se redondea a favor."""
    filled = provider_decimal(row["filled_count"], places=2, maximum=Decimal(1))
    quantity = provider_decimal(fills["quantity"], places=2, maximum=Decimal(1))
    gross, fees = (
        provider_decimal(fills[field], maximum=Decimal("0.30"))
        for field in ("gross_usd", "fees_usd")
    )
    expected_positions = {state["ticker"]: filled} if filled else {}
    actual_positions = {
        ticker: provider_decimal(value, places=2, signed=True)
        for ticker, value in positions_after.items()
    }
    debit = provider_decimal(state["cash_before_usd"], places=4) - provider_decimal(
        state["cash_after_usd"], places=4
    )
    # El dinero del ledger es confirmado, nunca estimación de precio límite/tarifa.
    ledger_costs = True
    for field, observed in (("fill_cost_usd_fp", gross), ("fees_usd_fp", fees)):
        recorded = row[field]
        if recorded is None:
            ledger_costs = ledger_costs and filled == 0
        else:
            ledger_costs = ledger_costs and provider_decimal(recorded) == observed
    matched = (
        quantity == filled
        and actual_positions == expected_positions
        and debit == gross + fees
        and ledger_costs
        and 0 <= debit * 100 <= state["budget_cents"]
        and fees * 100 <= state["fee_cap_cents"]
    )
    return {
        "account_reconciled": matched,
        "ledger_costs_reconciled": ledger_costs,
        "cash_debit_usd": str(debit),
        "account_comparison": "exact_no_rounding",
    }


async def preflight(client, journal: Journal):
    """Sin estrategias ficticias: cuenta plana, book y fees actuales verificables."""
    from src.clients.kalshi_rest import available_cash_usd
    from src.math.fees import kalshi_maker_fee_cents
    from src.strategies.data_capture import rest_orderbook_sides

    state = journal.state
    state["cash_before_usd"] = str(available_cash_usd(await client.get_balance(subaccount=0)))
    if await client.get_all_orders(status="resting"):
        raise ValueError("ACCOUNT_HAS_RESTING")
    if signed_positions(await positions(client)):
        raise ValueError("ACCOUNT_NOT_FLAT")
    market = (await client.get_market(state["ticker"])).get("market", {})
    event = market.get("event_ticker")
    if (
        market.get("ticker") != state["ticker"]
        or market.get("status") not in {"open", "active"}
        or not isinstance(event, str)
        or not TOKEN.fullmatch(event)
    ):
        raise ValueError("MARKET_NOT_ELIGIBLE")
    state["event_ticker"] = event
    event_data = (await client.get_event(event)).get("event", {})
    series = event_data.get("series_ticker")
    if (
        event_data.get("event_ticker") != event
        or not isinstance(series, str)
        or not TOKEN.fullmatch(series)
    ):
        raise ValueError("EVENT_IDENTITY_UNKNOWN")
    state["series_ticker"] = series
    schedule = (await client.get_series(series)).get("series", {})
    if schedule.get("ticker") != series:
        raise ValueError("SERIES_IDENTITY_UNKNOWN")
    fee_type, multiplier = schedule.get("fee_type"), schedule.get("fee_multiplier")
    override = event_data.get("fee_type_override"), event_data.get("fee_multiplier_override")
    if override != (None, None):
        if None in override:
            raise ValueError("PARTIAL_FEE_OVERRIDE")
        fee_type, multiplier = override
    if (
        fee_type not in {"quadratic", "quadratic_with_maker_fees"}
        or not 0 <= number(multiplier) <= 1
    ):
        raise ValueError("FEE_SCHEDULE_UNSUPPORTED")
    # Una comisión maker al multiplicador 1 es cota aun cuando el schedule sea gratis.
    fee_cap = kalshi_maker_fee_cents(1, state["price_cents"], fee_multiplier=1)
    if state["price_cents"] + fee_cap > CAP_CENTS:
        raise ValueError("TOTAL_BUDGET_EXCEEDED")
    book = rest_orderbook_sides(await client.get_orderbook(state["ticker"]))

    def best(levels):
        valid = []
        for price, count in levels:
            p = number(price) / 100 if type(price) is int else number(price)
            if not 0 < p < 1 or number(count) <= 0:
                raise ValueError("INVALID_BOOK")
            valid.append(p)
        if not valid:
            raise ValueError("EMPTY_BOOK")
        return max(valid)

    bid, ask = best(book["yes"]), 1 - best(book["no"])
    if bid >= ask or Decimal(state["price_cents"]) / 100 >= ask:
        raise ValueError("QUOTE_WOULD_CROSS")
    state.update(
        preflight_ok=True,
        fee_cap_cents=fee_cap,
        budget_cents=state["price_cents"] + fee_cap,
        external_bid_usd=str(bid),
        external_ask_usd=str(ask),
    )
    journal.persist()


def ledger_snapshot() -> dict:
    from sqlmodel import select

    from src.math.quantities import fixed_count
    from src.risk.manager import RiskManager
    from src.storage.models import Trade, get_session
    from src.storage.quantities import confirmed_fill
    from src.strategies.motor_5_mm.incidents import incident_status

    with get_session() as session:
        rows = list(session.exec(select(Trade)))
        if len(rows) > 1:
            raise ValueError("MULTIPLE_INTENTS")
        result = [
            {
                "status": row.status,
                "client_order_id": row.client_order_id,
                "order_id": row.kalshi_order_id,
                "filled_count": (
                    fixed_count(confirmed_fill(row)) if confirmed_fill(row) is not None else None
                ),
                "fill_cost_usd_fp": row.fill_cost_usd_fp,
                "fees_usd_fp": row.fees_usd_fp,
            }
            for row in rows
        ]
    return {
        "trades": result,
        "exposure_usd": RiskManager()._get_current_exposure_usd(),
        "incident_blocked": incident_status()[0],
    }


def restore_ownership(journal):
    """El diario limita intentos; la intención DB acredita identidad al recuperar."""
    from sqlmodel import select

    from src.storage.models import Trade, get_session
    from src.storage.quantities import requested_quantity

    state = journal.state
    with get_session() as session:
        rows = list(session.exec(select(Trade)))
        if not journal.recover:
            if rows or state["post_attempts"]:
                raise PermissionError("EXISTING_INTENT_USE_RECOVER")
            return
        if len(rows) > 1 or (not rows and state["post_attempts"]):
            raise ValueError("RECOVERY_LEDGER_MISMATCH")
        if rows:
            row = rows[0]
            if (
                row.ticker != state["ticker"]
                or row.side != "yes"
                or row.action != "buy"
                or row.strategy != "motor_5_mm"
                or requested_quantity(row) != 1
                or row.price_cents != state["price_cents"]
                or state.get("client_order_id", row.client_order_id) != row.client_order_id
            ):
                raise ValueError("RECOVERY_IDENTITY_MISMATCH")
            state["client_order_id"] = row.client_order_id
            # Un crash antes de copiar el ID a SQLite exige reaprenderlo por GET.
            if row.kalshi_order_id:
                if not TOKEN.fullmatch(row.kalshi_order_id):
                    raise ValueError("RECOVERY_ORDER_ID_INVALID")
                cached = state.get("order_ids", [])
                if cached and cached != [row.kalshi_order_id]:
                    raise ValueError("RECOVERY_ORDER_ID_MISMATCH")
                journal.owned.add(row.kalshi_order_id)
    journal.persist()


async def run_session(args, journal: Journal, *, transport=None) -> dict:
    """Runner gate → Engine.run → executor/conciliador reales; tick manual acotado."""
    from src.risk.manager import RiskManager
    from src.runner import ProductionRunner
    from src.strategies.motor_5_mm.engine import Motor5Engine
    from src.strategies.motor_5_mm.quoter import QuoteSet

    factory = client_factory(journal, transport=transport)
    state = journal.state
    state.update(
        partial_observed=None,
        position_may_remain=True,
        account_reconciled=False,
        ledger_costs_reconciled=False,
        terminal_order_confirmed=False,
        cash_debit_usd=None,
        own_fills_count=None,
        own_fills_summary=None,
        observed_positions=None,
        runner_shutdown_clean=False,
        reconcile_complete=False,
        account_after_complete=False,
    )
    restore_ownership(journal)
    runner = ProductionRunner()
    state["gates"] = {
        "cycle": runner.settings.MOTOR_MM_ENABLED,
        "global": runner.settings.TRADING_ENABLED,
        "execution": runner.settings.MOTOR_MM_EXECUTION_ENABLED,
    }
    if not all(state["gates"].values()):
        raise PermissionError("RUNNER_GATE_CLOSED")
    if not journal.recover:
        async with factory() as client:
            await preflight(client, journal)
        await RiskManager.refresh_capital_from_balance(client_factory=factory)
        if not RiskManager().can_quote_new_positions():
            raise PermissionError("CASH_GATE_CLOSED")
    state["ledger_before"] = ledger_snapshot()
    journal.persist()

    class CanaryEngine(Motor5Engine):
        def __init__(self, **kwargs):
            super().__init__(client_factory=factory, **kwargs)

        async def _tick(self):
            try:
                if self._executor is None or self._reconciler is None:
                    raise PermissionError("EXECUTOR_GATE_CLOSED")
                state["executor_constructed_by_runner"] = True
                if not journal.recover:
                    # El delay real de arranque no convierte el book previo en actual.
                    await preflight(self._client, journal)
                    state["quote_outcome"] = await self._executor.sync_quotes(
                        QuoteSet(
                            ticker=state["ticker"],
                            fair_prob=state["price_cents"] / 100,
                            bid_cents=state["price_cents"],
                            ask_cents=None,
                            size=1,
                        )
                    )
                    state["ledger_after_intent"] = ledger_snapshot()
                    journal.persist()
                    await asyncio.sleep(args.hold_seconds)
                report = await self._reconciler.reconcile(
                    live_coids=set() if journal.recover else self._executor.managed_coids()
                )
                self._executor.forget_confirmed(report.terminal_coids)
                self._apply_settled_fills()
                state["reconcile_complete"] = report.complete
            finally:
                runner._stop_event.set()

    try:
        with patch("src.strategies.motor_5_mm.engine.Motor5Engine", CanaryEngine):
            await runner._run_motor5_mm()
        state["runner_shutdown_clean"] = True
    except Exception as exc:
        state["runner_shutdown_clean"] = False
        record_error(state, "runner", exc)
    state["ledger_after"] = ledger_snapshot()
    state["account_after_complete"] = False
    try:
        from src.clients.kalshi_rest import available_cash_usd

        async with factory() as client:
            state["cash_after_usd"] = str(
                available_cash_usd(await client.get_balance(subaccount=0))
            )
            state["resting_after"] = len(await client.get_all_orders(status="resting"))
            state["observed_positions"] = signed_positions(await positions(client))
            state["position_after_abs"] = str(
                abs(Decimal(state["observed_positions"].get(state["ticker"], "0")))
            )
            state["own_fills_summary"] = await own_fills(client, state)
            state["own_fills_count"] = state["own_fills_summary"]["quantity"]
            state["account_after_complete"] = True
    except Exception as exc:
        record_error(state, "account_after", exc)
    rows = state["ledger_after"]["trades"]
    state["status"] = "UNCONFIRMED_RECOVER_REQUIRED"
    state["account_reconciled"] = False
    if not rows and state["post_attempts"] == 0:
        state["status"] = "ADMISSION_DECLINED"
    elif (
        len(rows) == 1
        and rows[0]["status"] in {"filled", "cancelled"}
        and state["runner_shutdown_clean"]
    ):
        if rows[0]["filled_count"] is None:
            journal.persist()
            return state
        filled = number(rows[0]["filled_count"])
        state["terminal_order_confirmed"] = True
        if state["account_after_complete"] and state["resting_after"] == 0:
            try:
                state.update(
                    reconcile_account(
                        state, rows[0], state["own_fills_summary"], state["observed_positions"]
                    )
                )
            except Exception as exc:
                record_error(state, "account_comparison", exc)
        state["status"] = (
            ("TERMINAL_WITH_FILL" if filled else "TERMINAL_NO_FILL")
            if state["account_reconciled"]
            else "TERMINAL_ACCOUNT_UNCONFIRMED"
        )
        # Conciliar no vende el inventario ni levanta pausas durables del core.
        state["partial_observed"] = 0 < filled < 1
        state["position_may_remain"] = filled > 0 or not state["account_reconciled"]
    state["http_attempts_this_process"] = journal.http
    journal.persist()
    return state


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--workspace", type=Path, required=True)
    result.add_argument("--ticker", default="KXHIGHTOKC-26OCT08-B88.5")
    result.add_argument("--price-cents", type=int, default=1)
    result.add_argument("--hold-seconds", type=int, default=5)
    group = result.add_mutually_exclusive_group()
    group.add_argument("--execute", action="store_true")
    group.add_argument("--recover", action="store_true")
    result.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return result


def worker(args):
    from loguru import logger

    from src.storage import models

    logger.remove()
    os.umask(0o077)
    workspace = args.workspace.absolute()
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    if workspace.is_symlink() or workspace.stat().st_mode & 0o077:
        raise PermissionError("WORKSPACE_NOT_PRIVATE")
    for name in ("journal.json", "ledger.db", "session.lock"):
        path = workspace / name
        if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
            raise PermissionError("WORKSPACE_FILE_INVALID")
    with open(workspace / "session.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = workspace / "journal.json"
        if args.recover:
            if (
                not path.exists()
                or path.stat().st_size > 65536
                or not (workspace / "ledger.db").exists()
            ):
                raise ValueError("RECOVERY_STATE_MISSING")
            state = json.loads(path.read_text())
            if (
                state.get("schema") != SCHEMA
                or state.get("environment") != "demo"
                or state.get("ticker") != args.ticker
                or state.get("price_cents") != args.price_cents
                or type(state.get("post_attempts")) is not int
                or state["post_attempts"] not in (0, 1)
            ):
                raise ValueError("RECOVERY_STATE_INVALID")
        else:
            if path.exists() or (workspace / "ledger.db").exists():
                raise ValueError("EXISTING_INTENT_USE_RECOVER")
            state = plan(args)
            state["created_at_unix"] = time.time()
            save(path, state)
        credentials = Path.home() / ".config/botkalshi/demo"
        journal = Journal(path, state, recover=args.recover)
        try:
            configure(workspace, credentials)
            models.init_db()
        except Exception as exc:
            state.update(
                status="CONFIGURATION_FAILED_RECOVER_ONLY",
                account_reconciled=False,
                position_may_remain=True,
            )
            record_error(state, "configure", exc)
            journal.persist()
            save(workspace / "report.json", state)
            return
        try:
            result = asyncio.run(run_session(args, journal))
        except Exception as exc:
            state["status"] = (
                "UNCONFIRMED_RECOVER_REQUIRED" if state["post_attempts"] else "DECLINED"
            )
            record_error(state, "session", exc)
            journal.persist()
            result = state
        save(workspace / "report.json", result)


def main(argv=None):
    args = parser().parse_args(argv)
    proposal = plan(args)
    if not args.execute and not args.recover:
        print(json.dumps(proposal, sort_keys=True))
        return 0
    if args.worker:
        worker(args)
        return 0
    # Un proceso/cwd nuevo evita singleton Settings y lecturas accidentales de .env.
    command = [
        sys.executable,
        "-I",
        "-B",
        str(Path(__file__).resolve()),
        "--worker",
        "--workspace",
        str(args.workspace.absolute()),
        "--ticker",
        args.ticker,
        "--price-cents",
        str(args.price_cents),
        "--hold-seconds",
        str(args.hold_seconds),
        "--recover" if args.recover else "--execute",
    ]
    with tempfile.TemporaryDirectory(prefix="demo-m5-worker-") as cwd:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env={"PATH": "/usr/bin:/bin"},
                timeout=180,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            status = (
                "WORKER_FINISHED" if completed.returncode == 0 else "WORKER_FAILED_RECOVER_ONLY"
            )
        except subprocess.TimeoutExpired:
            status = "WORKER_TIMEOUT_RECOVER_ONLY"
    print(
        json.dumps(
            {
                "status": status,
                "report": str(args.workspace.absolute() / "report.json"),
                "warning": "inspect_journal_and_ledger_before_any_other_demo_order",
            }
        )
    )
    return 0 if status == "WORKER_FINISHED" else 1


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    with contextlib.suppress(KeyboardInterrupt):
        raise SystemExit(main())
