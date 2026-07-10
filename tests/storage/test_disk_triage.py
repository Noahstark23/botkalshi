"""
scripts/disk_triage.py — triage de disco READ-ONLY (incidente 2026-07-10, disco 95%).

Verifica: el reporte corre sin tocar la DB (read-only) e imprime bytes/filas por tabla; y
--clean-logs borra *.log.gz y trunca el .log del día sin romper nada más (NUNCA la DB).
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlmodel import Session, SQLModel, create_engine

from src.storage.models import OrderbookEvent, Trade


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db}")
    eng = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(eng)
    with Session(eng) as s:
        for _ in range(50):
            s.add(OrderbookEvent(ticker="T", side="yes", price_cents=40, delta=1))
        s.add(
            Trade(
                client_order_id="k",
                ticker="T",
                side="yes",
                action="buy",
                count=1,
                price_cents=40,
                strategy="motor_1_arbitrage",
                status="settled",
            )
        )
        s.commit()
    eng.dispose()
    return db


def test_triage_is_readonly_and_reports_tables(seeded_db, monkeypatch, capsys):
    import sys

    import scripts.disk_triage as dt

    monkeypatch.setattr(sys, "argv", ["disk_triage.py"])
    assert dt.main() == 0
    out = capsys.readouterr().out
    assert "TRIAGE DE DISCO" in out
    assert "orderbook_events" in out  # aparece en el desglose por tabla
    # No modificó la DB: las 50 filas siguen ahí.
    conn = sqlite3.connect(str(seeded_db))
    try:
        assert conn.execute("SELECT count(*) FROM orderbook_events").fetchone()[0] == 50
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
    finally:
        conn.close()


def test_clean_logs_removes_gz_and_truncates_log(tmp_path, monkeypatch, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "bot.2026-07-08.log.gz").write_bytes(b"x" * 1000)
    live = logs / "bot.log"
    live.write_text("y" * 2000)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'nope.db'}")

    import sys

    import scripts.disk_triage as dt

    monkeypatch.setattr(dt, "_LOG_DIR", str(logs))
    monkeypatch.setattr(sys, "argv", ["disk_triage.py", "--clean-logs"])
    assert dt.main() == 0

    assert not (logs / "bot.2026-07-08.log.gz").exists()  # .gz borrado
    assert live.exists() and live.stat().st_size == 0  # .log truncado, no borrado
    assert "liberado de logs" in capsys.readouterr().out
