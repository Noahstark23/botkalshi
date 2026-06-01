#!/usr/bin/env python3
"""
Benchmark del path COMPLETO de arbitraje vía REST (Opción 2 vs V2).

Mide la latencia real de decisión del Motor 1: desde que se dispara la consulta
de un ticker hasta que el orderbook está parseado y la condición de arbitraje
binario evaluada — NO solo el round-trip HTTP.

Path medido por iteración (lo que decide en producción):
    get_orderbook(ticker)          # REST GET
    → parse a OrderbookState        # apply_snapshot (mismo parser que el hot path)
    → derivar asks de los bids      # yes_ask = 100 - best_no_bid, etc.
    → detect_binary_arb(...)        # evaluación de la condición de arbitraje

Escenario REAL simulado (no proxy simplificado):
    - N tickers concurrentes sostenidos a ~RATE_HZ cada uno (default 3 tickers @ 20Hz).
    - Contra mercados con orderbook real y profundo (pasar tickers MLB en vivo).
    - Duración sostenida (default 30s) para capturar P99 bajo carga, no el mejor caso.

Reporta: media/P95/P99 del path completo, % de 429, throughput, y comportamiento
bajo la ráfaga concurrente.

READ-ONLY: solo GETs de orderbook. No coloca órdenes, no toca V2 ni el flag,
no escribe en la DB. Aislado en scripts/, fuera del hot path.

Uso:
    poetry run python scripts/bench_rest_arb_path.py \\
        --tickers KXMLB-26-XXX KXMLB-26-YYY KXMLB-26-ZZZ \\
        --rate-hz 20 --duration 30
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from dataclasses import dataclass, field

from src.clients.kalshi_rest import KalshiRateLimitError, KalshiRestClient
from src.math.arbitrage import detect_binary_arb
from src.strategies.motor_1_arbitrage.orderbook import OrderbookState


@dataclass
class Stats:
    """Acumulador de resultados por ticker, agregable."""

    latencies_ms: list[float] = field(default_factory=list)
    n_429: int = 0
    n_errors: int = 0
    n_arb_found: int = 0
    n_ok: int = 0


def _best_bid(levels: list) -> tuple[int | None, int | None]:
    """(price_cents, size) del bid más alto, o (None, None)."""
    best_p, best_s = None, None
    for lvl in levels:
        if not isinstance(lvl, (list, tuple)) or len(lvl) < 2:
            continue
        try:
            p, s = int(lvl[0]), int(lvl[1])
        except (ValueError, TypeError):
            continue
        if s > 0 and (best_p is None or p > best_p):
            best_p, best_s = p, s
    return best_p, best_s


def _evaluate_arb_path(ticker: str, raw_orderbook: dict) -> bool:
    """
    Parsea el orderbook REST y evalúa arbitraje binario. Replica el trabajo
    de CPU del hot path real (parse + derivación de asks + detect).

    Returns True si encontró una oportunidad de arbitraje.
    """
    ob = raw_orderbook.get("orderbook", {})
    yes_bids = ob.get("yes") or []
    no_bids = ob.get("no") or []

    # Cargar a OrderbookState (mismo parser/validación que usa el sistema real).
    state = OrderbookState(ticker)
    state.apply_snapshot({"seq": 0, "yes": yes_bids, "no": no_bids})

    yes_top = state.top_of_book("yes").best_bid
    no_top = state.top_of_book("no").best_bid
    if yes_top is None or no_top is None:
        return False

    # En Kalshi binario: comprar YES ≡ vender NO. El ask de un lado se deriva
    # del mejor bid del lado opuesto: yes_ask = 100 - best_no_bid.
    yes_ask = 100 - no_top.price_cents
    no_ask = 100 - yes_top.price_cents
    if not (1 <= yes_ask <= 99 and 1 <= no_ask <= 99):
        return False

    opp = detect_binary_arb(
        ticker,
        yes_ask_cents=yes_ask,
        yes_available_size=no_top.size,   # liquidez del lado que ejecuta el YES sintético
        no_ask_cents=no_ask,
        no_available_size=yes_top.size,
    )
    return opp is not None


async def _ticker_loop(
    client: KalshiRestClient,
    ticker: str,
    rate_hz: float,
    deadline: float,
    stats: Stats,
) -> None:
    """Consulta un ticker a rate_hz sostenido hasta deadline, midiendo el path completo."""
    interval = 1.0 / rate_hz
    while time.monotonic() < deadline:
        cycle_start = time.monotonic()
        t0 = time.perf_counter()
        try:
            raw = await client.get_orderbook(ticker)
            found = _evaluate_arb_path(ticker, raw)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            stats.latencies_ms.append(elapsed_ms)
            stats.n_ok += 1
            if found:
                stats.n_arb_found += 1
        except KalshiRateLimitError:
            stats.n_429 += 1
        except Exception:
            stats.n_errors += 1

        # Mantener el rate objetivo (no acumular drift si la request fue rápida).
        sleep_for = interval - (time.monotonic() - cycle_start)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)


def _report(per_ticker: dict[str, Stats], duration: float, rate_hz: float) -> None:
    agg = Stats()
    for s in per_ticker.values():
        agg.latencies_ms.extend(s.latencies_ms)
        agg.n_429 += s.n_429
        agg.n_errors += s.n_errors
        agg.n_arb_found += s.n_arb_found
        agg.n_ok += s.n_ok

    total_req = agg.n_ok + agg.n_429 + agg.n_errors
    lat = sorted(agg.latencies_ms)

    def pct(p: float) -> float:
        if not lat:
            return float("nan")
        k = min(len(lat) - 1, int(round(p / 100.0 * (len(lat) - 1))))
        return lat[k]

    print("\n" + "=" * 60)
    print("BENCHMARK — path completo de arbitraje vía REST")
    print("=" * 60)
    print(f"Tickers concurrentes : {len(per_ticker)}")
    print(f"Rate objetivo        : {rate_hz} Hz/ticker  ({rate_hz * len(per_ticker):.0f} Hz agregado)")
    print(f"Duración             : {duration}s")
    print(f"Requests totales     : {total_req}  (ok={agg.n_ok}, 429={agg.n_429}, err={agg.n_errors})")
    print("-" * 60)
    if lat:
        print("Latencia path completo (parse+arb incluido):")
        print(f"  media : {statistics.mean(lat):7.1f} ms")
        print(f"  P50   : {pct(50):7.1f} ms")
        print(f"  P95   : {pct(95):7.1f} ms")
        print(f"  P99   : {pct(99):7.1f} ms")
        print(f"  max   : {lat[-1]:7.1f} ms")
    else:
        print("  (sin latencias exitosas registradas)")
    print("-" * 60)
    pct_429 = (agg.n_429 / total_req * 100.0) if total_req else 0.0
    print(f"429 rate             : {pct_429:.2f}%  ({agg.n_429}/{total_req})")
    print(f"Errores              : {agg.n_errors}")
    print(f"Arb detectados       : {agg.n_arb_found}")
    print("=" * 60)
    # Criterio de decisión (definido por el owner).
    p99 = pct(99)
    verdict_ok = (lat and p99 < 150.0 and pct_429 < 1.0)
    print(f"CRITERIO: P99<150ms y 429<1% → {'Opción 2 (REST) GANA' if verdict_ok else 'V2 se justifica'}")
    print(f"  P99={p99:.1f}ms  429={pct_429:.2f}%")
    print("=" * 60 + "\n")


async def _main() -> None:
    ap = argparse.ArgumentParser(description="Benchmark REST del path de arbitraje (read-only).")
    ap.add_argument("--tickers", nargs="+", required=True, help="Tickers con orderbook real (ej. MLB en vivo)")
    ap.add_argument("--rate-hz", type=float, default=20.0, help="Consultas/seg por ticker (default 20)")
    ap.add_argument("--duration", type=float, default=30.0, help="Duración sostenida en seg (default 30)")
    args = ap.parse_args()

    print(f"Arrancando benchmark: {len(args.tickers)} tickers @ {args.rate_hz}Hz por {args.duration}s")
    print(f"Tickers: {', '.join(args.tickers)}")
    print("READ-ONLY — solo GETs de orderbook. No coloca órdenes.\n")

    per_ticker: dict[str, Stats] = {t: Stats() for t in args.tickers}
    deadline = time.monotonic() + args.duration

    async with KalshiRestClient() as client:
        await asyncio.gather(
            *(
                _ticker_loop(client, t, args.rate_hz, deadline, per_ticker[t])
                for t in args.tickers
            )
        )

    _report(per_ticker, args.duration, args.rate_hz)


if __name__ == "__main__":
    asyncio.run(_main())
