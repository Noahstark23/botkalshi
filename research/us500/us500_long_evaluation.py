"""Ensambla tramos offline en una sola sesión, sin resetear capital ni estrategia.

Recibe capturas y costes explícitos, nunca lee el recolector vivo ni hace red.
El ensamblado no acredita cierre de mercado, cobertura, independencia o ventaja.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from us500_engine import DataError, body_digest, save_new
from us500_runner import MAX_FRAMES, replay
from us500_session import build_session, load_package

SCHEMA = "us500-long-evaluation-v1"
MAX_SEGMENTS = 8


def assemble(bundle):
    if (
        type(bundle) is not dict
        or set(bundle) != {"schema", "run_id", "ledger", "strategy", "segments"}
        or bundle["schema"] != SCHEMA
    ):
        raise DataError("INVALID_LONG_EVALUATION")
    segments = bundle["segments"]
    if type(segments) is not list or not 1 <= len(segments) <= MAX_SEGMENTS:
        raise DataError("INVALID_EVALUATION_SEGMENTS")
    snapshots, costs, boundaries = [], [], []
    for index, segment in enumerate(segments, 1):
        if (
            type(segment) is not dict
            or set(segment) != {"name", "snapshots", "interval_costs"}
            or segment["name"] != f"part{index:02d}"
        ):
            raise DataError("INVALID_EVALUATION_SEGMENT_ORDER")
        captures, supplied = segment["snapshots"], segment["interval_costs"]
        if (
            type(captures) is not list
            or not captures
            or type(supplied) is not list
            or len(captures) != len(supplied)
        ):
            raise DataError("EXPLICIT_SEGMENT_INPUTS_REQUIRED")
        if len(snapshots) + len(captures) > MAX_FRAMES:
            raise DataError("INVALID_FRAME_COUNT")
        boundaries.append(
            {
                "name": segment["name"],
                "first_frame": len(snapshots),
                "frames": len(captures),
                "input_sha256": body_digest(segment),
            }
        )
        snapshots.extend(captures)
        costs.extend(supplied)
    # Una sola validación incluye gaps, identidad y costes entre los tramos.
    # No se reinicia warmup, señal pendiente, equity ni el ledger por segmento.
    session = build_session(
        snapshots, bundle["ledger"], bundle["strategy"], costs, bundle["run_id"]
    )
    session["evaluation_segments"] = boundaries
    return session


def evaluate(bundle, database):
    session = assemble(bundle)
    report = replay(session, database)
    report["evaluation_segments"] = session["evaluation_segments"]
    report["capital_resets_between_segments"] = 0
    report["evaluation_bundle_sha256"] = body_digest(bundle)
    report["capture_audit_required"] = True
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        raise DataError("OUTPUT_ALREADY_EXISTS")
    report = evaluate(load_package(args.input), args.database)
    save_new(args.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
