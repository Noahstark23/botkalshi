# Runbook — activación de motores para el mes de prueba

Decisión del owner (2026-07-28): dar **un mes de prueba a todos los motores**,
incluidos M2 y REST que la auditoría 2026-07-18 marcó para apagar. Este runbook
existe para que ese mes sea **medible y reversible**, no para discutir la
decisión.

Contexto que el runbook asume conocido (auditoría 2026-07-18, un mes de datos):
M2 −$432.95 (100% de la sangría, edge techa 0.15pp vs umbral 3pp), REST 73% de
rollback (FOK no atómico en books finos), M1 +$0.065/trade (ruido), M6 mudo,
M8 p50 +3.18pp con n=130. Apagar M2+REST llevaba el mes de −$431 a ~$0.
**El mes de prueba re-testea esa conclusión con datos frescos.**

---

## PASO 0 — Arreglos de riesgo ANTES de encender (bloqueantes)

Estos no son opinión: son incoherencias verificadas en el código.

| # | Env var | Valor actual | Poner | Por qué |
|---|---|---|---|---|
| 1 | `MAX_DAILY_LOSS_PCT` | 9 | **3** | `_evaluate_windows()` (`src/risk/manager.py:595-615`) evalúa mensual → semanal → diario. Con 9 > 8 el semanal SIEMPRE dispara primero: **el stop diario nunca se ejecuta**. Encender motores sin freno de corto plazo es el peor escenario posible. |
| 2 | `MAX_SIMULTANEOUS_EXPOSURE_PCT` | 50 | **25** | Duplicar exposición simultánea justo cuando se multiplican los motores acumula riesgo dos veces. 25 es el valor de preview y del diseño original. |
| 3 | `SENTRY_DSN` | vacío | **configurar** | Sin monitoreo de errores no hay forma de ver un crash-loop de un motor recién encendido. |
| 4 | `DAILY_PNL_REPORT_ENABLED` | false | **true** | Es el reporte que detecta la sangría diaria. Sin él, el mes se evalúa recién al final. |
| 5 | `ODDS_API_SPORT_KEYS` | `baseball_mlb ,soccer_...` | sin espacio antes de la coma | Es un `str` plano (`config.py:504`) sin parser propio: el espacio queda dentro del valor. Solo afecta a M2. |

Verificación tras el redeploy: `GET :18080/status` debe mostrar los límites
nuevos, y `/health` responder 200.

### PASO 0.b — Leer el capital EFECTIVO antes de fijar umbrales

No hay que adivinar si el sizing usa `ACTIVE_CAPITAL_USD` (100) o el bankroll
(1200): `GET :18080/status` → bloque **`capital`** ya lo dice:

```
capital: { mode: dynamic|fixed, raw_balance_usd, effective_usd, is_paused }
```

Mecánica (`manager.py:87-134`), para leer ese bloque con criterio:
- `DYNAMIC_CAPITAL_ENABLED=false` → efectivo = `ACTIVE_CAPITAL_USD` ($100).
- `true` con balance cacheado → `cash_real × CAPITAL_SAFETY_FACTOR_PCT`,
  **capado por `CAPITAL_CAP_USD` y con piso `CAPITAL_FLOOR_USD`**, más el hard
  cap de $5k en producción.
- `true` sin balance todavía (refresh no corrió o la API falló) → fallback a
  `ACTIVE_CAPITAL_USD` con WARNING.

**`effective_usd` es la única fuente de verdad**: de ahí salen sizing por trade
(5%), exposición (25%) y los stop-losses. Anotar ese número en el baseline: si
cambia durante el mes (depósitos/retiros mueven el cash real), los umbrales
absolutos de la línea defensiva se recalculan proporcionalmente.

---

## PASO 1 — Orden de encendido (uno por redeploy)

Regla del propio proyecto: *"una cosa a la vez por redeploy"*. Encender los tres
flags juntos hace imposible atribuir un resultado malo a un motor.

Orden recomendado (del menos al más riesgoso según la evidencia):

