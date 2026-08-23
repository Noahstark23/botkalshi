#!/usr/bin/env python3
"""
Reconstruye trades.db a una DB NUEVA y chica — reclama el disco inflado por
orderbook_events (incidente 2026-07-10: 54G).

POR QUÉ este script y no un VACUUM: el full VACUUM bloquea la DB entera y necesita ~2× el
tamaño en disco libre → con el disco lleno SE NIEGA, no reclama nada. Este script en cambio
crea una DB nueva copiando SOLO lo que importa (todo el estado de trading + diagnóstico
reciente), DROPEANDO orderbook_events (los 54G). La nueva DB pesa MB. Además la crea con
`auto_vacuum=INCREMENTAL` para que el loop de mantenimiento (incremental_vacuum) recupere
espacio ONLINE en el futuro y no haga falta nunca más un full VACUUM.

SEGURIDAD:
  - Read-only sobre la DB vieja (ATTACH mode=ro). No la toca.
  - NO swapea solo: crea trades.db.rebuilt y te imprime los comandos del swap manual, para
    que verifiques (trades + operational_state/kill-switch) ANTES de reemplazar.
  - Guarda de espacio: si hay poco disco libre, avisa (borrá logs .gz primero).

USO (con el bot DETENIDO en Coolify):
    python scripts/rebuild_db.py
    # verificá la salida, luego (manual):
    #   mv /app/data/trades.db /app/data/trades.db.OLD54G
    #   mv /app/data/trades.db.rebuilt /app/data/trades.db
    #   (borrá los -wal/-shm viejos del OLD si quedaron)
    #   restart en Coolify; con el bot sano un rato: rm trades.db.OLD54G
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

# Tablas de ESTADO DE TRADING: se copian ENTERAS (nunca se pierden).
_SACRED = [
    "trades",
    "portfolio_positions",
    "risk_events",
    "daily_pnl",
    "operational_state",  # kill-switch
    "bot_runs",
    "analyst_verdicts",
    "mm_shadow_fills",  # ledger F1: pérdidas y trayectoria nunca se podan al reconstruir
    "mm_experiment_runs",  # cadena de custodia: una invalidación nunca puede expirar
]
# Tablas de DIAGNÓSTICO: se copian solo las filas dentro de la ventana (misma retención que
# src/storage/maintenance._RETENTION_DAYS). orderbook_events NO está → se crea vacía (drop 54G).
_DIAG_RETENTION = {
    "edge_windows": ("created_at", 30),
    "motor2_funnel_snapshots": ("created_at", 14),
    "market_snapshots": ("captured_at", 7),
    "mm_quotes": ("created_at", 7),
    "mm_funnel_snapshots": ("created_at", 7),
}
_MIN_FREE_BYTES = 500 * 1024 * 1024  # 500 MB de headroom para la DB nueva (chica)


def _db_path() -> str:
    url = os.environ.get("DATABASE_URL", "sqlite:////app/data/trades.db")
    # sqlite:////abs → /abs ; sqlite:///rel → rel
    return url.split("sqlite:///", 1)[-1] if "sqlite:///" in url else "/app/data/trades.db"


def main() -> int:
    src = _db_path()
    if not os.path.exists(src):
        print(f"ERROR: no existe la DB en {src}")
        return 1
    dst = src + ".rebuilt"
    if os.path.exists(dst):
        print(f"ERROR: ya existe {dst} — borralo o renombralo antes de correr de nuevo.")
        return 1

    free = shutil.disk_usage(os.path.dirname(src) or ".").free
    print(f"DB vieja: {src}  ({os.path.getsize(src) / 1e9:.2f} GB)")
    print(f"Disco libre: {free / 1e9:.2f} GB")
    if free < _MIN_FREE_BYTES:
        print(
            f"ERROR: menos de {_MIN_FREE_BYTES / 1e6:.0f} MB libres. Liberá headroom primero:\n"
            "  rm -f /app/logs/*.log.gz\n"
            "  (la DB nueva es chica, con ~500 MB alcanza)"
        )
        return 1

    print(f"\nCreando DB nueva con auto_vacuum=INCREMENTAL → {dst}")
    src_uri = Path(src).resolve().as_uri()
    dst_uri = Path(dst).resolve().as_uri()
    new = sqlite3.connect(dst_uri, uri=True)
    old = sqlite3.connect(f"{src_uri}?mode=ro", uri=True)
    try:
        # auto_vacuum DEBE setearse antes de crear cualquier tabla, y persiste en el archivo.
        new.execute("PRAGMA auto_vacuum=INCREMENTAL")
        new.execute("PRAGMA journal_mode=WAL")

        # 1. Replicar el schema EXACTO de la DB vieja (tablas + índices), sea cual sea.
        tables = [
            (name, sql)
            for name, sql in old.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND sql IS NOT NULL"
            )
        ]
        for _name, sql in tables:
            new.execute(sql)
        for (sql,) in old.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        ):
            new.execute(sql)
        new.commit()

        # 2. Copiar filas: sagradas enteras, diagnóstico por ventana, orderbook_events NADA.
        new.execute("ATTACH DATABASE ? AS old", (f"{src_uri}?mode=ro",))
        table_names = {n for n, _ in tables}
        copied: dict[str, int] = {}
        for t in _SACRED:
            if t in table_names:
                new.execute(f"INSERT INTO main.{t} SELECT * FROM old.{t}")  # noqa: S608 (const)
                copied[t] = new.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
        for t, (col, days) in _DIAG_RETENTION.items():
            if t in table_names:
                new.execute(
                    f"INSERT INTO main.{t} SELECT * FROM old.{t} "  # noqa: S608 (const)
                    f"WHERE {col} >= datetime('now', '-{days} days')"
                )
                copied[t] = new.execute(f"SELECT count(*) FROM main.{t}").fetchone()[0]
        new.commit()
        new.execute("DETACH DATABASE old")
        new.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        new.commit()
    finally:
        new.close()
        old.close()

    new_size = os.path.getsize(dst)
    print("\n=== copiado (filas) ===")
    for t, n in copied.items():
        print(f"  {t:26s} {n}")
    print(f"\nDB nueva: {dst}  ({new_size / 1e6:.1f} MB)  ← vs {os.path.getsize(src) / 1e9:.2f} GB")

    # 3. Verificación crítica: los conteos de las tablas SAGRADAS en la nueva DB DEBEN
    #    coincidir EXACTO con la vieja. Si alguna perdió filas, la copia está mal → se ABORTA
    #    y NO se imprimen los comandos de swap (fail-safe: jamás reemplazar con estado incompleto).
    old_ro = sqlite3.connect(f"{src_uri}?mode=ro", uri=True)
    new_ro = sqlite3.connect(f"{dst_uri}?mode=ro", uri=True)
    mismatches: list[str] = []
    print("\n=== VERIFICACIÓN de tablas sagradas (nueva vs vieja) ===")
    try:
        for t in _SACRED:
            if t not in table_names:
                continue
            old_n = old_ro.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608 (const)
            new_n = new_ro.execute(f"SELECT count(*) FROM {t}").fetchone()[0]  # noqa: S608 (const)
            ok = "OK" if old_n == new_n else "← MISMATCH"
            print(f"  {t:22s} vieja={old_n}  nueva={new_n}  {ok}")
            if old_n != new_n:
                mismatches.append(f"{t}: vieja={old_n} nueva={new_n}")
        opstate = list(new_ro.execute("SELECT key, value, reason FROM operational_state"))
    finally:
        old_ro.close()
        new_ro.close()
    print(f"  operational_state (kill-switch): {opstate or '(vacío)'}")

    if mismatches:
        print(
            "\nERROR: la copia PERDIÓ filas sagradas — NO swapear.\n  "
            + "\n  ".join(mismatches)
            + f"\nLa DB vieja {src} está intacta. Borrá {dst} y reintentá."
        )
        return 1

    print(
        "\n=== SWAP MANUAL (verificación OK) ===\n"
        f"  mv {src} {src}.OLD\n"
        f"  mv {dst} {src}\n"
        "  # borrá los -wal/-shm viejos si quedaron:  rm -f "
        f"{src}.OLD-wal {src}.OLD-shm\n"
        "  # restart en Coolify; con el bot sano un rato:  rm "
        f"{src}.OLD\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
