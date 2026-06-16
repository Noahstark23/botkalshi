"""
PortfolioPoller (Motor 3, FASE 1) — sincroniza el estado REAL de la cuenta de Kalshi
con la memoria del bot. Read-only respecto al capital: solo lee posiciones y cachea.

Cada 60s: GET /portfolio/positions → por cada posición neta != 0, resuelve el `close_time`
del mercado (cruzando get_market, porque el endpoint de posiciones no lo trae) y hace
UPSERT en la tabla PortfolioPosition. Las posiciones que ya no están abiertas (cerradas/
settled) se PURGAN del cache.

El cliente (`_request`) ya trae retry + backoff exponencial + _record_api_error, así que
el poller no los reimplementa; un fallo de tick se registra y el loop SIGUE (patrón
SettlementPoller — Lección 7: nunca gather(return_exceptions), supervisor explícito).

Convención de tiempo: NAIVE UTC (close_time/synced_at) — comparable sin TypeError.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from datetime import UTC, datetime

from loguru import logger
from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.storage.models import PortfolioPosition, get_session


def _as_int(value: object) -> int | None:
    """Castea a int un campo que puede venir int o fixed-point string. None si inválido."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(round(float(value)))
        except (ValueError, TypeError):
            return None
    return None


def _parse_close_time(raw: object) -> datetime | None:
    """ISO 8601 ('...Z') → datetime NAIVE UTC. None si ausente/inválido (fail-safe)."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(UTC).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _naive_utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class PortfolioPoller:
    """Sincroniza posiciones abiertas → PortfolioPosition. NO toca capital."""

    POLL_INTERVAL_SEC = 60.0
    _GET_MARKET_PAUSE_SEC = 0.3  # cortesía entre get_market (solo para posiciones sin close_time)

    def __init__(
        self, *, client_factory: Callable[[], KalshiRestClient] = KalshiRestClient
    ) -> None:
        self._client_factory = client_factory

    async def run(self, stop_event: asyncio.Event) -> None:
        """Loop supervisado: un fallo de tick se registra y el loop SIGUE."""
        logger.info(f"motor3.poller started interval={self.POLL_INTERVAL_SEC:.0f}s")
        while not stop_event.is_set():
            try:
                n = await self.sync_once()
                logger.info(f"motor3.poller.synced posiciones_abiertas={n}")
            except Exception as exc:
                logger.exception("motor3.poller.tick_failed")
                with contextlib.suppress(Exception):
                    from src.monitoring.health import BotState

                    BotState.record_error(f"motor3.poller: {type(exc).__name__}: {exc}")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=self.POLL_INTERVAL_SEC)

    async def sync_once(self) -> int:
        """Un tick: trae posiciones, resuelve close_time, upsert + purga. Devuelve nº abiertas."""
        # close_time cacheado: es ESTÁTICO por market → no re-consultar get_market si ya lo tenemos.
        with get_session() as s:
            cached_close = {
                p.ticker: p.close_time
                for p in s.exec(select(PortfolioPosition))
                if p.close_time is not None
            }

        async with self._client_factory() as client:
            raw_positions = await self._fetch_all_positions(client)
            # {ticker → (side, count, exposure_cents)} de las posiciones netas != 0.
            open_now: dict[str, tuple[str, int, int | None]] = {}
            for p in raw_positions:
                ticker = str(p.get("ticker", ""))
                pos = _as_int(p.get("position"))
                if not ticker or pos is None or pos == 0:
                    continue  # sin posición abierta en este market
                side = "yes" if pos > 0 else "no"
                open_now[ticker] = (side, abs(pos), _as_int(p.get("market_exposure")))

            # close_time: reusar el cacheado; solo get_market para los que falten.
            close_times: dict[str, datetime | None] = {}
            for ticker in open_now:
                if ticker in cached_close:
                    close_times[ticker] = cached_close[ticker]
                    continue
                try:
                    resp = await client.get_market(ticker)
                    market = resp.get("market", resp) if isinstance(resp, dict) else {}
                    close_times[ticker] = _parse_close_time(market.get("close_time"))
                except Exception as e:
                    logger.warning(
                        f"motor3.poller.get_market({ticker}) error: {type(e).__name__}: {e}"
                    )
                    close_times[ticker] = None
                await asyncio.sleep(self._GET_MARKET_PAUSE_SEC)

        self._persist(open_now, close_times)
        return len(open_now)

    async def _fetch_all_positions(self, client: KalshiRestClient) -> list[dict]:
        """Todas las market_positions, paginando por cursor (defensivo; normalmente 1 página)."""
        out: list[dict] = []
        cursor: str | None = None
        while True:
            resp = await client.get_positions(limit=100, cursor=cursor)
            if not isinstance(resp, dict):
                break
            out.extend(resp.get("market_positions", resp.get("positions", [])) or [])
            cursor = resp.get("cursor") or None
            if not cursor:
                break
        return out

    def _persist(
        self,
        open_now: dict[str, tuple[str, int, int | None]],
        close_times: dict[str, datetime | None],
    ) -> None:
        """UPSERT de las posiciones abiertas + PURGA de las que ya no lo están. Best-effort."""
        now = _naive_utc_now()
        try:
            with get_session() as s:
                existing = {p.ticker: p for p in s.exec(select(PortfolioPosition))}
                # Upsert
                for ticker, (side, count, exposure) in open_now.items():
                    row = existing.get(ticker)
                    if row is None:
                        row = PortfolioPosition(ticker=ticker, side=side, count=count)
                    row.side = side
                    row.count = count
                    row.exposure_cents = exposure
                    row.close_time = close_times.get(ticker)
                    row.synced_at = now
                    s.add(row)
                # Purga: posiciones cacheadas que ya no están abiertas (cerradas/settled).
                for ticker, row in existing.items():
                    if ticker not in open_now:
                        s.delete(row)
                s.commit()
        except Exception:
            logger.exception("motor3.poller.persist_failed")
