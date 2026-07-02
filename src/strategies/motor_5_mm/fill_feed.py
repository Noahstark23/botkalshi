"""
MMFillFeed (F2) — fast-path del canal WS `fill` (el canal existía sin handler, gap F0).

Se cuelga del KalshiWebSocket compartido (el de data_capture) y ENCOLA los fills cuyo
client_order_id lleva el prefijo del Motor 5 (m5mm-). NO muta estado: el engine drena
la cola al inicio de su tick (regla de oro Lección 9 — el estado del MM lo muta SOLO el
loop del engine, secuencialmente; un handler WS mutándolo en paralelo sería una carrera).

El reconciler sigue siendo la VERDAD (slow-path completo contra get_orders); este feed
solo baja la latencia entre "llenó" y "el engine lo sabe" de ~60s a ~0.
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from src.strategies.motor_5_mm.executor import COID_PREFIX, _parse_count


class MMFillFeed:
    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def attach(self, ws: Any) -> None:
        """Registra el handler y suscribe el canal `fill` en el WS compartido."""
        ws.on("fill", self._on_fill)
        ws.queue_subscription(["fill"])
        logger.info("motor5.fill_feed adjuntado al WS (canal fill suscrito)")

    async def _on_fill(self, msg: dict) -> None:
        body = msg.get("msg", msg) if isinstance(msg, dict) else {}
        coid = str(body.get("client_order_id") or "")
        if not coid.startswith(COID_PREFIX):
            return  # fill de otro motor/manual — no es nuestro
        fill = {
            "client_order_id": coid,
            "ticker": body.get("market_ticker") or body.get("ticker"),
            "count": _parse_count(body.get("count")) or 0,
            "side": body.get("side"),  # bid/ask del eje YES (V2)
            "price_cents": _parse_count(body.get("yes_price")) or _parse_count(body.get("price")),
        }
        try:
            self._queue.put_nowait(fill)
        except asyncio.QueueFull:
            # Se pierde el fast-path, NO la verdad: el reconciler lo resuelve igual.
            self.dropped += 1
            logger.warning(f"motor5.fill_feed cola llena (dropped={self.dropped})")

    def drain(self) -> list[dict]:
        """Todos los fills encolados desde el último tick (no bloquea)."""
        out: list[dict] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return out
