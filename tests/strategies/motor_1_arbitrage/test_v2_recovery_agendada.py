"""
Recovery AGENDADA en vez de descartada (P0 2026-08-03).

Línea base medida por el agente web en ventana larga (13 min, 2 Hz, post-#208):
duty cycle 20.03% — ventanas VIVAS de media 8.2s contra ventanas CIEGAS de media
34.6s (máx 77.1s), con 33 supresiones y 24 semillas. El mecanismo: la supresión
anti-espiral marcaba los books stale (correcto para un gap: mejor ciego que fantasma)
pero DESCARTABA la recovery — y como el gap ya se consumió, no quedaba nadie que
volviera a pedirla. El único rescate era el barrido de siembra cada 30s, de ahí las
ventanas ciegas de ~30s (y las de 48-77s cuando también la semilla se suprimía).

Ahora la supresión AGENDA el re-bootstrap para `last + interval`: la ceguera queda
acotada al intervalo anti-espiral (~5s), sin tocar su semántica, sin timers (dispara
el propio flujo de mensajes a ~190 msg/s) y sin cambiar el ruteo de deltas.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


def _snapshot(ticker: str, sid: int = 1, seq: int = 1, req_id: int | None = None) -> dict:
    msg: dict = {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.50", "100.00"]],
            "no_dollars_fp": [],
        },
    }
    if req_id is not None:
        msg["id"] = req_id
    return msg


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


@pytest.fixture
def mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    return ws


async def _manager_con_gap_suprimido(mock_ws) -> OrderbookManagerV2:
    """Manager con un GAP recién suprimido: books stale y recovery agendada."""
    # seam_grace_sec=0: estos tests ejercitan el path de DESYNC REAL (cuarentena +
    # recovery) — la gracia del empalme (2026-08-05) clampearía el underflow y taparía
    # la maquinaria que se está probando. El clamp tiene su propia suite:
    # test_v2_empalme_siembra.py.
    manager = OrderbookManagerV2(mock_ws, seam_grace_sec=0.0)
    await manager.handle_message(_snapshot("A", seq=1))
    await manager.handle_message(_snapshot("B", seq=2))
    manager._last_recovery_start_mono[1] = time.monotonic()  # dentro del intervalo
    await manager.handle_message(_delta("A", seq=99))  # gap suprimido (no raise)
    assert manager._recoveries_suppressed == 1
    assert manager._books["A"].is_stale and manager._books["B"].is_stale
    return manager


async def test_supresion_agenda_la_recovery(mock_ws):
    """MECANISMO: el gap suprimido deja el re-bootstrap AGENDADO para last+interval
    (antes lo descartaba y nadie volvía a pedirlo)."""
    manager = await _manager_con_gap_suprimido(mock_ws)

    due = manager._deferred_recovery_due.get(1)
    assert due is not None
    esperado = manager._last_recovery_start_mono[1] + manager._recovery_min_interval_sec
    assert due == pytest.approx(esperado)
    assert manager.stats()["deferred_recoveries_scheduled_total"] == 1
    assert manager.stats()["deferred_recoveries_fired_total"] == 0


async def test_recovery_agendada_dispara_al_vencer(mock_ws):
    """Vencido el intervalo, el PRÓXIMO mensaje del sid arranca el re-bootstrap — la
    ceguera queda acotada al intervalo, no a los 30s del barrido de siembra."""
    manager = await _manager_con_gap_suprimido(mock_ws)
    sends = mock_ws.send_command.await_count
    manager._deferred_recovery_due[1] = time.monotonic() - 0.01  # ya venció
    manager._last_recovery_start_mono[1] = time.monotonic() - 99.0  # fuera del intervalo

    await manager.handle_message(_delta("B", seq=100))

    assert 1 in manager._recovering
    assert mock_ws.send_command.await_count == sends + 1
    assert manager.stats()["deferred_recoveries_fired_total"] == 1
    assert 1 not in manager._deferred_recovery_due  # consumida

    # Y el ciclo cierra: el snapshot re-basea los books (fin de la ceguera).
    rid = manager._pending_req_id_for_sid(1)
    await manager.handle_message(_snapshot("A", seq=200, req_id=rid))
    await manager.handle_message(_snapshot("B", seq=201, req_id=rid))
    assert not manager._books["A"].is_stale
    assert manager.get_top_of_book("A", "yes") is not None


async def test_no_dispara_antes_de_vencer(mock_ws):
    """CONTROL: el anti-espiral sigue vigente — antes del vencimiento no re-bootstrapea
    (era la protección que cortó la espiral de 38.365 recoveries/9h)."""
    manager = await _manager_con_gap_suprimido(mock_ws)
    sends = mock_ws.send_command.await_count

    await manager.handle_message(_delta("B", seq=100))  # dentro del intervalo

    assert 1 not in manager._recovering
    assert mock_ws.send_command.await_count == sends
    assert manager.stats()["deferred_recoveries_fired_total"] == 0
    assert 1 in manager._deferred_recovery_due  # sigue agendada


async def test_varias_supresiones_agendan_una_sola_vez(mock_ws):
    """Ráfaga de supresiones = UNA recovery agendada (la más temprana), no una cola:
    el anti-espiral no puede convertirse en un generador de re-bootstraps."""
    manager = await _manager_con_gap_suprimido(mock_ws)
    due0 = manager._deferred_recovery_due[1]

    for seq in (200, 300, 400):
        await manager.handle_message(_delta("A", seq=seq))

    assert manager._recoveries_suppressed == 4
    assert manager._deferred_recovery_due[1] == due0  # sin adelantar ni acumular
    assert manager.stats()["deferred_recoveries_scheduled_total"] == 1


async def test_arranque_real_cancela_lo_agendado(mock_ws):
    """Una recovery que SÍ arranca (por gap, desync o siembra) cubre lo agendado — no
    queda un re-bootstrap huérfano disparando después de que el sid ya se re-baseó."""
    manager = await _manager_con_gap_suprimido(mock_ws)
    assert 1 in manager._deferred_recovery_due

    manager._last_recovery_start_mono[1] = time.monotonic() - 99.0  # fuera del intervalo
    assert await manager.seed_blind_sids() == 1

    assert 1 not in manager._deferred_recovery_due
    assert 1 in manager._recovering


async def test_no_dispara_con_recovery_en_curso(mock_ws):
    """CONTROL: con el sid EN recovery no se pisa nada (el mensaje se bufferea como
    siempre); lo agendado espera a que la recovery viva termine."""
    manager = await _manager_con_gap_suprimido(mock_ws)
    manager._deferred_recovery_due[1] = time.monotonic() - 0.01  # vencida
    manager._recovering.add(1)
    manager._pending_deltas[1] = []
    sends = mock_ws.send_command.await_count

    await manager.handle_message(_delta("B", seq=100))

    assert mock_ws.send_command.await_count == sends  # no arrancó otra
    assert manager._pending_deltas[1]  # el mensaje se bufferó


async def test_mensaje_con_gap_no_dispara_la_agendada(mock_ws):
    """REGRESIÓN (revisión adversarial del propio fix): si el mensaje que vence la
    agendada TRAE UN GAP, disparar acá lo bufferearía y el path de gap nunca correría —
    sin mark_stale masivo (books sirviendo precios con mensajes PERDIDOS), sin conteo y
    sin SidGapError. El gap debe manejarlo su propio path, que con el intervalo vencido
    arranca la misma recovery."""
    # seam_grace_sec=0: estos tests ejercitan el path de DESYNC REAL (cuarentena +
    # recovery) — la gracia del empalme (2026-08-05) clampearía el underflow y taparía
    # la maquinaria que se está probando. El clamp tiene su propia suite:
    # test_v2_empalme_siembra.py.
    manager = OrderbookManagerV2(mock_ws, seam_grace_sec=0.0)
    await manager.handle_message(_snapshot("A", seq=1))
    await manager.handle_message(_snapshot("B", seq=2))
    # Agendada nacida de un trigger NO-gap (desync): los books sanos siguen vivos (#208).
    manager._last_recovery_start_mono[1] = time.monotonic()
    with pytest.raises(Exception):  # noqa: B017 — OrderbookDesyncError
        await manager.handle_message(_delta("A", seq=3, delta="-500.00"))
    assert 1 in manager._deferred_recovery_due
    assert not manager._books["B"].is_stale  # sano, por diseño de #208
    manager._deferred_recovery_due[1] = time.monotonic() - 0.01  # vencida
    manager._last_recovery_start_mono[1] = time.monotonic() - 99.0

    from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import SidGapError

    with pytest.raises(SidGapError):  # el path de gap SÍ corre
        await manager.handle_message(_delta("B", seq=9999))

    assert manager._books["A"].is_stale and manager._books["B"].is_stale  # mass-stale
    assert manager.get_top_of_book("B", "yes") is None  # no sirve precios
    assert manager.stats()["gaps_last_60s"] == 1  # el gap SE CONTÓ
    assert 1 in manager._recovering  # y la recovery arrancó igual
    assert 1 not in manager._deferred_recovery_due  # agendada consumida


async def test_agendada_de_gap_contabiliza_el_gap_al_disparar(mock_ws):
    """REGRESIÓN: cuando la agendada nace de un gap SUPRIMIDO y la dispara un mensaje en
    secuencia, el gap real quedaría sin registrar — `gaps_last_60s`→0 apagaría
    sid_gap_warning/critical justo en el régimen degradado (mismo patrón que las 9.5h de
    /health verde). Se contabiliza al disparar: un registro por ventana, no por mensaje."""
    manager = await _manager_con_gap_suprimido(mock_ws)
    assert 1 in manager._deferred_from_gap
    gaps_antes = manager.stats()["gaps_last_60s"]
    manager._deferred_recovery_due[1] = time.monotonic() - 0.01
    manager._last_recovery_start_mono[1] = time.monotonic() - 99.0

    await manager.handle_message(_delta("B", seq=100))  # EN secuencia (100 = 99+1)

    assert manager.stats()["deferred_recoveries_fired_total"] == 1
    assert manager.stats()["gaps_last_60s"] == gaps_antes + 1
    assert 1 not in manager._deferred_from_gap  # consumido, no se cuenta dos veces


async def test_agendada_de_desync_no_inventa_un_gap(mock_ws):
    """CONTROL del anterior: una agendada nacida de un desync/siembra NO contabiliza
    gaps — inflar el contador dispararía alertas de feed roto sin feed roto."""
    # seam_grace_sec=0: estos tests ejercitan el path de DESYNC REAL (cuarentena +
    # recovery) — la gracia del empalme (2026-08-05) clampearía el underflow y taparía
    # la maquinaria que se está probando. El clamp tiene su propia suite:
    # test_v2_empalme_siembra.py.
    manager = OrderbookManagerV2(mock_ws, seam_grace_sec=0.0)
    await manager.handle_message(_snapshot("A", seq=1))
    manager._last_recovery_start_mono[1] = time.monotonic()
    with pytest.raises(Exception):  # noqa: B017 — desync, trigger NO-gap
        await manager.handle_message(_delta("A", seq=2, delta="-500.00"))
    assert 1 in manager._deferred_recovery_due
    assert 1 not in manager._deferred_from_gap
    manager._deferred_recovery_due[1] = time.monotonic() - 0.01
    manager._last_recovery_start_mono[1] = time.monotonic() - 99.0

    # EN secuencia: desde 2026-08-04 el desync CONSUME su seq (quedó en 2), así que el
    # siguiente esperado es 3.
    await manager.handle_message(_delta("A", seq=3))

    assert manager.stats()["deferred_recoveries_fired_total"] == 1
    assert manager.stats()["gaps_last_60s"] == 0  # no hubo gap: no se inventa


async def test_diag_de_supresion_registra_el_arranque_que_cerro_el_gate(mock_ws):
    """P0b: el diag debe decir si el arranque que cerró el gate fue INTERNO — es la
    pregunta abierta del forense (por qué un gap a 8s se suprimía con intervalo 5s)."""
    # seam_grace_sec=0: estos tests ejercitan el path de DESYNC REAL (cuarentena +
    # recovery) — la gracia del empalme (2026-08-05) clampearía el underflow y taparía
    # la maquinaria que se está probando. El clamp tiene su propia suite:
    # test_v2_empalme_siembra.py.
    manager = OrderbookManagerV2(mock_ws, seam_grace_sec=0.0)
    await manager.handle_message(_snapshot("A", seq=1))
    with pytest.raises(Exception):  # noqa: B017 — SidGapError: arranque real por gap
        await manager.handle_message(_delta("A", seq=99))
    assert manager._last_recovery_internal[1] is False

    manager._cleanup_recovery(1)
    await manager._start_recovery(1, internal=True)
    assert manager._last_recovery_internal[1] is True
