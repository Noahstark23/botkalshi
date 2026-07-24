---
name: motor-1-arbitraje
description: Motor 1 — arbitraje binario intra-ticker por WebSocket (yes+no < $1). Usar al diagnosticar, modificar o activar Motor 1, su executor/rollback, el OrderbookManagerV2, la exposición por evento o las huérfanas. Contiene los invariantes de órdenes (FOK/fill_count), el mapa de guards post-incidente 2026-07-07 y el checklist de activación.
---

# Motor 1 — arbitraje binario (WS)

Compra yes+no del MISMO ticker cuando la suma de asks + fees < $1. Risk-free **solo si
ambas patas fillan**: el modo de falla dominante es la HUÉRFANA (una pata fillada, la otra
no) que convierte un arb en posición direccional. Protocolo general: skill `botkalshi`.

## Mapa

```
src/strategies/motor_1_arbitrage/
├── engine.py                 # loop de detección sobre OrderbookManagerV2
├── executor.py               # patas concurrentes FOK + rollback IOC + circuit breaker
│                             #   + pre-check de balance + pausa persistente
├── event_exposure.py         # EventExposureTracker: cap direccional por EVENTO
├── orderbook.py              # OrderbookState (seq per-sid, stale/desync)
└── orderbook_manager_v2.py   # recovery WS + circuit breaker + buffer acotado
src/math/arbitrage.py         # detect_binary_arb / ArbOpportunity / ArbLeg
tests/strategies/motor_1_arbitrage/   # test_executor*, test_incident_4bugs, test_v2_*
```

## Flags y config

| Var | Default | Qué gatea |
|---|---|---|
| `MOTOR_1_ARBITRAGE_ENABLED` | false | el motor CORRE (detecta, graba EdgeWindow) |
| `MOTOR_1_EXECUTION_ENABLED` | false | Capa A de ENTRADA: el executor solo existe con esto + `TRADING_ENABLED` |
| `MIN_EDGE_PCT` (2.0) | — | umbral de DETECCIÓN (es de M1; Motor 2 usa `MOTOR_2_MIN_EDGE_PCT`) |
| `MOTOR_1_EXECUTION_EDGE_PCT` (1.5) / `MOTOR_1_MAX_EDGE_PCT` (10) | — | banda de EJECUCIÓN (piso fino + techo anti-fantasma) |
| `MAX_EVENT_DIRECTIONAL_EXPOSURE_USD` (25) | — | cap del EventExposureTracker |

Shadow = `ARBITRAGE_ENABLED=true` + `EXECUTION_ENABLED=false` (engine con `executor=None`,
estructuralmente incapaz de operar).

## Invariantes del executor (incidente 2026-07-07, ~−$139)

1. **Buys SIEMPRE `time_in_force="fill_or_kill"`** — el default gtc dejaba patas RESTING.
2. **HTTP 200 ≠ fillada**: leer `fill_count`/`fill_count_fp` de la respuesta. fill 0 (200
   killed o 409 `fill_or_kill_insufficient_resting_volume`) → fila `cancelled`; otro
   error → queda `pending` (lo resuelve `reconcile_pending_trades`). El count REAL va a
   la fila (`t.count = fill_count`).
3. **Rollback vende IOC** (jamás resting) y SOLO lo confirmado por `fill_count` libera
   posición. La pérdida se REALIZA en `pnl_cents` sobre fila `settled` (split si el sell
   fue parcial) → visible para los stop-losses. Lo no vendido queda `filled` (abierto):
   exposición real, huérfana gestionable por Motor 3 (`MOTOR_3_MANAGES_ORPHANS`).
4. **Guards de entrada, en orden**: risk check → guard de evento (tickers hermanos
   `…HOUWSH-HOU`/`-WSH` = MISMO evento; yes@A + no@B = misma dirección; fuente dual
   max(fills en memoria, portfolio_positions)) → pre-check de balance REAL (costo del arb
   ENTERO + fees vs cash de Kalshi; fail-OPEN si la lectura falla; caché 5s invalidado
   tras cada fill).
5. **Pausas**: 3 rollbacks/60min → pausa runtime (circuit breaker). UN rollback abortado
   por slippage (>10%) → `engage_kill_switch` PERSISTENTE al primer evento (Bug 3 —
   sobrevive redeploys; solo `scripts/clear_kill_switch.py` lo levanta).

## Diagnóstico

- Logs greppables: `ArbitrageExecutor:` (fills/aborts/rechazos), `rollback:`,
  `motor5.funnel`-equivalente no existe acá — la evidencia es `Trade` (strategy
  `motor_1_arbitrage`, notes `arb_id=`), `RiskEvent` (`atomic_rollback`,
  `rollback_aborted_slippage`, `event_exposure_cap`) y `EdgeWindow`.
- Huérfanas: pata `filled` cuyo `arb_id` no tiene hermana `filled` →
  `motor_3_clv/orphans.py::motor1_orphan_buys`. Un par hedged JAMÁS se toca.
- WS/books: desync/stale en `orderbook_manager_v2` (incidente 2026-05-28: recovery
  atascada; 2026-06/07: `buffer_overflow`, code 15 = problema de CUENTA, no de código).

## Activación (checklist específico)

1. Repetir riesgos: fixes FOK/fill_count/rollback (#137) sin validación en producción;
   huérfanas siguen POSIBLES (FOK las hace raras, no imposibles).
2. Colchón: `MAX_TRADE_SIZE_PCT` bajo, cap de evento en $25, resto de motores off.
3. Secuencia: kill-switch limpio (`clear_kill_switch.py`) → env con `TRADING_ENABLED=true`
   + `MOTOR_1_ARBITRAGE_ENABLED=true` + `MOTOR_1_EXECUTION_ENABLED=true` → redeploy →
   `curl /status` + primeros logs del executor. Validación intermedia recomendada: correr
   unos días con `EXECUTION_ENABLED=false` (shadow) y comparar EdgeWindow vs fills teóricos.
