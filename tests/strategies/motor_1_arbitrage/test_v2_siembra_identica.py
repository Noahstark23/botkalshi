"""
Siembra IDÉNTICA no re-arma el embargo (2026-08-09) — atacar la causa, no el umbral.

La medición del 09-ago (día completo, #219 etiquetando): 733 skips del guard de M1,
68% fuente=siembra con edad media 24.2s, 85/85 edges ejecutables bloqueados, 0 trades.
La fábrica: el desync de un hermano dispara recovery sid-wide cuyo snapshot re-basea
books SANOS con el MISMO contenido que ya servían — y cada re-baseo re-armaba 60s de
embargo por nada. Si el snapshot confirma byte a byte nuestro estado, no hubo
perturbación ni empalme que digerir: el embargo del book sano NO se re-arma, y el
contador identical_seeds_total mide en producción qué fracción del churn era ruido.

Un book STALE o sin inicializar jamás confirma identidad (su contenido es sospechoso
por definición): la cuarentena siempre exige re-baseo real con embargo. La gracia del
empalme tampoco se reabre en siembra idéntica — sin removals perdidos, un underflow
posterior es desync real, no artefacto de costura.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


def _snapshot(ticker: str, sid: int = 1, seq: int = 1, yes: str = "0.50", size: str = "100.00"):
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [[yes, size]],
            "no_dollars_fp": [],
        },
    }


def _delta(ticker: str, seq: int, sid: int = 1, delta: str = "10.00") -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.50",
            "delta_fp": delta,
            "side": "yes",
        },
    }


def _manager(seam_grace: float = 0.0) -> OrderbookManagerV2:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    return OrderbookManagerV2(ws, seam_grace_sec=seam_grace)


async def test_siembra_identica_no_rearma_el_embargo():
    """MECANISMO — el caso de plata: el hermano sano re-baseado sin cambios conserva
    su madurez. Es el 68% de los bloqueos del guard convertido en no-evento."""
    manager = _manager()
    await manager.handle_message(_snapshot("GAME", seq=1))
    await manager.handle_message(_snapshot("SEASON", seq=2))
    manager._seeded_at_mono["GAME"] -= 100.0  # GAME maduro

    with pytest.raises(OrderbookDesyncError):  # desync del HERMANO → recovery sid-wide
        await manager.handle_message(_delta("SEASON", seq=3, delta="-500.00"))
    rid = manager._pending_req_id_for_sid(1)
    msg = _snapshot("GAME", seq=10)  # MISMO contenido que el book vivo
    msg["id"] = rid
    await manager.handle_message(msg)

    assert manager.book_incident_age("GAME") > 50.0  # la madurez SOBREVIVE
    assert manager.stats()["identical_seeds_total"] == 1


async def test_siembra_con_cambio_si_rearma():
    """CONTROL: el mismo flujo con contenido DISTINTO re-arma (hay empalme real)."""
    manager = _manager()
    await manager.handle_message(_snapshot("GAME", seq=1))
    manager._seeded_at_mono["GAME"] -= 100.0

    await manager.handle_message(_snapshot("GAME", seq=2, yes="0.55"))  # cambió el book

    assert manager.book_incident_age("GAME") < 1.0  # en digestión
    assert manager.stats()["identical_seeds_total"] == 0


async def test_book_stale_jamas_confirma_identidad():
    """CONTROL fail-safe: tras un desync PROPIO el book queda stale — aunque el
    snapshot de recovery traiga el mismo contenido, la cuarentena exige embargo
    (el contenido de un book divergido es sospechoso por definición)."""
    manager = _manager()
    await manager.handle_message(_snapshot("TICK", seq=1))
    manager._seeded_at_mono["TICK"] -= 100.0
    manager._book_incident_mono["TICK"] = manager._seeded_at_mono["TICK"]  # incidente viejo
    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))
    manager._book_incident_mono["TICK"] -= 100.0  # envejecer el incidente del desync

    rid = manager._pending_req_id_for_sid(1)
    msg = _snapshot("TICK", seq=10)  # contenido igual al pre-desync
    msg["id"] = rid
    await manager.handle_message(msg)

    assert manager.book_incident_age("TICK") < 1.0  # re-armó igual: estaba stale
    assert manager.stats()["identical_seeds_total"] == 0


async def test_siembra_identica_no_reabre_la_gracia_del_empalme():
    """CONTROL: sin removals perdidos no hay costura — un underflow inmediatamente
    después de una siembra idéntica es desync REAL y debe desincronizar, no clampear."""
    manager = _manager(seam_grace=10.0)
    await manager.handle_message(_snapshot("TICK", seq=1))
    manager._seeded_at_mono["TICK"] -= 100.0  # fuera de la gracia original

    await manager.handle_message(_snapshot("TICK", seq=2))  # idéntica: gracia NO reabre

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", seq=3, delta="-500.00"))
    assert manager.stats()["seam_clamps_total"] == 0  # no fue clamp: fue desync real


async def test_primera_siembra_siempre_embarga():
    """CONTROL: un ticker sin book previo no puede confirmar nada — la primera
    siembra arranca el embargo como siempre (QA hallazgo (c) intacto)."""
    manager = _manager()
    await manager.handle_message(_snapshot("NUEVO", seq=1))

    assert manager.book_incident_age("NUEVO") < 1.0
    assert manager.stats()["identical_seeds_total"] == 0
