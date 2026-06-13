"""
Cliente asíncrono para The Odds API (v4) — consenso de sportsbooks (Motor 2).

Provee las cuotas de los books de consenso (Pinnacle / DK / FD / MGM) que el detector
del Motor 2 usa como benchmark de "precio justo" tras remover el vig (ver math/no_vig).

Patrón espejo de KalshiRestClient:
  - httpx async + context manager.
  - Auth por query param `apiKey` (no firma — el API es key-based).
  - Retries con backoff exponencial (tenacity, max=60s) ante 429/5xx/red.
  - Errores registrados vía BotState.record_error() → visibles en /status.

Devuelve modelos TIPADOS (OddsEvent/Bookmaker/Market/Outcome). El parseo es fail-safe:
un evento malformado se descarta (log), no tira el batch entero.

NO toca capital ni ejecución. NO escribe SQLite (no aplica la regla de naive-UTC; los
commence_time se parsean a datetime AWARE en UTC para comparación del matcher).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.utils.config import get_settings

ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4"


class OddsAPIError(Exception):
    """Error genérico de The Odds API."""

    def __init__(self, status_code: int, message: str, body: str = ""):
        self.status_code = status_code
        self.message = message
        self.body = body
        super().__init__(f"[{status_code}] {message}")


class OddsAPIRateLimitError(OddsAPIError):
    """429 — cuota agotada; retryable con backoff."""


class OddsAPIServerError(OddsAPIError):
    """5xx — retryable."""


class OddsAPIAuthError(OddsAPIError):
    """401 — API key inválida o faltante; NO retry."""


_RETRYABLE_EXCEPTIONS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    OddsAPIRateLimitError,
    OddsAPIServerError,
)


# =====================================================
# Modelos tipados (shape de The Odds API v4)
# =====================================================


@dataclass(frozen=True, slots=True)
class Outcome:
    name: str  # nombre del equipo/resultado ("Argentina", "Draw", ...)
    price: float  # cuota DECIMAL (oddsFormat=decimal)


@dataclass(frozen=True, slots=True)
class Market:
    key: str  # "h2h" (moneyline), "spreads", "totals"
    outcomes: tuple[Outcome, ...]


@dataclass(frozen=True, slots=True)
class Bookmaker:
    key: str  # "pinnacle", "draftkings", ...
    title: str
    markets: tuple[Market, ...]


@dataclass(frozen=True, slots=True)
class OddsEvent:
    id: str
    sport_key: str
    commence_time: datetime  # AWARE, UTC
    home_team: str
    away_team: str
    bookmakers: tuple[Bookmaker, ...]


def _parse_commence_time(raw: str) -> datetime:
    """ISO 8601 ('...Z') → datetime aware en UTC."""
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt.astimezone(UTC)


def _parse_event(d: dict[str, Any]) -> OddsEvent | None:
    """Parsea un evento crudo a OddsEvent. None si el shape es inválido (fail-safe)."""
    try:
        bookmakers = tuple(
            Bookmaker(
                key=str(bk["key"]),
                title=str(bk.get("title", "")),
                markets=tuple(
                    Market(
                        key=str(mk["key"]),
                        outcomes=tuple(
                            Outcome(name=str(o["name"]), price=float(o["price"]))
                            for o in mk.get("outcomes", [])
                        ),
                    )
                    for mk in bk.get("markets", [])
                ),
            )
            for bk in d.get("bookmakers", [])
        )
        return OddsEvent(
            id=str(d["id"]),
            sport_key=str(d["sport_key"]),
            commence_time=_parse_commence_time(str(d["commence_time"])),
            home_team=str(d["home_team"]),
            away_team=str(d["away_team"]),
            bookmakers=bookmakers,
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning(
            f"odds_api: evento descartado por shape inválido: {type(exc).__name__}: {exc}"
        )
        return None


def _record_api_error(path: str, exc: Exception) -> None:
    """Registra el error en BotState para /status. Lazy import (evita ciclos)."""
    try:
        from src.monitoring.health import BotState

        BotState.record_error(f"OddsAPI {path}: {type(exc).__name__}: {str(exc)[:200]}")
    except Exception:
        pass  # best-effort; nunca dejar que el logging tire el request


class OddsAPIClient:
    """
    Cliente async de The Odds API.

    Uso:
        async with OddsAPIClient() as client:
            events = await client.get_odds("soccer_fifa_world_cup")
    """

    RETRY_ATTEMPTS = 4
    RETRY_WAIT_MIN = 1.0  # s — primer backoff
    RETRY_WAIT_MAX = 60.0  # s — cap del backoff (regla del brief)

    def __init__(
        self, api_key: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.api_key = api_key if api_key is not None else get_settings().ODDS_API_KEY
        self._transport = transport  # seam de testing (httpx.MockTransport)
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> OddsAPIClient:
        self._client = httpx.AsyncClient(
            base_url=ODDS_API_BASE_URL,
            timeout=httpx.Timeout(15.0, connect=5.0),
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @staticmethod
    def _classify(status_code: int, body: str) -> OddsAPIError:
        if status_code == 401:
            return OddsAPIAuthError(status_code, "API key inválida o faltante", body)
        if status_code == 429:
            return OddsAPIRateLimitError(status_code, "Rate limit / cuota agotada", body)
        if 500 <= status_code < 600:
            return OddsAPIServerError(status_code, "Server error", body)
        return OddsAPIError(status_code, "Request error", body)

    async def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET firmado con apiKey + retries con backoff exponencial (max=60s)."""
        if self._client is None:
            raise RuntimeError("Cliente no inicializado. Usar `async with OddsAPIClient()`")
        if not self.api_key:
            raise OddsAPIAuthError(401, "ODDS_API_KEY no configurada")

        query: dict[str, Any] = {"apiKey": self.api_key, **(params or {})}
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.RETRY_ATTEMPTS),
                wait=wait_exponential(
                    multiplier=1, min=self.RETRY_WAIT_MIN, max=self.RETRY_WAIT_MAX
                ),
                retry=retry_if_exception_type(_RETRYABLE_EXCEPTIONS),
                reraise=True,
            ):
                with attempt:
                    resp = await self._client.get(path, params=query)
                    if resp.status_code >= 400:
                        err = self._classify(resp.status_code, resp.text[:500])
                        logger.warning(
                            f"odds_api GET {path} → {resp.status_code}: {resp.text[:200]}"
                        )
                        raise err
                    return resp.json()
        except Exception as exc:
            _record_api_error(path, exc)
            raise
        raise RuntimeError("Retries inesperadamente agotados")  # inalcanzable (mypy)

    # =====================================================
    # API pública
    # =====================================================

    async def get_sports(self) -> list[dict[str, Any]]:
        """Lista de deportes disponibles (para descubrir sport_keys, ej. del Mundial)."""
        data = await self._request("/sports")
        return data if isinstance(data, list) else []

    async def get_odds(
        self,
        sport_key: str,
        *,
        regions: str = "us",
        markets: str = "h2h",
        odds_format: str = "decimal",
    ) -> list[OddsEvent]:
        """
        Cuotas de los eventos de un deporte. Default: moneyline (h2h), región us, decimal.

        Devuelve OddsEvent tipados; los eventos con shape inválido se descartan (fail-safe).
        """
        data = await self._request(
            f"/sports/{sport_key}/odds",
            params={"regions": regions, "markets": markets, "oddsFormat": odds_format},
        )
        if not isinstance(data, list):
            return []
        parsed = [_parse_event(d) for d in data]
        return [e for e in parsed if e is not None]
