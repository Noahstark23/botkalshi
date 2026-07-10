#!/usr/bin/env python3
"""
Triage de disco READ-ONLY — "saber qué eliminar" ANTES de borrar nada (incidente 2026-07-10:
disco 95%, ~4.6 GB libres, bot corriendo con dinero real en Docker/Coolify).

POR QUÉ existe: con el disco lleno no se puede copiar ni hacer VACUUM (necesita ~2×). Antes
de cualquier acción destructiva hay que MEDIR dónde están los GB. Este script no toca nada
por defecto: solo reporta.
  - uso de disco del mount de la DB (shutil.disk_usage);
  - tamaño de trades.db + -wal + -shm;
  - bytes POR TABLA vía la virtual table `dbstat` (si está), con fallback a conteo de filas;
  - tamaño de /app/logs y sus archivos más grandes.

Con --clean-logs SÍ borra basura de logs (seguro con el bot ARRIBA): elimina *.log.gz
rotados y trunca el .log del día. NUNCA toca la DB (eso es rebuild_db.py, con el bot parado).

USO (dentro del container, read-only por defecto):
    python scripts/disk_triage.py
    python scripts/disk_triage.py --clean-logs      # libera logs, no toca la DB
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3

_LOG_DIR = os.environ.get("LOG_DIR", "/app/logs")

# Estado de trading (nunca se prunea) vs diagnóstico (candidato a borrar). Solo para el
# veredicto impreso — el script no borra filas.
_SACRED = {
    "trades",
    "portfolio_positions",
    "risk_events",
    "daily_pnl",
    "operational_state",
    "bot_runs",
    "analyst_verdicts",
}


def _db_path() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:////app/data/trades.db")
    return url.split("sqlite:///", 1)[-1] if "sqlite:///" in url else "/app/data/trades.db"


def _fmt(n: int) -> str:
    """Bytes → humano (GB/MB/KB) para que el veredicto se lea de un vistazo."""
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.2f} {unit}"
    return f"{n} B"


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _report_disk(src: str) -> None:
    mount = os.path.dirname(src) or "."
    try:
        usage = shutil.disk_usage(mount)
    except OSError as exc:
        print(f"  (no pude leer disk_usage de {mount}: {exc})")
        return
    pct = usage.used / usage.total * 100 if usage.total else 0
    print(f"MOUNT {mount}")
    print(
        f"  total={_fmt(usage.total)}  usado={_fmt(usage.used)} ({pct:.1f}%)  libre={_fmt(usage.free)}"
    )


def _report_db_files(src: str) -> None:
    print(f"\nDB {src}")
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = src + suffix
        size = _file_size(p)
        total += size
        if size or suffix == "":
            label = os.path.basename(p)
            print(f"  {label:24s} {_fmt(size)}")
    print(f"  {'TOTAL (db+wal+shm)':24s} {_fmt(total)}")


def _report_tables(src: str) -> None:
    """Bytes por tabla vía dbstat; si dbstat no está compilado, fallback a filas."""
    if not os.path.exists(src):
        print(f"\n(no existe {src})")
        return
    conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        try:
            rows = list(
                conn.execute(
                    "SELECT name, sum(pgsize) AS bytes FROM dbstat "
                    "GROUP BY name ORDER BY bytes DESC"
                )
            )
            print("\nBYTES POR TABLA (dbstat):")
            for name, byts in rows:
                tag = "" if name in _SACRED else "  ← diagnóstico (prunable)"
                if name.startswith("sqlite_") or name.startswith("idx_") or name.startswith("ix_"):
                    tag = ""
                print(f"  {name:30s} {_fmt(int(byts or 0)):>12s}{tag}")
        except sqlite3.OperationalError:
            # dbstat no compilado en este SQLite → conteo de filas como proxy.
            print("\n(dbstat no disponible — muestro CONTEO DE FILAS por tabla como proxy):")
            tables = [
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            ]
            counts = []
            for t in tables:
                try:
                    n = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608
                except sqlite3.OperationalError:
                    n = -1
                counts.append((t, n))
            for t, n in sorted(counts, key=lambda x: x[1], reverse=True):
                tag = "" if t in _SACRED else "  ← diagnóstico (prunable)"
                print(f"  {t:30s} {n:>12,} filas{tag}")
    finally:
        conn.close()


def _report_logs(clean: bool) -> None:
    if not os.path.isdir(_LOG_DIR):
        print(f"\nLOGS {_LOG_DIR}: (no existe)")
        return
    entries: list[tuple[str, int]] = []
    for root, _dirs, files in os.walk(_LOG_DIR):
        for f in files:
            p = os.path.join(root, f)
            entries.append((p, _file_size(p)))
    total = sum(s for _, s in entries)
    print(f"\nLOGS {_LOG_DIR}  total={_fmt(total)}  ({len(entries)} archivos)")
    for p, s in sorted(entries, key=lambda x: x[1], reverse=True)[:10]:
        print(f"  {_fmt(s):>10s}  {p}")

    if not clean:
        gz = [p for p, _ in entries if p.endswith(".gz")]
        if gz:
            reclaim = sum(_file_size(p) for p in gz)
            print(
                f"  → --clean-logs borraría {len(gz)} .gz rotados ({_fmt(reclaim)}) + truncaría el .log del día"
            )
        return

    # --clean-logs: seguro con el bot arriba (no toca la DB). Borra .gz y trunca .log.
    freed = 0
    for p, s in entries:
        if p.endswith(".gz"):
            try:
                os.remove(p)
                freed += s
            except OSError as exc:
                print(f"  (no pude borrar {p}: {exc})")
    for p, s in entries:
        if p.endswith(".log") and s > 0:
            try:
                # Truncar in-place: el proceso que escribe con append sigue vivo, el fd no se rompe.
                with open(p, "r+") as fh:
                    fh.truncate(0)
                freed += s
            except OSError as exc:
                print(f"  (no pude truncar {p}: {exc})")
    print(f"  ✓ liberado de logs: {_fmt(freed)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Triage de disco read-only (opcional: limpiar logs)."
    )
    parser.add_argument(
        "--clean-logs",
        action="store_true",
        help="Borra *.log.gz rotados y trunca el .log del día (seguro con el bot arriba; NO toca la DB).",
    )
    args = parser.parse_args()

    src = _db_path()
    print("=" * 72)
    print("TRIAGE DE DISCO (read-only salvo --clean-logs)")
    print("=" * 72)
    _report_disk(src)
    _report_db_files(src)
    _report_tables(src)
    _report_logs(clean=args.clean_logs)

    print("\n" + "=" * 72)
    print("QUÉ HACER CON LO QUE VES:")
    print("  1. Logs: --clean-logs libera ya, sin parar el bot ni tocar la DB.")
    print("  2. Si el gigante es orderbook_events (o el .db pesa GB): la DB NO se achica")
    print("     borrando filas — hay que RECONSTRUIR: scripts/rebuild_db.py (bot PARADO).")
    print("  3. Cruft de Docker en el HOST (imágenes/cache de cada deploy): docker system df")
    print("     y docker system prune -af  (fuera del container, no afecta al bot vivo).")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
