"""
Recovery debe terminar con un orderbook_snapshot del ticker en recovery, lleve o no
el `id` del comando (incidente 2026-05-28).

Durante la recovery el manager manda update_subscription/get_snapshot y rutea cada mensaje
del sid en recovery a _pending_deltas, esperando un orderbook_snapshot cuyo `id` top-level
coincida con el request pendiente (dispatch → _handle_recovery_snapshot). Eso depende de UNA
invariante no garantizada: que Kalshi SIEMPRE eco-devuelve el id del comando en el snapshot.
Si entrega el snapshot SIN id (reconnect, cambio de server), el dispatch lo tira a
_pending_deltas como cualquier mensaje, _handle_recovery_snapshot nunca dispara, el sid queda
en _recovering, los deltas se acumulan y el book se queda stale hasta que el watchdog aborta.

Fix especificado: un orderbook_snapshot cuyo market_ticker mapee a un sid en _recovering debe
tratarse como fin de recovery y rutearse a _handle_recovery_snapshot por ticker/sid, SIN
importar el id.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import (
    OrderbookManagerV2,
    SidGapError,
)

TICKER = "KXBTC-25DEC31-100000"
FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "ws" / "get_snapshot_response.json"

# Seq plan: snapshot inicial en 10 → próximo delta esperado 11. Mandamos 13 (gap), bufferamos
# 14 durante recovery, el snapshot de recovery re-basea en 15.
SEQ_INIT = 10
SEQ_GAP = 13
SEQ_DURING = 14
SEQ_RECOVERY = 15


# --------------------------------------------------------------------------- #
# Payloads (schema correcto; id/seq/sid se sobrescriben por test)             #
# --------------------------------------------------------------------------- #


def load_canonical_snapshot() -> dict:
    snap = json.loads(FIXTURE.read_text())
    snap.setdefault("msg", {})["market_ticker"] = TICKER
    return snap


def with_cmd_id(snapshot: dict, cmd_id) -> dict:
    s = copy.deepcopy(snapshot)
    s["id"] = cmd_id
    return s


def without_cmd_id(snapshot: dict) -> dict:
    s = copy.deepcopy(snapshot)
    s.pop("id", None)
    return s


def _set(snapshot: dict, *, sid, seq) -> dict:
    s = copy.deepcopy(snapshot)
    s["sid"] = sid
    s["seq"] = seq
    s.setdefault("msg", {})["market_ticker"] = TICKER
    return s


def make_delta(*, sid, seq, price, delta, side) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": TICKER,
            "price_dollars": price,
            "delta_fp": delta,
            "side": side,
        },
    }


# --------------------------------------------------------------------------- #
# WIRE: adapters a la API real del manager (copiados de los tests AC1/AC2)     #
# --------------------------------------------------------------------------- #


def make_manager():
    """(manager, sid) — manager con un mock_ws cuyo send_command devuelve el req_id (42),
    igual setup que las fixtures `manager`/`mock_ws` de test_orderbook_manager_v2."""
    ws = AsyncMock()
    ws.send_command.return_value = 42
    return OrderbookManagerV2(ws), 1


def feed(manager, raw_msg: dict) -> None:
    """Entrega un mensaje WS al manager (mismo entry point del dispatch). Traga SidGapError:
    el gap LANZA la excepción DESPUÉS de iniciar la recovery (igual que AC1), y los tests de
    recovery siguen operando sobre el estado post-gap."""
    with contextlib.suppress(SidGapError):
        asyncio.run(manager.handle_message(raw_msg))


def last_sent_command(manager) -> dict:
    """Último comando enviado, leído del mock_ws (manager._ws). Normaliza a
    {'cmd','id','params':{'action',...}} — `action` va dentro de params para el assert."""
    sc = manager._ws.send_command
    assert sc.called, "no se envió ningún comando WS"
    args, kwargs = sc.call_args
    params = dict(kwargs.get("params", {}))
    if "action" in kwargs:
        params["action"] = kwargs["action"]
    return {"cmd": args[0] if args else kwargs.get("cmd"), "id": sc.return_value, "params": params}


def pending_for(manager, sid):
    pd = getattr(manager, "_pending_deltas", None)
    if pd is None:
        return None
    return pd.get(sid, pd.get(TICKER)) if isinstance(pd, dict) else pd


def is_empty(buf) -> bool:
    return buf is None or len(buf) == 0


# --------------------------------------------------------------------------- #


@pytest.fixture
def manager_and_sid():
    return make_manager()


@pytest.fixture
def canonical_snapshot():
    return load_canonical_snapshot()


def drive_into_recovery(manager, sid, canonical_snapshot):
    """Establece un book, abre un gap de seq → el sid entra en _recovering y se emite el
    get_snapshot. Devuelve el id del comando usado."""
    # 1. Snapshot inicial (server-pushed, sin id) establece book + baseline de seq.
    feed(manager, _set(without_cmd_id(canonical_snapshot), sid=sid, seq=SEQ_INIT))
    assert sid not in manager._recovering, "un book fresco no debería estar en recovery"

    # 2. Delta con gap de seq (esperado 11, llega 13) → dispara recovery.
    feed(manager, make_delta(sid=sid, seq=SEQ_GAP, price="0.960", delta="-5.00", side="yes"))
    assert sid in manager._recovering, "el gap de seq debería haber disparado recovery"
    cmd = last_sent_command(manager)
    assert cmd.get("cmd") == "update_subscription"
    assert cmd.get("params", {}).get("action") == "get_snapshot"

    # 3. Un delta que llega durante la recovery debe bufferarse, no aplicarse.
    feed(manager, make_delta(sid=sid, seq=SEQ_DURING, price="0.970", delta="+7.00", side="yes"))
    return cmd["id"]


def _mut_matching(snapshot, cmd_id):
    return with_cmd_id(snapshot, cmd_id)


def _mut_missing(snapshot, cmd_id):
    return without_cmd_id(snapshot)


def _mut_nonmatching(snapshot, cmd_id):
    bad = cmd_id + 10_000 if isinstance(cmd_id, int) else f"{cmd_id}-stale"
    return with_cmd_id(snapshot, bad)


@pytest.mark.parametrize(
    "snapshot_mutator",
    [
        pytest.param(_mut_matching, id="matching_id"),
        pytest.param(_mut_missing, id="missing_id"),
        pytest.param(_mut_nonmatching, id="nonmatching_id"),
    ],
)
def test_recovery_terminates_on_orderbook_snapshot(
    manager_and_sid, canonical_snapshot, snapshot_mutator
):
    """Un snapshot del ticker en recovery debe terminar la recovery y drenar el buffer —
    lleve o no un id de comando que coincida."""
    manager, sid = manager_and_sid
    cmd_id = drive_into_recovery(manager, sid, canonical_snapshot)

    recovery_snapshot = _set(
        snapshot_mutator(canonical_snapshot, cmd_id), sid=sid, seq=SEQ_RECOVERY
    )
    feed(manager, recovery_snapshot)

    assert sid not in manager._recovering, "el snapshot de recovery debe limpiar _recovering"
    assert is_empty(pending_for(manager, sid)), "_pending_deltas debe drenarse en la recovery"
