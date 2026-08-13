"""
Apuesta 1 (plan de reestructuración 2026-08-12) — contabilidad MAKER + markout.

El "fee fantasma": quoter.py cobraba fee de TAKER (0.07) a las DOS patas de un
round-trip que por construcción es maker (post_only GTC). VERIFICADO 2026-08-13
contra el PDF oficial (July 7, 2026): el maker de deportes NO paga $0 (la creencia
del dossier murió contra la evidencia primaria antes de encender nada) — paga ¼ del
taker (0.0175, multiplicador 1). MOTOR_MM_FEES_AS_MAKER=true usa la fee REAL de
maker; el default False conserva el modelo taker legacy. Con la fee real: round-trip
maker ≈ 1¢/contrato a size=10 → spreads ≥2¢ rentables (el taker exigía ≥4¢).

El markout (mid posterior vs precio del fill, signado) es la otra mitad del gate:
mtm positivo con markout negativo sistemático = los fills son tóxicos (selección
adversa) y el spread capture no existe a esta escala.
"""

from __future__ import annotations

import pytest
from sqlmodel import select

from src.math.fees import kalshi_fee_cents, kalshi_maker_fee_cents
from src.storage.models import MMShadowFill, get_session
from src.strategies.fair_value_book import FairValueBook
from src.strategies.motor_5_mm.engine import Motor5Engine
from src.strategies.motor_5_mm.inventory import InventoryBook
from src.strategies.motor_5_mm.quoter import compute_quote
from src.strategies.motor_5_mm.shadow_fill import ShadowFill

# =====================================================
# Fee model — quoter
# =====================================================


def test_maker_cotiza_el_spread_de_2c_que_el_modelo_taker_rechazaba():
    """EL HÁBITAT RECUPERADO: spread capturado 2¢ en zona media — el taker (4¢ de fee
    round-trip a count=1) lo rechazaba; con la fee REAL de maker al size (10¢ de fee
    por orden vs 20¢ capturados) se cotiza. La matemática del PDF, no la del deseo."""
    kwargs = {
        "half_spread_cents": 1,
        "size_contracts": 10,
        "inventory_contracts": 0,
        "max_inventory_contracts": 50,
        "best_yes_bid": 40,
        "best_yes_ask": 60,
    }
    taker, motivo = compute_quote("T", 0.50, **kwargs)
    assert taker is None and motivo == "unprofitable"  # CONTROL: el modelo viejo intacto

    maker, motivo = compute_quote("T", 0.50, fees_as_maker=True, **kwargs)
    assert maker is not None and motivo is None
    assert maker.ask_cents - maker.bid_cents == 2  # el spread de 2¢ ahora se cotiza


def test_maker_sigue_rechazando_spread_cero():
    """CONTROL: el modelo maker no habilita spread ≤0 — captured debe superar la fee."""
    quote, motivo = compute_quote(
        "T",
        0.50,
        half_spread_cents=0,
        size_contracts=10,
        inventory_contracts=0,
        max_inventory_contracts=50,
        best_yes_bid=49,
        best_yes_ask=51,
        fees_as_maker=True,
    )
    # half_spread=0 → bid=ceil/floor del centro → bid=50, ask=50 → degenerado.
    assert quote is None


# =====================================================
# Fee model — inventario shadow
# =====================================================


def _fill(side: str = "buy", count: int = 10, price: int = 47) -> ShadowFill:
    return ShadowFill(ticker="T", side=side, price_cents=price, count=count, rule="test")


def test_inventario_maker_paga_la_fee_real_de_maker():
    maker = InventoryBook(fees_as_maker=True)
    inv = maker.apply_fill(_fill())
    assert inv.fees_cents == kalshi_maker_fee_cents(10, 47)  # ¼ del taker, NO cero
    assert 0 < inv.fees_cents < kalshi_fee_cents(10, 47)

    taker = InventoryBook()  # default = modelo histórico
    inv_t = taker.apply_fill(_fill())
    assert inv_t.fees_cents == kalshi_fee_cents(10, 47)  # CONTROL: sin cambio


