# botkalshi — bot de trading algorítmico sobre Kalshi

Bot 24/7 con **dinero real** sobre mercados de predicción de Kalshi (deportes principalmente).
Corre en un container de Coolify; la config vive en env vars (Pydantic `Settings`, fail-fast al
arranque). SQLite (`/app/data/trades.db`) como única DB. **Todo error acá cuesta plata.**

## Estructura del proyecto

```
src/
├── runner.py                    # ProductionRunner: orquesta TODAS las tasks (una por servicio)
├── clients/
│   ├── kalshi_rest.py           # REST auth firmada (get_balance/get_event/place_order/…)
│   ├── kalshi_ws.py             # WebSocket con reconexión + suscripciones re-aplicables
│   └── odds_api.py              # The Odds API (cuotas de sportsbooks)
├── math/
│   ├── arbitrage.py             # detect_binary_arb / ArbOpportunity / ArbLeg
│   ├── fees.py                  # kalshi_fee_cents — fórmula OFICIAL (¡corregida 2026-07-01!)
│   ├── kelly.py                 # quarter_kelly_size (Motor 2 lo abandonó → flat sizing)
│   └── no_vig.py                # implied_prob / remove_vig_multiplicative
├── risk/
│   └── manager.py               # RiskManager: capital dinámico (cash real), stop-losses
│                                #   diario/semanal/mensual, exposición, sizing, kill-switch
├── storage/
│   └── models.py                # Trade / PortfolioPosition / RiskEvent / DailyPnL /
│                                #   Motor2FunnelSnapshot / OperationalState (kill-switch DB)
├── strategies/
│   ├── data_capture.py          # discovery de markets + feed WS + OrderbookManagerV2 host
│   ├── motor_1_arbitrage/       # M1: arbs binarios intra-ticker (yes+no < $1)
│   │   ├── engine.py / detector # detección sobre OrderbookManagerV2
│   │   ├── executor.py          # patas concurrentes + rollback atómico + circuit breaker
│   │   │                        #   + pre-check de balance (Bug 1) + pausa persistente (Bug 3)
│   │   ├── event_exposure.py    # EventExposureTracker: cap direccional por EVENTO (Bug 2)
│   │   ├── orderbook.py         # OrderbookState (seq per-sid, stale/desync semantics)
│   │   └── orderbook_manager_v2.py  # recovery WS + circuit breaker + buffer acotado
│   ├── motor_2_consensus/       # M2: direccional vs consenso de sportsbooks
│   │   ├── detector.py          # find_signals: fair no-vig → edge neto → banda (min,max]
│   │   ├── matcher.py           # nombres canónicos + fecha ET (reference-set)
│   │   ├── sources.py           # quotes Kalshi (asks 1..99) + odds live/fake
│   │   ├── poller.py            # loop + funnel diag persistido (Motor2FunnelSnapshot)
│   │   └── exit_engine.py       # cierre TP/trailing de posiciones M2 (reusa helpers M3)
│   ├── motor_3_clv/             # M3: gestión de salidas (take-profit / trailing / T-30)
│   │   ├── engine.py            # atribución de posiciones + detección + (venta si live)
│   │   ├── executor.py          # SELL IOC + _settle_originals (closed_by_clv + pnl real)
│   │   ├── orphans.py           # huérfanas de M1 gestionables (Bug 4) — NUNCA hedges
│   │   ├── take_profit.py / trailing_stop.py   # helpers PUROS (sin red/DB)
│   │   └── poller.py            # PortfolioPoller: sync portfolio_positions desde Kalshi
│   └── motor_rest_arb/          # Motor REST: arbs multi-outcome + SettlementPoller
├── analytics/                   # daily_pnl (digest) / analyst_loop / shadow_fee_recalc
└── monitoring/
    ├── health.py                # FastAPI /status + BotState (estado runtime compartido)
    ├── dashboard.py             # /dashboard de Telegram on-demand (read-only)
    └── telegram_alerts.py       # send_alert + alert_* (best-effort SIEMPRE)

scripts/                         # diagnóstico read-only + operación (correr EN el container)
├── diag_motor2_funnel.py        # por qué señales=0 (veredicto DB + pasada live)
├── diag_motor2_match.py         # por qué kalshi↔odds no matchea
├── clear_kill_switch.py         # ÚNICA forma de levantar el kill-switch persistente
├── calibrar_take_profit.py / check_portfolio.py / query de logs, etc.
tests/                           # pytest, espejo de src/ (~1000+ tests, asyncio auto)
```

## Los motores (y su estado de madurez)

| Motor | Qué hace | Riesgo |
|---|---|---|
| M1 arbitraje | yes+no del mismo ticker < $1 (risk-free SI ambas patas fillan) | huérfanas por fill parcial → direccional (incidente 2026-07-07) |
| M2 consenso | direccional vs fair de sportsbooks (edge neto en banda (3pp, 8pp]) | PUEDE PERDER; sizing flat (Kelly amplificaba edges falsos) |
| M3 CLV | NO abre — cierra: take-profit/trailing/T-30 sobre posiciones | vender la pata de un hedge (por eso la atribución estricta) |
| REST arb | arbs multi-outcome vía REST + settlement atómico por arb_id | igual que M1 |

## Arquitectura de seguridad (NO debilitar jamás)

1. **Capa A** — el executor de cada motor SOLO se construye con los flags de ejecución on
   (`TRADING_ENABLED` && `MOTOR_X_EXECUTION_ENABLED`). En shadow el executor es `None`:
   estructuralmente incapaz de operar. Los `sell` NO los frena la Capa C → la Capa A es la
   única protección de los motores de salida.
