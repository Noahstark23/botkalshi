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

# El repo exige Python >=3.12 y el python del sistema del runner web es 3.11:
# instalar directo fallaba en TODAS las sesiones ("requires a different Python").
# Fix: venv con python3.12, primero en el PATH y persistido para el resto de la
# sesion via CLAUDE_ENV_FILE. .venv/ ya esta ignorado por git.
VENV="$CLAUDE_PROJECT_DIR/.venv"
[ -x "$VENV/bin/python" ] || python3.12 -m venv "$VENV"
export PATH="$VENV/bin:$PATH"
echo "export PATH=\"$VENV/bin:\$PATH\"" >> "$CLAUDE_ENV_FILE"

echo "[session-start] Installing kalshi-bot deps (pip install -e .[dev])..."
pip install --quiet --timeout 60 -e ".[dev]"

# Sanity check: los dos comandos que usa CI deben existir y las deps importar.
python -c "import loguru, pydantic, websockets, httpx" \
  || { echo "[session-start] ERROR: runtime deps missing after install"; exit 1; }
ruff --version >/dev/null || { echo "[session-start] ERROR: ruff not available"; exit 1; }
pytest --version >/dev/null || { echo "[session-start] ERROR: pytest not available"; exit 1; }

echo "[session-start] OK — deps installed; pytest y ruff listos (mismos comandos que CI)."
