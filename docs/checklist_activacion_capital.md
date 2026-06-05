# Checklist de activación con capital — Motor REST

**Estado:** el `RestExecutor` está en `main` (PR #20, `1e4fe28`) pero **DORMANT** — nadie llama
`execute()`. **NO toca capital** hasta que TODOS los gates de abajo pasen, aunque el código
esté mergeado.

`TRADING_ENABLED=False` y `MOTOR_REST_ENABLED=True` (shadow) es el estado operativo actual.

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

## Resto del checklist (gates estándar de trading)

- [ ] **7 días sin crashes** en producción (uptime continuo, sin reinicios por excepción).
- [ ] **RiskManager sin excepciones** en ese período (`check_pre_trade` corriendo limpio).
- [ ] **Cap de 5% por trade / ¼ Kelly** confirmado activo en la config efectiva.
- [ ] **Wiring de `execute()`** detrás de `TRADING_ENABLED` — paso aparte, con su propio gate
  de código (diseño → review → código → review). Hoy NO está wireado.

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
| Detección (shadow, `RestArbEngine`) | ✅ en main, **corriendo** (graba EdgeWindow, riesgo cero) |
| Ejecución (`RestExecutor`) | ✅ en main, **DORMANT** (sin wiring, sin validar en demo) |
| Sensor de fill validado en API viva (FILL/KILL/ERROR_RED) | ✅ **cerrado** (demo, PR #22) |
| Wiring `execute()` detrás de `TRADING_ENABLED` | ❌ pendiente (paso aparte) |
