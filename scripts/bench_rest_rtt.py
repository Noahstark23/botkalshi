#!/usr/bin/env python3
"""
Benchmark de RTT PURO DE RED de get_orderbook contra Kalshi (modelo híbrido NBA).

A diferencia de bench_rest_arb_path.py (que mide el path COMPLETO: GET + parse +
detect_binary_arb), este script aísla **solo el round-trip de red** del
get_orderbook — la latencia de la pata de EJECUCIÓN REST del modelo híbrido
(Detección WS + Ejecución REST).

Escenario: 3-4 tickers NBA simultáneos sostenidos. Reporta mediana/P95/P99 del RTT.

CRITERIO DE DECISIÓN (fijado de antemano por el CTO):
    P95 < 150ms bajo carga NBA → híbrido viable, se procede a V2-light.
    P95 ≥ 150ms → repensar el lado de ejecución antes de implementar.

⚠️ NO EJECUTAR EN HORARIO DE BAJO TRÁFICO. El RTT de las ~4am no refleja la
carga real. Lo ejecuta el operador durante horario PICO de partidos NBA en vivo.

READ-ONLY: solo GETs de orderbook. No coloca órdenes, no parsea para arbitraje
(eso lo mide el otro script), no toca V2/flag/DB. Aislado en scripts/.

Uso (lo corre el operador en horario NBA):
    PYTHONPATH=. poetry run python scripts/bench_rest_rtt.py \\
        --tickers KXNBA-26-AAA KXNBA-26-BBB KXNBA-26-CCC \\
        --rate-hz 10 --duration 30
"""
from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass, field

from src.clients.kalshi_rest import KalshiRateLimitError, KalshiRestClient


@dataclass
class RttStats:
    rtt_ms: list[float] = field(default_factory=list)
    n_429: int = 0
    n_errors: int = 0
    n_ok: int = 0


async def _ticker_loop(
    client: KalshiRestClient,
    ticker: str,
    rate_hz: float,
    deadline: float,
    stats: RttStats,
) -> None:
    """Consulta un ticker a rate_hz sostenido midiendo SOLO el RTT del GET."""
    interval = 1.0 / rate_hz
    while time.monotonic() < deadline:
        cycle_start = time.monotonic()
        t0 = time.perf_counter()
        try:
            # Solo el round-trip de red. Sin parse, sin arb: RTT puro.
            await client.get_orderbook(ticker)
            stats.rtt_ms.append((time.perf_counter() - t0) * 1000.0)
            stats.n_ok += 1
        except KalshiRateLimitError:
            stats.n_429 += 1
        except Exception:
            stats.n_errors += 1

        sleep_for = interval - (time.monotonic() - cycle_start)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


def _report(per_ticker: dict[str, RttStats], duration: float, rate_hz: float) -> None:
    agg = RttStats()
    for s in per_ticker.values():
        agg.rtt_ms.extend(s.rtt_ms)
        agg.n_429 += s.n_429
        agg.n_errors += s.n_errors
        agg.n_ok += s.n_ok

    total = agg.n_ok + agg.n_429 + agg.n_errors
    rtt = sorted(agg.rtt_ms)

    def pct(p: float) -> float:
        if not rtt:
            return float("nan")
        k = min(len(rtt) - 1, int(round(p / 100.0 * (len(rtt) - 1))))
        return rtt[k]

    print("\n" + "=" * 60)
    print("BENCHMARK — RTT PURO DE RED de get_orderbook (ejecución híbrida)")
    print("=" * 60)
    print(f"Tickers concurrentes : {len(per_ticker)}")
    print(f"Rate objetivo        : {rate_hz} Hz/ticker ({rate_hz * len(per_ticker):.0f} Hz agregado)")
    print(f"Duración             : {duration}s")
    print(f"Requests totales     : {total}  (ok={agg.n_ok}, 429={agg.n_429}, err={agg.n_errors})")
    print("-" * 60)
    if rtt:
        print("RTT puro de red:")
        print(f"  mediana (P50) : {pct(50):7.1f} ms")
        print(f"  P95           : {pct(95):7.1f} ms")
        print(f"  P99           : {pct(99):7.1f} ms")
        print(f"  max           : {rtt[-1]:7.1f} ms")
    else:
        print("  (sin RTT exitosos registrados)")
    print("-" * 60)
    pct_429 = (agg.n_429 / total * 100.0) if total else 0.0
    print(f"429 rate             : {pct_429:.2f}%  ({agg.n_429}/{total})")
    print(f"Errores              : {agg.n_errors}")
    print("=" * 60)
    p95 = pct(95)
    viable = bool(rtt) and p95 < 150.0
    print(f"CRITERIO: P95 < 150ms bajo carga NBA → {'HÍBRIDO VIABLE (proceder a V2-light)' if viable else 'REPENSAR ejecución antes de implementar'}")
    print(f"  P95={p95:.1f}ms")
    print("=" * 60 + "\n")


async def _main() -> None:
    ap = argparse.ArgumentParser(description="RTT puro de red de get_orderbook (read-only).")
    ap.add_argument("--tickers", nargs="+", required=True, help="3-4 tickers NBA en vivo")
    ap.add_argument("--rate-hz", type=float, default=10.0, help="Consultas/seg por ticker (default 10)")
    ap.add_argument("--duration", type=float, default=30.0, help="Duración sostenida en seg (default 30)")
    args = ap.parse_args()

    if len(args.tickers) < 3:
        print("⚠️  Se esperan 3-4 tickers NBA simultáneos para reflejar la carga real.")

    print(f"RTT benchmark: {len(args.tickers)} tickers @ {args.rate_hz}Hz por {args.duration}s")
    print(f"Tickers: {', '.join(args.tickers)}")
    print("⚠️  Correr en horario PICO NBA. El RTT de bajo tráfico NO sirve para decidir.")
    print("READ-ONLY — solo GETs.\n")

    per_ticker: dict[str, RttStats] = {t: RttStats() for t in args.tickers}
    deadline = time.monotonic() + args.duration

    async with KalshiRestClient() as client:
        await asyncio.gather(
            *(_ticker_loop(client, t, args.rate_hz, deadline, per_ticker[t]) for t in args.tickers)
        )

    _report(per_ticker, args.duration, args.rate_hz)


if __name__ == "__main__":
    asyncio.run(_main())
