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

### 4.1 — Ejecución de dos patas (BLOQUEANTE 1, cerrado) — EL riesgo financiero del motor

Un arb binario requiere **ambas** patas (comprar YES y NO). Si se llena una sola, el bot queda **apostador direccional con capital real**. El diseño debe especificar qué pasa ante fallo de la 2ª pata.

**Análisis de `executor.py` existente (leído, `execute()` líneas 147-214):**
- Coloca **ambas patas en paralelo** (`asyncio.gather` de `_place_leg`), **simultáneas**, no secuenciales.
- Cada pata es una **orden `limit`** al `price_cents` de la `ArbLeg` (`_place_leg`, `order_type="limit"`), NO una FOK real de Kalshi.
- Si una pata lanza excepción (reject/red) → `failed=True` → `_execute_iterative_rollback`: vende las patas que SÍ se llenaron a `price=1` (agresivo, consume cualquier bid>0), con `max_rollback_retries=3`, slippage cap, y **circuit breaker** tras 3 rollbacks/hora (pausa el bot).

**El gap que el executor actual NO cubre (hay que decirlo):** el rollback se dispara solo ante **excepción** de una pata. Pero una orden `limit` puede **no fallar y tampoco llenarse** (queda resting/parcial si el precio se movió en los ~100ms del RTT). En ese caso `_place_leg` retorna OK (la orden se *aceptó*), `failed=False`, y el executor cree "all legs filled" **cuando en realidad una quedó sin ejecutar** → exposición direccional silenciosa. El executor confía en "orden aceptada = pata llena", lo cual con `limit` **no es cierto**.

**Las 3 estrategias y su trade-off:**

| Estrategia | Exposición direccional | Captura | Complejidad |
|---|---|---|---|
| **(A) FOK en ambas** (fill-or-kill nativo Kalshi) | **Cero** — si una no se llena completa, ambas se cancelan, nada queda abierto | Menor (pierde ventanas donde una pata se llena parcial) | Baja — requiere que Kalshi soporte FOK y cambiar `order_type` |
| **(B) Limit paralelo + rollback** (lo que hace executor.py hoy) | **Real** — ventana entre fill de pata 1 y rollback; el rollback puede perder plata (vende a 1¢ / slippage) | Mayor | Media — ya implementado, pero con el gap del partial no-detectado |
| **(C) Patas simultáneas IOC + verificación de fill** | Baja si se verifica fill real y se hace rollback solo del lado llenado | Media | Alta — requiere polling de fill status post-orden |

**Recomendación: (A) FOK en ambas patas, si Kalshi lo soporta.** Razón: para un motor que arranca y se valida en vivo, **cero exposición direccional > maximizar captura**. Un arb es ganancia garantizada *solo si se capturan ambas patas*; media pata es especulación con el capital de $300. FOK convierte el peor caso de "pata huérfana + rollback con pérdida" en "no pasó nada, esperamos la próxima ventana". Se sacrifica algo de captura (ventanas donde una pata se llena y la otra no) — pero esas son justo las ventanas peligrosas. El 73% de captura perfecta no sirve si el 27% restante sangra capital en rollbacks.

**Pendiente de verificación pre-implementación `[verificar contra API Kalshi]`:** ¿Kalshi soporta `order_type="fill_or_kill"` (o `time_in_force` equivalente) en `place_order`? El `_place_leg` actual usa `"limit"`. Si FOK no existe en la API, el fallback es **(C)** (IOC + verificación de fill, con rollback solo del lado ejecutado), NO (B) — porque (B) tiene el gap del partial silencioso. **Esto se confirma antes de escribir el ejecutor del Motor REST.**

**Decisión de reuso:** el Motor REST tendrá su **propio ejecutor** (o un `executor.py` parametrizado por `order_type`), NO reusa el de Motor 1 tal cual — porque el de Motor 1 asume `limit`+rollback (estrategia B, con el gap). El rollback/circuit-breaker de `executor.py` sí se reusa como **fallback** si se va por (C).

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
| `rollback_triggered`, `rollback_pnl_cents` | si hubo rollback y cuánto costó |
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

**Bloqueantes cerrados en esta revisión:**
1. ✅ **Ejecución de 2 patas (era bloqueante 1)** — cerrado en §4.1. Recomendación FOK-ambas (cero exposición direccional), con `[verificar API Kalshi soporta FOK]`; fallback IOC+verificación de fill (C), NO limit+rollback (B, tiene gap de partial silencioso). Ejecutor propio del Motor REST.
2. ✅ **Shape del `ticker` (era bloqueante 2)** — cerrado en §1.1. Gate 0: captura en shadow puro antes de diseñar la fórmula del trigger; condicionado a que el ticker traiga BBO.

**Mejoras cerradas:**
3. ✅ **Throttle global + estrategia 429** — §2.1. Por-ticker + global (token-bucket), priorización por mayor `trigger_spread` ante saturación, budget que baja ante 429.
4. ✅ **Instrumentación de FILL de ambas patas** — §5. `both_legs_filled` es la métrica central, no "edge detectado".

**Riesgos residuales que siguen abiertos (para la próxima revisión / a medir en shadow):**
5. **Race trigger→REST:** entre el ticker y el retorno del GET (~RTT) el book pudo moverse. El edge se evalúa sobre datos frescos del GET, pero la ventana pudo cerrarse. **Empírico:** lo mide `cycle_latency_ms` + `outcome=missed_latency` en vivo.
6. **Calibración de umbrales sin datos aún:** `TRIGGER_SPREAD_THRESHOLD` y `EXECUTION_EDGE_THRESHOLD` arrancan como estimación; se calibran en shadow antes de `TRADING_ENABLED`.
7. **Reuso de `RiskManager`:** confirmar que `check_pre_trade` (escrito para Motor 1) no asume nada que no aplique al caso soccer/REST.
8. **`[verificar API Kalshi]` FOK existe** (de §4.1) — determina si la ejecución va por (A) o (C). Confirmar antes de implementar el ejecutor.

## 8. Gobernanza
- V2 (PR #11, `orderbook_manager_v2.py`) **archivado, intacto, recuperable**. No se borra.
- Motor REST = código nuevo en módulo separado, detrás de `MOTOR_REST_ENABLED=False`.
- `USE_ORDERBOOK_MANAGER_V2=False` y `TRADING_ENABLED=False` durante diseño y primera fase (shadow).
- Nada se implementa hasta que este diseño pase revisión adversarial.
