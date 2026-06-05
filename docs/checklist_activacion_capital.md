# Checklist de activación con capital — Motor REST

**Estado:** el `RestExecutor` está en `main` (PR #20, `1e4fe28`) pero **DORMANT** — nadie llama
`execute()`. **NO toca capital** hasta que TODOS los gates de abajo pasen, aunque el código
esté mergeado.

`TRADING_ENABLED=False` y `MOTOR_REST_ENABLED=True` (shadow) es el estado operativo actual.

---

## 🔴 GATE IRREDUCIBLE — Validación del sensor de fill en demo (BLOQUEANTE)

El sensor `_create_order_filled` (`src/strategies/motor_rest_arb/executor.py`) está verificado
**solo contra la doc de Kalshi**, NO contra la API viva. Es el sensor primario de toda la
máquina de estados (FILL/KILL/ERROR_RED se ramifican de "¿llenó sí o no?"). Antes de capital:

- [ ] Mandar una orden **`fill_or_kill` REAL en cuenta demo**.
- [ ] Capturar la **respuesta cruda** (JSON completo de CreateOrder).
- [ ] Confirmar que `_create_order_filled` la lee correctamente:
  - [ ] caso **FILL → FILL** (orden que se llena completa).
  - [ ] caso **KILL → KILL** (orden FOK que no cruza y se cancela).
- [ ] **Confirmar el sufijo `_fp`** de los campos numéricos: ¿la API devuelve `fill_count`/
  `remaining_count` (int) o `fill_count_fp`/`remaining_count_fp` (fixed-point string)? El
  código prueba ambos, pero hay que confirmar cuál es el real y que se parsea bien.

**Hasta que esta validación en demo pase, el executor NO opera, aunque esté en main.**
(Recordatorio del modo de fallo evitado: el sensor original adivinaba campos inexistentes
—`status`/`filled_count`— que habrían leído todo FILL como KILL → rollback en cada fill.
Ahora falla conservador hacia KILL, pero el shape real debe confirmarse en vivo.)

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
| Sensor de fill validado en API viva | ❌ **pendiente — gate irreducible** |
| Wiring `execute()` detrás de `TRADING_ENABLED` | ❌ pendiente (paso aparte) |
