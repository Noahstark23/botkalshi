---
name: motor-rest-arb
description: Motor REST — arbitraje multi-outcome (winner-take-all ≥3 patas) con detección WS y ejecución REST, más el SettlementPoller que settlea los fills de TODOS los motores. Usar al tocar la ejecución FOK multi-pata, los LegState (FILL/KILL/ERROR_RED), el settlement atómico por arb_id o _leg_pnl_cents.
---

# Motor REST — arbs multi-outcome + settlement

Detecta grupos winner-take-all (≥3 outcomes, ej. 1X2 de fútbol) donde comprar TODOS los
yes < $1, y ejecuta por REST. Mismo riesgo de familia que Motor 1 (huérfanas por fill
parcial), mitigado con FOK + estados de pata explícitos. Además es el DUEÑO del
`SettlementPoller`, que settlea los `filled` de **todos** los motores. Protocolo general:
skill `botkalshi`.

## Mapa

```
src/strategies/motor_rest_arb/
├── executor.py      # ejecución multi-pata FOK: LegState FILL / KILL / ERROR_RED
├── settlement.py    # SettlementPoller + KalshiSettlementSource + _leg_pnl_cents
└── (trigger/engine) # detección WS + EdgeWindow
tests/strategies/motor_rest_arb/   # test_settlement es el contrato del poller
```

## Flags y config

| Var | Default | Qué gatea |
|---|---|---|
| `MOTOR_REST_ENABLED` | false | el motor corre (detecta + graba EdgeWindow) |
| `MOTOR_REST_EXECUTION_ENABLED` | false | Capa A de ejecución (con `TRADING_ENABLED`) |
| `MOTOR_REST_MIN_EDGE_CENTS` (1) / `MIN_DEPTH` (2) | — | trigger grueso de detección |
| `MOTOR_REST_EXECUTION_EDGE_PCT` (1.5) / `MAX_EDGE_PCT` (10) | — | banda fina de EJECUCIÓN. Un edge enorme (132% visto en shadow) = pata ~0¢ de eliminado / quote stale / grupo a medio resolver — fantasma, NO regalo |
| `MULTI_SERIES` | — | universo winner-take-all (distinto de `MOTOR2_SERIES`) |

## Ejecución: estados de pata (el corazón del motor)

- Todas las patas van **FOK**. Tres desenlaces por pata:
  - `FILL` — fill confirmado (leer `fill_count`, no el HTTP 200).
  - `KILL` — rechazo determinístico: **HTTP 409 + error.code
    `fill_or_kill_insufficient_resting_volume`** (verificado contra la API viva,
    2026-06-05). Match ESTRICTO: cualquier otro 409/code NO es KILL.
  - `ERROR_RED` — timeout/5xx/red o code desconocido: la orden PUDO entrar → estado
    DESCONOCIDO, fila `pending`, lo resuelve la reconciliación. Jamás asumir "no pasó".
- Guardarraíl pata-dura-primero (#85): se coloca primero la pata limitante/ilíquida;
  si KILLea, no se colocó nada más.
- Mixto FILL+KILL → liquidar lo fillado (mismo espíritu que el rollback de M1); las
  patas `arb_id` comparten notes para el netting del RiskManager.

## SettlementPoller (corre SIEMPRE, para todos los motores)

- `STRATEGIES = (motor_rest_arb, motor_2_consensus, motor_1_arbitrage, motor_5_mm)` —
  si un motor nuevo genera filas `filled` que esperan resolución, **hay que agregarlo**
  (M1 lo aprendió el 2026-07-02, M5 el 2026-07-07: fills eternos = exposición fantasma
  que estrangula el headroom/canary cap y pérdidas invisibles al stop-loss).
- **Invariante atómico**: un grupo (arb_id) se settlea con TODAS sus patas en UNA
  transacción o con NINGUNA — settlear solo la ganadora crea la "pérdida fantasma".
- `_leg_pnl_cents`: usa `fill_price_cents` (o el límite), `filled_count` si existe (M5;
  los demás caen a `count`), y maneja `action="sell"` como el lado opuesto a 100−P.
  Salta filas `closed_by_clv` (su pnl ya se realizó al vender — doble conteo prohibido).
- `KalshiSettlementSource`: `get_market(ticker).result` ∈ {yes,no}; cualquier otra cosa
  → None → el grupo ESPERA (fail-safe: nunca settlear de más).
- No gateado por `TRADING_ENABLED`: apagar el trading no debe frenar la liquidación de
  lo ya tradeado.

## Diagnóstico

- Fills que no settlean: ¿strategy en `STRATEGIES`? ¿el mercado ya trae `result`?
  (`scripts/inspect_settlement_shape.py`, `scan_settled_voids.py`) ¿grupo incompleto
  esperando una pata?
- Edges fantasma en EdgeWindow: `scripts/report_edge_windows.py`; comparar contra la
  banda de ejecución antes de proponer bajar umbrales.
