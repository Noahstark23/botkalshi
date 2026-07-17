"""
Auditoría 2026-07-17 — el gap de la sangría gradual y sus dos frenos nuevos.

Contexto verificado: M2 acumuló −$430 repartidos entre junio y julio SIN que ninguna
ventana de CALENDARIO (diario/semanal/mensual, resetean lunes/día 1) cruzara su umbral
en un check individual — el freno funcionó como fue diseñado, y el diseño tiene un hueco.

Acá: (1) el test de REPRODUCCIÓN del gap (documenta que las ventanas de calendario no
atrapan la sangría a caballo del rollover), (2) el rolling drawdown que SÍ la atrapa,
(3) el gate SOFT de pérdida latente (MTM, flag off), y (4) la paridad freno↔status
(el "796% fantasma" del dashboard nació de dos matemáticas separadas).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from src.math.arbitrage import ArbLeg, ArbOpportunity
from src.monitoring.health import BotState
from src.risk.manager import RiskManager
from src.storage.models import Trade

# Reloj congelado: 2026-07-17 (viernes). month_start=Jul 1, week_start=lun Jul 13,
# rolling30_start=Jun 17. Determinístico: el repro no depende del día en que corra la suite.
_FROZEN_NOW = datetime(2026, 7, 17, 12, 0)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return _FROZEN_NOW.replace(tzinfo=tz) if tz else _FROZEN_NOW


@pytest.fixture(autouse=True)
def reset_state():
    BotState.is_paused = False
    BotState.pause_reason = None
    RiskManager._cached_capital_usd = None
    RiskManager._marks = {}
    RiskManager._unrealized_alert_date = None
    RiskManager._daily_stop_alert_date = None
    yield
    BotState.is_paused = False
    BotState.pause_reason = None
    RiskManager._cached_capital_usd = None
    RiskManager._marks = {}
    RiskManager._unrealized_alert_date = None
    RiskManager._daily_stop_alert_date = None


@pytest.fixture
def settings():
    """Capital estático $400 (determinístico) con TODO neutral salvo lo que cada test enciende.
    Límites: diario $12 / semanal $32 / mensual $60 / rolling 15% = $60."""
    with patch("src.risk.manager.get_settings") as m:
        s = MagicMock()
        s.DYNAMIC_CAPITAL_ENABLED = False
        s.ACTIVE_CAPITAL_USD = 400.0
        s.KALSHI_ENV = "demo"
        s.MAX_DAILY_LOSS_PCT = 3.0
        s.MAX_WEEKLY_LOSS_PCT = 8.0
        s.MAX_MONTHLY_LOSS_PCT = 15.0
        s.MAX_DAILY_LOSS_FLOOR_USD = 0.0
        s.MAX_WEEKLY_LOSS_FLOOR_USD = 0.0
        s.MAX_MONTHLY_LOSS_FLOOR_USD = 0.0
        s.DAILY_STOP_ENTRIES_ONLY = False
        s.ROLLING_DRAWDOWN_STOP_ENABLED = False
        s.MAX_ROLLING_DRAWDOWN_PCT = 15.0
        s.MAX_ROLLING_DRAWDOWN_DAYS = 30
        s.MAX_ROLLING_DRAWDOWN_FLOOR_USD = 0.0
        s.UNREALIZED_STOP_ENABLED = False
        s.MAX_UNREALIZED_LOSS_PCT = 10.0
        s.MAX_UNREALIZED_LOSS_FLOOR_USD = 0.0
        s.UNREALIZED_MARK_TTL_SEC = 900.0
        s.MAX_SIMULTANEOUS_EXPOSURE_PCT = 25.0
        s.MAX_TRADE_SIZE_PCT = 5.0
        s.MAX_TRADE_SIZE_USD = 200.0
        s.CAPITAL_FLOOR_USD = 1.0
        s.CAPITAL_CAP_USD = 100_000.0
        s.CAPITAL_SAFETY_FACTOR_PCT = 100.0
        s.telegram_configured = False
        m.return_value = s
        yield s


@pytest.fixture
def db(monkeypatch):
    import src.risk.manager as rm_module

    engine = create_engine("sqlite:///:memory:")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(rm_module, "get_session", lambda: Session(engine))
    return engine


@pytest.fixture
def opp():
    leg1 = ArbLeg(market_ticker="KX-T", side="yes", price_cents=40, count=10, available_size=50)
    leg2 = ArbLeg(market_ticker="KX-T", side="no", price_cents=45, count=10, available_size=50)
    return ArbOpportunity(
        legs=(leg1, leg2),
        count=10,
        gross_profit_cents=150,
        fees_cents=4,
        net_profit_cents=146,
        edge_pct=15.0,
    )


def _settled(db, coid: str, pnl_usd: float, settled_at: datetime) -> None:
    with Session(db) as s:
        s.add(
            Trade(
                client_order_id=coid,
                ticker="KX-BLEED",
                side="yes",
                action="buy",
                count=10,
                price_cents=50,
                strategy="motor_2_consensus",
                status="settled",
                pnl_cents=int(pnl_usd * 100),
                placed_at=settled_at - timedelta(hours=2),
                filled_at=settled_at - timedelta(hours=2),
                settled_at=settled_at,
            )
        )
        s.commit()


def _bleed_scenario(db) -> None:
    """La sangría real de M2, comprimida: −$300 el 25-jun (mes ANTERIOR) + −$50 el 10-jul
    (este mes, semana PASADA). Total rolling-30d: −$350. Ninguna ventana de calendario
    la ve entera: mensual jul = −$50 < $60; semanal (Jul 13+) = $0; diario = $0."""
    _settled(db, "bleed-jun", -300.0, datetime(2026, 6, 25, 18, 0))
    _settled(db, "bleed-jul", -50.0, datetime(2026, 7, 10, 18, 0))


# ── 1. REPRO del gap (tarea 1 del brief): el calendario NO atrapa la sangría ────────


@pytest.mark.asyncio
async def test_calendar_windows_miss_gradual_bleed(settings, db, opp):
    """DOCUMENTA EL GAP: −$350 en 30 días (87% del capital de riesgo mensual consumido
    en términos rolling) y las tres ventanas de calendario dicen OK — la pérdida grande
    quedó en el mes/semana ANTERIOR. El trade se aprueba. Este test es la evidencia
    de por qué existe el rolling; si alguien 'arregla' las ventanas de calendario y
    esto empieza a fallar, está cambiando semántica documentada (avisar al owner)."""
    _bleed_scenario(db)
    with patch("src.risk.manager.datetime", _FrozenDatetime):
        decision = await RiskManager().check_pre_trade(opp)
    assert decision.approved is True
    assert BotState.is_paused is False


@pytest.mark.asyncio
async def test_rolling_drawdown_catches_the_same_bleed(settings, db, opp):
    """MECANISMO: mismo escenario, rolling ON → −$350 ≥ límite $60 (15% de $400) →
    kill-switch persistente + rechazo. Lo que el calendario dejó pasar, la ventana
    móvil lo frena."""
    settings.ROLLING_DRAWDOWN_STOP_ENABLED = True
    _bleed_scenario(db)
    with (
        patch("src.risk.manager.datetime", _FrozenDatetime),
        patch("src.risk.manager.alert_risk_event", new_callable=AsyncMock),
        patch("src.risk.manager.engage_kill_switch"),
    ):
        decision = await RiskManager().check_pre_trade(opp)
    assert decision.approved is False
    assert "Rolling30d" in decision.reason
    assert BotState.is_paused is True


@pytest.mark.asyncio
async def test_rolling_window_evicts_old_losses(settings, db, opp):
    """CONTROL: la misma pérdida grande con >30 días de antigüedad queda FUERA de la
    ventana → no frena (el rolling rueda, no acumula para siempre)."""
    settings.ROLLING_DRAWDOWN_STOP_ENABLED = True
    _settled(db, "old-loss", -300.0, _FROZEN_NOW - timedelta(days=35))
    with patch("src.risk.manager.datetime", _FrozenDatetime):
        decision = await RiskManager().check_pre_trade(opp)
    assert decision.approved is True
    assert BotState.is_paused is False


@pytest.mark.asyncio
async def test_rolling_disabled_is_observability_only(settings, db, opp):
    """CONTROL (default off): con el flag apagado el rolling se COMPUTA (status lo
    muestra con gate_disabled) pero JAMÁS frena — activarlo es decisión del operador."""
    _bleed_scenario(db)
    with patch("src.risk.manager.datetime", _FrozenDatetime):
        rm = RiskManager()
        decision = await rm.check_pre_trade(opp)
        status = rm.stop_loss_status()
    assert decision.approved is True
    rolling = status["windows"][0]
    assert rolling["name"] == "Rolling30d" and rolling["gate_disabled"] is True
    assert rolling["used_pct"] > 100  # el drawdown SE VE aunque no frene


# ── 2. Paridad freno ↔ status (la causa raíz del "796% fantasma") ───────────────────


@pytest.mark.asyncio
async def test_status_parity_with_the_brake(settings, db, opp):
    """El status read-only y el check usan LA MISMA matemática: si una ventana nuclear
    muestra used_pct ≥ 100, el check DEBE rechazar — y viceversa. (El dashboard viejo
    calculaba aparte, con ventanas rolling y sin pisos → 796% de un freno sano.)"""
    _settled(db, "week-loss", -33.0, _FROZEN_NOW - timedelta(days=2))  # semanal: 33 ≥ $32
    with (
        patch("src.risk.manager.datetime", _FrozenDatetime),
        patch("src.risk.manager.alert_risk_event", new_callable=AsyncMock),
        patch("src.risk.manager.engage_kill_switch"),
    ):
        rm = RiskManager()
        status = rm.stop_loss_status()
        decision = await rm.check_pre_trade(opp)
    weekly = next(w for w in status["windows"] if w["name"] == "Semanal")
    assert weekly["used_pct"] >= 100
    assert decision.approved is False and "Semanal" in decision.reason


@pytest.mark.asyncio
async def test_per_motor_breakdown_in_status(settings, db):
    """Tarea 4 (observabilidad): el neto global ENMASCARA qué motor sangra — el status
    desglosa el PnL del mes por strategy."""
    _settled(db, "m2-loss", -50.0, datetime(2026, 7, 10, 18, 0))
    with Session(db) as s:
        s.add(
            Trade(
                client_order_id="m1-win",
                ticker="KX-W",
                side="yes",
                action="buy",
                count=5,
                price_cents=50,
                strategy="motor_1_arbitrage",
                status="settled",
                pnl_cents=3400,
                placed_at=datetime(2026, 7, 12, 10, 0),
                filled_at=datetime(2026, 7, 12, 10, 0),
                settled_at=datetime(2026, 7, 12, 11, 0),
            )
        )
        s.commit()
    with patch("src.risk.manager.datetime", _FrozenDatetime):
        status = RiskManager().stop_loss_status()
    assert status["per_motor_month"]["motor_2_consensus"] == pytest.approx(-50.0)
    assert status["per_motor_month"]["motor_1_arbitrage"] == pytest.approx(34.0)


# ── 3. Gate SOFT de pérdida latente (MTM) — flag default OFF ────────────────────────


def _open_filled(db, coid: str, ticker: str, price: int, count: int) -> None:
    with Session(db) as s:
        s.add(
            Trade(
                client_order_id=coid,
                ticker=ticker,
                side="yes",
                action="buy",
                count=count,
                price_cents=price,
                strategy="motor_2_consensus",
                status="filled",
                placed_at=_FROZEN_NOW - timedelta(hours=3),
                filled_at=_FROZEN_NOW - timedelta(hours=3),
            )
        )
        s.commit()


@pytest.mark.asyncio
async def test_unrealized_stop_pauses_entries_softly(settings, db, opp):
    """MECANISMO: posición 100×60¢ con mark en 15¢ → −$45 latente ≥ $40 (10% de $400)
    → entradas rechazadas SIN kill-switch ni BotState (soft, como el stop diario:
    las salidas siguen para poder CERRAR lo que sangra)."""
    settings.UNREALIZED_STOP_ENABLED = True
    _open_filled(db, "open-bleeding", "KX-MTM", 60, 100)
    with patch("src.risk.manager.datetime", _FrozenDatetime):
        RiskManager.record_mark("KX-MTM", "yes", 15)
        decision = await RiskManager().check_pre_trade(opp)
    assert decision.approved is False
    assert "latente" in decision.reason
    assert BotState.is_paused is False  # SOFT: sin kill-switch


@pytest.mark.asyncio
async def test_unrealized_without_fresh_mark_never_triggers(settings, db, opp):
    """FAIL-SAFE honesto: sin mark fresco la posición NO cuenta (cobertura parcial
    declarada > mark inventado) — un mark viejo (TTL vencido) tampoco."""
    settings.UNREALIZED_STOP_ENABLED = True
    settings.UNREALIZED_MARK_TTL_SEC = 60.0
    _open_filled(db, "open-nomark", "KX-MTM", 60, 100)
    stale = (15, _FROZEN_NOW - timedelta(minutes=30))  # 30min > TTL 60s
    with patch("src.risk.manager.datetime", _FrozenDatetime):
        RiskManager._marks[("KX-MTM", "yes")] = stale
        decision = await RiskManager().check_pre_trade(opp)
    assert decision.approved is True


@pytest.mark.asyncio
async def test_unrealized_skips_hedged_arb_legs(settings, db, opp):
    """CONTROL: una pata con arb_id (hedged) NO entra al MTM — su neto real ≈ 0 y el
    netting de exposición ya la descuenta; contarla acá doblaría el castigo."""
    settings.UNREALIZED_STOP_ENABLED = True
    with Session(db) as s:
        s.add(
            Trade(
                client_order_id="arb-leg",
                ticker="KX-MTM",
                side="yes",
                action="buy",
                count=100,
                price_cents=60,
                strategy="motor_rest_arb",
                status="filled",
                notes="arb_id=abc123",
                placed_at=_FROZEN_NOW - timedelta(hours=3),
                filled_at=_FROZEN_NOW - timedelta(hours=3),
            )
        )
        s.commit()
    with patch("src.risk.manager.datetime", _FrozenDatetime):
        RiskManager.record_mark("KX-MTM", "yes", 15)
        decision = await RiskManager().check_pre_trade(opp)
    assert decision.approved is True


@pytest.mark.asyncio
async def test_unrealized_disabled_by_default_even_with_marks(settings, db, opp):
    """CONTROL del flag: con UNREALIZED_STOP_ENABLED=False (default) ni la peor pérdida
    latente frena — la semántica realized-only del owner sigue intacta hasta que ÉL
    encienda el flag."""
    _open_filled(db, "open-bleeding", "KX-MTM", 60, 100)
    with patch("src.risk.manager.datetime", _FrozenDatetime):
        RiskManager.record_mark("KX-MTM", "yes", 5)
        decision = await RiskManager().check_pre_trade(opp)
    assert decision.approved is True
