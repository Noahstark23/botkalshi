"""Prueba acotada de place/cancel/reinicio, exclusivamente en Kalshi DEMO.

Lee claves de ~/.config/botkalshi/demo; nunca carga .env ni claves de producción.
Sin --execute sólo consulta. Una ejecución admite como máximo UNA orden post-only
de un contrato a 1 centavo. Conserva intención antes de red y permite --recover
sin volver a crearla. No prueba alpha, partial fills ni el runner completo.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
from loguru import logger

from src.clients.kalshi_rest import KalshiRestClient
from src.utils.config import Settings, get_settings, reset_settings_for_testing

DEMO_ROOT = "https://external-api.demo.kalshi.co/trade-api/v2"
PREFIX = "demo-readiness-"
CREATE_TIMEOUT_SECONDS = 15
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TICKER = re.compile(r"[A-Z0-9][A-Z0-9.-]{2,99}\Z")
_COUNT = re.compile(r"[0-9]+(?:\.[0-9]{1,2})?\Z")
_UNCERTAINTY = ("count_contradiction", "invalid_count_observed", "invalid_order_observed")


def save_state(path: Path, data: dict) -> None:
    """Diario previo a red; reemplazo atómico, dueño privado, sin secretos."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def count(order: dict, field: str) -> Decimal:
    raw = order.get(field + "_fp", order.get(field))
    if raw is None or isinstance(raw, bool) or not isinstance(raw, (str, int)):
        raise ValueError("Cantidad sin confirmar")
    if len(str(raw)) > 32 or not _COUNT.fullmatch(str(raw)):
        raise ValueError("Cantidad inválida")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Cantidad inválida") from exc
    if not value.is_finite() or not 0 <= value <= 1:
        raise ValueError("Cantidad fuera del contrato de la prueba")
    legacy = order.get(field)
    if legacy is not None and (
        isinstance(legacy, bool)
        or not isinstance(legacy, (str, int))
        or len(str(legacy)) > 32
        or not _COUNT.fullmatch(str(legacy))
        or Decimal(str(legacy)) != value
    ):
        raise ValueError("Cantidades contradictorias")
    return value


def correlated(order: dict, state: dict) -> bool:
    return (
        isinstance(order, dict)
        and order.get("client_order_id") == state["client_order_id"]
        and order.get("ticker") == state["ticker"]
        and isinstance(order.get("order_id"), str)
        and bool(_ID.fullmatch(order["order_id"]))
        and (state.get("order_id") is None or state["order_id"] == order.get("order_id"))
    )


def _remember_fill(filled: Decimal, state: dict) -> None:
    previous = count({"fill_count": state.get("filled_count", "0")}, "fill_count")
    if filled < previous:
        state["count_contradiction"] = True
    state["filled_count"] = format(max(previous, filled), ".2f")
    state["position_may_remain"] = max(previous, filled) > 0 or any(
        state.get(flag) for flag in _UNCERTAINTY
    )


def _read_order_counts(order: dict) -> tuple[Decimal, Decimal]:
    """GET Order tiene dirección canónica e initial_count; el ack V2 no los tiene."""
    if order.get("outcome_side") != "yes" or order.get("book_side") != "bid":
        raise ValueError("Dirección de orden no corresponde al intent")
    for field, expected in (("side", "yes"), ("action", "buy")):
        if field in order and order[field] is not None and order[field] != expected:
            raise ValueError("Dirección legacy contradictoria")
    if "subaccount_number" in order and (
        type(order["subaccount_number"]) is not int or order["subaccount_number"] != 0
    ):
        raise ValueError("Subcuenta de orden distinta")
    if count(order, "initial_count") != 1:
        raise ValueError("Cantidad inicial distinta del intent")
    filled, remaining = count(order, "fill_count"), count(order, "remaining_count")
    status = order.get("status")
    if filled + remaining > 1:
        raise ValueError("Acumulados fuera del intent")
    if status == "executed":
        if filled != 1 or remaining != 0:
            raise ValueError("Executed sin ejecución completa")
    elif status in {"canceled", "cancelled"}:
        if remaining != 0:
            raise ValueError("Cancelación con remanente")
    elif status == "resting":
        if remaining <= 0 or filled + remaining != 1:
            raise ValueError("Resting sin cantidad pendiente coherente")
    else:
        raise ValueError("Estado de orden no verificable")
    return filled, remaining


