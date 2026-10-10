"""CLI del simulador: reproduce un JSONL de eventos contra `paper_engine`. Sin órdenes.

Uso (histórico o una captura en vivo volcada a archivo; ambos usan el mismo formato):

    python infra/digitalocean-shadow/paper_replay.py \\
        --db /var/lib/botkalshi-research/paper.sqlite3 \\
        --capital 100.00 \\
        --input eventos.jsonl

Cada línea es UN objeto, uno de:
  {"type": "signal", "signal": {<botkalshi-paper-signal-v1>}, "book": {<book>}}
  {"type": "m1_book", "cycle_id": "...", "signal_at": "...", "max_count": 1,
   "book": {<book>}}           → corre el detector PURO de M1 (detect_binary_arb) y,
                                 si hay arb, simula las DOS patas como señales
  {"type": "settle", "ticker": "...", "result": "yes" | "no"}
  {"type": "settle", "ticker": "...", "yes_payout_cents": 0..100}
  {"type": "m5_quote", "quote": {...}, "book": {...}}
  {"type": "m5_observe", "quote_id": "...", "observation_id": "...", "at": "...", "book": {...}}
  {"type": "m5_cancel", "quote_id": "...", "at": "..."}
  {"type": "m5_settle", "quote_id": "...", "yes_payout_cents": 0 | 50 | 100}
  M5 usa su propio BBO y cartera: ver docs/simulador-cartera-m5-2026-10-09.md.

Book: {"ticker": "...", "observed_at": "<ISO aware>", "yes_bids": [[c, qty], ...],
       "no_bids": [[c, qty], ...], "max_book_age_sec": 10 (opcional)}

Sale con código 0 y un resumen JSON (PnL por motor). Una línea inválida aborta con
código 2 y número de línea: un replay que saltea basura en silencio no es evidencia.
`--capital` inicializa el banco ficticio; repetirlo con OTRO monto falla (el banco no
se redefine en silencio — ver simulation_bank).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))
sys.path.insert(0, str(_BASE.parents[1]))

import m5_paper_ledger  # noqa: E402
import paper_engine  # noqa: E402
import simulation_bank  # noqa: E402

from src.math.arbitrage import detect_binary_arb  # noqa: E402


def m1_signals_from_book(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Pasa el book por el detector PURO de M1. Arb → dos señales (YES y NO) al ask.

    Usa el MISMO detector que el motor desplegado (src.math.arbitrage) y el mismo
    `max_count`: el sim mide la señal que M1 emitiría, no una versión mejorada.
    """
    book = event["book"]
    yes_bids = sorted((lv for lv in book["yes_bids"] if lv[1] > 0), key=lambda x: -x[0])
    no_bids = sorted((lv for lv in book["no_bids"] if lv[1] > 0), key=lambda x: -x[0])
    if not yes_bids or not no_bids:
        return []
    yes_ask, yes_size = 100 - no_bids[0][0], no_bids[0][1]
    no_ask, no_size = 100 - yes_bids[0][0], yes_bids[0][1]
    if not (1 <= yes_ask <= 99 and 1 <= no_ask <= 99):
        return []
    opp = detect_binary_arb(
        book["ticker"], yes_ask, yes_size, no_ask, no_size, max_count=event.get("max_count", 1)
    )
    if opp is None:
        return []
    signals = []
    for leg in opp.legs:
        signals.append(
            {
                "schema_version": paper_engine.SCHEMA_VERSION_SIGNAL,
                "signal_id": f"m1-{event['cycle_id']}-{book['ticker']}-{leg.side}",
                "origin": "M1",
                "cycle_id": event["cycle_id"],
                "ticker": book["ticker"],
                "side": leg.side,
                "limit_price_cents": leg.price_cents,
                "count": leg.count,
                "signal_at": event["signal_at"],
            }
        )
    return signals


def run(db: Path, capital: str, lines: list[str]) -> dict[str, Any]:
    simulation_bank.init_bank(db, initial_capital_usd=capital)
    processed: list[dict[str, Any]] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise paper_engine.PaperEngineError("event must be an object")
            kind = event.get("type")
            if kind == "signal":
                processed.append(paper_engine.process_signal(db, event["signal"], event["book"]))
            elif kind == "m1_book":
                for sig in m1_signals_from_book(event):
                    processed.append(paper_engine.process_signal(db, sig, event["book"]))
            elif kind == "settle":
                paper_engine.settle_market(
                    db,
                    ticker=event["ticker"],
                    market_result=event.get("result"),
                    yes_payout_cents=event.get("yes_payout_cents"),
                )
            elif kind == "m5_quote":
                m5_paper_ledger.admit_quote(db, event["quote"], event["book"])
            elif kind == "m5_observe":
                m5_paper_ledger.observe(
                    db,
                    event["quote_id"],
                    observation_id=event["observation_id"],
                    at=event["at"],
                    book=event["book"],
                )
            elif kind == "m5_cancel":
                m5_paper_ledger.cancel_quote(db, event["quote_id"], at=event["at"])
            elif kind == "m5_settle":
                m5_paper_ledger.settle_quote(
                    db, event["quote_id"], yes_payout_cents=event["yes_payout_cents"]
                )
            else:
                raise paper_engine.PaperEngineError(f"type desconocido: {kind!r}")
        except (
            KeyError,
            TypeError,
            ValueError,
            paper_engine.PaperEngineError,
            simulation_bank.SimulationBankError,
        ) as exc:
            raise SystemExit(f"línea {n}: {type(exc).__name__}: {exc}") from exc
    statuses: dict[str, int] = {}
    for r in processed:
        statuses[r["status"]] = statuses.get(r["status"], 0) + 1
    return {
        "fills_by_status": dict(sorted(statuses.items())),
        "summary": paper_engine.summary(db),
        "summary_scope": "paper_engine (excludes M5); see m5_summary and shared bank",
        "m5_summary": m5_paper_ledger.summary(db),
        "bank": simulation_bank.get_snapshot(db) | {"active_reservations": "omitted"},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay de paper trading (SIMULATION_ONLY).")
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--capital", required=True, help="Capital FICTICIO, p. ej. 100.00")
    ap.add_argument("--input", required=True, type=Path)
    args = ap.parse_args(argv)
    try:
        report = run(args.db, args.capital, args.input.read_text(encoding="utf-8").splitlines())
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
