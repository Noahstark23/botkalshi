"""Paridad: el fee de M5 research y el `SeriesFeePolicy` de Coolify leen IGUAL la API.

El droplet no puede importar `fee_policy.py` (arrastra `KalshiRestClient`), así que
`infra/digitalocean-shadow/m5_inputs.py` lee serie + evento por su cuenta. Este test
corre en la suite principal (con el venv) y ata los dos lectores a los MISMOS payloads:
misma tarifa y misma fuente cuando aceptan, y los dos rechazan cuando uno rechaza.
Así un cambio de un lado que no se replique en el otro rompe CI.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_5_mm.fee_policy import SeriesFeePolicy, UnsupportedSeriesFeeError

RESEARCH = Path(__file__).resolve().parents[3] / "infra" / "digitalocean-shadow"
sys.path.insert(0, str(RESEARCH))

import m5_inputs  # noqa: E402

SERIES = "KXMLBGAME"
EVENT = "KXMLBGAME-26AUG221805STLPHI"
NOW = datetime(2026, 9, 22, 19, 0, tzinfo=UTC)
SERIES_BODY = {
    "series": {"ticker": SERIES, "fee_type": "quadratic_with_maker_fees", "fee_multiplier": 0.5}
}


def _event(**override):
    return {"event": {"event_ticker": EVENT, "series_ticker": SERIES, **override}}


class _Reader:
    def __init__(self, event_body):
        self.bodies = {
            f"/series/{SERIES}": SERIES_BODY,
            f"/events/{EVENT}": event_body,
            "/series/fee_changes": {"series_fee_change_arr": []},
        }

    def get_json(self, origin, path, params=None):
        return self.bodies[path]


async def _coolify(event_body):
    client = AsyncMock()
    client.get_series.return_value = SERIES_BODY
    client.get_event.return_value = event_body
    try:
        obs = await SeriesFeePolicy(client).observe(f"{EVENT}-STL", event_ticker=EVENT)
    except UnsupportedSeriesFeeError:
        return None
    return (obs.fee_type, obs.multiplier, obs.source)


def _research(event_body):
    markets = [{"ticker": f"{EVENT}-STL", "event_ticker": EVENT, "status": "open"}]
    entries, _ = m5_inputs.produce_fees(
        _Reader(event_body), markets=markets, previous=[], now=NOW, clock=lambda: NOW
    )
    if not entries:
        return None
    [entry] = entries
    return (entry["fee_type"], Fraction(entry["fee_multiplier"]), entry["source"])


CASES = [
    _event(),
    _event(fee_type_override=None, fee_multiplier_override=None),
    _event(fee_type_override="quadratic_with_maker_fees", fee_multiplier_override=1),
    _event(fee_type_override="quadratic_with_maker_fees", fee_multiplier_override="0.25"),
    _event(fee_multiplier_override=1),  # partial → both refuse
    _event(fee_type_override="quadratic_with_maker_fees"),  # partial → both refuse
    _event(fee_type_override="flat", fee_multiplier_override=1),  # unsupported → both refuse
    {"event": {"event_ticker": "KXMLBGAME-OTHER", "series_ticker": SERIES}},  # identity
]


@pytest.mark.asyncio
@pytest.mark.parametrize("event_body", CASES)
async def test_research_reader_matches_coolify_fee_policy(event_body):
    assert _research(event_body) == await _coolify(event_body)