def preserve_fill_floor(order: object, state: dict) -> None:
    """Cualquier respuesta correlacionada puede demostrar un fill, incluso un error."""
    if not isinstance(order, dict) or not correlated(order, state):
        return
    try:
        filled = count(order, "fill_count")
    except ValueError:
        state["invalid_count_observed"] = True
        state["position_may_remain"] = True
        return
    try:
        _read_order_counts(order)
    except ValueError:
        state["invalid_order_observed"] = True
    _remember_fill(filled, state)


def preserve_create_response(response: object, state: dict) -> None:
    """Ack de la única petición V2: plano, vinculado al intent por esa respuesta.

    No inventa ticker/dirección ausentes como si fueran evidencia de GET. Sólo
    conserva ID y piso de ejecución; el barrido posterior confirma el contrato.
    """
    order = response.get("order", response) if isinstance(response, dict) else None
    if not isinstance(order, dict):
        raise ValueError("Ack de creación inválido")
    oid = order.get("order_id")
    if (
        not isinstance(oid, str)
        or not _ID.fullmatch(oid)
        or (state.get("order_id") is not None and oid != state["order_id"])
        or ("client_order_id" in order and order["client_order_id"] != state["client_order_id"])
        or ("ticker" in order and order["ticker"] != state["ticker"])
    ):
        state["invalid_order_observed"] = True
        state["position_may_remain"] = True
        raise ValueError("Identidad del ack distinta del intent")
    state["order_id"] = oid
    try:
        filled = count(order, "fill_count")
        _remember_fill(filled, state)
        remaining = count(order, "remaining_count")
        if filled + remaining > 1:
            state["count_contradiction"] = True
        if ("initial_count" in order or "initial_count_fp" in order) and count(
            order, "initial_count"
        ) != 1:
            state["invalid_order_observed"] = True
        for field, expected in (("outcome_side", "yes"), ("book_side", "bid")):
            if field in order and order[field] != expected:
                state["invalid_order_observed"] = True
        _remember_fill(filled, state)
    except ValueError:
        state["invalid_count_observed"] = True
        state["position_may_remain"] = True
        raise


def _validate_journal(state: object, ticker: str) -> None:
    """Un piso corrupto jamás se convierte a cero al recuperar el diario."""
    if not isinstance(state, dict) or (
        state.get("schema") != "kalshi-demo-probe-v1"
        or state.get("environment") != "demo"
        or state.get("ticker") != ticker
        or type(state.get("real_money_orders")) is not int
        or state["real_money_orders"] != 0
        or state.get("status")
        not in {
            "INSPECT_ONLY",
            "INTENT_BEFORE_NETWORK",
            "UNCONFIRMED_NO_RETRY",
            "TERMINAL_CONFIRMED",
        }
    ):
        raise ValueError("Diario no corresponde a esta prueba")
    coid = state.get("client_order_id")
    if not isinstance(coid, str) or not coid.startswith(PREFIX):
        raise ValueError("Identidad de diario inválida")
    try:
        if str(uuid.UUID(coid.removeprefix(PREFIX))) != coid.removeprefix(PREFIX):
            raise ValueError("UUID no canónico")
    except (ValueError, AttributeError) as exc:
        raise ValueError("Identidad de diario inválida") from exc
    oid = state.get("order_id")
    if oid is not None and (not isinstance(oid, str) or not _ID.fullmatch(oid)):
        raise ValueError("ID de diario inválido")
    for field in ("filled_count", "remaining_count"):
        if field in state:
            count({"fill_count": state[field]}, "fill_count")
    for flag in (*_UNCERTAINTY, "position_may_remain"):
        if flag in state and type(state[flag]) is not bool:
            raise ValueError("Bandera de diario inválida")
    _remember_fill(count({"fill_count": state.get("filled_count", "0")}, "fill_count"), state)


