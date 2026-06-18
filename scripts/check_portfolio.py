"""
Dashboard de cartera real de Kalshi — verifica firma, reconcilia fills, fees y P&L por motor.

Cruza los fills de la API con la tabla `trades` local (por order_id) para etiquetar cada
fill con su motor y P&L realizado; concilia el balance contra P&L+fees; explica el
portfolio_value (incluye posiciones con position=0 pero exposición/settlement pendiente); y
agrupa los trades de Motor REST por arb_id para ver si cada arbitraje quedó COMPLETO (todas
sus patas) o dejó una pata huérfana.

Read-only total: solo GET /portfolio/{balance,positions,fills,orders} + SELECT a trades.
NO coloca ni cancela órdenes. NO escribe en la DB. TRADING_ENABLED no se toca.

Uso (en Coolify, tras redeploy):
    python scripts/check_portfolio.py
"""

from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.storage.models import DailyPnL, Trade, get_session
from src.utils.config import get_settings


def _cents(v: object) -> str:
    try:
        return f"${int(v) / 100:.2f}"
    except (TypeError, ValueError):
        return str(v)


def _num(v: object) -> int | None:
    """int desde int o fixed-point string ('10.00', '0.42'→0). None si inválido."""
    if v is None:
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _fnum(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fill_count(f: dict) -> int | None:
    """Cantidad de contratos: count int, o count_fp fixed-point."""
    c = _num(f.get("count"))
    return c if c is not None else _num(f.get("count_fp"))


def _fill_price_cents(f: dict) -> int | None:
    """
    Precio ejecutado del lado tradeado, en ¢. Prioriza yes_price/no_price según el side;
    si no, escanea CUALQUIER clave que contenga 'price' (robusto al nombre exacto del campo).
    """
    side = str(f.get("side", "")).lower()
    preferred = "yes_price" if side == "yes" else "no_price"
    if f.get(preferred) is not None:
        return _num(f.get(preferred))
    # Fallback: cualquier clave *price* con valor (price, price_dollars, yes_price, ...).
    # Si la clave está denominada en dólares, escalá a ¢ (evita un 100× silencioso).
    for k, v in f.items():
        kl = k.lower()
        if "price" in kl and v is not None:
            try:
                cents = float(v) * 100 if "dollar" in kl else float(v)
            except (TypeError, ValueError):
                continue
            return int(round(cents))
    return None


def _fill_fee_raw(f: dict) -> float | None:
    """Valor crudo del fee del fill (primera clave *fee* con valor). Unidad sin asumir."""
    for k, v in f.items():
        if "fee" in k.lower() and v is not None:
            n = _fnum(v)
            if n is not None:
                return n
    return None


def _trade_for_fill(f: dict, idx: dict[str, Trade]) -> Trade | None:
    return idx.get(str(f.get("order_id", ""))) or idx.get(str(f.get("client_order_id", "")))


def _load_trades() -> tuple[dict[str, Trade], list[Trade]]:
    """
    (index order_id→Trade, lista completa de trades) desde la DB local. Read-only (SELECT).
    El index liga por kalshi_order_id (lo que trae el fill) y client_order_id (fallback).
    """
    try:
        with get_session() as s:
            trades = list(s.exec(select(Trade)).all())
    except Exception as e:
        print(f"⚠️  DB local no accesible ({type(e).__name__}: {e}) — sin strategy/PnL por fill.")
        return {}, []
    index: dict[str, Trade] = {}
    for t in trades:
        if t.kalshi_order_id:
            index[t.kalshi_order_id] = t
        if t.client_order_id:
            index.setdefault(t.client_order_id, t)
    return index, trades


async def run() -> int:
    ok = True
    idx, all_trades = _load_trades()
    n_with_koid = sum(1 for t in all_trades if t.kalshi_order_id)
    print(f"📒 trades en DB local: {len(all_trades)} (con kalshi_order_id: {n_with_koid})")

    fills: list[dict] = []
    positions: list[dict] = []
    settlements: list[dict] = []
    async with KalshiRestClient() as kc:
        # 1) balance crudo (control + auditar portfolio_value).
        bal: dict = {}
        try:
            bal = await kc.get_balance()
            print(f"\n✅ balance (crudo): {bal}")
        except Exception as e:
            ok = False
            print(f"❌ balance: {type(e).__name__}: {e}")

        # 2) positions — TODAS (incluye position=0 con exposición/settlement → explica pv).
        try:
            resp = await kc.get_positions(limit=200)
            positions = resp.get("market_positions", resp.get("positions", [])) or []
            ok &= _print_positions(positions)
        except Exception as e:
            ok = False
            print(f"\n❌ positions: {type(e).__name__}: {e}  ← el fix de firma NO está activo")

        # 3) fills — cruzados con la DB local para motor + PnL.
        try:
            resp = await kc.get_fills(limit=100)
            fills = resp.get("fills", []) or []
            print(f"\n✅ fills: 200 OK — {len(fills)} fill(s) recientes")
            if fills:
                print(f"  (1er fill crudo: {fills[0]})")
            _print_fills(fills, idx)
        except Exception as e:
            ok = False
            print(f"\n❌ fills: {type(e).__name__}: {e}  ← el fix de firma NO está activo")

        # 4) órdenes resting (parte del portfolio_value con 0 posiciones).
        try:
            resp = await kc.get_orders(status="resting", limit=100)
            orders = resp.get("orders", []) or []
            print(f"\n✅ orders resting: 200 OK — {len(orders)} viva(s)")
            for o in orders[:20]:
                rem = _num(o.get("remaining_count")) or _num(o.get("remaining_count_fp"))
                side = str(o.get("side", "")).lower()
                price = o.get("yes_price") if side == "yes" else o.get("no_price")
                print(
                    f"  {str(o.get('ticker', ''))[:30]:<30} {side:<4} "
                    f"{str(o.get('action', '')):<5} rem={rem} @ {_num(price)}c"
                )
        except Exception as e:
            print(f"\n⚠️  orders: {type(e).__name__}: {e}")

        # 5) settlements (revenue por resolución) — desglosa el portfolio_value.
        try:
            resp = await kc.get_settlements(limit=200)
            settlements = resp.get("settlements", []) or []
            print(f"\n✅ settlements: 200 OK — {len(settlements)} resuelto(s)")
            if settlements:
                print(f"  (1er settlement crudo: {settlements[0]})")
        except Exception as e:
            print(f"\n⚠️  settlements: {type(e).__name__}: {e}  (verificá el path/campo en prod)")

    _print_summary(fills, idx, bal)
    _print_pv_breakdown(positions, settlements, bal)
    _print_balance_reconciliation(bal, settlements, all_trades)
    _print_unmatched(fills, idx, all_trades)
    _print_motor_rest_arbs(all_trades)
    _print_pnl_by_motor(all_trades)

    print(f"\n{'=' * 70}")
    if ok:
        print("✅ FIX CONFIRMADO: el bot lee balance + positions + fills (cartera visible).")
        return 0
    print("❌ Algún endpoint sigue fallando — ¿redeploy pendiente? Pegame el error.")
    return 1


def _print_positions(positions: list[dict]) -> bool:
    """Imprime TODAS las posiciones (no solo position!=0): la masa del portfolio_value
    suele estar en filas con position=0 pero market_exposure / settlement pendiente."""
    nonzero = [p for p in positions if _num(p.get("position"))]
    withexp = [
        p for p in positions if not _num(p.get("position")) and _num(p.get("market_exposure"))
    ]
    print(
        f"\n✅ positions: 200 OK — {len(nonzero)} abierta(s), "
        f"{len(withexp)} con exposición pero position=0 (settlement/margen pendiente)"
    )
    rows = nonzero + withexp
    if rows:
        print(f"  {'ticker':<34} {'pos':>6} {'exposición':>12} {'pnl_real':>10}")
        for p in rows[:30]:
            print(
                f"  {str(p.get('ticker', '')):<34} {_num(p.get('position')) or 0:>6} "
                f"{_cents(p.get('market_exposure')):>12} {_cents(p.get('realized_pnl')):>10}"
            )
        if rows and withexp:
            print(f"  (1er position con position=0+exposición, crudo: {withexp[0]})")
    return True


def _print_fills(fills: list[dict], idx: dict[str, Trade]) -> None:
    """Tabla de fills: cantidad, precio efectivo, notional, fee, motor y PnL (DB)."""
    print(
        f"  {'ticker':<24} {'sd':<3} {'act':<4} {'cant':>5} {'¢':>4} {'notional':>9} "
        f"{'fee':>6} {'motor':<16} {'pnl':>8}"
    )
    for f in fills[:40]:
        cnt = _fill_count(f)
        pc = _fill_price_cents(f)
        notional = _cents(cnt * pc) if (cnt is not None and pc is not None) else "—"
        fee = _fill_fee_raw(f)
        t = _trade_for_fill(f, idx)
        motor = t.strategy if t else "?"
        pnl = _cents(t.pnl_cents) if (t and t.pnl_cents is not None) else "—"
        print(
            f"  {str(f.get('ticker', ''))[:24]:<24} {str(f.get('side', ''))[:3]:<3} "
            f"{str(f.get('action', ''))[:4]:<4} {str(cnt):>5} {str(pc):>4} {notional:>9} "
            f"{(f'{fee:.3f}' if fee is not None else '—'):>6} {motor[:16]:<16} {pnl:>8}"
        )


def _print_summary(fills: list[dict], idx: dict[str, Trade], bal: dict) -> None:
    """Flujo de caja + fees (ambas interpretaciones de unidad) por los fills."""
    print(f"\n{'=' * 70}\n📊 RESUMEN AGREGADO")
    spent = received = 0  # ¢
    fee_as_total = fee_as_per_contract = 0.0  # dólares
    by_motor: dict[str, dict[str, int]] = defaultdict(lambda: {"fills": 0, "contratos": 0})
    for f in fills:
        cnt = _fill_count(f) or 0
        pc = _fill_price_cents(f) or 0
        cash = cnt * pc
        if str(f.get("action", "")).lower() == "sell":
            received += cash
        else:
            spent += cash
        fee = _fill_fee_raw(f)
        if fee is not None:
            fee_as_total += fee
            fee_as_per_contract += fee * cnt
        t = _trade_for_fill(f, idx)
        motor = t.strategy if t else "?"
        by_motor[motor]["fills"] += 1
        by_motor[motor]["contratos"] += cnt

    print(
        f"  fills: {len(fills)}  |  comprado: {_cents(spent)}  vendido: {_cents(received)}  "
        f"flujo_neto: {_cents(received - spent)}"
    )
    # El fill trae fee_cost ~0.017 (≈ fee Kalshi por-contrato a 50¢). Mostramos las dos
    # lecturas de unidad para que el lector vea cuál cuadra con la baja de balance.
    print(
        f"  fees fills: Σfee_cost={fee_as_total:.2f}  |  Σ(fee_cost×contratos)="
        f"{fee_as_per_contract:.2f}  (la 2da asume fee_cost=por-contrato en $)"
    )
    print("  por motor (fills):")
    for motor, d in sorted(by_motor.items()):
        print(f"    {motor:<20} {d['fills']:>3} fills  {d['contratos']:>6} contratos")
    if "balance" in bal:
        print(f"  balance actual: {_cents(bal.get('balance'))}  (crudo arriba para auditar pv)")


def _pv_cents(bal: dict) -> int | None:
    """portfolio_value en ¢ desde el balance crudo (escanea clave *portfolio*/*value*)."""
    for k, v in bal.items():
        kl = k.lower()
        if ("portfolio" in kl or "value" in kl) and v is not None:
            n = _num(v)
            if n is not None:
                return n
    return None


def _settle_revenue(s: dict) -> int | None:
    """revenue del settlement en ¢ (revenue; fallback settlement_amount/value)."""
    for key in ("revenue", "settlement_amount", "value"):
        if s.get(key) is not None:
            return _num(s.get(key))
    return None


def _parse_ts(v: object) -> datetime | None:
    """A datetime naive-UTC desde ISO ('...Z') o epoch (seg/ms). None si no parsea."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        ts = v / 1000 if v > 1e12 else v
        return datetime.fromtimestamp(ts, UTC).replace(tzinfo=None)
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt
    except (TypeError, ValueError):
        return None


def _print_pv_breakdown(positions: list[dict], settlements: list[dict], bal: dict) -> None:
    """Desglosa el portfolio_value: exposición abierta + revenue de settlements recientes."""
    pv = _pv_cents(bal)
    print(f"\n{'=' * 70}\n💼 DESGLOSE portfolio_value ({_cents(pv) if pv is not None else '—'})")
    exp = sum(_num(p.get("market_exposure")) or 0 for p in positions if _num(p.get("position")))
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)
    rev_24h = 0
    n_24h = 0
    for s in settlements:
        ts = _parse_ts(s.get("settled_time") or s.get("settled_ts"))
        if ts is None or ts >= cutoff:  # sin ts confiable → incluir (conservador)
            rev_24h += _settle_revenue(s) or 0
            n_24h += 1
    print(f"  Σ exposición posiciones abiertas:   {_cents(exp):>10}")
    print(f"  Σ revenue settlements (~24h, {n_24h} filas): {_cents(rev_24h):>10}")
    if pv is not None:
        residual = pv - exp - rev_24h
        print(f"  residual no atribuido:              {_cents(residual):>10}")
    if not settlements:
        print(
            "  (sin settlements legibles — si el endpoint falló, verificá en el dashboard Kalshi)"
        )


def _initial_bankroll_cents() -> tuple[int | None, str]:
    """Bankroll inicial en ¢: DailyPnL.starting_capital (hoy/último) o env. (valor, fuente)."""
    try:
        with get_session() as s:
            rows = s.exec(select(DailyPnL).order_by(DailyPnL.date.desc())).all()
        if rows:
            return int(round(rows[0].starting_capital * 100)), f"DailyPnL {rows[0].date}"
    except Exception:
        pass
    try:
        env = get_settings().KALSHI_INITIAL_BANKROLL
        if env > 0:
            return int(round(env * 100)), "env KALSHI_INITIAL_BANKROLL"
    except Exception:
        pass
    return None, "(sin fuente — seteá KALSHI_INITIAL_BANKROLL)"


def _print_balance_reconciliation(
    bal: dict, settlements: list[dict], all_trades: list[Trade]
) -> None:
    """Concilia balance_actual vs balance_inicial usando pnl settled y fees (24h, DB)."""
    print(f"\n{'=' * 70}\n🧮 RECONCILIACIÓN BALANCE")
    actual = _num(bal.get("balance"))
    inicial, fuente = _initial_bankroll_cents()
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=24)
    fees_24h = sum(t.fees_cents or 0 for t in all_trades if t.filled_at and t.filled_at >= cutoff)
    pnl_24h = sum(t.pnl_cents or 0 for t in all_trades if t.settled_at and t.settled_at >= cutoff)
    pv = _pv_cents(bal)
    print(f"  balance_actual:        {_cents(actual):>10}")
    print(
        f"  balance_inicial:       {_cents(inicial) if inicial is not None else '—':>10}  {fuente}"
    )
    if actual is not None and inicial is not None:
        delta = actual - inicial
        explained = pnl_24h - fees_24h
        print(f"  Δ (actual − inicial):  {_cents(delta):>10}")
        print(f"  Σ pnl settled (24h):   {_cents(pnl_24h):>10}")
        print(f"  Σ fees pagados (24h):  {_cents(-fees_24h):>10}")
        if pv is not None:
            print(f"  Σ portfolio_value:     {_cents(pv):>10}  (capital atado, NO perdido)")
        no_expl = delta - explained
        print(f"  delta no explicado:    {_cents(no_expl):>10}  (Δ − (pnl − fees))")
        if pv is not None:
            print(
                f"  └─ tras descontar pv:  {_cents(no_expl + pv):>10}  "
                "(≈0 si el faltante es solo capital atado en pv)"
            )
        if abs(no_expl) > 100:
            print(
                "  ⚠️  delta no explicado > $1 — flujo no atribuido (revisá fills sin motor / fees)"
            )
    else:
        print("  (sin balance_inicial → no se puede conciliar; seteá KALSHI_INITIAL_BANKROLL)")


def _print_unmatched(fills: list[dict], idx: dict[str, Trade], all_trades: list[Trade]) -> None:
    """Diagnostica los fills sin motor ('?'): ¿persistencia, pre-DB o manuales?"""
    unmatched = [f for f in fills if _trade_for_fill(f, idx) is None]
    if not unmatched:
        return
    has_coid = sum(1 for f in fills if f.get("client_order_id"))
    print(f"\n{'=' * 70}\n🔎 FILLS SIN MOTOR ('?'): {len(unmatched)}/{len(fills)}")
    print(
        f"  fills con client_order_id: {has_coid}/{len(fills)}  |  "
        f"trades DB sin kalshi_order_id: {sum(1 for t in all_trades if not t.kalshi_order_id)}"
    )
    # Atribución APROXIMADA por ticker: ¿hay algún trade del bot en ese mismo ticker?
    by_ticker: dict[str, str] = {}
    for t in all_trades:
        by_ticker.setdefault(t.ticker, t.strategy)
    approx = defaultdict(int)
    for f in unmatched:
        approx[by_ticker.get(str(f.get("ticker", "")), "NO-EN-DB")] += 1
    print("  atribución aprox por ticker (qué motor tocó ese ticker alguna vez):")
    for motor, n in sorted(approx.items(), key=lambda kv: -kv[1]):
        print(f"    {motor:<20} {n:>3} fills")
    print("  (NO-EN-DB = ticker que el bot nunca tradeó → fill manual tuyo o cuenta previa)")
    print(
        f"  muestra order_ids sin match: {[str(f.get('order_id', ''))[:18] for f in unmatched[:5]]}"
    )


def _print_motor_rest_arbs(all_trades: list[Trade]) -> None:
    """Agrupa Motor REST por arb_id (en notes) → ¿cada arb quedó COMPLETO o con pata huérfana?"""
    rest = [t for t in all_trades if t.strategy == "motor_rest_arb"]
    if not rest:
        return
    groups: dict[str, list[Trade]] = defaultdict(list)
    for t in rest:
        m = re.search(r"arb_id=([0-9a-f-]+)", t.notes or "")
        groups[m.group(1) if m else "sin_arb_id"].append(t)
    print(f"\n{'=' * 70}\n🧮 MOTOR REST por arb_id: {len(groups)} arbitraje(s), {len(rest)} patas")
    print("  (un arb SANO compra todas sus patas y su suma de precios < 100¢; si quedó")
    print("   una pata 'filled' suelta y las otras 'cancelled' = pata huérfana → rollback)")
    for arb_id, legs in list(groups.items())[:20]:
        suma = sum(t.price_cents for t in legs if t.status in ("filled", "settled"))
        states = ",".join(sorted({t.status for t in legs}))
        pnl = sum(t.pnl_cents or 0 for t in legs)
        print(
            f"\n  arb {arb_id[:8]} — {len(legs)} patas · estados=[{states}] · Σ¢(vivas)={suma} · pnl={_cents(pnl)}"
        )
        for t in legs:
            print(
                f"    {t.ticker[:30]:<30} {t.side:<3} {t.action:<4} {t.count:>4}@{t.price_cents:>3}c "
                f"{t.status:<10} pnl={_cents(t.pnl_cents) if t.pnl_cents is not None else '—'}"
            )


def _print_pnl_by_motor(all_trades: list[Trade]) -> None:
    """P&L realizado + win-rate por motor (trades con pnl_cents)."""
    settled = [t for t in all_trades if t.pnl_cents is not None]
    if not settled:
        print(f"\n{'=' * 70}\n💰 P&L realizado: (aún sin trades con pnl_cents en la DB)")
        return
    agg: dict[str, dict[str, Any]] = defaultdict(lambda: {"pnl": 0, "wins": 0, "n": 0})
    for t in settled:
        d = agg[t.strategy]
        d["pnl"] += t.pnl_cents or 0
        d["n"] += 1
        if (t.pnl_cents or 0) > 0:
            d["wins"] += 1
    print(f"\n{'=' * 70}\n💰 P&L REALIZADO por motor (trades settled)")
    print(f"  {'motor':<20} {'n':>4} {'win%':>6} {'pnl':>10}")
    tp = tn = tw = 0
    for motor, d in sorted(agg.items()):
        wr = 100 * d["wins"] / d["n"] if d["n"] else 0
        print(f"  {motor:<20} {d['n']:>4} {wr:>5.0f}% {_cents(d['pnl']):>10}")
        tp += d["pnl"]
        tn += d["n"]
        tw += d["wins"]
    wr_t = 100 * tw / tn if tn else 0
    print(f"  {'TOTAL':<20} {tn:>4} {wr_t:>5.0f}% {_cents(tp):>10}")


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
