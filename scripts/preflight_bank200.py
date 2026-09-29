"""Preflight READ-ONLY para armar la prueba real de USD 200.

No coloca, cancela ni modifica órdenes. Verifica configuración, autenticación,
cash disponible, posiciones y órdenes abiertas. Ante cualquier dato desconocido
sale NO-GO.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

from src.clients.kalshi_rest import KalshiRestClient
from src.utils.config import get_settings

_PAGE_LIMIT = 100
_MAX_POSITION_PAGES = 1000


def _finite_decimal(raw: object, field: str) -> Decimal:
    # Un dato desconocido nunca demuestra ausencia de exposición ni cash suficiente.
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
        raise ValueError(f"{field}: número ausente o inválido")
    try:
        value = Decimal(str(raw))
    except InvalidOperation as exc:
        raise ValueError(f"{field}: número inválido") from exc
    if not value.is_finite():
        raise ValueError(f"{field}: número no finito")
    return value


def _position_count(row: dict) -> Decimal:
    if not isinstance(row, dict):
        raise ValueError("posición: estructura inválida")
    # Conservar fracciones: redondear 0.1 a cero ocultaba una posición real.
    return _finite_decimal(row.get("position_fp", row.get("position")), "posición")


def _page_rows(page: object, *fields: str) -> list[dict]:
    if not isinstance(page, dict):
        raise ValueError("respuesta: estructura inválida")
    for field in fields:
        if field in page:
            rows = page[field]
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(f"{field}: lista inválida")
            return rows
    raise ValueError("respuesta: falta colección esperada")


def _page_cursor(page: dict) -> str | None:
    cursor = page.get("cursor")
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str) or not cursor.strip():
        raise ValueError("cursor inválido")
    return cursor


async def _read_account() -> dict:
    positions: list[dict] = []
    async with KalshiRestClient() as client:
        balance = await client.get_balance()
        if not isinstance(balance, dict):
            raise ValueError("balance: estructura inválida")
        cents = _finite_decimal(balance.get("balance"), "balance")
        if cents < 0 or cents != cents.to_integral_value():
            raise ValueError("balance: se requieren centavos enteros no negativos")

        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_POSITION_PAGES):
            page = await client.get_positions(limit=_PAGE_LIMIT, cursor=cursor)
            rows = _page_rows(page, "market_positions", "positions")
            positions.extend(p for p in rows if _position_count(p) != 0)
            cursor = _page_cursor(page)
            if cursor is None:
                if "cursor" not in page and len(rows) >= _PAGE_LIMIT:
                    raise ValueError("posiciones: exhaustividad no demostrada")
                break
            if cursor in seen_cursors:
                raise ValueError("posiciones: cursor repetido")
            seen_cursors.add(cursor)
        else:
            raise ValueError("posiciones: límite de páginas; exhaustividad no demostrada")

        orders_page = await client.get_orders(limit=_PAGE_LIMIT)
        orders = _page_rows(orders_page, "orders")
        # El cliente actual no pagina órdenes: una página parcial jamás es un GO.
        if _page_cursor(orders_page) is not None or len(orders) >= _PAGE_LIMIT:
            raise ValueError("órdenes: exhaustividad no demostrada")

    return {
        "cash_usd": cents / Decimal(100),
        "positions": positions,
        "orders": orders,
    }


def _open_orders(rows: list[dict]) -> list[dict]:
    open_states = {"resting", "pending", "open"}
    closed_states = {"executed", "canceled"}
    opened = []
    for row in rows:
        status = row.get("status")
        if not isinstance(status, str) or status.lower() not in open_states | closed_states:
            raise ValueError("orden: estado ausente o desconocido")
        if status.lower() in open_states:
            opened.append(row)
    return opened


async def main() -> int:
    settings = get_settings()
    blockers: list[str] = []

    if settings.KALSHI_ENV != "production":
        blockers.append("KALSHI_ENV debe ser production")
    if not settings.EXPERIMENT_BANK200_ENABLED:
        blockers.append("EXPERIMENT_BANK200_ENABLED debe ser true")
    if settings.EXPERIMENT_BANK_CONFIRMED_USD != 200.0:
        blockers.append("falta ack EXPERIMENT_BANK_CONFIRMED_USD=200")
    if settings.TRADING_ENABLED:
        blockers.append("TRADING_ENABLED debe seguir false durante el preflight")
    if not settings.MOTOR_1_ARBITRAGE_ENABLED:
        blockers.append("MOTOR_1_ARBITRAGE_ENABLED debe estar preparado=true")
    if not settings.MOTOR_1_EXECUTION_ENABLED:
        blockers.append("MOTOR_1_EXECUTION_ENABLED debe estar preparado=true")
    if not settings.MOTOR_3_CLV_ENABLED:
        blockers.append("MOTOR_3_CLV_ENABLED debe estar preparado=true")
    if not settings.MOTOR_3_EXECUTION_ENABLED:
        blockers.append("MOTOR_3_EXECUTION_ENABLED debe estar preparado=true")
    if not settings.MOTOR_3_MANAGES_ORPHANS:
        blockers.append("MOTOR_3_MANAGES_ORPHANS debe estar true")
    if settings.MOTOR_2_ENTRY_EXECUTION_ENABLED:
        blockers.append("Motor 2 entradas debe seguir off")
    if settings.MOTOR_REST_EXECUTION_ENABLED:
        blockers.append("Motor REST ejecución debe seguir off")
    if settings.MOTOR_MM_EXECUTION_ENABLED:
        blockers.append("Motor MM ejecución debe seguir off")

    try:
        account = await _read_account()
        opened = _open_orders(account["orders"])
    except Exception as exc:  # noqa: BLE001 - fail-closed
        print(f"NO-GO: lectura autenticada falló ({type(exc).__name__})")
        return 2

    cash = account["cash_usd"]
    positions = account["positions"]

    if cash < 200.0:
        blockers.append(f"cash Kalshi ${cash:.2f} < $200 separados")
    if positions:
        blockers.append(f"{len(positions)} posición(es) abierta(s) sin conciliar")
    if opened:
        blockers.append(f"{len(opened)} orden(es) abierta(s) sin conciliar")

    print("BANK200 PREFLIGHT — READ ONLY")
    print(f"cash_exchange=${cash:.2f}")
    print(f"open_positions={len(positions)}")
    print(f"open_orders={len(opened)}")
    print(f"trading_enabled={settings.TRADING_ENABLED}")
    print("credentials=present (contenido no mostrado)")

    if blockers:
        print("NO-GO")
        for blocker in blockers:
            print(f"- {blocker}")
        return 2

    print("READY_TO_ARM")
    print("El preflight NO armó trading ni modificó la cuenta.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
