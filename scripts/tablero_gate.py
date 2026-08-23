"""Tablero READ-ONLY del gate económico de Motor 5 F1-v2.

El markout ya incorpora el precio de la quote: buy = mid futuro − fill, sell = fill −
mid futuro. Por eso el gate correcto NO vuelve a sumar el half-spread. Evalúa el límite
superior económico por contrato después de la fee de entrada::

    net_markout_pc = markout2_cents - fee_effective_cents / count

Solo entran filas de ``metric_version=f1-v2-bbo-depth``: mid bilateral de Kalshi,
multiplicador observado en GET /series y profundidad cruzada suficiente para el contrato
afirmado. Las filas legacy se conservan y muestran como diagnóstico; nunca pueden
graduar el motor.

Uso dentro del contenedor::

    python3 scripts/tablero_gate.py --db /app/data/trades.db
"""

from __future__ import annotations

import argparse
import math
import random
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from src.math.fees import kalshi_maker_fee_cents, maker_fee_multiplier_for_ticker
from src.strategies.fair_value_book import FAIR_METHOD_VERSION

CORTE_MARK_CONGELADO = "2026-08-14 19:14"
METRIC_VERSION = "f1-v2-bbo-depth"
MIN_FILLS = 500
MIN_EVENTS = 100
MIN_DAYS = 30
BOOTSTRAP_SAMPLES = 5_000

REVENUE_BROAD_BASED = 0.82
REVENUE_SINGLE_NAME = 1.91


@dataclass(frozen=True, slots=True)
class Row:
    ticker: str
    event_ticker: str
    created_at: str
    count: int
    markout2: float
    fee_effective: int

    @property
    def net_per_contract(self) -> float:
        return self.markout2 - self.fee_effective / self.count

    @property
    def net_cents(self) -> float:
        return self.markout2 * self.count - self.fee_effective

    @property
    def event_key(self) -> str:
        # Identidad oficial transportada por GET /events. Un evento puede tener muchos
        # market tickers; derivarlo con rsplit infla falsamente el n independiente.
        return self.event_ticker

    @property
    def day(self) -> str:
        return self.created_at[:10]


@dataclass(frozen=True, slots=True)
class GateStats:
    fills: int
    events: int
    days: int
    mean_net: float | None
    lcb95: float | None
    median_daily: float | None
    max_event_pnl_share: float | None
    max_event_fill_share: float | None


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:+.3f}¢"


def _columns(con: sqlite3.Connection) -> set[str]:
    return {row[1] for row in con.execute("pragma table_info(mm_shadow_fills)")}


def _run_integrity(con: sqlite3.Connection, experiment_id: str | None) -> tuple[int, str]:
    """Una cohorte solo cierra si todas sus épocas terminaron explícitamente clean."""
    if experiment_id is None:
        return 0, "sin cohorte"
    exists = con.execute(
        "select 1 from sqlite_master where type='table' and name='mm_experiment_runs'"
    ).fetchone()
    if exists is None:
        return 1, "sin cadena de custodia"
    rows = con.execute(
        "select status, ended_at, reason from mm_experiment_runs where experiment_id = ?",
        (experiment_id,),
    ).fetchall()
    if not rows:
        return 1, "sin runs registrados"
    invalid_rows = [
        (status, reason)
        for status, ended_at, reason in rows
        if status != "clean" or ended_at is None
    ]
    invalid = len(invalid_rows)
    clean = len(rows) - invalid
    details = ", ".join(
        f"{status}:{(reason or 'sin cierre')[:80]}" for status, reason in invalid_rows[:3]
    )
    suffix = f" [{details}]" if details else ""
    return invalid, f"clean={clean} no-clean={invalid}{suffix}"


def _parse_dt(raw: str) -> datetime:
    value = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _legacy_rows(con: sqlite3.Connection, columns: set[str]) -> list[Row]:
    """Recalcula fees históricas por fecha; estas filas son diagnóstico, no gate."""
    metric_filter = (
        " and (metric_version is null or metric_version != ?)"
        if "metric_version" in columns
        else ""
    )
    params = (CORTE_MARK_CONGELADO, METRIC_VERSION) if metric_filter else (CORTE_MARK_CONGELADO,)
    rows = con.execute(
        "select ticker, created_at, count, markout2_cents, price_cents "
        "from mm_shadow_fills where created_at >= ? and markout2_cents is not null" + metric_filter,
        params,
    ).fetchall()
    result: list[Row] = []
    for ticker, created_at, count, markout2, price in rows:
        if not ticker or not count or count < 1:
            continue
        multiplier = maker_fee_multiplier_for_ticker(ticker, as_of=_parse_dt(created_at))
        fee = kalshi_maker_fee_cents(count, price, fee_multiplier=multiplier)
        # Legacy no trae event_ticker. La derivación solo alimenta el diagnóstico y
        # nunca puede graduar F1-v2.
        result.append(Row(ticker, ticker.rsplit("-", 1)[0], created_at, count, markout2, fee))
    return result


