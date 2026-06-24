"""
Monitor de memoria (advisory-only).

Lee el uso REAL de memoria del cgroup del contenedor y avisa por Telegram + log
cuando cruza un umbral (% del límite). Su razón de ser: el baseline crece de forma
ORGÁNICA a medida que el universo de mercados crece (más tickers trackeados → más
snapshots → más working-set). Eso no es un leak, pero es crecimiento monotónico que
eventualmente alcanza cualquier techo. El monitor da AVISO TEMPRANO antes del OOM kill,
en vez de enterarse por los reinicios (post-mortem).

NUNCA tradea, ni toca capital, ni reinicia nada — solo observa y alerta.

cgroup v2 (Docker moderno):  /sys/fs/cgroup/memory.current  +  /sys/fs/cgroup/memory.max
cgroup v1 (legacy):          .../memory/memory.usage_in_bytes + .../memory/memory.limit_in_bytes

El alert es EDGE-TRIGGERED con histéresis: avisa al CRUZAR el umbral hacia arriba y se
re-arma sólo cuando baja por debajo de (umbral - margen). Así no spamea cada intervalo.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from loguru import logger

from src.monitoring.telegram_alerts import send_alert
from src.utils.config import get_settings

# Rutas cgroup (v2 primero, v1 como fallback)
_V2_CURRENT = Path("/sys/fs/cgroup/memory.current")
_V2_MAX = Path("/sys/fs/cgroup/memory.max")
_V1_CURRENT = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
_V1_MAX = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")

# Por encima de esto el "límite" del cgroup es en realidad "sin límite" (v1 reporta un
# número astronómico cuando no hay cap). Filtramos para no dividir contra basura.
_UNLIMITED_THRESHOLD_BYTES = 1 << 62

# Margen de histéresis: tras alertar, re-arma sólo si baja a (umbral - este margen).
_REARM_MARGIN_PCT = 5.0


def _read_int(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def read_cgroup_memory() -> tuple[int, int] | None:
    """
    Devuelve (uso_bytes, limite_bytes) del cgroup, o None si no hay límite legible.

    None significa "no puedo medir el % de forma confiable" (cgroup ausente, o sin
    límite configurado) → el monitor lo trata como no-op, NO como 0%.
    """
    for current_p, max_p in ((_V2_CURRENT, _V2_MAX), (_V1_CURRENT, _V1_MAX)):
        if not current_p.exists():
            continue
        current = _read_int(current_p)
        if current is None:
            continue
        # v2 puede traer literalmente "max" (sin límite); _read_int devuelve None ahí.
        limit = _read_int(max_p)
        if limit is None or limit <= 0 or limit >= _UNLIMITED_THRESHOLD_BYTES:
            return None
        return current, limit
    return None


class MemoryMonitor:
    """Loop advisory que vigila el uso de memoria del contenedor."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.threshold_pct = self.settings.MEMORY_MONITOR_THRESHOLD_PCT
        self.interval = self.settings.MEMORY_MONITOR_INTERVAL_SEC
        self._alerted = False  # histéresis: True mientras estemos por encima del umbral

    async def _check_once(self, reader=read_cgroup_memory) -> float | None:
        """
        Una iteración: lee memoria, evalúa umbral, alerta en el flanco de subida.

        Devuelve el % de uso (o None si no medible). `reader` es inyectable para tests.
        """
        sample = reader()
        if sample is None:
            return None
        current, limit = sample
        pct = current / limit * 100.0
        used_mb = current / (1024 * 1024)
        limit_mb = limit / (1024 * 1024)

        if pct >= self.threshold_pct:
            if not self._alerted:
                self._alerted = True
                msg = (
                    f"*Memoria alta*: {pct:.0f}% "
                    f"({used_mb:.0f}MB / {limit_mb:.0f}MB, umbral {self.threshold_pct:.0f}%). "
                    "Crecimiento del baseline acercándose al límite del contenedor — "
                    "revisar antes del OOM kill (subir límite o investigar retención)."
                )
                logger.warning(
                    f"memory_monitor: {pct:.1f}% ({used_mb:.0f}/{limit_mb:.0f}MB) "
                    f">= umbral {self.threshold_pct:.0f}% → alerta"
                )
                await send_alert(msg, urgent=True)
        elif self._alerted and pct < self.threshold_pct - _REARM_MARGIN_PCT:
            # Re-arma sólo tras bajar con margen (evita flapping alrededor del umbral).
            self._alerted = False
            logger.info(f"memory_monitor: {pct:.1f}% bajó del umbral con margen → re-armado")
        return pct

    async def run(self, stop_event: asyncio.Event) -> None:
        """Loop principal. Best-effort: un fallo de lectura NO tira la task."""
        # Log de arranque: deja constancia de si el cgroup es legible en este entorno.
        sample = read_cgroup_memory()
        if sample is None:
            logger.info(
                "memory_monitor: cgroup sin límite legible en este entorno → monitor inactivo"
            )
            return
        _, limit = sample
        logger.info(
            f"memory_monitor: activo (límite {limit / (1024 * 1024):.0f}MB, "
            f"umbral {self.threshold_pct:.0f}%, cada {self.interval}s)"
        )

        while not stop_event.is_set():
            try:
                await self._check_once()
            except Exception as e:
                logger.warning(f"memory_monitor tick: {type(e).__name__}: {e}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval)
                return  # stop solicitado
            except TimeoutError:
                pass
