"""
Veredicto read-only de los shadows que buscan edge FUERA del pre-match (M8 OFI, M6 line-move).

POR QUÉ EXISTE (2026-07-17): el pre-match contra el consenso de sportsbooks (Motor 2) está
medido y es EFICIENTE a esta escala — con el universo ampliado (30 eventos, 26 matcheados) el
best_edge vive en ~0.2pp contra un umbral de 3pp. La pregunta ya no es "por qué M2 no opera"
(respondida: no hay edge pre-match), sino "¿dónde SÍ hay edge?". Los dos candidatos vivos son
la microestructura: el FLUJO (M8 OFI) y los SALTOS de línea (M6). Este script lee lo que sus
shadows ya persistieron y da un veredicto por motor, sin más intuición.

`report_edge_windows.py` cubre el Motor REST (kind binary/multi_outcome); este cubre el hueco
de kind='ofi', kind='linemove' y kind='spillover' (M9, derrame entre hermanos — 2026-07-18).
Read-only: solo lee edge_windows. NO toca capital ni red.

QUÉ MIDE CADA UNO (asimetría IMPORTANTE — no confundir señal con resultado):
  - M8 OFI es AUTO-VALIDANTE: cada fila trae el movimiento REAL del precio DESPUÉS de la
    señal, firmado desde la presión (gross_spread_cents=move a T+30s, magnitude_cents=T+60s;
    >0 = el precio SIGUIÓ la presión = momentum; <0 = revirtió = adverse/contrarian). O sea:
    la DB ya sabe si el flujo predice. Veredicto REAL de la tesis.
  - M6 line-move NO auto-valida el resultado: cada fila es el edge DETECTADO (net_edge_pp),
    no si el precio efectivamente lo confirmó. Este script solo DIMENSIONA (frecuencia +
    magnitud de señal). El ROI verdadero exige cruzar con settlements → fase siguiente,
    fuera de este script (se dice explícito, no se finge).

Uso (dentro del container Coolify):
    python scripts/diag_edge_shadow.py                 # ventana default 14 días
    python scripts/diag_edge_shadow.py --hours 72      # últimas 72h
"""

from __future__ import annotations

import argparse
import math
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlmodel import col, select

from src.storage.models import EdgeWindow, get_session

# Mínimo de señales para arriesgar un veredicto direccional (debajo = "acumulando").
# Con menos de esto, la media de un flujo ruidoso es indistinguible de suerte.
_MIN_SIGNALS_FOR_VERDICT = 20
# Umbral de move medio (¢) bajo el cual el flujo NO predice nada útil, aunque sea significativo.
_NOISE_BAND_CENTS = 1.0


@dataclass(frozen=True, slots=True)
class OfiVerdict:
    n: int
    move30_mean: float
    move60_mean: float
    move60_tstat: float  # significancia de la media de T+60 (|t|>2 ≈ no-ruido)
    pct_move60_positive: float
    verdict: str
    recommendation: str


def _tstat(vals: list[float]) -> float:
    """t de una muestra contra media 0 (mide si el move medio es señal o azar)."""
    if len(vals) < 2:
        return 0.0
    sd = statistics.stdev(vals)
    if sd == 0:
        return math.inf if statistics.fmean(vals) != 0 else 0.0
    return statistics.fmean(vals) / (sd / math.sqrt(len(vals)))