class DemoOnlyClient(KalshiRestClient):
    """Restricción de host y métodos anterior a firma/red, además de Capa C."""

    def __init__(self, state: dict, *, allow_create: bool):
        settings = get_settings()
        if settings.KALSHI_ENV != "demo" or settings.rest_url != DEMO_ROOT:
            raise PermissionError("La prueba sólo admite el host oficial de demo")
        super().__init__()
        self.state = state
        self.allow_create = allow_create
        self.create_attempted = False
        self.owned_order_ids: set[str] = set()

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            base_url=DEMO_ROOT,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        return self

    async def _request(self, method, path, **kwargs):
        if (
            self.base_url != DEMO_ROOT
            or self.settings.KALSHI_ENV != "demo"
            or self.settings.rest_url != DEMO_ROOT
            or str(self._client.base_url).rstrip("/") != DEMO_ROOT
        ):
            raise PermissionError("Host de demo alterado")
        ticker = self.state["ticker"]
        allowed_reads = {
            "/portfolio/balance",
            "/portfolio/orders",
            f"/markets/{ticker}",
            f"/markets/{ticker}/orderbook",
        }
        if method == "GET":
            order_id = path.removeprefix("/portfolio/orders/")
            if path not in allowed_reads and not (
                path.startswith("/portfolio/orders/") and order_id in self.owned_order_ids
            ):
                raise PermissionError("Lectura fuera del alcance de la prueba")
            if path == "/portfolio/orders":
                # get_all_orders heredado no admite subaccount: fijarlo aquí en
                # CADA página, incluido recovery y el barrido previo al create.
                kwargs["params"] = {**(kwargs.get("params") or {}), "subaccount": 0}
        elif method == "POST":
            body = kwargs.get("json", {})
            if (
                not self.allow_create
                or self.create_attempted
                or path != "/portfolio/events/orders"
                or body.get("ticker") != ticker
                or body.get("client_order_id") != self.state["client_order_id"]
                or body.get("count") not in ("1", "1.00")
                or body.get("price") != "0.01"
                or body.get("side") != "bid"
                or body.get("post_only") is not True
                or body.get("time_in_force") != "good_till_canceled"
            ):
                raise PermissionError("Orden fuera del presupuesto de demo")
            # Se cuenta el intento incluso si la respuesta se pierde.
            self.create_attempted = True
            # El cliente general reintenta POST. Este ensayo autoriza un único
            # envío: una respuesta ambigua sólo conduce a lectura/recuperación.
            async with asyncio.timeout(CREATE_TIMEOUT_SECONDS):
                headers = self.signer.sign(method, self._build_sign_path(path))
                async with self._client.stream(method, path, headers=headers, **kwargs) as response:
                    if response.status_code not in (200, 201):
                        raise RuntimeError(f"Demo create HTTP {response.status_code}")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise ValueError("Respuesta de demo demasiado grande")
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError("Respuesta de demo inválida")
                    return result
        elif method == "DELETE":
            order_id = path.removeprefix("/portfolio/events/orders/")
            if not (
                path.startswith("/portfolio/events/orders/") and order_id in self.owned_order_ids
            ):
                raise PermissionError("No se cancela una orden ajena a la prueba")
        else:
            raise PermissionError("Método fuera del alcance de demo")
        return await super()._request(method, path, **kwargs)


async def reconcile(client: DemoOnlyClient, state: dict, path: Path) -> dict | None:
    orders = await client.get_all_orders(ticker=state["ticker"])
    mine = [o for o in orders if o.get("client_order_id") == state["client_order_id"]]
    # Un listado duplicado no acredita cierre, pero tampoco puede borrar el
    # piso de un fill propio que aparezca dentro de ese mismo listado incierto.
    for observed in mine:
        preserve_fill_floor(observed, state)
    if len(mine) != 1 or not correlated(mine[0], state):
        state["status"] = "UNCONFIRMED_NO_RETRY"
        state["position_may_remain"] = True
        if mine:
            state["invalid_order_observed"] = True
        save_state(path, state)
        return None
    order = mine[0]
    state["order_id"] = order["order_id"]
    client.owned_order_ids.add(order["order_id"])
    state["observed_status"] = order.get("status")
    if any(state.get(flag) for flag in _UNCERTAINTY):
        state["status"] = "UNCONFIRMED_NO_RETRY"
        save_state(path, state)
        return None
    _, remaining = _read_order_counts(order)
    state["remaining_count"] = str(remaining)
    save_state(path, state)
    return order


async def run_probe(args: argparse.Namespace) -> dict:
    # Impide dos intenciones distintas simultáneas sobre el mismo diario.
    args.state = args.state.resolve()
    args.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(args.state) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return await _run_probe_locked(args)


