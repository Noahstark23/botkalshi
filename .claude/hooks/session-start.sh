#!/bin/bash
# SessionStart hook — Claude Code on the web.
# Instala las deps del bot (runtime + dev) para que pytest/ruff/mypy funcionen
# desde el primer turno de la sesión. Idempotente: pip no reinstala lo que ya está.
set -euo pipefail

# Solo en el entorno remoto (Claude Code web). En local no tocamos nada.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

echo "[session-start] Installing kalshi-bot deps (pip install -e .[dev])..."
pip install --quiet -e ".[dev]"

# Sanity check: los dos comandos que usa CI deben existir y las deps importar.
python -c "import loguru, pydantic, websockets, httpx" \
  || { echo "[session-start] ERROR: runtime deps missing after install"; exit 1; }
ruff --version >/dev/null || { echo "[session-start] ERROR: ruff not available"; exit 1; }
pytest --version >/dev/null || { echo "[session-start] ERROR: pytest not available"; exit 1; }

echo "[session-start] OK — deps installed; pytest y ruff listos (mismos comandos que CI)."