def summarize_ofi(rows: list[EdgeWindow]) -> OfiVerdict:
    """Veredicto direccional del flujo (M8): ¿el precio SIGUE la presión (momentum, edge
    comprable) o REVIERTE contra ella (adverse selection, trampa)? Puro/testeable.

    Los moves ya vienen firmados desde la presión por el shadow: >0 = el precio se movió
    A FAVOR de la presión del OFI. Una media claramente positiva y significativa = la tesis
    de momentum se sostiene; negativa = el flujo es informado EN CONTRA (o adverse selection);
    cerca de 0 o pocos datos = ruido, archivar.
    """
    m30 = [float(r.gross_spread_cents) for r in rows if r.gross_spread_cents is not None]
    m60 = [float(r.magnitude_cents) for r in rows if r.magnitude_cents is not None]
    n = len(m60)
    if n == 0:
        return OfiVerdict(0, 0.0, 0.0, 0.0, 0.0, "SIN DATOS", "El shadow no persistió filas ofi.")
    mean30 = statistics.fmean(m30) if m30 else 0.0
    mean60 = statistics.fmean(m60)
    tstat = _tstat(m60)
    pct_pos = 100.0 * sum(1 for v in m60 if v > 0) / n

    if n < _MIN_SIGNALS_FOR_VERDICT:
        verdict = "ACUMULANDO"
        rec = f"{n}/{_MIN_SIGNALS_FOR_VERDICT} señales — sin muestra para veredicto. Esperar."
    elif abs(mean60) < _NOISE_BAND_CENTS or abs(tstat) < 2.0:
        verdict = "RUIDO → ARCHIVAR"
        rec = (
            f"move60 medio {mean60:+.2f}¢ (t={tstat:.1f}) no se distingue de azar: el flujo "
            "no predice el precio a esta escala. Archivar M8 es resultado válido y barato."
        )
    elif mean60 > 0:
        verdict = "MOMENTUM (edge potencial)"
        rec = (
            f"el precio SIGUE la presión: move60 medio {mean60:+.2f}¢ (t={tstat:.1f}, "
            f"{pct_pos:.0f}% positivos). Candidato a F2: simular ROI post-fee comprando la "
            "dirección del OFI. OJO reservas: books finos y flujo informado."
        )
    else:
        verdict = "REVERSIÓN / ADVERSE (contrarian)"
        rec = (
            f"el precio REVIERTE contra la presión: move60 medio {mean60:+.2f}¢ (t={tstat:.1f}). "
            "El flujo es informado EN CONTRA — seguir el OFI perdería. Explorar contrarian con "
            "MUCHA cautela (adverse selection es exactamente esto), o archivar."
        )
    return OfiVerdict(n, mean30, mean60, tstat, pct_pos, verdict, rec)


def summarize_spillover(rows: list[EdgeWindow]) -> OfiVerdict:
    """Veredicto del derrame (M9): mismo SHAPE de datos que OFI (follow-through firmado en
    gross_spread=T+60 / magnitude=T+120) y misma matemática (media + t-stat), pero con su
    semántica propia — acá el follow está firmado desde la dirección ESPERADA (inversa del
    salto), así que >0 = el hermano ajustó DESPUÉS del trigger (rezago capturable)."""
    m60_rows = [float(r.gross_spread_cents) for r in rows if r.gross_spread_cents is not None]
    m120 = [float(r.magnitude_cents) for r in rows if r.magnitude_cents is not None]
    n = len(m60_rows)
    if n == 0:
        return OfiVerdict(
            0, 0.0, 0.0, 0.0, 0.0, "SIN DATOS", "El shadow no persistió filas spillover."
        )
    mean60 = statistics.fmean(m60_rows)
    mean120 = statistics.fmean(m120) if m120 else 0.0
    tstat = _tstat(m60_rows)
    pct_pos = 100.0 * sum(1 for v in m60_rows if v > 0) / n

    if n < _MIN_SIGNALS_FOR_VERDICT:
        verdict = "ACUMULANDO"
        rec = f"{n}/{_MIN_SIGNALS_FOR_VERDICT} mediciones — sin muestra para veredicto. Esperar."
    elif abs(mean60) < _NOISE_BAND_CENTS or abs(tstat) < 2.0:
        verdict = "AJUSTE INSTANTÁNEO / SIN PROPAGACIÓN → ARCHIVAR"
        rec = (
            f"follow60 medio {mean60:+.2f}¢ (t={tstat:.1f}): el hermano no se mueve (o ya se "
            "movió ANTES del trigger). No hay rezago capturable. Archivar M9 es válido y barato."
        )
    elif mean60 > 0:
        verdict = "DERRAME REZAGADO (edge potencial)"
        rec = (
            f"el hermano ajusta DESPUÉS del trigger: follow60 medio {mean60:+.2f}¢ "
            f"(t={tstat:.1f}, {pct_pos:.0f}% positivos). Candidato a F2 completo: verificar "
            "contra el ASK (no el mid — lección del REST: detectado ≠ capturable) y fees."
        )
    else:
        verdict = "TESIS INVERTIDA"
        rec = (
            f"el hermano se mueve AL REVÉS de lo esperado: follow60 medio {mean60:+.2f}¢ "
            f"(t={tstat:.1f}). Probable: el 'hermano' LIDERA y el trigger es el eco. NO operar "
            "contrarian sin re-derivar el mecanismo."
        )
    return OfiVerdict(n, mean60, mean120, tstat, pct_pos, verdict, rec)


