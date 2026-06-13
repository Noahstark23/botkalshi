"""
CaptureWatchdog: monitors capture loop health for OrderbookManagerV2 operation.

If capture_running has been False for more than GRACE_SECONDS after having been
True at some point, pauses the bot and fires a Telegram alert.

Does NOT flip USE_ORDERBOOK_MANAGER_V2 (avoids race conditions).
Only observes BotState.capture_running and BotState.last_capture_running_true_at.
"""

from __future__ import annotations

import asyncio
import time

from loguru import logger

from src.monitoring.health import BotState


class CaptureWatchdog:
    """
    Monitors that the capture loop remains running after V2 activation.

    Start alongside the capture service:
        watchdog = CaptureWatchdog()
        asyncio.create_task(watchdog.run())
    """

    GRACE_SECONDS = 60
    CHECK_INTERVAL = 10

    def __init__(self) -> None:
        self._running = False

    async def run(self) -> None:
        """Watch loop. Runs until cancelled or stop() called."""
        self._running = True
        was_ever_running = False

        while self._running:
            await asyncio.sleep(self.CHECK_INTERVAL)

            if BotState.capture_running:
                was_ever_running = True
                continue

            if not was_ever_running:
                continue  # Still in startup

            if BotState.last_capture_running_true_at == 0.0:
                continue

            elapsed = time.monotonic() - BotState.last_capture_running_true_at
            if elapsed > self.GRACE_SECONDS:
                msg = (
                    f"CaptureWatchdog: capture_running has been False for "
                    f"{elapsed:.0f}s (threshold {self.GRACE_SECONDS}s). "
                    "Pausing bot."
                )
                logger.critical(msg)
                BotState.is_paused = True
                BotState.pause_reason = msg
                BotState.record_error(msg)
                try:
                    from src.monitoring.telegram_alerts import alert_error

                    await alert_error(msg)
                except Exception:
                    logger.exception("Watchdog Telegram alert failed")
                # Reset so we don't spam on every tick
                was_ever_running = False

    async def stop(self) -> None:
        """Stop the watch loop."""
        self._running = False
