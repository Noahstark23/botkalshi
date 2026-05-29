"""
Tests de integracion del mecanismo de reconexion del WS contra un socket real.

A diferencia de los tests del watchdog (que mockean ws.force_reconnect), aca
levantamos un servidor websockets local y ejercitamos el run() REAL para probar
que:
    - force_reconnect() cierra el socket y run() RECONECTA (sigue _running=True).
    - stop() SI termina el loop (no reconecta).

Esto valida la diferencia critica entre ambos: si el watchdog usara stop() en vez
de force_reconnect(), mataria el feed en vez de reconectarlo.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
import websockets

from src.clients.kalshi_ws import KalshiWebSocket


async def _server_handler(websocket) -> None:
    """Servidor trivial: emite un mensaje cada 20ms y mantiene la conexion abierta."""
    try:
        while True:
            await websocket.send('{"type": "ticker", "msg": {}}')
            await asyncio.sleep(0.02)
    except Exception:
        # cliente cerro la conexion (force_reconnect / stop) -> salir limpio
        return


async def _wait_until(cond, timeout: float = 8.0, interval: float = 0.02) -> bool:  # noqa: ASYNC109
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(interval)
    return False


def _make_client(port: int) -> KalshiWebSocket:
    with patch("src.clients.kalshi_ws.get_settings"), patch(
        "src.clients.kalshi_ws.KalshiSigner"
    ):
        ws = KalshiWebSocket()
    ws.url = f"ws://localhost:{port}"
    ws.signer = MagicMock()
    ws.signer.sign.return_value = {}
    return ws


@pytest.mark.asyncio
async def test_force_reconnect_actually_reconnects():
    """force_reconnect() cierra el socket pero run() vuelve a conectar (feed sigue vivo)."""
    async with websockets.serve(_server_handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = _make_client(port)

        task = asyncio.create_task(ws.run())
        try:
            assert await _wait_until(
                lambda: ws.is_connected and ws.last_message_at is not None
            ), "no conecto la primera vez"
            first_conn = ws._ws

            # Forzar reconexion: cierra el socket SIN tocar _running.
            await ws.force_reconnect()

            # Debe reconectar (nuevo objeto de conexion) y seguir corriendo.
            assert await _wait_until(
                lambda: ws.is_connected and ws._ws is not first_conn
            ), "no reconecto tras force_reconnect"
            assert ws._running is True  # clave: NO se detuvo el loop

            # Y debe seguir recibiendo mensajes tras reconectar.
            ts_after_reconnect = ws.last_message_at
            assert await _wait_until(
                lambda: ws.last_message_at != ts_after_reconnect
            ), "no llegan mensajes tras reconexion"
        finally:
            await ws.stop()
            try:
                await asyncio.wait_for(task, timeout=3)
            except TimeoutError:
                task.cancel()


@pytest.mark.asyncio
async def test_stop_terminates_loop_no_reconnect():
    """stop() pone _running=False y run() termina: NO reconecta (contraste con force_reconnect)."""
    async with websockets.serve(_server_handler, "localhost", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = _make_client(port)

        task = asyncio.create_task(ws.run())
        try:
            assert await _wait_until(
                lambda: ws.is_connected and ws.last_message_at is not None
            ), "no conecto"

            await ws.stop()

            # El loop debe terminar solo (run() sale por _running=False).
            await asyncio.wait_for(task, timeout=3)
            assert task.done()
            assert ws._running is False
        finally:
            if not task.done():
                task.cancel()
