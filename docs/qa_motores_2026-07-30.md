# QA de motores — 5 rondas (2026-07-30, previo al mes de operación)

Primera auditoría sistemática de los paths de EJECUCIÓN con las lentes que cazaron 3
fail-open en el PR #200 (truthiness, excepciones tragadas, gather, invariantes de órdenes,
visibilidad de frenos). Objetivo: que el mes arranque sobre motores auditados, no asumidos.

**Veredicto global: los 4 paths de dinero PASAN.** Cero bugs nuevos — un mes de incidentes
los endureció. Una corrección al registro (mía) y una nota de diseño documentada.

## R1 · Motor 1 (executor + engine) — PASA

- `_place_leg` con `time_in_force="fill_or_kill"` explícito ✓ (invariante #14/2026-07-07).
- `gather(return_exceptions=True)` en la colocación es **uso correcto**, no el patrón
  prohibido: `_process_leg_result` inspecciona `isinstance(result, BaseException)` y
  clasifica killed (409 FOK sin volumen / fill 0) vs error (fila pending → reconcile).
  La prohibición de Lección 7 es para supervisores que TRAGAN errores; acá se capturan
  para decidir. HTTP 200 ≠ fillada: el fill real sale de `fill_count` ✓.
- Task de ejecución: referencia guardada (anti-GC), `_exec_lock` serializa, `finally`
  limpia ✓. EdgeWindow best-effort que jamás tira el tick ✓.
- **Nota de diseño (no bug)**: pata fácil en `error` (excepción no-kill) → se rollbackea lo
  CONOCIDO y la fila errada queda pending para el reconcile. Si esa orden en realidad
  llenó, el resultado es una huérfana GESTIONADA (M3) — trade-off deliberado: la
  alternativa (esperar al reconcile antes de rollbackear) dejaría exposición desnuda
  confirmada. Acotado y correcto.

## R2 · Motor 2 (entradas + brazo de salidas) — PASA

- Entradas: `immediate_or_cancel` explícito ✓; `fill_count` con coalesce por is-not-None
  (fix de la deuda 2026-07-01: presente-con-null enmascaraba el fallback `_fp`) ✓;
  fees al count REAL fillado ✓.
- El brazo de salidas NO coloca órdenes propias: delega en el executor compartido de
  Motor 3 (ver R3) — un solo path de venta que auditar, no dos.

## R3 · Motor 3 (salidas + huérfanas) — PASA

- Venta: IOC explícito ✓; **intent-antes-de-red con ABORT si no persiste** ("sin rastro no
  se vende" — fail-closed) ✓; taxonomía de errores (4xx determinístico libera; ambiguo
  escala) ✓; defensa ante `TradingDisabledError` aunque el sell no se bloquee (Capa C) ✓.
- **Regla de oro verificada** (`orphans.py::motor1_orphan_buys`): ambos sides del arb
  vivos → hedge → se excluye el arb ENTERO; sin `arb_id` → conservador, NO se gestiona.
  Fail-closed en las dos direcciones.

## R4 · RiskManager (path compartido) + REST — PASA, con corrección al registro

- `_check_pre_trade_locked` (el gate que TODOS los brazos de entrada atraviesan) chequea
  en orden: `BotState.is_paused` → stop-losses por ventana → **piso de capital**
  (`can_open_new_positions`) → gate MTM opcional → exposición simultánea. 
- **CORRECCIÓN al registro (2026-07-29)**: se afirmó que "M1 esquiva el piso de capital
  porque su pre-check solo valida balance". FALSO: el pre-check de balance del executor es
  ADICIONAL a `check_pre_trade`, que sí incluye el piso. M1 operó el 25-26 jul porque el
  cash fluctuaba por ENCIMA del piso con los settlements nocturnos — legítimo, no bypass.
- REST: invariantes FOK/fill_count/hard-first ya batalladas en producción (el 73% de
  rollbacks fue el mercado, no el código). Sin hallazgos nuevos.

## R5 · Transversal — PASA

- Los 3 `except: pass` del árbol son benignos y correctos: cadena de fallback de parseo
  (M3 poller), guard de logging diagnóstico que RE-LANZA la excepción original (V2), y
  timeout de backoff sobre el stop-event (data_capture).
- `gather(return_exceptions=True)` restante: solo en shutdown/cleanup (runner,
  data_capture, kalshi_ws) — paths donde tragar excepciones de cierre es correcto.
- Suite completa: **1391 passed** sobre la rama del QA.

## Qué NO cubrió este QA (honesto)

- **Motor 5 F3** (fuera del mes por decisión): su executor/reconciler NO se auditó con
  estas lentes — hacerlo es prerequisito si algún día se gira `MOTOR_MM_F3_ACK`.
- Los shadows M8/M9 (miden, no operan): riesgo acotado a telemetría.
- Rutas de red del cliente REST (throttling/reintentos): cubiertas por sus propios tests,
  no re-auditadas acá.
