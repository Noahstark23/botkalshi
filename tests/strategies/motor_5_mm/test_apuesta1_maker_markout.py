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

from datetime import timedelta

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

    eng._markouts_pendientes[0]["creado"] -= timedelta(seconds=60.0)  # el fill fue "hace 60s"
    client.books["T-A"] = _book(38, 42)  # mid ahora 40: siguió en contra
    await eng._tick()

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout1_cents == -7.0  # 40 − 47
    assert row.markout1_age_sec >= 60.0
    assert row.markout2_cents is None  # T+5min todavía no


@pytest.mark.asyncio
async def test_markout2_completa_y_saca_de_la_cola():
    """⚠️ CAMBIO SEMÁNTICO DELIBERADO (2026-08-14): la versión previa de este test
    pineaba el BUG — esperaba markout1 y markout2 escritos en la MISMA pasada con el
    MISMO mark (ambos 3.0). Producción lo delató: 5/5 fills con markout2 == markout1
    exacto, o sea el horizonte largo era una copia del corto. Ahora cada horizonte
    exige su propia observación; acá el mercado no se mueve entre pasadas, así que
    ambos valen 3.0 — pero se escriben en ticks DISTINTOS."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    client.books["T-A"] = _book(54, 60)  # bid cruza el ask → fill sell @53
    await eng._tick()

    eng._markouts_pendientes[0]["creado"] -= timedelta(seconds=301.0)
    client.books["T-A"] = _book(48, 52)  # mid 50: vendimos a 53 y bajó → sell markout +3
    await eng._tick()  # pasada 1: markout1

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout1_cents == 3.0  # sell: precio − mark = 53 − 50
    assert row.markout2_cents is None  # el largo NO se copia del corto
    assert eng._markouts_pendientes  # sigue en cola esperando su observación

    await eng._tick()  # pasada 2: markout2 con su propio mark

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout2_cents == 3.0  # mismo valor porque el mercado no se movió
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

    eng._markouts_pendientes[0]["creado"] -= timedelta(seconds=700.0)  # >10min sin medir
    eng._last_marks.clear()  # el ticker ya no tiene mark
    eng._medir_markouts()

    assert eng._markouts_pendientes == []  # soltado
    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout1_cents is None and row.markout2_cents is None


# =====================================================
# La tabla se auto-describe (2026-08-14)
# =====================================================
# El agente web leyó fee_cents (referencia taker) como "el flag no está aplicado" TRES
# veces. Una columna ambigua en la tabla que juzga el gate es deuda que cuesta
# veredictos: ahora cada fila dice qué modelo rigió y cuánto se cobró de verdad.


@pytest.mark.asyncio
async def test_fila_declara_modelo_maker_y_fee_efectiva():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = Motor5Engine(
        max_tickers=2,
        half_spread_cents=3,
        quote_size_contracts=10,
        max_inventory_contracts=50,
        fair_ttl_sec=600.0,
        fees_as_maker=True,
        jump_retreat_cents=0.0,
    )
    eng._client = client
    await eng._tick()
    client.books["T-A"] = _book(40, 46)  # cruza → fill buy @47
    await eng._tick()

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.fee_model == "maker"
    assert row.fee_effective_cents == kalshi_maker_fee_cents(10, 47)
    assert row.fee_cents == kalshi_fee_cents(10, 47)  # la referencia taker SIGUE ahí


@pytest.mark.asyncio
async def test_fila_declara_modelo_taker_por_default():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)  # default: modelo taker
    await eng._tick()
    client.books["T-A"] = _book(40, 46)
    await eng._tick()

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.fee_model == "taker"
    assert row.fee_effective_cents == row.fee_cents == kalshi_fee_cents(10, 47)


# =====================================================
# El markout exige un mark FRESCO (bug de la sonda 2026-08-14)
# =====================================================
# _last_marks nunca se invalidaba: con max_tickers=10 y fair_fresh=20-48 los tickers
# ROTAN fuera del universo, el mark queda congelado, y markout1/markout2 medían contra
# el MISMO valor viejo (5/5 fills con markout2 == markout1 exacto en producción). El
# T+5min — el horizonte que detecta selección adversa SOSTENIDA — estaba ciego.


@pytest.mark.asyncio
async def test_mark_congelado_no_produce_markout():
    """El ticker rotó fuera del universo: su mark ya no se refresca. Medir contra él
    sería inventar una observación — se espera, y a los 10min se suelta con NULL."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    client.books["T-A"] = _book(40, 46)
    await eng._tick()  # fill
    assert len(eng._markouts_pendientes) == 1

    # El fill fue hace 60s y el mark quedó congelado hace 200s (ticker fuera del universo).
    eng._markouts_pendientes[0]["creado"] -= timedelta(seconds=60.0)
    eng._last_marks_at["T-A"] -= 200.0
    eng._medir_markouts()

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout1_cents is None  # no se mide contra un mark congelado
    assert eng._markouts_pendientes  # sigue esperando uno fresco


