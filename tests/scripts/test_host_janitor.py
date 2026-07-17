"""
scripts/host_janitor.sh — janitor del HOST (incidente disco-lleno 2026-07-10).

Test-guard: el janitor JAMÁS debe volverse peligroso. La regla dura es que borra cruft de
Docker + logs rotados, pero NUNCA toca volúmenes (donde vive trades.db). Si alguien agrega
`--volumes` o un `rm -rf` amplio en el futuro, este test lo frena.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "host_janitor.sh"


def test_janitor_exists_and_is_bash():
    assert SCRIPT.exists(), "falta scripts/host_janitor.sh"
    assert SCRIPT.read_text().startswith("#!/usr/bin/env bash")


def test_janitor_never_touches_volumes():
    body = SCRIPT.read_text()
    # --volumes borraría el volumen con la DB; jamás debe aparecer.
    assert "--volumes" not in body
    # tampoco debe inspeccionar/borrar volúmenes ni hacer un rm amplio.
    assert "docker volume" not in body
    assert "rm -rf /" not in body


def test_janitor_prunes_expected_cruft():
    body = SCRIPT.read_text()
    assert "docker image prune" in body
    assert "docker builder prune" in body
    assert "docker container prune" in body
    # resiliente: sin `set -e` (un prune que falle no debe abortar el resto).
    assert "set -uo pipefail" in body
    assert "set -euo pipefail" not in body