def test_mtm_maker_vs_taker_difiere_exactamente_en_el_delta_de_fee():
    maker, taker = InventoryBook(fees_as_maker=True), InventoryBook()
    for book in (maker, taker):
        book.apply_fill(_fill("buy", 10, 47))
        book.apply_fill(_fill("sell", 10, 53))
    delta_fee = (kalshi_fee_cents(10, 47) - kalshi_maker_fee_cents(10, 47)) + (
        kalshi_fee_cents(10, 53) - kalshi_maker_fee_cents(10, 53)
    )
    assert maker.total_mtm_cents({}) - taker.total_mtm_cents({}) == delta_fee
    # Round-trip 6¢ × 10 = 60¢ bruto; fee maker 5+5=10¢ → 50¢ neto (taker dejaba 24¢).
    assert maker.total_mtm_cents({}) == 50


# =====================================================
# Markout — la métrica de selección adversa
# =====================================================


class _ReadOnlyClient:
    def __init__(self):
        self.books: dict[str, dict] = {}

    async def get_orderbook(self, ticker: str) -> dict:
        book = self.books.get(ticker)
        if book is None:
            raise RuntimeError("book no disponible")
        return {"orderbook": book}


def _book(yes_bid: int, yes_ask: int) -> dict:
    return {"yes": [[yes_bid, 100]], "no": [[100 - yes_ask, 100]]}


def _engine(client) -> Motor5Engine:
    eng = Motor5Engine(
        max_tickers=2,
        half_spread_cents=3,
        quote_size_contracts=10,
        max_inventory_contracts=50,
        fair_ttl_sec=600.0,
    )
    eng._client = client
    return eng


@pytest.mark.asyncio
async def test_markout_negativo_cuando_el_mercado_sigue_en_contra():
    """SELECCIÓN ADVERSA: compramos a 47 (cruce) y el mid siguió cayendo a 40 —
    markout1 = mark − precio = −7. El signo que delata al fill tóxico."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()  # quote resting bid 47 / ask 53

    client.books["T-A"] = _book(40, 46)  # ask cruza el bid → fill buy @47
    await eng._tick()
    assert len(eng._markouts_pendientes) == 1

    eng._markouts_pendientes[0]["t_mono"] -= 60.0  # el fill fue "hace 60s"
    client.books["T-A"] = _book(38, 42)  # mid ahora 40: siguió en contra
    await eng._tick()

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout1_cents == -7.0  # 40 − 47
    assert row.markout1_age_sec >= 60.0
    assert row.markout2_cents is None  # T+5min todavía no


@pytest.mark.asyncio
async def test_markout2_completa_y_saca_de_la_cola():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    client.books["T-A"] = _book(54, 60)  # bid cruza el ask → fill sell @53
    await eng._tick()

    eng._markouts_pendientes[0]["t_mono"] -= 301.0
    client.books["T-A"] = _book(48, 52)  # mid 50: vendimos a 53 y bajó → sell markout +3
    await eng._tick()

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout1_cents == 3.0  # sell: precio − mark = 53 − 50
    assert row.markout2_cents == 3.0
    assert eng._markouts_pendientes == []  # completo → fuera de la cola


@pytest.mark.asyncio
async def test_markout_sin_mark_espera_y_luego_suelta():
    """FAIL-SAFE: ticker que salió del universo (sin mark) no bloquea la cola — espera
    hasta 10min y se suelta con markouts NULL, jamás inventa un mark."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    client.books["T-A"] = _book(40, 46)
    await eng._tick()  # fill

    eng._markouts_pendientes[0]["t_mono"] -= 700.0  # >10min sin medir
    eng._last_marks.clear()  # el ticker ya no tiene mark
    eng._medir_markouts()

    assert eng._markouts_pendientes == []  # soltado
    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout1_cents is None and row.markout2_cents is None
