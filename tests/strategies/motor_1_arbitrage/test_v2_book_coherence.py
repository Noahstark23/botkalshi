"""
Invariante de coherencia del book binario (incidente 2026-07-28).

La cuarentena por desync es REACTIVA: solo dispara cuando un delta dejaría qty<0. Un book
puede divergir y servir precios FANTASMA durante minutos sin tocar nunca esa condición —
medido en producción: 30 de 130 edges binarios sobre el techo anti-fantasma, máximo 86.5pp.

Matemática (idéntica a `engine._detect`): el ask sintético de un lado es 100 − bid del otro,
así que el cruce bruto es `yes_bid + no_bid − 100`. Un arb REAL vive en 1-5¢; 86¢ significa
los dos bids altos a la vez = imposible en un binario. Por encima del umbral → book stale +
recovery, protegiendo a TODOS los lectores (M1/M5/M8/M9), no solo a la orden que se iba a
mandar. El control CRÍTICO de este archivo: el arb legítimo NO debe caer en la cuarentena.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.strategies.motor_1_arbitrage.orderbook_manager_v2 import OrderbookManagerV2

TICKER = "KXMLBGAME-26JUL28HOUWSH-HOU"


def _snapshot(yes_bid: int, no_bid: int, *, sid: int = 1, seq: int = 1) -> dict:
    """Snapshot con una punta por lado (el book solo mantiene BIDS; los asks son sintéticos)."""
    return {
        "type": "orderbook_snapshot",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": TICKER,
            "yes_dollars_fp": [[f"0.{yes_bid:02d}00", "500.00"]],
            "no_dollars_fp": [[f"0.{no_bid:02d}00", "500.00"]],
        },
    }


def _delta(side: str, price: int, delta: int, *, sid: int = 1, seq: int = 2) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": sid,
        "seq": seq,
        "msg": {
            "market_ticker": TICKER,
            "price_dollars": f"0.{price:02d}00",
            "delta_fp": f"{delta}.00",
            "side": side,
        },
    }


@pytest.fixture
def ws() -> AsyncMock:
    w = AsyncMock()
    w.send_command.side_effect = list(range(42, 200))
    return w


async def _seed(manager: OrderbookManagerV2, yes_bid: int, no_bid: int) -> None:
    await manager.handle_message(_snapshot(yes_bid, no_bid))


# =====================================================
# MECANISMO: el fantasma cae en cuarentena
# =====================================================


async def test_absurd_cross_quarantines_book(ws):
    """El caso de producción: yes_bid=95 + no_bid=91 → cruce 86¢ (el edge de 86.5pp).
    El book queda stale, deja de servir precios y se pide recovery."""
    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=10)
    await _seed(manager, 50, 40)  # book sano (cruce −10)
    assert manager.get_top_of_book(TICKER, "yes") is not None
    ws.send_command.reset_mock()

    # Deltas que dejan los DOS bids altos: 95 y 91 → cruce 186−100 = 86¢
    await manager.handle_message(_delta("yes", 95, 500, seq=2))
    await manager.handle_message(_delta("no", 91, 500, seq=3))

    assert manager.get_top_of_book(TICKER, "yes") is None  # stale: no sirve fantasmas
    assert TICKER in manager._incoherent_tickers
    assert manager._incoherent_quarantines == 1
    assert ws.send_command.await_count >= 1  # pidió recovery


async def test_quarantine_is_one_shot_until_snapshot_rebases(ws):
    """One-shot por ticker: deltas incoherentes seguidos NO re-cuentan ni tormentean la
    recovery. Un snapshot fresco re-arma la invariante (si vuelve a divergir, vuelve a contar)."""
    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=10)
    await _seed(manager, 50, 40)
    await manager.handle_message(_delta("yes", 95, 500, seq=2))
    await manager.handle_message(_delta("no", 91, 500, seq=3))
    assert manager._incoherent_quarantines == 1
    sends = ws.send_command.await_count

    await manager.handle_message(_delta("no", 92, 500, seq=4))  # sigue incoherente
    assert manager._incoherent_quarantines == 1  # NO re-cuenta
    assert ws.send_command.await_count == sends  # NO re-pide recovery

    # El snapshot de recovery re-basea → la invariante se re-arma para este ticker.
    await manager.handle_message(_snapshot(50, 40, seq=100))
    assert TICKER not in manager._incoherent_tickers
    assert manager.get_top_of_book(TICKER, "yes") is not None  # vuelve a servir


# =====================================================
# CONTROL CRÍTICO: el arb legítimo NO se cuarentena
# =====================================================


@pytest.mark.parametrize("yes_bid,no_bid,cross", [(51, 51, 2), (52, 53, 5), (55, 55, 10)])
async def test_plausible_cross_is_never_quarantined(ws, yes_bid, no_bid, cross):
    """LA razón de ser de M1: un book auto-cruzado de 2-10¢ es el arb que buscamos.
    La invariante NO puede tocarlo — si lo hiciera, mataría el motor entero."""
    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=10)
    await _seed(manager, 40, 40)
    await manager.handle_message(_delta("yes", yes_bid, 500, seq=2))
    await manager.handle_message(_delta("no", no_bid, 500, seq=3))

    assert manager._incoherent_quarantines == 0, f"cruce de {cross}¢ NO debe cuarentenarse"
    assert manager.get_top_of_book(TICKER, "yes") is not None  # sigue sirviendo el arb


async def test_normal_book_untouched(ws):
    """CONTROL: un book normal (sin cruce) ni se acerca a la invariante."""
    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=10)
    await _seed(manager, 45, 45)  # cruce −10
    await manager.handle_message(_delta("yes", 46, 100, seq=2))
    assert manager._incoherent_quarantines == 0
    assert manager.get_top_of_book(TICKER, "yes") is not None


# =====================================================
# FAIL-SAFE y observabilidad
# =====================================================


async def test_missing_side_does_not_evaluate(ws):
    """Sin las DOS puntas no hay invariante que evaluar (no sobre-filtrar): un book con
    un solo lado se deja pasar — lo cubren los guards de _detect/_mid_of."""
    manager = OrderbookManagerV2(ws)
    await manager.handle_message(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "seq": 1,
            "msg": {"market_ticker": TICKER, "yes_dollars_fp": [["0.9500", "500.00"]]},
        }
    )
    await manager.handle_message(_delta("yes", 96, 100, seq=2))
    assert manager._incoherent_quarantines == 0


async def test_threshold_is_configurable(ws):
    """El umbral es tunable: con tolerancia 3¢, un cruce de 5¢ ya es sospechoso."""
    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=3)
    await _seed(manager, 40, 40)
    await manager.handle_message(_delta("yes", 52, 500, seq=2))
    await manager.handle_message(_delta("no", 53, 500, seq=3))  # cruce 5¢ > 3¢
    assert manager._incoherent_quarantines == 1


async def test_stats_expose_incoherence_counters(ws):
    """La corrupción del feed se MIDE: stats expone books en cuarentena ahora y el total
    acumulado (si sube sostenido, el problema está upstream del manager)."""
    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=10)
    await _seed(manager, 50, 40)
    s0 = manager.stats()
    assert s0["incoherent_books_now"] == 0 and s0["incoherent_quarantines_total"] == 0

    await manager.handle_message(_delta("yes", 95, 500, seq=2))
    await manager.handle_message(_delta("no", 91, 500, seq=3))
    s1 = manager.stats()
    assert s1["incoherent_books_now"] == 1
    assert s1["incoherent_quarantines_total"] == 1


# =====================================================
# Invariante de ORDEN y persistencia (2026-07-29)
# =====================================================


async def test_mark_stale_ocurre_antes_del_primer_await(ws):
    """PINEA LA GARANTÍA DE SEGURIDAD: el book debe quedar stale ANTES del primer punto de
    suspensión (_start_recovery). Si alguien agrega un `await` real antes del mark_stale,
    el event loop podría dar control a M1/M5 con precios FANTASMA todavía servibles y
    mandar una orden sobre liquidez inexistente. Este test se pone rojo si se reordena."""
    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=10)
    await _seed(manager, 50, 40)
    stale_al_entrar: list[bool] = []

    async def _spy_start_recovery(sid: int) -> None:
        state = manager._books[TICKER]
        stale_al_entrar.append(state.is_stale)

    manager._start_recovery = _spy_start_recovery  # type: ignore[method-assign]

    await manager.handle_message(_delta("yes", 95, 500, seq=2))
    await manager.handle_message(_delta("no", 91, 500, seq=3))

    assert stale_al_entrar == [True], "el book DEBE estar stale antes del primer await"


async def test_incoherencia_se_persiste_en_risk_events(ws, tmp_path, monkeypatch):
    """El contador en memoria se reinicia en cada arranque del container (6 redeploys en una
    noche = métrica del mes subestimada sin que se note). El evento se persiste en
    risk_events para poder contarlo POR DÍA, inmune a reinicios."""
    from sqlmodel import select

    import src.storage.models as models

    engine = models.create_engine(f"sqlite:///{tmp_path / 'risk.db'}")
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)

    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=10)
    await _seed(manager, 50, 40)
    # Un solo delta ya rompe la invariante: yes_bid 50→95 con no_bid=40 da cruce 35¢.
    await manager.handle_message(_delta("yes", 95, 500, seq=2))

    with models.get_session() as db:
        rows = list(db.exec(select(models.RiskEvent)))
    assert len(rows) == 1
    assert rows[0].event_type == "book_incoherent"
    assert rows[0].severity == "warning"
    assert TICKER in rows[0].message
    assert "cruce=35¢" in rows[0].message


async def test_persistencia_falla_sin_romper_la_cuarentena(ws, monkeypatch):
    """FAIL-SAFE: si la DB falla, la cuarentena — que es lo que protege la plata — igual
    ocurre. El orden importa: mark_stale ya pasó cuando se intenta persistir."""
    import src.storage.models as models

    def _boom(*a, **kw):
        raise RuntimeError("DB caída")

    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=10)
    await _seed(manager, 50, 40)
    monkeypatch.setattr(models, "get_session", _boom)

    await manager.handle_message(_delta("yes", 95, 500, seq=2))

    assert manager.get_top_of_book(TICKER, "yes") is None  # cuarentena OK pese al fallo de DB
    assert manager._incoherent_quarantines == 1


async def test_persistencia_no_bloquea_el_event_loop(ws, monkeypatch):
    """La escritura del RiskEvent va en THREAD: si SQLite está tomado (backup, checkpoint,
    query pesada), el commit NO puede congelar el event loop del WS — misma lección que
    /stats/daily. Se verifica que la persistencia corre FUERA del hilo del loop."""
    import threading

    import src.storage.models as models

    hilo_de_la_persistencia: list[int] = []
    hilo_del_loop = threading.get_ident()

    def _spy_session(*a, **kw):
        hilo_de_la_persistencia.append(threading.get_ident())
        raise RuntimeError("DB tomada")  # además: el fallo no rompe la cuarentena

    monkeypatch.setattr(models, "get_session", _spy_session)
    manager = OrderbookManagerV2(ws, max_plausible_cross_cents=10)
    await _seed(manager, 50, 40)

    await manager.handle_message(_delta("yes", 95, 500, seq=2))

    assert hilo_de_la_persistencia, "la persistencia debe haberse intentado"
    assert hilo_de_la_persistencia[0] != hilo_del_loop, "debe correr en un worker, no en el loop"
    assert manager.get_top_of_book(TICKER, "yes") is None  # cuarentena intacta
