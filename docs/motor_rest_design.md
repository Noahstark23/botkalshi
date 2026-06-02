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

**`[verificar contra feed real]`** El shape exacto del mensaje `ticker` no está en fixtures (el handler actual `_on_ticker` solo hace heartbeat). Antes de implementar: capturar 1 mensaje real para confirmar qué campos trae (esperado: `yes_bid`, `yes_ask`, `no_bid`, `no_ask` o `price`/`spread`). El trigger del paso 2 depende de esto.

## 2. Trigger — spread crudo elegible dispara GET REST

- En `_on_ticker`, calcular el **spread crudo** del mensaje (p.ej. `yes_ask + no_ask` vs 100, o el spread bid/ask según shape real).
- Si el spread cruza un **umbral grueso `TRIGGER_SPREAD_THRESHOLD`** (a definir; deliberadamente laxo — el ticker es ruidoso y resumido, sirve para *filtrar*, no para *decidir*) → disparar `get_orderbook(ticker)` por REST.
- **Throttle por ticker:** un `_last_trigger_at[ticker]` (monotonic) evita martillar REST si el ticker oscila alrededor del umbral. Cooldown corto (p.ej. 200-500ms) — calibrable. Esto acota el rate REST y el riesgo de 429.
- El trigger es **barato y falible a propósito**: prefiere falsos positivos (pedir orderbook de más) sobre falsos negativos (perder una ventana). El REST + `detect_binary_arb` filtran los falsos positivos sin costo de capital.

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
- Si `edge_pct >= EXECUTION_EDGE_THRESHOLD` (umbral fino, distinto del trigger grueso) **y** `check_pre_trade` aprueba **y** `TRADING_ENABLED` → emitir las patas vía `place_order(...)` (una por `ArbLeg`). Evaluar reuso de `executor.py` (FOK paralelo + rollback ya existente) para la ejecución de las dos patas — **a confirmar en review** si aplica al caso binario REST o si se hace una ejecución más simple.

**Dos umbrales distintos (clave del diseño):**
- `TRIGGER_SPREAD_THRESHOLD` (grueso, paso 2): decide cuándo *mirar*. Laxo.
- `EXECUTION_EDGE_THRESHOLD` (fino, paso 4): decide cuándo *ejecutar*. Estricto, sobre el edge real post-fees de `ArbOpportunity`.

## 5. Instrumentación OBLIGATORIA desde día 1 — medición de captura neta

Cada vez que el trigger dispara, loguear un **registro estructurado de la ventana de edge** (a SQLite, tabla nueva `edge_windows`, o log estructurado JSON):

| Campo | Descripción |
|---|---|
| `ticker`, `ts` | mercado y timestamp |
| `trigger_spread` | spread crudo que disparó el GET |
| `edge_pct`, `net_profit_cents` | edge real evaluado tras el REST (None si no hubo arb) |
| `outcome` | `captured` / `missed_no_arb` / `missed_risk_rejected` / `missed_shadow` / `missed_429` / `missed_latency` |
| `cycle_latency_ms` | latencia real del ciclo completo: trigger → GET → parse → eval → (orden enviada) |
| `rest_rtt_ms` | RTT del GET aislado (para correlacionar con el bench) |
| `executed`, `order_ids` | si se colocó orden y cuáles |

**Por qué es el corazón del motor, no un extra:** mide la **captura NETA real del Mundial en vivo** — cuántas ventanas hubo, cuánto duraron, cuáles se capturaron vs se perdieron y *por qué* (no-arb, riesgo, latencia, shadow). Sin esto, no hay forma de saber si el motor funciona ni de calibrar los dos umbrales. Se enciende desde el día 1, **incluso en modo shadow** (`outcome=missed_shadow`), para juntar datos antes de arriesgar capital.

## 6. Estructura de código propuesta (nueva, separada)

- `src/strategies/motor_rest_arb/` (módulo nuevo, hermano de `motor_1_arbitrage/`).
  - `engine.py` — `RestArbEngine`: wiring WS ticker + trigger + throttle + orquestación del ciclo.
  - `edge_logger.py` — el registro de instrumentación (paso 5).
- Storage: tabla `edge_windows` en `src/storage/models.py` (modelo nuevo, no toca los existentes).
- Wiring: registrado en `runner.py`/`data_capture.py` detrás de un **flag nuevo `MOTOR_REST_ENABLED` (default False)** — mismo patrón dormant que V2. No se activa hasta review + ventana de validación.
- **No toca:** `orderbook_manager_v2.py`, el flag `USE_ORDERBOOK_MANAGER_V2`, ni el path de V1 data-capture.

---

## 7. Puntos para la revisión adversarial (los que yo atacaría)

1. **Shape del `ticker` `[verificar]`:** todo el trigger depende de campos que aún no confirmé contra el feed real. Bloqueante menor: capturar 1 mensaje antes de implementar.
2. **Race trigger→REST:** entre que el ticker dispara y el GET vuelve (RTT ~100ms), el book pudo moverse. El orderbook REST es la verdad al momento del GET, no del trigger → el edge se evalúa sobre datos frescos, pero la ventana pudo cerrarse en esos ms. **La instrumentación (`cycle_latency_ms`, `outcome=missed_latency`) mide exactamente esto** — es la pregunta empírica que el motor responde en vivo.
3. **Ejecución de 2 patas:** ¿`executor.py` (FOK+rollback) o ejecución directa? El arb binario necesita ambas patas o ninguna; una pata sin la otra es exposición direccional. Definir en review.
4. **Rate REST / 429 bajo ráfaga:** si muchos tickers disparan a la vez (gol → varios mercados se mueven), la ráfaga de GETs podría tocar 429. El throttle por-ticker ayuda; el `bench_rest_arb_path.py` ya mide %429 bajo concurrencia. Confirmar headroom.
5. **Dos umbrales calibrables sin datos aún:** `TRIGGER_SPREAD_THRESHOLD` y `EXECUTION_EDGE_THRESHOLD` arrancan como estimación; la instrumentación en modo shadow los calibra antes de `TRADING_ENABLED`.
6. **Reuso de `RiskManager`:** confirmar que `check_pre_trade` no asume nada de Motor 1 que no aplique acá.

## 8. Gobernanza
- V2 (PR #11, `orderbook_manager_v2.py`) **archivado, intacto, recuperable**. No se borra.
- Motor REST = código nuevo en módulo separado, detrás de `MOTOR_REST_ENABLED=False`.
- `USE_ORDERBOOK_MANAGER_V2=False` y `TRADING_ENABLED=False` durante diseño y primera fase (shadow).
- Nada se implementa hasta que este diseño pase revisión adversarial.
