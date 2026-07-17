"""
Re-cálculo del shadow HISTÓRICO con la fee corregida (radar post-fix 0dbf9b7, 2026-07-01).

El bug: kalshi_fee_cents dividía por 1_000_000 en vez de 10_000 (~100× subestimado).
Todo el shadow grabado ANTES del fix (EdgeWindow: consensus/binary/multi_outcome) tiene
el edge NETO sobreestimado — y las decisiones de canary se apoyan en ese histórico.

LÍMITE HONESTO: las ventanas viejas NO grabaron los precios por pata, así que la fee
correcta exacta es irrecuperable. Lo que SÍ es recuperable:

  - consensus (¢/contrato): la fee vieja era ≡1¢ para TODO precio 1-99 (el numerador
    máximo 17_500 dividido por 1e6 → ceil = 1). La correcta es 1¢ (ask ≤17 o ≥83) o 2¢
    (ask 18-82). Corrección EXACTA acotada: neto_corregido ∈ {magnitude−1, magnitude}.
  - binary (total, count contratos): gross = (100−yes−no)·count → la SUMA de precios
    s = 100 − gross/count es exacta. La fee correcta se acota escaneando TODAS las
    particiones p1+p2=s (98 evaluaciones) → [fee_min, fee_max] exactos.
  - multi_outcome: como binary pero con n patas desconocido → cotas por distribución
    extrema (concentrada = mínimo, reparto uniforme = máximo) sobre n ∈ [2, max_legs].

El veredicto por fila usa la cota CONSERVADORA (fee máxima): si el neto corregido
conservador sigue > umbral, la ventana histórica era real incluso en el peor caso.

Read-only: este módulo no escribe nada; el CLI (scripts/recompute_shadow_fees.py) tampoco.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from src.math.fees import kalshi_fee_cents

# Fix de fees: commit 0dbf9b7 (2026-07-01T21:02:37Z). Filas creadas DESPUÉS ya son
# correctas. Para consensus el discriminador primario es fees_cents IS NOT NULL (el
# desglose gross/fee se deployó junto con el fix); para binary/multi (campos que ya
# existían desde 2026-06) el discriminador es este cutover, overrideable en el CLI.
FEE_FIX_AT_DEFAULT = datetime(2026, 7, 1, 21, 2, 37)  # naive UTC (convención DB)


def old_fee_cents(count: int, price_cents: int) -> int:
    """La fórmula BUGGY pineada (fossil pre-0dbf9b7): denominador 1_000_000.
    Solo para reconstruir qué fee se descontó en las ventanas históricas."""
    numerator = 7 * count * price_cents * (100 - price_cents)
    return (numerator + 999_999) // 1_000_000


def consensus_corrected_bounds(magnitude_cents: int) -> tuple[int, int]:
    """Neto corregido (¢/contrato) de una fila consensus pre-fix: [conservador, optimista].

    magnitude = fair·100 − ask − fee_vieja, con fee_vieja ≡ 1¢. La correcta es 1 o 2¢
    según el ask (no grabado) → corregido = magnitude + 1 − {1,2} = {magnitude−1, magnitude}.
    """
    return magnitude_cents - 1, magnitude_cents


def binary_fee_bounds(count: int, sum_prices_cents: int) -> tuple[int, int]:
    """[fee_min, fee_max] EXACTOS (¢ totales, ambas patas) de un arb binario con
    yes+no = sum_prices_cents: escanea todas las particiones válidas p ∈ [1,99]."""
    s = sum_prices_cents
    lo = max(1, s - 99)
    hi = min(99, s - 1)
    if lo > hi:
        raise ValueError(f"suma de precios sin partición válida: {s}")
    fees = [kalshi_fee_cents(count, p) + kalshi_fee_cents(count, s - p) for p in range(lo, hi + 1)]
    return min(fees), max(fees)


def multi_fee_bounds(count: int, sum_prices_cents: int, *, max_legs: int = 8) -> tuple[int, int]:
    """[fee_min, fee_max] (¢ totales) de un arb multi-outcome con Σasks = sum_prices_cents
    y n patas DESCONOCIDO (n ∈ [2, max_legs], cada ask en [1,99]).

    p(100−p) es cóncavo → dado Σp fijo, la fee total es MÍNIMA concentrando (una pata
    con casi todo, el resto a 1¢) y MÁXIMA repartiendo uniforme. Se evalúan ambas
    distribuciones para cada n y se toman los extremos globales. Cota, no exacto:
    el n real no se grabó.
    """
    s = sum_prices_cents
    best_min: int | None = None
    best_max: int | None = None
    for n in range(2, max_legs + 1):
        if s < n:  # n patas a ≥1¢ requieren Σ ≥ n
            break
        big = s - (n - 1)
        if big > 99:
            continue  # concentración imposible con asks ≤ 99 para este n
        concentrated = kalshi_fee_cents(count, big) + (n - 1) * kalshi_fee_cents(count, 1)
        base, extra = divmod(s, n)
        uniform = (n - extra) * kalshi_fee_cents(count, base) + extra * kalshi_fee_cents(
            count, base + 1
        )
        lo_n, hi_n = min(concentrated, uniform), max(concentrated, uniform)
        best_min = lo_n if best_min is None else min(best_min, lo_n)
        best_max = hi_n if best_max is None else max(best_max, hi_n)
    if best_min is None or best_max is None:
        raise ValueError(f"suma de precios sin distribución válida: {s} (max_legs={max_legs})")
    return best_min, best_max


# =====================================================
# Agregación sobre filas EdgeWindow
# =====================================================


@dataclass
class ConsensusRecalc:
    """Población consensus (¢/contrato; umbral = MIN_EDGE en pp)."""

    post_fix: int = 0  # filas con desglose (fee ya correcta) — no se tocan
    pre_fix: int = 0
    pre_kept_conservative: int = 0  # magnitude−1 > umbral (sobreviven en el peor caso)
    pre_kept_optimistic: int = 0  # magnitude > umbral (como estaban grabadas)
    pre_mean_recorded_pp: float = 0.0
    pre_mean_conservative_pp: float = 0.0
    inconsistent_post: int = 0  # post-fix donde |magnitude − (gross − fees)| > 1 (±1 = redondeo)


@dataclass
class ArbRecalc:
    """Población binary/multi_outcome (¢ totales por ventana)."""

    post_fix: int = 0
    pre_fix: int = 0
    pre_recorded_positive: int = 0  # netas > 0 tal como se grabaron
    pre_still_positive_conservative: int = 0  # gross − fee_max > 0 (arb real seguro)
    pre_phantom_conservative: int = 0  # grabada positiva pero ≤ 0 con fee_max (fantasma probable)
    pre_uncorrectable: int = 0  # sin gross/count (pre-2026-06) → irrecuperable
    # Totales SOLO sobre filas corregibles (con gross/count) — comparación manzanas-con-manzanas.
    pre_recorded_net_correctable_cents: int = 0
    pre_corrected_net_conservative_total_cents: int = 0


@dataclass
class ShadowRecalcReport:
    consensus: ConsensusRecalc = field(default_factory=ConsensusRecalc)
    binary: ArbRecalc = field(default_factory=ArbRecalc)
    multi_outcome: ArbRecalc = field(default_factory=ArbRecalc)
    skipped_other_kind: int = 0


def _recalc_consensus(row, agg: ConsensusRecalc, min_edge_pp: float) -> None:
    if row.fees_cents is not None and row.gross_spread_cents is not None:
        agg.post_fix += 1
        # magnitude y gross se redondean en puntos distintos del pipeline → ±1¢ es legítimo.
        if abs(row.magnitude_cents - (row.gross_spread_cents - row.fees_cents)) > 1:
            agg.inconsistent_post += 1
        return
    agg.pre_fix += 1
    conservative, optimistic = consensus_corrected_bounds(row.magnitude_cents)
    if conservative > min_edge_pp:
        agg.pre_kept_conservative += 1
    if optimistic > min_edge_pp:
        agg.pre_kept_optimistic += 1
    # medias incrementales (magnitude está en ¢/contrato ≡ pp)
    n = agg.pre_fix
    agg.pre_mean_recorded_pp += (row.magnitude_cents - agg.pre_mean_recorded_pp) / n
    agg.pre_mean_conservative_pp += (conservative - agg.pre_mean_conservative_pp) / n


def _recalc_arb(row, agg: ArbRecalc, fee_fix_at: datetime, *, is_binary: bool) -> None:
    created = row.created_at.replace(tzinfo=None) if row.created_at.tzinfo else row.created_at
    if created >= fee_fix_at:
        agg.post_fix += 1
        return
    agg.pre_fix += 1
    if row.magnitude_cents > 0:
        agg.pre_recorded_positive += 1
    count = row.count
    gross = row.gross_spread_cents
    if not count or gross is None or gross % count != 0:
        agg.pre_uncorrectable += 1
        return
    sum_prices = 100 - gross // count
    try:
        bounds = (
            binary_fee_bounds(count, sum_prices)
            if is_binary
            else multi_fee_bounds(count, sum_prices)
        )
    except ValueError:
        agg.pre_uncorrectable += 1
        return
    corrected_conservative = gross - bounds[1]  # peor caso: fee máxima
    agg.pre_recorded_net_correctable_cents += row.magnitude_cents
    agg.pre_corrected_net_conservative_total_cents += corrected_conservative
    if corrected_conservative > 0:
        agg.pre_still_positive_conservative += 1
    elif row.magnitude_cents > 0:
        agg.pre_phantom_conservative += 1


def recompute_shadow(
    rows,
    *,
    fee_fix_at: datetime = FEE_FIX_AT_DEFAULT,
    min_edge_pp: float = 3.0,
) -> ShadowRecalcReport:
    """Agrega el re-cálculo sobre un iterable de filas EdgeWindow (o equivalentes).

    Puro: no toca la DB. El caller (CLI) trae las filas y formatea el reporte.
    """
    report = ShadowRecalcReport()
    for row in rows:
        kind = row.kind or "binary"  # NULL = pre-P3, todas eran binary
        if kind == "consensus":
            _recalc_consensus(row, report.consensus, min_edge_pp)
        elif kind == "binary":
            _recalc_arb(row, report.binary, fee_fix_at, is_binary=True)
        elif kind == "multi_outcome":
            _recalc_arb(row, report.multi_outcome, fee_fix_at, is_binary=False)
        else:
            report.skipped_other_kind += 1
    return report
