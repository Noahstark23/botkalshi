"""
Endurecimiento de la MEDICIÓN del consenso (auditoría rentabilidad 2026-07-07).

El edge de Motor 2 = fair − ask − fee, y el fair salía de una media simple de casas sin
mínimo, sin robustez a outliers, sin frescura, con set de referencia = la PRIMERA casa
del array. Cuatro fixes: mediana, MOTOR_2_MIN_BOOKS, filtro por last_update, y referencia
= set más frecuente. Más la re-validación pre-place del executor (selección adversa).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import _consensus_fair_probs

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def _bk(key: str, h2h: dict[str, float], *, last_update: datetime | None = None) -> Bookmaker:
    market = Market(key="h2h", outcomes=tuple(Outcome(name=n, price=p) for n, p in h2h.items()))
    return Bookmaker(key=key, title=key, markets=(market,), last_update=last_update)


def _event(*bks: Bookmaker) -> OddsEvent:
    return OddsEvent(
        id="e1",
        sport_key="baseball_mlb",
        commence_time=NOW + timedelta(hours=2),
        home_team="Los Angeles Lakers",
        away_team="Boston Celtics",
        bookmakers=bks,
    )


LINEA = {"Los Angeles Lakers": 1.90, "Boston Celtics": 2.00}


def test_min_books_gate_rejects_thin_consensus():
    """2 casas < min_books=3 → sin fair (consenso degenerado, no señal posible)."""
    ev = _event(_bk("a", LINEA), _bk("b", LINEA))
    assert _consensus_fair_probs(ev, min_books=3) == {}


def test_min_books_gate_passes_with_enough_books():
    ev = _event(_bk("a", LINEA), _bk("b", LINEA), _bk("c", LINEA))
    fair = _consensus_fair_probs(ev, min_books=3)
    assert set(fair) == {"los angeles lakers", "boston celtics"} or len(fair) == 2


def test_median_resists_single_soft_book():
    """Una casa desviada (soft) NO arrastra el fair: la mediana de 3 la ignora — con la
    media anterior el fair del outcome subía y fabricaba edge."""
    soft = {"Los Angeles Lakers": 2.60, "Boston Celtics": 1.50}  # muy desviada
    ev = _event(_bk("a", LINEA), _bk("b", LINEA), _bk("soft", soft))
    fair = _consensus_fair_probs(ev, min_books=1)
    ev_sin_soft = _event(_bk("a", LINEA), _bk("b", LINEA))
    fair_limpio = _consensus_fair_probs(ev_sin_soft, min_books=1)
    for name, p in fair.items():
        assert p == pytest.approx(fair_limpio[name], abs=1e-9)  # la soft no movió nada


def test_stale_book_filtered_by_last_update():
    """Una línea congelada hace 2h queda FUERA del consenso con max_book_age_min=15;
    las frescas entran y una casa sin timestamp falla cerrada."""
    ev = _event(
        _bk("fresca_a", LINEA, last_update=NOW - timedelta(minutes=3)),
        _bk("fresca_b", LINEA, last_update=NOW - timedelta(minutes=4)),
        _bk(
            "congelada",
            {"Los Angeles Lakers": 3.5, "Boston Celtics": 1.3},
            last_update=NOW - timedelta(hours=2),
        ),
        _bk("sin_ts", LINEA),  # last_update=None → sin evidencia, se descarta
    )
    fair = _consensus_fair_probs(ev, min_books=2, max_book_age_min=15.0, now=NOW)
    assert fair  # las 2 usables forman consenso
    ev_limpio = _event(_bk("fresca_a", LINEA), _bk("fresca_b", LINEA))
    fair_limpio = _consensus_fair_probs(ev_limpio, min_books=2)
    for name, p in fair.items():
        assert p == pytest.approx(fair_limpio[name], abs=1e-9)  # la congelada no aportó


def test_reference_set_is_most_frequent_not_first():
    """La PRIMERA casa lista un set atípico (sin Draw en un 1X2): antes TODAS las demás
    se descartaban por 'set distinto' y el evento moría. Ahora la referencia es el set
    más frecuente → la atípica se descarta y el consenso vive."""
    tres_way = {"Los Angeles Lakers": 2.5, "Boston Celtics": 3.0, "Draw": 3.2}
    dos_way_atipico = {"Los Angeles Lakers": 1.9, "Boston Celtics": 2.0}
    ev = _event(
        _bk("atipica", dos_way_atipico),  # primera del array
        _bk("a", tres_way),
        _bk("b", tres_way),
        _bk("c", tres_way),
    )
    fair = _consensus_fair_probs(ev, min_books=3)
    assert len(fair) == 3  # el 1X2 completo, no el 2-way de la primera casa
    assert "draw" in fair


# =====================================================
# Executor: re-validación pre-place (selección adversa)
# =====================================================


def _executor_with_book(orderbook: dict | Exception):
    from src.risk.manager import RiskManager
    from src.strategies.motor_2_consensus.executor import Motor2Executor

    client = MagicMock()
    if isinstance(orderbook, Exception):
        client.get_orderbook = AsyncMock(side_effect=orderbook)
    else:
        client.get_orderbook = AsyncMock(return_value=orderbook)
    return Motor2Executor(client, MagicMock(spec=RiskManager))


@pytest.mark.asyncio
async def test_current_ask_synthetic_from_opposite_bid():
    """Comprar YES: el ask vivo = 100 − mejor bid de NO, con su size."""
    ex = _executor_with_book({"orderbook": {"yes": [[40, 50]], "no": [[45, 30]]}})
    ask, size = await ex._current_ask("T", "yes")
    assert ask == 55 and size == 30


@pytest.mark.asyncio
async def test_current_ask_fails_open_on_error():
    """FAIL-OPEN: book ilegible → (None, None) — el caller procede (IOC limit acota)."""
    ex = _executor_with_book(RuntimeError("api caída"))
    assert await ex._current_ask("T", "yes") == (None, None)
