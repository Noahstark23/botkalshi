# Motor 5 — Market Maker · Plan de integración por fases (F0→F3)

> **Estado:** plan aprobable. F0 (discovery) EJECUTADO el 2026-07-01 contra el código real
> de `main` (c123683) — no contra docstrings ni memoria (Lección 8). F1–F3 NO implementan
> nada todavía: cada fase tiene gates explícitos y la transición entre fases la decide Noel.
>
> **Naming:** Motor 4 ya es el cross-venue Kalshi↔Polymarket (`docs/motor_4_cross_venue_fase0.md`),
> así que el market maker es **Motor 5**. Prefijo de config propuesto: `MOTOR_MM_*`.
>
> **Bucket:** 🔴 crítica (toca sizing, executor de trades y — en F2 — queries del
> RiskManager). Workflow completo de 3 capas + human gate en cada transición de fase.

---

## 0. Tesis y por qué por fases

Cotizar bid/ask alrededor de un precio justo y cobrar el spread. El fair value de referencia
ya existe (Motor 2: consenso sportsbook sin vig); el book en tiempo real ya existe
(OrderbookManagerV2). Lo que NO existe es todo lo que hace seguro tener **órdenes resting
bilaterales vivas**: post-only, batch cancel, fill feed, reserva de capital, reconciliación
runtime. Por eso el plan es shadow-first (como Motor 3, que se construyó FASE 1→4 en shadow
sin un solo dólar expuesto) y cada fase produce evidencia medible antes de exponer nada.

El sistema de contención existente ya demostró funcionar dos veces (Lección 9: dos rollbacks
< 5 min, cero daño). El plan reusa esa maquinaria en vez de inventar una nueva.

---

## 1. F0 — Discovery (EJECUTADO 2026-07-01)

Cuatro tracks contra el código real. Lo que sigue es el estado empírico, con referencias.

### 1.1 Patrón de integración (molde: Motor 3)

Motor 3 (CLV) **sí existe** — construido 2026-06-16→28 en 4 fases shadow-first
(`docs/estrategias.md` está desactualizado en ese punto). Es el molde a replicar:

- **Estructura:** `engine.py` (orquestador con `LOOP_INTERVAL_SEC`), `poller.py` (sync de
  estado externo), `detector.py` (lógica pura testeable), `executor.py` (órdenes + lock
  por-ticker), `__init__.py` (docstring).
- **Wiring:** `runner.py:179-206` (`_run_motor3_clv`): gate por flag → `asyncio.sleep(20)`
  post-boot → try/except con `logger.exception` + `BotState.record_error` → `run(stop_event)`.
- **Gating en capas:** Capa A = el executor **solo se construye** si
  `TRADING_ENABLED AND MOTOR_3_EXECUTION_ENABLED` (`engine.py:74-75`); Capa B = guard
  `if self._executor is not None` en el tick (`engine.py:137`); Capa C = `place_order`
  bloquea entradas con `TRADING_ENABLED=false` (`kalshi_rest.py:371-384`).
  ⚠️ Igual que en Motor 3, **Capa C no frena `sell`** (protección de rollback) — para un MM
  que también vende, la protección real en shadow es Capa A.
- **Supervisor:** loop explícito `while not stop_event.is_set(): try: tick / except: log +
  record_error + sigue` (`engine.py:78-86`). PROHIBIDO `gather(return_exceptions=True)`
  (Lección 7).
- **Shadow logging:** prefijos `[MOTOR 3 SHADOW]` / `[MOTOR 3 TP SHADOW]` con PnL neto de
  fees en el log — el shadow loguea exactamente lo que live ejecutaría.

### 1.2 Fair value de Motor 2 (el precio de referencia)

- **Shape:** `ConsensusSignal.odds_api_fair_prob` — `float [0.0, 1.0]`, 4 decimales,
  no-vig **multiplicativo** (`remove_vig_multiplicative`, `src/math/no_vig.py:44-58`),
  promediado sobre bookmakers (`detector.py:105-138`).
- **Vida útil:** in-memory por ciclo del poller (300s default). **NO se persiste el valor**
  (EdgeWindow guarda solo derivados: `edge_pct`, `gross_spread_cents`, `fees_cents`) y
  **no tiene timestamp propio** ni consumidor fuera de Motor 2.
- **Staleness:** guardarraíl pre-match (`commence_time <= now` → skip); sin campo de
  "última actualización".
- **Gap para el MM:** necesita un canal de consumo. Decisión de diseño (F1): registry
  in-memory compartido (`FairValueBook`: ticker → (fair_prob, computed_at)) poblado por el
  poller de Motor 2, con TTL explícito. Persistirlo en DB es opcional y NO bloquea F1
  (el shadow puede loguear el fair usado en cada quote).

