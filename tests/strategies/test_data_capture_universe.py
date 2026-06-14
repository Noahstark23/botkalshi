"""
Tests del seam read-only `event_universe` (Motor 2): agrupar tickers trackeados por
event_key, filtrando por SERIE configurable. Generaliza el universo del Mundial a
cualquier deporte (MLB, etc.) sin tocar el arb multi-outcome (que sigue WC-only).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.strategies.data_capture import DataCaptureService


@pytest.fixture
def service():
    with (
        patch("src.strategies.data_capture.get_settings"),
        patch("src.strategies.data_capture.KalshiWebSocket"),
    ):
        return DataCaptureService()


def test_event_universe_groups_by_event_and_filters_series(service):
    service._tracked_tickers = {
        "KXWCGAME-26JUN27JORARG-ARG",
        "KXWCGAME-26JUN27JORARG-JOR",
        "KXWCGAME-26JUN27JORARG-TIE",
        "KXMLBGAME-25JUN14NYYBOS-NYY",
        "KXMLBGAME-25JUN14NYYBOS-BOS",
        "KXNBA-26-LAL",  # otra serie, no pedida
    }
    # Solo el Mundial:
    wc = service.event_universe({"KXWCGAME"})
    assert wc == {
        "KXWCGAME-26JUN27JORARG": {
            "KXWCGAME-26JUN27JORARG-ARG",
            "KXWCGAME-26JUN27JORARG-JOR",
            "KXWCGAME-26JUN27JORARG-TIE",
        }
    }
    # Solo MLB (2-way moneyline agrupado por evento):
    mlb = service.event_universe({"KXMLBGAME"})
    assert mlb == {
        "KXMLBGAME-25JUN14NYYBOS": {
            "KXMLBGAME-25JUN14NYYBOS-NYY",
            "KXMLBGAME-25JUN14NYYBOS-BOS",
        }
    }
    # Ambos a la vez (config con varias series):
    both = service.event_universe({"KXWCGAME", "KXMLBGAME"})
    assert set(both) == {"KXWCGAME-26JUN27JORARG", "KXMLBGAME-25JUN14NYYBOS"}


def test_event_universe_series_match_is_exact(service):
    """Igualdad EXACTA de serie: KXWCGAMEGOALS (totales) NO cae en KXWCGAME."""
    service._tracked_tickers = {
        "KXWCGAME-26JUN27JORARG-ARG",
        "KXWCGAMEGOALS-26JUN27JORARG-O25",
    }
    assert set(service.event_universe({"KXWCGAME"})) == {"KXWCGAME-26JUN27JORARG"}


def test_multi_event_universe_backcompat_is_world_cup(service):
    """multi_event_universe sigue devolviendo solo el Mundial (MULTI_SERIES del Motor REST)."""
    service._tracked_tickers = {
        "KXWCGAME-26JUN27JORARG-ARG",
        "KXWCGAME-26JUN27JORARG-JOR",
        "KXMLBGAME-25JUN14NYYBOS-NYY",  # MLB NO debe aparecer acá
    }
    uni = service.multi_event_universe()
    assert set(uni) == {"KXWCGAME-26JUN27JORARG"}