2. **Capa C** — `place_order` bloquea `buy` con `TRADING_ENABLED=false` (muro global).
3. **Kill-switch persistente** (`operational_state` en DB) — sobrevive redeploys; el boot
   re-hidrata la pausa (`_rehydrate_kill_switch`). SOLO se levanta con
   `scripts/clear_kill_switch.py` (verifica posiciones=0 + input "CLEAR"). **No auto-resetea
   por diseño.** Lo disparan: stop-losses del RiskManager, y desde el incidente 2026-07-07
   también UN rollback abortado por slippage (`rollback_aborted_slippage`).
4. **Circuit breaker de M1** — 3 rollbacks en 60min → pausa runtime. El caso *abortado por
   slippage* pausa PERSISTENTE al PRIMER evento (Bug 3).
5. **RiskManager** — gatekeeper único pre-trade (lock de clase): capital efectivo dinámico
   (cash REAL de Kalshi × factor, piso/techo), stop-losses derivados del mismo capital,
   exposición simultánea, cap por trade. Los arbs hedged se netean por `arb_id`.
6. **Guards de entrada** — M1: pre-check de balance real (Bug 1) + cap direccional por
   evento (Bug 2, `EventExposureTracker`). M2: una apuesta por evento, banda de edge,
   techo anti-fantasma, entrada mínima.

## Cómo debe trabajar Claude (Opus/Fable) en este repo

**Principios no negociables:**

1. **Shadow-first para TODA lógica de trading nueva.** Flag de detección separado del de
   ejecución; default off o shadow; logs `[... SHADOW] net=$` con fees reales para validar
   contra datos ANTES de prender la venta/compra real.
2. **Diagnóstico read-only ANTES de tocar código.** Si el reporte viene de producción,
   primero entender con evidencia (logs/DB/snapshots persistidos). Este repo ya tiene el
   patrón: funnel snapshots, scripts diag_*, logs greppables. No hay acceso directo al
   container desde las sesiones de código: pedile al operador el output de los scripts.
3. **Al proponer una ACTIVACIÓN (flags de ejecución), repetir SIEMPRE los riesgos conocidos
   pendientes aunque ya se hayan hablado** — y recomendar el colchón (sizing chico, motor en
   off, cap bajo). Lección del incidente 2026-07-07: proceder "como rutina" con bugs P0
   conocidos costó ~$140 reales.
4. **Nunca vender la pata de un hedge.** Cualquier código que gestione posiciones debe
   atribuir el origen (BUYs abiertos por estrategia) y excluir arbs con ambas patas vivas.
   Ver `motor_3_clv/orphans.py` y `_attributable_positions`.
5. **Fail-safe direccional correcto:** paths de LECTURA fallan abiertos (un hiccup no apaga
   el bot); paths de VENTA/CIERRE fallan cerrados (ante la duda, NO vender). Ejemplos:
   balance pre-check fail-open; `_open_attributable_count` error→0 (no vende).
6. **Lección 7:** nunca `gather(return_exceptions=True)` sin supervisor; cada loop tiene
   try/except por tick que registra (`BotState.record_error`) y SIGUE. Nada de
   `except: pass` silencioso.
7. **Convenciones de tiempo:** `settled_at`/`close_time` NAIVE UTC (comparar con
   `datetime.now(UTC).replace(tzinfo=None)`); `placed_at` AWARE UTC. No mezclar.
8. **Dinero en cents enteros** (`pnl_cents`, `price_cents`); fees SIEMPRE con
   `kalshi_fee_cents` (la fórmula oficial; estuvo ~100× subestimada hasta 2026-07-01 —
   cualquier análisis histórico pre-fix tiene edges inflados ~1pp).

**Flujo de trabajo:**

- Branch por cambio desde `origin/main` fresco (main se mueve rápido — hay varias sesiones).
- Tests para todo: mecanismo + control (el caso que NO debe disparar) + fail-safe. Suite
  completa verde + `ruff check` + `ruff format` antes de push.
- Commits y comentarios en español, estilo del repo: el PORQUÉ con contexto de incidente
  ("Bug X, incidente AAAA-MM-DD: …"), no el qué.
- PR draft con: problema (evidencia), fix, verificación, y **limitaciones honestas** (qué NO
  resuelve). Si el hallazgo contradice el brief del operador, decirlo con evidencia.
- Config nueva: env var en `utils/config.py` (Field con description) + `.env.example` +
  threading runner→componente. Tuneable en vivo > hardcodeado.
- No tocar la DB de producción; no "arreglar" datos. Los scripts nuevos: read-only y con
  docstring de uso.

## Incidentes clave (contexto para decisiones)

- **2026-05-28** — recovery WS atascada (snapshot sin id) → books stale. Fixes: routing por
  ticker/sid, circuit breaker de recovery, no aplicar deltas a books stale.
- **2026-06-28** — Motor 2: −$390 acumulado. Causas encadenadas: sin brazo de salida (→
  exit_engine), doble exposición por evento (→ one_per_event), Kelly sobre edges inflados
  (→ flat sizing), fair inflado por casas parciales (→ consenso des-inflado).
- **2026-07-01** — fee oficial corregida (~100× subestimada) + stop-loss semanal subcontaba
  en borde de mes.
- **2026-07-07** — M1: 186 trades/6min; huérfanas por `insufficient_balance` en la 2ª pata +
  residual direccional cross-ticker del MISMO evento (~$135) + pausa runtime-only que un
  redeploy borraba. Fixes: Bugs 1-4 (pre-check de balance, EventExposureTracker,
  `rollback_aborted_slippage` → kill-switch persistente al 1er evento, M3 gestiona huérfanas
  con `MOTOR_3_MANAGES_ORPHANS`).
