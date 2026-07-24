"""
Burst polling pre-kickoff del poller M2 (auditoría 2026-07-12).

Los edges reales del funnel son transitorios (5.63pp el 07-05: UN ciclo y desapareció) y
se concentran cerca del kickoff; con el ritmo base (300s) se ven de casualidad.

Verifica: mecanismo (kickoff dentro de la ventana → intervalo burst), control (fuera de
ventana / sin burst configurado → ritmo base EXACTO), y fail-safe (feed raro → base).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

from src.strategies.motor_2_consensus.poller import Motor2ShadowPoller


def _poller(**kwargs) -> Motor2ShadowPoller:
    with patch("src.strategies.motor_2_consensus.poller.get_settings") as gs:
        gs.return_value.ACTIVE_CAPITAL_USD = 180.0
        return Motor2ShadowPoller(MagicMock(), MagicMock(), interval_sec=300.0, **kwargs)


def _odds_event(minutes_from_now: float) -> MagicMock:
    oe = MagicMock()
    oe.commence_time = datetime.now(UTC) + timedelta(minutes=minutes_from_now)
    return oe


def test_burst_when_kickoff_inside_window():
    """MECANISMO: kickoff en 20min (< ventana 45) → el próximo ciclo corre a 60s."""
    p = _poller(burst_interval_sec=60.0, burst_window_min=45.0)
    p._note_next_kickoff([_odds_event(20), _odds_event(300)])
    assert p._cycle_timeout() == 60.0


def test_base_interval_when_kickoff_far():
    """CONTROL: el kickoff más próximo está a 3h → ritmo base (no quemar API sin motivo)."""
    p = _poller(burst_interval_sec=60.0, burst_window_min=45.0)
    p._note_next_kickoff([_odds_event(180)])
    assert p._cycle_timeout() == 300.0


def test_burst_disabled_is_exact_historical_behavior():
    """CONTROL: burst_interval_sec=0 (default) → SIEMPRE ritmo base, aunque haya kickoff
    encima (comportamiento histórico exacto; el flag es opt-in)."""
    p = _poller()  # defaults: burst 0.0 = off
    p._note_next_kickoff([_odds_event(5)])
    assert p._cycle_timeout() == 300.0


def test_started_games_do_not_trigger_burst():
    """CONTROL: partidos ya arrancados (commence_time en el pasado) no cuentan — el burst
    es PRE-kickoff; in-play no hay nada que cazar (guard pre-match del detector)."""
    p = _poller(burst_interval_sec=60.0, burst_window_min=45.0)
    p._note_next_kickoff([_odds_event(-30)])
    assert p._next_kickoff is None
    assert p._cycle_timeout() == 300.0


def test_malformed_feed_fails_safe_to_base():
    """FAIL-SAFE: un feed raro (sin commence_time) deja next_kickoff=None → ritmo base;
    nunca rompe el loop ni acelera por basura."""
    p = _poller(burst_interval_sec=60.0, burst_window_min=45.0)
    weird = MagicMock()
    weird.commence_time = None
    p._note_next_kickoff([weird])
    assert p._next_kickoff is None
    assert p._cycle_timeout() == 300.0
