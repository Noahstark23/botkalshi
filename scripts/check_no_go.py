#!/usr/bin/env python3
"""
CI check: detect NO-GO patterns in source files.

NO-GO patterns (from 2026-05-09 incident: swallow-and-continue left stale
orderbook running for 3+ hours undetected):

  1. except Exception: pass    — silent swallow, masks real errors
  2. asyncio.Lock()            — not needed in single-threaded asyncio;
                                 its presence signals a threading assumption bug

Usage:
    python scripts/check_no_go.py                  # checks src/ (default)
    python scripts/check_no_go.py src/strategies/  # specific subtree
    python scripts/check_no_go.py path/to/file.py  # single file

Exit code 0 = clean, 1 = violations found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

NO_GO_PATTERNS: list[tuple[str, str]] = [
    # bare except+pass (with optional whitespace/comment between them not matching)
    (
        r"except\s+Exception\s*:\s*\n\s*pass\b",
        "silent exception swallow — except Exception: pass",
    ),
    (
        r"asyncio\.Lock\(\)",
        "asyncio.Lock() — unnecessary in single-threaded asyncio; signals a threading bug",
    ),
]

EXCLUDE_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", "node_modules", ".mypy_cache"})


def check_file(path: Path) -> list[tuple[int, str, str]]:
    """Return list of (line_no, description, matched_line) for violations."""
    violations: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return violations

    lines = text.splitlines()
    for pattern, description in NO_GO_PATTERNS:
        for match in re.finditer(pattern, text, re.MULTILINE):
            line_no = text[: match.start()].count("\n") + 1
            line_text = lines[line_no - 1].strip() if line_no <= len(lines) else ""
            violations.append((line_no, description, line_text))

    return violations


def iter_python_files(root: Path):
    if root.is_file():
        if root.suffix == ".py":
            yield root
        return
    for p in sorted(root.rglob("*.py")):
        if not any(ex in p.parts for ex in EXCLUDE_DIRS):
            yield p


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("src")

    py_files = list(iter_python_files(root))
    total_violations = 0

    for path in py_files:
        violations = check_file(path)
        for line_no, description, line_text in violations:
            print(f"{path}:{line_no}: NO-GO: {description}")
            print(f"  {line_text}")
            total_violations += 1

    if total_violations:
        print(f"\n{total_violations} NO-GO pattern(s) found. Fix before merging.")
        return 1

    file_count = len(py_files)
    print(f"OK: no NO-GO patterns in {file_count} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
