"""
Tablero READ-ONLY del gate maker (Apuesta 1) — la única lectura autorizada del veredicto.

POR QUÉ EXISTE (2026-08-14): el gate se venía leyendo con queries ad-hoc, y eso ya produjo
una lectura contaminada — el agregado de markout2 (avg −11.09¢ sobre n=11) mezclaba nueve
filas del BUG DEL MARK CONGELADO (#235): fills cuyos dos horizontes midieron contra el mismo
mark viejo, reconocibles porque markout1 == markout2 EXACTO. Un veredicto de plata no puede
depender de que quien consulta se acuerde de filtrar. El filtro vive acá, versionado.

QUÉ FILTRA Y POR QUÉ:
  - Fills anteriores al fix del mark congelado (#235, 2026-08-14 19:14 UTC) → CONTAMINADOS:
    su markout2 no es una observación independiente. Se cuentan aparte, jamás en el agregado.
  - El resto es la muestra LIMPIA del gate.

CÓMO SE LEE (criterio pre-registrado, no re-litigable sin causa escrita):
  - markout2 (T+5min) es el juez PRINCIPAL: la selección adversa que mata a un maker chico es
    la SOSTENIDA, no el instante. markout1 (T+30s) es secundario.
  - Umbral: markout medio > −(spread capturado / 2). Con half_spread=5¢ → spread 10¢ → −5¢.
  - n≥100 fills limpios para que el veredicto valga. Debajo de eso el script lo dice y NO
    emite veredicto — un promedio de n=4 no es un resultado, es una anécdota.
  - Segmentación por quote_jump_cents = el CONTRAFACTUAL del blindaje: "blindaje@X" es el
    subconjunto con quote_jump < X. En shadow la quote no se retira (#236), así que las dos
    políticas salen del mismo dataset.

USO (en el container, jamás read-write sobre una DB viva):
    python3 scripts/tablero_gate.py
    python3 scripts/tablero_gate.py --db /app/data/trades.db --half-spread 5
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass

# Fix del mark congelado (#235): los fills anteriores tienen markout2 no-independiente.
CORTE_MARK_CONGELADO = "2026-08-14 19:14"
N_MINIMO_VEREDICTO = 100


@dataclass(frozen=True, slots=True)
class Resumen:
    n: int
    avg1: float | None
    avg2: float | None
    n2: int


def _fmt(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2f}¢"


def _resumir(filas: list[tuple]) -> Resumen:
    """filas = [(markout1, markout2), ...] — promedia cada horizonte por separado
    (markout2 tiene menos observaciones: los fills recientes aún no lo alcanzaron)."""
    m1 = [f[0] for f in filas if f[0] is not None]
    m2 = [f[1] for f in filas if f[1] is not None]
    return Resumen(
        n=len(m1),
        avg1=(sum(m1) / len(m1)) if m1 else None,
        avg2=(sum(m2) / len(m2)) if m2 else None,
        n2=len(m2),
    )


def tablero(db_path: str, half_spread: int) -> str:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    out: list[str] = []
    umbral = -(half_spread * 2) / 2  # spread capturado = 2×half_spread

    total, fee_crudo, fee_efectivo = con.execute(
        "select count(*), coalesce(sum(fee_cents),0), "
        "coalesce(sum(coalesce(fee_effective_cents, fee_cents)),0) from mm_shadow_fills"
    ).fetchone()
    out.append(
        f"FILLS TOTALES: {total} | fee crudo {fee_crudo}¢ → efectivo {fee_efectivo}¢ "
        f"(ahorro maker {fee_crudo - fee_efectivo}¢)"
    )

    contaminados = con.execute(
        "select count(*) from mm_shadow_fills where created_at < ? and markout1_cents is not null",
        (CORTE_MARK_CONGELADO,),
    ).fetchone()[0]
    out.append(f"CONTAMINADOS (pre-#235, markout2 no independiente): {contaminados} — EXCLUIDOS")

    limpias = con.execute(
        "select markout1_cents, markout2_cents from mm_shadow_fills "
        "where created_at >= ? and markout1_cents is not null",
        (CORTE_MARK_CONGELADO,),
    ).fetchall()
    r = _resumir(limpias)
    out.append("")
    out.append(
        f"MUESTRA LIMPIA: n={r.n} (markout2 medidos: {r.n2}) | umbral del gate {umbral:+.1f}¢"
    )
    out.append(f"  markout1 (T+30s, secundario): {_fmt(r.avg1)}")
    out.append(f"  markout2 (T+5min, PRINCIPAL): {_fmt(r.avg2)}")

    out.append("")
    out.append("SEGMENTADO por quote_jump (contrafactual del blindaje):")
    for etiqueta, cond in (
        ("blindaje@5  (quote_jump < 5)", "quote_jump_cents < 5"),
        ("solo saltos (quote_jump >= 5)", "quote_jump_cents >= 5"),
        ("sin etiqueta (pre-#236)", "quote_jump_cents is null"),
    ):
        filas = con.execute(
            "select markout1_cents, markout2_cents from mm_shadow_fills "
            f"where created_at >= ? and markout1_cents is not null and {cond}",
            (CORTE_MARK_CONGELADO,),
        ).fetchall()
        s = _resumir(filas)
        out.append(f"  {etiqueta:<32} n={s.n:<4} mk1={_fmt(s.avg1):<9} mk2={_fmt(s.avg2)}")

    out.append("")
    if r.n2 < N_MINIMO_VEREDICTO:
        out.append(
            f"VEREDICTO: SIN DATOS ({r.n2}/{N_MINIMO_VEREDICTO} markout2 limpios). "
            "Un promedio de n chico no es un resultado — el gate NO se declara todavía."
        )
    elif r.avg2 is not None and r.avg2 > umbral:
        out.append(f"VEREDICTO: PASA — markout2 {_fmt(r.avg2)} > umbral {umbral:+.1f}¢.")
    else:
        out.append(
            f"VEREDICTO: FALLA — markout2 {_fmt(r.avg2)} ≤ umbral {umbral:+.1f}¢ "
            "(fills tóxicos: el spread capture no existe a esta escala)."
        )
    con.close()
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/app/data/trades.db")
    ap.add_argument("--half-spread", type=int, default=5, help="MOTOR_MM_HALF_SPREAD_CENTS")
    args = ap.parse_args()
    print(tablero(args.db, args.half_spread))


if __name__ == "__main__":
    main()
