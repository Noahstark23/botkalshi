"""
Dashboard de cartera real de Kalshi — verifica firma, reconcilia fills y P&L por motor.

Cruza los fills de la API con la tabla `trades` local (por order_id) para etiquetar cada
fill con su estrategia (qué motor lo generó) y su P&L realizado, y arma un resumen agregado
de la sesión: invertido, recuperado, P&L neto y win-rate por motor.

Read-only total: solo GET /portfolio/{balance,positions,fills,orders} + SELECT a trades.
NO coloca ni cancela órdenes. NO escribe en la DB. TRADING_ENABLED no se toca.

Uso (en Coolify, tras redeploy):
    python scripts/check_portfolio.py
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

from sqlmodel import select

from src.clients.kalshi_rest import KalshiRestClient
from src.storage.models import Trade, get_session


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


def _load_trades_by_order_id() -> dict[str, Trade]:
    """
    Mapa order_id → Trade desde la DB local. Indexa por kalshi_order_id (lo que liga al
    fill) y también por client_order_id como fallback. Read-only (SELECT).
    """
    index: dict[str, Trade] = {}
    try:
        with get_session() as s:
            trades = s.exec(select(Trade)).all()
    except Exception as e:
        print(f"⚠️  DB local no accesible ({type(e).__name__}: {e}) — sin strategy/PnL por fill.")
        return index
    for t in trades:
        if t.kalshi_order_id:
            index[t.kalshi_order_id] = t
        if t.client_order_id:
            index.setdefault(t.client_order_id, t)
    return index


def _trade_for_fill(f: dict, trades_idx: dict[str, Trade]) -> Trade | None:
    return trades_idx.get(str(f.get("order_id", ""))) or trades_idx.get(
        str(f.get("client_order_id", ""))
    )


async def run() -> int:
    ok = True
    trades_idx = _load_trades_by_order_id()
    print(f"📒 trades en DB local: {len({id(t) for t in trades_idx.values()})}")

    fills: list[dict] = []
    async with KalshiRestClient() as kc:
        # 1) balance (siempre funcionó — control). Imprime crudo para auditar portfolio_value.
        try:
            bal = await kc.get_balance()
            print(f"\n✅ balance (crudo): {bal}")
        except Exception as e:
            ok = False
            print(f"❌ balance: {type(e).__name__}: {e}")

        # 2) positions (la que daba 401 — la prueba del fix)
        try:
            resp = await kc.get_positions(limit=200)
            positions = resp.get("market_positions", resp.get("positions", [])) or []
            open_pos = [p for p in positions if p.get("position")]
            print(f"\n✅ positions: 200 OK — {len(open_pos)} posición(es) abierta(s)")
            print(f"  {'ticker':<34} {'pos':>6} {'exposición':>12} {'pnl_real':>10}")
            for p in open_pos:
                print(
                    f"  {str(p.get('ticker', '')):<34} {p.get('position', 0):>6} "
                    f"{_cents(p.get('market_exposure')):>12} {_cents(p.get('realized_pnl')):>10}"
                )
        except Exception as e:
            ok = False
            print(f"\n❌ positions: {type(e).__name__}: {e}  ← el fix de firma NO está activo")

        # 3) fills — cruzados con la DB local para strategy + PnL por motor.
        try:
            resp = await kc.get_fills(limit=100)
            fills = resp.get("fills", []) or []
            print(f"\n✅ fills: 200 OK — {len(fills)} fill(s) recientes")
            if fills:
                # Dump CRUDO completo del 1er fill (claves Y valores) → cero adivinanza.
                print(f"  (1er fill crudo: {fills[0]})")
            _print_fills(fills, trades_idx)
        except Exception as e:
            ok = False
            print(f"\n❌ fills: {type(e).__name__}: {e}  ← el fix de firma NO está activo")

        # 4) órdenes resting (explica portfolio_value con 0 posiciones)
        try:
            resp = await kc.get_orders(status="resting", limit=100)
            orders = resp.get("orders", []) or []
            print(f"\n✅ orders resting: 200 OK — {len(orders)} viva(s)")
            for o in orders[:20]:
                rem = _num(o.get("remaining_count")) or _num(o.get("remaining_count_fp"))
                price = (
                    o.get("yes_price")
                    if str(o.get("side", "")).lower() == "yes"
                    else o.get("no_price")
                )
                print(
                    f"  {str(o.get('ticker', ''))[:30]:<30} {str(o.get('side', '')):<4} "
                    f"{str(o.get('action', '')):<5} rem={rem} @ {_num(price)}c"
                )
        except Exception as e:
            print(f"\n⚠️  orders: {type(e).__name__}: {e}")

    _print_summary(fills, trades_idx)

    print(f"\n{'=' * 64}")
    if ok:
        print("✅ FIX CONFIRMADO: el bot lee balance + positions + fills (cartera visible).")
        return 0
    print("❌ Algún endpoint sigue fallando — ¿redeploy pendiente? Pegame el error.")
    return 1


def _print_fills(fills: list[dict], trades_idx: dict[str, Trade]) -> None:
    """Tabla de fills con cantidad, precio efectivo, notional, motor y PnL (desde la DB)."""
    hdr = f"  {'ticker':<26} {'side':<4} {'act':<5} {'cant':>5} {'¢':>4} {'notional':>9} "
    hdr += f"{'motor':<18} {'pnl':>9}"
    print(hdr)
    for f in fills[:40]:
        cnt = _fill_count(f)
        pc = _fill_price_cents(f)
        notional = _cents(cnt * pc) if (cnt is not None and pc is not None) else "—"
        t = _trade_for_fill(f, trades_idx)
        motor = t.strategy if t else "?"
        pnl = _cents(t.pnl_cents) if (t and t.pnl_cents is not None) else "—"
        print(
            f"  {str(f.get('ticker', ''))[:26]:<26} {str(f.get('side', '')):<4} "
            f"{str(f.get('action', '')):<5} {str(cnt):>5} {str(pc):>4} {notional:>9} "
            f"{motor[:18]:<18} {pnl:>9}"
        )


def _print_summary(fills: list[dict], trades_idx: dict[str, Trade]) -> None:
    """Resumen agregado: flujo de caja por fills + P&L/win-rate por motor desde la DB."""
    print(f"\n{'=' * 64}\n📊 RESUMEN AGREGADO")

    # --- Flujo de caja desde los fills (lo realmente ejecutado en la cuenta) ---
    spent = received = 0  # ¢
    by_motor: dict[str, dict[str, int]] = defaultdict(lambda: {"fills": 0, "contratos": 0})
    for f in fills:
        cnt = _fill_count(f) or 0
        pc = _fill_price_cents(f) or 0
        cash = cnt * pc
        if str(f.get("action", "")).lower() == "sell":
            received += cash
        else:
            spent += cash
        t = _trade_for_fill(f, trades_idx)
        motor = t.strategy if t else "?"
        by_motor[motor]["fills"] += 1
        by_motor[motor]["contratos"] += cnt

    print(
        f"  fills: {len(fills)}  |  comprado: {_cents(spent)}  vendido: {_cents(received)}  "
        f"flujo_neto: {_cents(received - spent)}"
    )
    if by_motor:
        print("  por motor (fills):")
        for motor, d in sorted(by_motor.items()):
            print(f"    {motor:<20} {d['fills']:>3} fills  {d['contratos']:>5} contratos")

    # --- P&L realizado + win-rate desde la DB (trades settled con pnl_cents) ---
    uniq = {id(t): t for t in trades_idx.values()}.values()
    settled = [t for t in uniq if t.pnl_cents is not None]
    if not settled:
        print("\n  P&L realizado: (aún no hay trades con pnl_cents en la DB — sin settlements)")
        return
    pnl_by_motor: dict[str, dict[str, Any]] = defaultdict(lambda: {"pnl": 0, "wins": 0, "n": 0})
    for t in settled:
        d = pnl_by_motor[t.strategy]
        d["pnl"] += t.pnl_cents or 0
        d["n"] += 1
        if (t.pnl_cents or 0) > 0:
            d["wins"] += 1
    total_pnl = sum(d["pnl"] for d in pnl_by_motor.values())
    total_n = sum(d["n"] for d in pnl_by_motor.values())
    total_wins = sum(d["wins"] for d in pnl_by_motor.values())
    print("\n  P&L realizado por motor (trades settled):")
    print(f"  {'motor':<20} {'n':>4} {'win%':>6} {'pnl':>10}")
    for motor, d in sorted(pnl_by_motor.items()):
        wr = 100 * d["wins"] / d["n"] if d["n"] else 0
        print(f"  {motor:<20} {d['n']:>4} {wr:>5.0f}% {_cents(d['pnl']):>10}")
    wr_total = 100 * total_wins / total_n if total_n else 0
    print(f"  {'TOTAL':<20} {total_n:>4} {wr_total:>5.0f}% {_cents(total_pnl):>10}")


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