def summarize_spillover_exec(rows: list[EdgeWindow]) -> OfiVerdict:
    """Veredicto F2 del derrame EJECUTABLE (kind='spillover_exec', 2026-07-28): a diferencia
    del mid, acá gross_spread = bid_salida(T+60) − ask_entrada(T0) y magnitude = NETO por
    contrato tras fee de ida y vuelta a count real. Es el número que decide F3: el mid puede
    dar +2.69¢ y este dar ≤0 si el spread se lo come (exactamente donde murió REST arb)."""
    gross = [float(r.gross_spread_cents) for r in rows if r.gross_spread_cents is not None]
    net = [float(r.magnitude_cents) for r in rows if r.magnitude_cents is not None]
    n = len(net)
    if n == 0:
        return OfiVerdict(
            0,
            0.0,
            0.0,
            0.0,
            0.0,
            "SIN DATOS",
            "Sin filas spillover_exec: el shadow F2 aún no midió puntas ejecutables "
            "(requiere deploy del quote_fn + books sanos con ambas puntas).",
        )
    mean_gross = statistics.fmean(gross) if gross else 0.0
    mean_net = statistics.fmean(net)
    stdev = statistics.stdev(net) if n > 1 else 0.0
    tstat = (mean_net / (stdev / n**0.5)) if stdev > 0 else 0.0
    pct_pos = 100.0 * sum(1 for v in net if v > 0) / n
    if n < _MIN_SIGNALS_FOR_VERDICT:
        verdict = "ACUMULANDO"
        rec = f"{n}/{_MIN_SIGNALS_FOR_VERDICT} mediciones ejecutables — esperar."
    elif mean_net > 0 and tstat >= 2.0:
        verdict = "CAPTURABLE AL ASK (gate F2 VERDE)"
        rec = (
            f"neto medio {mean_net:+.2f}¢/contrato post-fees roundtrip (t={tstat:.1f}, "
            f"{pct_pos:.0f}% positivos; bruto ejecutable {mean_gross:+.2f}¢). Discutir F3 "
            "con el operador: Capa A doble-flag, canary, one-per-event — y capital."
        )
    else:
        verdict = "EL SPREAD SE LO COME → ARCHIVAR"
        rec = (
            f"neto medio {mean_net:+.2f}¢ (t={tstat:.1f}; bruto ejecutable {mean_gross:+.2f}¢): "
            "el edge del mid no sobrevive al ask+fees. Mismo final que REST arb: detectado ≠ "
            "capturable. Archivar M9 es un resultado válido y barato."
        )
    return OfiVerdict(n, mean_gross, mean_net, tstat, pct_pos, verdict, rec)


@dataclass(frozen=True, slots=True)
class LinemoveSummary:
    n: int
    edge_pp: list[float]
    verdict: str
    recommendation: str


def summarize_linemove(rows: list[EdgeWindow]) -> LinemoveSummary:
    """Dimensiona las señales de line-move (M6): cuántas y de qué tamaño. NO es veredicto de
    rentabilidad — M6 registra el edge DETECTADO, no si el precio lo confirmó. El ROI real
    exige cruzar cada señal con su settlement (fase siguiente, no acá)."""
    edges = [float(r.edge_pct) for r in rows if r.edge_pct is not None]
    n = len(edges)
    if n == 0:
        verdict = "SIN SEÑAL"
        rec = (
            "0 filas linemove. Si M6 arrancó recién, es esperado (necesita 2 fotos del fair "
            "con delta ≥3pp cerca de un kickoff). Si pasan días con partidos y sigue en 0, "
            "la tesis del line-move no produce a esta cadencia — archivar es válido."
        )
    else:
        verdict = f"{n} SEÑALES DETECTADAS"
        rec = (
            f"M6 detecta line-moves (edge neto medio {statistics.fmean(edges):.1f}pp). Esto "
            "DIMENSIONA la frecuencia, NO la rentabilidad: el próximo paso es cruzar cada "
            "señal con su settlement para el ROI real (M6 no auto-valida el resultado)."
        )
    return LinemoveSummary(n, edges, verdict, rec)


