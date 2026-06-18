"""
Análisis de patas huérfanas de Motor REST — clasifica cada arbitraje en COMPLETO,
HUÉRFANO, SIN-FILL o EN-VUELO, y cuantifica el costo neto real al balance.

Agrupa los trades `strategy="motor_rest_arb"` por arb_id (notes 'arb_id=<uuid>', misma
clave que usa el settlement) y por grupo decide:
  - COMPLETO   → todas las patas filled/settled (el arb se capturó: ganancia chica garantizada)
  - HUÉRFANO   → ≥1 pata filled/settled y ≥1 pata cancelled/error (rollback: riesgo direccional)
  - SIN-FILL   → todas cancelled (both-KILL: exposición cero, inofensivo)
  - EN-VUELO   → alguna pata pending (aún no se sabe)

Imprime: win-rate REAL del arb (completos / (completos+huérfanos), NO el win-rate por pata),
P&L realizado (settled) y capital en riesgo (huérfanos sin settlear) por clase, y el detalle
de CADA arb huérfano pata por pata.

Read-only total: solo SELECT a la tabla trades. NO coloca/cancela órdenes. NO escribe en la DB.

Uso (en Coolify, tras redeploy):
    python scripts/analyze_orphan_arbs.py
"""

from __future__ import annotations

from collections import defaultdict

from sqlmodel import select

from src.storage.models import Trade, get_session
from src.strategies.motor_rest_arb.settlement import arb_group_key

_FILLED = {"filled", "settled"}
_DEAD = {"cancelled", "error"}


def _cents(v: object) -> str:
    try:
        return f"${int(v) / 100:.2f}"
    except (TypeError, ValueError):
        return str(v)


def _load_rest_trades() -> list[Trade]:
    """Todos los trades de Motor REST desde la DB local. Read-only (SELECT)."""
    try:
        with get_session() as s:
            return list(s.exec(select(Trade).where(Trade.strategy == "motor_rest_arb")).all())
    except Exception as e:  # noqa: BLE001 — script de diagnóstico, queremos el mensaje crudo
        print(f"⚠️  DB local no accesible ({type(e).__name__}: {e}).")
        return []


def _classify(legs: list[Trade]) -> str:
    filled = sum(1 for t in legs if t.status in _FILLED)
    dead = sum(1 for t in legs if t.status in _DEAD)
    pending = sum(1 for t in legs if t.status not in _FILLED and t.status not in _DEAD)
    if pending:
        return "EN-VUELO"
    if filled == len(legs):
        return "COMPLETO"
    if filled > 0 and dead > 0:
        return "HUÉRFANO"
    return "SIN-FILL"  # todas dead, ninguna filled → exposición cero


def _sum_pnl(legs: list[Trade]) -> int:
    return sum(t.pnl_cents or 0 for t in legs)


def _sum_fees(legs: list[Trade]) -> int:
    return sum(t.fees_cents or 0 for t in legs)


def _exposure_cents(legs: list[Trade]) -> int:
    """Capital comprometido en patas filled sin settlear (fill_price × count)."""
    return sum(
        (t.fill_price_cents or t.price_cents or 0) * t.count
        for t in legs
        if t.status == "filled"  # filled pero aún no settled
    )


def run() -> int:
    trades = _load_rest_trades()
    if not trades:
        print("No hay trades de Motor REST en la DB (o DB inaccesible). Nada para analizar.")
        return 0

    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        groups[arb_group_key(t)].append(t)

    buckets: dict[str, list[tuple[str, list[Trade]]]] = defaultdict(list)
    for arb_id, legs in groups.items():
        buckets[_classify(legs)].append((arb_id, legs))

    n_complete = len(buckets.get("COMPLETO", []))
    n_orphan = len(buckets.get("HUÉRFANO", []))
    n_nofill = len(buckets.get("SIN-FILL", []))
    n_inflight = len(buckets.get("EN-VUELO", []))

    print("=" * 70)
    print(f"MOTOR REST — análisis de arbs   ({len(trades)} patas, {len(groups)} arbs)")
    print("=" * 70)
    print(f"  ✅ COMPLETO  : {n_complete:3d}  (ambas patas filled — arb capturado)")
    print(f"  🩸 HUÉRFANO  : {n_orphan:3d}  (1 pata filled + 1 muerta — rollback/riesgo)")
    print(f"  ⚪ SIN-FILL  : {n_nofill:3d}  (both-KILL — exposición cero, inofensivo)")
    print(f"  ⏳ EN-VUELO  : {n_inflight:3d}  (alguna pata pending)")

    decided = n_complete + n_orphan
    if decided:
        wr = 100.0 * n_complete / decided
        print(
            f"\n  WIN-RATE REAL del arb = {n_complete}/{decided} = {wr:.1f}%"
            "   (completos / (completos+huérfanos); ignora SIN-FILL y EN-VUELO)"
        )

    print("\n  P&L realizado (settled) y exposición por clase:")
    for cls in ("COMPLETO", "HUÉRFANO", "SIN-FILL", "EN-VUELO"):
        items = buckets.get(cls, [])
        if not items:
            continue
        pnl = sum(_sum_pnl(legs) for _, legs in items)
        fees = sum(_sum_fees(legs) for _, legs in items)
        exp = sum(_exposure_cents(legs) for _, legs in items)
        n_settled = sum(1 for _, legs in items if any(t.status == "settled" for t in legs))
        print(
            f"    {cls:9s}  pnl={_cents(pnl):>9s}  fees={_cents(fees):>8s}  "
            f"exp_sin_settlear={_cents(exp):>9s}  (settled: {n_settled}/{len(items)})"
        )

    total_pnl = sum(t.pnl_cents or 0 for t in trades)
    total_fees = sum(t.fees_cents or 0 for t in trades)
    print(
        f"\n  💥 IMPACTO NETO REALIZADO Motor REST: pnl={_cents(total_pnl)}  fees={_cents(total_fees)}"
    )

    orphans = buckets.get("HUÉRFANO", [])
    if orphans:
        print("\n" + "=" * 70)
        print(f"DETALLE — {len(orphans)} arb(s) HUÉRFANO(s), pata por pata")
        print("=" * 70)
        for i, (arb_id, legs) in enumerate(orphans, 1):
            grp_pnl = _sum_pnl(legs)
            print(f"\n[{i}] arb_id={arb_id}   pnl_grupo={_cents(grp_pnl)}")
            for t in sorted(legs, key=lambda x: x.client_order_id):
                fill = _cents(t.fill_price_cents) if t.fill_price_cents is not None else "—"
                pnl = _cents(t.pnl_cents) if t.pnl_cents is not None else "—"
                flag = "🩸" if t.status in _FILLED else "✂️"
                print(
                    f"   {flag} {t.ticker:<22s} {t.side:<3s} {t.action:<4s} x{t.count:<3d}  "
                    f"limit={_cents(t.price_cents)} fill={fill:>7s} status={t.status:<9s} "
                    f"pnl={pnl:>8s} fees={_cents(t.fees_cents or 0)}"
                )
                if t.notes:
                    print(f"        notes: {t.notes}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
