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

# Revenue del market maker por contrato, medido sobre 41,6M de trades de Kalshi
# (Bartlett/O'Hara, SSRN 6615739 rev. 2026-08-12). Es el TECHO teórico de lo que un MM
# captura ANTES de fees — y su fuente no es el spread sino el sesgo conductual del
# retail (compra YES en mercados que resuelven NO; el MM gana 63,4% de las veces).
REVENUE_BROAD_BASED = 0.82  # ganador del juego (KXMLBGAME = lo que M5 cotiza hoy)
REVENUE_SINGLE_NAME = 1.91  # props de jugador — 2,3x el edge, pero más flujo informado


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


def margen_teorico(precio_cents: int, revenue: float = REVENUE_BROAD_BASED) -> float:
    """Margen teórico por contrato: revenue documentado − fee de maker round-trip.

    El fee de Kalshi es proporcional a p(1−p): MÁXIMO en 50¢ y mínimo en las colas. En
    la categoría broad-based (0.82¢ de revenue) eso significa que el margen del maker es
    NEGATIVO en la zona media — justo donde vive el precio de un ganador de partido — y
    solo se vuelve positivo hacia las colas. Esta función existe para que el gate no
    gaste tres semanas midiendo una estrategia cuyo techo aritmético ya es ≤0."""
    fee_rt = 2 * 1.75 * (precio_cents / 100) * (1 - precio_cents / 100)
    return revenue - fee_rt


def zona_muerta(revenue: float = REVENUE_BROAD_BASED) -> tuple[int, int] | None:
    """(low, high) del rango CONTIGUO donde el margen teórico es ≤0, o None si no hay.

    Se reporta la zona muerta y no "la banda viable" porque el conjunto viable es
    DISJUNTO (las dos colas): dar su min/max sería decir "1¢-99¢" y ocultar el pozo
    del medio, que es justo donde M5 cotiza hoy."""
    muertos = [p for p in range(1, 100) if margen_teorico(p, revenue) <= 0]
    return (min(muertos), max(muertos)) if muertos else None


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
    out.append("MARGEN TEÓRICO (techo aritmético ANTES de selección adversa):")
    out.append(
        f"  revenue MM broad-based {REVENUE_BROAD_BASED}¢/contrato (Bartlett/O'Hara, 41.6M trades)"
    )
    zm = zona_muerta()
    if zm is None:
        out.append("  sin zona muerta: el fee no supera al revenue en ningún precio")
    else:
        out.append(
            f"  ZONA MUERTA {zm[0]}¢-{zm[1]}¢ (fee round-trip ≥ revenue) — viable solo en las colas"
        )
    precios = con.execute(
        "select price_cents from mm_shadow_fills where created_at >= ?",
        (CORTE_MARK_CONGELADO,),
    ).fetchall()
    if precios:
        margenes = [margen_teorico(p[0]) for p in precios]
        en_rojo = sum(1 for m in margenes if m <= 0)
        out.append(
            f"  tus fills: n={len(precios)} | margen medio {sum(margenes) / len(margenes):+.3f}¢ | "
            f"{en_rojo}/{len(precios)} ({100 * en_rojo / len(precios):.0f}%) en zona de margen ≤0"
        )
        # BRAZO DE RESCATE (pregunta del agente, 2026-08-15): los fills FUERA de la
        # zona muerta son los únicos con margen teórico positivo — el primer dato de la
        # palanca #1 (cotizar solo las colas). Se listan con su markout: si el maker
        # funciona en algún lado, funciona acá y hay que verlo apenas aparezca.
        zm_lo, zm_hi = zm if zm else (0, 0)
        fuera = con.execute(
            "select price_cents, markout1_cents, markout2_cents from mm_shadow_fills "
            "where created_at >= ? and (price_cents < ? or price_cents > ?) "
            "order by id desc limit 10",
            (CORTE_MARK_CONGELADO, zm_lo, zm_hi),
        ).fetchall()
        if fuera:
            out.append(f"  ✅ BRAZO DE RESCATE: {len(fuera)} fill(s) FUERA de la zona muerta:")
            for px, m1, m2 in fuera:
                out.append(
                    f"     {px}¢ (margen teórico {margen_teorico(px):+.3f}¢) "
                    f"mk1={_fmt(m1)} mk2={_fmt(m2)}"
                )
        else:
            out.append(
                "  brazo de rescate: 0 fills fuera de la zona muerta todavía "
                "(la palanca #1 sigue sin su primer dato)"
            )
        if en_rojo / len(precios) > 0.5:
            out.append(
                "  ⚠️ MAYORÍA EN ZONA MUERTA: el gate puede fallar por aritmética de fee, no por"
            )
            out.append(
                "     mala cotización. Palanca ANTES de archivar: cotizar solo la banda viable."
            )

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
