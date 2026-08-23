"""Motor5Engine (F1 shadow): flujo del tick de punta a punta, sin una sola orden.

El 'cliente' fake solo tiene get_orderbook — si el engine intentara colocar/cancelar una
orden, el AttributeError haría fallar el test: la garantía de CERO órdenes es estructural.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import src.strategies.motor_5_mm.engine as engine_module
from src.monitoring.health import BotState
from src.storage.models import (
    MMExperimentRun,
    MMFunnelSnapshot,
    MMQuote,
    MMShadowFill,
    get_session,
)
from src.strategies.fair_value_book import FairProvenance, FairValueBook
from src.strategies.motor_5_mm.engine import Motor5DataIntegrityError, Motor5Engine
from src.strategies.motor_5_mm.fee_policy import SeriesFeeObservation


class _ReadOnlyClient:
    """Solo lectura de orderbook. Sin place_order/cancel_order a propósito."""

    def __init__(self):
        self.books: dict[str, dict] = {}

    async def get_orderbook(self, ticker: str) -> dict:
        book = self.books.get(ticker)
        if book is None:
            raise RuntimeError("book no disponible")
        return {"orderbook": book}


def test_f1_runtime_rechaza_demo_y_size_mayor_a_uno():
    with pytest.raises(ValueError, match="exchange_environment=production"):
        Motor5Engine(fees_as_maker=True)
    with pytest.raises(ValueError, match="quote_size_contracts=1"):
        Motor5Engine(
            fees_as_maker=True,
            exchange_environment="production",
            quote_size_contracts=10,
        )


def test_experiment_id_separa_entorno_y_config_del_fair_upstream():
    base = Motor5Engine(
        experiment_label="cohorte",
        exchange_environment="production",
        fair_min_books=3,
        fair_max_book_age_min=15,
        fair_odds_regions="us",
        fair_sport_keys_config="baseball_mlb",
        fair_cache_ttl_sec=60,
    )
    demo = Motor5Engine(
        experiment_label="cohorte",
        exchange_environment="demo",
        fair_min_books=3,
        fair_max_book_age_min=15,
        fair_odds_regions="us",
        fair_sport_keys_config="baseball_mlb",
        fair_cache_ttl_sec=60,
    )
    different_books = Motor5Engine(
        experiment_label="cohorte",
        exchange_environment="production",
        fair_min_books=4,
        fair_max_book_age_min=15,
        fair_odds_regions="us",
        fair_sport_keys_config="baseball_mlb",
        fair_cache_ttl_sec=60,
    )

    assert len({base._experiment_id, demo._experiment_id, different_books._experiment_id}) == 3


def _book(
    yes_bid: int | None,
    yes_ask: int | None,
    *,
    yes_bid_depth: object = 100,
    yes_ask_depth: object = 100,
) -> dict:
    yes = [[yes_bid, yes_bid_depth]] if yes_bid is not None else []
    no = [[100 - yes_ask, yes_ask_depth]] if yes_ask is not None else []
    return {"yes": yes, "no": no}


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
async def test_tick_quotes_and_persists():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    with get_session() as s:
        quotes = list(s.exec(select(MMQuote)))
        snaps = list(s.exec(select(MMFunnelSnapshot)))
    assert len(quotes) == 1
    q = quotes[0]
    assert q.ticker == "T-A" and q.bid_cents == 47 and q.ask_cents == 53
    assert q.yes_bid == 40 and q.yes_ask == 60
    assert len(snaps) == 1 and snaps[0].quoted == 1 and snaps[0].fills == 0


@pytest.mark.asyncio
async def test_pregame_gate_exige_kickoff_y_buffer_de_120s():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    now = datetime.now(UTC)
    eng = Motor5Engine(require_pregame=True, kickoff_buffer_sec=120)
    eng._client = client

    FairValueBook.publish({"T-A": 0.50}, commence_times={"T-A": now + timedelta(seconds=60)})
    await eng._tick()
    assert "T-A" not in eng._live_quotes

    FairValueBook.publish({"T-A": 0.50}, commence_times={"T-A": now + timedelta(minutes=10)})
    await eng._tick()
    assert "T-A" in eng._live_quotes


@pytest.mark.asyncio
async def test_cross_on_next_tick_fills_and_updates_inventory():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()  # quote resting: bid 47 / ask 53
    client.books["T-A"] = _book(40, 46)  # el ask del book cruza POR DEBAJO de nuestro bid
    await eng._tick()
    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
    assert len(fills) == 1
    f = fills[0]
    # El cruce del top demuestra al menos 1 contrato, no el fill completo de size=10.
    assert f.side == "buy" and f.price_cents == 47 and f.count == 1
    assert f.inventory_after == 1
    assert eng._inventory.net("T-A") == 1


@pytest.mark.asyncio
async def test_cross_requires_one_full_contract_of_fixed_point_top_depth():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    eng._jump_retreat = 0.0
    await eng._tick()  # quote resting 47/53

    client.books["T-A"] = _book(40, 46, yes_ask_depth="0.33")
    await eng._tick()  # cruza 47, pero 0.33 no cubre count=1
    with get_session() as s:
        assert list(s.exec(select(MMShadowFill))) == []

    client.books["T-A"] = _book(40, 44, yes_ask_depth="1.00")
    await eng._tick()  # cruza la nueva quote 45 y sí cubre count=1
    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
    assert len(fills) == 1
    assert fills[0].count == 1
    assert fills[0].crossed_depth == Decimal("1.000000000000000000")


@pytest.mark.asyncio
async def test_touch_never_fills():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    client.books["T-A"] = _book(40, 47)  # TOCA el bid (==47), no cruza
    await eng._tick()
    with get_session() as s:
        assert list(s.exec(select(MMShadowFill))) == []


@pytest.mark.asyncio
async def test_no_book_skips_and_retires_live_quote():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    assert "T-A" in eng._live_quotes
    del client.books["T-A"]  # book caído
    await eng._tick()
    assert "T-A" not in eng._live_quotes  # no se cotiza (ni se llena) a ciegas
    with get_session() as s:
        snaps = list(s.exec(select(MMFunnelSnapshot)))
    assert snaps[-1].skip_no_book == 1 and snaps[-1].quoted == 0


@pytest.mark.asyncio
async def test_stale_fair_shrinks_universe():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    eng._fair_ttl = 0.0  # todo fair es viejo
    await eng._tick()
    with get_session() as s:
        snaps = list(s.exec(select(MMFunnelSnapshot)))
        assert list(s.exec(select(MMQuote))) == []
    assert snaps[0].fair_fresh == 0 and snaps[0].quoted == 0


@pytest.mark.asyncio
async def test_max_tickers_caps_universe_deterministically():
    client = _ReadOnlyClient()
    for t in ("T-A", "T-B", "T-C"):
        client.books[t] = _book(40, 60)
    FairValueBook.publish({"T-C": 0.5, "T-A": 0.5, "T-B": 0.5})
    eng = _engine(client)  # max_tickers=2
    await eng._tick()
    with get_session() as s:
        quoted = {q.ticker for q in s.exec(select(MMQuote))}
    assert quoted == {"T-A", "T-B"}  # orden alfabético, capado


@pytest.mark.asyncio
async def test_ticker_leaving_universe_observes_final_interval_then_retires_quote():
    """El fair expira, pero la quote estuvo expuesta desde el tick anterior: se observa
    ese último intervalo antes de retirarla para no censurar justo el cruce final."""
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    await eng._tick()
    FairValueBook.clear()  # fair desaparece (equivale a TTL vencido)
    client.books["T-A"] = _book(40, 44)  # cruce que ANTES habría llenado
    await eng._tick()
    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
        assert len(fills) == 1 and fills[0].side == "buy"
    assert "T-A" not in eng._live_quotes


@pytest.mark.asyncio
async def test_revalida_reloj_despues_del_await_y_no_cotiza_dentro_del_buffer(monkeypatch):
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    base = datetime.now(UTC)
    FairValueBook.publish({"T-A": 0.50}, commence_times={"T-A": base + timedelta(seconds=121)})
    eng = Motor5Engine(require_pregame=True, kickoff_buffer_sec=120, fair_ttl_sec=600)
    eng._client = client

    instants = iter([base, base + timedelta(seconds=2)])

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(instants)

    monkeypatch.setattr(engine_module, "datetime", _Clock)
    await eng._tick()

    assert "T-A" not in eng._live_quotes
    with get_session() as s:
        assert list(s.exec(select(MMQuote))) == []


@pytest.mark.asyncio
async def test_fill_del_ultimo_intervalo_persiste_hora_real_despues_del_cutoff(monkeypatch):
    ticker = "KXMLBGAME-E1-YES"
    client = _ReadOnlyClient()
    client.books[ticker] = _book(40, 60)
    commence = datetime.now(UTC) + timedelta(minutes=10)
    FairValueBook.publish(
        {ticker: 0.50},
        commence_times={ticker: commence},
        event_tickers={ticker: "KXMLBGAME-E1"},
        provenances={
            ticker: FairProvenance(
                event_ticker="KXMLBGAME-E1",
                odds_event_id="odds-e1",
                sport_key="baseball_mlb",
                bookmaker_keys=("pinnacle",),
                oldest_book_update=None,
                newest_book_update=None,
                min_books=1,
                max_book_age_min=None,
            )
        },
    )
    eng = Motor5Engine(
        series_csv="KXMLBGAME",
        require_pregame=True,
        kickoff_buffer_sec=120,
        fair_ttl_sec=600,
        fees_as_maker=True,
        quote_size_contracts=1,
        exchange_environment="production",
        fair_odds_regions="us",
        fair_sport_keys_config="baseball_mlb",
        fair_cache_ttl_sec=60,
    )
    eng._client = client
    observation = SeriesFeeObservation(
        series_ticker="KXMLBGAME",
        fee_type="quadratic_with_maker_fees",
        multiplier=Fraction(1, 2),
        observed_at=datetime.now(UTC),
        event_ticker="KXMLBGAME-E1",
        base_multiplier=Fraction(1, 2),
    )
    eng._fee_policy = type(
        "FakeFeePolicy",
        (),
        {"observe": staticmethod(lambda _ticker, event_ticker: _async(observation))},
    )()
    await eng._tick()

    instants = iter(
        [
            commence - timedelta(seconds=100),
            commence - timedelta(seconds=90),
            commence - timedelta(seconds=90),  # reloj de _medir_markouts
        ]
    )

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(instants)

    monkeypatch.setattr(engine_module, "datetime", _Clock)
    client.books[ticker] = _book(40, 44)
    await eng._tick()

    with get_session() as s:
        row = list(s.exec(select(MMShadowFill)))[0]
    assert row.quote_seconds_to_kickoff is not None
    assert row.quote_seconds_to_kickoff >= 120
    assert row.seconds_to_kickoff == 90.0  # el gate la rechaza, no la etiqueta pregame


@pytest.mark.asyncio
async def test_cambio_de_override_durante_quote_invalida_sin_inventar_fill():
    ticker = "KXMLBGAME-E1-YES"
    event_ticker = "KXMLBGAME-E1"
    client = _ReadOnlyClient()
    client.books[ticker] = _book(40, 60)
    FairValueBook.publish(
        {ticker: 0.50},
        event_tickers={ticker: event_ticker},
        provenances={
            ticker: FairProvenance(
                event_ticker=event_ticker,
                odds_event_id="odds-e1",
                sport_key="baseball_mlb",
                bookmaker_keys=("pinnacle",),
                oldest_book_update=None,
                newest_book_update=None,
                min_books=1,
                max_book_age_min=None,
            )
        },
    )
    eng = Motor5Engine(
        series_csv="KXMLBGAME",
        fees_as_maker=True,
        exchange_environment="production",
        fair_odds_regions="us",
        fair_sport_keys_config="baseball_mlb",
        fair_cache_ttl_sec=60,
    )
    eng._client = client
    base = SeriesFeeObservation(
        series_ticker="KXMLBGAME",
        event_ticker=event_ticker,
        fee_type="quadratic_with_maker_fees",
        multiplier=Fraction(1, 2),
        source="series",
        base_multiplier=Fraction(1, 2),
        observed_at=datetime.now(UTC),
    )
    overridden = SeriesFeeObservation(
        series_ticker="KXMLBGAME",
        event_ticker=event_ticker,
        fee_type="quadratic_with_maker_fees",
        multiplier=Fraction(1, 1),
        source="event_override",
        base_multiplier=Fraction(1, 2),
        override_fee_type="quadratic_with_maker_fees",
        override_multiplier=Fraction(1, 1),
        observed_at=datetime.now(UTC),
    )

    class _ChangingFee:
        calls = 0

        async def observe(self, _ticker, *, event_ticker):
            self.calls += 1
            return base if self.calls == 1 else overridden

    eng._fee_policy = _ChangingFee()
    eng._begin_experiment_run()
    await eng._tick()
    client.books[ticker] = _book(40, 46)
    await eng._tick()

    with get_session() as s:
        assert list(s.exec(select(MMShadowFill))) == []
        run = s.get(MMExperimentRun, eng._experiment_run_id)
    assert run is not None and run.status == "invalid"
    assert "fee cambió" in (run.reason or "")


async def _async(value):
    return value


def test_fill_no_persistido_no_muta_inventario_y_marca_run_invalido(monkeypatch):
    client = _ReadOnlyClient()
    eng = _engine(client)
    eng._begin_experiment_run()
    quote = engine_module.QuoteSet(ticker="T-A", fair_prob=0.50, bid_cents=47, ask_cents=53, size=1)
    monkeypatch.setattr(eng, "_persist_fill", lambda *args, **kwargs: None)

    with pytest.raises(Motor5DataIntegrityError, match="no persistido"):
        eng._settle_fills(
            quote,
            40,
            46,
            yes_bid_depth=Decimal("10"),
            yes_ask_depth=Decimal("1"),
            fill_fair_prob=0.50,
        )

    assert eng._inventory.net("T-A") == 0
    with get_session() as s:
        run = s.get(MMExperimentRun, eng._experiment_run_id)
    assert run is not None and run.status == "invalid"
    assert BotState.motor5_experiment_invalid is True


@pytest.mark.asyncio
async def test_quote_expuesta_sin_bbo_deja_cadena_invalida():
    client = _ReadOnlyClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    eng = _engine(client)
    eng._begin_experiment_run()
    await eng._tick()

    del client.books["T-A"]
    await eng._tick()

    with get_session() as s:
        run = s.get(MMExperimentRun, eng._experiment_run_id)
    assert run is not None and run.status == "invalid"
    assert "sin BBO" in (run.reason or "")


def test_run_limpio_y_run_invalido_son_durables():
    first = Motor5Engine(experiment_label="clean")
    first._begin_experiment_run()
    first._close_experiment_run_clean()

    second = Motor5Engine(experiment_label="broken")
    second._begin_experiment_run()
    second._invalidate_experiment("gap de prueba")

    with get_session() as s:
        clean = s.get(MMExperimentRun, first._experiment_run_id)
        broken = s.get(MMExperimentRun, second._experiment_run_id)
    assert clean is not None and clean.status == "clean" and clean.ended_at is not None
    assert broken is not None and broken.status == "invalid" and broken.reason == "gap de prueba"


def test_rehidrata_inventario_de_la_misma_cohorte():
    eng = Motor5Engine(
        experiment_label="rehydrate",
        fees_as_maker=True,
        series_csv="KXMLBGAME",
        exchange_environment="production",
    )
    with get_session() as s:
        s.add(
            MMShadowFill(
                ticker="KXMLBGAME-E1-YES",
                side="buy",
                price_cents=47,
                count=1,
                rule="test",
                inventory_after=1,
                metric_version=eng.F1_METRIC_VERSION,
                experiment_id=eng._experiment_id,
                fee_multiplier=0.5,
            )
        )
        s.commit()

    eng._rehidratar_inventory()

    assert eng._inventory.net("KXMLBGAME-E1-YES") == 1


@pytest.mark.asyncio
async def test_excepcion_no_clasificada_del_tick_es_fatal_y_no_cierra_clean():
    class _ContextClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    eng = Motor5Engine(client_factory=_ContextClient)
    eng._tick = AsyncMock(side_effect=RuntimeError("fallo después de mutación parcial"))

    with pytest.raises(Motor5DataIntegrityError, match="trayectoria shadow"):
        await eng.run(asyncio.Event())

    with get_session() as s:
        run = s.get(MMExperimentRun, eng._experiment_run_id)
    assert run is not None and run.status == "invalid"
    assert "tick incompleto" in (run.reason or "")


@pytest.mark.asyncio
async def test_shutdown_observa_cruce_final_antes_de_marcar_run_clean():
    class _ContextClient(_ReadOnlyClient):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    client = _ContextClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    stop = asyncio.Event()
    eng = Motor5Engine(client_factory=lambda: client)
    original_tick = eng._tick

    async def tick_then_stop():
        await original_tick()
        client.books["T-A"] = _book(40, 46)  # cruza durante el último intervalo
        stop.set()

    eng._tick = tick_then_stop
    await eng.run(stop)

    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
        run = s.get(MMExperimentRun, eng._experiment_run_id)
    assert len(fills) == 1
    assert run is not None and run.status == "clean"


@pytest.mark.asyncio
async def test_shutdown_sin_bbo_nunca_cierra_clean_con_quote_expuesta():
    class _ContextClient(_ReadOnlyClient):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    client = _ContextClient()
    client.books["T-A"] = _book(40, 60)
    FairValueBook.publish({"T-A": 0.50})
    stop = asyncio.Event()
    eng = Motor5Engine(client_factory=lambda: client)
    original_tick = eng._tick

    async def tick_then_stop():
        await original_tick()
        del client.books["T-A"]
        stop.set()

    eng._tick = tick_then_stop
    await eng.run(stop)

    with get_session() as s:
        fills = list(s.exec(select(MMShadowFill)))
        run = s.get(MMExperimentRun, eng._experiment_run_id)
    assert fills == []
    assert run is not None and run.status == "invalid"
    assert "shutdown" in (run.reason or "")


# =====================================================
# Shape 2026 del orderbook REST + diagnóstico one-shot (incidente 2026-07-14)
# =====================================================


@pytest.mark.asyncio
async def test_book_top_parses_fixed_point_shape():
    """Shape 2026 (*_dollars_fp): el parser NO debe leer vacío en silencio (la causa
    candidata de skip_no_book=~11k/día sin un solo error en logs)."""
    client = AsyncMock()
    client.get_orderbook.return_value = {
        "orderbook": {
            "yes_dollars_fp": [["0.4200", "50.00"]],
            "no_dollars_fp": [["0.5500", "30.00"]],
        }
    }
    eng = _engine(client)
    top = await eng._book_top("KXMLBGAME-X")
    assert top == (42, 45, Decimal("50.00"), Decimal("30.00"))


@pytest.mark.asyncio
async def test_book_top_parses_rest_shape_2026_07():
    """El shape REAL que el diag de #170 capturó en producción (160 líneas, 2026-07-15):
    wrapper 'orderbook_fp' + 'yes_dollars'/'no_dollars' en dólares-string. Era LA causa
    de fair_fresh=10/skip_book=10: books llenos (~$500k) que parseaban vacío."""
    client = AsyncMock()
    client.get_orderbook.return_value = {
        "orderbook_fp": {
            "yes_dollars": [["0.5400", "558383.77"]],
            "no_dollars": [["0.4500", "2000.00"]],
        }
    }
    eng = _engine(client)
    top = await eng._book_top("KXMLBGAME-X")
    assert top == (54, 55, Decimal("558383.77"), Decimal("2000.00"))


@pytest.mark.asyncio
async def test_book_top_legacy_shape_still_works():
    """CONTROL: el shape legacy (yes/no en cents) sigue parseando igual que siempre."""
    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"yes": [[42, 50]], "no": [[55, 30]]}}
    eng = _engine(client)
    top = await eng._book_top("KXMLBGAME-X")
    assert top == (42, 45, Decimal("50"), Decimal("30"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "book",
    [
        {"yes": [["27", "10"]], "no": [["30", "10"]]},  # strings ambiguos -> 2700¢
        {"yes": [[60, 10]], "no": [[60, 10]]},  # ask=40, book cruzado
        {"yes": [[40, 10]], "no": []},  # unilateral no sirve como evidencia F1
    ],
)
async def test_book_top_rechaza_bbo_fuera_de_rango_cruzado_o_unilateral(book):
    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": book}
    eng = _engine(client)

    assert await eng._book_top("KXMLBGAME-X") is None


@pytest.mark.asyncio
async def test_unknown_shape_logs_diagnostic_once_per_ticker():
    """Book inusable → None (skip_no_book) con log one-shot POR TICKER: el mismo ticker
    no re-loguea, pero otro ticker con problema SÍ (un bool global dejaba que el primer
    ticker consumiera el diagnóstico de todos los demás)."""
    from loguru import logger as _logger

    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"claves_nuevas_2027": []}}
    eng = _engine(client)
    records: list[str] = []
    sink = _logger.add(records.append, level="WARNING", format="{message}")
    try:
        assert await eng._book_top("KXMLBGAME-X") is None
        assert await eng._book_top("KXMLBGAME-X") is None  # mismo ticker: sin re-log
        assert await eng._book_top("KXMLBGAME-Y") is None  # OTRO ticker: log propio
    finally:
        _logger.remove(sink)
    shape_logs = [r for r in records if "motor5.book_shape" in r]
    assert len(shape_logs) == 2
    assert "KXMLBGAME-X" in shape_logs[0] and "claves_nuevas_2027" in shape_logs[0]
    assert "KXMLBGAME-Y" in shape_logs[1]


@pytest.mark.asyncio
async def test_chronic_skip_relogs_after_backoff():
    """Un skip CRÓNICO re-loguea tras BOOK_DIAG_REARM_SEC: el one-shot de por vida se
    consumía (p.ej. en el boot, fuera de la ventana de log visible) y 127 funnels con
    skip_book>0 quedaban sin UNA línea de causa (forense 2026-07-15)."""
    from loguru import logger as _logger

    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"yes": [], "no": []}}
    eng = _engine(client)
    records: list[str] = []
    sink = _logger.add(records.append, level="WARNING", format="{message}")
    try:
        assert await eng._book_top("KXMLBGAME-X") is None  # log 1
        assert await eng._book_top("KXMLBGAME-X") is None  # dentro del backoff: silencio
        # Simular que pasó el backoff (envejecer el timestamp guardado, sin mockear reloj).
        eng._book_diag_last["KXMLBGAME-X"] -= eng.BOOK_DIAG_REARM_SEC
        assert await eng._book_top("KXMLBGAME-X") is None  # log 2 (re-armado)
    finally:
        _logger.remove(sink)
    assert len([r for r in records if "motor5.book_shape" in r]) == 2


@pytest.mark.asyncio
async def test_empty_book_diagnostic_distinguishes_thin_market():
    """CONTROL forense: claves correctas pero listas VACÍAS (book fino, sin resting) →
    el log dice yes_levels=0/no_levels=0 — distinguible de un problema de shape."""
    from loguru import logger as _logger

    client = AsyncMock()
    client.get_orderbook.return_value = {"orderbook": {"yes": [], "no": []}}
    eng = _engine(client)
    records: list[str] = []
    sink = _logger.add(records.append, level="WARNING", format="{message}")
    try:
        assert await eng._book_top("KXMLBGAME-Z") is None
    finally:
        _logger.remove(sink)
    shape_logs = [r for r in records if "motor5.book_shape" in r]
    assert len(shape_logs) == 1
    assert "yes_levels=0" in shape_logs[0] and "no_levels=0" in shape_logs[0]
