"""
Diagnóstico READ-ONLY de M5: ¿el markout depende de la EDAD DEL FAIR? + estimación de k.

POR QUÉ EXISTE (2026-08-15). Dos preguntas distintas que se contestan con la MISMA tabla,
así que van en un solo script y en una sola lectura de la DB:

(A) LA HIPÓTESIS DEL ZOMBIE. `FairValueBook.publish()` es upsert y NO borra los tickers
    ausentes (fair_value_book.py:65) — a propósito: un ciclo con odds parcial no debe dejar
    mudo al motor. El efecto colateral aparece en la CONTRACCIÓN del universo: el 15-ago a
    las 20:12 los eventos de Kalshi cayeron de 60 a 11, y con TTL=600s el book habría
    quedado con ~52 fairs de partidos que ya no matchean + 14 frescos. Y el selector de
    universo es `sorted(fairs)[:max_tickers]` — ALFABÉTICO, ciego a la edad. O sea: en el
    bajón de la tarde M5 puede estar cotizando mayoritariamente partidos terminados.
    Cotizar el fair de un partido que terminó es selección adversa POR CONSTRUCCIÓN, y se
    vería exactamente como el markout negativo que estamos midiendo. Si los fills tóxicos
    se concentran en edad alta, el TTL es causa REAL y bajarlo es un arreglo, no un retoque.
    Si el markout es plano contra la edad, la hipótesis muere acá y no se toca el TTL.

(B) NUESTRO k. La intensidad de fill se modela λ(δ) = A·e^(−k·δ) con δ = distancia de la
    quote al mid. El k=150 que circula es del repo público de rodlaf, NO nuestro: con él,
    bajar el half-spread de 5¢ a 2¢ multiplicaría los fills por ~90. Decidir un spread con
    el k de otro es la misma clase de error que la fee ~100× subestimada. Este script lo
    estima con NUESTROS datos o se niega a estimarlo.

    ⚠️ UNIDAD DE k: δ va en DÓLARES (0.03 = 3¢), que es la convención de rodlaf. Un k
    reportado sobre δ en centavos sale 100× más chico y es incomparable. Se imprime la
    unidad en la misma línea que el número, siempre.

CÓMO SE UNE UN FILL A SU QUOTE: el fill del tick t nace de la quote resting del tick t−1,
así que se toma la fila de `mm_quotes` MÁS RECIENTE del mismo ticker con created_at <= el
del fill. `mm_quotes.fair_age_sec` es la edad del fair al cotizar — el dato que (A) necesita.

SE NIEGA A CONCLUIR con muestra chica (misma disciplina que tablero_gate.py): un k regresado
sobre 3 fills no es una calibración, es un dibujo. Los datos crudos se imprimen igual —
mirarlos es gratis; actuar sobre ellos es lo que tiene precio.

USO (en el container; jamás read-write sobre una DB viva):
    python3 scripts/diag_m5_edad_fair.py
    python3 scripts/diag_m5_edad_fair.py --db /app/data/trades.db --desde 2026-08-14
"""

from __future__ import annotations

import argparse
import math
import sqlite3

# Mínimos para EMITIR un número (debajo se imprimen los datos y se declara "sin veredicto").
N_MINIMO_EDAD = 20  # fills con markout2 para comparar bandas de edad
N_MINIMO_K = 30  # fills para regresar log(tasa) contra distancia
BUCKETS_MINIMOS_K = 3  # una recta con 2 puntos pasa por cualquier lado

# Bandas de edad del fair (segundos). Los cortes NO son arbitrarios: 325s es el período
# real de publicación de Motor 2 medido el 15-ago (300s de sleep + el trabajo del ciclo),
# así que "≤325" = el fair de ESTE ciclo y ">325" = el fair ya sobrevivió una publicación
# que no lo renovó — la definición operativa de zombie.
BANDAS_EDAD: tuple[tuple[str, float, float], ...] = (
    ("fresco  (≤120s)", 0.0, 120.0),
    ("medio   (120-325s)", 120.0, 325.0),
    ("zombie  (325-600s)", 325.0, 600.0),
    ("zombie+ (>600s)", 600.0, float("inf")),
)

# Buckets de distancia al mid, en CENTAVOS (se convierte a dólares para el ajuste de k).
BUCKETS_DIST_CENTS: tuple[tuple[float, float], ...] = (
    (0.0, 1.5),
    (1.5, 2.5),
    (2.5, 3.5),
    (3.5, 5.0),
    (5.0, 7.0),
    (7.0, 10.0),
    (10.0, float("inf")),
)


def _fmt(v: float | None, suf: str = "¢") -> str:
    return "—" if v is None else f"{v:+.2f}{suf}"


def _media(xs: list[float]) -> float | None:
    return (sum(xs) / len(xs)) if xs else None


def _bucket_dist(d: float) -> tuple[float, float] | None:
    for lo, hi in BUCKETS_DIST_CENTS:
        if lo <= d < hi:
            return (lo, hi)
    return None


