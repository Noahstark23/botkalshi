"""Contrato del gate económico M5 F1-v2."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from scripts.tablero_gate import METRIC_VERSION, margen_teorico, tablero, zona_muerta
from src.strategies.fair_value_book import FAIR_METHOD_VERSION


def _db(
    tmp_path, rows: list[tuple], *, current_schema: bool = True, run_status: str = "clean"
) -> str:
    path = tmp_path / "gate.db"
    con = sqlite3.connect(path)
    extra = (
        ", metric_version TEXT, experiment_id TEXT, event_ticker TEXT, "
        "fee_multiplier REAL, fee_type TEXT, fee_source TEXT, fee_base_multiplier REAL, "
        "fee_base_type TEXT, fee_override_multiplier REAL, fee_override_type TEXT, "
        "fee_schedule_observed_at TEXT, exchange_environment TEXT, "
        "fair_method_version TEXT, fair_odds_event_id TEXT, fair_sport_key TEXT, "
        "fair_bookmaker_keys TEXT, fair_book_count INTEGER, fair_oldest_book_update TEXT, "
        "fair_newest_book_update TEXT, fair_min_books INTEGER, fair_max_book_age_min REAL, "
        "fair_computed_at TEXT, fair_odds_regions TEXT, fair_sport_keys_config TEXT, "
        "fair_cache_ttl_sec REAL, fair_ttl_sec REAL, "
        "commence_time TEXT, seconds_to_kickoff REAL, "
        "fee_model TEXT, markout2_age_sec REAL, "
        "policy_require_pregame INTEGER, policy_kickoff_buffer_sec REAL, "
        "quote_seconds_to_kickoff REAL, side TEXT, rule TEXT, yes_bid INTEGER, "
        "yes_ask INTEGER, crossed_depth NUMERIC"
        if current_schema
        else ""
    )
    con.execute(
        "create table mm_shadow_fills (id integer primary key, ticker text, created_at text, "
        "count integer, price_cents integer, markout2_cents real, fee_effective_cents integer"
        + extra
        + ")"
    )
    columns = (
        "ticker, created_at, count, price_cents, markout2_cents, fee_effective_cents, "
        "metric_version, experiment_id, event_ticker, fee_multiplier, fee_type, fee_source, "
        "fee_base_multiplier, fee_base_type, fee_override_multiplier, fee_override_type, "
        "fee_schedule_observed_at, exchange_environment, "
        "fair_method_version, fair_odds_event_id, fair_sport_key, fair_bookmaker_keys, "
        "fair_book_count, fair_oldest_book_update, fair_newest_book_update, fair_min_books, "
        "fair_max_book_age_min, fair_computed_at, fair_odds_regions, fair_sport_keys_config, "
        "fair_cache_ttl_sec, fair_ttl_sec, "
        "commence_time, seconds_to_kickoff, "
        "fee_model, markout2_age_sec, policy_require_pregame, "
        "policy_kickoff_buffer_sec, quote_seconds_to_kickoff, side, rule, yes_bid, yes_ask, "
        "crossed_depth"
    )
    if current_schema:
        placeholders = ",".join("?" for _ in columns.split(","))
        con.executemany(f"insert into mm_shadow_fills ({columns}) values ({placeholders})", rows)
        con.execute(
            "create table mm_experiment_runs (id integer primary key, experiment_id text, "
            "status text, reason text, started_at text, ended_at text)"
        )
        experiments = {row[7] for row in rows if row[6] == METRIC_VERSION and row[7]}
        for experiment in experiments:
            con.execute(
                "insert into mm_experiment_runs "
                "(experiment_id,status,started_at,ended_at) values (?,?,?,?)",
                (
                    experiment,
                    run_status,
                    datetime(2026, 8, 22, tzinfo=UTC).isoformat(),
                    datetime(2026, 9, 22, tzinfo=UTC).isoformat()
                    if run_status == "clean"
                    else None,
                ),
            )
    else:
        con.executemany(
            "insert into mm_shadow_fills (ticker,created_at,count,price_cents,"
            "markout2_cents,fee_effective_cents) values (?,?,?,?,?,?)",
            [row[:6] for row in rows],
        )
    con.commit()
    con.close()
    return str(path)


def _row(
    event: int,
    *,
    day: int = 0,
    markout: float | None = 2.0,
    version: str | None = METRIC_VERSION,
    experiment: str = "exp-a",
    require_pregame: bool = True,
    buffer_sec: float = 120.0,
    fill_seconds: float = 7_200.0,
    quote_seconds: float = 7_260.0,
    fee_effective: int = 1,
    markout2_age: float = 360.0,
    side: str = "buy",
    yes_bid: int = 40,
    yes_ask: int = 46,
    rule: str | None = None,
    fee_multiplier: float = 0.5,
    fee_source: str = "series",
    fee_override_multiplier: float | None = None,
    fee_override_type: str | None = None,
    exchange_environment: str = "production",
    crossed_depth: str | None = "1.00",
    event_ticker_override: str | None = None,
    market_ticker_override: str | None = None,
) -> tuple:
    created = datetime(2026, 8, 22, tzinfo=UTC) + timedelta(days=day)
    event_ticker = event_ticker_override or f"KXMLBGAME-E{event:04d}"
    market_ticker = market_ticker_override or f"{event_ticker}-YES"
    return (
        market_ticker,
        created.isoformat(),
        1,
        50,
        markout,
        fee_effective,
        version,
        experiment if version else None,
        event_ticker if version else None,
        fee_multiplier if version else None,
        "quadratic_with_maker_fees" if version else None,
        fee_source if version else None,
        0.5 if version else None,
        "quadratic_with_maker_fees" if version else None,
        fee_override_multiplier if version else None,
        fee_override_type if version else None,
        created.isoformat() if version else None,
        exchange_environment if version else None,
        FAIR_METHOD_VERSION,
        f"odds-{event}",
        "baseball_mlb",
        "pinnacle,draftkings,fanduel",
        3,
        (created - timedelta(minutes=1)).isoformat(),
        (created - timedelta(seconds=30)).isoformat(),
        3,
        15.0,
        (created - timedelta(seconds=30)).isoformat(),
        "us",
        "baseball_mlb",
        60.0,
        120.0,
        (created + timedelta(hours=2)).isoformat() if version else None,
        fill_seconds if version else None,
        "maker" if version else None,
        markout2_age if version else None,
        int(require_pregame) if version else None,
        buffer_sec if version else None,
        quote_seconds if version else None,
        side,
        rule or (f"ask {yes_ask} < bid 50" if side == "buy" else f"bid {yes_bid} > ask 50"),
        yes_bid,
        yes_ask,
        crossed_depth,
    )


def test_legacy_se_muestra_pero_no_puede_graduar(tmp_path):
    db = _db(tmp_path, [_row(1, version=None)])
    output = tablero(db)

    assert "COHORTE LEGACY — SOLO DIAGNÓSTICO" in output
    assert "fills=0/500" in output
    assert "F2/F3 bloqueados" in output


def test_n_pequeno_positivo_no_declara_pasa(tmp_path):
    db = _db(tmp_path, [_row(1), _row(2)])
    output = tablero(db, half_spread=20)  # el spread no cambia la economía del markout

    assert "media neta/contrato=+1.000¢" in output
    assert "SIN DATOS SUFICIENTES" in output
    assert "PASA F1" not in output


def test_markout_neto_negativo_falla_sin_volver_a_sumar_spread(tmp_path):
    db = _db(tmp_path, [_row(1, markout=0.5)])  # 0.5 - fee 1.0 = -0.5c
    output = tablero(db, half_spread=5)

    assert "media neta/contrato=-0.500¢" in output
    assert "FALLA ECONOMÍA" in output


def test_agrupa_fills_correlacionados_por_evento(tmp_path):
    rows = [_row(1), _row(1), _row(1), _row(2)]
    output = tablero(_db(tmp_path, rows))

    assert "fills=4/500 | eventos=2/100" in output


def test_identidad_oficial_impide_contar_mercados_como_eventos(tmp_path):
    event_ticker = "KXMLBGAME-ONE-EVENT"
    rows = [
        _row(
            market,
            day=market % 30,
            event_ticker_override=event_ticker,
            market_ticker_override=f"{event_ticker}-MARKET{market:03d}",
        )
        for market in range(100)
        for _ in range(5)
    ]

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "fills=500/500 | eventos=1/100 | días=30/30" in output
    assert "SIN DATOS SUFICIENTES" in output
    assert "PASA F1" not in output


def test_no_mezcla_configuraciones_y_elije_la_cohorte_mas_reciente(tmp_path):
    old = _row(1, markout=20.0, experiment="exp-old")
    newest = _row(2, day=10, markout=0.5, experiment="exp-new")
    output = tablero(_db(tmp_path, [old, newest]))

    assert "experiment_id=exp-new" in output
    assert "media neta/contrato=-0.500¢" in output
    assert "+19.000¢" not in output


def test_pasa_solo_con_muestra_independiente_y_economia_positiva(tmp_path):
    rows = [_row(event, day=event % 30) for event in range(100) for _ in range(5)]
    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "fills=500/500 | eventos=100/100 | días=30/30" in output
    assert "LCB95 block-bootstrap=+1.000¢" in output
    assert "VEREDICTO: PASA F1-v2" in output
    assert "NO autoriza producción" in output


def test_no_aprueba_implicitamente_la_ultima_cohorte_con_fills(tmp_path):
    rows = [_row(event, day=event % 30) for event in range(100) for _ in range(5)]
    output = tablero(_db(tmp_path, rows))

    assert "REQUIERE --experiment-id explícito" in output
    assert "PASA F1" not in output


def test_no_aprueba_si_un_evento_repetido_hace_perder_el_total(tmp_path):
    # Regresión adversarial: la media no ponderada por evento era +1.88c, aunque 401/500
    # fills pertenecían a un evento que perdía 10c/fill y el total era -7.624c/fill.
    rows = [_row(0, day=0, markout=-9.0) for _ in range(401)]
    rows.extend(_row(event, day=event % 39, markout=3.0) for event in range(1, 100))

    output = tablero(_db(tmp_path, rows))

    assert "fills=500/500 | eventos=100/100 | días=39/30" in output
    assert "media neta/contrato=-7.624¢" in output
    assert "FALLA ECONOMÍA" in output
    assert "PASA F1" not in output


def test_concentracion_por_fills_bloquea_evento_dominante_aunque_su_pnl_sea_cero(tmp_path):
    # El evento dominante queda neto en cero (markout 1c - fee 1c), por lo que una
    # concentración calculada solo con abs(PnL) lo ocultaba por completo.
    rows = [_row(0, day=index % 30, markout=1.0) for index in range(401)]
    rows.extend(_row(event, day=event % 30, markout=2.0) for event in range(1, 100))

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "fills=500/500 | eventos=100/100 | días=30/30" in output
    assert "concentración fills/evento=80.2%" in output
    assert "FALLA GATE" in output
    assert "PASA F1" not in output


def test_fill_observado_despues_del_cutoff_bloquea_toda_la_cohorte(tmp_path):
    rows = [_row(event, day=event % 30) for event in range(100) for _ in range(5)]
    rows[-1] = _row(99, day=29, fill_seconds=119.0)

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "violaciones de política/metadata=1" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output
    assert "PASA F1" not in output


def test_buffer_persistido_no_esta_hardcodeado_a_120(tmp_path):
    rows = [
        _row(
            event,
            day=event % 30,
            buffer_sec=300.0,
            fill_seconds=360.0,
            quote_seconds=420.0,
        )
        for event in range(100)
        for _ in range(5)
    ]

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "violaciones de política/metadata=0" in output
    assert "VEREDICTO: PASA F1-v2" in output


def test_require_pregame_false_no_puede_graduar(tmp_path):
    rows = [
        _row(event, day=event % 30, require_pregame=False) for event in range(100) for _ in range(5)
    ]

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "FALLA VALIDEZ DE POLÍTICA" in output
    assert "PASA F1" not in output


def test_markouts_faltantes_no_desaparecen_del_denominador(tmp_path):
    completos = [_row(event, day=event % 30) for event in range(100) for _ in range(5)]
    faltantes = [
        _row(event + 100, day=event % 30, markout=None) for event in range(100) for _ in range(5)
    ]

    output = tablero(_db(tmp_path, completos + faltantes), experiment_id="exp-a")

    assert "fills=500/500" in output  # antes este numerador solo veía los completos buenos
    assert "violaciones de política/metadata=500" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output
    assert "PASA F1" not in output


def test_fee_persistida_debe_coincidir_con_formula_y_ceil(tmp_path):
    rows = [
        _row(event, day=event % 30, markout=0.5, fee_effective=0)
        for event in range(100)
        for _ in range(5)
    ]

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "violaciones de política/metadata=500" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output
    assert "PASA F1" not in output


def test_overrides_distintos_por_evento_no_corrompen_la_cohorte(tmp_path):
    base = _row(1)
    overridden = _row(
        2,
        fee_multiplier=1.0,
        fee_source="event_override",
        fee_override_multiplier=1.0,
        fee_override_type="quadratic_with_maker_fees",
    )

    output = tablero(_db(tmp_path, [base, overridden]), experiment_id="exp-a")

    assert "violaciones de política/metadata=0" in output


def test_un_mismo_evento_con_dos_schedules_inconsistentes_bloquea(tmp_path):
    base = _row(1)
    changed = _row(
        1,
        fee_multiplier=1.0,
        fee_source="event_override",
        fee_override_multiplier=1.0,
        fee_override_type="quadratic_with_maker_fees",
    )

    output = tablero(_db(tmp_path, [base, changed]), experiment_id="exp-a")

    assert "violaciones de política/metadata=1" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output


def test_f1_demo_o_provenance_incompleta_no_pueden_graduar(tmp_path):
    demo = _row(1, exchange_environment="demo")
    bad_provenance = list(_row(2))
    bad_provenance[18] = "metodo-desconocido"  # fair_method_version

    output = tablero(_db(tmp_path, [demo, tuple(bad_provenance)]), experiment_id="exp-a")

    assert "violaciones de política/metadata=2" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output


def test_markout_t5_debe_tener_al_menos_300_segundos(tmp_path):
    rows = [_row(event, day=event % 30) for event in range(100) for _ in range(5)]
    rows[-1] = _row(99, day=29, markout2_age=299.9)

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "violaciones de política/metadata=1" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output
    assert "PASA F1" not in output


@pytest.mark.parametrize("bad_markout", [float("inf"), 99.0, 0.25])
def test_markout_debe_ser_finito_fisicamente_posible_y_en_medio_centavo(tmp_path, bad_markout):
    rows = [_row(event, day=event % 30) for event in range(100) for _ in range(5)]
    rows[-1] = _row(99, day=29, markout=bad_markout)

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "violaciones de política/metadata=1" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output
    assert "PASA F1" not in output


def test_run_activo_o_crasheado_bloquea_la_cohorte(tmp_path):
    rows = [_row(event, day=event % 30) for event in range(100) for _ in range(5)]

    output = tablero(_db(tmp_path, rows, run_status="running"), experiment_id="exp-a")

    assert "cadena de custodia: clean=0 no-clean=1" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output
    assert "PASA F1" not in output


@pytest.mark.parametrize(
    "bad_row",
    [
        _row(99, yes_bid=2_700, yes_ask=-2_900),
        _row(99, yes_bid=60, yes_ask=40),
        _row(99, rule="ask 46 < bid 49"),
        _row(99, side="sell", yes_bid=40, yes_ask=46),
    ],
)
def test_bbo_o_regla_incoherente_no_pueden_graduar(tmp_path, bad_row):
    rows = [_row(event, day=event % 30) for event in range(100) for _ in range(5)]
    rows[-1] = bad_row

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "violaciones de política/metadata=1" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output
    assert "PASA F1" not in output


@pytest.mark.parametrize("crossed_depth", [None, "0.33", "NaN"])
def test_depth_cruzada_debe_cubrir_el_count_persistido(tmp_path, crossed_depth):
    rows = [_row(event, day=event % 30) for event in range(100) for _ in range(5)]
    rows[-1] = _row(99, day=29, crossed_depth=crossed_depth)

    output = tablero(_db(tmp_path, rows), experiment_id="exp-a")

    assert "violaciones de política/metadata=1" in output
    assert "FALLA VALIDEZ DE POLÍTICA" in output
    assert "PASA F1" not in output


def test_schema_legacy_sin_columnas_nuevas_es_compatible(tmp_path):
    output = tablero(_db(tmp_path, [_row(1, version=None)], current_schema=False))
    assert "COHORTE LEGACY" in output
    assert "SIN DATOS F1-v2" in output


def test_mlb_medio_ya_no_tiene_zona_muerta_con_medio_multiplicador():
    assert margen_teorico(50, fee_multiplier=0.5) == pytest.approx(0.3825)
    assert zona_muerta(fee_multiplier=0.5) is None


def test_control_multiplicador_uno_reproduce_la_antigua_zona_muerta():
    assert margen_teorico(50, fee_multiplier=1.0) < 0
    assert zona_muerta(fee_multiplier=1.0) is not None
