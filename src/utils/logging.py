"""
Configuración de logging.

En Coolify, los logs se ven mejor en formato simple con timestamp.
Loguru maneja rotación, retención, y output simultáneo a stdout + archivo.
"""

import sys
from pathlib import Path

from loguru import logger

from src.utils.config import get_settings


def setup_logging() -> None:
    """
    Configura logger global.
    Llamar UNA vez al arranque del bot.
    """
    settings = get_settings()
    logger.remove()  # quitar default handler

    # Console handler - lo que Coolify captura como container logs
    logger.add(
        sys.stderr,
        level=settings.LOG_LEVEL,
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"
        ),
        backtrace=True,
        diagnose=False,  # no exponer valores en errores (puede leak datos)
    )

    # File handler - retención local en volumen Docker
    logs_dir = Path("/app/logs")
    logs_dir.mkdir(exist_ok=True, parents=True)

    # Incidente 2026-07-25: este sink en DEBUG con rotación SOLO diaria escribió 8.5GB en
    # un día (dumps de payload por snapshot en tormentas de recovery) y llenó el disco que
    # el .OLD ya venía comiendo. "Nada sin tope" aplica a los logs: nivel configurable
    # (default INFO; subir a DEBUG por env SOLO mientras se diagnostica) + rotación por
    # TAMAÑO (un día verboso rota varias veces y la retención poda igual).
    logger.add(
        logs_dir / "bot_{time:YYYY-MM-DD}.log",
        level=settings.LOG_FILE_LEVEL,
        rotation="500 MB",
        retention="14 days",
        compression="gz",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}",
        backtrace=True,
        diagnose=False,
    )

    # File handler crítico separado - para auditoría de eventos importantes
    logger.add(
        logs_dir / "critical_{time:YYYY-MM-DD}.log",
        level="WARNING",
        rotation="00:00",
        retention="90 days",
        compression="gz",
    )

    logger.info(
        "Logging inicializado",
        extra={
            "level": settings.LOG_LEVEL,
            "env": settings.KALSHI_ENV,
            "trading_enabled": settings.TRADING_ENABLED,
        },
    )
