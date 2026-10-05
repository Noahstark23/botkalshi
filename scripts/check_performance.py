"""Lee un JSON local de snapshots/flujos y calcula resultado separado de aportes.

    python -m scripts.check_performance informe-periodo.json

No conecta a la API, no lee llaves, no escribe DB/archivos ni autoriza órdenes.
"""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from src.analytics.capital_performance import period_performance
from src.analytics.portfolio_report import json_safe


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        if args.input.stat().st_size > 20_000_000:
            raise ValueError("Presupuesto de archivo agotado")
        document = json.loads(args.input.read_text(), parse_float=Decimal)
        report = period_performance(document)
    except (OSError, ValueError) as exc:
        report = {
            "status": "ATENCION",
            "project_pnl_usd": None,
            "authorizes_trading": False,
            "error": f"archivo inválido ({type(exc).__name__})",
        }
    print(json.dumps(json_safe(report), ensure_ascii=False))
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