def _v2_rows(
    con: sqlite3.Connection, columns: set[str], experiment_id: str | None
) -> tuple[str | None, list[Row], int]:
    required = {
        "metric_version",
        "experiment_id",
        "event_ticker",
        "fee_multiplier",
        "fee_type",
        "fee_source",
        "fee_base_multiplier",
        "fee_base_type",
        "fee_override_multiplier",
        "fee_override_type",
        "fee_schedule_observed_at",
        "fee_effective_cents",
        "fee_model",
        "markout2_age_sec",
        "commence_time",
        "seconds_to_kickoff",
        "policy_require_pregame",
        "policy_kickoff_buffer_sec",
        "quote_seconds_to_kickoff",
        "yes_bid",
        "yes_ask",
        "crossed_depth",
        "side",
        "rule",
        "exchange_environment",
        "fair_method_version",
        "fair_odds_event_id",
        "fair_sport_key",
        "fair_bookmaker_keys",
        "fair_book_count",
        "fair_oldest_book_update",
        "fair_newest_book_update",
        "fair_min_books",
        "fair_max_book_age_min",
        "fair_computed_at",
        "fair_odds_regions",
        "fair_sport_keys_config",
        "fair_cache_ttl_sec",
        "fair_ttl_sec",
    }
    if not required.issubset(columns):
        return None, [], 0
    selected = experiment_id
    if selected is None:
        latest = con.execute(
            "select experiment_id from mm_shadow_fills where metric_version = ? "
            "and experiment_id is not null order by created_at desc, id desc limit 1",
            (METRIC_VERSION,),
        ).fetchone()
        selected = latest[0] if latest else None
    if selected is None:
        return None, [], 0
    rows = con.execute(
        "select ticker, event_ticker, created_at, count, price_cents, markout2_cents, "
        "markout2_age_sec, "
        "fee_effective_cents, fee_model, "
        "fee_multiplier, fee_type, fee_source, fee_base_multiplier, fee_base_type, "
        "fee_override_multiplier, fee_override_type, fee_schedule_observed_at, "
        "exchange_environment, fair_method_version, fair_odds_event_id, fair_sport_key, "
        "fair_bookmaker_keys, fair_book_count, fair_oldest_book_update, "
        "fair_newest_book_update, fair_min_books, fair_max_book_age_min, fair_computed_at, "
        "fair_odds_regions, fair_sport_keys_config, fair_cache_ttl_sec, fair_ttl_sec, "
        "commence_time, "
        "seconds_to_kickoff, policy_require_pregame, policy_kickoff_buffer_sec, "
        "quote_seconds_to_kickoff, yes_bid, yes_ask, side, rule, crossed_depth "
        "from mm_shadow_fills "
        "where metric_version = ? and experiment_id = ?",
        (METRIC_VERSION, selected),
    ).fetchall()
    result: list[Row] = []
    violations = 0
    series: set[str] = set()
    buffers: set[float] = set()
    event_policies: dict[str, set[tuple[object, ...]]] = defaultdict(set)
    fair_policies: set[tuple[object, ...]] = set()
    for (
        ticker,
        event_ticker,
        created_at,
        count,
        price,
        markout2,
        markout2_age,
        fee,
        fee_model,
        multiplier,
        fee_type,
        fee_source,
        base_multiplier,
        base_type,
        override_multiplier,
        override_type,
        fee_observed_at,
        exchange_environment,
        fair_method_version,
        fair_odds_event_id,
        fair_sport_key,
        fair_bookmaker_keys,
        fair_book_count,
        fair_oldest_book_update,
        fair_newest_book_update,
        fair_min_books,
        fair_max_book_age_min,
        fair_computed_at,
        fair_odds_regions,
        fair_sport_keys_config,
        fair_cache_ttl_sec,
        fair_ttl_sec,
        commence_time,
        fill_seconds,
        require_pregame,
        buffer_sec,
        quote_seconds,
        yes_bid,
        yes_ask,
        side,
        rule,
        crossed_depth,
    ) in rows:
        if ticker:
            series.add(ticker.split("-", 1)[0].upper())
        if buffer_sec is not None:
            buffers.add(float(buffer_sec))
        try:
            expected_fee = kalshi_maker_fee_cents(count, price, fee_multiplier=multiplier)
        except (TypeError, ValueError):
            expected_fee = None
        bbo_valid = (
            isinstance(yes_bid, int)
            and isinstance(yes_ask, int)
            and 0 <= yes_bid <= 99
            and 1 <= yes_ask <= 100
            and yes_bid < yes_ask
        )
        fill_evidence_valid = (
            (side == "buy" and yes_ask < price and rule == f"ask {yes_ask} < bid {price}")
            or (side == "sell" and yes_bid > price and rule == f"bid {yes_bid} > ask {price}")
            if bbo_valid and isinstance(price, int)
            else False
        )
        try:
            observed_depth = Decimal(str(crossed_depth))
            depth_valid = (
                observed_depth.is_finite()
                and isinstance(count, int)
                and count >= 1
                and observed_depth >= Decimal(count)
            )
        except (InvalidOperation, TypeError, ValueError):
            depth_valid = False
        source_valid = (
            fee_source == "series"
            and override_multiplier is None
            and override_type is None
            and multiplier == base_multiplier
            and fee_type == base_type
        ) or (
            fee_source == "event_override"
            and override_multiplier is not None
            and override_type == "quadratic_with_maker_fees"
            and multiplier == override_multiplier
            and fee_type == override_type
        )
        try:
            created_dt = _parse_dt(created_at)
            oldest_dt = _parse_dt(fair_oldest_book_update)
            newest_dt = _parse_dt(fair_newest_book_update)
            computed_dt = _parse_dt(fair_computed_at)
            book_keys = tuple(
                value.strip() for value in str(fair_bookmaker_keys).split(",") if value.strip()
            )
            configured_sports = {
                value.strip() for value in str(fair_sport_keys_config).split(",") if value.strip()
            }
            fair_age_sec = (created_dt - computed_dt).total_seconds()
            oldest_age_min = (created_dt - oldest_dt).total_seconds() / 60.0
            provenance_valid = (
                fair_method_version == FAIR_METHOD_VERSION
                and bool(fair_odds_event_id)
                and bool(fair_sport_key)
                and fair_sport_key in configured_sports
                and fair_book_count == len(set(book_keys))
                and fair_min_books is not None
                and int(fair_min_books) >= 1
                and fair_book_count is not None
                and int(fair_book_count) >= int(fair_min_books)
                and fair_max_book_age_min is not None
                and float(fair_max_book_age_min) > 0
                and oldest_dt <= newest_dt <= created_dt
                and 0 <= oldest_age_min <= float(fair_max_book_age_min)
                and fair_cache_ttl_sec is not None
                and float(fair_cache_ttl_sec) > 0
                and fair_ttl_sec is not None
                and float(fair_ttl_sec) > 0
                and 0 <= fair_age_sec <= float(fair_ttl_sec)
                and bool(str(fair_odds_regions).strip())
            )
        except (TypeError, ValueError):
            provenance_valid = False
        valid = (
            bool(ticker)
            and bool(event_ticker)
            and ticker.startswith(f"{event_ticker}-")
            and count == 1  # F1-v2 solo demuestra un contrato observable por cruce BBO
            and isinstance(price, int)
            and 1 <= price <= 99
            and bbo_valid
            and fill_evidence_valid
            and depth_valid
            and fee is not None
            and fee == expected_fee
            and fee_model == "maker"
            and multiplier is not None
            and float(multiplier) > 0
            and fee_type == "quadratic_with_maker_fees"
            and base_multiplier is not None
            and float(base_multiplier) > 0
            and base_type == "quadratic_with_maker_fees"
            and source_valid
            and fee_observed_at is not None
            and exchange_environment == "production"
            and provenance_valid
            and commence_time is not None
            and require_pregame in (1, True)
            and buffer_sec is not None
            and float(buffer_sec) >= 0
            and quote_seconds is not None
            and float(quote_seconds) >= float(buffer_sec)
            and fill_seconds is not None
            and float(fill_seconds) >= float(buffer_sec)
            and markout2_age is not None
            and 300.0 <= float(markout2_age) <= 600.0
        )
        if not valid:
            violations += 1
            continue
        if markout2 is None:
            # No se infiere que los faltantes se parezcan a los completos: eso es exactamente
            # el survivorship bias que puede convertir 500 buenos + 500 NULL en un PASS falso.
            # Para el veredicto final se detiene F1 y se espera a que no quede ninguno pendiente.
            violations += 1
            continue
        try:
            markout_value = float(markout2)
            if side == "buy":
                lower_markout, upper_markout = 0.5 - price, 99.5 - price
            else:
                lower_markout, upper_markout = price - 99.5, price - 0.5
            markout_valid = (
                math.isfinite(markout_value)
                and lower_markout <= markout_value <= upper_markout
                # El mid de dos precios enteros en centavos solo cae en pasos de 0.5¢.
                and math.isclose(markout_value * 2, round(markout_value * 2), abs_tol=1e-9)
            )
        except (TypeError, ValueError):
            markout_valid = False
        if not markout_valid:
            violations += 1
            continue
        event_policies[event_ticker].add(
            (
                fee_source,
                float(base_multiplier),
                base_type,
                float(override_multiplier) if override_multiplier is not None else None,
                override_type,
                float(multiplier),
                fee_type,
            )
        )
        fair_policies.add(
            (
                fair_method_version,
                fair_min_books,
                fair_max_book_age_min,
                fair_odds_regions,
                fair_sport_keys_config,
                fair_cache_ttl_sec,
                fair_ttl_sec,
            )
        )
        result.append(Row(ticker, event_ticker, created_at, count, markout_value, fee))
    # Una cohorte significa una serie y una política. Distintos eventos SÍ pueden tener
    # overrides efectivos distintos; lo corrupto es que UN MISMO evento cambie de
    # procedencia/schedule sin una transición temporal demostrable en la fila.
    inconsistent_events = sum(1 for policies in event_policies.values() if len(policies) > 1)
    if len(series) > 1 or len(buffers) > 1 or len(fair_policies) > 1:
        violations += 1
    violations += inconsistent_events
    return selected, result, violations


