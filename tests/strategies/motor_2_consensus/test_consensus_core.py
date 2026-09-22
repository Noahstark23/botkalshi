"""
Núcleo puro del consenso de M2 (extraído 2026-09-22) — equivalencia y portabilidad.

El servicio de research (droplet, python3 del sistema sin venv) necesita el MISMO fair
que M2 publica para M5. La extracción no puede cambiar la matemática: estos tests fijan
que `detector._consensus_fair_probs` y el núcleo dan el mismo resultado, que el núcleo
entrega los timestamps ORIGINALES de las casas, y que se importa sin loguru/pydantic.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome
from src.strategies.motor_2_consensus import consensus
from src.strategies.motor_2_consensus.detector import _consensus_fair_probs, _select_candidate

NOW = datetime(2026, 9, 22, 18, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[3]


def _bk(key, h2h, *, age_min=None):
    market = Market(key="h2h", outcomes=tuple(Outcome(name=n, price=p) for n, p in h2h.items()))
    last = None if age_min is None else NOW - timedelta(minutes=age_min)
    return Bookmaker(key=key, title=key, markets=(market,), last_update=last)


def _event(*bks, commence=NOW + timedelta(hours=2), eid="e1"):
    return OddsEvent(
        id=eid,
        sport_key="baseball_mlb",
        commence_time=commence,
        home_team="New York Yankees",
        away_team="Boston Red Sox",
        bookmakers=bks,
    )


CASES = [
    (_event(_bk("a", {"New York Yankees": 1.90, "Boston Red Sox": 2.00}, age_min=1)), {}),
    (
        _event(
            _bk("a", {"New York Yankees": 1.90, "Boston Red Sox": 2.00}, age_min=1),
            _bk("b", {"New York Yankees": 1.85, "Boston Red Sox": 2.05}, age_min=3),
            _bk("c", {"New York Yankees": 1.95, "Boston Red Sox": 1.95}, age_min=20),
            _bk("d", {"New York Yankees": 1.88}, age_min=2),
        ),
        {"min_books": 2, "max_book_age_min": 15.0, "now": NOW},
    ),
    (_event(_bk("a", {"New York Yankees": 1.90})), {}),
]


def test_detector_and_core_agree_exactly():
    for event, kwargs in CASES:
        fair, _stats = consensus.consensus_fair_probs(event, **kwargs)
        assert _consensus_fair_probs(event, **kwargs) == fair


def test_core_reports_original_book_timestamps_not_fetch_time():
    event = CASES[1][0]
    fair, stats = consensus.consensus_fair_probs(event, **CASES[1][1])
    assert fair
    assert stats["bookmaker_keys"] == ("a", "b")
    # The stale book (20 min) and the incomplete one never contribute their timestamp.
    assert stats["oldest_book_update"] == NOW - timedelta(minutes=3)
    assert stats["newest_book_update"] == NOW - timedelta(minutes=1)


def test_too_few_books_reason_is_explicit():
    fair, stats = consensus.consensus_fair_probs(
        CASES[1][0], min_books=3, max_book_age_min=15.0, now=NOW
    )
    assert fair == {}
    assert stats["reason"] == "too_few_books"


def test_select_candidate_is_the_same_rule():
    a = _event(commence=NOW + timedelta(hours=1), eid="a")
    b = _event(commence=NOW + timedelta(hours=26), eid="b")
    for key in ("26SEP221900NYYBOS", "26SEP23NYYBOS", "NOKEY"):
        assert _select_candidate(key, [a, b], NOW) == consensus.select_candidate(key, [a, b], NOW)


def test_core_imports_without_site_packages():
    """`-S` hides loguru/pydantic/httpx exactly as the droplet's system python does."""
    code = (
        f"import sys; sys.path.append({str(ROOT)!r}); "
        "import src.strategies.motor_2_consensus.consensus as c; "
        "import src.strategies.motor_2_consensus.matcher as m; "
        "assert 'loguru' not in sys.modules and 'pydantic' not in sys.modules"
    )
    done = subprocess.run(
        [sys.executable, "-S", "-c", code], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