def _centro_cents(lo: float, hi: float) -> float:
    """Centro del bucket; el bucket abierto usa su borde + 1¢ (no hay media definida)."""
    return (lo + hi) / 2.0 if math.isfinite(hi) else lo + 1.0


def ajustar_k(puntos: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Mínimos cuadrados de log(tasa) = log(A) − k·δ, con δ en DÓLARES.

    puntos = [(delta_dolares, tasa_fill), ...] con tasa > 0. Devuelve (k, A) o None si no
    hay dispersión en δ (todas las quotes a la misma distancia → la recta no está definida:
    es exactamente el caso de un half-spread fijo, y hay que decirlo en vez de inventar)."""
    usables = [(d, t) for d, t in puntos if t > 0.0]
    if len(usables) < BUCKETS_MINIMOS_K:
        return None
    n = len(usables)
    sx = sum(d for d, _ in usables)
    sy = sum(math.log(t) for _, t in usables)
    sxx = sum(d * d for d, _ in usables)
    sxy = sum(d * math.log(t) for d, t in usables)
    denom = n * sxx - sx * sx
    if abs(denom) < 1e-12:  # sin dispersión en δ
        return None
    pendiente = (n * sxy - sx * sy) / denom
    intercepto = (sy - pendiente * sx) / n
    return (-pendiente, math.exp(intercepto))


def _cargar_fills(con: sqlite3.Connection, desde: str | None) -> list[dict]:
    """Cada fill con la EDAD DEL FAIR de la quote que lo produjo (la del tick t−1)."""
    where = "where f.created_at >= ?" if desde else ""
    params = (desde,) if desde else ()
    filas = con.execute(
        "select f.id, f.ticker, f.side, f.price_cents, f.markout1_cents, f.markout2_cents, "
        "(select q.fair_age_sec from mm_quotes q "
        " where q.ticker = f.ticker and q.created_at <= f.created_at "
        " order by q.created_at desc limit 1) as edad_fair, "
        "(select q.bid_cents from mm_quotes q "
        " where q.ticker = f.ticker and q.created_at <= f.created_at "
        " order by q.created_at desc limit 1) as bid_cents, "
        "(select q.ask_cents from mm_quotes q "
        " where q.ticker = f.ticker and q.created_at <= f.created_at "
        " order by q.created_at desc limit 1) as ask_cents, "
        "(select q.yes_bid from mm_quotes q "
        " where q.ticker = f.ticker and q.created_at <= f.created_at "
        " order by q.created_at desc limit 1) as yes_bid, "
        "(select q.yes_ask from mm_quotes q "
        " where q.ticker = f.ticker and q.created_at <= f.created_at "
        " order by q.created_at desc limit 1) as yes_ask "
        f"from mm_shadow_fills f {where}",
        params,
    ).fetchall()
    campos = [
        "id",
        "ticker",
        "side",
        "price_cents",
        "markout1",
        "markout2",
        "edad_fair",
        "bid_cents",
        "ask_cents",
        "yes_bid",
        "yes_ask",
    ]
    return [dict(zip(campos, f, strict=True)) for f in filas]


def distancia_al_mid(fila: dict) -> float | None:
    """Distancia (¢) de NUESTRA quote al mid del book, del lado que llenó.

    side='buy' = nos llenaron el bid (compramos) → δ = mid − bid.
    side='sell' = nos llenaron el ask (vendimos) → δ = ask − mid.
    Sin los dos lados del book no hay mid: None (no se inventa con un solo lado)."""
    yb, ya = fila.get("yes_bid"), fila.get("yes_ask")
    if yb is None or ya is None:
        return None
    mid = (yb + ya) / 2.0
    if fila["side"] == "buy":
        precio = fila.get("bid_cents")
        return None if precio is None else mid - precio
    precio = fila.get("ask_cents")
    return None if precio is None else precio - mid


def _seccion_edad(out: list[str], fills: list[dict]) -> None:
    out.append("(A) MARKOUT POR EDAD DEL FAIR — ¿estamos cotizando partidos terminados?")
    con_edad = [f for f in fills if f["edad_fair"] is not None]
    out.append(f"  fills con quote empatada: {len(con_edad)}/{len(fills)}")
    if not con_edad:
        out.append("  sin quotes empatadas — nada que decir (¿mm_quotes vacía o ticker distinto?)")
        return
    n_m2 = 0
    for nombre, lo, hi in BANDAS_EDAD:
        grupo = [f for f in con_edad if lo <= f["edad_fair"] < hi]
        m1 = [f["markout1"] for f in grupo if f["markout1"] is not None]
        m2 = [f["markout2"] for f in grupo if f["markout2"] is not None]
        n_m2 += len(m2)
        out.append(
            f"  {nombre:<20} n={len(grupo):<4} mk1={_fmt(_media(m1)):<9} "
            f"mk2={_fmt(_media(m2)):<9} (n_mk2={len(m2)})"
        )
    if n_m2 < N_MINIMO_EDAD:
        out.append(
            f"  → SIN VEREDICTO ({n_m2}/{N_MINIMO_EDAD} markout2). La hipótesis del zombie "
            "no se confirma NI se descarta todavía."
        )
        return
    zombies = [
        f["markout2"] for f in con_edad if f["edad_fair"] >= 325.0 and f["markout2"] is not None
    ]
    frescos = [
        f["markout2"] for f in con_edad if f["edad_fair"] < 325.0 and f["markout2"] is not None
    ]
    mz, mf = _media(zombies), _media(frescos)
    if mz is None or mf is None:
        out.append("  → SIN VEREDICTO: una de las dos bandas quedó vacía (no hay comparación).")
    elif mz < mf:
        out.append(
            f"  → ZOMBIE CONFIRMADO: mk2 con fair viejo {_fmt(mz)} vs fresco {_fmt(mf)} "
            f"(diferencia {mz - mf:+.2f}¢). Bajar el TTL ataca una causa REAL."
        )
    else:
        out.append(
            f"  → ZOMBIE DESCARTADO: mk2 con fair viejo {_fmt(mz)} no es peor que fresco "
            f"{_fmt(mf)}. El TTL no explica el markout — NO tocarlo por esta vía."
        )


def _seccion_k(
    out: list[str], fills: list[dict], con: sqlite3.Connection, desde: str | None
) -> None:
    out.append("")
    out.append("(B) INTENSIDAD DE FILL λ(δ)=A·e^(−k·δ) — nuestro k, no el de rodlaf")
    where = "where created_at >= ?" if desde else ""
    params = (desde,) if desde else ()
    quotes = con.execute(
        f"select bid_cents, ask_cents, yes_bid, yes_ask from mm_quotes {where}", params
    ).fetchall()
    # Oportunidades por bucket: cada LADO cotizado con book de dos lados es un ensayo.
    oportunidades: dict[tuple[float, float], int] = {}
    for bid, ask, yb, ya in quotes:
        if yb is None or ya is None:
            continue
        mid = (yb + ya) / 2.0
        for precio, dist in (
            (bid, None if bid is None else mid - bid),
            (ask, None if ask is None else ask - mid),
        ):
            if precio is None or dist is None or dist < 0:
                continue
            b = _bucket_dist(dist)
            if b is not None:
                oportunidades[b] = oportunidades.get(b, 0) + 1
    llenos: dict[tuple[float, float], int] = {}
    n_dist = 0
    for f in fills:
        d = distancia_al_mid(f)
        if d is None or d < 0:
            continue
        n_dist += 1
        b = _bucket_dist(d)
        if b is not None:
            llenos[b] = llenos.get(b, 0) + 1
    out.append(
        f"  quotes-lado con mid usable: {sum(oportunidades.values())} | fills con δ: {n_dist}"
    )
    puntos: list[tuple[float, float]] = []
    for lo, hi in BUCKETS_DIST_CENTS:
        opo = oportunidades.get((lo, hi), 0)
        lle = llenos.get((lo, hi), 0)
        if opo == 0:
            continue
        tasa = lle / opo
        etiqueta = f"{lo:g}-{hi:g}¢" if math.isfinite(hi) else f">{lo:g}¢"
        out.append(f"  δ {etiqueta:<10} ensayos={opo:<6} fills={lle:<4} tasa={tasa:.4f}")
        puntos.append((_centro_cents(lo, hi) / 100.0, tasa))
    if n_dist < N_MINIMO_K:
        out.append(
            f"  → SIN VEREDICTO ({n_dist}/{N_MINIMO_K} fills con δ). Un k sobre esta muestra "
            "no calibra nada: NO usarlo para mover el half-spread."
        )
        return
    ajuste = ajustar_k(puntos)
    if ajuste is None:
        out.append(
            f"  → SIN VEREDICTO: menos de {BUCKETS_MINIMOS_K} buckets con fills, o toda la "
            "muestra a la misma δ (half-spread fijo). Hace falta variar el spread para medir k."
        )
        return
    k, a = ajuste
    out.append(f"  → k ≈ {k:.1f} por DÓLAR de δ (A ≈ {a:.4f}). rodlaf usa k=150.")
    out.append(
        f"     lectura: pasar de 5¢ a 2¢ multiplicaría la tasa por "
        f"e^(k·0.03) ≈ {math.exp(k * 0.03):.1f}×"
    )


def informe(db_path: str, desde: str | None) -> str:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        fills = _cargar_fills(con, desde)
        out: list[str] = [
            f"FILLS ANALIZADOS: {len(fills)}" + (f" (desde {desde})" if desde else "")
        ]
        out.append("")
        _seccion_edad(out, fills)
        _seccion_k(out, fills, con, desde)
        return "\n".join(out)
    finally:
        con.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="/app/data/trades.db")
    ap.add_argument("--desde", default=None, help="Corte inferior de created_at (ej. 2026-08-14)")
    args = ap.parse_args()
    print(informe(args.db, args.desde))


if __name__ == "__main__":
    main()
