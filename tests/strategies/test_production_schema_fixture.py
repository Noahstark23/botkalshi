"""El auditor read-only (infra/digitalocean-shadow/production_audit.py) se prueba contra
`tests/fixtures/production_schema.sql`, el DDL REAL compilado desde src/storage/models.py.
Si el modelo cambia y la fixture no, este test falla: el auditor nunca se valida contra un
esquema que producción ya no tiene. Regenerar con el mismo compilado que usa este test."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel

import src.storage.models  # noqa: F401  (registra las tablas en la metadata)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "digitalocean-shadow"
    / "tests"
    / "fixtures"
    / "production_schema.sql"
)
TABLES = ("trades", "operational_state", "risk_events", "portfolio_positions")


def test_audit_fixture_matches_the_real_models():
    engine = sa.create_engine("sqlite://")
    text = FIXTURE.read_text()
    for name in TABLES:
        ddl = str(CreateTable(SQLModel.metadata.tables[name]).compile(engine)).strip() + ";"
        assert ddl in text, f"fixture drifted from models for table {name}"
