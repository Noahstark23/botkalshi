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
from datetime import UTC, datetime, timedelta

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


def _money_to_cents(position: dict, name: str) -> int | None:
    """
    Lee un campo de DINERO de una posición Kalshi y lo devuelve en CENTAVOS.

    Convención Kalshi: el dinero viene como `<name>_dollars` (string USD fixed-point, ej.
    "0.22") y/o `<name>` (entero en centavos, histórico). El sufijo `_fp` es para CONTEOS
    de contratos, NO para dinero — por eso `market_exposure_fp` nunca existió y exposure_cents
    quedaba None. Devuelve None si no aparece en ningún shape conocido.

    Coalesce por `is not None` (deuda auditoría 2026-07-01): antes `get(name, get(name_cents))`
    solo caía al `_cents` si la key plana estaba AUSENTE — presente con None la enmascaraba.
    """
    dollars = position.get(f"{name}_dollars")
    if dollars is not None:
        try:
            return int(round(float(dollars) * 100))
        except (TypeError, ValueError):
            pass
    flat = _as_int(position.get(name))
    if flat is not None:
        return flat
    return _as_int(position.get(f"{name}_cents"))


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
    _GET_MARKET_PAUSE_SEC = 0.3  # cortesía entre get_market (solo cuando hay que consultarlo)
    # Refresh del close_time cerca del cierre (deuda auditoría 2026-07-01): NO es estático
    # — Kalshi extiende mercados (prórrogas) y cierra anticipado (determinación temprana).
    # Lejos del cierre el cache vale; dentro de esta ventana se re-consulta para que la
    # ventana de salida T-30 se calcule sobre el close REAL.
    _CLOSE_REFRESH_WINDOW = timedelta(minutes=45)
    # Guard de purga (deuda auditoría): una respuesta 200 "vacía" (shape-drift / respuesta
    # parcial) borraría TODAS las filas de golpe y el trailing perdería sus peaks. Un
    # resultado vacío con filas existentes se confirma en el tick siguiente antes de purgar.
    _MAX_PAGES = 20  # tope defensivo de paginación (~2000 posiciones; hoy son <100)

    def __init__(
        self, *, client_factory: Callable[[], KalshiRestClient] = KalshiRestClient
    ) -> None:
        self._client_factory = client_factory
        # Diagnóstico one-shot: si el campo de exposición no se resuelve en ningún shape
        # conocido, se loguean las keys crudas UNA vez (para descubrir el nombre real).
        self._exposure_keys_logged = False
        # Guard de purga total: nº de syncs consecutivos con resultado vacío.
        self._empty_syncs = 0

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
        # close_time cacheado: vale mientras el cierre esté LEJOS; dentro de la ventana de
        # refresh se re-consulta (el close_time de deportes cambia: prórroga/early close).
        now = _naive_utc_now()
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
                # Kalshi devuelve los numéricos como fixed-point string en el campo
                # `<name>_fp` (ej. position_fp="-1.00"); el `<name>` plano puede no venir.
                # Leer el _fp primero con fallback al plano (robusto ante ambos shapes).
                pos = _as_int(p.get("position_fp", p.get("position")))
                if not ticker or pos is None or pos == 0:
                    continue  # sin posición abierta en este market
                side = "yes" if pos > 0 else "no"
                exposure = _money_to_cents(p, "market_exposure")
                if exposure is None and not self._exposure_keys_logged:
                    self._exposure_keys_logged = True
                    logger.info(
                        f"motor3.poller.exposure_unresolved ticker={ticker} "
                        f"keys={sorted(p.keys())} (campo de exposición ausente en shapes "
                        "conocidos → agregar el nombre real)"
                    )
                open_now[ticker] = (side, abs(pos), exposure)

            # close_time: reusar el cacheado salvo que esté dentro de la ventana de refresh.
            close_times: dict[str, datetime | None] = {}
            for ticker in open_now:
                cached = cached_close.get(ticker)
                if cached is not None and cached - now > self._CLOSE_REFRESH_WINDOW:
                    close_times[ticker] = cached
                    continue
                try:
                    resp = await client.get_market(ticker)
                    market = resp.get("market", resp) if isinstance(resp, dict) else {}
                    fresh = _parse_close_time(market.get("close_time"))
                    # Fail-safe: si el refresh no resuelve, conservar el cacheado.
                    close_times[ticker] = fresh if fresh is not None else cached
                    if fresh is not None and cached is not None and fresh != cached:
                        logger.warning(
                            f"motor3.poller.close_time_moved ticker={ticker} "
                            f"{cached.isoformat()} -> {fresh.isoformat()} "
                            "(prórroga/early close: la ventana T-30 se recalcula)"
                        )
                except Exception as e:
                    logger.warning(
                        f"motor3.poller.get_market({ticker}) error: {type(e).__name__}: {e}"
                    )
                    close_times[ticker] = cached
                await asyncio.sleep(self._GET_MARKET_PAUSE_SEC)

        self._persist(open_now, close_times)
        return len(open_now)

    async def _fetch_all_positions(self, client: KalshiRestClient) -> list[dict]:
        """Todas las market_positions, paginando por cursor. Tope de páginas + detección de
        cursor repetido (deuda auditoría: un cursor pegado colgaba el tick para siempre)."""
        out: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _page in range(self._MAX_PAGES):
            resp = await client.get_positions(limit=100, cursor=cursor)
            if not isinstance(resp, dict):
                break
            out.extend(resp.get("market_positions", resp.get("positions", [])) or [])
            cursor = resp.get("cursor") or None
            if not cursor:
                return out
            if cursor in seen_cursors:
                logger.warning("motor3.poller.cursor_repetido — corto la paginación")
                return out
            seen_cursors.add(cursor)
        logger.warning(
            f"motor3.poller.max_pages alcanzado ({self._MAX_PAGES}) — resultado truncado"
        )
        return out

    def _persist(
        self,
        open_now: dict[str, tuple[str, int, int | None]],
        close_times: dict[str, datetime | None],
    ) -> None:
        """UPSERT de las posiciones abiertas + PURGA de las que ya no lo están. Best-effort.

        GUARD DE PURGA TOTAL: un resultado vacío con filas existentes NO purga en el primer
        sync (puede ser shape-drift/respuesta parcial "200 OK"); se purga recién con el
        SEGUNDO vacío consecutivo. Un cierre real de todas las posiciones se refleja con
        un tick (60s) de retraso — aceptable; perder los peaks del trailing por un blip, no.
        """
        now = _naive_utc_now()
        try:
            with get_session() as s:
                existing = {p.ticker: p for p in s.exec(select(PortfolioPosition))}
                if not open_now and existing:
                    self._empty_syncs += 1
                    if self._empty_syncs < 2:
                        logger.warning(
                            f"motor3.poller.purge_deferred filas={len(existing)} "
                            "(resultado vacío — se confirma en el próximo sync antes de purgar)"
                        )
                        return
                else:
                    self._empty_syncs = 0
                # Upsert
                for ticker, (side, count, exposure) in open_now.items():
                    row = existing.get(ticker)
                    if row is None:
                        row = PortfolioPosition(ticker=ticker, side=side, count=count)
                    if row.side != side:
                        # Flip de side entre polls (yes→no sin pasar por 0 en un snapshot):
                        # es una posición de identidad NUEVA — el peak del episodio
                        # anterior no puede armar el trailing de esta (fix auditoría
                        # 2026-07-01: un peak=80 del lado viejo contra un entry nuevo de
                        # 25 vendía la posición nueva al primer tick).
                        row.peak_bid_cents = None
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
