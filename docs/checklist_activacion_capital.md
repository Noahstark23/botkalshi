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

## 🔴 BLOQUEANTE pre-plata real: RiskManager CIEGO al Motor REST (verificado 2026-06-10)

Rastreo contra el código real (read-only):

- El RiskManager lee **SOLO la tabla `Trade`**: exposición = `Trade` con
  `status in ('pending','filled')` (manager.py:95); stop-losses −3/−8/−15% = `Trade` con
  `status='settled'` + `pnl_cents` (manager.py:148-150).
- El cable del Motor REST **NO escribe `Trade`**: `execute()` → `ExecutionOutcome` → solo
  EdgeWindow. Único escritor de `Trade` en el codebase: Motor 1 (`_persist_intents`).
- Consecuencias: (a) pérdidas del Motor REST (p.ej. rollbacks) **jamás disparan stop-loss**;
  (b) `check_pre_trade` ve **$0 de exposición** del propio motor → el cap de exposición
  simultánea (25%) no acumula sus posiciones; (c) el kill-switch del Motor REST pausa solo
  el `RestExecutor` **en memoria** (no `BotState.is_paused`) → un restart borra la pausa.

**Fix (tarea aparte, diseño → review → código):** el cable persiste `Trade` rows
(`strategy="motor_rest_arb"`, intents pre-red + status post-outcome, patrón Motor 1) →
alimenta exposición y stop-losses sin tocar `manager.py`. Resolver además settlement
(`settled_at`/`pnl_cents`). **NO bloquea la demo** (capital ficticio); **SÍ bloquea**
`TRADING_ENABLED=true` real.

---

## Resto del checklist (gates estándar de trading)

- [ ] **7 días sin crashes** en producción — **EN CERO**: el cable nunca corrió; el reloj
  arranca con el deploy post-demo.
- [ ] **Arbitrajes detectados en logs** — **NO cumplido** (`edge_windows=0` a la fecha); lo
  llena la data del Mundial en shadow.
- [ ] **RiskManager apto para Motor REST** — NO cumplido: deuda documentada (manager.py:33-39:
  sobrestima exposición, PnL realized-only, race en check concurrente) + tests de integración
  pendientes + **veredicto CIEGO de arriba (🔴)**.
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

---

## Estado del frente Motor REST (resumen)

| Componente | Estado |
|---|---|
| Detección (shadow, `RestArbEngine`) | ✅ en main (graba EdgeWindow + heartbeat con diag de size) |
| Ejecución (`RestExecutor`) | ✅ en main, sensor validado en API viva |
| Sensor de fill validado en API viva (FILL/KILL/ERROR_RED) | ✅ **cerrado** (demo, PR #22) |
| Wiring `execute()` (cable Capas 1+2+3, muro A/B/C) | ✅ en main (PRs #26/#27/#28) — **sin validar en demo** |
| Demo end-to-end (Telegram suena + EdgeWindow poblado) | ❌ pendiente (ventana muerta, Opción B) |
| RiskManager VE al Motor REST (Trade rows) | 🔴 **CIEGO** — bloqueante pre-plata real |
| Runbook kill-switch manual | ✅ `docs/runbook_kill_switch.md` |
