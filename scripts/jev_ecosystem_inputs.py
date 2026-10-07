"""Entradas sintéticas para ejercitar el cálculo real de M2 sin red ni publicación."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from src.clients.odds_api import OddsEvent, _parse_event
from src.strategies.motor_2_consensus.detector import KalshiEventQuotes, find_signals
from src.strategies.motor_2_consensus.sources import _parse_event_quotes

BASE_TIME = datetime(2026, 10, 6, 16, 0, tzinfo=UTC)
EVENT_TICKER = "KXMLBGAME-26OCT06PHINYM"
TARGET_TICKER = f"{EVENT_TICKER}-PHI"

_ODDS_MODES = frozenset(
    {"fresh", "stale", "missing_timestamp", "insufficient_books", "mismatch", "kickoff"}
)
_TEAMS = ("Philadelphia Phillies", "New York Mets")


def build_inputs(scenario: dict, now: datetime) -> tuple[KalshiEventQuotes, OddsEvent]:
    """Construye fixtures y los convierte con los mismos parsers usados por M2."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now debe ser un datetime aware")
    if not isinstance(scenario, dict):
        raise ValueError("scenario debe ser un dict")
    scenario_id = scenario.get("id")
    mode = scenario.get("odds_mode")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise ValueError("scenario.id debe ser un string no vacío")
    if not isinstance(mode, str) or mode not in _ODDS_MODES:
        raise ValueError("scenario.odds_mode no reconocido")

    now = now.astimezone(UTC)
    # El evento no se desplaza al avanzar el reloj de cada frame del replay.
    kickoff = BASE_TIME + (timedelta(seconds=60) if mode == "kickoff" else timedelta(hours=3))
    teams = ("Baltimore Orioles", "Boston Red Sox") if mode == "mismatch" else _TEAMS
    fair_prob = scenario.get("fair_prob", 0.5)
    if type(fair_prob) not in (int, float) or not isfinite(fair_prob) or not 0 < fair_prob < 1:
        raise ValueError("fair_prob debe ser finito y estar entre 0 y 1, sin extremos")
    prices = (1 / (fair_prob * 1.05), 1 / ((1 - fair_prob) * 1.05))
    if any(price <= 1 for price in prices):
        raise ValueError("fair_prob produce cuotas decimales inválidas con vig 1.05")
    update = now - (timedelta(hours=1) if mode == "stale" else timedelta(seconds=60))
    bookmakers = []
    for index in range(2 if mode == "insufficient_books" else 3):
        bookmaker: dict[str, Any] = {
            "key": f"synthetic_book_{index + 1}",
            "title": f"Fixture {index + 1}",
            "markets": [
                {
                    "key": "h2h",
                    "outcomes": [
                        {"name": team, "price": price}
                        for team, price in zip(teams, prices, strict=True)
                    ],
                }
            ],
        }
        if mode != "missing_timestamp":
            bookmaker["last_update"] = update.isoformat()
        bookmakers.append(bookmaker)

    odds = _parse_event(
        {
            "id": f"synthetic-{scenario_id}",
            "sport_key": "baseball_mlb",
            "commence_time": kickoff.isoformat(),
            "home_team": teams[0],
            "away_team": teams[1],
            "bookmakers": bookmakers,
        }
    )
    yes_bid, yes_ask = scenario.get("yes_bid"), scenario.get("yes_ask")
    if yes_bid is None and yes_ask is None:
        # Metadatos del evento aún disponibles aunque falte el BBO del replay.
        # Este fallback permite calcular fair; no demuestra liquidez. M5 debe
        # recibir el book vacío original y rechazar la cotización por separado.
        phi_asks = (60, 60)
    else:
        if (
            type(yes_bid) is not int
            or type(yes_ask) is not int
            or not 1 <= yes_bid <= yes_ask <= 99
        ):
            raise ValueError("el frame debe tener ambos precios BBO enteros válidos")
        phi_asks = (yes_ask, 100 - yes_bid)
    # En este fixture binario NYM es el resultado complementario de PHI.
    asks_by_outcome = (phi_asks, tuple(reversed(phi_asks)))
    kalshi = _parse_event_quotes(
        EVENT_TICKER,
        [
            {
                "ticker": f"{EVENT_TICKER}-{suffix}",
                "status": "open",
                "yes_sub_title": team,
                "yes_ask": asks[0],
                "no_ask": asks[1],
            }
            for suffix, team, asks in zip(("PHI", "NYM"), _TEAMS, asks_by_outcome, strict=True)
        ],
    )
    if kalshi is None or odds is None:
        raise ValueError("los parsers rechazaron el fixture sintético")
    return kalshi, odds


def compute_fairs(scenario: dict, now: datetime) -> dict:
    """Devuelve los objetos calculados; el harness decide su uso en memoria aislada."""
    kalshi, odds = build_inputs(scenario, now)
    fairs, commence_times, event_tickers, provenances = {}, {}, {}, {}
    signals = find_signals(
        [kalshi],
        [odds],
        now=now,
        capital_usd=200.0,
        min_edge=0.03,
        max_edge=0.08,
        max_stake_pct=1.0,
        one_per_event=True,
        min_books=3,
        max_book_age_min=15,
        fee_at_stake_count=True,
        fair_out=fairs,
        fair_commence_out=commence_times,
        fair_event_out=event_tickers,
        fair_provenance_out=provenances,
    )
    return {
        "fairs": fairs,
        "commence_times": commence_times,
        "event_tickers": event_tickers,
        "provenances": provenances,
        "signal_count": len(signals),
    }
