"""CLI READ-ONLY de conciliación: snapshot de cuenta (JSON) ↔ DB del bot (SQLite ro).

Uso (operador; no llama a Kalshi — el snapshot lo produce read_research_account.py):

    python scripts/read_research_account.py --output account.json  # solo GET; exige flags
                                                                  # de ejecución en false EN
                                                                  # SU shell (no source del env)
    python infra/digitalocean-shadow/reconcile_account.py \\
        --account account.json --db /app/data/trades.db --out reconciliation.json

Código de salida: 0 = RECONCILED, 3 = NOT_RECONCILED (ver `blockers`), 2 = entrada inválida.
Nunca escribe en la DB (abre con mode=ro y query_only). No solicita checkpoints ni
altera el historial. El resultado se limita a los snapshots provistos, no certifica
el estado actual de la cuenta ni su cobertura histórica completa.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(_BASE))
sys.path.insert(0, str(_BASE.parents[1]))

from account_reader import atomic_write  # noqa: E402
from reconciliation import (  # noqa: E402
    ReconciliationInputError,
    load_ledger_rows,
    reconcile,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Conciliación read-only exchange ↔ ledger.")
    ap.add_argument("--account", required=True, type=Path)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    try:
        account = json.loads(args.account.read_text(encoding="utf-8"))
        report = reconcile(account, load_ledger_rows(args.db))
    except (OSError, json.JSONDecodeError, ReconciliationInputError) as exc:
        print(f"entrada inválida: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.out:
        atomic_write(args.out, report)
    brief = {
        k: report[k] for k in ("status", "blockers", "ledger_open_tickers", "ledger_pending_rows")
    }
    brief["fee_tally"] = report["fee_validation"]["tally"]
    brief["position_discrepancies"] = report["position_discrepancies"]
    print(json.dumps(brief, indent=2, sort_keys=True))
    return 0 if report["status"] == "RECONCILED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
