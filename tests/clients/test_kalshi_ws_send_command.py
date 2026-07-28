"""
Wire format de send_command (incidente 2026-07-23 — books_initialized=0).

El spec vigente de Kalshi exige `action` DENTRO de `params`:
    {"id": N, "cmd": "update_subscription",
     "params": {"sids": [1], "action": "get_snapshot", "market_tickers": [...]}}

Nuestro send_command lo mandaba en el TOP-LEVEL del mensaje ({"action": "get_snapshot",
"params": {...}}). El server de mayo lo aceptaba (probe 2026-05-19); el vigente lo busca
en params, no lo encuentra, y rechaza el comando entero con code 15 "Action required" —
todos los get_snapshot de recovery rechazados en ms, recovery muerta por timeout, books
en 0. Este test PINEA el wire format (lección #155: pinear el call site/wire, no solo el
componente): si alguien vuelve a moverlo, esto se pone rojo antes que producción.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

from src.clients.kalshi_ws import KalshiWebSocket, WsState


def _connected_ws() -> tuple[KalshiWebSocket, AsyncMock]:
    with (
        patch("src.clients.kalshi_ws.KalshiSigner"),
        patch("src.clients.kalshi_ws.get_settings") as gs,
    ):
        s = MagicMock()
        s.KALSHI_PRIVATE_KEY_PATH = "/fake/key.pem"
        s.KALSHI_API_KEY_ID = "test-key"
        s.ws_url = "wss://fake/ws"
        gs.return_value = s
        client = KalshiWebSocket()
    fake = AsyncMock()
    fake.state = WsState.OPEN
    client._ws = fake
    return client, fake


async def test_action_goes_inside_params_not_top_level():
    client, fake = _connected_ws()
    req_id = await client.send_command(
        "update_subscription",
        action="get_snapshot",
        params={"market_tickers": ["KXMLBGAME-X"], "sids": [1]},
    )

    sent = json.loads(fake.send.call_args[0][0])
    assert sent["id"] == req_id
    assert sent["cmd"] == "update_subscription"
    assert "action" not in sent  # el top-level era el bug: code 15 "Action required"
    assert sent["params"]["action"] == "get_snapshot"
    assert sent["params"]["market_tickers"] == ["KXMLBGAME-X"]
    assert sent["params"]["sids"] == [1]


async def test_action_without_params_still_lands_in_params():
    client, fake = _connected_ws()
    await client.send_command("update_subscription", action="get_snapshot")
    sent = json.loads(fake.send.call_args[0][0])
    assert "action" not in sent
    assert sent["params"] == {"action": "get_snapshot"}


async def test_command_without_action_unchanged():
    """CONTROL: comandos sin action (el resto del protocolo) no cambian de shape."""
    client, fake = _connected_ws()
    await client.send_command("subscribe", params={"channels": ["ticker"]})
    sent = json.loads(fake.send.call_args[0][0])
    assert "action" not in sent
    assert sent["params"] == {"channels": ["ticker"]}


async def test_caller_params_dict_not_mutated():
    """El merge no debe mutar el dict del caller (el manager reusa/loguea sus params)."""
    client, _ = _connected_ws()
    original = {"market_tickers": ["A"], "sids": [1]}
    await client.send_command("update_subscription", action="get_snapshot", params=original)
    assert "action" not in original