def _print_dist(label: str, vals: list[float], unit: str) -> None:
    if not vals:
        print(f"  {label}: —")
        return
    print(
        f"  {label}: n={len(vals)} min={min(vals):+.2f}{unit} "
        f"med={statistics.median(vals):+.2f}{unit} media={statistics.fmean(vals):+.2f}{unit} "
        f"max={max(vals):+.2f}{unit}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=336, help="ventana hacia atrás (default 14 días)")
    args = ap.parse_args()
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=args.hours)

    with get_session() as s:
        rows = list(
            s.exec(
                select(EdgeWindow).where(
                    col(EdgeWindow.kind).in_(["ofi", "linemove", "spillover", "spillover_exec"]),
                    col(EdgeWindow.created_at) >= since,
                )
            )
        )
    ofi_rows = [r for r in rows if r.kind == "ofi"]
    lm_rows = [r for r in rows if r.kind == "linemove"]
    sp_rows = [r for r in rows if r.kind == "spillover"]
    spx_rows = [r for r in rows if r.kind == "spillover_exec"]

    print(f"{'=' * 68}\nVEREDICTO DE EDGE SHADOW · últimas {args.hours}h · {len(rows)} filas")
    print("Pregunta: si el pre-match está eficiente, ¿hay edge en la microestructura?\n")

    print(f"{'-' * 68}\nMOTOR 8 · OFI (flujo) — AUTO-VALIDANTE, mide el precio real post-señal")
    ofi = summarize_ofi(ofi_rows)
    _print_dist(
        "move T+30s (firmado)",
        [float(r.gross_spread_cents) for r in ofi_rows if r.gross_spread_cents is not None],
        "¢",
    )
    _print_dist(
        "move T+60s (firmado)",
        [float(r.magnitude_cents) for r in ofi_rows if r.magnitude_cents is not None],
        "¢",
    )
    if ofi.n:
        print(
            f"  positivos a T+60: {ofi.pct_move60_positive:.0f}%  ·  t-stat: {ofi.move60_tstat:.1f}"
        )
    print(f"  → VEREDICTO: {ofi.verdict}\n    {ofi.recommendation}")

    print(
        f"\n{'-' * 68}\nMOTOR 9 · DERRAME — AUTO-VALIDANTE, mide el follow del hermano "
        "post-trigger (firmado desde la dirección esperada)"
    )
    sp = summarize_spillover(sp_rows)
    _print_dist(
        "follow hermano T+60 (firmado)",
        [float(r.gross_spread_cents) for r in sp_rows if r.gross_spread_cents is not None],
        "¢",
    )
    _print_dist(
        "follow hermano T+120 (firmado)",
        [float(r.magnitude_cents) for r in sp_rows if r.magnitude_cents is not None],
        "¢",
    )
    if sp.n:
        print(
            f"  positivos a T+60: {sp.pct_move60_positive:.0f}%  ·  t-stat: {sp.move60_tstat:.1f}"
        )
    print(f"  → VEREDICTO: {sp.verdict}\n    {sp.recommendation}")

    print(
        f"\n{'-' * 68}\nMOTOR 9 · DERRAME EJECUTABLE (F2) — bid(T+60) − ask(T0) − fees "
        "roundtrip (el número que decide F3)"
    )
    spx = summarize_spillover_exec(spx_rows)
    _print_dist(
        "bruto ejecutable (bid60−ask0)",
        [float(r.gross_spread_cents) for r in spx_rows if r.gross_spread_cents is not None],
        "¢",
    )
    _print_dist(
        "NETO post-fees por contrato",
        [float(r.magnitude_cents) for r in spx_rows if r.magnitude_cents is not None],
        "¢",
    )
    if spx.n:
        print(
            f"  positivos netos: {spx.pct_move60_positive:.0f}%  ·  t-stat: {spx.move60_tstat:.1f}"
        )
    print(f"  → VEREDICTO: {spx.verdict}\n    {spx.recommendation}")

    print(f"\n{'-' * 68}\nMOTOR 6 · LINE-MOVE — dimensiona señal (el ROI real es fase siguiente)")
    lm = summarize_linemove(lm_rows)
    _print_dist("edge neto detectado", lm.edge_pp, "pp")
    print(f"  → VEREDICTO: {lm.verdict}\n    {lm.recommendation}")

    print(
        f"\n{'=' * 68}\nLECTURA: M8 MOMENTUM o M9 DERRAME REZAGADO con significancia = el edge "
        "está en la\nmicroestructura (candidato a F2). Todo en RUIDO/INSTANTÁNEO tras días de "
        "muestra = la\nrespuesta honesta es que no hay edge microestructural a esta escala — y "
        "eso también\nes un resultado.\n"
    )


if __name__ == "__main__":
    main()
