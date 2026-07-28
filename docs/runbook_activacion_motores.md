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
| 1 | `MAX_DAILY_LOSS_PCT` | 9 | **3** ✅ aplicado | `_evaluate_windows()` (`manager.py:595-615`) evalúa mensual → semanal → diario. Con 9 > 8 el semanal dispara antes que el diario **cuando los porcentajes dominan** (ver 0.c: hoy no dominan). Bug LATENTE, no activo — se materializaba arriba de ~$500 de capital. Corregido igual: ahora el orden es coherente a cualquier nivel. |
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

**Baseline medido 2026-07-28 21:26 UTC: `effective_usd = $270.11`**
(cash real $300.12 × 90% de factor de seguridad, modo dynamic).

### PASO 0.c — Los PISOS USD mandan, no los porcentajes (verificado en vivo)

`manager.py:577`: `limit = max(capital_usd * (pct/100), floor)`. Con $270.11 de
capital efectivo y los `MAX_*_LOSS_FLOOR_USD` en sus defaults (20/40/60, no
seteados en Coolify):

| Ventana | % configurado | % en USD | Piso | **Límite real** | = % efectivo |
|---|---|---|---|---|---|
| Diario | 3% | $8.10 | $20 | **$20** | 7.4% |
| Semanal | 8% | $21.61 | $40 | **$40** | 14.8% |
| Mensual | 15% | $40.52 | $60 | **$60** | 22.2% |

**El freno diario real es $20 (7.4%), no 3%.** El cambio del PASO 0 igual
apretó ~18% (antes: `max(24.31, 20) = $24.31`). Si querés que el diario sea
realmente 3% del capital, hay que bajar también `MAX_DAILY_LOSS_FLOOR_USD`
(p. ej. a 8). Decisión pendiente del owner — **con capital chico los pisos son
una protección deliberada** (un stop de $8 se dispara con ruido normal), así
que dejarlos también es defendible. Lo que NO es defendible es no saber cuál
manda: ahora está medido.

Gate adicional visto en el arranque: **Rolling30d está al 65% de su límite pero
`gate_off`** — mide pero no actúa. Si se quiere ese freno activo durante el mes,
hay que encenderlo explícitamente (`ROLLING_DRAWDOWN_STOP_ENABLED`).

---

### PASO 0.d — Deploy de instrumentos ANTES de encender flags

Los arreglos de medición (fees por motor, unidades de `/stats/edges`) son
**código, no flags**: van en su propio redeploy, sin tocar
`MOTOR_*_EXECUTION_ENABLED`. Son read-only respecto del trading — no pueden
mover un solo contrato — así que no compiten con la regla de "una cosa a la vez":
la cosa que cambia en ESE deploy es el instrumento, no el comportamiento.

Recién con el instrumento verificado se hace el PASO 1, que **no requiere
redeploy de código**: el flag es una env var en Coolify.

Verificación post-deploy del instrumento (todo por GET, sin terminal):
- `/stats/motors?days=30` → cada motor trae `fees_coverage_pct` y
  `ruido_umbral_usd`
- `/stats/edges?days=7` → cada bloque trae `unit`, y solo los `pct` traen
  `gt_8pp_sospechosos`

---

### PASO 0.e — El motor que va primero ya reprobó el criterio escrito

Medido el 2026-07-28, con el instrumento ya desplegado: **REST tiene
`verdict_hint: "sangra"`**, `pnl_per_trade_usd: −1.952` sobre 15 trades y peor
día −$22.86 (2026-07-14). El criterio del PASO 3 dice "se apaga si PnL neto < 0",
sin excepción por causa raíz.

O sea: encender REST primero es **decidir a sabiendas arrancar el mes con un
motor que ya reprobó la regla**. Eso puede ser correcto — pero entonces la
excepción se escribe ANTES, no después de ver el resultado. Escrita:

> **Excepción REST.** Su falla documentada es de EJECUCIÓN (73% de rollback:
> FOK no atómico en books finos), no de edge — el edge detectado era real
> (3,13pp). Por eso su mes NO se evalúa por PnL sino por **fill-rate**: si el
> problema es ejecución, se arregla o no se arregla, y eso se ve en días.
>
> - Métrica del mes: `rollback_triggered / intentos`. Meta: **< 25%**.
> - Tripwire duro, independiente del fill-rate: PnL < **−$25** acumulado → se
>   apaga sin discusión (misma línea que el resto en el PASO 2).
> - Plazo: **7 días**, no 30. Un problema de ejecución se manifiesta en la
>   primera decena de intentos; no hace falta un mes para verlo.
> - Si a los 7 días el fill-rate no bajó de 25%, se apaga. La tesis "es
>   ejecución, no edge" queda refutada por la vía rápida y barata.

Y los 15 trades de baseline no alcanzan para nada: `muestra_suficiente: false`.
Con n < 30 el endpoint ya no emite veredicto — la decisión de REST se toma con
fill-rate, que sí se mide con pocos intentos.

