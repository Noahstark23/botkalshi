"""
Guard de CONFIANZA del book por ticker (forense 2026-08-06) — el fix de plata de M1.

La evidencia: 44 intentos consecutivos con CERO arbitrajes completados. El 53% que
tocó el exchange murió limpio (pata dura FOK-kill, gratis); el 47% llenó la dura y la
"fácil" NO EXISTÍA — FOK sin volumen en 141ms desde la detección, 9¢ de vacío al
deshacer. La prueba pericial del evento 21:02: el edge de 3.19% y un `book_incoherent
cruce=11¢` 0.4s antes eran EL MISMO BOOK. M1 no pierde carreras: le compra a
profundidad fantasma de books recién re-baseados (el empalme pierde también removals
→ book INFLADO sin desincronizar — la mitad silenciosa que ningún detector ve).

El guard: un ticker con incidente PROPIO reciente (desync, incoherencia, desync en
drain, clamp del empalme) no ejecuta hasta llevar MOTOR_1_BOOK_TRUST_SEC sin
incidentes. La detección y el EdgeWindow se graban igual (shadow intacto); el techo
anti-fantasma no cubre este caso porque el cruce fantasma de 2-5¢ cae DENTRO de la
banda plausible.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.strategies.motor_1_arbitrage.engine import Motor1Engine
from src.strategies.motor_1_arbitrage.orderbook import OrderbookDesyncError
from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2


def _snapshot(ticker: str, sid: int = 1, seq: int = 1) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "yes_dollars_fp": [["0.50", "100.00"]],
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


# =====================================================
# El manager registra PERTURBACIONES por ticker (incidente O re-baseo)
# =====================================================
# QA adversarial 2026-08-06: la v1 anclaba solo al incidente y dejaba tres agujeros
# (re-seed sid-wide por hermano sin incidente propio; recovery ≥trust con 0s de
# protección post-re-baseo; dict vacío post-deploy). Ahora la SIEMBRA también arranca
# el embargo: la digestión empieza cuando el book vuelve a servir.


def _manager(seam_grace: float = 0.0) -> OrderbookManagerV2:
    ws = AsyncMock()
    ws.send_command.side_effect = list(range(42, 400))
    return OrderbookManagerV2(ws, seam_grace_sec=seam_grace)


async def test_la_siembra_arranca_el_embargo():
    """QA hallazgo (c): post-deploy el dict de incidentes está vacío pero la siembra de
    boot re-basea todo — el embargo debe arrancar en la SIEMBRA, no solo en incidentes."""
    manager = _manager()
    assert manager.book_incident_age("TICK") is None  # jamás sembrado: sin book

    await manager.handle_message(_snapshot("TICK", seq=1))

    edad = manager.book_incident_age("TICK")
    assert edad is not None and edad < 1.0  # recién sembrado = en digestión


async def test_desync_refresca_el_embargo_de_un_book_maduro():
    manager = _manager()
    await manager.handle_message(_snapshot("TICK", seq=1))
    manager._seeded_at_mono["TICK"] -= 100.0  # book maduro (sembrado hace 100s)
    assert manager.book_incident_age("TICK") > 50.0

    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))

    assert manager.book_incident_age("TICK") < 1.0  # el incidente re-arma el embargo


async def test_clamp_del_empalme_refresca_el_embargo():
    manager = _manager(seam_grace=10.0)
    await manager.handle_message(_snapshot("TICK", seq=1))
    await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))  # clamp, no raise
    assert manager.stats()["seam_clamps_total"] == 1
    manager._seeded_at_mono["TICK"] -= 100.0  # aunque la siembra fuera vieja...

    assert manager.book_incident_age("TICK") < 1.0  # ...el clamp manda (max de marcas)


async def test_recovery_larga_protege_desde_el_rebaseo_no_desde_el_incidente():
    """QA hallazgo (b): con ancla solo-incidente, una recovery ≥trust consumía la
    ventana entera con el book stale y el PRIMER tick servible ya pasaba el guard.
    Ahora el re-baseo re-arma el embargo: la protección corre donde hay riesgo."""
    manager = _manager()
    await manager.handle_message(_snapshot("TICK", seq=1))
    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))
    manager._book_incident_mono["TICK"] -= 100.0  # el incidente fue hace 100s (recovery larga)
    assert 1 in manager._recovering

    rid = manager._pending_req_id_for_sid(1)
    msg = _snapshot("TICK", seq=50)
    msg["id"] = rid
    await manager.handle_message(msg)  # el re-baseo llega recién ahora

    assert manager.book_incident_age("TICK") < 1.0  # embargo fresco POST-re-baseo


async def test_reseed_por_hermano_tambien_embarga():
    """QA hallazgo (a) — LA FÁBRICA DOMINANTE: el desync de un season market dispara
    recovery sid-wide que re-basea al GAME sin incidente propio; el empalme puede
    inflarlo en silencio. El re-baseo del hermano ahora también arranca SU embargo."""
    manager = _manager()
    await manager.handle_message(_snapshot("GAME", seq=1))
    await manager.handle_message(_snapshot("SEASON", seq=2))
    manager._seeded_at_mono["GAME"] -= 100.0  # GAME maduro
    assert manager.book_incident_age("GAME") > 50.0

    with pytest.raises(OrderbookDesyncError):  # desync del HERMANO → recovery sid-wide
        await manager.handle_message(_delta("SEASON", seq=3, delta="-500.00"))
    rid = manager._pending_req_id_for_sid(1)
    for i, t in enumerate(("GAME", "SEASON")):
        msg = _snapshot(t, seq=10 + i)
        msg["id"] = rid
        await manager.handle_message(msg)  # la recovery re-basea a AMBOS

    assert manager.book_incident_age("GAME") < 1.0  # re-sembrado = en digestión
    assert manager._book_incident_mono.get("GAME") is None  # sin incidente propio (correcto)


# =====================================================
# Atribución de FUENTE del embargo (2026-08-08, calibración del guard)
# =====================================================
# 495/495 skips en 20h no distinguen "el guard frena fantasmas" de "las recoveries
# sid-wide re-arman el embargo más rápido de lo que expira". book_trust_info separa
# las hipótesis: la distribución incidente/siembra decide QUÉ palanca calibrar.


async def test_fuente_siembra_para_book_sembrado_sin_incidente():
    manager = _manager()
    await manager.handle_message(_snapshot("TICK", seq=1))

    edad, fuente = manager.book_trust_info("TICK")
    assert fuente == "siembra" and edad < 1.0


async def test_fuente_incidente_cuando_el_desync_es_posterior_a_la_siembra():
    manager = _manager()
    await manager.handle_message(_snapshot("TICK", seq=1))
    manager._seeded_at_mono["TICK"] -= 100.0
    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))

    edad, fuente = manager.book_trust_info("TICK")
    assert fuente == "incidente" and edad < 1.0


async def test_fuente_siembra_cuando_el_rebaseo_es_posterior_al_incidente():
    """El caso dominante en producción: incidente viejo, pero la recovery sid-wide
    acaba de re-basear — el embargo vigente lo causó la SIEMBRA, no el incidente."""
    manager = _manager()
    await manager.handle_message(_snapshot("TICK", seq=1))
    with pytest.raises(OrderbookDesyncError):
        await manager.handle_message(_delta("TICK", seq=2, delta="-500.00"))
    manager._book_incident_mono["TICK"] -= 100.0  # el incidente fue hace 100s
    rid = manager._pending_req_id_for_sid(1)
    msg = _snapshot("TICK", seq=50)
    msg["id"] = rid
    await manager.handle_message(msg)  # la recovery re-basea AHORA

    edad, fuente = manager.book_trust_info("TICK")
    assert fuente == "siembra" and edad < 1.0


async def test_book_trust_info_none_sin_marcas():
    assert _manager().book_trust_info("JAMAS-VISTO") is None


# =====================================================
# El engine corta SOLO la ejecución (el shadow sigue)
# =====================================================


def _engine_con_arb(trust_sec: float, incidente_hace: float | None):
    """Engine LIVE con un arb detectable en 'TICK' y el incidente configurado."""
    manager = MagicMock()
    manager.tracked_tickers = ["TICK"]
    top_yes = MagicMock()
    top_yes.best_bid.price_cents = 60  # no_ask_synth = 40
    top_yes.best_bid.size = 50
    top_no = MagicMock()
    top_no.best_bid.price_cents = 45  # yes_ask_synth = 55 → 55+40=95 < 100: arb
    top_no.best_bid.size = 50
    manager.get_top_of_book.side_effect = lambda t, side: top_yes if side == "yes" else top_no
    manager.book_trust_info.return_value = (
        None if incidente_hace is None else (incidente_hace, "incidente")
    )

    settings = MagicMock()
    settings.TRADING_ENABLED = True
    settings.MIN_EDGE_PCT = 1.0
    settings.MOTOR_1_EXECUTION_EDGE_PCT = 1.0
    settings.MOTOR_1_MAX_EDGE_PCT = 10.0
    settings.MOTOR_1_CONFIRM_TICKS = 1
    settings.MOTOR_1_TICKER_COOLDOWN_SEC = 60.0
    settings.MOTOR_1_BOOK_TRUST_SEC = trust_sec

    with patch("src.strategies.motor_1_arbitrage.engine.get_settings", return_value=settings):
        engine = Motor1Engine(manager, executor=AsyncMock())
    engine.settings = settings
    engine._record_edge_window = MagicMock(return_value=7)  # aisla la DB del test
    return engine


async def test_incidente_reciente_corta_la_ejecucion_pero_no_el_shadow():
    """EL CASO 21:02: incidente 0.4s antes → el edge se DETECTA y graba, pero NO se
    ejecuta — la huérfana de 80¢→71¢ no habría existido."""
    engine = _engine_con_arb(trust_sec=60.0, incidente_hace=0.4)

    await engine._tick()

    assert engine._signals_seen == 1  # la señal se vio
    engine._record_edge_window.assert_called_once()  # y el shadow la grabó
    assert engine._skips_untrusted == 1  # pero la ejecución se cortó
    assert engine._skips_por_fuente == {"incidente": 1}  # con la fuente atribuida
    assert engine._exec_task is None  # sin orden lanzada


async def test_skip_por_siembra_alimenta_su_propio_contador():
    """El desglose del heartbeat: un embargo causado por re-siembra rutinaria cuenta
    en 'siembra', no en 'incidente' — la distribución decide la calibración."""
    engine = _engine_con_arb(trust_sec=60.0, incidente_hace=0.4)
    engine._manager.book_trust_info.return_value = (0.4, "siembra")

    await engine._tick()

    assert engine._skips_untrusted == 1
    assert engine._skips_por_fuente == {"siembra": 1}
    assert engine._exec_task is None


async def test_book_con_historia_limpia_ejecuta():
    """CONTROL: sin incidentes registrados (None) el guard no frena nada."""
    engine = _engine_con_arb(trust_sec=60.0, incidente_hace=None)

    await engine._tick()

    assert engine._skips_untrusted == 0
    assert engine._exec_task is not None  # la ejecución se lanzó
    engine._exec_task.cancel()


async def test_incidente_viejo_ya_no_frena():
    """CONTROL: pasado el período de confianza, el ticker vuelve al ruedo solo."""
    engine = _engine_con_arb(trust_sec=60.0, incidente_hace=61.0)

    await engine._tick()

    assert engine._skips_untrusted == 0
    assert engine._exec_task is not None
    engine._exec_task.cancel()


async def test_trust_cero_desactiva_el_guard():
    """CONTROL de config: MOTOR_1_BOOK_TRUST_SEC=0 = comportamiento pre-fix."""
    engine = _engine_con_arb(trust_sec=0.0, incidente_hace=0.4)

    await engine._tick()

    assert engine._skips_untrusted == 0
    assert engine._exec_task is not None
    engine._exec_task.cancel()
