---
name: motor-2-consenso
description: Motor 2 — apuestas direccionales contra el consenso no-vig de sportsbooks (The Odds API). Usar al diagnosticar señales=0, matching kalshi↔odds, sizing/stakes, el funnel persistido, la banda de edge o el brazo de salida (take-profit/trailing). Contiene la cadena de guards post-incidente 2026-06-28 y los dos flags de ejecución (entrada vs salida).
---

# Motor 2 — consenso de sportsbooks

Compara el ask de Kalshi contra el fair no-vig del consenso de casas: si el edge neto
post-fee cae en la banda `(MIN, MAX]`, apuesta direccional. **PUEDE PERDER** — no es un
arb. Perdió −$390 acumulado (2026-06-28) por una cadena de causas ya remediadas; cada
guard de abajo existe por una pérdida real. Protocolo general: skill `botkalshi`.

## Mapa

```
src/strategies/motor_2_consensus/
├── detector.py       # find_signals: fair no-vig → edge neto → banda (min,max]
├── matcher.py        # nombres canónicos + fecha ET (reference-set kalshi↔odds)
├── sources.py        # RestKalshiQuoteSource (asks 1..99, descarta ask=100=book vacío,
│                     #   status!=open) + LiveOddsSource / FakeOddsSource (fixture)
├── poller.py         # loop + funnel diag persistido (Motor2FunnelSnapshot)
├── executor.py       # entrada (apuesta) — IOC, one-bet-per-event, underdog filter
└── exit_engine.py    # cierre TP/trailing (reusa helpers PUROS de M3)
src/math/no_vig.py    # implied_prob / remove_vig_multiplicative
scripts/diag_motor2_funnel.py   # por qué señales=0 (veredicto DB + pasada live)
scripts/diag_motor2_match.py    # por qué kalshi↔odds no matchea
```

## Flags y config (¡dos ejecuciones distintas!)

| Var | Default | Qué gatea |
|---|---|---|
| `MOTOR_2_SPORTSBOOK_ENABLED` | false | el motor CORRE (poller + funnel) |
| `MOTOR_2_ENTRY_EXECUTION_ENABLED` | false | Capa A de ENTRADA (apuestas). Con `TRADING_ENABLED` |
| `MOTOR_2_EXECUTION_ENABLED` | false | Capa A de SALIDA (ventas TP/trailing del exit_engine) — **NO confundir con el anterior** |
| `MOTOR_2_MIN_EDGE_PCT` (3.0) / `MOTOR_2_MAX_EDGE_PCT` (8.0) | — | banda de edge en pp. El bucket ~5% fue rentable; 12-13% sangró −$621 (edge alto en MLB = consenso mal calibrado, no regalo) |
| `MOTOR_2_MAX_STAKE_PCT` (1.0) | — | sizing FLAT (% del capital efectivo). 0 = ¼ Kelly (previo — Kelly amplificaba edges falsos: −19% ROI vs +22.9% flat en sim de 141 settled) |
| `MOTOR_2_ONE_BET_PER_EVENT` (true) | — | UNA apuesta por partido (yes@A + no@B = misma dirección; sangró −$218) |
| `MOTOR_2_MIN_ENTRY_CENTS` (40) + `MOTOR_2_UNDERDOG_FILTER_ENABLED` (false) | — | filtro underdog (<40c sangró −$110); off = shadow intra-live (loguea, no bloquea) |
| `ODDS_API_KEY` / `ODDS_API_SPORT_KEYS` (baseball_mlb) / `ODDS_API_REGIONS` (eu,us) | — | vacía → FakeOddsSource (fixture, JAMÁS apuesta) |
| `MOTOR2_SERIES` (KXMLBGAME,KXWC…) | — | universo de eventos; agregar series acá onboarda deportes sin tocar código |

Odds reales + `TRADING_ENABLED` + `ENTRY_EXECUTION` = apuesta real. Cualquiera en falso →
shadow puro.

## Diagnóstico (señales=0 es lo más frecuente)

1. **Primero el funnel persistido**: `Motor2FunnelSnapshot` (o log `motor2.funnel`) —
   ¿en qué etapa muere? `kalshi_events` → `matched` → `evaluated` → `signals`.
2. `matched=0` → `scripts/diag_motor2_match.py` (nombres canónicos, fecha ET; el matcher
   compara por reference-set — un alias que falta se agrega en matcher.py).
3. `matched>0` + `best_edge=-100pp` → sentinel "nada evaluado": books vacíos (ask=100)
   ya se filtran en sources; verificar mercados `status!=open` (colapsan a 0/100 y
   generan edges fantasma contra odds pre-partido).
4. `evaluated>0` + `signals=0` → edge fuera de banda: mirar `best_edge` del snapshot
   contra la banda. Puede ser el mercado eficiente (veredicto del analyst_loop).
5. Odds: `LiveOddsSource` tolera fallo por deporte (no tira el batch); consenso se
   des-infla con casas parciales (fix del fair inflado, 2026-06-28).

## Reglas duras

- El stake usa el capital EFECTIVO del RiskManager por ciclo (cash real × factor), no un
  número estático. El RM re-capea aguas abajo.
- La entrada es IOC. La salida (exit_engine) reusa `take_profit.py`/`trailing_stop.py`
  de M3 — helpers PUROS, sin red/DB: cualquier cambio de lógica de salida se testea ahí.
- Análisis históricos pre-2026-07-01 tienen fees ~100× subestimadas (edges inflados ~1pp).
