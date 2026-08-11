"""
Observabilidad del rollback y atribución de outcome (sonda de producción 2026-08-11).

Las primeras dos ejecuciones reales de M1 post-destrabe (23:15:09 y 23:15:16) dejaron
dos gaps que el agente web detectó leyendo el log en vivo:

1. El ÉXITO del rollback era el único desenlace silencioso: logueaban el sin-fill, el
   parcial y el CRITICAL sin cerrar — pero "vendió y realizó la pérdida" había que
   inferirlo de la ausencia de rollback_aborted + pausa. El éxito se afirma, no se
   deduce (misma regla que el resto de la observabilidad del proyecto).

2. La segunda ejecución salió con edge_id=None: el de-dupe anti-flood de EdgeWindow
   devolvía None para señales sin cambios, y _update_edge_window_outcome(None) es
   no-op → la fila de la señal ejecutada JAMÁS recibió su outcome. Ahora el de-dupe
   devuelve el id de la fila ORIGINAL (es la misma oportunidad; su outcome es de ella).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger
from sqlmodel import SQLModel, create_engine, select

import src.storage.models as _models
from src.math.arbitrage import detect_binary_arb
from src.risk.manager import RiskManager, TradeDecision
from src.storage.models import EdgeWindow, get_session
from src.strategies.motor_1_arbitrage.engine import Motor1Engine
from src.strategies.motor_1_arbitrage.executor import ArbitrageExecutor


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(_models, "_engine", engine)
    yield engine
    SQLModel.metadata.drop_all(engine)
    monkeypatch.setattr(_models, "_engine", None)


def _settings_trading() -> MagicMock:
    s = MagicMock()
    s.TRADING_ENABLED = True
    s.ACTIVE_CAPITAL_USD = 300.0
    s.MAX_TRADE_SIZE_PCT = 5.0
    s.MAX_EVENT_DIRECTIONAL_EXPOSURE_USD = 10_000.0
    return s


def _opp(ticker: str = "MKT-A"):
    opp = detect_binary_arb(ticker, 40, 200, 45, 200, max_count=10)
    assert opp is not None
    return opp


def _engine_shadow() -> Motor1Engine:
    settings = MagicMock()
    settings.TRADING_ENABLED = False
    settings.MIN_EDGE_PCT = 1.0
    settings.MOTOR_1_EXECUTION_EDGE_PCT = 1.0
    with patch("src.strategies.motor_1_arbitrage.engine.get_settings", return_value=settings):
        return Motor1Engine(MagicMock(), executor=None)


# =====================================================
# 2) De-dupe devuelve el id de la fila original
# =====================================================


def test_dedupe_devuelve_el_id_original_no_none():
    """LA SEGUNDA EJECUCIÓN DE LA HISTORIA (23:15:16, edge_id=None): una señal sin
    cambios que ejecuta debe poder atribuir su outcome a la fila ya grabada."""
    engine = _engine_shadow()
    opp = _opp("TICK")

    primera = engine._record_edge_window("TICK", opp)
    segunda = engine._record_edge_window("TICK", opp)  # sin cambios → de-dupe

    assert primera is not None
    assert segunda == primera  # la MISMA fila, no None

    engine._update_edge_window_outcome(segunda, False)
    with get_session() as s:
        rows = list(s.exec(select(EdgeWindow)))
    assert len(rows) == 1  # el anti-flood sigue: una sola fila


def test_dedupe_de_ticker_sin_fila_grabada_devuelve_none():
    """CONTROL best-effort: si la key quedó cacheada pero la fila jamás se grabó (fallo
    de DB), no se inventa atribución — None como antes."""
    engine = _engine_shadow()
    opp = _opp("TICK")
    engine._last_recorded["TICK"] = (opp.net_profit_cents, opp.count)  # key sin id

    assert engine._record_edge_window("TICK", opp) is None


# =====================================================
# 1) El éxito del rollback se afirma en el log
# =====================================================


@pytest.mark.asyncio
async def test_rollback_exitoso_loguea_vendido():
    client = AsyncMock()
    client.get_available_balance_usd.return_value = 10_000.0
    client.place_order.side_effect = [
        {"order": {"order_id": "k-no", "fill_count": 10}},  # NO (dura) llena
        Exception("fail"),  # YES (fácil) falla
        {"order": {"fill_count": 10}},  # rollback IOC llena
    ]
    client.get_orderbook.return_value = {"orderbook": {"no": [[44, 10]], "yes": []}}
    rm = MagicMock(spec=RiskManager)
    rm.check_pre_trade = AsyncMock(
        return_value=TradeDecision(approved=True, reason="ok", max_allowed_count=10)
    )
    executor = ArbitrageExecutor(client, rm)

    lineas: list[str] = []
    sink = logger.add(lambda m: lineas.append(str(m)), level="INFO")
    try:
        with patch(
            "src.strategies.motor_1_arbitrage.executor.get_settings",
            return_value=_settings_trading(),
        ):
            await executor.execute(_opp())
    finally:
        logger.remove(sink)

    vendidos = [ln for ln in lineas if "rollback: VENDIDO" in ln]
    assert len(vendidos) == 1
    assert "10x" in vendidos[0] and "@~44¢" in vendidos[0]  # count y precio contable