### 1.3 RiskManager y resting orders (el gap más grande)

El RiskManager está diseñado para órdenes **inmediatas** (IOC/FOK). Todos los motores
actuales llaman `check_pre_trade()` y ejecutan al instante; nadie deja órdenes descansando.

| Gap | Hoy | Impacto en MM |
|---|---|---|
| Capital reservado vs expuesto | `pending` y `filled` suman igual a exposición (`manager.py:356-402`) | Quotes bilaterales consumen 2× cap aunque nada llene |
| Reconciliación runtime de resting | Solo al boot (Motor 1) o en ERROR_RED (Motor REST) | Orden cancelada por Kalshi/UI → `pending` fantasma bloquea cap para siempre |
| Fill parcial | No hay `filled_count` (solo `count` total) | 500/1000 llenados = exposición contada como 1000 |
| Fill feed en runtime | Canal WS `fill` existe (`kalshi_ws.py:46`) pero sin handler | Una pata llena y la opuesta queda viva sin enterarse |
| PnL no realizado | Stop-loss solo cuenta `settled` (`manager.py:454`) | Inventario del MM bajo agua es invisible al stop-loss |
| Pausa de cotización vs pausa total | Kill-switch pausa TODO | No hay modo "no cotices más pero gestiona/cancela lo abierto" |

**Reusable tal cual:** intents pre-red (`_persist_intents`, patrón Motor REST), kill-switch
persistente (`OperationalState`), circuit breaker de rollbacks, `effective_capital_usd()`.

En shadow (F1) **ninguno de estos gaps bloquea** — no hay órdenes. Se cierran en F2, como
cambios 🔴 al RiskManager con su propio ciclo de aprobación.

### 1.4 Capacidades del cliente Kalshi (REST/WS)

- ✅ Ya existe: `place_order` V2 (`/portfolio/events/orders`, todo cotizado desde el libro
  YES como bid/ask en dólares string, `client_order_id` obligatorio,
  `self_trade_prevention_type="taker_at_cross"`), TIF `gtc|fok|ioc`, `cancel_order`,
  `get_orders/fills/positions`, retry con backoff, clasificación de errores.
- ❌ **No existe** (gaps a construir): `post_only` (no está en el body que enviamos),
  batch create (`POST /portfolio/orders/batched`), batch cancel (`DELETE .../batched`),
  amend/replace, handler del canal WS `fill`, throttle preventivo de writes (límite Kalshi:
  20 writes/seg; hoy solo hay retry reactivo).
