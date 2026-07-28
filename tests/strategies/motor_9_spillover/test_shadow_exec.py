"""
Motor 9 — F2: medición EJECUTABLE del derrame (2026-07-28).

El veredicto del mid (+2.69¢, t=8.2, n=518) dejó UNA pregunta: ¿sobrevive al ask+fees?
Acá se verifica el mecanismo que la responde: al trigger se captura el ASK de entrada del
lado que un F3 compraría (inverso al salto); a T+60 el BID de salida; y se persiste una
fila kind='spillover_exec' con gross = bid60−ask0 y NETO por contrato tras fee de ida y
vuelta (kalshi_fee_cents a EXEC_COUNT). Las filas 'spillover' (mid) siguen intactas —
la DIFERENCIA entre ambas series mide cuánto se come el spread (lección REST arb).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.math.fees import kalshi_fee_cents
from src.storage.models import EdgeWindow
from src.strategies.motor_9_spillover.shadow import Motor9SpilloverShadow


@pytest.fixture(autouse=True)
def sqlite_engine(tmp_path):
    """DB real temporal como singleton de models (el shadow persiste EdgeWindow)."""
    db = tmp_path / "m9x.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


EVENT = "KXMLBGAME-26JUL28HOUWSH"
A = f"{EVENT}-HOU"  # el que salta
B = f"{EVENT}-WSH"  # el hermano (donde se mediría la captura)


class _Feed:
    """mid/quote controlables por test: dicts mutables ticker → valor."""

    def __init__(self) -> None:
        self.mids: dict[str, float | None] = {}
        self.quotes: dict[str, tuple[int, int] | None] = {}

    def mid(self, t: str) -> float | None:
        return self.mids.get(t)

    def quote(self, t: str) -> tuple[int, int] | None:
        return self.quotes.get(t)


def _shadow(feed: _Feed, *, with_quotes: bool = True) -> Motor9SpilloverShadow:
    return Motor9SpilloverShadow(
        feed.mid,
        lambda _t: {B},
        trigger_move_cents=5.0,
        window_sec=60.0,
        cooldown_sec=300.0,
        quote_fn=feed.quote if with_quotes else None,
    )


def _rows(kind: str) -> list[EdgeWindow]:
    with models.get_session() as s:
        return list(s.exec(select(EdgeWindow).where(EdgeWindow.kind == kind)))


def _run_flow(feed: _Feed, shadow: Motor9SpilloverShadow) -> None:
    """Secuencia estándar: referencia → trigger UP (+6) en A → madurar T+60 → T+120."""
    shadow.observe(A, now=0.0)  # referencia
    shadow.observe(A, now=10.0)  # salto (el test setea el mid de A entre medio)


def test_exec_row_full_math():
    """MECANISMO: trigger UP → side NO del hermano; ask0=100−yes_bid(t0); bid60=no_bid(t60);
    gross=bid60−ask0; fees=roundtrip a EXEC_COUNT; net=gross−fees/count. Y la fila mid
    sigue existiendo con su propia matemática."""
    feed = _Feed()
    shadow = _shadow(feed)
    # t0: A en 50, B con yes_bid=44/no_bid=54 (mid B = (44 + 46)/2 = 45)
    feed.mids[A] = 50.0
    feed.mids[B] = 45.0
    feed.quotes[B] = (44, 54)
    shadow.observe(A, now=0.0)
    feed.mids[A] = 56.0  # +6 → trigger; exec_side = "no"; ask0 = 100−44 = 56
    shadow.observe(A, now=10.0)

    # T+60: B bajó — yes_bid=40/no_bid=58 (mid 41); bid de salida NO = 58
    feed.mids[B] = 41.0
    feed.quotes[B] = (40, 58)
    shadow.observe(A, now=75.0)

    # T+120: persistencia (mid120 = 41)
    shadow.observe(A, now=135.0)

    [mid_row] = _rows("spillover")
    [exec_row] = _rows("spillover_exec")
    # Fila mid: follow firmado desde la dirección esperada (move +6 → sign −1): (41−45)·−1 = +4
    assert mid_row.gross_spread_cents == 4
    # Fila exec: la matemática completa
    ask0, bid60, count = 56, 58, Motor9SpilloverShadow.EXEC_COUNT
    fees = kalshi_fee_cents(count, ask0) + kalshi_fee_cents(count, bid60)
    assert exec_row.gross_spread_cents == bid60 - ask0
    assert exec_row.fees_cents == fees
    assert exec_row.count == count
    assert exec_row.magnitude_cents == int(round((bid60 - ask0) - fees / count))
    assert "no" in (exec_row.leg_states or "")
    assert f"a{ask0}b{bid60}" in (exec_row.leg_states or "")


def test_down_trigger_buys_yes_side():
    """Dirección: trigger DOWN → hermano esperado al ALZA → se compra YES:
    ask0 = 100 − no_bid; bid60 = yes_bid."""
    feed = _Feed()
    shadow = _shadow(feed)
    feed.mids[A] = 50.0
    feed.mids[B] = 45.0
    feed.quotes[B] = (44, 54)  # ask YES = 100−54 = 46
    shadow.observe(A, now=0.0)
    feed.mids[A] = 43.0  # −7 → trigger DOWN
    shadow.observe(A, now=10.0)

    feed.quotes[B] = (48, 50)  # bid salida YES = 48
    feed.mids[B] = 49.0
    shadow.observe(A, now=75.0)
    shadow.observe(A, now=135.0)

    [exec_row] = _rows("spillover_exec")
    assert exec_row.gross_spread_cents == 48 - 46
    assert "yes" in (exec_row.leg_states or "")


def test_no_quote_fn_is_backcompat_mid_only():
    """CONTROL: sin quote_fn (no cableado) el shadow se comporta EXACTAMENTE como F1 —
    fila mid sí, fila exec no."""
    feed = _Feed()
    shadow = _shadow(feed, with_quotes=False)
    feed.mids[A] = 50.0
    feed.mids[B] = 45.0
    shadow.observe(A, now=0.0)
    feed.mids[A] = 56.0
    shadow.observe(A, now=10.0)
    feed.mids[B] = 41.0
    shadow.observe(A, now=75.0)
    shadow.observe(A, now=135.0)

    assert len(_rows("spillover")) == 1
    assert _rows("spillover_exec") == []


def test_missing_entry_quote_skips_exec_but_keeps_mid():
    """FAIL-SAFE: book del hermano sin punta al trigger → sin experimento ejecutable
    (jamás un precio inventado), pero la medición del mid sigue."""
    feed = _Feed()
    shadow = _shadow(feed)
    feed.mids[A] = 50.0
    feed.mids[B] = 45.0
    feed.quotes[B] = None  # sin puntas al trigger
    shadow.observe(A, now=0.0)
    feed.mids[A] = 56.0
    shadow.observe(A, now=10.0)
    feed.quotes[B] = (40, 58)  # aparece después: tarde, el experimento ya nació sin ask0
    feed.mids[B] = 41.0
    shadow.observe(A, now=75.0)
    shadow.observe(A, now=135.0)

    assert len(_rows("spillover")) == 1
    assert _rows("spillover_exec") == []


def test_missing_exit_quote_skips_exec():
    """FAIL-SAFE: punta de salida ausente a T+60 → sin fila exec (cobertura parcial
    honesta), la fila mid se persiste igual."""
    feed = _Feed()
    shadow = _shadow(feed)
    feed.mids[A] = 50.0
    feed.mids[B] = 45.0
    feed.quotes[B] = (44, 54)
    shadow.observe(A, now=0.0)
    feed.mids[A] = 56.0
    shadow.observe(A, now=10.0)
    feed.quotes[B] = None  # el book perdió las puntas a T+60
    feed.mids[B] = 41.0
    shadow.observe(A, now=75.0)
    shadow.observe(A, now=135.0)

    assert len(_rows("spillover")) == 1
    assert _rows("spillover_exec") == []
