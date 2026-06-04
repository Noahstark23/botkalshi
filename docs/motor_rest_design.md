# Diseño de Arquitectura — Motor REST (Mundial / soccer)

**Estado:** DISEÑO EN TEXTO para revisión adversarial. **NO implementado.** Ciclo: diseño → review → implementación → review.
**Fecha:** 2026-06-01
**Decisión origen:** análisis cerrado → Motor REST puro para el Mundial; V2 (PR #11, manager) **archivado, no se toca ni se borra** (recuperable).
**Invariantes:** `USE_ORDERBOOK_MANAGER_V2=False`. `TRADING_ENABLED` controla ejecución real. El Motor REST es **código nuevo y separado**; no modifica `orderbook_manager_v2.py`.

---

## 0. Principio arquitectónico

**Detección barata por WS, ejecución cara por REST, solo cuando vale la pena.**

El error de V2 fue mantener un orderbook completo en memoria por WS (estado mutable frágil → desync). El Motor REST **no mantiene estado de orderbook**: usa el WS solo como *campana* (ticker liviano) para decidir *cuándo* pedir el orderbook fresco por REST. El REST da una foto puntual, consistente, sin máquina de estados que se desincronice. A escala de 4-8 mercados, el costo REST por trigger es despreciable y el RTT (a validar con `bench_rest_rtt.py`, criterio P95<150ms) es aceptable.

---

## 1. Detección — suscripción WS al canal `ticker`

- Reusar `KalshiWebSocket` existente: `ws.on("ticker", self._on_ticker)` + `ws.queue_subscription(channels=["ticker"], market_tickers=[...mundial...])`.
- **Solo canal `ticker`** — NO `orderbook_delta`. Sin deltas no hay buffer, no hay seq, no hay desync, no hay recovery: **toda la clase de bugs de V2 desaparece por construcción.** El ticker es un mensaje liviano de mid/bid/ask resumido.
- Suscripción acotada a los mercados de soccer del Mundial (handful, no 38).

### 1.1 — Captura del shape del `ticker` (BLOQUEANTE 2, cerrado): paso 0 de implementación

El shape exacto del mensaje `ticker` **no está en fixtures** y el handler actual `_on_ticker` solo hace heartbeat. **El trigger NO se diseña sobre supuestos.** Paso 0, antes de escribir el trigger:

1. Suscribir el WS al canal `ticker` de unos mercados de soccer, en **modo shadow puro** (sin trigger, sin REST, sin operar — solo `logger.info(raw_msg)` de N mensajes).
2. Capturar 10-20 mensajes reales y confirmar qué traen: ¿BBO completo (`yes_bid/yes_ask/no_bid/no_ask`)? ¿solo `price`/last? ¿`spread`? ¿con qué unidades (cents vs fixed-point dollars)?
3. Guardar 1 mensaje como fixture (`tests/fixtures/ws/ticker_*.json`) para los tests del trigger.
4. **Recién entonces** diseñar la fórmula exacta del spread del paso 2 sobre los campos reales.

Esto es trabajo de implementación (turno separado), pero el diseño lo fija como **gate 0 obligatorio**: si el `ticker` resulta ser solo last-price (sin BBO), el trigger por spread **no es viable** y habría que repensar la detección (p.ej. trigger por movimiento de precio + GET de confirmación). El diseño del paso 2 abajo asume BBO; queda **condicionado a la captura**.

## 2. Trigger — spread crudo elegible dispara GET REST

> Asume que la captura (§1.1) confirma BBO en el `ticker`. Si no, repensar (ver §1.1).

- En `_on_ticker`, calcular el **spread crudo** del mensaje (`yes_ask + no_ask` vs 100, según shape real confirmado).
- Si cruza el **umbral grueso `TRIGGER_SPREAD_THRESHOLD`** (laxo a propósito — el ticker es ruidoso, filtra, no decide) → disparar `get_orderbook(ticker)` REST, **sujeto a los dos throttles (§2.1)**.
- El trigger es **barato y falible a propósito**: prefiere falsos positivos (pedir de más) sobre falsos negativos (perder ventana). REST + `detect_binary_arb` filtran sin costo de capital.

### 2.1 — Throttle por-ticker + GLOBAL anti-429 (MEJORA 1, cerrada)

Un gol mueve **muchos mercados soccer simultáneamente** → ráfaga de GETs concurrentes → riesgo de 429 justo en el momento de máxima oportunidad. Dos capas:

1. **Throttle por-ticker:** `_last_trigger_at[ticker]` (monotonic), cooldown corto (200-500ms, calibrable). Evita martillar un ticker que oscila alrededor del umbral.
2. **Throttle GLOBAL:** un limitador de tasa de GETs agregado (token-bucket o semáforo de N concurrentes + budget/seg), dimensionado bajo el rate-limit real de Kalshi (la doc dice ~200 reads/s, pero el budget operativo debe dejar headroom). Acota la ráfaga total, no solo por-ticker.

**Estrategia ante 429 / saturación del budget (priorizar por edge):** cuando hay más triggers que budget, NO atender en orden de llegada. Encolar los triggers pendientes y **atender primero el de mayor `trigger_spread`** (proxy del edge potencial) — la ventana más jugosa se mira primero. Los que no entran en el budget se descartan con `outcome=missed_429` (registrado en la instrumentación). Ante un 429 real de Kalshi, el `KalshiRestClient` ya tiene backoff/retry (verificado: maneja 429); el motor además **baja su budget global temporalmente** (circuit-breaker suave) para no amplificar la saturación.

> La interacción throttle↔429 se calibra en shadow con datos reales de cuántos triggers/seg genera un gol. El `bench_rest_arb_path.py`/`bench_rest_rtt.py` ya miden %429 bajo concurrencia → insumo para dimensionar el budget global.

## 3. Evaluación — parse + derivar asks + `detect_binary_arb` (reuso)

Reusar el path ya validado en `bench_rest_arb_path.py`:
1. `raw = await get_orderbook(ticker)` → `raw["orderbook"]["yes"|"no"]` (bids).
2. Cargar a `OrderbookState.apply_snapshot({"seq":0,"yes":...,"no":...})` (parser/validación existente).
3. Derivar asks: `yes_ask = 100 - best_no_bid`, `no_ask = 100 - best_yes_bid`.
4. `detect_binary_arb(ticker, yes_ask, yes_size, no_ask, no_size)` → `ArbOpportunity | None`.

`ArbOpportunity` ya trae lo necesario para decidir y ejecutar: `legs`, `count`, `net_profit_cents`, `edge_pct`, `fees_cents`.

## 4. Ejecución — REST, gateada por riesgo (reuso de lo existente)

**No** inventar el gate de riesgo. Reusar:
- **`RiskManager.check_pre_trade(opp)`** (ya existe, `src/risk/manager.py:44`) → `TradeDecision`. Aplica stop-loss diario/semanal/mensual, exposición, sizing. **Obligatorio antes de toda orden.**
- **`TRADING_ENABLED`** (config): si `False`, el motor corre en **modo shadow** — detecta, evalúa, **loggea la ventana, pero NO coloca orden**. Esto permite medir captura neta en vivo sin arriesgar capital hasta validar.
- Si `edge_pct >= EXECUTION_EDGE_THRESHOLD` (umbral fino, distinto del trigger grueso) **y** `check_pre_trade` aprueba **y** `TRADING_ENABLED` → ejecutar las dos patas (ver §4.1).

**Dos umbrales distintos (clave del diseño):**
- `TRIGGER_SPREAD_THRESHOLD` (grueso, paso 2): decide cuándo *mirar*. Laxo.
- `EXECUTION_EDGE_THRESHOLD` (fino, paso 4): decide cuándo *ejecutar*. Estricto, sobre el edge real post-fees de `ArbOpportunity`.

### 4.1 — Ejecución de dos patas — EL riesgo financiero del motor (refinado, 2 verificaciones cerradas)

Un arb binario requiere **ambas** patas. Si se llena una sola, el bot queda **apostador direccional con capital real**. Dos verificaciones cerradas refinan el diseño:

**VERIFICACIÓN 1 — FOK existe (Gate 0.5):** Kalshi soporta `time_in_force="fill_or_kill"` nativo (`docs/gate_0_5_fok_support.md`). → ejecución primaria por FOK.

**VERIFICACIÓN 2 — Kalshi NO tiene órdenes market** ([changelog oficial](https://docs.kalshi.com/changelog): `type=market` fue removido; solo `type="limit"`). → **cualquier rollback es limit, y un rollback limit PUEDE NO LLENARSE.** Esto cambia el diseño del rollback (ver §4.3).

#### 4.2 — Máquina de estados de ejecución por pata (4 estados, NO 3)

El error a evitar (recrea Issue #14 / viola el principio de Lección 7 "no tragar excepciones de tareas críticas"): un `asyncio.gather(return_exceptions=True)` sobre las dos patas colapsa **éxito y fallo-de-red en lo mismo** — si una pata lanza excepción de red, el bot **no sabe si la orden llegó a Kalshi y se llenó** mientras él cree que falló. Eso es exposición direccional silenciosa.

Por eso cada pata resuelve **explícitamente** a uno de **4 estados** (no 3):

| Estado | Significado | Cómo se determina |
|---|---|---|
| **FILL** | la pata se ejecutó completa | respuesta FOK confirma fill total |
| **KILL** | la pata NO se ejecutó, se canceló limpia | respuesta FOK confirma cancelación (FOK no llenó) |
| **ERROR_RED** | **estado DESCONOCIDO** — excepción de red/timeout: la orden **pudo haber llegado a Kalshi y llenado, o no** | excepción en el `place_order` (timeout, conn reset, 5xx sin body) |
| (resuelto vía reconciliación) | — | ver abajo |

**El 4º estado (ERROR_RED) es el más peligroso y NUNCA se asume como KILL.** Asumir "excepción = no se ejecutó" es precisamente el bug de Issue #14. Tratamiento explícito:

```
ejecutar ambas patas con manejo de excepción POR PATA (no gather que colapsa):
  cada pata → FILL | KILL | ERROR_RED

evaluar la combinación:
  (FILL, FILL)         → arb capturado completo. outcome=captured_full. ✓
  (KILL, KILL)         → ninguna se ejecutó (ventana se cerró). Cero exposición. outcome=missed_kill. ✓
  (FILL, KILL)         → UNA pata abierta → rollback de la pata FILL (§4.3).
  (*, ERROR_RED) o     → ESTADO DESCONOCIDO. NO decidir a ciegas.
  (ERROR_RED, *)          → RECONCILIAR: consultar la posición REAL vía API antes de rollback.
```

**Reconciliación del estado ERROR_RED (clave):** ante un `ERROR_RED`, el bot **no asume nada**. Consulta el estado real:
- `get_positions()` (existe: `kalshi_rest.py:199`) y/o `get_orders()` por `client_order_id` (existe: `kalshi_rest.py:314`) para saber si esa orden **realmente** se llenó en Kalshi.
- El `client_order_id` (UUID idempotente, ya lo usa el executor) permite localizar la orden aunque la respuesta original se haya perdido.
- Con la posición real confirmada → recién ahí decidir: si la pata huérfana se llenó → rollback; si no → cerrar el ciclo sin exposición.
- Si la consulta de reconciliación **también** falla (red sigue caída) → **kill-switch + alerta** (no operar a ciegas; ver §4.3).

Esto convierte FOK de "3 casos limpios" en "4 casos donde el 4º se resuelve por reconciliación, no por suposición". FOK reduce la *frecuencia* de patas huérfanas (KILL es limpio), pero **no elimina** el ERROR_RED — la red puede fallar después de que Kalshi llenó. Por eso la reconciliación es obligatoria aunque se use FOK.

#### 4.3 — Rollback robusto (el rollback limit PUEDE fallar — Kalshi no tiene market)

Como Kalshi **no tiene órdenes market** (Verif. 2), el rollback de una pata huérfana es una **orden limit de venta**, que puede **no llenarse** si el mercado que causó el desbalance se sigue moviendo. "Fallback = ejecución manual" es **inaceptable** para operación desatendida durante el Mundial. Diseño del rollback:

1. **Intento 1 — limit agresivo:** vender la pata huérfana a un precio que cruce el bid actual (consume liquidez disponible). Reusa la lógica de `_execute_iterative_rollback` de `executor.py` (sell agresivo, slippage cap, `max_rollback_retries`).
2. **Si no se llena → reintento acotado:** N reintentos con re-pricing al bid vigente (el bid se mueve; re-cotizar). Backoff corto.
3. **Si agota reintentos → KILL-SWITCH automático, no "esperar a Noel":**
   - Marcar la posición huérfana como **exposición abierta conocida** (persistir en DB, no perderla).
   - **Pausar el motor** (`MOTOR_REST_ENABLED` efectivo a False en runtime / `BotState.is_paused`) — deja de abrir nuevas posiciones.
   - **Alerta Telegram CRÍTICA** con el detalle (ticker, lado, size, intentos de rollback).
   - El circuit-breaker existente de `executor.py` (3 rollbacks/hora → pausa) se reusa como segunda capa.
   - La posición queda abierta y **señalizada** hasta intervención, pero el bot **no sigue operando a ciegas** ni acumula más exposición. La pérdida máxima queda acotada a esa única posición, no a un sangrado continuo.

**Principio:** el bot desatendido nunca debe quedar en un estado donde *necesita* que el operador esté despierto para no perder más. Ante lo irresoluble automáticamente: **acotar, pausar, alertar** — no seguir.

#### 4.4 — Decisión de reuso de `executor.py`

El Motor REST tiene **ejecutor propio** (`motor_rest_arb/executor.py`), porque el de Motor 1: (a) usa `gather` que colapsa estados (no tiene el ERROR_RED explícito), (b) asume `limit`+rollback sin FOK, (c) tiene el bug de Issue #14. Se **reusa** de `executor.py`: la lógica de `_execute_iterative_rollback` (sell agresivo + slippage cap) y el circuit-breaker, como componentes del §4.3 — pero la orquestación de las patas es nueva (FOK + máquina de 4 estados + reconciliación).

## 5. Instrumentación OBLIGATORIA desde día 1 — medición de captura neta

Cada vez que el trigger dispara, loguear un **registro estructurado de la ventana de edge** (tabla nueva `edge_windows` en SQLite):

| Campo | Descripción |
|---|---|
| `ticker`, `ts` | mercado y timestamp |
| `trigger_spread` | spread crudo que disparó el GET |
| `edge_pct`, `net_profit_cents` | edge real evaluado tras el REST (None si no hubo arb) |
| `outcome` | `captured_full` / `captured_partial_rolled_back` / `missed_no_arb` / `missed_risk_rejected` / `missed_shadow` / `missed_429` / `missed_latency` |
| `cycle_latency_ms` | latencia real del ciclo: trigger → GET → parse → eval → (orden enviada) |
| `rest_rtt_ms` | RTT del GET aislado (para correlacionar con el bench) |
| `leg_yes_filled`, `leg_no_filled` | **fill real de CADA pata** (no "orden aceptada"): count llenado por lado |
| `both_legs_filled` | bool — **la métrica que importa**: ¿se capturó el arb COMPLETO? |
| `rollback_triggered`, `rollback_pnl_cents`, `rollback_filled` | si hubo rollback, cuánto costó, y **si el rollback limit realmente se llenó** (§4.3) |
| `leg_states` | estado por pata: `FILL`/`KILL`/`ERROR_RED` (§4.2) |
| `reconciled` | bool — si hubo `ERROR_RED` que requirió consulta de posición real |
| `kill_switch_fired` | bool — rollback no convergió → motor pausado + alerta (§4.3) |
| `order_ids` | ids de las órdenes colocadas |

**El número que importa es `both_legs_filled`, NO `edge detectado` (MEJORA 2, cerrada).** Detectar un edge no es capturarlo; capturar una pata no es capturar el arb. La instrumentación debe registrar el **fill confirmado de ambas patas** (vía respuesta de la orden FOK o polling de fill status), porque la pregunta de negocio es *"¿ejecuté el arb completo y neto?"*, no *"¿vi el edge?"*. Sin `both_legs_filled` + `rollback_pnl_cents`, el PnL neto real es invisible.

**Por qué es el corazón del motor:** mide la **captura NETA real del Mundial en vivo** — ventanas, duración, capturadas completas vs perdidas vs parciales-con-rollback, y el costo de los rollbacks. Calibra los dos umbrales. Se enciende día 1, **incluso en shadow** (`outcome=missed_shadow`), para juntar datos antes de arriesgar capital.

## 6. Estructura de código propuesta (nueva, separada)

- `src/strategies/motor_rest_arb/` (módulo nuevo, hermano de `motor_1_arbitrage/`).
  - `engine.py` — `RestArbEngine`: wiring WS ticker + trigger + throttle + orquestación del ciclo.
  - `edge_logger.py` — el registro de instrumentación (paso 5).
- Storage: tabla `edge_windows` en `src/storage/models.py` (modelo nuevo, no toca los existentes).
- Wiring: registrado en `runner.py`/`data_capture.py` detrás de un **flag nuevo `MOTOR_REST_ENABLED` (default False)** — mismo patrón dormant que V2. No se activa hasta review + ventana de validación.
- **No toca:** `orderbook_manager_v2.py`, el flag `USE_ORDERBOOK_MANAGER_V2`, ni el path de V1 data-capture.

---

## 7. Estado de los puntos de revisión

**Bloqueantes cerrados:**
1. ✅ **Ejecución de 2 patas** — §4.1/4.2/4.3/4.4. FOK-ambas (Verif. 1: FOK existe) + **máquina de 4 estados** (FILL/KILL/ERROR_RED + reconciliación de posición real ante red incierta — NO se asume ERROR=KILL, eso era Issue #14) + **rollback robusto** (Verif. 2: Kalshi sin market → rollback limit que puede no llenarse → reintento acotado → kill-switch+alerta, NO "ejecución manual").
2. ✅ **Shape del `ticker`** — Gate 0 PASA (trae BBO, `docs/gate_0_ticker_shape.md`).
3. ✅ **Throttle global + estrategia 429** — §2.1.
4. ✅ **Instrumentación de FILL de ambas patas + estados + reconciliación** — §5. `both_legs_filled` métrica central; `leg_states`, `reconciled`, `kill_switch_fired` registrados.
5. ✅ **FOK existe** — Gate 0.5 (`docs/gate_0_5_fok_support.md`).
6. ✅ **Kalshi NO tiene órdenes market** — verificado ([changelog](https://docs.kalshi.com/changelog)); rollback rediseñado en §4.3.

> **Nota de principio:** el riesgo de `gather` que colapsa estados corresponde a **Lección 7** ("PROHIBIDO `asyncio.gather(..., return_exceptions=True)` para tareas críticas"), KALSHI_BOT_CONTEXT.md línea 410 — NO Lección 3 (que es "el edge es marginal"). El principio que aplica es el de Lección 7; el número en la directiva estaba corrido.

**Riesgos residuales abiertos (a medir en shadow / próxima revisión):**
7. **Race trigger→REST:** ~RTT de latencia; el book pudo moverse. Empírico: `cycle_latency_ms` + `outcome=missed_latency`.
8. **Calibración de umbrales** `TRIGGER_SPREAD_THRESHOLD`/`EXECUTION_EDGE_THRESHOLD` — en shadow antes de `TRADING_ENABLED`.
9. **Reuso de `RiskManager`** — confirmar que `check_pre_trade` no asume nada de Motor 1 inaplicable.
10. **`[verificar en demo]`** el envío FOK real + el comportamiento de la reconciliación `get_positions`/`get_orders` contra cuenta demo, antes de capital real.
11. **Cadencia del ticker** (de Gate 0): cada cuánto Kalshi empuja un ticker nuevo al moverse el BBO — define si la detección reacciona a tiempo. Se mide con el RTT bajo carga.

## 8. Gobernanza
- V2 (PR #11, `orderbook_manager_v2.py`) **archivado, intacto, recuperable**. No se borra.
- Motor REST = código nuevo en módulo separado, detrás de `MOTOR_REST_ENABLED=False`.
- `USE_ORDERBOOK_MANAGER_V2=False` y `TRADING_ENABLED=False` durante diseño y primera fase (shadow).
- Nada se implementa hasta que este diseño pase revisión adversarial.
