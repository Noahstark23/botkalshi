"""Make the repository's pure `src/` modules importable from the research service.

The unit runs `/usr/bin/python3 research_runner.py` from `infra/digitalocean-shadow/`
on a full checkout, with no venv and no PYTHONPATH (install.sh refuses one). Without
this, `import src...` in m1_observation failed under the system interpreter — found
2026-09-22 with `python3 -S`; the dev venv hid it through its editable install.

APPENDED, not prepended: nothing in the repo root may shadow a stdlib module. Only the
pure modules are imported through it (src.math, src.strategies.motor_5_mm.quoter /
shadow_fill); none of them needs a third-party package or opens a connection.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))
