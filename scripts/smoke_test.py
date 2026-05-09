"""
Smoke test - verificación rápida de que el bot puede arrancar.

Uso (local o dentro del container):
    python -m scripts.smoke_test

Verifica:
    1. Settings se cargan correctamente
    2. Private key existe y es válida
    3. DB se inicializa
    4. Conexión REST con Kalshi funciona
    5. Balance se obtiene correctamente

Si todo pasa, el bot está listo para arrancar en modo producción.
"""
from __future__ import annotations

import asyncio
import sys

from loguru import logger

from src.clients.kalshi_rest import KalshiRestClient
from src.storage.models import init_db
from src.utils.config import get_settings
from src.utils.logging import setup_logging


async def run_smoke_test() -> int:
    """Returns 0 if all OK, 1 if any check failed."""
    setup_logging()

    logger.info("=" * 60)
    logger.info("SMOKE TEST - Kalshi Bot")
    logger.info("=" * 60)

    # === Test 1: Settings ===
    try:
        settings = get_settings()
        logger.success(f"✓ Settings cargados (env={settings.KALSHI_ENV})")
        logger.info(f"  Capital: ${settings.ACTIVE_CAPITAL_USD}")
        logger.info(f"  Trading: {settings.TRADING_ENABLED}")
        logger.info(f"  Private key: {settings.KALSHI_PRIVATE_KEY_PATH}")
    except Exception:
        logger.exception("✗ Settings inválidos")
        return 1

    # === Test 2: Database ===
    try:
        init_db()
        logger.success("✓ DB inicializada")
    except Exception:
        logger.exception("✗ DB falló")
        return 1

    # === Test 3: Kalshi API connection ===
    try:
        async with KalshiRestClient() as client:
            balance_resp = await client.get_balance()
            balance_dollars = balance_resp.get("balance", 0) / 100
            logger.success(f"✓ Kalshi conectado - Balance: ${balance_dollars:.2f}")

            # Listar algunos eventos para verificar que la API responde data
            events_resp = await client.list_events(limit=5)
            events = events_resp.get("events", [])
            logger.success(f"✓ {len(events)} eventos sample disponibles")

            for e in events[:3]:
                logger.info(f"  - {e.get('event_ticker')}: {e.get('title', '')[:60]}")

            # Listar markets
            markets_resp = await client.list_markets(limit=3)
            markets = markets_resp.get("markets", [])
            logger.success(f"✓ {len(markets)} markets sample disponibles")
            for m in markets[:3]:
                logger.info(
                    f"  - {m.get('ticker')}: "
                    f"YES {m.get('yes_bid')}/{m.get('yes_ask')}"
                )

    except Exception:
        logger.exception("✗ Kalshi API falló")
        return 1

    # === All good ===
    logger.info("=" * 60)
    logger.success("✅ TODOS LOS CHECKS PASARON - bot listo para arrancar")
    logger.info("=" * 60)

    if not settings.is_production:
        logger.info("")
        logger.info("Estás en DEMO. Para arrancar el bot completo:")
        logger.info("  python -m src.runner")
    else:
        logger.warning("")
        logger.warning("⚠️  Estás en PRODUCTION. Capital REAL será usado.")
        logger.warning("    Verifica TRADING_ENABLED antes de arrancar runner.")

    return 0


def main() -> None:
    exit_code = asyncio.run(run_smoke_test())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
