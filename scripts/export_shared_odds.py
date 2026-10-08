"""Exportación unidireccional de cuatro snapshots públicos, sin HTTP ni credenciales.

Artefacto local para análisis de datos; no instala timers/servicios ni arma motores.

Ejecutar manualmente o desde un timer SEPARADO, con usuario que pueda leer research
y grupo de escritura/lectura del destino preparado previamente por el instalador:

  python -m scripts.export_shared_odds \\
    --source-dir /var/lib/botkalshi-research/m5 \\
    --destination-dir /var/lib/botkalshi-odds-share --max-age-seconds 600

El instalador crea el destino 0750 y configura usuario/grupo; este script NO hace
chown, no modifica usuarios ni servicios. Publica archivos 0640 con replace+fsync.
Sólo serializa fetched_at original, sport/params fijos y campos públicos de los
OddsEvent validados. Fechas normalizadas a ISO UTC conservan el instante original;
no se sustituye ninguna fecha por el momento de exportar. El lector compatible es
OddsSnapshotClient(destination_dir). Fuente inválida: conserva la copia previa con
su fecha original. Salida JSON saneada; exit 2 si algún deporte no pudo publicarse.
Una fuente vacía o sin libros utilizables tampoco reemplaza la copia anterior;
su vencimiento lo sigue comprobando el consumidor con las fechas originales.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import stat
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from src.clients.odds_snapshot import (
    MAX_BYTES,
    PARAMS,
    SPORT_FILES,
    OddsSnapshot,
    OddsSnapshotClient,
    OddsSnapshotError,
)


class ExportError(ValueError):
    """Motivos estáticos; nunca rutas, contenido o excepciones sin sanear."""


def _path(value: Path | str) -> Path:
    try:
        result = Path(value)
    except (TypeError, ValueError):
        raise ExportError("PATH_UNSAFE") from None
    if not result.is_absolute() or ".." in result.parts or "\x00" in str(result):
        raise ExportError("PATH_UNSAFE")
    return result


def _directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _serialize(snapshot: OddsSnapshot, sport: str) -> bytes:
    events = []
    for event in snapshot.events:
        events.append(
            {
                "id": event.id,
                "sport_key": event.sport_key,
                "commence_time": event.commence_time.isoformat(),
                "home_team": event.home_team,
                "away_team": event.away_team,
                "bookmakers": [
                    {
                        "key": book.key,
                        "last_update": book.last_update.isoformat(),
                        "markets": [
                            {
                                "key": market.key,
                                "outcomes": [
                                    {"name": outcome.name, "price": outcome.price, "point": None}
                                    for outcome in market.outcomes
                                ],
                            }
                            for market in book.markets
                        ],
                    }
                    for book in event.bookmakers
                ],
            }
        )
    payload = {
        "fetched_at": snapshot.fetched_at.isoformat(),
        "sport": sport,
        "params": dict(PARAMS),
        "events": events,
    }
    raw = (
        json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode()
    if len(raw) > MAX_BYTES:
        raise ExportError("EXPORT_TOO_LARGE")
    return raw


def _identity(info) -> tuple:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _existing(directory: int, name: str, sport: str) -> tuple[bytes | None, tuple | None]:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        # Un enlace roto es una colisión, no un archivo ausente para sobrescribir.
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            return None, None
        raise ExportError("DESTINATION_COLLISION") from None
    before = os.fstat(descriptor)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or stat.S_IMODE(before.st_mode) != 0o640
        or not 0 < before.st_size <= MAX_BYTES
        or before.st_gid != os.fstat(directory).st_gid
    ):
        os.close(descriptor)
        raise ExportError("DESTINATION_COLLISION")
    with os.fdopen(descriptor, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
        if _identity(before) != _identity(os.fstat(stream.fileno())) or len(raw) != before.st_size:
            raise ExportError("DESTINATION_CHANGED")
    try:
        existing = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise ExportError("DESTINATION_COLLISION") from None
    if (
        not isinstance(existing, dict)
        or set(existing) != {"fetched_at", "sport", "params", "events"}
        or existing.get("sport") != sport
        or existing.get("params") != PARAMS
        or not isinstance(existing.get("fetched_at"), str)
        or not isinstance(existing.get("events"), list)
    ):
        raise ExportError("DESTINATION_COLLISION")
    return raw, _identity(before)


def _publish(directory: int, name: str, sport: str, raw: bytes) -> str:
    previous, identity = _existing(directory, name, sport)
    if previous == raw:
        return "UNCHANGED"
    temporary = ".odds-export-" + uuid.uuid4().hex
    try:
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640, dir_fd=directory
        )
        with os.fdopen(fd, "wb") as stream:
            if os.fstat(stream.fileno()).st_gid != os.fstat(directory).st_gid:
                raise ExportError("DESTINATION_GROUP_NOT_PREPARED")
            os.fchmod(stream.fileno(), 0o640)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            current = _identity(os.stat(name, dir_fd=directory, follow_symlinks=False))
        except FileNotFoundError:
            current = None
        if current != identity:
            raise ExportError("DESTINATION_CHANGED")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)
    return "EXPORTED"


async def export_snapshots(
    source_dir: Path | str,
    destination_dir: Path | str,
    *,
    max_age_seconds: float = 600,
    clock: Callable[[], datetime] | None = None,
) -> dict:
    source, destination = _path(source_dir), _path(destination_dir)
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ExportError("DIRECTORY_COLLISION")
    reader = OddsSnapshotClient(source, max_age_seconds=max_age_seconds, clock=clock)
    source_fd = destination_fd = None
    try:
        source_fd = _directory(source)
        destination_fd = _directory(destination)
        src, dst = os.fstat(source_fd), os.fstat(destination_fd)
        if (src.st_dev, src.st_ino) == (dst.st_dev, dst.st_ino):
            raise ExportError("DIRECTORY_COLLISION")
        if stat.S_IMODE(dst.st_mode) != 0o750:
            raise ExportError("DESTINATION_DIRECTORY_MODE")
        try:
            # Un lock sobre el directorio evita otro archivo compartido y serializa timers/manuales.
            fcntl.flock(destination_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExportError("EXPORT_BUSY") from None
        sports = {}
        async with reader:
            for sport, name in SPORT_FILES.items():
                try:
                    snapshot = await reader.read_snapshot(sport)
                except OddsSnapshotError:
                    sports[sport] = "SOURCE_REJECTED"
                    continue
                if not snapshot.events:
                    sports[sport] = "SOURCE_EMPTY"
                    continue
                try:
                    sports[sport] = _publish(
                        destination_fd, name, sport, _serialize(snapshot, sport)
                    )
                except (ExportError, OSError):
                    sports[sport] = "DESTINATION_REJECTED"
        return {
            "status": "OK"
            if all(s in {"EXPORTED", "UNCHANGED"} for s in sports.values())
            else "PARTIAL",
            "sports": sports,
        }
    except OSError:
        raise ExportError("DIRECTORY_UNAVAILABLE_OR_UNSAFE") from None
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", default="/var/lib/botkalshi-research/m5")
    parser.add_argument("--destination-dir", default="/var/lib/botkalshi-odds-share")
    parser.add_argument("--max-age-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(
            export_snapshots(
                args.source_dir, args.destination_dir, max_age_seconds=args.max_age_seconds
            )
        )
    except (ExportError, OddsSnapshotError) as exc:
        result = {"status": "REJECTED", "reason": str(exc)}
    except Exception:
        result = {"status": "REJECTED", "reason": "EXPORT_UNAVAILABLE"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
