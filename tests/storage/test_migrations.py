"""
Tests de las migraciones idempotentes de ADD COLUMN (models.apply_migrations).

El primer ALTER de la historia del proyecto: la DB productiva tiene `edge_windows`
sin count/fees_cents/edge_pct (la tabla nació antes). create_all NO altera tablas
existentes → apply_migrations las agrega, idempotente, sin perder datos.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine

import src.storage.models as models


def _sqlite_engine(tmp_path):
    db = tmp_path / "mig.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    return create_engine(f"sqlite:///{db}"), settings


def _columns(engine, table: str) -> set[str]:
    with engine.begin() as conn:
        return models._existing_columns(conn, table)


def test_fresh_db_has_new_columns_and_migration_is_noop(tmp_path):
    """En DB nueva, create_all ya trae las columnas → apply_migrations no rompe nada."""
    engine, settings = _sqlite_engine(tmp_path)
    models.SQLModel.metadata.create_all(engine)
    cols_before = _columns(engine, "edge_windows")
    assert {"count", "fees_cents", "edge_pct"} <= cols_before  # create_all las creó

    with patch("src.storage.models.get_settings", return_value=settings):
        models.apply_migrations(engine)  # no debe lanzar ni duplicar
    assert _columns(engine, "edge_windows") == cols_before


def test_migration_adds_missing_columns_on_legacy_table(tmp_path):
    """Simula la DB productiva: edge_windows SIN las 3 columnas → la migración las agrega."""
    engine, settings = _sqlite_engine(tmp_path)
    # Tabla "vieja": el subconjunto de columnas que existían antes del cambio.
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE edge_windows ("
            "id INTEGER PRIMARY KEY, market_ticker TEXT, magnitude_cents INTEGER, "
            "gross_spread_cents INTEGER, created_at TEXT)"
        )
        conn.exec_driver_sql(
            "INSERT INTO edge_windows (market_ticker, magnitude_cents) VALUES ('KXOLD', 7)"
        )

    assert {"count", "fees_cents", "edge_pct"}.isdisjoint(_columns(engine, "edge_windows"))

    with patch("src.storage.models.get_settings", return_value=settings):
        models.apply_migrations(engine)

    cols = _columns(engine, "edge_windows")
    assert {"count", "fees_cents", "edge_pct"} <= cols
    # La fila vieja sobrevive; las columnas nuevas quedan NULL.
    with engine.begin() as conn:
        row = conn.exec_driver_sql(
            "SELECT market_ticker, magnitude_cents, count, fees_cents, edge_pct "
            "FROM edge_windows WHERE market_ticker='KXOLD'"
        ).fetchone()
    assert row == ("KXOLD", 7, None, None, None)


def test_migration_is_idempotent(tmp_path):
    """Correr la migración dos veces no lanza ni duplica columnas."""
    engine, settings = _sqlite_engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE edge_windows (id INTEGER PRIMARY KEY, market_ticker TEXT, "
            "magnitude_cents INTEGER)"
        )
    with patch("src.storage.models.get_settings", return_value=settings):
        models.apply_migrations(engine)
        cols_once = _columns(engine, "edge_windows")
        models.apply_migrations(engine)  # segunda pasada
        cols_twice = _columns(engine, "edge_windows")
    assert cols_once == cols_twice
    assert {"count", "fees_cents", "edge_pct"} <= cols_twice


def test_migration_skips_non_sqlite(tmp_path):
    """En un engine no-SQLite, apply_migrations no intenta nada (Alembic sería el camino)."""
    engine, _ = _sqlite_engine(tmp_path)
    settings = MagicMock()
    settings.DATABASE_URL = "postgresql://x/y"
    with patch("src.storage.models.get_settings", return_value=settings):
        models.apply_migrations(engine)  # no-op, no debe tocar el engine SQLite
    # edge_windows ni siquiera existe → si hubiera intentado ALTER, habría lanzado.
