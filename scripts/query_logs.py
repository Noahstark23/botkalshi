#!/usr/bin/env python3
"""
Consulta los logs PERSISTENTES del bot (volumen kalshi-logs → /app/logs) — plan verde
2026-07-02: el análisis operativo migra de leer el stdout volátil de la Coolify UI
(ventana por container) a leer los archivos rotados del volumen, que cruzan restarts
(bot_*.log 30 días, critical_*.log 90 días, gz automático a medianoche).

Read-only, stdlib puro (corre dentro del container o contra una copia del volumen).

Uso típico (dentro del container / VPS):
    python scripts/query_logs.py --pattern "motor2.funnel" --since 2026-07-01
    python scripts/query_logs.py --pattern "dedup_skip|stake_below" --since 2026-06-28
    python scripts/query_logs.py --sink critical --pattern "kill" --since 2026-06-20
    python scripts/query_logs.py --pattern "motor2.exec.cycle" --count

Notas:
- --pattern es una regex de Python (grep -E equivalente).
- Lee .log y .log.gz de forma transparente; los días se filtran por el NOMBRE del
  archivo (bot_YYYY-MM-DD.log[.gz]) — barato, sin parsear timestamps línea a línea.
- --count imprime solo el conteo por archivo (para trends rápidos día a día).
"""

from __future__ import annotations

import argparse
import gzip
import re
import sys
from datetime import date
from pathlib import Path

DEFAULT_LOGS_DIR = Path("/app/logs")
_FILE_RE = re.compile(r"^(?P<sink>bot|critical)_(?P<date>\d{4}-\d{2}-\d{2})\.log(?:\.gz)?$")


def _iter_files(logs_dir: Path, sink: str, since: date | None, until: date | None):
    """Archivos del sink dentro del rango de fechas, ordenados cronológicamente."""
    out = []
    for f in sorted(logs_dir.iterdir()):
        m = _FILE_RE.match(f.name)
        if not m or m.group("sink") != sink:
            continue
        d = date.fromisoformat(m.group("date"))
        if since is not None and d < since:
            continue
        if until is not None and d > until:
            continue
        out.append(f)
    return out


def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--pattern", required=True, help="regex a buscar (grep -E)")
    ap.add_argument("--sink", choices=("bot", "critical"), default="bot")
    ap.add_argument("--since", type=date.fromisoformat, default=None, help="YYYY-MM-DD inclusive")
    ap.add_argument("--until", type=date.fromisoformat, default=None, help="YYYY-MM-DD inclusive")
    ap.add_argument("--logs-dir", type=Path, default=DEFAULT_LOGS_DIR)
    ap.add_argument("--count", action="store_true", help="solo conteo por archivo (trend diario)")
    ap.add_argument("--max-lines", type=int, default=2000, help="tope de líneas impresas")
    args = ap.parse_args()

    if not args.logs_dir.is_dir():
        print(f"logs dir no existe: {args.logs_dir}", file=sys.stderr)
        return 2
    rx = re.compile(args.pattern)
    files = _iter_files(args.logs_dir, args.sink, args.since, args.until)
    if not files:
        print("sin archivos en el rango", file=sys.stderr)
        return 1

    printed = 0
    total = 0
    for f in files:
        n = 0
        with _open_text(f) as fh:
            for line in fh:
                if rx.search(line):
                    n += 1
                    if not args.count and printed < args.max_lines:
                        print(f"{f.name}: {line.rstrip()}")
                        printed += 1
        total += n
        if args.count:
            print(f"{f.name}: {n}")
    if not args.count and printed >= args.max_lines:
        print(f"... truncado a --max-lines={args.max_lines}", file=sys.stderr)
    print(f"total: {total}", file=sys.stderr)
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
