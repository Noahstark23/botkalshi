"""
CLV-al-kickoff (Propuesta 2026-08-13): el fair del ÚLTIMO ciclo pre-kickoff se
persiste como el "cierre" — el benchmark del CLV de los fills shadow de M5.

El estándar de la industria (Buchdahl: CLV→PnL) y el diseño del gemelo público de
M2. Converge en cientos de observaciones vs miles de settlements, y diagnostica el
confound del fair degradado: markout OK + CLV negativo = el problema es el fair.
Pasajero best-effort del ciclo de M2 — jamás rompe al host; una fila por ticker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from sqlmodel import select

from src.storage.models import FairKickoffSnapshot, get_session
from src.strategies.motor_2_consensus.detector import KalshiEventQuotes, KalshiQuote
from src.strategies.motor_2_consensus.matcher import start_time_et
from src.strategies.motor_2_consensus.poller import Motor2ShadowPoller

_KEY_MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _key_para(kickoff: datetime, teams: str = "PHINYM") -> str:
    et = start_time_et(kickoff)
    return (
        f"KXMLBGAME-{et.year % 100:02d}{_KEY_MONTHS[et.month - 1]}{et.day:02d}"
        f"{et.hour:02d}{et.minute:02d}{teams}"
    )


def _evento(kickoff: datetime, teams: str = "PHINYM") -> KalshiEventQuotes:
    key = _key_para(kickoff, teams)
    return KalshiEventQuotes(
        event_key=key,
        outcomes=(
            KalshiQuote(f"{key}-PHI", "Philadelphia Phillies", 50, 52),
            KalshiQuote(f"{key}-NYM", "New York Mets", 50, 52),
        ),
    )


def _poller() -> Motor2ShadowPoller:
    # capital_usd explícito: evita el get_settings() fail-fast del fallback estático.
    return Motor2ShadowPoller(MagicMock(), MagicMock(), interval_sec=300.0, capital_usd=300.0)


def _rounded_now_plus(seconds: float) -> datetime:
    """Kickoff a +seconds, redondeado al minuto (los keys no llevan segundos)."""
    k = datetime.now(UTC) + timedelta(seconds=seconds)
    return k.replace(second=0, microsecond=0)


def test_ultimo_ciclo_pre_kickoff_snapshotea_el_cierre():
    poller = _poller()
    kickoff = _rounded_now_plus(240)  # dentro del próximo intervalo (300s)
    ev = _evento(kickoff)
    fair = {ev.outcomes[0].market_ticker: 0.6234, ev.outcomes[1].market_ticker: 0.3766}

    poller._snapshot_fair_kickoff([ev], fair)

    with get_session() as s:
        rows = list(s.exec(select(FairKickoffSnapshot)))
    assert len(rows) == 2
    por_ticker = {r.ticker: r for r in rows}
    assert por_ticker[ev.outcomes[0].market_ticker].fair_prob == 0.6234
    # kickoff_at coincide con el del key (comparación naive: SQLite no preserva tz).
    assert por_ticker[ev.outcomes[0].market_ticker].kickoff_at.replace(
        tzinfo=None
    ) == kickoff.astimezone(UTC).replace(tzinfo=None)


def test_lejos_del_kickoff_no_snapshotea():
    """CONTROL: a 2 horas del kickoff NO es el último ciclo — nada se persiste
    (el cierre es el ÚLTIMO fair, no cualquier fair)."""
    poller = _poller()
    ev = _evento(_rounded_now_plus(7200))

    poller._snapshot_fair_kickoff([ev], {q.market_ticker: 0.5 for q in ev.outcomes})

    with get_session() as s:
        assert list(s.exec(select(FairKickoffSnapshot))) == []


def test_partido_ya_arrancado_no_snapshotea():
    """CONTROL: kickoff en el pasado → tarde para un 'cierre' (sería in-play)."""
    poller = _poller()
    ev = _evento(_rounded_now_plus(-600))

    poller._snapshot_fair_kickoff([ev], {q.market_ticker: 0.5 for q in ev.outcomes})

    with get_session() as s:
        assert list(s.exec(select(FairKickoffSnapshot))) == []


def test_una_sola_fila_por_ticker():
    """DEDUP: dos ciclos dentro de la ventana (p.ej. burst) no duplican el cierre."""
    poller = _poller()
    ev = _evento(_rounded_now_plus(240))
    fair = {q.market_ticker: 0.55 for q in ev.outcomes}

    poller._snapshot_fair_kickoff([ev], fair)
    poller._snapshot_fair_kickoff([ev], fair)  # segundo ciclo, misma ventana

    with get_session() as s:
        assert len(list(s.exec(select(FairKickoffSnapshot)))) == 2  # 2 tickers, no 4


def test_ticker_sin_fair_este_ciclo_se_saltea():
    """FAIL-SAFE: un outcome sin fair (no matcheó este ciclo) no inventa cierre."""
    poller = _poller()
    ev = _evento(_rounded_now_plus(240))
    solo_uno = {ev.outcomes[0].market_ticker: 0.6}

    poller._snapshot_fair_kickoff([ev], solo_uno)

    with get_session() as s:
        rows = list(s.exec(select(FairKickoffSnapshot)))
    assert len(rows) == 1 and rows[0].ticker == ev.outcomes[0].market_ticker
