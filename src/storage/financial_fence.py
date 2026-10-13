"""Revisión local opcional para recibos; no autoriza ni instala nada al importar.

Incluye UPDATE/ABA y cancel intents. No acredita atomicidad remota. La secuencia
satura: invalida las lecturas sin impedir escrituras protectoras al agotarse.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass

TABLE = "financial_fence"
MAX_SEQUENCE = 2**63 - 1
TRACKED = (
    "trades",
    "risk_commitments",
    "portfolio_positions",
    "motor_permission_admissions",
    "operational_state",
)
DDL = (
    "CREATE TABLE financial_fence (id INTEGER PRIMARY KEY CHECK(id=1), "
    "epoch TEXT NOT NULL, seq INTEGER NOT NULL CHECK(typeof(seq)='integer' AND seq>=0), "
    "tables_sha256 TEXT NOT NULL)"
)
TRIGGERS = {
    f"financial_fence_{table}_{action.lower()}": (
        f"CREATE TRIGGER financial_fence_{table}_{action.lower()} AFTER {action} ON {table} "
        f"BEGIN UPDATE financial_fence SET seq=CASE WHEN seq<{MAX_SEQUENCE} "
        "THEN seq+1 ELSE seq END WHERE id=1; END"
    )
    for table in TRACKED
    for action in ("INSERT", "UPDATE", "DELETE")
}
TRIGGERS.update(
    {
        "financial_fence_no_delete": (
            "CREATE TRIGGER financial_fence_no_delete BEFORE DELETE ON financial_fence "
            "BEGIN SELECT RAISE(ABORT,'FENCE_DELETE_FORBIDDEN'); END"
        ),
        "financial_fence_no_replace": (
            "CREATE TRIGGER financial_fence_no_replace BEFORE INSERT ON financial_fence "
            "WHEN EXISTS(SELECT 1 FROM financial_fence) "
            "BEGIN SELECT RAISE(ABORT,'FENCE_REPLACE_FORBIDDEN'); END"
        ),
        "financial_fence_monotonic": (
            "CREATE TRIGGER financial_fence_monotonic BEFORE UPDATE ON financial_fence "
            "WHEN NEW.id IS NOT OLD.id OR NEW.epoch IS NOT OLD.epoch "
            "OR NEW.tables_sha256 IS NOT OLD.tables_sha256 OR typeof(NEW.seq)!='integer' "
            f"OR NEW.seq!=CASE WHEN OLD.seq<{MAX_SEQUENCE} THEN OLD.seq+1 ELSE OLD.seq END "
            "BEGIN SELECT RAISE(ABORT,'FENCE_RESET_FORBIDDEN'); END"
        ),
    }
)


def require(value, code):
    if not value:
        raise ValueError(code)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _schema(db):
    return dict(db.execute("SELECT name,sql FROM sqlite_master"))


def _tables_digest(schema):
    require(all(schema.get(name) for name in TRACKED), "FENCE_TABLE_MISSING")
    return digest({name: schema[name] for name in TRACKED})


@dataclass(frozen=True)
class Fence:
    epoch: str
    sequence: int
    schema_version: int


def provision_fence(db: sqlite3.Connection, *, epoch: str):
    """Sólo bajo transacción explícita. Nunca repara ni reinicia un fence parcial."""
    require(db.in_transaction, "FENCE_TRANSACTION_REQUIRED")
    require(
        type(epoch) is str and len(epoch) == 32 and all(c in "0123456789abcdef" for c in epoch),
        "FENCE_INVALID_EPOCH",
    )
    schema = _schema(db)
    existing = {name for name in schema if name == TABLE or name.startswith(TABLE + "_")}
    if existing:
        value = read_fence(db)
        require(value.epoch == epoch, "FENCE_BINDING_CHANGED")
        return value
    tables_hash = _tables_digest(schema)
    db.execute(DDL)
    db.execute("INSERT INTO financial_fence VALUES(1,?,0,?)", (epoch, tables_hash))
    for sql in TRIGGERS.values():
        db.execute(sql)
    return read_fence(db)


def _checked_state(db: sqlite3.Connection):
    require(db.in_transaction, "FENCE_TRANSACTION_REQUIRED")
    schema = _schema(db)
    require(schema.get(TABLE) == DDL, "FENCE_SCHEMA_CHANGED")
    relevant = {
        name
        for name, table in db.execute(
            "SELECT name,tbl_name FROM sqlite_master WHERE type='trigger'"
        )
        if table in (*TRACKED, TABLE) or name.startswith(TABLE + "_")
    }
    require(relevant == set(TRIGGERS), "FENCE_TRIGGERS_CHANGED")
    require(
        all(schema.get(name) == sql for name, sql in TRIGGERS.items()), "FENCE_TRIGGERS_CHANGED"
    )
    rows = db.execute("SELECT id,epoch,seq,tables_sha256 FROM financial_fence").fetchall()
    require(len(rows) == 1 and rows[0][0] == 1, "FENCE_STATE_MISSING")
    _, epoch, sequence, tables_hash = rows[0]
    require(type(sequence) is int and 0 <= sequence < MAX_SEQUENCE, "FENCE_EXHAUSTED")
    return Fence(epoch, sequence, db.execute("PRAGMA schema_version").fetchone()[0]), tables_hash


def read_fence(db: sqlite3.Connection) -> Fence:
    """La misma conexión/transacción del caller; no modifica, autoflush ni commit."""
    value, tables_hash = _checked_state(db)
    require(tables_hash == _tables_digest(_schema(db)), "FENCE_TABLES_CHANGED")
    return value


def accept_reviewed_schema(db: sqlite3.Connection, *, previous: Fence, expected_tables_sha256: str):
    """Paso explícito de migración revisada; el caller controla commit/rollback.

    Debe correr con escritores detenidos y BEGIN IMMEDIATE. No restaura triggers
    perdidos ni infiere que una migración sea segura por tener columnas nuevas.
    El hash esperado se fija al revisar la migración, nunca del DB a ciegas.
    """
    current, old_hash = _checked_state(db)
    new_hash = _tables_digest(_schema(db))
    require(
        current.epoch == previous.epoch
        and current.sequence == previous.sequence
        and current.schema_version > previous.schema_version
        and new_hash != old_hash
        and new_hash == expected_tables_sha256,
        "FENCE_MIGRATION_MISMATCH",
    )
    require(current.sequence < MAX_SEQUENCE - 1, "FENCE_EXHAUSTED")
    # La única transición del sello de esquema se acota a esta transacción.
    # La época se conserva y la secuencia avanza; un recibo viejo no revive.
    db.execute("DROP TRIGGER financial_fence_monotonic")
    db.execute("UPDATE financial_fence SET tables_sha256=?,seq=seq+1 WHERE id=1", (new_hash,))
    db.execute(TRIGGERS["financial_fence_monotonic"])
    return read_fence(db)