def _stats(rows: list[Row]) -> GateStats:
    if not rows:
        return GateStats(0, 0, 0, None, None, None, None, None)

    by_event: dict[str, list[Row]] = defaultdict(list)
    by_day: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        by_event[row.event_key].append(row)
        by_day[row.day].append(row)

    total_count = sum(row.count for row in rows)
    mean_net = sum(row.net_cents for row in rows) / total_count

    def cluster_totals(groups: dict[str, list[Row]]) -> list[tuple[float, int]]:
        return [
            (sum(row.net_cents for row in group), sum(row.count for row in group))
            for group in groups.values()
        ]

    def bootstrap_lcb(clusters: list[tuple[float, int]], *, seed: int) -> float | None:
        if len(clusters) < 2:
            return None
        rng = random.Random(seed)
        estimates: list[float] = []
        for _ in range(BOOTSTRAP_SAMPLES):
            sampled = rng.choices(clusters, k=len(clusters))
            sampled_count = sum(count for _, count in sampled)
            estimates.append(sum(net for net, _ in sampled) / sampled_count)
        estimates.sort()
        return estimates[int(0.05 * (len(estimates) - 1))]

    event_totals = cluster_totals(by_event)
    day_totals = cluster_totals(by_day)
    lcbs = [
        value
        for value in (
            bootstrap_lcb(event_totals, seed=5),
            bootstrap_lcb(day_totals, seed=50),
        )
        if value is not None
    ]
    lcb = min(lcbs) if lcbs else None
    daily_means = [net / count for net, count in day_totals]

    absolute_total = sum(abs(net) for net, _ in event_totals)
    max_pnl_share = (
        max(abs(net) for net, _ in event_totals) / absolute_total if absolute_total else 0.0
    )
    max_fill_share = max(count for _, count in event_totals) / total_count
    return GateStats(
        fills=len(rows),
        events=len(by_event),
        days=len(daily_means),
        mean_net=mean_net,
        lcb95=lcb,
        median_daily=statistics.median(daily_means),
        max_event_pnl_share=max_pnl_share,
        max_event_fill_share=max_fill_share,
    )