- **OrderbookManagerV2:** `get_top_of_book(ticker, side)` + `total_size()` + `stats()` —
  API suficiente para el shadow. ⚠️ Su historia (Lección 9, causa raíz cerrándose con
  #119 stale-books) obliga a que el MM **verifique `is_stale`/`initialized` antes de usar
  un book** y trate book stale = no cotizar ese ticker.

**Residual F0 (verificar en F2 contra API viva, no antes):** si la API de Kalshi soporta
`post_only` y los endpoints batched, y con qué semántica exacta (el repo no los usa hoy;
la doc pública dice que existen — validarlo empíricamente en demo como se validó el sensor
FOK, `docs/checklist_activacion_capital.md`). Si `post_only` NO existe en la API, el
fallback es chequear el book pre-envío + `self_trade_prevention` + precio nunca cruzando
el spread — decisión documentada en F2.

---

## 2. Arquitectura propuesta (constante en todas las fases)

```
src/strategies/motor_5_mm/
├── __init__.py       docstring de alto nivel
├── engine.py         Motor5Engine: loop supervisado, gating Capa A, cadence
├── quoter.py         PURO: fair_prob + book + inventario → QuoteSet (bid/ask/size) o None
├── shadow_fill.py    PURO (F1): QuoteSet + stream del book → fills hipotéticos
├── inventory.py      posición neta por ticker + skew de quotes por inventario
├── executor.py       (F2+) place/cancel real, lock por-ticker, batch wrapper
└── reconciler.py     (F2+) get_orders()/fill feed ↔ tabla Trade
```

- **Config:** `MOTOR_MM_ENABLED=False` (correr), `MOTOR_MM_EXECUTION_ENABLED=False`
  (Capa A), `MOTOR_MM_SERIES`, `MOTOR_MM_MAX_TICKERS`, `MOTOR_MM_HALF_SPREAD_CENTS`,
  `MOTOR_MM_QUOTE_SIZE_CONTRACTS`, `MOTOR_MM_MAX_INVENTORY_CONTRACTS`,
  `MOTOR_MM_FAIR_TTL_SEC`. Shadow = `ENABLED=True + EXECUTION=False` (idéntico a Motor 3).
- **Storage (F1):** tabla nueva `mm_quotes` (quote emitida por tick: ticker, fair_prob,
  bid/ask, size, book top en ese instante) y `mm_shadow_fills` (fill hipotético: quote_id,
  lado, contratos, precio, motivo). Más `MMFunnelSnapshot` por ciclo (patrón
  `Motor2FunnelSnapshot`) para el loop de ingeniería.
- **Trades reales (F2+):** filas `Trade` con `strategy="motor_5_mm"`, intents pre-red,
  y los campos nuevos que salgan del trabajo 🔴 de RiskManager (`filled_count`, reserva).
- **Regla de oro (Lección 9, decisión 2):** el estado del MM (inventario, quotes vivas) es
  una state machine mutable → cualquier excepción en su aplicación de estado marca el
  ticker como corrupto y fuerza re-sync, nunca "sigue operando".

---

## 3. F1 — Shadow (mínimo 2 semanas de tracker)

**Qué es:** calcular quotes reales contra el book real y loguear fills hipotéticos.
**Cero órdenes** — ni demo ni producción. Todo el motor corre con `EXECUTION=False`
(el executor ni se construye).

### Alcance

1. `FairValueBook` compartido: Motor 2 publica `(ticker, fair_prob, computed_at)` por ciclo;
   el MM lo consume con TTL (`MOTOR_MM_FAIR_TTL_SEC`, propuesta inicial 600s = 2 ciclos).
   Sin fair fresco → no se cotiza ese ticker (y se cuenta en el funnel como `skip_stale_fair`).
2. `quoter.py`: quotes alrededor del fair (half-spread configurable), con skew por
   inventario simulado y clamp [1,99]¢. Función pura con tests.
3. `shadow_fill.py`: contra el stream del book (V2 si está activo; fallback REST
   `get_orderbook` a cadence del loop), regla conservadora: un fill hipotético ocurre solo
   si el mercado **cruza** el precio de la quote (trade printeado o book que atraviesa),
   no si apenas lo toca. Documentar la regla en el docstring — es la fuente #1 de
   sobre-optimismo en backtests de MM.
4. PnL shadow: mark-to-market del inventario simulado + spread capturado, **neto de fees
   reales** (`kalshi_fee_cents`, fórmula exacta — anti-patrón: 7% flat).
5. `MMFunnelSnapshot` por ciclo + integración al `analyst_loop` (digest Telegram existente).
6. Wiring en `runner.py` (patrón `_run_motor3_clv`) + `/status` con bloque `motor_5_mm`.

### Gates F1 → F2 (todos, medidos sobre ≥ 14 días de tracker continuo)

- [ ] ≥ 14 días de `mm_quotes`/`mm_shadow_fills` sin huecos > 1h no explicados.
- [ ] PnL shadow neto de fees **positivo y estable** (mediana diaria > 0; sin depender de
      1-2 días outlier), con la regla conservadora de fills.
- [ ] Inventario simulado respeta `MAX_INVENTORY` (cero excursiones sin explicación).
- [ ] Cero errores del motor en logs (mismo estándar que runbook 12.5: un ERROR nuevo
      sostenido = se investiga antes de avanzar).
- [ ] Selección de mercados decidida con datos del funnel (qué series/tickers cotizan bien).
- [ ] Revisión adversarial del shadow-fill model (¿los fills hipotéticos son creíbles
      contra `trade` prints reales?).

---

## 4. F2 — Demo con órdenes reales

**Qué es:** el mismo motor, ejecutando contra `demo-api.kalshi.co` con llaves demo.
La data de demo es sintética (anti-patrón conocido: demo ≠ producción) — F2 **no valida el
edge**, valida la **mecánica**: semántica de la API, ciclo de vida de órdenes resting,
reconciliación, y los cambios de RiskManager.

### Alcance

1. **Cliente (🟡):** `post_only` (o su fallback documentado si la API no lo da), batch
   create/cancel, throttle preventivo de writes (techo interno ~25% del límite = 5/seg),
   handler del canal WS `fill`. Cada capability validada contra la API viva de demo con
   respuestas crudas capturadas (precedente: validación del sensor FOK, PR #22).
2. **RiskManager (🔴, ciclo de aprobación propio + 48h monitoring por cambio):**
   `Trade.filled_count`, distinción reservado (resting) vs expuesto (filled) en
   `_get_current_exposure_usd()`, y modo `quotes_paused` (para de cotizar, sigue
   gestionando/cancelando). Stop-losses intactos.
3. **`reconciler.py`:** loop periódico `get_orders()` ↔ `Trade` por `client_order_id` +
   fills por WS; toda discrepancia → cancel-all del ticker + re-sync (estado corrupto no
   opera, Lección 9).
4. **Kill-switch del MM:** cancel-all-quotes < 5 seg como acción de pánico, integrada al
   kill-switch persistente existente; probada en demo, cronometrada.

### Gates F2 → F3

- [ ] Matriz de validación API cerrada: post_only (o fallback), batch cancel, fill feed,
      partial fills, cancel bajo carga — cada una con evidencia cruda capturada.
- [ ] Reconciler sobrevive un fin de semana en demo sin `pending` fantasma ni divergencia
      bot↔Kalshi.
- [ ] Cancel-all < 5 seg medido, incluida la ruta de kill-switch.
- [ ] Cambios de RiskManager mergeados con tests E2E (patrón de los existentes) y 48h de
      monitoring post-merge sin regresión de los motores actuales.
- [ ] Runbook de activación/rollback del MM escrito (molde: runbook 12.5 — criterios
      numéricos literales, sin discreción en mitad de incidente).

---

## 5. F3 — Producción

Solo tras F2 completo. Tres pre-condiciones **no negociables**, y el orden importa:

1. **Smoke test** contra producción: 1 quote unilateral post-only de 1 contrato en un
   mercado de bajo volumen, cancelada a los N segundos — valida auth, mapeo V2 y cancel
   en el ambiente real (equivalente al "smoke de `place_order`" del checklist Motor REST).
2. **Canonicalización mergeada** a `main` (workstream separado; F3 no arranca con ese
   trabajo pendiente en rama).
3. **OK explícito de Noel** — decisión documentada en el commit del flag, como exige la
   sección 5 del contexto. Sin OK, no hay activación, sin importar cuán verde esté todo.

Más los gates estándar que ya rigen: checklist completo de `TRADING_ENABLED` (sección 5),
capital canary topado (propuesta: $100, techo duro por config), ventana de supervisión
activa de 2-3h con Telegram verificado y backup de DB < 30 min (molde runbook 12.5), y
criterios de rollback numéricos definidos ANTES de la ventana. Primera semana: revisión
diaria del digest + límite de inventario a la mitad del valor de demo.

---

## 6. El loop de ingeniería (transversal a F1–F3)

Cada fase instrumenta ANTES de actuar, igual que Motor 2:

```
OBSERVAR   MMFunnelSnapshot por ciclo (tickers evaluados, skips por causa, quotes emitidas,
           fills shadow/reales, spread medio, inventario, PnL neto)
ANALIZAR   analyst_loop extendido con agregador MM (funciones puras, patrón FunnelAgg)
RECORDAR   veredicto comparable día a día (patrón AnalystVerdict)
REPORTAR   digest Telegram existente — el humano lee, decide, recalibra config
ITERAR     cambios de parámetros SOLO por config, con el dato del funnel como justificación
```

Regla anti-Lección-9: ningún avance de fase por "vamos atrasados". Las fechas de este plan
son estimaciones, los gates son la autoridad. Un gate rojo detiene la fase, se documenta la
causa, y se re-entra por el loop.

---

## 7. Riesgos conocidos y decisión de no-hacer

- **Dependencia de V2:** si `USE_ORDERBOOK_MANAGER_V2` sigue apagado al llegar F1, el
  shadow arranca con REST polling (peor resolución de fills hipotéticos, aceptable) y
  migra a V2 cuando esté estable. El MM nunca consume un book `is_stale`.
- **Adverse selection:** el MM pierde contra flujo informado. El fair de Motor 2 refresca
  cada 300s — en mercados rápidos eso es viejo. Mitigación F1: medirlo (¿los fills shadow
  ocurren justo antes de movimientos del fair?); si el patrón aparece, el motor cotiza
  solo mercados lentos o no pasa de F1. Ese resultado también es éxito del plan.
- **Fees:** la fórmula real de Kalshi castiga precios cerca de 50¢. El quoter usa
  `kalshi_fee_cents` exacto en el cálculo de spread mínimo rentable, desde F1.
- **No-hacer (por ahora):** amend/replace (cancel+create es suficiente a este volumen),
  multi-nivel de profundidad (un nivel por lado), ML de pricing (anti-patrón: nada de
  modelos en el hot path), y cotizar series que Motor 2 no cubre (sin fair, no hay quote).

---

**FIN — Motor 5 Market Maker · plan F0→F3**
