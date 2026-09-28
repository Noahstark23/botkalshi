"""Preflight READ-ONLY para armar la prueba real de USD 200.

No coloca, cancela ni modifica órdenes. Verifica configuración, autenticación,
cash disponible, posiciones y órdenes abiertas. Ante cualquier dato desconocido
sale NO-GO.
"""

from __future__ import annotations

import asyncio

from src.clients.kalshi_rest import KalshiRestClient
from src.utils.config import get_settings


def _position_count(row: dict) -> int:
    raw = row.get("position_fp", row.get("position"))
    try:
        return int(round(float(raw))) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


async def _read_account() -> dict:
    positions: list[dict] = []
    async with KalshiRestClient() as client:
        balance = await client.get_balance()
        cursor: str | None = None
        while True:
            page = await client.get_positions(limit=100, cursor=cursor)
            positions.extend(page.get("market_positions", page.get("positions", [])))
            cursor = page.get("cursor")
            if not cursor:
                break

        orders_page = await client.get_orders(limit=100)

    cents = balance.get("balance") if isinstance(balance, dict) else None
    if cents is None:
        raise ValueError("balance response sin campo balance")
    return {
        "cash_usd": float(cents) / 100.0,
        "positions": [p for p in positions if _position_count(p) != 0],
        "orders": orders_page.get("orders", []),
    }


def _open_orders(rows: list[dict]) -> list[dict]:
    open_states = {"resting", "pending", "open"}
    return [row for row in rows if str(row.get("status", "")).lower() in open_states]


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
    except Exception as exc:  # noqa: BLE001 - fail-closed
        print(f"NO-GO: lectura autenticada falló ({type(exc).__name__})")
        return 2

    cash = account["cash_usd"]
    positions = account["positions"]
    orders = account["orders"]
    opened = _open_orders(orders)

    if cash < 200.0:
        blockers.append(f"cash Kalshi ${cash:.2f} < $200 separados")
    if positions:
        blockers.append(f"{len(positions)} posición(es) abierta(s) sin conciliar")
    if opened:
        blockers.append(f"{len(opened)} orden(es) abierta(s) sin conciliar")
    if len(orders) >= 100:
        blockers.append("lista de órdenes alcanzó limit=100; exhaustividad no demostrada")

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
