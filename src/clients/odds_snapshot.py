"""Odds tipadas desde el exportador research; jamás HTTP, claves ni fallback.

El directorio es el que contiene odds-latest.json (MLB) y odds-<sport>-latest.json.
El formato existente no lleva schema_version: fetched_at, sport, params, events.
El archivo y cada libro deben conservar sus fechas originales. La frescura local
no acredita por sí sola procedencia del proveedor ni autoriza ejecución.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from src.clients.odds_api import Bookmaker, Market, OddsEvent, Outcome

SPORT_FILES = {
    "baseball_mlb": "odds-latest.json",
    "americanfootball_nfl": "odds-americanfootball_nfl-latest.json",
    "americanfootball_ncaaf": "odds-americanfootball_ncaaf-latest.json",
    "basketball_nba": "odds-basketball_nba-latest.json",
    "icehockey_nhl": "odds-icehockey_nhl-latest.json",
}
PARAMS = {"regions": "us", "markets": "h2h", "oddsFormat": "decimal", "dateFormat": "iso"}
MAX_BYTES = 4_000_000
MAX_EVENTS = 256
MAX_BOOKS = 64


class OddsSnapshotError(ValueError):
    """Motivo estable y saneado; nunca incluye contenido, rutas ni excepciones crudas."""


@dataclass(frozen=True, slots=True)
class OddsSnapshot:
    """Fecha de captura y eventos validados desde la MISMA lectura del archivo."""

    fetched_at: datetime
    events: tuple[OddsEvent, ...]


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise OddsSnapshotError("SNAPSHOT_TIMESTAMP_INVALID")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError
        return moment.astimezone(UTC)
    except (ValueError, OverflowError):
        raise OddsSnapshotError("SNAPSHOT_TIMESTAMP_INVALID") from None


def _text(value: object, *, maximum: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
    ):
        raise OddsSnapshotError("SNAPSHOT_SHAPE_INVALID")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise OddsSnapshotError("SNAPSHOT_DUPLICATE_KEY")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise OddsSnapshotError("SNAPSHOT_NUMBER_INVALID")


class OddsSnapshotClient:
    """Interfaz get_odds/context manager de OddsAPIClient, con lectura local acotada.

    max_age_seconds aplica al archivo y a los libros (máximo 660 s). Libros sin
    fecha, futuros o vencidos se descartan; un evento sin libros no se devuelve.
    Los demás errores de forma/identidad invalidan el archivo entero. No se exige
    un mínimo de casas aquí: el consenso M2 mantiene su propio guard min_books.
    """

    def __init__(
        self,
        snapshot_dir: Path | str,
        *,
        max_age_seconds: float = 600,
        clock: Callable[[], datetime] | None = None,
        max_bytes: int = MAX_BYTES,
        max_events: int = MAX_EVENTS,
    ) -> None:
        if (
            isinstance(max_age_seconds, bool)
            or not isinstance(max_age_seconds, (int, float))
            or not math.isfinite(max_age_seconds)
            or not 0 < max_age_seconds <= 660
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_BYTES
            or type(max_events) is not int
            or not 1 <= max_events <= MAX_EVENTS
            or (clock is not None and not callable(clock))
        ):
            raise OddsSnapshotError("SNAPSHOT_CONFIG_INVALID")
        try:
            path = Path(snapshot_dir)
        except (TypeError, ValueError):
            raise OddsSnapshotError("SNAPSHOT_PATH_UNSAFE") from None
        if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
            raise OddsSnapshotError("SNAPSHOT_PATH_UNSAFE")
        self.snapshot_dir = path
        self.max_age_seconds = float(max_age_seconds)
        self.max_bytes = max_bytes
        self.max_events = max_events
        self._clock = clock or (lambda: datetime.now(UTC))

    async def __aenter__(self) -> OddsSnapshotClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        # Cada lectura cierra sus descriptores; no hay transporte ni sesión externa.
        return None

    async def get_odds(
        self,
        sport_key: str,
        *,
        regions: str = "us",
        markets: str = "h2h",
        odds_format: str = "decimal",
    ) -> list[OddsEvent]:
        snapshot = await self.read_snapshot(
            sport_key, regions=regions, markets=markets, odds_format=odds_format
        )
        return list(snapshot.events)

    async def read_snapshot(
        self,
        sport_key: str,
        *,
        regions: str = "us",
        markets: str = "h2h",
        odds_format: str = "decimal",
    ) -> OddsSnapshot:
        """Metadato original junto a modelos; evita releer otra versión al exportar."""
        if (
            not isinstance(sport_key, str)
            or sport_key not in SPORT_FILES
            or regions != "us"
            or markets != "h2h"
            or odds_format != "decimal"
        ):
            raise OddsSnapshotError("SNAPSHOT_REQUEST_UNSUPPORTED")
        return await asyncio.to_thread(self._load, sport_key)

    def _read(self, filename: str) -> bytes:
        """openat por componente: no seguir symlinks ni abrir FIFOs/dispositivos.

        Los reemplazos atómicos del productor pueden cambiar el nombre mientras
        leemos: el descriptor conserva una única versión completa del archivo.
        """
        directory = None
        descriptor = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(self.snapshot_dir.anchor, flags)
            for part in self.snapshot_dir.parts[1:]:
                child = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = child
            if os.fstat(directory).st_mode & stat.S_IWOTH:
                raise OddsSnapshotError("SNAPSHOT_PATH_UNSAFE")
            descriptor = os.open(
                filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_mode & stat.S_IWOTH
            ):
                raise OddsSnapshotError("SNAPSHOT_PATH_UNSAFE")
            if not 0 < before.st_size <= self.max_bytes:
                raise OddsSnapshotError("SNAPSHOT_SIZE_INVALID")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                raw = stream.read(self.max_bytes + 1)
                after = os.fstat(stream.fileno())
            if len(raw) > self.max_bytes:
                raise OddsSnapshotError("SNAPSHOT_SIZE_INVALID")
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or len(raw) != before.st_size:
                raise OddsSnapshotError("SNAPSHOT_CHANGED_DURING_READ")
            return raw
        except OSError:
            raise OddsSnapshotError("SNAPSHOT_UNAVAILABLE_OR_UNSAFE") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory is not None:
                os.close(directory)

    def _load(self, sport: str) -> OddsSnapshot:
        raw = self._read(SPORT_FILES[sport])
        try:
            document = json.loads(
                raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
        except (ValueError, UnicodeError, RecursionError):
            raise OddsSnapshotError("SNAPSHOT_JSON_INVALID") from None
        if not isinstance(document, dict) or document.get("sport") != sport:
            raise OddsSnapshotError("SNAPSHOT_SPORT_INVALID")
        if document.get("params") != PARAMS:
            raise OddsSnapshotError("SNAPSHOT_PARAMS_INVALID")
        try:
            now = self._clock()
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError
            now = now.astimezone(UTC)
        except Exception:
            raise OddsSnapshotError("SNAPSHOT_CLOCK_INVALID") from None
        fetched = _timestamp(document.get("fetched_at"))
        if not 0 <= (now - fetched).total_seconds() <= self.max_age_seconds:
            raise OddsSnapshotError("SNAPSHOT_STALE_OR_FUTURE")
        events = document.get("events")
        if not isinstance(events, list) or len(events) > self.max_events:
            raise OddsSnapshotError("SNAPSHOT_EVENT_LIMIT_OR_SHAPE")
        result = []
        seen = set()
        for event in events:
            parsed = self._event(event, sport, now, fetched)
            if parsed.id in seen:
                raise OddsSnapshotError("SNAPSHOT_DUPLICATE_EVENT")
            seen.add(parsed.id)
            if parsed.bookmakers:
                result.append(parsed)
        return OddsSnapshot(fetched_at=fetched, events=tuple(result))

    def _event(self, event: object, sport: str, now: datetime, fetched: datetime) -> OddsEvent:
        if not isinstance(event, dict) or event.get("sport_key") != sport:
            raise OddsSnapshotError("SNAPSHOT_EVENT_SPORT_INVALID")
        eid = _text(event.get("id"))
        home, away = _text(event.get("home_team")), _text(event.get("away_team"))
        if home == away:
            raise OddsSnapshotError("SNAPSHOT_TEAMS_INVALID")
        commence = _timestamp(event.get("commence_time"))
        raw_books = event.get("bookmakers")
        if not isinstance(raw_books, list) or len(raw_books) > MAX_BOOKS:
            raise OddsSnapshotError("SNAPSHOT_BOOK_LIMIT_OR_SHAPE")
        books, seen = [], set()
        for book in raw_books:
            if not isinstance(book, dict):
                raise OddsSnapshotError("SNAPSHOT_BOOK_SHAPE_INVALID")
            key = _text(book.get("key"))
            if key in seen:
                raise OddsSnapshotError("SNAPSHOT_DUPLICATE_BOOK")
            seen.add(key)
            try:
                update = _timestamp(book.get("last_update"))
            except OddsSnapshotError:
                continue
            if update > fetched or not 0 <= (now - update).total_seconds() <= self.max_age_seconds:
                continue
            markets = book.get("markets")
            if (
                not isinstance(markets, list)
                or len(markets) != 1
                or not isinstance(markets[0], dict)
                or markets[0].get("key") != "h2h"
            ):
                raise OddsSnapshotError("SNAPSHOT_MARKET_INVALID")
            rows = markets[0].get("outcomes")
            if not isinstance(rows, list) or not 2 <= len(rows) <= 3:
                raise OddsSnapshotError("SNAPSHOT_OUTCOME_INVALID")
            outcomes, names = [], set()
            for row in rows:
                if not isinstance(row, dict) or row.get("point") is not None:
                    raise OddsSnapshotError("SNAPSHOT_OUTCOME_INVALID")
                name = _text(row.get("name"))
                price = row.get("price")
                if (
                    type(price) not in (int, float)
                    or not 1 < price <= 10000
                    or not math.isfinite(price)
                    or name in names
                ):
                    raise OddsSnapshotError("SNAPSHOT_OUTCOME_INVALID")
                outcomes.append(Outcome(name=name, price=float(price)))
                names.add(name)
            if names not in ({home, away}, {home, away, "Draw"}):
                raise OddsSnapshotError("SNAPSHOT_OUTCOME_TEAMS_INVALID")
            books.append(
                Bookmaker(
                    key=key, title="", markets=(Market("h2h", tuple(outcomes)),), last_update=update
                )
            )
        return OddsEvent(eid, sport, commence, home, away, tuple(books))
