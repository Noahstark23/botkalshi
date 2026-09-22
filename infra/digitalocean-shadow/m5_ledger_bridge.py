"""Verify a local durable M5 shadow row before reserving FICTIONAL capital.
The source is an explicit, existing SQLite file opened read-only. Only
mm_shadow_fills and its schema are read. No account/engine/network imports.
Verification describes the row snapshot read, not a deployed producer, public
market observation, current source immutability, or financial authorization.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
from fractions import Fraction
from pathlib import Path
import math
import re
import sqlite3
from typing import Any

import simulation_bank
from m5_shadow_fill_review import SCHEMA_VERSION_INPUT, review_m5_fill

FIELDS = ('id', 'experiment_id', 'ticker', 'side', 'price_cents', 'count',
          'fee_effective_cents', 'fee_model', 'metric_version', 'fee_source',
          'fee_type', 'fee_multiplier')
VERIFIED = 'LEDGER_VERIFIED_SHADOW_FILL'
_DECIMAL = re.compile(r'[+]?[0-9]{1,4}(?:\.[0-9]{1,24})?')


class LedgerEvidenceError(Exception):
    """Source evidence is unusable; no reservation may be created."""

def _multiplier(value: object) -> Fraction:
    # Bound integers BEFORE str(); never expand exponent notation or bools.
    if type(value) is int:
        if not 0 < value <= 1000:
            raise LedgerEvidenceError('INVALID_MULTIPLIER')
    elif type(value) is float:
        if not math.isfinite(value) or not 0 < value <= 1000:
            raise LedgerEvidenceError('INVALID_MULTIPLIER')
    elif type(value) is not str:
        raise LedgerEvidenceError('INVALID_MULTIPLIER')
    text = str(value)
    if len(text) > 32 or _DECIMAL.fullmatch(text) is None:
        raise LedgerEvidenceError('INVALID_MULTIPLIER')
    result = Fraction(text)
    if not 0 < result <= 1000:
        raise LedgerEvidenceError('INVALID_MULTIPLIER')
    return result


def _review(raw: object, now: datetime | None) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get('fill'), dict):
        raise LedgerEvidenceError('INVALID_DECLARATION')
    _multiplier(raw['fill'].get('fee_multiplier'))
    review = review_m5_fill(raw, now=now)
    if review['status'] != 'ACCEPTED':
        raise LedgerEvidenceError('INVALID_DECLARATION')
    return review

def _canonical_evidence(review: dict[str, Any]) -> dict[str, Any]:
    evidence = dict(review['evidence'])
    multiplier = _multiplier(evidence['fee_multiplier'])
    evidence['fee_multiplier'] = f'{multiplier.numerator}/{multiplier.denominator}'
    return evidence


def _paths(source: str | Path, bank: str | Path) -> tuple[Path, Path]:
    source_path, bank_path = Path(source).absolute(), Path(bank).absolute()
    for path in (source_path, bank_path):
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise LedgerEvidenceError('SYMLINK_PATH')
    if not source_path.is_file():
        raise LedgerEvidenceError('SOURCE_NOT_FOUND')
    if source_path == bank_path or (bank_path.exists() and source_path.samefile(bank_path)):
        raise LedgerEvidenceError('SOURCE_BANK_SAME_FILE')
    return source_path, bank_path


def _read_row(source: Path, experiment_id: str, fill_id: int) -> dict[str, Any]:
    # URI escaping preserves literal '?' / '#' in file names. Do not use
    # immutable=1: a live WAL must not be silently ignored.
    uri = source.resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=3)) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('PRAGMA trusted_schema=OFF')
        con.execute('BEGIN')
        try:
            kinds = con.execute('SELECT type FROM sqlite_master WHERE name=?',
                                ('mm_shadow_fills',)).fetchall()
            if kinds != [('table',)]:
                raise LedgerEvidenceError('SOURCE_TABLE_REQUIRED')
            columns = {row[1] for row in con.execute('PRAGMA table_info(mm_shadow_fills)')}
            if not set(FIELDS).issubset(columns):
                raise LedgerEvidenceError('SOURCE_SCHEMA_INCOMPLETE')
            rows = con.execute(
                'SELECT ' + ','.join(FIELDS) +
                ' FROM mm_shadow_fills WHERE experiment_id=? AND id=? LIMIT 2',
                (experiment_id, fill_id),
            ).fetchall()
            if len(rows) != 1:
                raise LedgerEvidenceError('SOURCE_ROW_MISSING_OR_DUPLICATED')
            return dict(zip(FIELDS, rows[0], strict=True))
        finally:
            con.execute('ROLLBACK')


def _result() -> dict[str, Any]:
    return {
        'schema': 'botkalshi-m5-ledger-bridge-v1', 'status': 'BLOCKED',
        'reason_codes': [], 'review': None, 'reservation': None,
        'mode': 'SIMULATION_ONLY', 'capital_source': 'FICTIONAL_TEST_CAPITAL',
        'evidence_state': 'NOT_LEDGER_VERIFIED',
        'execution_authorized': False, 'order_capability_present': False,
        'real_entry_eligible': False,
    }

def apply_ledger_verified_m5_fill(
    source_db_path: str | Path, bank_db_path: str | Path, raw: object,
    *, now: datetime | None = None,
) -> dict[str, Any]:
    """Read and compare one durable shadow fill, then reserve its fictional risk.

    The source read is a consistent snapshot; it is NOT an atomic transaction
    across both databases. Producers must not mutate risk fields of an accepted
    fill: a later changed replay is a conflict, not an automatic reconciliation.
    No timestamps/absolute paths enter the idempotent stored evidence.
    """
    result = _result()
    try:
        declared = _review(raw, now)
        source, bank = _paths(source_db_path, bank_db_path)
        identity = declared['evidence']
        row = _read_row(source, identity['experiment_id'], identity['fill_id'])
        durable = _review({'schema_version': SCHEMA_VERSION_INPUT, 'fill': row}, now)
        evidence = _canonical_evidence(durable)
        if evidence != _canonical_evidence(declared):
            raise LedgerEvidenceError('SOURCE_DECLARATION_MISMATCH')
    except LedgerEvidenceError as exc:
        result['reason_codes'] = [str(exc)]
        return result
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        result['reason_codes'] = ['SOURCE_OR_DECLARATION_UNUSABLE']
        return result
    evidence.update({
        'evidence_state': VERIFIED,
        'exactness': 'EXACT_CONDITIONAL_ON_VERIFIED_LOCAL_SHADOW_ROW',
        'source_record': {'table': 'mm_shadow_fills',
                          'experiment_id': row['experiment_id'], 'id': row['id']},
    })
    verified_review = dict(durable, evidence=evidence, evidence_state=VERIFIED)
    result.update(review=verified_review, evidence_state=VERIFIED)
    try:
        result['reservation'] = simulation_bank.reserve_with_evidence(
            bank, idempotency_key=durable['idempotency_key'], origin='M5',
            cycle_id=durable['cycle_id'], amount_usd=durable['max_loss_usd'],
            evidence=evidence,
        )
    except simulation_bank.SimulationBankError as exc:
        result['reason_codes'] = ['BANK_REJECTED', type(exc).__name__]
        return result
    result['status'] = 'RESERVED'
    return result
