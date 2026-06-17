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
            for f in fills[:20]:
                print(
                    f"  {str(f.get('ticker', '')):<34} {f.get('side', ''):<4} "
                    f"{f.get('action', ''):<5} count={f.get('count')} @ {f.get('yes_price', f.get('price'))}"
                )
        except Exception as e:
            ok = False
            print(f"\n❌ fills: {type(e).__name__}: {e}  ← el fix de firma NO está activo")

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
