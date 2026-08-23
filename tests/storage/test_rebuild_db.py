"""
scripts/rebuild_db.py — reclama el disco inflado por orderbook_events (incidente 2026-07-10)
reconstruyendo a una DB nueva chica, sin full VACUUM (que no entra con el disco lleno).

Verifica: orderbook_events se DROPEA entero; el estado de trading (trades + kill-switch) se
copia COMPLETO; el diagnóstico se copia por ventana (reciente sí, viejo no); y la DB nueva
queda con auto_vacuum=INCREMENTAL (para el incremental_vacuum online del loop).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine

from src.storage.models import EdgeWindow, MMShadowFill, OperationalState, OrderbookEvent, Trade

NOW = datetime.utcnow()


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """DB temporal con el schema real, sembrada con el gigante + estado de trading."""
    db = tmp_path / "trades.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    eng = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        for _ in range(1000):  # el "gigante" que infla el disco
            s.add(OrderbookEvent(ticker="T", side="yes", price_cents=40, delta=1))
        s.add(
            Trade(
                client_order_id="keepme",
                ticker="T",
                side="yes",
                action="buy",
                count=1,
                price_cents=40,
                strategy="motor_1_arbitrage",
                status="settled",
            )
        )
        s.add(
            EdgeWindow(
                kind="consensus",
                market_ticker="T",
                magnitude_cents=1,
                created_at=NOW - timedelta(days=90),
            )
        )  # viejo → NO se copia
        s.add(
            EdgeWindow(
                kind="consensus",
                market_ticker="T",
                magnitude_cents=1,
                created_at=NOW - timedelta(hours=1),
            )
        )  # reciente → sí
        s.add(OperationalState(key="kill_switch", value="engaged", reason="test-kill"))
        s.add(
            MMShadowFill(
                ticker="KXMLBGAME-OLD-YES",
                side="buy",
                price_cents=40,
                count=1,
                rule="ask 39 < bid 40",
                created_at=NOW - timedelta(days=90),
            )
        )
        s.add(
            MMShadowFill(
                ticker="KXMLBGAME-NEW-YES",
                side="buy",
                price_cents=40,
                count=1,
                rule="ask 39 < bid 40",
                created_at=NOW - timedelta(hours=1),
            )
        )
        s.commit()
    eng.dispose()
    return db


def test_rebuild_drops_orderbook_keeps_trading_state(seeded_db):
    import scripts.rebuild_db as rb

    assert "mm_shadow_fills" not in rb._DIAG_RETENTION
    assert "mm_shadow_fills" in rb._SACRED
    assert "mm_experiment_runs" in rb._SACRED
    assert rb.main() == 0
    new = sqlite3.connect(f"{seeded_db}.rebuilt")
    try:
        assert (
            new.execute("SELECT count(*) FROM orderbook_events").fetchone()[0] == 0
        )  # los 1000 DROPEADOS
        assert (
            new.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
        )  # trading state INTACTO
        assert (
            new.execute("SELECT count(*) FROM edge_windows").fetchone()[0] == 1
        )  # solo el reciente
        assert new.execute("SELECT count(*) FROM mm_shadow_fills").fetchone()[0] == 2
        ks = list(new.execute("SELECT key, value FROM operational_state"))
        assert ks == [("kill_switch", "engaged")]  # kill-switch preservado
        # auto_vacuum=INCREMENTAL (2) → el incremental_vacuum del loop recupera online
        assert new.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    finally:
        new.close()


def test_rebuild_refuses_if_dst_exists(seeded_db):
    """No pisa un .rebuilt previo (evita clobber de una reconstrucción a medio hacer)."""
    import scripts.rebuild_db as rb

    (seeded_db.parent / "trades.db.rebuilt").write_text("x")
    assert rb.main() == 1


def test_rebuild_verifies_sacred_counts_and_prints_swap(seeded_db, capsys):
    """La copia buena verifica cada tabla sagrada nueva==vieja (OK) e imprime los comandos de
    swap. Con trades vieja=1 y nueva=1, la verificación pasa y aparece el bloque de swap."""
    import scripts.rebuild_db as rb

    assert rb.main() == 0
    out = capsys.readouterr().out
    assert "VERIFICACIÓN de tablas sagradas" in out
    assert "trades" in out and "OK" in out
    assert "SWAP MANUAL (verificación OK)" in out  # solo se imprime si NO hubo mismatch
