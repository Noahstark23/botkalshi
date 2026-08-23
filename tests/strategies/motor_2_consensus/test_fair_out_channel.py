"""Canal Motor 2 → Motor 5: fair_out en find_signals + gate live en el poller."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_2_consensus.detector import (
    KalshiEventQuotes,
    KalshiQuote,
    find_signals,
)
from src.strategies.motor_2_consensus.matcher import ET, start_time_et
from src.strategies.motor_2_consensus.poller import Motor2ShadowPoller
from src.strategies.motor_2_consensus.sources import FakeOddsSource

_KEY_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
NOW = datetime(2026, 6, 27, 12, 0, tzinfo=ET).astimezone(UTC)


@pytest.fixture(autouse=True)
def _clean_book():
    FairValueBook.clear()
    yield
    FairValueBook.clear()


def _stamp(dt: datetime) -> str:
    et = start_time_et(dt)
    return f"{et.year % 100:02d}{_KEY_MONTHS[et.month - 1]}{et.day:02d}"


def _fixture(commence: datetime) -> tuple[KalshiEventQuotes, OddsEvent]:
    odds = OddsEvent(
        id="x",
        sport_key="baseball_mlb",
        commence_time=commence,
        home_team="Philadelphia Phillies",
        away_team="New York Mets",
        bookmakers=(
            Bookmaker(
                key="pinnacle",
                title="P",
                last_update=NOW - timedelta(minutes=1),
                markets=(
                    Market(
                        key="h2h",
                        outcomes=(
                            Outcome(name="Philadelphia Phillies", price=1.60),
                            Outcome(name="New York Mets", price=2.60),
                        ),
                    ),
                ),
            ),
        ),
    )
    key = f"KXMLBGAME-{_stamp(commence)}PHINYM"
    ke = KalshiEventQuotes(
        event_key=key,
        outcomes=(
            KalshiQuote(f"{key}-PHI", "Philadelphia Phillies", 50, 55),
            KalshiQuote(f"{key}-NYM", "New York Mets", 55, 52),
        ),
    )
    return ke, odds


def test_fair_out_covers_all_matched_outcomes_not_only_signals():
    """El fair de AMBOS outcomes se expone aunque la señal sea solo una (el MM cotiza
    alrededor del fair, no del edge)."""
    ke, odds = _fixture(NOW + timedelta(hours=6))
    fair_out: dict[str, float] = {}
    find_signals([ke], [odds], min_edge=0.03, now=NOW, fair_out=fair_out)
    assert set(fair_out) == {f"{ke.event_key}-PHI", f"{ke.event_key}-NYM"}
    assert fair_out[f"{ke.event_key}-PHI"] == pytest.approx(0.62, abs=0.01)
    assert fair_out[f"{ke.event_key}-NYM"] == pytest.approx(0.38, abs=0.01)


def test_fair_out_transporta_kickoff_por_ticker():
    kickoff = NOW + timedelta(hours=6)
    ke, odds = _fixture(kickoff)
    fair_out: dict[str, float] = {}
    commence_out: dict[str, datetime] = {}

    find_signals(
        [ke],
        [odds],
        min_edge=0.03,
        now=NOW,
        fair_out=fair_out,
        fair_commence_out=commence_out,
    )

    assert set(commence_out) == set(fair_out)
    assert set(commence_out.values()) == {kickoff}


def test_fair_out_transporta_event_ticker_oficial():
    ke, odds = _fixture(NOW + timedelta(hours=6))
    fair_out: dict[str, float] = {}
    event_out: dict[str, str] = {}

    find_signals(
        [ke],
        [odds],
        min_edge=0.03,
        now=NOW,
        fair_out=fair_out,
        fair_event_out=event_out,
    )

    assert set(event_out) == set(fair_out)
    assert set(event_out.values()) == {ke.event_key}


def test_fair_out_transporta_provenance_de_books_frescos():
    ke, odds = _fixture(NOW + timedelta(hours=6))
    fair_out: dict[str, float] = {}
    provenance_out = {}

    find_signals(
        [ke],
        [odds],
        min_edge=0.03,
        now=NOW,
        fair_out=fair_out,
        fair_provenance_out=provenance_out,
        min_books=1,
        max_book_age_min=15,
    )

    assert set(provenance_out) == set(fair_out)
    provenance = next(iter(provenance_out.values()))
    assert provenance.event_ticker == ke.event_key
    assert provenance.odds_event_id == odds.id
    assert provenance.bookmaker_keys == ("pinnacle",)
    assert provenance.oldest_book_update == NOW - timedelta(minutes=1)


def test_fair_con_filtro_de_edad_rechaza_book_sin_timestamp():
    ke, odds = _fixture(NOW + timedelta(hours=6))
    bookmaker = odds.bookmakers[0]
    odds_without_timestamp = OddsEvent(
        id=odds.id,
        sport_key=odds.sport_key,
        commence_time=odds.commence_time,
        home_team=odds.home_team,
        away_team=odds.away_team,
        bookmakers=(
            Bookmaker(
                key=bookmaker.key,
                title=bookmaker.title,
                markets=bookmaker.markets,
                last_update=None,
            ),
        ),
    )
    fair_out: dict[str, float] = {}

    find_signals(
        [ke],
        [odds_without_timestamp],
        now=NOW,
        fair_out=fair_out,
        min_books=1,
        max_book_age_min=15,
    )

    assert fair_out == {}


def test_unmatched_event_publishes_nothing():
    ke, _ = _fixture(NOW + timedelta(hours=6))
    fair_out: dict[str, float] = {}
    find_signals([ke], [], min_edge=0.03, now=NOW, fair_out=fair_out)
    assert fair_out == {}


@pytest.mark.asyncio
async def test_poller_does_not_publish_with_fake_odds():
    """Gate de datos reales: la fuente FAKE jamás puebla el FairValueBook (un fair de
    fixture no es precio de referencia para cotizar)."""
    ke, odds = _fixture(datetime.now(UTC) + timedelta(hours=6))

    class _FakeKalshi:
        async def fetch(self):
            return [ke]

    poller = Motor2ShadowPoller(
        kalshi_source=_FakeKalshi(),
        odds_source=FakeOddsSource(lambda: [odds]),
        capital_usd=300.0,
    )
    await poller.poll_once()
    assert FairValueBook.size() == 0
