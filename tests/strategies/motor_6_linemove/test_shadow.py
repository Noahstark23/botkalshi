"""
Motor 6 F1 SHADOW — line-move follower (tesis del funnel 2026-07-12).

Verifica: el detector puro (move → señal en banda, con fees reales), el shadow (persiste
EdgeWindow kind=linemove SOLO con odds live; foto previa siempre actualizada; best-effort
total), y el guard ESTRUCTURAL: en F1 el módulo ni siquiera importa el cliente de órdenes.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.math.fees import kalshi_fee_cents
from src.strategies.motor_2_consensus.detector import KalshiEventQuotes, KalshiQuote
from src.strategies.motor_6_linemove.detector import find_linemove_signals
from src.strategies.motor_6_linemove.shadow import Motor6LineMoveShadow


@pytest.fixture(autouse=True)
def sqlite_engine(tmp_path):
    """DB real temporal como singleton de models (el shadow persiste EdgeWindow)."""
    db = tmp_path / "m6.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


def _q(ticker: str = "KXWCGAME-26JUL15ARGFRA-ARG", yes_ask: int = 52, no_ask: int = 50):
    return KalshiQuote(
        market_ticker=ticker, outcome_name="Argentina", yes_ask_cents=yes_ask, no_ask_cents=no_ask
    )


def _events(*quotes: KalshiQuote) -> list[KalshiEventQuotes]:
    return [KalshiEventQuotes(event_key="KXWCGAME-26JUL15ARGFRA", outcomes=tuple(quotes))]


BAND = {"move_min_pp": 3.0, "edge_min_pp": 2.0, "max_edge_pp": 10.0}


# ── Detector (puro) ──────────────────────────────────────────────────────────


def test_upmove_with_lagging_ask_signals_yes():
    """MECANISMO: fair 0.50→0.56 (+6pp ≥ 3) con ask clavado en 52 → YES.
    net = 56 − 52 − fee(1,52)=2 → 2pp, justo en el piso (inclusive)."""
    q = _q(yes_ask=52)
    sigs = find_linemove_signals(
        {q.market_ticker: q}, {q.market_ticker: 0.56}, {q.market_ticker: 0.50}, **BAND
    )
    assert len(sigs) == 1
    s = sigs[0]
    assert s.side == "YES" and s.ask_cents == 52
    assert s.move_pp == pytest.approx(6.0)
    assert s.fee_cents == kalshi_fee_cents(1, 52)
    assert s.net_edge_pp == pytest.approx(56 - 52 - kalshi_fee_cents(1, 52))


def test_downmove_signals_no_side():
    """MECANISMO: fair 0.50→0.42 (−8pp) → NO contra el no_ask (prob complementaria)."""
    q = _q(yes_ask=48, no_ask=50)
    sigs = find_linemove_signals(
        {q.market_ticker: q}, {q.market_ticker: 0.42}, {q.market_ticker: 0.50}, **BAND
    )
    assert len(sigs) == 1
    assert sigs[0].side == "NO"
    assert sigs[0].net_edge_pp == pytest.approx((1 - 0.42) * 100 - 50 - kalshi_fee_cents(1, 50))


def test_small_move_is_noise():
    """CONTROL: +2pp < move_min 3 → ruido, sin señal (aunque el neto diera)."""
    q = _q(yes_ask=40)
    assert not find_linemove_signals(
        {q.market_ticker: q}, {q.market_ticker: 0.52}, {q.market_ticker: 0.50}, **BAND
    )


def test_digested_move_no_signal():
    """CONTROL: el move fue real (+6pp) pero Kalshi YA ajustó (ask=56) → neto negativo → nada.
    Comprar después del ajuste es comprar el pico."""
    q = _q(yes_ask=56)
    assert not find_linemove_signals(
        {q.market_ticker: q}, {q.market_ticker: 0.56}, {q.market_ticker: 0.50}, **BAND
    )


def test_phantom_edge_above_ceiling_discarded():
    """CONTROL anti-fantasma: neto enorme (> techo 10pp) = quote stale, no un regalo
    (lección 2026-06-16) → se descarta."""
    q = _q(yes_ask=30)  # fair 0.60 vs ask 30 → neto ~28pp
    assert not find_linemove_signals(
        {q.market_ticker: q}, {q.market_ticker: 0.60}, {q.market_ticker: 0.50}, **BAND
    )


def test_ticker_without_prev_or_quote_skipped():
    """CONTROL: outcome nuevo (sin foto previa) o sin quote actual → sin delta, sin señal."""
    q = _q()
    assert not find_linemove_signals({q.market_ticker: q}, {q.market_ticker: 0.56}, {}, **BAND)
    assert not find_linemove_signals({}, {q.market_ticker: 0.56}, {q.market_ticker: 0.50}, **BAND)


# ── Shadow (memoria + persistencia + best-effort) ────────────────────────────


def _shadow() -> Motor6LineMoveShadow:
    return Motor6LineMoveShadow(move_min_pp=3.0, edge_min_pp=2.0, max_edge_pp=10.0)


def _linemove_windows() -> list[models.EdgeWindow]:
    with models.get_session() as s:
        return [w for w in s.exec(select(models.EdgeWindow)).all() if w.kind == "linemove"]


def test_first_cycle_never_signals_and_seeds_prev():
    """El primer ciclo no tiene foto previa → 0 señales, pero siembra la foto."""
    sh = _shadow()
    q = _q(yes_ask=52)
    assert sh.observe(_events(q), {q.market_ticker: 0.50}, is_live=True) == []
    # Segundo ciclo: ahora sí hay delta.
    sigs = sh.observe(_events(q), {q.market_ticker: 0.56}, is_live=True)
    assert len(sigs) == 1


def test_persists_edgewindow_only_when_live():
    """Con odds LIVE la señal se graba (kind=linemove); con fixture fake NO (basura ≠ data)."""
    sh = _shadow()
    q = _q(yes_ask=52)
    sh.observe(_events(q), {q.market_ticker: 0.50}, is_live=False)
    sh.observe(_events(q), {q.market_ticker: 0.56}, is_live=False)  # señal, pero fake
    assert _linemove_windows() == []

    sh2 = _shadow()
    sh2.observe(_events(q), {q.market_ticker: 0.50}, is_live=True)
    sh2.observe(_events(q), {q.market_ticker: 0.56}, is_live=True)
    wins = _linemove_windows()
    assert len(wins) == 1
    assert wins[0].kind == "linemove" and wins[0].edge_pct == pytest.approx(2.0)


def test_observe_is_best_effort_and_still_updates_prev():
    """FAIL-SAFE: si el detector explota, observe devuelve [] (el ciclo de M2 sigue) y la
    foto previa IGUAL se actualiza (el próximo delta no abarca dos ciclos)."""
    sh = _shadow()
    q = _q()
    sh.observe(_events(q), {q.market_ticker: 0.50}, is_live=True)
    with patch(
        "src.strategies.motor_6_linemove.shadow.find_linemove_signals",
        side_effect=RuntimeError("boom"),
    ):
        assert sh.observe(_events(q), {q.market_ticker: 0.56}, is_live=True) == []
    assert sh._fair_prev == {q.market_ticker: 0.56}  # foto fresca pese al error


# ── Guard ESTRUCTURAL de F1 ──────────────────────────────────────────────────


def test_module_cannot_place_orders():
    """F1: el paquete de M6 no importa el cliente de órdenes ni conoce place_order.
    Si alguien agrega ejecución acá sin pasar por el diseño F3 (Capa A + RiskManager),
    este test lo frena."""
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[3] / "src" / "strategies" / "motor_6_linemove"
    for f in pkg.glob("*.py"):
        body = f.read_text()
        assert "kalshi_rest" not in body, f"{f.name} importa el cliente REST"
        assert "place_order" not in body, f"{f.name} referencia place_order"
        assert "KalshiRestClient" not in body, f"{f.name} referencia el cliente de órdenes"
