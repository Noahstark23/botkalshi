"""
Mantenimiento de la DB — retención de tablas de DIAGNÓSTICO (incidente disco-lleno
2026-07-10).

El bot escribía sin tope tablas de telemetría (`orderbook_events` sobre todo: una fila por
delta del WS) y NUNCA las podaba → el disco se llenó y SQLite empezó a fallar cada write.
Este módulo acota esas tablas por ventana de tiempo; el loop del runner lo corre periódico.

REGLA DURA: solo se tocan tablas de DIAGNÓSTICO. Las de ESTADO DE TRADING —trades,
portfolio_positions, risk_events, daily_pnl, operational_state (kill-switch!), bot_runs,
analyst_verdicts— NUNCA se podan (no están en _RETENTION_DAYS). El borrado deja páginas en
la freelist de SQLite (reusadas por inserts futuros); wal_checkpoint(TRUNCATE) además achica
el archivo -wal. NO se hace VACUUM automático (bloquea la DB entera + necesita ~2× espacio):
eso es una acción manual de emergencia del operador.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import text

from src.storage.models import get_session

# tabla → (columna de timestamp NAIVE UTC, días a retener). SOLO diagnóstico.
# orderbook_events entra con ventana corta como red de seguridad si alguien prende
# PERSIST_ORDERBOOK_EVENTS; edge_windows retiene más (sustrato de análisis del shadow).
_RETENTION_DAYS: dict[str, tuple[str, int]] = {
    "orderbook_events": ("received_at", 2),
    "market_snapshots": ("captured_at", 7),
    "mm_quotes": ("created_at", 7),
    # mm_shadow_fills NO entra: es el ledger científico y la trayectoria de inventario
    # de una cohorte F1. Podarlo puede borrar pérdidas y convertir un FAIL en PASS.
    "fair_kickoff_snapshots": ("captured_at", 30),  # el "cierre" del CLV, mismo plazo
    "mm_funnel_snapshots": ("created_at", 7),
    "motor2_funnel_snapshots": ("created_at", 14),
    "edge_windows": ("created_at", 30),
}


def _naive_utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def prune_diagnostics(*, now: datetime | None = None) -> dict[str, int]:
    """Borra filas de diagnóstico más viejas que su ventana de retención. Devuelve
    {tabla: filas_borradas}. Best-effort por tabla: un error en una NO frena las demás
    (una tabla ausente/renombrada no debe romper el mantenimiento del resto)."""
    now = now or _naive_utc_now()
    deleted: dict[str, int] = {}
    for table, (col, days) in _RETENTION_DAYS.items():
        cutoff = now - timedelta(days=days)
        try:
            with get_session() as s:
                res = s.execute(
                    text(f"DELETE FROM {table} WHERE {col} < :cutoff"),  # noqa: S608 (tabla/col constantes)
                    {"cutoff": cutoff},
                )
                s.commit()
                deleted[table] = res.rowcount or 0
        except Exception:
            logger.exception(f"db_maintenance.prune_failed table={table} (se sigue con las demás)")
            deleted[table] = -1
    return deleted


def checkpoint_wal() -> None:
    """PRAGMA wal_checkpoint(TRUNCATE): vuelca el -wal al archivo principal y lo achica a 0.
    Sin esto, el -wal puede crecer y comer disco aunque las tablas estén acotadas. Best-effort."""
    try:
        with get_session() as s:
            s.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            s.commit()
    except Exception:
        logger.exception("db_maintenance.wal_checkpoint_failed")


def incremental_vacuum() -> None:
    """PRAGMA incremental_vacuum: devuelve al SO las páginas libres del freelist SIN el
    full VACUUM (que bloquea la DB entera + necesita ~2× espacio). Reclama ONLINE, en
    caliente, de a poco.

    IMPORTANTE: solo hace algo si la DB fue creada con auto_vacuum=INCREMENTAL (ver
    scripts/rebuild_db.py). En una DB con auto_vacuum=NONE (el default histórico, y las ya
    existentes) es un NO-OP inofensivo — por eso agregarlo al loop es seguro. Best-effort."""
    try:
        with get_session() as s:
            s.execute(text("PRAGMA incremental_vacuum"))
            s.commit()
    except Exception:
        logger.exception("db_maintenance.incremental_vacuum_failed")


def run_maintenance_once(*, now: datetime | None = None) -> dict[str, int]:
    """Una pasada de mantenimiento: poda + wal_checkpoint + incremental_vacuum. Devuelve las
    filas borradas. El incremental_vacuum recupera disco online SI la DB es auto_vacuum=
    INCREMENTAL (reconstruida con rebuild_db); si no, no-op."""
    deleted = prune_diagnostics(now=now)
    checkpoint_wal()
    incremental_vacuum()
    total = sum(v for v in deleted.values() if v > 0)
    if total:
        logger.info(f"db_maintenance.pruned total={total} por_tabla={deleted}")
    return deleted
