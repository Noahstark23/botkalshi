"""
Fuentes del cable shadow del Motor 2 — el plumbing entre Kalshi y The Odds API.

Diseño clave (decisión del owner): el cable se construye con una fuente de odds FAKE
para que el pipeline completo (extractor de quotes de Kalshi → matcher → detector →
grabado) corra HOY, sin pagar la suscripción. El día que se paga, cambiar a la fuente
real es UNA LÍNEA — `FakeOddsSource(...)` → `LiveOddsSource([...])` — y nada más en el
poller cambia.

Dos seams simétricos (Protocols), ambos `async def fetch()`:
  - OddsSource: el consenso de sportsbooks (fake/fixture hoy, The Odds API mañana).
  - KalshiQuoteSource: los quotes 1X2 de Kalshi (yes/no ask + nombre real del outcome).

Invariante de seguridad: NINGUNA de estas fuentes ejecuta capital. La fuente fake se
marca `is_live=False` para que el poller NO persista señales basura (las odds fake dan
edges sin sentido); solo loguea. Con la fuente real (`is_live=True`) el poller graba.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from loguru import logger

from src.clients.kalshi_rest import KalshiRestClient
from src.clients.odds_api import OddsAPIClient, OddsEvent
from src.strategies.data_capture import parse_price_to_cents
from src.strategies.motor_2_consensus.detector import KalshiEventQuotes, KalshiQuote

# Pausa entre get_event para no martillar la API de Kalshi (read-only, background).
_KALSHI_EVENT_PAUSE_SEC = 0.3


# =====================================================
# OddsSource — el consenso de sportsbooks
# =====================================================


@runtime_checkable
class OddsSource(Protocol):
    """Fuente de eventos de odds. `is_live` distingue fixture de The Odds API real."""

    is_live: bool

    async def fetch(self) -> list[OddsEvent]:
        ...


class FakeOddsSource:
    """
    Fuente FAKE: devuelve un fixture fijo de OddsEvents. NO toca red.

    Existe para correr el cable end-to-end sin la suscripción paga. Se marca
    `is_live=False` → el poller loguea pero NO persiste (las edges fake no son data real).
    """

    is_live = False

    def __init__(self, events: list[OddsEvent]):
        self._events = events

    async def fetch(self) -> list[OddsEvent]:
        logger.warning(
            f"motor2.odds FAKE source — {len(self._events)} eventos fixture "
            "(edges NO reales; cambiar a LiveOddsSource al pagar la API)"
        )
        return list(self._events)


class LiveOddsSource:
    """
    Fuente REAL: The Odds API vía OddsAPIClient, sobre una lista de sport_keys.

    El flip de una línea: el poller se construye con esta en vez de FakeOddsSource.
    Requiere ODDS_API_KEY configurada (el cliente la lee de settings).
    """

    is_live = True

    def __init__(
        self,
        sport_keys: list[str],
        *,
        client_factory: Callable[[], OddsAPIClient] = OddsAPIClient,
    ):
        self._sport_keys = sport_keys
        self._client_factory = client_factory

    async def fetch(self) -> list[OddsEvent]:
        events: list[OddsEvent] = []
        async with self._client_factory() as client:
            for sk in self._sport_keys:
                try:
                    events.extend(await client.get_odds(sk))
                except Exception as e:  # un sport_key que falla no tira el batch entero
                    logger.warning(f"motor2.odds get_odds({sk}) error: {type(e).__name__}: {e}")
        logger.info(f"motor2.odds LIVE — {len(events)} eventos de {len(self._sport_keys)} deportes")
        return events


# =====================================================
# KalshiQuoteSource — los quotes 1X2 de Kalshi
# =====================================================


@runtime_checkable
class KalshiQuoteSource(Protocol):
    async def fetch(self) -> list[KalshiEventQuotes]:
        ...


def _parse_event_quotes(event_key: str, markets: list[dict]) -> KalshiEventQuotes | None:
    """
    Markets crudos de get_event → KalshiEventQuotes con el NOMBRE REAL del outcome.

    El nombre sale de `yes_sub_title` ("Argentina", "Draw", ...) — lo que el matcher
    cruza contra The Odds API. Un market sin nombre o sin ambos asks se descarta
    (fail-safe). Solo devuelve el evento si quedan ≥2 outcomes usables.
    """
    quotes: list[KalshiQuote] = []
    for m in markets:
        ticker = m.get("ticker")
        name = m.get("yes_sub_title") or m.get("yes_subtitle") or ""
        yes_ask = parse_price_to_cents(m.get("yes_ask_dollars") or m.get("yes_ask"))
        no_ask = parse_price_to_cents(m.get("no_ask_dollars") or m.get("no_ask"))
        if ticker and name and yes_ask and no_ask:
            quotes.append(
                KalshiQuote(
                    market_ticker=ticker,
                    outcome_name=name,
                    yes_ask_cents=yes_ask,
                    no_ask_cents=no_ask,
                )
            )
    if len(quotes) < 2:
        return None
    return KalshiEventQuotes(event_key=event_key, outcomes=tuple(quotes))


class RestKalshiQuoteSource:
    """
    Extractor de quotes 1X2 de Kalshi vía REST (get_event por evento).

    `universe_provider` devuelve {event_key → tickers hermanos} (los partidos 1X2
    descubiertos por el data_capture). Por cada evento, un get_event trae los markets
    con su `yes_sub_title` + asks en una sola llamada. Read-only, fail-safe por evento.
    """

    def __init__(
        self,
        universe_provider: Callable[[], dict[str, set[str]]],
        *,
        client_factory: Callable[[], KalshiRestClient] = KalshiRestClient,
        pause_sec: float = _KALSHI_EVENT_PAUSE_SEC,
    ):
        self._universe_provider = universe_provider
        self._client_factory = client_factory
        self._pause_sec = pause_sec

    async def fetch(self) -> list[KalshiEventQuotes]:
        import asyncio

        universe = self._universe_provider()
        if not universe:
            logger.info("motor2.kalshi universo vacío (discovery aún sin partidos 1X2)")
            return []
        out: list[KalshiEventQuotes] = []
        async with self._client_factory() as client:
            for event_key in universe:
                try:
                    detail = await client.get_event(event_key)
                except Exception as e:
                    logger.warning(f"motor2.kalshi get_event({event_key}) error: {type(e).__name__}: {e}")
                    continue
                eq = _parse_event_quotes(event_key, detail.get("markets", []))
                if eq is not None:
                    out.append(eq)
                if self._pause_sec:
                    await asyncio.sleep(self._pause_sec)
        logger.info(f"motor2.kalshi {len(out)}/{len(universe)} eventos 1X2 con quotes usables")
        return out