def margen_teorico(
    precio_cents: int,
    revenue: float = REVENUE_BROAD_BASED,
    *,
    fee_multiplier: float = 0.5,
) -> float:
    """Techo continuo por contrato, antes de selección adversa y del ceil por orden."""
    probability = precio_cents / 100
    maker_round_trip = 2 * 1.75 * fee_multiplier * probability * (1 - probability)
    return revenue - maker_round_trip


def zona_muerta(
    revenue: float = REVENUE_BROAD_BASED, *, fee_multiplier: float = 0.5
) -> tuple[int, int] | None:
    muertos = [
        price
        for price in range(1, 100)
        if margen_teorico(price, revenue, fee_multiplier=fee_multiplier) <= 0
    ]
    return (min(muertos), max(muertos)) if muertos else None


def tablero(
    db_path: str, half_spread: int | None = None, *, experiment_id: str | None = None
) -> str:
    del half_spread  # compatibilidad CLI: el spread ya está incorporado en fill_price.
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        columns = _columns(con)
        legacy = _legacy_rows(con, columns)
        selected_experiment, v2, policy_violations = _v2_rows(con, columns, experiment_id)
        run_violations, run_summary = _run_integrity(con, selected_experiment)
    finally:
        con.close()
    policy_violations += run_violations

    out: list[str] = ["MOTOR 5 — GATE ECONÓMICO F1-v2 (READ-ONLY)"]
    if legacy:
        legacy_stats = _stats(legacy)
        out.extend(
            [
                "",
                "COHORTE LEGACY — SOLO DIAGNÓSTICO, NO PUEDE GRADUAR:",
                f"  fills con T+5m={legacy_stats.fills} | eventos={legacy_stats.events} "
                f"| días={legacy_stats.days}",
                f"  net markout corregido/contrato={_fmt(legacy_stats.mean_net)}",
                "  contaminaciones conocidas: fair como mark y fill completo sin profundidad",
            ]
        )

    stats = _stats(v2)
    out.extend(
        [
            "",
            f"COHORTE VÁLIDA {METRIC_VERSION}:",
            f"  experiment_id={selected_experiment or '—'}",
            f"  fills={stats.fills}/{MIN_FILLS} | eventos={stats.events}/{MIN_EVENTS} "
            f"| días={stats.days}/{MIN_DAYS}",
            f"  media neta/contrato={_fmt(stats.mean_net)} | LCB95 block-bootstrap="
            f"{_fmt(stats.lcb95)}",
            f"  mediana diaria={_fmt(stats.median_daily)} | concentración fills/evento="
            + ("—" if stats.max_event_fill_share is None else f"{stats.max_event_fill_share:.1%}"),
            "  concentración abs(PnL)/evento (diagnóstico)="
            + ("—" if stats.max_event_pnl_share is None else f"{stats.max_event_pnl_share:.1%}"),
            f"  violaciones de política/metadata={policy_violations}",
            f"  cadena de custodia: {run_summary}",
        ]
    )

    enough = stats.fills >= MIN_FILLS and stats.events >= MIN_EVENTS and stats.days >= MIN_DAYS
    economics = (
        stats.lcb95 is not None
        and stats.lcb95 > 0
        and stats.median_daily is not None
        and stats.median_daily > 0
        and stats.max_event_fill_share is not None
        and stats.max_event_fill_share <= 0.20
        and policy_violations == 0
    )
    if policy_violations:
        out.append(
            "VEREDICTO: FALLA VALIDEZ DE POLÍTICA — hay fills off-policy/ambiguos o "
            "metadata incoherente; F2/F3 bloqueados."
        )
    elif stats.fills == 0:
        out.append("VEREDICTO: SIN DATOS F1-v2 — M5 permanece shadow; F2/F3 bloqueados.")
    elif stats.mean_net is not None and stats.mean_net <= 0:
        out.append("VEREDICTO: FALLA ECONOMÍA — continuar shadow o rediseñar; F2/F3 bloqueados.")
    elif not enough:
        out.append("VEREDICTO: SIN DATOS SUFICIENTES — no se gradúa por un promedio pequeño.")
    elif economics and experiment_id is None:
        out.append(
            "VEREDICTO: REQUIERE --experiment-id explícito — la cohorte más reciente "
            "puede ser una anterior si el deploy activo aún tiene 0 fills."
        )
    elif economics:
        out.append(
            "VEREDICTO: PASA F1-v2 — habilita revisión F2 con tape/queue; "
            "NO autoriza producción ni capital."
        )
    else:
        out.append("VEREDICTO: FALLA GATE — LCB/mediana/concentración no cumplen.")
    return "\n".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="/app/data/trades.db")
    parser.add_argument("--half-spread", type=int, default=None, help="obsoleto; compatibilidad")
    parser.add_argument("--experiment-id", default=None, help="default: cohorte más reciente")
    args = parser.parse_args()
    print(tablero(args.db, args.half_spread, experiment_id=args.experiment_id))


if __name__ == "__main__":
    main()