**M2 tampoco está listo para empezar.** Ocho días seguidos (07-21 → 07-28) con
`m2_signals: 0` sobre 250–518 ciclos de embudo por día, y el analyst reporta
`inconsistente | best_edge del embudo cruzó el umbral pero no se grabaron
señales`. Un mes de M2 en ese estado mide CERO: no es un resultado negativo, es
ausencia de medición. Hay que resolver primero si es umbral o persistencia.

**Consecuencia:** el mes de prueba no empieza el día que se pueda, empieza el día
que haya algo que medir. Eso no cambia la decisión de darle un mes a cada motor
— cambia cuándo arranca el reloj de cada uno.

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

## PASO 2.b — BASELINE medido (2026-07-28 21:55 UTC, deploy `b6df916`)

`GET /stats/motors?days=30`, **antes** de encender nada. Este es el número contra
el que se compara el cierre del mes:

| Motor | Trades | PnL | Win-rate | Peor día |
|---|---|---|---|---|
| motor_2_consensus | 16 | **−$261.28** | 6.2% | 2026-06-28 −$233.28 |
| motor_rest_arb | 15 | **−$29.28** | — | — |
| motor_1_arbitrage | 519 | **+$33.37** | — | — |
| **NETO** | | **−$257.19** | | |

Dos lecturas que el número crudo esconde:

1. **La sangría de M2 en JULIO es ~$28, no $260.** Los −$233.28 son UN día
   (2026-06-28) que entra por el borde de la ventana de 30 días. La ventana se
   corre sola: mañana ese día sale y el "M2 −$261" se convierte en "M2 −$28"
   sin que nada haya cambiado. Comparar siempre contra ESTA tabla congelada, no
   contra el `days=30` de otro día.
2. **M2 y REST no ejecutaron nada en el período** — sus flags están en `false`
   y con flags apagados el executor ni se construye. Esas pérdidas son
   settlements viejos aterrizando, no actividad. El baseline real de ambos para
   el mes de prueba arranca en $0 el día que se enciendan.

---

## PASO 3 — Medición del mes (el instrumento nuevo)

`GET :18080/stats/motors?days=N` da PnL **por motor**: trades settleados, PnL
neto, fees, win-rate, PnL por trade, peor día y un `verdict_hint` descriptivo
(sangra / ruido / positivo / negativo leve / indeterminado).

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
  - **Sigue**: PnL neto > 0 **Y** `|pnl_t_stat| >= 2`
  - **Se apaga**: PnL neto < 0
  - **Se archiva**: 0 señales en el mes (caso M6 hoy)
  - **No se decide**: `muestra_suficiente: false` (< 30 trades settleados) →
    no alcanza la muestra. Se extiende o se mide por otra vía (caso REST, 0.e).

  > **El umbral en dólares se descartó — falló dos veces por el mismo motivo.**
  > Primero fue absoluto ($0.15/trade): depende del sizing y del capital del
  > momento, así que significa cosas distintas en meses distintos. Después
  > relativo (2 × fee): con los fees de Kalshi el umbral queda en centavos
  > ($0.028 para M2), y medido en producción el piso absoluto lo tapaba en 2 de
  > 3 motores — el criterio "relativo" no estaba actuando.
  >
  > La pregunta real nunca fue "¿la media supera X?" sino **"¿esta media se
  > distingue de cero con esta muestra y esta dispersión?"**. Eso es
  > `t = media / error estándar`, y no depende ni del capital ni del fee.
  > `|t| < 2` ≈ indistinguible de cero al 95%: es ruido por muy positiva que se
  > vea la media. `/stats/motors` publica `pnl_t_stat`,
  > `pnl_per_trade_stderr_usd` y `muestra_suficiente`.
  >
  > Por qué importa acá: M1 cerró julio con +$0.0643/trade sobre 519 trades y
  > una dispersión enorme. Con el criterio en dólares esa media "aprobaba"; con
  > el estadístico se ve si es señal o si es la varianza.

### market_snapshots crecía 7× en 3 días — y no era la retención (2026-07-28)

`/stats/daily`: 1,83M filas el 07-26, 7,95M el 07-27, **13,33M el 07-28 con el
día sin terminar**. Consultar ese endpoint congeló el bot.

La retención de `market_snapshots` existe y son 7 días. No era el freno que
faltaba: **el problema es el ritmo de escritura, no la ventana.** Con dos
escritores medidos:

| Escritor | Volumen | Acotado |
|---|---|---|
| Ciclo REST (`_take_snapshots`) | 50 tickers × 288 ciclos = **14,4k/día** | sí, por `MAX_TICKERS_PER_SNAPSHOT_CYCLE` |
| Handler WS (`_on_orderbook_snapshot`) | **~13,3M/día** (≈154 inserts/seg) | **no** |

El handler grababa una fila por cada frame `orderbook_snapshot`, con su propio
commit. Kalshi reemite el snapshot COMPLETO en cada (re)suscripción, así que cada
recovery del manager V2 disparaba una ráfaga de una fila por mercado seguido —
casi todas idénticas a la anterior. Es el mismo incidente de `orderbook_events`
(57GB) en otra tabla: *nada sin tope*, pero el tope tiene que estar en el ritmo.

