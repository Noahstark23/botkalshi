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
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level: <8} | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
        backtrace=True,
        diagnose=False,  # no exponer valores en errores (puede leak datos)
    )

    # File handler - retención local en volumen Docker
    logs_dir = Path("/app/logs")
    logs_dir.mkdir(exist_ok=True, parents=True)

    logger.add(
        logs_dir / "bot_{time:YYYY-MM-DD}.log",
        level="DEBUG",
        rotation="00:00",  # rotate diario a medianoche
        retention="30 days",
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
