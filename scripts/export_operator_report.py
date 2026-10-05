"""Exporta una lectura API a un archivo privado. No escribe la DB ni hace trading.

    python -m scripts.export_operator_report --output /ruta/privada/operator-report.json

Su instalación/cadencia corresponde al operador; la falla no gatea al bot.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from scripts.check_portfolio import run
from src.monitoring.operator_report_files import publish_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--subaccount", type=int, default=0)
    args = parser.parse_args()
    started = datetime.now(UTC).isoformat()
    try:
        report = asyncio.run(
            run(
                argparse.Namespace(
                    subaccount=args.subaccount,
                    hours=24,
                    page_size=100,
                    max_pages=50,
                    timeout=60,
                )
            )
        )
    except Exception as exc:
        report = {
            "schema_version": 1,
            "status": "ATENCION",
            "read_only": True,
            "authorizes_trading": False,
            "collection_started_at": started,
            "collection_finished_at": datetime.now(UTC).isoformat(),
            "errors": [f"colección falló ({type(exc).__name__})"],
            "pnl_net_usd": None,
        }
    try:
        publish_report(args.output, report)
    except (OSError, ValueError):
        # No escribir material de configuración ni paths de llaves al journal.
        print("ATENCION: publicación falló; el reporte anterior caducará por TTL.")
        return 1
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
