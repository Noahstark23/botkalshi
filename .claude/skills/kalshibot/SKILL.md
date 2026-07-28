---
name: kalshibot
description: Contexto operativo del bot Kalshi (DINERO REAL) para cualquier agente que trabaje en este proyecto — estado, reglas duras, mapa de motores, comandos y protocolo de comunicación entre agentes. Usar al inicio de CUALQUIER tarea sobre botkalshi.
---

# Skill kalshibot — contexto y reglas para agentes

Sos un agente trabajando en **botkalshi**, bot multi-motor para Kalshi de Noel
Pineda. ⚠️ **Este bot opera DINERO REAL.** El escrutinio escala con el costo
del error.

## Qué es el proyecto (30 segundos)

Monolito asyncio Python contra Kalshi (mercados de predicción CFTC, USD fiat,
precios 0–100¢). SQLite + FastAPI + Docker en Coolify, droplet
`104.236.211.240`, health `:18080`. Convive con Polybot (`:18081`, el bot
Polymarket — OTRO proyecto, ver abajo). Estado según auditoría 2026-07-18:
sin fuente de alpha comprobada; M2 y REST apagándose, M8 (OFI) única promesa
viva juntando muestra.

## Reglas NO negociables

Las reglas completas viven en `CLAUDE.md` de este repo — leelo SIEMPRE antes
de tocar código. Resumen de las que más se violan por accidente:

1. **Merge a main ≈ deploy con dinero real.** Ningún merge de runtime sin OK
   explícito del humano. Fixes de incidente llevan "⚠️ NO mergear sin OK".
2. Todo lo que toca `risk/manager.py`, `auth/`, sizing, executores o flags
   `*_EXECUTION_ENABLED`/`TRADING_ENABLED` es bucket 🔴 (workflow completo +
   human gate). El humano asigna el bucket al inicio, no se reabre a mitad.
3. PROHIBIDO `asyncio.gather(..., return_exceptions=True)` en tareas críticas
   (Lección 7). Supervisor pattern + registro en `BotState`.
4. Estado mutable corrupto (orderbooks) → cuarentena/re-sync, nunca "sigue
   operando" (Lección 9). Tests verdes ≠ bug resuelto.
5. Nada sin tope: toda tabla/buffer nuevo nace con retención y presupuesto.
6. Kill-switch persistente sólo se limpia con `clear_kill_switch.py` (humano).
7. Fees SIEMPRE con la fórmula oficial exacta (`ceil(7·count·price·(100−price)/10000)`).

## Dos bots en el droplet — diferenciarlos SIEMPRE

| | **BOT KALSHI** (este proyecto) | **POLYBOT** (el otro) |
|---|---|---|
| Venue | Kalshi (USD, 0–100¢) | Polymarket (USDC, 0.00–1.00) |
| Puerto | **:18080** | :18081 |
| Dinero | **REAL** | Paper/shadow |
| Motores | M1,M2,M3,M5,M6,M8,M9,REST (M4 diseño) | UNO: `motor_1_arbitrage` |
| Identidad | `ticker`/`sid` | `condition_id`/`token_id` |
| Auth | RSA-PSS (`KALSHI-ACCESS-*`) | EIP-712/EVM |

Vocabulario centinela: si un análisis de ESTE bot menciona `condition_id`,
`token_id`, EIP-712, USDC 0–1.00 o "sampling markets" → se mezcló contexto de
Polybot; descartar y re-verificar. (El catálogo inverso vive en el repo de
Polybot: `docs/agents/motores_kalshi.md`.)

## Comandos de verificación (antes de proponer cualquier cambio)

```bash
pytest -q
ruff check src/ tests/ && ruff format --check src/ tests/
```

## Protocolo de comunicación entre agentes

Los agentes NO comparten memoria. El canal = repo + humano:
- Estado → `docs/handoff/`. Lecciones → `KALSHI_BOT_CONTEXT.md`.
- Material para otros agentes → `docs/agents/`.
- Agente web (navegador) → usa `docs/agents/web_agent_context.md` como brief;
  reporta como `## REPORTE KALSHI` con números literales; el humano pega el
  reporte en la sesión de Claude Code. Toda narrativa del agente web se
  re-verifica; sus números de endpoints son confiables.
