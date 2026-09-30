"""Lee un reporte ya publicado, sin API, llaves, shell ni cambios de estado.

El operador puede agregar UN comando fijo a su whitelist SSH que ejecute este lector
con un path fijo. Esta herramienta no modifica ni rodea la whitelist existente.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.analytics.portfolio_report import json_safe
from src.monitoring.operator_report_files import read_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--max-age-seconds", type=int, default=600)
    args = parser.parse_args()
    report = read_report(args.input, max_age_seconds=args.max_age_seconds)
    print(json.dumps(json_safe(report), ensure_ascii=False))
    return 0 if report["status"] == "OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
