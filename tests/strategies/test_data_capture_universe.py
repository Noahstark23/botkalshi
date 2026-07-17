"""
Tests de las fotos read-only por serie de DataCaptureService.

`series_histogram` es el diagnóstico de onboarding de MLB: a partir de los tickers
trackeados (sin red) reporta, por prefijo de serie, cuántos eventos hay y cuántos
markets por evento — revelando el prefijo exacto y la ESTRUCTURA (2-market-por-equipo
que el Motor 2 ya maneja vs 1-market-2-way que necesitaría variante).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.strategies.data_capture import DataCaptureService


@pytest.fixture
def service() -> DataCaptureService:
    with (
        patch("src.strategies.data_capture.get_settings"),
        patch("src.strategies.data_capture.KalshiWebSocket"),
    ):
        svc = DataCaptureService()
        svc._tracked_tickers = set()
        return svc


def test_series_histogram_empty(service: DataCaptureService) -> None:
    assert service.series_histogram() == {}


def test_series_histogram_groups_by_prefix_and_structure(service: DataCaptureService) -> None:
    service._tracked_tickers = {
        # Mundial 1X2: 2 eventos × 3 markets por-evento (Gana/Empata/Gana).
        "KXWCGAME-26JUN27JORARG-ARG",
        "KXWCGAME-26JUN27JORARG-JOR",
        "KXWCGAME-26JUN27JORARG-TIE",
        "KXWCGAME-26JUN28BRAFRA-BRA",
        "KXWCGAME-26JUN28BRAFRA-FRA",
        "KXWCGAME-26JUN28BRAFRA-TIE",
        # MLB hipotético: 2 eventos × 2 markets por-equipo (estructura que SÍ maneja Motor 2).
        "KXMLBGAME-26JUN15NYYBOS-NYY",
        "KXMLBGAME-26JUN15NYYBOS-BOS",
        "KXMLBGAME-26JUN15LADSFG-LAD",
        "KXMLBGAME-26JUN15LADSFG-SFG",
    }
    hist = service.series_histogram()

    assert set(hist) == {"KXWCGAME", "KXMLBGAME"}
    wc_events, wc_mpe = hist["KXWCGAME"]
    assert wc_events == 2 and wc_mpe == pytest.approx(3.0)  # 3-way → cardinalidad vs odds 1X2
    mlb_events, mlb_mpe = hist["KXMLBGAME"]
    assert mlb_events == 2 and mlb_mpe == pytest.approx(2.0)  # 2-way por-equipo → ✓ Motor 2


def test_series_histogram_one_market_two_way_signature(service: DataCaptureService) -> None:
    """~1.0 mkt/evento = 1 market 2-way → señal de que haría falta variante de parseo."""
    service._tracked_tickers = {
        "KXSOMEGAME-26JUN15AAABBB-GAME",
        "KXSOMEGAME-26JUN15CCCDDD-GAME",
    }
    n_events, mpe = service.series_histogram()["KXSOMEGAME"]
    assert n_events == 2 and mpe == pytest.approx(1.0)