Arreglado con el patrón anti-flood que ya usaba Motor 1 (`_record_edge_window`):
solo se persiste cuando el top-of-book **cambió**. Un libro quieto no aporta
información y ahora no escribe nada. El cache de de-dupe tiene su propio tope
(`MAX_DEDUPE_ENTRIES`), y cada ciclo loguea `capture.snapshots_deduped=N` — si
ese número es ~0 y la tabla igual crece, el de-dupe no es el freno correcto y hay
que volver a mirar.

**Y el freeze del endpoint tenía causa propia**, en el código que escribí yo:
los filtros eran `WHERE date(captured_at) >= :cutoff`. Envolver la columna en una
función anula el índice → full scan de una tabla de decenas de millones de filas.
Ahora el `date()` está solo en el SELECT/GROUP BY y el WHERE compara la columna
desnuda (los naive UTC se guardan como ISO, así que el orden lexicográfico es el
cronológico). **Regla: nunca envolver en función la columna del WHERE.**

### Por qué el criterio necesitó un piso: la división por cero (2026-07-28)

El criterio relativo, tal como estaba escrito arriba, **aprobaba a M1 — el motor
que fue diseñado para descartar**. `/stats/motors` reportaba `fees_usd: 0.00`
sobre 519 trades settleados, así que `2 × fee promedio = 0` y cualquier
PnL/trade positivo pasaba, incluido el +$0.0643 que la auditoría llamó ruido.

No era un fee real de cero: era un **fee no registrado**. `_persist_intents` de
M1 era el único de los tres motores que no guardaba `fees_cents` (M2 y REST sí),
y el `SettlementPoller` recomputaba el fee, lo descontaba del PnL y lo tiraba sin
persistirlo. El costo estaba siempre ahí, pero invisible para toda métrica.

Arreglado en tres capas:
- **Origen**: M1 guarda el fee de entrada al persistir el intent, como M2/REST.
- **Red de seguridad**: el settlement persiste el fee que efectivamente usó, sea
  cual sea el motor — ningún trade settleado puede volver a quedar sin fee. No
  pisa un `fees_cents` ya escrito (M3 lo ajusta por tramos al cerrar por CLV).
- **Honestidad del reporte**: `fees_missing_trades` y `fees_coverage_pct` en
  `/stats/motors`. El criterio final ya no usa el fee (pasó a ser estadístico),
  pero un motor con el costo subregistrado lo dice igual en su `verdict_hint`:
  cambia cómo se lee su `pnl_usd`.

Las filas históricas siguen en NULL. `python -m scripts.backfill_trade_fees`
(dry-run por defecto) las completa con el mismo valor que el settlement ya había
descontado — **el PnL registrado no cambia**. Correr con `--apply` solo después
del backup con `.backup()`.

### El edge de M8/M9 no estaba en puntos porcentuales (2026-07-28)

`/stats/edges` mostraba para `ofi`: 32.996 filas, máximo **2.678,83pp**, 1.349
filas sobre el guardarraíl de plausibilidad de 8pp, y los contadores >0pp, >1pp y
>3pp **idénticos en 16.410** — o sea ni una sola observación entre 0 y 3 puntos.
Mismo patrón en `spillover` (263 en los tres).

No es data podrida ni un artefacto de captura: **la columna no mide lo que su
nombre dice**. `edge_windows.edge_pct` es polimórfica según `kind`:

| kind | qué guarda realmente |
|---|---|
| `binary`, `multi_outcome` (M1, REST) | edge neto post-fee, en % |
| `ofi` (M8) | **z-score** de la señal (adimensional) |
| `spillover`, `spillover_exec` (M9) | **centavos** del move del trigger |

De ahí salen los tres síntomas: 2.678 es un z-score, los 1.349 "sospechosos" son
z > 8 perfectamente normales, y el hueco entre 0 y 3 existe porque el detector
solo emite con `|z| ≥ z_min` — no hay señales por debajo del umbral, por
construcción. La distribución nunca fue rara; la regla de lectura sí.

Arreglado: `/stats/edges` declara `unit` por kind, calcula los buckets en pp
**solo** donde la unidad es `pct`, y el `top_10_edges` excluye las series que no
están en porcentaje (antes los z-scores de M8 copaban el ranking y se leían como
edges enormes). El mapa autoritativo de unidades vive en `_EDGE_UNITS`
(`src/monitoring/health.py`) y está documentado en el modelo.

**Consecuencia para el mes de prueba:** M8 es el motor que la auditoría marcó
como la única promesa viva, y su métrica venía siendo ilegible. Su decisión se
toma con el p50 del **move a T+60 en centavos** (`magnitude_cents`), que siempre
estuvo bien; no con `edge_pct` leído como porcentaje.

## Nota honesta para el cierre del mes

Si al final del mes el neto es negativo, la respuesta correcta NO es "un mes más
para confirmar". La auditoría de julio ya fue ese mes; este es el segundo. Dos
meses con la misma conclusión es evidencia, no mala suerte — y "archivar un
motor que no rinde es un resultado válido y barato" (CLAUDE.md).
