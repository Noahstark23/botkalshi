# Checklist de activación con capital — Motor REST

**Estado (2026-06-10):** el **cable está COMPLETO en `main`** (Capas 1+2+3, PRs #26/#27/#28):
detección → umbral fino → `check_pre_trade` → resize → `execute()` → Telegram → EdgeWindow,
detrás del muro de 3 capas (A: construcción gateada; B: guard en el path; C: `place_order`
bloquea entradas). **NO toca capital**: `TRADING_ENABLED=False` y el cable nunca corrió en
vivo — producción sigue en shadow. Próximo paso: **demo en ventana muerta** (Opción B).

---

## ✅ GATE DEL SENSOR DE FILL — CERRADO (validado contra API viva, 2026-06-05)

El sensor de fill (`src/strategies/motor_rest_arb/`) fue validado mandando una orden
`fill_or_kill` REAL en cuenta demo y capturando las respuestas crudas. Las 3 rutas de la
máquina de estados están confirmadas contra la API viva de Kalshi:

- [x] **FILL → FILL** — HTTP 200 + `fill_count_fp="1.00"`, `remaining_count_fp="0.00"`,
  `status="executed"`. `_create_order_filled` lo lee correctamente (incluido el sufijo `_fp`:
  los campos vienen como fixed-point strings; el parser castea bien).
- [x] **KILL → KILL** — HTTP **409** + `error.code="fill_or_kill_insufficient_resting_volume"`.
  Hallazgo de la prueba: Kalshi modela el KILL como 409, NO como order object. El sensor ahora
  lo detecta con match estricto (`status_code==409 AND error_code conocido`) y devuelve KILL
  determinístico — sin reconciliar. (PR #22.)
- [x] **ERROR_RED → reconcilia** — excepción de red/timeout o cualquier OTRO 409/code → estado
  desconocido → reconciliación de doble fuente (`get_orders` + `get_positions`).

**Validado en la máquina de estados, no solo en el sensor aislado:** el caso FILL/KILL-409 →
rollback INMEDIATO de la pata llena, sin reconciliación demorada (test
`test_fill_plus_kill409_rolls_back_immediately_no_reconcile`).

> **`[verificar]` residual menor:** `fill_or_kill_insufficient_resting_volume` es el único
> error code de "FOK no llenó" observado. Si en producción aparece otro 409 relacionado a FOK,
> se suma a `_FOK_KILL_ERROR_CODES`. Fallo conservador hasta entonces (code desconocido →
> ERROR_RED → reconcilia, nunca exposición silenciosa).

---

## ✅ RESUELTO (re-auditado 2026-06-16): RiskManager YA VE al Motor REST

> El veredicto 🔴 abajo era de 2026-06-10. Re-auditado contra el código de hoy,
> el bloqueante está **resuelto en las 3 patas** por PRs posteriores. Se deja el
> diagnóstico original como contexto histórico, tachado por la resolución.

Verificación contra el código (read-only, 2026-06-16):

- (a) **Exposición** — `_persist_intents` (`motor_rest_arb/executor.py:500`) graba una
  fila `Trade` por pata (`status='pending'`, `strategy='motor_rest_arb'`,
  `notes='arb_id=…'`) **antes de tocar la red**; si la DB falla → **ABORTA sin operar**.
  El RiskManager la lee en `_get_current_exposure_usd` (`manager.py:131`), y el
  `notes='arb_id='` alimenta el descuento de hedge (`manager.py:142`). ✅
- (b) **Stop-losses** — `SettlementPoller` (`settlement.py:124`) settlea los `Trade`
  `filled` de `motor_rest_arb` escribiendo `pnl_cents` + `status='settled'` +
  `settled_at` **atómico por arb_id** (`settlement.py:209`); inyectado con el adaptador
  **REAL** `KalshiSettlementSource` en prod (`runner.py:172`, corre siempre). Los
  stop-losses (`manager.py:210`) ya tienen de dónde leer. ✅
- (c) **Pausa persistente** — `_update_trade` crítico fallido → pausa preventiva
  (`executor.py:566`); `engage_kill_switch` persiste en `OperationalState`
  (`manager.py:251`, tabla `operational_state`) → sobrevive restarts de Coolify. ✅

**Residual de código (único):** `[verificar en smoke]` el shape EXACTO del campo
`result` de `get_market` contra un mercado del Mundial ya resuelto
(`KalshiSettlementSource.get_resolution`). Cubierto por
`scripts/inspect_settlement_shape.py` (read-only, sin capital). Fase 1 del go-live.

<details><summary>Diagnóstico original 2026-06-10 (histórico — ya resuelto)</summary>

- El RiskManager lee **SOLO la tabla `Trade`**: exposición = `Trade` con
  `status in ('pending','filled')` (manager.py:95); stop-losses −3/−8/−15% = `Trade` con
  `status='settled'` + `pnl_cents` (manager.py:148-150).
- El cable del Motor REST **NO escribe `Trade`**: `execute()` → `ExecutionOutcome` → solo
  EdgeWindow. Único escritor de `Trade` en el codebase: Motor 1 (`_persist_intents`).
- Consecuencias: (a) pérdidas del Motor REST (p.ej. rollbacks) **jamás disparan stop-loss**;
  (b) `check_pre_trade` ve **$0 de exposición** del propio motor → el cap de exposición
  simultánea (25%) no acumula sus posiciones; (c) el kill-switch del Motor REST pausa solo
  el `RestExecutor` **en memoria** (no `BotState.is_paused`) → un restart borra la pausa.

</details>

---

## Resto del checklist (gates estándar de trading)

- [ ] **7 días sin crashes** en producción — **EN CERO**: el cable nunca corrió; el reloj
  arranca con el deploy post-demo.
- [ ] **Arbitrajes detectados en logs** — **NO cumplido** (`edge_windows=0` a la fecha); lo
  llena la data del Mundial en shadow.
- [x] **RiskManager apto para Motor REST** — ✅ resuelto (ver sección de arriba, re-auditado
  2026-06-16): intents + settlement real + pausa persistente. Deuda menor vigente documentada
  (manager.py:42-50: PnL realized-only por decisión del owner, residual de lock con N=1 motor).
  Smoke del shape de `result` ✅ CONFIRMADO 2026-06-16 (`scripts/inspect_settlement_shape.py`:
  campo `result` ∈ {yes,no}, `KalshiSettlementSource` lo mapea bien).
- [ ] **Cap de 5% por trade** confirmado activo (`MAX_TRADE_SIZE_PCT=5.0`); sizing por caps,
  **cero Kelly** (decisión 2026-06: en arb p≈1, Kelly diría 100% del bankroll).
- [ ] **Smoke test de `place_order` contra producción** — pendiente, post-demo.
- [x] **Wiring de `execute()`** detrás de `TRADING_ENABLED` — ✅ HECHO (cable Capas 1+2+3,
  PRs #26/#27/#28, con muro de 3 capas y review por capa). Falta validarlo en demo.

---

## Pendientes menores documentados (no bloquean, validar/ajustar en demo)

- **No-reentrancia del circuit breaker:** `execute()` no debe llamarse concurrentemente sobre
  la misma instancia (`_paused`/`_rollback_timestamps` sin lock). El caller debe serializar.
  Documentado en el docstring de `RestExecutor`.
- **Rollback sell a 1¢ sin re-cotizar:** liquidación agresiva (a 1¢ debería cruzar cualquier
  bid > 0). Si el book está vacío, no se llena → kill-switch (correcto). Validar en demo.
- **Reconciliación de doble fuente:** `get_orders` (por `client_order_id`) + `get_positions`.
  Discrepancia o fallo de cualquiera → exposición (rollback). Validar shapes en demo.
## Fase 2 (acumular + validar shadow) — evidencia 2026-06-16

Camino del canary = **Motor REST** (arb 1X2/binary, hedged p≈1). Validación con
`scripts/report_edge_windows.py --breakdown` sobre ~3 días de shadow (3054 EdgeWindows,
todas multi_outcome):

- **Señal real y repartida:** 653/3054 cruzan el umbral de ejecución (≥1.5%); 98% en
  bandas sanas (64% en 1.5–3%, 34% en 3–10%), solo 2% en la cola >10%. 22 eventos
  distintos, ninguno domina (máx ~16%). Medianas por evento ~2%; los picos (best 132%)
  son ticks outlier aislados que NO contaminan la mediana → estructura de arb genuino.
- **Pendiente Fase 2 (time-bound):** confirmar **consistencia multi-día** cuando baje el
  volumen del Mundial (que los 653 no sean un pico de fin de semana cargado).
- **Salvaguarda añadida:** techo `MOTOR_REST_MAX_EDGE_PCT` (default 10%, ver § toggles):
  el edge de cola (ej. 132%) es casi seguro fantasma (pata ~0¢ / mercado por resolver);
  se graba pero NO se ejecuta. Cierra la pérdida #1 del canary. Con test
  (`test_above_max_edge_does_not_execute`).

**Antes del canary de $100 (Motor REST) quedan:** (1) consistencia multi-día, (2) cerrar
el hardening de void/refund (abajo). El techo anti-fantasma ✅ ya está.

---

- **Settlement sin rama de refund/void (HARDENING, baja prioridad):** `_leg_pnl_cents`
  (`settlement.py:107`) solo tiene rama ganadora/perdedora; un grupo multi-outcome que se
  voidee (cero ganadores, todas las patas `no`) se contaría como pérdida total → PnL fantasma.
  **Evidencia (2026-06-16):** escaneo de 22 eventos settled (`scripts/scan_settled_voids.py`)
  encontró **0 voids reales** — el único 🚩 (`KXWCGROUPWIN-26K`) era un grupo a medio jugar
  (2/6 patas finalized), confirmado con `scripts/inspect_event_legs.py`. No es mundo (A) ni (C):
  Kalshi marca las patas no resueltas con `result=''`, no con un status/result especial.
  **Plan:** fix defensivo a nivel GRUPO (grupo COMPLETO con cero ganadores → refund) con test
  sintético; **cerrar antes del canary de Fase 3**, sin urgencia hoy (no hay caso real que lo
  dispare). El scanner ya exige grupo completo (`get_event`) para no confundir void con stale.

---

## Estado del frente Motor REST (resumen)

| Componente | Estado |
|---|---|
| Detección (shadow, `RestArbEngine`) | ✅ en main (graba EdgeWindow + heartbeat con diag de size) |
| Ejecución (`RestExecutor`) | ✅ en main, sensor validado en API viva |
| Sensor de fill validado en API viva (FILL/KILL/ERROR_RED) | ✅ **cerrado** (demo, PR #22) |
| Wiring `execute()` (cable Capas 1+2+3, muro A/B/C) | ✅ en main (PRs #26/#27/#28) — **sin validar en demo** |
| Demo end-to-end (Telegram suena + EdgeWindow poblado) | ❌ pendiente (ventana muerta, Opción B) |
| RiskManager VE al Motor REST (Trade rows) | ✅ resuelto (intents + settlement + pausa persist.) — falta smoke del shape `result` |
| Runbook kill-switch manual | ✅ `docs/runbook_kill_switch.md` |
