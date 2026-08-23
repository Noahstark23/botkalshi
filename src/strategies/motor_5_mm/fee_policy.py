"""Política de fee efectiva, fail-closed, para las quotes maker del Motor 5.

Kalshi publica una política base por serie y puede superponer un override por evento.
Cotizar usando solo ``GET /series`` produce una fee falsa cuando el evento trae
``fee_type_override``/``fee_multiplier_override``. Este módulo consulta ambas fuentes,
persiste la procedencia efectiva y nunca inventa una tarifa favorable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction

from src.clients.kalshi_rest import KalshiRestClient

SUPPORTED_FEE_TYPE = "quadratic_with_maker_fees"

# Aserciones de la política BASE verificadas contra GET /series el 2026-08-22. Un
# override de evento puede diferir; se acepta únicamente cuando GET /events/{event}
# lo declara explícitamente y conserva el tipo soportado.
EXPECTED_BASE_MULTIPLIERS: dict[str, Fraction] = {
    "KXMLBGAME": Fraction(1, 2),
    "KXNFLGAME": Fraction(1, 1),
    "KXNBAGAME": Fraction(1, 1),
    "KXEPLGAME": Fraction(1, 1),
    "KXUCLGAME": Fraction(1, 1),
    "KXWCGAME": Fraction(1, 1),
    "KXMENWORLDCUP": Fraction(1, 1),
}


class UnsupportedSeriesFeeError(RuntimeError):
    """La fee no se pudo demostrar; la única respuesta segura es no cotizar."""


@dataclass(frozen=True, slots=True)
class EffectiveFeeObservation:
    series_ticker: str
    fee_type: str
    multiplier: Fraction
    observed_at: datetime
    event_ticker: str = ""
    source: str = "series"  # "series" | "event_override"
    base_fee_type: str = SUPPORTED_FEE_TYPE
    base_multiplier: Fraction = Fraction(1, 1)
    override_fee_type: str | None = None
    override_multiplier: Fraction | None = None


# Alias temporal para callers/tests históricos del branch. El dato ya no es solo serie.
SeriesFeeObservation = EffectiveFeeObservation


@dataclass(frozen=True, slots=True)
class _SeriesSchedule:
    fee_type: str
    multiplier: Fraction


class SeriesFeePolicy:
    """Resuelve serie + override de evento y cachea sin perder fail-closed.

    Serie y evento se refrescan cada 30 segundos: una quote expuesta se liquida solo tras
    demostrar que ambos schedules siguen iguales. Los fallos se cachean brevemente para
    no martillar la API en cada ticker/tick.
    """

    def __init__(
        self,
        client: KalshiRestClient,
        *,
        series_ttl_sec: float = 30.0,
        event_ttl_sec: float = 30.0,
        negative_ttl_sec: float = 300.0,
    ) -> None:
        self._client = client
        self._series_ttl_sec = series_ttl_sec
        self._event_ttl_sec = event_ttl_sec
        self._negative_ttl_sec = negative_ttl_sec
        self._series_cache: dict[str, tuple[float, _SeriesSchedule]] = {}
        self._event_cache: dict[str, tuple[float, EffectiveFeeObservation]] = {}
        self._negative_cache: dict[str, tuple[float, str]] = {}

    @staticmethod
    def series_from_ticker(ticker: str) -> str:
        return ticker.split("-", 1)[0].upper()

    @staticmethod
    def _fraction(value: object, *, label: str) -> Fraction:
        try:
            result = Fraction(str(value))
        except (ValueError, ZeroDivisionError) as exc:
            raise UnsupportedSeriesFeeError(f"{label} inválido: {value!r}") from exc
        if result <= 0:
            raise UnsupportedSeriesFeeError(f"{label} debe ser positivo: {value!r}")
        return result

    def _negative_hit(self, key: str, now_mono: float) -> None:
        negative = self._negative_cache.get(key)
        if negative is not None and now_mono - negative[0] < self._negative_ttl_sec:
            raise UnsupportedSeriesFeeError(negative[1])

    def _fail(self, key: str, now_mono: float, message: str) -> UnsupportedSeriesFeeError:
        self._negative_cache[key] = (now_mono, message)
        return UnsupportedSeriesFeeError(message)

    async def _series_schedule(self, series: str, now_mono: float) -> _SeriesSchedule:
        expected = EXPECTED_BASE_MULTIPLIERS.get(series)
        if expected is None:
            raise UnsupportedSeriesFeeError(f"serie sin aserción de fee: {series}")
        cached = self._series_cache.get(series)
        if cached is not None and now_mono - cached[0] < self._series_ttl_sec:
            return cached[1]
        key = f"series:{series}"
        self._negative_hit(key, now_mono)
        try:
            payload = await self._client.get_series(series)
        except Exception as exc:
            message = f"GET /series/{series} falló: {type(exc).__name__}: {exc}"
            raise self._fail(key, now_mono, message) from exc
        raw = payload.get("series") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            raise self._fail(key, now_mono, f"GET /series/{series} sin objeto series")
        fee_type = raw.get("fee_type")
        if fee_type != SUPPORTED_FEE_TYPE:
            raise self._fail(
                key, now_mono, f"fee_type base no soportado para {series}: {fee_type!r}"
            )
        try:
            multiplier = self._fraction(
                raw.get("fee_multiplier"), label=f"fee_multiplier base para {series}"
            )
        except UnsupportedSeriesFeeError as exc:
            raise self._fail(key, now_mono, str(exc)) from exc
        if multiplier != expected:
            message = (
                f"fee_multiplier base cambió para {series}: API={float(multiplier):g}, "
                f"esperado={float(expected):g}"
            )
            raise self._fail(key, now_mono, message)
        schedule = _SeriesSchedule(fee_type=fee_type, multiplier=multiplier)
        self._negative_cache.pop(key, None)
        self._series_cache[series] = (now_mono, schedule)
        return schedule

    async def observe(self, ticker: str, *, event_ticker: str) -> EffectiveFeeObservation:
        """Devuelve la tarifa efectiva demostrada para ``ticker`` y su evento oficial.

        ``event_ticker`` viene del matching M2/Kalshi, no se deriva recortando el market
        ticker: ese convenio no forma parte del contrato de la API.
        """
        series = self.series_from_ticker(ticker)
        if not event_ticker or self.series_from_ticker(event_ticker) != series:
            raise UnsupportedSeriesFeeError(
                f"evento incompatible con market: market={ticker}, event={event_ticker!r}"
            )
        now_mono = time.monotonic()
        cached = self._event_cache.get(event_ticker)
        if cached is not None and now_mono - cached[0] < self._event_ttl_sec:
            return cached[1]

        base = await self._series_schedule(series, now_mono)
        key = f"event:{event_ticker}"
        self._negative_hit(key, now_mono)
        try:
            payload = await self._client.get_event(event_ticker)
        except Exception as exc:
            message = f"GET /events/{event_ticker} falló: {type(exc).__name__}: {exc}"
            raise self._fail(key, now_mono, message) from exc
        raw = payload.get("event") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            raise self._fail(key, now_mono, f"GET /events/{event_ticker} sin objeto event")
        if raw.get("event_ticker") != event_ticker or raw.get("series_ticker") != series:
            raise self._fail(
                key,
                now_mono,
                f"identidad de evento inconsistente para {event_ticker}: "
                f"event={raw.get('event_ticker')!r}, series={raw.get('series_ticker')!r}",
            )

        override_type = raw.get("fee_type_override")
        raw_override_multiplier = raw.get("fee_multiplier_override")
        if override_type is None and raw_override_multiplier is None:
            source = "series"
            fee_type = base.fee_type
            multiplier = base.multiplier
            override_multiplier = None
        elif override_type is None or raw_override_multiplier is None:
            raise self._fail(
                key,
                now_mono,
                f"override parcial para {event_ticker}: "
                f"type={override_type!r}, multiplier={raw_override_multiplier!r}",
            )
        else:
            if override_type != SUPPORTED_FEE_TYPE:
                raise self._fail(
                    key,
                    now_mono,
                    f"fee_type override no soportado para {event_ticker}: {override_type!r}",
                )
            try:
                override_multiplier = self._fraction(
                    raw_override_multiplier,
                    label=f"fee_multiplier override para {event_ticker}",
                )
            except UnsupportedSeriesFeeError as exc:
                raise self._fail(key, now_mono, str(exc)) from exc
            source = "event_override"
            fee_type = override_type
            multiplier = override_multiplier

        observation = EffectiveFeeObservation(
            series_ticker=series,
            event_ticker=event_ticker,
            fee_type=fee_type,
            multiplier=multiplier,
            source=source,
            base_fee_type=base.fee_type,
            base_multiplier=base.multiplier,
            override_fee_type=override_type,
            override_multiplier=override_multiplier,
            observed_at=datetime.now(UTC),
        )
        self._negative_cache.pop(key, None)
        self._event_cache[event_ticker] = (now_mono, observation)
        return observation