1. **REST** (`MOTOR_REST_EXECUTION_ENABLED=true`) — su problema documentado es
   ejecución (rollback), no edge: el edge detectado era real (3.13pp). Se mide
   fill-rate desde el día 1.
2. **M2 entradas** (`MOTOR_2_ENTRY_EXECUTION_ENABLED=true` +
   `MOTOR_2_EXECUTION_ENABLED=true`) — el de pérdida comprobada. Encenderlo
   último y con el resto ya estabilizado.

Entre uno y otro: **mínimo 48h** de observación. Si el primero rompe algo,
enterarse antes de sumar el segundo.

Antes de CADA redeploy (pre-flight del vault):
- Backup SQLite con la API `.backup()` (nunca `cp` crudo) + `integrity_check`
- Baseline capturado: `GET /stats/motors?days=30` guardado ANTES de encender
- Señal de "vivo" definida: qué log/campo confirma que el motor arrancó

---

## PASO 2 — Línea defensiva (definida ANTES, no durante)

Copiada de la disciplina de la Lección 9 (dos rollbacks en <5 min, cero daño).

| Ventana | Qué se mira | Rollback inmediato si… |
|---|---|---|
| T+5 min | logs del contenedor | cualquier excepción NUEVA del motor encendido, o crash-loop |
| T+30 min | `GET /stats/motors?days=1` | el motor nuevo ya acumula PnL < −$15 |
| T+24 h | `/stats/motors?days=1` + `/status` | PnL del motor < −$25, o >30 trades/hora (bug de exposición, precedente 2026-07-07: 186 trades/6min = −$140) |
| Semanal | `/stats/motors?days=7` | cualquier motor con `verdict_hint: "sangra"` dos semanas seguidas |

**Rollback = poner el flag en `false` + redeploy.** No hay que debuggear en
caliente: primero se apaga, después se investiga (Lección 9).

---

## PASO 3 — Medición del mes (el instrumento nuevo)

`GET :18080/stats/motors?days=N` da PnL **por motor**: trades settleados, PnL
neto, fees, win-rate, PnL por trade, peor día y un `verdict_hint` descriptivo
(sangra / ruido / positivo / negativo leve).

Es el corte que la auditoría tuvo que hacer por SQL — ahora es un GET, así que
el agente web lo puede traer sin terminal.

Cadencia sugerida del mes de prueba:
- **Diaria** (2 min): `/stats/motors?days=1` → ¿algún motor cruzó su límite de
  la línea defensiva?
- **Semanal** (10 min): `/stats/motors?days=7` → **apagar el peor motor si
  sangra**. No esperar al mes: el mes es el plazo máximo, no un compromiso de
  mantener todo encendido 30 días.
- **Cierre del mes**: `/stats/motors?days=30` vs el baseline del PASO 1.
  Criterio de éxito por motor, definido ahora (anti confirmation bias):
  - **Sigue**: PnL neto > 0 **Y** `pnl_per_trade_usd` > 2 × fee promedio por
    trade (`fees_usd / settled_trades`, ambos en la respuesta de
    `/stats/motors`)
  - **Se apaga**: PnL neto < 0
  - **Se archiva**: 0 señales en el mes (caso M6 hoy)

  > El umbral de "ruido" es **relativo al fee, no un absoluto en dólares**: un
  > PnL/trade fijo significaría cosas distintas según el capital efectivo del
  > momento (ver PASO 0.b), y contaminaría justo la métrica con la que se
  > decide. Contra el fee es auto-escalante: si el motor no le gana varias
  > veces a su propio costo de transacción, es ruido — que es exactamente lo
  > que mostró M1 en julio (+$0.065/trade).

## Nota honesta para el cierre del mes

Si al final del mes el neto es negativo, la respuesta correcta NO es "un mes más
para confirmar". La auditoría de julio ya fue ese mes; este es el segundo. Dos
meses con la misma conclusión es evidencia, no mala suerte — y "archivar un
motor que no rinde es un resultado válido y barato" (CLAUDE.md).
