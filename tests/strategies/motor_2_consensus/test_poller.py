"""
Tests del poller shadow del Motor 2 (poller.py).

Verifica: un ciclo cruza Kalshi↔odds y emite señales; con fuente FAKE NO persiste
(edges no reales); con fuente LIVE persiste EdgeWindow(kind="consensus"); el path JAMÁS
ejecuta capital; el loop respeta stop_event.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

import src.storage.models as models
from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus.detector import KalshiEventQuotes, KalshiQuote
from src.strategies.motor_2_consensus.matcher import start_time_et
from src.strategies.motor_2_consensus.poller import Motor2ShadowPoller

# El matching ahora exige que la fecha del event_key coincida con el commence_time (en ET);
# el datestamp se deriva del commence del test para que la coherencia se mantenga sola.
_KEY_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _key_datestamp(dt) -> str:
    et = start_time_et(dt)
    return f"{et.year % 100:02d}{_KEY_MONTHS[et.month - 1]}{et.day:02d}"

_COMMENCE = datetime.now(UTC) + timedelta(hours=2)
EV = f"KXWCGAME-{_key_datestamp(_COMMENCE)}JORARG"


class _FakeKalshiSource:
    def __init__(self, events: list[KalshiEventQuotes]):
        self._events = events

    async def fetch(self) -> list[KalshiEventQuotes]:
        return list(self._events)


class _StubOdds:
    def __init__(self, events: list[OddsEvent], *, is_live: bool):
        self._events = events
        self.is_live = is_live

    async def fetch(self) -> list[OddsEvent]:
        return list(self._events)


def _kalshi_event() -> KalshiEventQuotes:
    # Argentina a 64c en Kalshi vs fair ~0.726 → edge YES ~8pp (PLAUSIBLE, no artefacto).
    return KalshiEventQuotes(
        event_key=EV,
        outcomes=(
            KalshiQuote(f"{EV}-ARG", "Argentina", yes_ask_cents=64, no_ask_cents=38),
            KalshiQuote(f"{EV}-JOR", "Jordan", yes_ask_cents=8, no_ask_cents=93),
            KalshiQuote(f"{EV}-TIE", "Draw", yes_ask_cents=30, no_ask_cents=71),
        ),
    )


def _odds_event() -> OddsEvent:
    # Consenso: Argentina favorita fuerte → fair_prob ~0.726. PRE-MATCH (futuro vs now).
    from datetime import UTC, datetime, timedelta

    outcomes = (
        Outcome("Argentina", 1.30),
        Outcome("Jordan", 11.0),
        Outcome("Draw", 5.0),
    )
    return OddsEvent(
        id="x",
        sport_key="soccer_fifa_world_cup",
        commence_time=datetime.now(UTC) + timedelta(hours=2),
        home_team="Argentina",
        away_team="Jordan",
        bookmakers=(Bookmaker("book", "Book", (Market("h2h", outcomes),)),),
    )


def _consensus_windows() -> list[models.EdgeWindow]:
    with models.get_session() as s:
        rows = list(s.exec(select(models.EdgeWindow)).all())
    return [w for w in rows if w.kind == "consensus"]


@pytest.mark.asyncio
async def test_poll_once_emits_signals_for_matched_event():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=False),
        capital_usd=300.0,
    )
    signals = await poller.poll_once()
    # Con one_per_event (default), el evento colapsa a UNA sola apuesta direccional (la de
    # mayor edge neto) — antes emitía una por cada outcome/lado con edge.
    assert len(signals) == 1
    assert signals[0].market_ticker.startswith(EV)


@pytest.mark.asyncio
async def test_fake_source_does_not_persist():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=False),  # FAKE → no persiste
        capital_usd=300.0,
    )
    signals = await poller.poll_once()
    assert signals  # hubo señales...
    assert _consensus_windows() == []  # ...pero NADA en la DB (fuente no-live)


@pytest.mark.asyncio
async def test_live_source_persists_consensus_windows():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=True),  # LIVE → persiste
        capital_usd=300.0,
    )
    signals = await poller.poll_once()
    wins = _consensus_windows()
    assert len(wins) == len(signals) > 0
    assert all(w.kind == "consensus" and w.edge_pct > 0 for w in wins)
    # Desglose AUDITABLE persistido (antes None): gross/fee poblados, neto = bruto − fee.
    for w in wins:
        assert w.gross_spread_cents is not None and w.fees_cents is not None
        assert w.magnitude_cents == w.gross_spread_cents - w.fees_cents


@pytest.mark.asyncio
async def test_live_cycle_persists_funnel_snapshot():
    """Con odds reales, cada ciclo graba un Motor2FunnelSnapshot (memoria para el Analyst Loop)."""
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=True),
        capital_usd=300.0,
    )
    await poller.poll_once()
    with models.get_session() as s:
        snaps = list(s.exec(select(models.Motor2FunnelSnapshot)).all())
    assert len(snaps) == 1
    assert snaps[0].kalshi_total >= 1 and snaps[0].events_matched >= 1


@pytest.mark.asyncio
async def test_fake_cycle_does_not_persist_funnel():
    """Con fixture fake (no-live), NO se graba snapshot (data no real)."""
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=False),
        capital_usd=300.0,
    )
    await poller.poll_once()
    with models.get_session() as s:
        assert list(s.exec(select(models.Motor2FunnelSnapshot)).all()) == []


@pytest.mark.asyncio
async def test_min_edge_threshold_filters_signals():
    """min_edge alto → no pasa ninguna señal (umbral tuneable desde el runner/config)."""
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=True),
        capital_usd=300.0,
        min_edge=0.99,  # 99pp: imposible → 0 señales
    )
    assert await poller.poll_once() == []
    assert _consensus_windows() == []


@pytest.mark.asyncio
async def test_poll_once_no_kalshi_returns_empty():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([]),
        _StubOdds([_odds_event()], is_live=True),
        capital_usd=300.0,
    )
    assert await poller.poll_once() == []
    assert _consensus_windows() == []  # sin quotes Kalshi → ni siquiera consulta odds


@pytest.mark.asyncio
async def test_executor_bets_only_on_live_odds():
    """Con executor + odds LIVE → apuesta. Con odds FAKE → JAMÁS apuesta (gate de plata)."""
    from unittest.mock import AsyncMock

    class _Out:
        filled = False
        filled_count = 0

    ex = AsyncMock()
    ex.execute = AsyncMock(return_value=_Out())

    # FAKE odds → no debe ejecutar nunca (apostar contra odds inventadas = quemar plata).
    poller_fake = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=False),
        capital_usd=300.0,
        executor=ex,
    )
    await poller_fake.poll_once()
    ex.execute.assert_not_awaited()

    # LIVE odds → ejecuta las señales detectadas.
    poller_live = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=True),
        capital_usd=300.0,
        executor=ex,
    )
    signals = await poller_live.poll_once()
    assert signals
    assert ex.execute.await_count == len(signals)


@pytest.mark.asyncio
async def test_filled_bet_sends_telegram_alert(monkeypatch):
    """Cuando una apuesta de Motor 2 LLENA → se manda aviso a Telegram con los datos."""
    from unittest.mock import AsyncMock

    class _Out:
        filled = True
        filled_count = 4

    ex = AsyncMock()
    ex.execute = AsyncMock(return_value=_Out())
    alert = AsyncMock()
    monkeypatch.setattr("src.monitoring.telegram_alerts.alert_bet_placed", alert)

    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=True),
        capital_usd=300.0,
        executor=ex,
    )
    signals = await poller.poll_once()

    assert signals
    assert alert.await_count == ex.execute.await_count  # un aviso por apuesta llena
    kwargs = alert.await_args.kwargs
    assert kwargs["count"] == 4
    assert kwargs["ticker"].startswith(EV)
    assert kwargs["side"] in ("YES", "NO")
    assert "price_cents" in kwargs and "edge_pct" in kwargs


@pytest.mark.asyncio
async def test_no_fill_does_not_alert(monkeypatch):
    """IOC sin fill (no apostó de verdad) → NO se manda aviso (no spamear)."""
    from unittest.mock import AsyncMock

    class _Out:
        filled = False
        filled_count = 0

    ex = AsyncMock()
    ex.execute = AsyncMock(return_value=_Out())
    alert = AsyncMock()
    monkeypatch.setattr("src.monitoring.telegram_alerts.alert_bet_placed", alert)

    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=True),
        capital_usd=300.0,
        executor=ex,
    )
    await poller.poll_once()
    alert.assert_not_awaited()


@pytest.mark.asyncio
async def test_alert_failure_does_not_break_betting_loop(monkeypatch):
    """Si Telegram tira excepción, el loop de apuestas NO se rompe (best-effort aislado)."""
    from unittest.mock import AsyncMock

    class _Out:
        filled = True
        filled_count = 2

    ex = AsyncMock()
    ex.execute = AsyncMock(return_value=_Out())
    monkeypatch.setattr(
        "src.monitoring.telegram_alerts.alert_bet_placed",
        AsyncMock(side_effect=RuntimeError("telegram down")),
    )

    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=True),
        capital_usd=300.0,
        executor=ex,
    )
    signals = await poller.poll_once()  # no debe propagar la excepción
    assert ex.execute.await_count == len(signals)  # se intentaron todas las apuestas


@pytest.mark.asyncio
async def test_run_loop_stops_on_event():
    poller = Motor2ShadowPoller(
        _FakeKalshiSource([_kalshi_event()]),
        _StubOdds([_odds_event()], is_live=False),
        interval_sec=0.01,
        capital_usd=300.0,
    )
    stop = asyncio.Event()

    async def _stop_soon():
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(poller.run(stop), _stop_soon())
    assert stop.is_set()
