"""
DiskGuard — lazo CERRADO de presión de disco (incidente 2026-07-10).

El control de disco previo era todo lazo ABIERTO: retención por tiempo (maintenance),
gates estáticos (PERSIST_ORDERBOOK_EVENTS) y un cron en el host. Nadie DENTRO del bot
miraba cuánto disco quedaba: cuando algo inesperado escribió de más (el WAL a ~8MB/s),
el bot siguió escribiendo feliz hasta que el disco tocó 96% y el propio deploy del fix
falló por `no space left on device`.

Este módulo cierra el lazo: mide el disco libre del mount de la DB y expone un estado
(OK / WARN / CRITICAL) que:
  - el loop del runner usa para ALERTAR (Telegram) y disparar una poda inmediata;
  - los escritores de DIAGNÓSTICO consultan via `diagnostics_allowed()` para hacer
    backpressure: en CRITICAL la telemetría se descarta (orderbook_events,
    market_snapshots) — el ESTADO DE TRADING (trades, risk_events, operational_state)
    JAMÁS se gatea: perder telemetría es gratis, perder un trade cuesta plata.

Fail-safe direccional (principio 5 del repo): la MEDICIÓN es un path de lectura → falla
ABIERTA (un hiccup de statvfs no apaga la telemetría; se mantiene el último estado
conocido). El shed de diagnóstico solo se activa con una medición REAL en critical.
"""

from __future__ import annotations

import os
import shutil
from typing import ClassVar

from loguru import logger

from src.utils.config import get_settings

# Estados en orden de severidad (para detectar transiciones en el runner).
OK = "ok"
WARN = "warn"
CRITICAL = "critical"


def _db_mount_dir() -> str:
    """Directorio del archivo de la DB (el mount cuyo espacio importa)."""
    url = get_settings().DATABASE_URL
    path = url.split("sqlite:///", 1)[-1] if "sqlite:///" in url else "/app/data/trades.db"
    return os.path.dirname(path) or "."


class DiskGuard:
    """Estado de presión de disco compartido a nivel de proceso (mismo patrón que BotState).

    `evaluate()` la llama el loop del runner; `diagnostics_allowed()` la consultan los
    escritores de telemetría en su hot path (es un read de atributo de clase: gratis).
    """

    _state: ClassVar[str] = OK
    _free_gb: ClassVar[float | None] = None  # última medición real (para /status y logs)

    @classmethod
    def evaluate(cls) -> str:
        """Mide el disco libre y actualiza el estado. Devuelve el estado resultante.

        Falla ABIERTA: si la medición explota (mount desaparecido, permiso, etc.) se
        LOGUEA y se mantiene el último estado conocido — un error del guard no debe
        apagar la telemetría ni, peor, des-apagarla si estábamos en critical real.
        """
        settings = get_settings()
        try:
            free_gb = shutil.disk_usage(_db_mount_dir()).free / 1e9
        except OSError:
            logger.exception("disk_guard: no pude medir el disco (se mantiene el estado previo)")
            return cls._state

        cls._free_gb = free_gb
        if free_gb < settings.DISK_GUARD_CRITICAL_FREE_GB:
            cls._state = CRITICAL
        elif free_gb < settings.DISK_GUARD_WARN_FREE_GB:
            cls._state = WARN
        else:
            cls._state = OK
        return cls._state

    @classmethod
    def diagnostics_allowed(cls) -> bool:
        """False SOLO en critical: la telemetría se descarta para no comerse el disco que
        el estado de trading necesita. En ok/warn se escribe normal."""
        return cls._state != CRITICAL

    @classmethod
    def snapshot(cls) -> dict[str, object]:
        """Estado para /status y logs."""
        return {
            "state": cls._state,
            "free_gb": round(cls._free_gb, 2) if cls._free_gb is not None else None,
            "diagnostics_allowed": cls.diagnostics_allowed(),
        }

    @classmethod
    def reset(cls) -> None:
        """Solo para tests."""
        cls._state = OK
        cls._free_gb = None
