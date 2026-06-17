"""
Pre-flight de Motor 3 (CLV) — inyecta una posición a T-29min y corre un tick del engine.

Valida la cadena en SHADOW de punta a punta sin tocar Kalshi ni la DB de producción:
inserta una PortfolioPosition con close_time a 29 min (dentro de la ventana [28,30]),
corre un tick manual del engine (con el poller mockeado para no clobbear la posición),
y confirma que el detector la procesó y que en shadow se logueó la intención de salida
SIN ejecutar orden (executor=None).

DB: temporal y aislada (no toca /app/data). Read-only respecto a Kalshi.

Uso:
    python scripts/diag_motor3_clv.py
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlmodel import SQLModel, create_engine, select

import src.storage.models as models
from src.storage.models import PortfolioPosition, get_session
from src.strategies.motor_3_clv.detector import scan_exits
from src.strategies.motor_3_clv.engine import Motor3Engine

DIAG_TICKER = "KXDIAG-MOTOR3-CLV"


def _temp_db() -> None:
    """Engine SQLite temporal como global de models → aislado, sin tocar la DB de prod."""
    db = os.path.join(tempfile.gettempdir(), "motor3_preflight.db")
    models._engine = create_engine(f"sqlite:///{db}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(models._engine)


async def run() -> int:
    _temp_db()
    now = datetime.now(UTC).replace(tzinfo=None)
    close = now + timedelta(minutes=29)

    with get_session() as s:
        s.add(PortfolioPosition(ticker=DIAG_TICKER, side="yes", count=7, close_time=close))
        s.commit()
    print(f"Inyectada: {DIAG_TICKER} side=yes count=7 close_time={close} (T-29min, en ventana)")

    # Engine SHADOW (trading off → executor None). Poller mockeado: NO sincroniza contra
    # Kalshi (si lo hiciera, purgaría la posición inyectada por no estar en la cuenta real).
    eng = Motor3Engine(trading_enabled=False)

    async def _noop_sync() -> int:
        return 0

    eng._poller.sync_once = _noop_sync  # type: ignore[method-assign]

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        await eng._tick()  # corre detect_and_log (shadow); executor None → no vende
    finally:
        logger.remove(sink)

    with get_session() as s:
        positions = list(
            s.exec(select(PortfolioPosition).where(PortfolioPosition.ticker == DIAG_TICKER))
        )
    due = scan_exits(positions, now)
    shadow_logged = any("[MOTOR 3 SHADOW] CLV Exit" in m and DIAG_TICKER in m for m in captured)

    print(f"\nscan_exits → debidas: {[p.ticker for p in due]}")
    print(f"shadow-log de intención disparado: {shadow_logged}")
    print(f"executor construido (debe ser None en shadow): {eng._executor}")

    ok = bool(due) and shadow_logged and eng._executor is None
    print(f"\n{'=' * 60}")
    if ok:
        print("✅ PRE-FLIGHT VERDE")
        print("   El detector procesó la posición a T-29min; en shadow se logueó la intención")
        print("   de salida CLV y NO se ejecutó ninguna orden (executor=None, Capa A).")
        return 0
    print("❌ PRE-FLIGHT ROJO — la posición no fue detectada o el wiring del engine falla.")
    return 1


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
