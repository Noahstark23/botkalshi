"""Diagnóstico readonly de la cuenta/subcuenta, sin DB ni autorización de trading.

En la instancia que ya dispone de credenciales, sin imprimirlas:
    python -m scripts.check_portfolio
    python -m scripts.check_portfolio --json --subaccount 0 --hours 24

Exit 0: consultas completas y válidas. Exit 1: ATENCIÓN, no se certifica cartera.
El CLI no coloca/cancela órdenes, no toca flags, no estima PnL ni escribe archivos.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json

from loguru import logger

from src.analytics.portfolio_report import collect_portfolio, json_safe
from src.clients.kalshi_readonly import ReadOnlyKalshiRestClient
from src.utils.config import get_settings


async def run(args: argparse.Namespace) -> dict:
    # En este proceso de diagnóstico no se publican cuerpos HTTP ni errores de auth.
    logger.disable("src.clients.kalshi_rest")
    settings = get_settings()
    fingerprint = hashlib.sha256(settings.KALSHI_API_KEY_ID.encode()).hexdigest()
    async with ReadOnlyKalshiRestClient() as client:
        return await collect_portfolio(
            client,
            environment=settings.KALSHI_ENV,
            source_fingerprint=fingerprint,
            subaccount=args.subaccount,
            hours=args.hours,
            limit=args.page_size,
            max_pages=args.max_pages,
            timeout_seconds=args.timeout,
        )


def render_text(report: dict) -> str:
    balance = report.get("balance", {})
    totals = report.get("totals", {})

    def shown(value):
        return "DESCONOCIDO" if value is None else str(value)

    lines = [
        f"Estado de lectura: {report['status']} (no autoriza trading)",
        f"Ámbito: {report.get('scope', {}).get('environment', '?')} / "
        f"subcuenta {report.get('scope', {}).get('subaccount', '?')}",
        f"Cash USD: {shown(balance.get('cash_usd'))}",
        f"Portfolio value reportado USD: {shown(balance.get('portfolio_value_usd'))}",
        f"Posiciones no cero: {shown(totals.get('nonzero_positions'))}",
        f"Exposición reportada USD: {shown(totals.get('market_exposure_usd'))}",
        f"Órdenes resting: {shown(totals.get('resting_orders'))}",
        f"Fees efectivos de fills en ventana USD: {shown(totals.get('fill_fees_usd'))}",
        "PnL neto: NO EVALUADO; cash, aportes y revenue no equivalen a ganancias.",
        "La lectura no es atómica; los fills cubren solo el endpoint reciente.",
    ]
    for name in ("positions", "orders", "fills", "settlements"):
        resource = report.get(name, {})
        lines.append(
            f"{name}: {len(resource.get('rows', []))} filas / "
            f"{resource.get('pages', 0)} páginas / completa={resource.get('complete', False)}"
        )
    for row in report.get("positions", {}).get("rows", [])[:20]:
        if row["quantity"] != 0 or row["market_exposure_usd"] != 0:
            lines.append(
                f"  {row['ticker']}: cantidad={row['quantity']}, "
                f"exposición USD={row['market_exposure_usd']}"
            )
    lines.extend(f"ATENCIÓN: {error}" for error in report.get("errors", []))
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="Snapshot con strings decimales exactos"
    )
    parser.add_argument("--subaccount", type=int, default=0)
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        report = {
            "schema_version": 1,
            "status": "ATENCION",
            "read_only": True,
            "authorizes_trading": False,
            "errors": [f"configuración o consulta falló ({type(exc).__name__})"],
        }
    print(json.dumps(json_safe(report), ensure_ascii=False) if args.json else render_text(report))
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