@pytest.mark.asyncio
async def test_los_dos_horizontes_son_observaciones_distintas():
    """EL BUG EXACTO: con el fill ya viejo (>300s), markout1 y markout2 no pueden
    escribirse en la misma pasada leyendo el mismo mark — dos horizontes exigen dos
    observaciones. markout2 espera a la pasada siguiente (con su mark propio)."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    client.books["T-A"] = _book(40, 46)
    await eng._tick()  # fill buy @47

    eng._markouts_pendientes[0]["creado"] -= timedelta(seconds=400.0)  # el fill ya pasó los 300s
    client.books["T-A"] = _book(38, 42)  # mid 40
    await eng._tick()  # pasada 1: solo markout1

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout1_cents == -7.0 and row.markout2_cents is None

    client.books["T-A"] = _book(48, 52)  # mid 50: el mercado SE MOVIÓ entre horizontes
    await eng._tick()  # pasada 2: markout2 con su propia observación

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.markout2_cents == 3.0  # 50 − 47: distinto de markout1, como debe ser
    # El fill 1 completó y salió de la cola (puede haber otros pendientes: en shadow la
    # quote ya no se retira por salto, así que el mercado sigue generando fills).
    assert all(p["id"] != row.id for p in eng._markouts_pendientes)


# =====================================================
# Los markouts sobreviven al redeploy (fuga 2026-08-14)
# =====================================================
# La cola vivía solo en RAM: los fills 254/255 perdieron su T+5min porque el proceso
# se reinició 6 min después del fill. Con 5 deploys en un día eso es una fuga MATERIAL
# de la métrica que decide el gate. La cola se RECONSTRUYE de la tabla (que ya sabe
# qué falta medir), no se persiste aparte.


@pytest.mark.asyncio
async def test_rehidrata_los_markouts_pendientes_tras_restart():
    from datetime import UTC, datetime

    from src.storage.models import MMShadowFill

    # Un fill de hace 2 min con markout1 medido y markout2 pendiente (el caso 254/255).
    with get_session() as s:
        fila = MMShadowFill(
            ticker="T-A",
            side="buy",
            price_cents=47,
            count=10,
            fee_cents=18,
            rule="test",
            inventory_after=10,
            markout1_cents=-2.0,
            created_at=datetime.now(UTC) - timedelta(seconds=120),
        )
        s.add(fila)
        s.commit()
        s.refresh(fila)
        fill_id = fila.id

    eng = _engine(_ReadOnlyClient())  # proceso NUEVO: cola vacía
    assert eng._markouts_pendientes == []

    eng._rehidratar_markouts()

    assert len(eng._markouts_pendientes) == 1
    p = eng._markouts_pendientes[0]
    assert p["id"] == fill_id and p["price"] == 47 and p["side"] == "buy"
    assert p["m1_hecho"] is True  # markout1 ya estaba: solo falta el largo


@pytest.mark.asyncio
async def test_no_rehidrata_fills_vencidos_ni_completos():
    from datetime import UTC, datetime

    from src.storage.models import MMShadowFill

    with get_session() as s:
        s.add(  # vencido: su ventana de 600s ya pasó
            MMShadowFill(
                ticker="VIEJO",
                side="buy",
                price_cents=47,
                count=10,
                fee_cents=18,
                rule="t",
                inventory_after=10,
                created_at=datetime.now(UTC) - timedelta(seconds=900),
            )
        )
        s.add(  # completo: ya tiene los dos horizontes
            MMShadowFill(
                ticker="COMPLETO",
                side="buy",
                price_cents=47,
                count=10,
                fee_cents=18,
                rule="t",
                inventory_after=10,
                markout1_cents=-1.0,
                markout2_cents=-3.0,
                created_at=datetime.now(UTC) - timedelta(seconds=60),
            )
        )
        s.commit()

    eng = _engine(_ReadOnlyClient())
    eng._rehidratar_markouts()

    assert eng._markouts_pendientes == []  # ninguno de los dos entra
