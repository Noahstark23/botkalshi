"""
Tests del Motor3Engine (Motor 3) — FASE 1 QA: auditoría de Capa A (shadow vs ejecución).

Verifica el invariante de seguridad: con TRADING_ENABLED=false NO se construye cliente
REST de órdenes ni executor, pero el detector SÍ loguea el trigger (shadow). Con
TRADING_ENABLED=true sí se construye el executor (Capa A).
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from loguru import logger
from sqlmodel import select

from src.storage.models import PortfolioPosition, Trade, get_session
from src.strategies.motor_3_clv.engine import Motor3Engine


def _due_pos(ticker: str = "KXDUE", count: int = 10) -> PortfolioPosition:
    """Posición en la ventana de salida (close_time ~29 min en el futuro, naive UTC)."""
    close = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=29)
    return PortfolioPosition(ticker=ticker, side="yes", count=count, close_time=close)


@contextmanager
def _fake_session(positions: list[PortfolioPosition]):
    s = MagicMock()
    s.exec.return_value = positions
    yield s


@contextmanager
def _patch_db(positions: list[PortfolioPosition]):
    """Sesión fake + bypass del filtro de atribución: estos tests validan la MECÁNICA del
    tick (shadow/Capa A/TP/dedup) con una sesión MagicMock que responde a cualquier query;
    la atribución real (skip de legs rest_arb / posiciones ajenas) se testea aparte con DB
    real en test_engine_attribution.py."""
    with (
        patch(
            "src.strategies.motor_3_clv.engine.get_session", new=lambda: _fake_session(positions)
        ),
        patch.object(Motor3Engine, "_attributable_positions", new=lambda self, ps: ps),
    ):
        yield


def _fake_client_ctx():
    """KalshiRestClient como async context manager fake (devuelve un cliente MagicMock)."""
    fake_client = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx, fake_client


@pytest.mark.asyncio
async def test_capa_a_no_executor_when_trading_disabled():
    """
    TRADING_ENABLED=false → NO se construye el executor (Capa A intacta: imposible vender).

    FASE 1: el cliente REST SÍ se abre ahora (read-only) porque el take-profit necesita leer
    el orderbook aun en shadow; la protección es la ausencia del executor, no la del cliente.
    """
    stop = asyncio.Event()
    stop.set()  # salir del loop inmediatamente
    eng = Motor3Engine(trading_enabled=False)
    ctx, _ = _fake_client_ctx()
    with patch("src.strategies.motor_3_clv.engine.KalshiRestClient", return_value=ctx):
        await eng.run(stop)
    assert eng._executor is None  # nunca hay executor → imposible vender en shadow


@pytest.mark.asyncio
async def test_capa_a_builds_executor_when_trading_enabled():
    """TRADING_ENABLED=true → SÍ se construye el executor (Capa A) con su cliente REST."""
    stop = asyncio.Event()
    stop.set()
    eng = Motor3Engine(trading_enabled=True)
    fake_client = MagicMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=fake_client)
    ctx.__aexit__ = AsyncMock(return_value=None)
    with patch(
        "src.strategies.motor_3_clv.engine.KalshiRestClient", return_value=ctx
    ) as mock_client:
        await eng.run(stop)
    mock_client.assert_called_once()  # se instanció el cliente de órdenes
    assert eng._executor is not None  # executor construido (Capa A)


@pytest.mark.asyncio
async def test_shadow_tick_logs_detection_but_never_sells():
    """Shadow (executor None): el detector LOGUEA el trigger pero no se ejecuta venta."""
    eng = Motor3Engine(trading_enabled=False)
    eng._poller.sync_once = AsyncMock(return_value=1)  # no clobber DB
    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        with _patch_db([_due_pos("KXSHADOW")]):
            await eng._tick()
    finally:
        logger.remove(sink)
    assert any("[MOTOR 3 SHADOW] CLV Exit" in m and "KXSHADOW" in m for m in captured)
    assert eng._executor is None  # nunca hubo executor → imposible vender


@pytest.mark.asyncio
async def test_partial_fill_reattempts_remainder_next_tick():
    """
    FASE 3 (state loop): tras un fill parcial (10→6), el SIGUIENTE tick re-detecta (sigue
    en ventana) y reintenta el remanente. Se simula el re-sync del poller actualizando la
    PortfolioPosition a 6 tras la primera venta.
    """
    close = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=29)
    with get_session() as s:
        s.add(PortfolioPosition(ticker="KXP", side="yes", count=10, close_time=close))
        # Pata BUY que respalda la posición: sin ella, el filtro de atribución la skipea
        # (posiciones sin filas propias no se gestionan — fix auditoría 2026-07-01).
        s.add(
            Trade(
                client_order_id="KXP-buy",
                ticker="KXP",
                side="yes",
                action="buy",
                count=10,
                price_cents=50,
                fill_price_cents=50,
                strategy="motor_2_consensus",
                status="filled",
            )
        )
        s.commit()

    eng = Motor3Engine(trading_enabled=False)
    eng._poller.sync_once = AsyncMock()  # el side-effect del executor simula el re-sync
    seen_counts: list[int] = []

    async def _fake_exit(position):
        seen_counts.append(position.count)
        if (
            position.count == 10
        ):  # primer intento: vende 4 → quedan 6 (lo que sincronizaría el poller)
            with get_session() as s:
                row = s.exec(
                    select(PortfolioPosition).where(PortfolioPosition.ticker == "KXP")
                ).first()
                row.count = 6
                s.add(row)
                s.commit()
        return MagicMock()

    eng._executor = MagicMock()
    eng._executor.exit_position = AsyncMock(side_effect=_fake_exit)

    await eng._tick()  # vende parcial → posición queda en 6
    await eng._tick()  # reintenta el remanente
    assert seen_counts == [10, 6]  # segundo intento sobre los 6 restantes


# =========================================================
# FASE 1 — Take-profit por precio
# =========================================================


def _far_pos(ticker: str = "KXTP", *, count: int = 10, side: str = "yes") -> PortfolioPosition:
    """Posición FUERA de la ventana de tiempo (2h al cierre) → solo el take-profit puede salir."""
    close = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=2)
    return PortfolioPosition(ticker=ticker, side=side, count=count, close_time=close)


def _orderbook(side: str, bid_cents: int):
    """Orderbook mínimo con un bid en el lado dado (formato [["0.95","100"], ...])."""
    return {"orderbook": {side: [[f"0.{bid_cents:02d}", "100.00"]]}}


@pytest.mark.asyncio
async def test_take_profit_shadow_detects_but_never_sells():
    """TP en shadow (executor None): bid≥umbral LOGUEA [MOTOR 3 TP SHADOW] pero no vende."""
    eng = Motor3Engine(trading_enabled=False, take_profit_enabled=True, tp_threshold=90)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 95))

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        with _patch_db([_far_pos("KXTP")]):
            await eng._tick()
    finally:
        logger.remove(sink)

    assert any("[MOTOR 3 TP SHADOW]" in m and "KXTP" in m for m in captured)
    assert eng._executor is None  # imposible vender


@pytest.mark.asyncio
async def test_take_profit_below_threshold_no_trigger():
    """bid < umbral → no se loguea TP ni se vende (aunque haya executor)."""
    eng = Motor3Engine(trading_enabled=False, take_profit_enabled=True, tp_threshold=90)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 80))
    eng._executor = MagicMock()
    eng._executor.exit_position = AsyncMock()

    with _patch_db([_far_pos("KXLOW")]):
        await eng._tick()

    eng._executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_take_profit_executes_when_executor_present():
    """Con executor (trading on): bid≥umbral → exit_position se invoca para la posición TP."""
    eng = Motor3Engine(trading_enabled=True, take_profit_enabled=True, tp_threshold=90)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 92))
    eng._executor = MagicMock()
    eng._executor.exit_position = AsyncMock()

    with _patch_db([_far_pos("KXSELL")]):
        await eng._tick()

    eng._executor.exit_position.assert_awaited_once()
    assert eng._executor.exit_position.call_args.args[0].ticker == "KXSELL"


@pytest.mark.asyncio
async def test_take_profit_dedupes_with_time_exit():
    """Una posición debida por tiempo Y por take-profit se liquida UNA sola vez (dedup)."""
    eng = Motor3Engine(trading_enabled=True, take_profit_enabled=True, tp_threshold=90)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 95))
    eng._executor = MagicMock()
    eng._executor.exit_position = AsyncMock()

    # _due_pos está en la ventana de tiempo (29 min) Y su bid (95) supera el umbral.
    with _patch_db([_due_pos("KXBOTH")]):
        await eng._tick()

    eng._executor.exit_position.assert_awaited_once()  # NO dos veces


@pytest.mark.asyncio
async def test_take_profit_orderbook_error_is_failsafe():
    """Lección 7: un error de orderbook loguea y NO rompe el tick ni dispara la salida."""
    eng = Motor3Engine(trading_enabled=True, take_profit_enabled=True, tp_threshold=90)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(side_effect=RuntimeError("kalshi down"))
    eng._executor = MagicMock()
    eng._executor.exit_position = AsyncMock()

    with _patch_db([_far_pos("KXERR")]):
        await eng._tick()  # no debe levantar

    eng._executor.exit_position.assert_not_called()  # bid None → no salida


# =========================================================
# FASE 2 — Trailing stop (DB real del conftest: _entry_bid_for/_persist_peak consultan get_session)
# =========================================================


def _seed_position(ticker: str, *, side="yes", count=10, peak=None, minutes_to_close=120) -> None:
    close = datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=minutes_to_close)
    with get_session() as s:
        s.add(
            PortfolioPosition(
                ticker=ticker, side=side, count=count, close_time=close, peak_bid_cents=peak
            )
        )
        s.commit()


def _seed_entry(ticker: str, *, side="yes", price=60) -> None:
    with get_session() as s:
        s.add(
            Trade(
                client_order_id=f"{ticker}-buy",
                ticker=ticker,
                side=side,
                action="buy",
                count=10,
                price_cents=price,
                fill_price_cents=price,
                strategy="motor_2_consensus",
                status="filled",
            )
        )
        s.commit()


@pytest.mark.asyncio
async def test_trailing_shadow_detects_but_never_sells():
    """Shadow (executor None): pico 90, bid 84 (retroceso 6≥5) → [MOTOR 3 TRAIL SHADOW], no vende."""
    eng = Motor3Engine(trading_enabled=False, trailing_enabled=True, trailing_drop=5)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 84))
    _seed_position("KXTR", peak=90)
    _seed_entry("KXTR", price=60)

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        await eng._tick()
    finally:
        logger.remove(sink)

    assert any("[MOTOR 3 TRAIL SHADOW]" in m and "KXTR" in m for m in captured)
    assert eng._executor is None


@pytest.mark.asyncio
async def test_trailing_persists_peak_between_ticks():
    """El pico se persiste en portfolio_positions: bid 80 sin pico previo → peak_bid_cents=80."""
    eng = Motor3Engine(trading_enabled=False, trailing_enabled=True, trailing_drop=5)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 80))
    _seed_position("KXPK", peak=None)
    _seed_entry("KXPK", price=60)

    await eng._tick()

    with get_session() as s:
        row = s.exec(select(PortfolioPosition).where(PortfolioPosition.ticker == "KXPK")).first()
    assert row.peak_bid_cents == 80  # max(entry 60, bid 80) → persistido


@pytest.mark.asyncio
async def test_trailing_entry_none_does_not_arm():
    """Fail-safe: sin pata BUY filled → entry None → trailing NO se arma (no vende)."""
    eng = Motor3Engine(trading_enabled=True, trailing_enabled=True, trailing_drop=5)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 84))
    eng._executor = MagicMock()
    eng._executor.exit_position = AsyncMock()
    _seed_position("KXNOENTRY", peak=90)  # pico alto pero SIN trade de entrada

    await eng._tick()

    eng._executor.exit_position.assert_not_called()


@pytest.mark.asyncio
async def test_trailing_executes_when_executor_present():
    """Con executor: pico 90, bid 84 (retroceso ≥drop) → exit_position de esa posición."""
    eng = Motor3Engine(trading_enabled=True, trailing_enabled=True, trailing_drop=5)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 84))
    eng._executor = MagicMock()
    eng._executor.exit_position = AsyncMock()
    _seed_position("KXTSELL", peak=90)
    _seed_entry("KXTSELL", price=60)

    await eng._tick()

    eng._executor.exit_position.assert_awaited_once()
    assert eng._executor.exit_position.call_args.args[0].ticker == "KXTSELL"


@pytest.mark.asyncio
async def test_trailing_dedupes_with_time_exit():
    """Posición debida por tiempo Y por trailing → una sola salida (dedup por el dict exits)."""
    eng = Motor3Engine(trading_enabled=True, trailing_enabled=True, trailing_drop=5)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 84))
    eng._executor = MagicMock()
    eng._executor.exit_position = AsyncMock()
    _seed_position("KXTB", peak=90, minutes_to_close=29)  # en ventana [28,30] Y trailing-due
    _seed_entry("KXTB", price=60)

    await eng._tick()

    eng._executor.exit_position.assert_awaited_once()


# =========================================================
# Shadow PnL neto de fees (validación "contando fees reales")
# =========================================================


@pytest.mark.asyncio
async def test_tp_shadow_logs_net_pnl_with_fees():
    """[MOTOR 3 TP SHADOW] incluye entry + net (PnL neto de fees) para validar contra el backtest."""
    eng = Motor3Engine(trading_enabled=False, take_profit_enabled=True, tp_threshold=62)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 62))
    _seed_position("KXTPP", peak=None)
    _seed_entry("KXTPP", price=54)  # entry 54¢, bid 62¢ → gross +0.80 sobre 10c, menos fees

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        await eng._tick()
    finally:
        logger.remove(sink)

    line = next(m for m in captured if "[MOTOR 3 TP SHADOW]" in m and "KXTPP" in m)
    assert "entry=54c" in line
    assert "gross=$+0.80" in line
    assert "net=$" in line  # neto de fees presente


@pytest.mark.asyncio
async def test_tp_shadow_pnl_handles_missing_entry():
    """Sin pata BUY (entry None) → el log no rompe: muestra entry=? net_pnl=?."""
    eng = Motor3Engine(trading_enabled=False, take_profit_enabled=True, tp_threshold=62)
    eng._poller.sync_once = AsyncMock()
    eng._client = MagicMock()
    eng._client.get_orderbook = AsyncMock(return_value=_orderbook("yes", 62))
    _seed_position("KXNOENT", peak=None)
    # Atribuible (tiene pata BUY) pero el entry no se pudo derivar (p.ej. error de DB en
    # _entry_bid_for): el log del TP shadow debe degradar a entry=? sin romper.
    _seed_entry("KXNOENT", price=60)
    eng._entry_bid_for = lambda p: None

    captured: list[str] = []
    sink = logger.add(lambda m: captured.append(str(m)), level="INFO")
    try:
        await eng._tick()
    finally:
        logger.remove(sink)

    line = next(m for m in captured if "[MOTOR 3 TP SHADOW]" in m and "KXNOENT" in m)
    assert "entry=?" in line
