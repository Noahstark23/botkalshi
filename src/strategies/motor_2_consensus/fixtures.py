"""
Fixture ILUSTRATIVO de odds para el cable shadow del Motor 2 (sin la API paga).

Estos OddsEvents NO son cuotas reales — son un fixture con nombres de selección reales
del Mundial para que el pipeline (matcher → detector → grabado) corra end-to-end contra
los partidos KXWCGAME de Kalshi HOY. Las edges que produzcan son artificiales y por eso
FakeOddsSource va marcado `is_live=False` (el poller NO las persiste).

El día que se paga The Odds API, este fixture se descarta: el poller se construye con
`LiveOddsSource(["soccer_fifa_world_cup"])` y mide cuotas reales.
"""
from __future__ import annotations

from datetime import UTC, datetime

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome


def _h2h_event(
    event_id: str, home: str, away: str, draw: str, prices: dict[str, float]
) -> OddsEvent:
    """Un OddsEvent 1X2 con un solo bookmaker h2h (cardinalidad 3, nombres reales)."""
    outcomes = tuple(Outcome(name=n, price=prices[n]) for n in (home, away, draw))
    return OddsEvent(
        id=event_id,
        sport_key="soccer_fifa_world_cup",
        commence_time=datetime(2026, 6, 27, 18, 0, tzinfo=UTC),
        home_team=home,
        away_team=away,
        bookmakers=(
            Bookmaker(
                key="fixture_book",
                title="Fixture Book (ilustrativo)",
                markets=(Market(key="h2h", outcomes=outcomes),),
            ),
        ),
    )


def world_cup_demo_fixture() -> list[OddsEvent]:
    """
    Fixture de partidos 1X2 con nombres de selección reales (para cruzar con KXWCGAME).

    Los nombres de outcome deben canonizar igual que el `yes_sub_title` de Kalshi
    (el matcher pliega 'Tie'→'draw'). Las cuotas son inventadas.
    """
    return [
        _h2h_event(
            "fixture-jor-arg",
            home="Argentina",
            away="Jordan",
            draw="Draw",
            prices={"Argentina": 1.20, "Jordan": 13.0, "Draw": 6.5},
        ),
        _h2h_event(
            "fixture-ecu-ger",
            home="Germany",
            away="Ecuador",
            draw="Draw",
            prices={"Germany": 1.70, "Ecuador": 5.0, "Draw": 3.6},
        ),
    ]
