---
name: riesgo-capital
description: RiskManager y el modelo de capital del bot — capital dinámico (cash real × factor, piso/techo/hard-cap), stop-losses diario/semanal/mensual, exposición simultánea, neteo de arbs por arb_id y el ciclo de vida del kill-switch persistente. Usar al tocar sizing, límites, stop-losses, el kill-switch o cualquier check pre-trade.
---

# Riesgo y capital (RiskManager)

`src/risk/manager.py` es el **gatekeeper único pre-trade** (lock de clase): ningún motor
compra sin pasar por acá. Todos los techos (sizing por trade, stop-losses, exposición)
derivan de UN número: el capital efectivo. Protocolo general: skill `botkalshi`.

## Capital dinámico (C-01/C-02) — el modelo exacto

```
efectivo = clamp( cash_real_kalshi × CAPITAL_SAFETY_FACTOR_PCT,
                  CAPITAL_FLOOR_USD, CAPITAL_CAP_USD )
efectivo = min(efectivo, PROD_CAPITAL_HARD_CAP_USD)   # $5k en prod, SIEMPRE
```

- **cash real** = balance disponible de Kalshi (NO equity), refrescado por
  `_run_balance_refresh` cada `BALANCE_REFRESH_SECONDS`.
- `ACTIVE_CAPITAL_USD` **no es lo que se apuesta**: es el FALLBACK si `get_balance()`
  falla sin caché previa (piso de seguridad) y la referencia de la alerta de desfase
  (C-03, advisory: config vieja vs cash real — NO cambia sizing, solo avisa). Mantenerlo
  cercano al cash real: si el fallback se activa con un valor 6× el cash, sobre-sizea.
- `DYNAMIC_CAPITAL_ENABLED=false` fuerza el estático (escudo / dry-run).
- Fail-safe: `get_balance()` que falla NO crashea ni pisa la caché — mantiene el último
  balance conocido; sin balance previo, cae a `ACTIVE_CAPITAL_USD`.

## Stop-losses y exposición

- Diario / semanal / mensual, derivados del MISMO capital efectivo (coherencia: todos los
  límites miran el mismo "dinero real"). El semanal subcontaba en borde de mes hasta el
  fix 2026-07-01. Límite efectivo = `max(capital × %, piso USD)` (pisos 20/40/60).
- **Ventanas de CALENDARIO** (resetean lunes / día 1) + **realized-only** (solo `settled`)
  = el hueco de la sangría gradual (auditoría 2026-07-17: M2 −$430 jun→jul sin un breach).
  Dos frenos complementarios, ambos por flag:
  - `ROLLING_DRAWDOWN_STOP_ENABLED` (default off): PnL settled de los últimos N días
    (`MAX_ROLLING_DRAWDOWN_DAYS=30`) vs `max(capital×15%, $60)` → kill-switch NUCLEAR.
    ⚠️ Activarlo con un drawdown histórico dentro de la ventana latchea al primer intento.
    Se COMPUTA siempre (status lo muestra con `gate_off`) aunque el gate esté apagado.
  - `UNREALIZED_STOP_ENABLED` (default off): pérdida LATENTE (MTM) vs `max(capital×10%,
    $40)` → SOFT (rechaza entradas, sin kill-switch). Cambia la semántica realized-only
    del owner — solo con su decisión. Marks: `RiskManager.record_mark` los publican
    M3/M2-exit como pasajeros; posición sin mark fresco NO cuenta.
- **Una sola matemática freno↔observabilidad**: `stop_loss_status()` (mismo snapshot que
  el check) alimenta el dashboard, y `risk.sl_status` se loguea en cada refresh de
  balance. NUNCA recalcular ventanas/límites aparte en un consumidor: el dashboard lo
  hacía (rolling 30d sin pisos vs límite mensual) y mostró "796% consumido" de un freno
  sano — el operador persiguió un fantasma.
- Exposición simultánea con **neteo por `arb_id`**: un arb hedged (ambas patas vivas) no
  cuenta como exposición direccional; una huérfana SÍ.
- Cap por trade + guards de entrada por motor (balance pre-check M1, one-per-event M2,
  cap direccional por EVENTO via `EventExposureTracker`).

## Kill-switch persistente — ciclo de vida completo

1. **Se dispara** por: stop-losses del RiskManager, o UN rollback abortado por slippage
   (`rollback_aborted_slippage`, Bug 3 del incidente 2026-07-07).
2. **Persiste** en DB (`operational_state`, key `kill_switch`) → sobrevive redeploys; el
   boot re-hidrata la pausa (`_rehydrate_kill_switch`).
3. **NO auto-resetea por diseño.** La ÚNICA forma de levantarlo:
   `scripts/clear_kill_switch.py` (verifica posiciones=0 + input literal "CLEAR").
4. Al operar sobre la DB (rebuild/backup), `operational_state` es tabla SAGRADA: se copia
   entera y se verifica su valor antes de cualquier swap (skill `operacion-disco-db`).

## Reglas al tocar esta capa

- Cualquier cambio acá es **capa de seguridad**: shadow-first no aplica (no es señal),
  pero el estándar de tests sí — mecanismo + control + fail-safe, y NUNCA debilitar un
  clamp/floor/cap sin decisión explícita del operador con los riesgos repetidos.
- Paths de LECTURA (balance, checks) fallan ABIERTOS con el último valor sano; paths que
  AUTORIZAN plata nueva fallan CERRADOS (sin capital confirmado no se agranda exposición).
- Dinámico controla el TAMAÑO, no el riesgo direccional: los riesgos por motor (huérfanas
  M1, direccional M2, quotes resting M5) viven en las skills de cada motor.
