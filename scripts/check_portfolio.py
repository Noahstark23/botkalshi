"""
Chequeo de cartera real de Kalshi — verifica el fix de firma y reconcilia el P&L.

Tras el fix del querystring en la firma (2026-06-17), `positions`/`fills` deberían dar
200. Este script lo confirma y de paso lista la cartera real: balance, posiciones
abiertas (con su exposición y PnL realizado si Kalshi lo expone) y los fills recientes.

Read-only: solo GET /portfolio/{balance,positions,fills}. NO coloca ni cancela órdenes.

Uso (en Coolify, tras redeploy):
    python scripts/check_portfolio.py
"""

from __future__ import annotations

import asyncio

from src.clients.kalshi_rest import KalshiRestClient


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


async def run() -> int:
    ok = True
    async with KalshiRestClient() as kc:
        # 1) balance (siempre funcionó — control)
        try:
            bal = await kc.get_balance()
            print(f"✅ balance: {bal}")
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

        # 3) fills (también daba 401)
        try:
            resp = await kc.get_fills(limit=50)
            fills = resp.get("fills", []) or []
            print(f"\n✅ fills: 200 OK — {len(fills)} fill(s) recientes")
            if fills:
                # Dump CRUDO del 1er fill → vemos las claves/valores reales (sin adivinar).
                print(f"  (claves 1er fill: {sorted(fills[0].keys())})")
            print(f"  {'ticker':<30} {'side':<4} {'act':<5} {'cant':>5} {'precio¢':>8}")
            for f in fills[:25]:
                side = str(f.get("side", "")).lower()
                # cantidad: count int, o count_fp fixed-point.
                count = _num(f.get("count"))
                if count is None:
                    count = _num(f.get("count_fp"))
                # precio del lado tradeado: yes_price/no_price (¢) según side; fallbacks.
                price = f.get("yes_price") if side == "yes" else f.get("no_price")
                if price is None:
                    price = f.get("price") or f.get("price_dollars") or f.get("yes_price")
                price_c = _num(price)
                print(
                    f"  {str(f.get('ticker', ''))[:30]:<30} {side:<4} "
                    f"{str(f.get('action', '')):<5} {str(count):>5} {str(price_c):>8}"
                )
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

    print(f"\n{'=' * 60}")
    if ok:
        print("✅ FIX CONFIRMADO: el bot lee balance + positions + fills (cartera visible).")
        return 0
    print("❌ Algún endpoint sigue fallando — ¿redeploy pendiente? Pegame el error.")
    return 1


def main() -> None:
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
