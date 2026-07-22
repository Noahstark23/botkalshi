# botkalshi — guía para Claude Code

Bot de trading algorítmico para mercados de predicción de Kalshi (Python 3.12,
asyncio). Varios motores de estrategia + captura de datos + monitoreo/riesgo.

## Contexto obligatorio

- `KALSHI_BOT_CONTEXT.md` — historia, decisiones y lecciones del proyecto.
  Consultarlo antes de cambios de arquitectura.
- Subagente `bot-data` (`.claude/agents/bot-data.md`) — usarlo para diagnóstico
  de motores, salud del feed WS, incidentes y datos del bot.

## Comandos (los mismos que CI — `.github/workflows/ci.yml`)

```bash
pip install -e ".[dev]"        # lo hace el SessionStart hook en la web
ruff check src/ tests/         # lint
ruff format --check src/ tests/  # formato
pytest                         # tests (asyncio_mode=auto)
mypy src/                      # types (no bloqueante en CI)
```

Test dirigido: `pytest tests/strategies/motor_1_arbitrage/ -q`.

## Reglas del repo

- Fail-loud: nada de `try/except Exception: pass` (lección del incidente
  2026-05-09). Los errores de orderbook (`SidGapError`, `OrderbookDesyncError`)
  propagan; el recovery se inicia ANTES del raise.
- El protocolo WS se valida contra `tests/fixtures/ws/` (mensajes reales
  capturados) — no contra memoria ni suposiciones. Leer su README.
- Convención de commits: `feat(motorN)/fix(...)` en español, como el historial.
- `TRADING_ENABLED` y flags de motores viven en env vars (ver `.env.example`);
  nunca activarlos en código ni en tests.
