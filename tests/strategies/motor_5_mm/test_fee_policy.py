"""Fee efectiva M5: base de serie + override por evento, con cache fail-closed."""

from __future__ import annotations

from fractions import Fraction
from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_5_mm.fee_policy import SeriesFeePolicy, UnsupportedSeriesFeeError

SERIES = {
    "series": {
        "ticker": "KXMLBGAME",
        "fee_type": "quadratic_with_maker_fees",
        "fee_multiplier": 0.5,
    }
}
EVENT = "KXMLBGAME-26AUG221805STLPHI"


def _event(*, override_type=None, override_multiplier=None, event_ticker: str = EVENT):
    return {
        "event": {
            "event_ticker": event_ticker,
            "series_ticker": "KXMLBGAME",
            "fee_type_override": override_type,
            "fee_multiplier_override": override_multiplier,
        }
    }


@pytest.mark.asyncio
async def test_sin_override_usa_base_y_cachea_por_evento():
    client = AsyncMock()
    client.get_series.return_value = SERIES
    client.get_event.return_value = _event()
    policy = SeriesFeePolicy(client)

    first = await policy.observe(f"{EVENT}-STL", event_ticker=EVENT)
    second = await policy.observe(f"{EVENT}-PHI", event_ticker=EVENT)

    assert first.multiplier == Fraction(1, 2)
    assert first.source == "series"
    assert first.event_ticker == EVENT
    assert second is first
    client.get_series.assert_awaited_once_with("KXMLBGAME")
    client.get_event.assert_awaited_once_with(EVENT)


@pytest.mark.asyncio
async def test_override_evento_gana_sobre_base_de_serie():
    client = AsyncMock()
    client.get_series.return_value = SERIES
    client.get_event.return_value = _event(
        override_type="quadratic_with_maker_fees", override_multiplier=1
    )

    observed = await SeriesFeePolicy(client).observe(f"{EVENT}-STL", event_ticker=EVENT)

    assert observed.multiplier == 1
    assert observed.base_multiplier == Fraction(1, 2)
    assert observed.override_multiplier == 1
    assert observed.source == "event_override"


@pytest.mark.asyncio
async def test_cache_serie_se_comparte_pero_eventos_se_validan_separados():
    other = "KXMLBGAME-26AUG221910MIACIN"
    client = AsyncMock()
    client.get_series.return_value = SERIES
    client.get_event.side_effect = [_event(), _event(event_ticker=other)]
    policy = SeriesFeePolicy(client)

    await policy.observe(f"{EVENT}-STL", event_ticker=EVENT)
    await policy.observe(f"{other}-MIA", event_ticker=other)

    client.get_series.assert_awaited_once()
    assert client.get_event.await_count == 2


@pytest.mark.asyncio
async def test_multiplicador_base_cambiado_bloquea_la_serie():
    client = AsyncMock()
    client.get_series.return_value = {
        "series": {"fee_type": "quadratic_with_maker_fees", "fee_multiplier": 1}
    }

    with pytest.raises(UnsupportedSeriesFeeError, match="base cambió"):
        await SeriesFeePolicy(client).observe(f"{EVENT}-STL", event_ticker=EVENT)
    client.get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_tipo_base_sin_maker_fees_falla_cerrado():
    client = AsyncMock()
    client.get_series.return_value = {"series": {"fee_type": "quadratic", "fee_multiplier": 0.5}}

    with pytest.raises(UnsupportedSeriesFeeError, match="base no soportado"):
        await SeriesFeePolicy(client).observe(f"{EVENT}-STL", event_ticker=EVENT)


@pytest.mark.asyncio
async def test_override_parcial_falla_cerrado():
    client = AsyncMock()
    client.get_series.return_value = SERIES
    client.get_event.return_value = _event(
        override_type="quadratic_with_maker_fees", override_multiplier=None
    )

    with pytest.raises(UnsupportedSeriesFeeError, match="override parcial"):
        await SeriesFeePolicy(client).observe(f"{EVENT}-STL", event_ticker=EVENT)


@pytest.mark.asyncio
async def test_evento_incompatible_no_hace_requests():
    client = AsyncMock()

    with pytest.raises(UnsupportedSeriesFeeError, match="evento incompatible"):
        await SeriesFeePolicy(client).observe(f"{EVENT}-STL", event_ticker="KXNFLGAME-X")
    client.get_series.assert_not_awaited()
    client.get_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_serie_desconocida_no_hace_request_ni_cotiza():
    client = AsyncMock()

    with pytest.raises(UnsupportedSeriesFeeError, match="sin aserción"):
        await SeriesFeePolicy(client).observe("KXUNKNOWN-X-Y", event_ticker="KXUNKNOWN-X")
    client.get_series.assert_not_awaited()


@pytest.mark.asyncio
async def test_identidad_del_payload_evento_se_valida():
    client = AsyncMock()
    client.get_series.return_value = SERIES
    client.get_event.return_value = _event(event_ticker="KXMLBGAME-OTRO")

    with pytest.raises(UnsupportedSeriesFeeError, match="identidad de evento inconsistente"):
        await SeriesFeePolicy(client).observe(f"{EVENT}-STL", event_ticker=EVENT)


@pytest.mark.asyncio
async def test_fallo_de_serie_se_cachea_y_no_martilla_api():
    client = AsyncMock()
    client.get_series.side_effect = RuntimeError("temporal")
    policy = SeriesFeePolicy(client, negative_ttl_sec=300)

    for ticker in (f"{EVENT}-STL", f"{EVENT}-PHI"):
        with pytest.raises(UnsupportedSeriesFeeError, match="GET /series/KXMLBGAME falló"):
            await policy.observe(ticker, event_ticker=EVENT)

    client.get_series.assert_awaited_once_with("KXMLBGAME")
    client.get_event.assert_not_awaited()