async def _run_probe_locked(args: argparse.Namespace) -> dict:
    if not _TICKER.fullmatch(args.ticker):
        raise ValueError("Ticker inválido")
    path = args.state.resolve()
    if path.exists():
        if not args.recover:
            raise ValueError("Ya hay un diario; usar --recover para conciliarlo")
        if path.stat().st_size > 65536:
            raise ValueError("Diario demasiado grande")
        state = json.loads(path.read_text())
        _validate_journal(state, args.ticker)
    elif args.recover:
        raise ValueError("No hay diario para recuperar")
    else:
        state = {
            "schema": "kalshi-demo-probe-v1",
            "environment": "demo",
            "ticker": args.ticker,
            "client_order_id": PREFIX + str(uuid.uuid4()),
            "status": "INSPECT_ONLY",
            "created_at_unix": time.time(),
            "real_money_orders": 0,
        }

    async with DemoOnlyClient(state, allow_create=args.execute and not args.recover) as client:
        await client.get_balance(subaccount=0)
        if not args.execute and not args.recover:
            return state
        if not args.recover:
            # Evita mezclar un ensayo nuevo con órdenes resting heredadas.
            if await client.get_all_orders(status="resting"):
                raise ValueError("Demo tiene órdenes abiertas: no se crea otra")
            market = (await client.get_market(args.ticker)).get("market", {})
            if market.get("status") not in {"open", "active"}:
                raise ValueError("Mercado demo no abierto")
            state["status"] = "INTENT_BEFORE_NETWORK"
            save_state(path, state)
            try:
                response = await client.place_order(
                    ticker=args.ticker,
                    side="yes",
                    action="buy",
                    count=1,
                    yes_price=1,
                    client_order_id=state["client_order_id"],
                    time_in_force="gtc",
                    post_only=True,
                )
                preserve_create_response(response, state)
                save_state(path, state)
            except Exception as exc:
                state["create_error_type"] = type(exc).__name__
                save_state(path, state)
        order = await reconcile(client, state, path)
        if order is None:
            return state
        state["resting_observed"] = order.get("status") == "resting"
        if count(order, "remaining_count") > 0:
            started = time.monotonic()
            try:
                cancelled = await client.cancel_order(order["order_id"])
                if isinstance(cancelled, dict):
                    preserve_fill_floor(cancelled.get("order"), state)
            except Exception as exc:
                state["cancel_error_type"] = type(exc).__name__
                preserve_fill_floor(getattr(exc, "observed_order", None), state)
                observations = getattr(exc, "observed_orders", None)
                if isinstance(observations, list):
                    for observed in observations[:60]:
                        preserve_fill_floor(observed, state)
            state["cancel_elapsed_seconds"] = round(time.monotonic() - started, 3)
            save_state(path, state)

    # Cliente nuevo: prueba la lectura tras perder el estado de transporte.
    async with DemoOnlyClient(state, allow_create=False) as restarted:
        order = await reconcile(restarted, state, path)
    if order is not None:
        terminal = order.get("status") in {"executed", "canceled", "cancelled"}
        empty_remaining = count(order, "remaining_count") == 0
        state["status"] = (
            "TERMINAL_CONFIRMED" if terminal and empty_remaining else "UNCONFIRMED_NO_RETRY"
        )
        state["partial_fill_observed"] = 0 < count(order, "fill_count") < 1
        state["position_may_remain"] = count(order, "fill_count") > 0
        state["runner_integration_validated"] = False
        save_state(path, state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--recover", action="store_true")
    args = parser.parse_args()
    credentials = Path.home() / ".config/botkalshi/demo"
    logger.remove()
    try:
        # Configuración aislada antes de construir cualquier cliente/firma.
        key_id = (credentials / "key_id.txt").read_text().strip()
        key_path = credentials / "kalshi_private_key.pem"
        if not key_path.is_file():
            raise FileNotFoundError
        os.environ.clear()
        os.environ.update(
            KALSHI_ENV="demo",
            KALSHI_API_KEY_ID=key_id,
            KALSHI_PRIVATE_KEY_PATH=str(key_path),
            KALSHI_DEMO_REST_URL=DEMO_ROOT,
            TRADING_ENABLED="true" if args.execute else "false",
            DATABASE_URL="sqlite:///:memory:",
        )
        Settings.model_config["env_file"] = None
        reset_settings_for_testing()
        result = asyncio.run(run_probe(args))
    except Exception as exc:
        result = {"status": "BLOCKED", "error_type": type(exc).__name__, "real_money_orders": 0}
    print(json.dumps(result, indent=2))
    return 0 if result["status"] in {"INSPECT_ONLY", "TERMINAL_CONFIRMED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
