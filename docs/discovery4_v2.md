# Discovery 4 — V2 bootstrap desync: por qué el buffering/recovery existente no evitó ni recuperó el crash

**Fecha:** 2026-05-30
**Bucket:** read-only (análisis). Sin implementación en este documento.
**Origen:** attempt #3 (30-may 17:41), `KXMLB-26-PHI` 3c, `qty=-11`, capturado por la instrumentación de PR #2 (`V2 desync diagnostic`, `orderbook_manager_v2.py:407`).

## Nota de evidencia (honestidad)

El log crudo `data/v2_attempt3_20260530_174849.log` **no es accesible** desde el entorno de análisis (gitignoreado, vive en el volumen Docker del host). Este discovery se basa en: (1) el **código** (autoritativo), y (2) los **valores del diagnóstico ya capturados** por el operador. Los puntos que requieren el stream crudo están marcados `[requiere log]` y no se infieren.

---

## (a) Causa raíz real: ventana ciega de bootstrap por falta de buffering inicial

Durante la ventana entre la suscripción y la primera inicialización de un ticker, V2 **no** tenía ni buffering ni detección de gap:

- El buffering de `handle_message` está gateado por `if sid in self._recovering` (`:160`). `_recovering` solo se puebla en `_start_recovery` (`:273`), que solo se llama ante un gap **ya detectado**. Durante el bootstrap inicial `_recovering` está vacío → los deltas no se bufferean.
- No existía un estado "initializing" que encolara deltas antes del primer snapshot. Un delta para un ticker sin snapshot se **descartaba** (`_apply_delta_msg`, rama `state is None or not is_initialized` → `logger.warning("skipping"); return`).
- La detección de gap de sid también tiene punto ciego: `if sid in self._last_seq_by_sid` (`:160`) — antes del primer apply exitoso esa key no existe, así que los primeros mensajes del sid saltean la detección de gap.

**Efecto:** un delta con `seq > snapshot_seq` que llegaba antes del snapshot se perdía → book sub-construido → un delta posterior legítimo (`-13` sobre un bucket de `2`) → `qty=-11` → `OrderbookDesyncError`.

**Estado:** corregido en **Part A** (commit `49231da`, en `main`): `_bootstrap_buffer` por ticker + `_drain_bootstrap_buffer` al aplicar el snapshot; `_apply_delta_msg` encola (devuelve `False`) en vez de descartar; el baseline del sid no avanza al encolar. (Part A entró a `main` por merge directo sin gate de review; ver PR de auditoría retroactiva "Part A — bootstrap buffer (review retroactivo)".)

## (b) Framing corregido: `seq` es global por `sid`, NO por ticker

Cita: `orderbook.py:265-268` (docstring de `apply_delta`): *"OrderbookManager… conoce el seq global por sid"*. Corroborado por `_last_seq_by_sid` (gap detection por sid) y `_drain_buffer` (toma `max` de sequences de todos los tickers del sid).

**Consecuencia:** un mismo `sid` agrupa muchos tickers (~38 en prod). El `seq` es un contador **global del sid**, consumido por los deltas de todos los tickers entremezclados. Por lo tanto:

- La sequence local de un ticker es **naturalmente dispersa**. Para `KXMLB-26-PHI`, ir `184 → 186` es **normal y esperado** si `seq=185` fue de **otro** ticker del sid.
- **No hay pérdida externa de mensajes** implícita en ese "salto". Los saltos de secuencia por-ticker son el comportamiento normal del feed.
- Por lo tanto, un fix de tipo "detectar gap por-ticker / rechazar deltas con hueco local" sería **incorrecto**: daría falsos positivos masivos. (Descartado explícitamente; ver `docs/propuesta_fix_v2.md §4`.)

`[requiere log]` Confirmar el dueño de `seq=185` necesita el stream crudo; la instrumentación solo loguea el delta que crashea (186), no los normales. Probablemente ni el log de 390 líneas lo contiene.

## (c) Bomba activa: el recovery no converge ante fallas (sigue ABIERTO en `main`)

El mecanismo de recovery se cierra por **un solo camino** y **no tiene timeout, retry ni limpieza ante error**:

- `_recovering.add(sid)` → solo en `_start_recovery` (`:273`).
- `_recovering.discard(sid)` → solo en `_handle_recovery_snapshot` (`:300`), y **únicamente cuando llegan los recovery snapshots de TODOS los tickers** del sid (`if not tickers_pending`).
- Un mensaje WS `error` (p.ej. `code 15 "Action required"`) se maneja en `:127-131` con solo `logger.error` + `record_error`; **no toca `_recovering` ni `_pending_snapshot_requests`**.
- `_start_recovery` pide el `get_snapshot` para **todos** los tickers del sid de una sola vez (`:274-278`).
- Grep confirmó: **cero** `timeout`/`retry` en el manager.

**Mecanismo de congelamiento permanente:** si el `get_snapshot` masivo es rechazado o sus respuestas no vuelven, `_pending_snapshot_requests` nunca se vacía → `_recovering` nunca se limpia → todos los deltas del sid se bufferean indefinidamente → los books nunca se re-inicializan. Consistente con `books_initialized=0` a T+6min observado en attempt #3.

**Part A NO corrige esto.** Es un segundo bug, independiente. Si por cualquier causa se dispara un recovery cuyo snapshot no retorna, V2 queda colgado igual que en attempt #3. → Se aborda en **Part B** (congelada; requiere Brief de Arquitectura + revisión adversarial antes de código).

`[requiere log]` Confirmar la secuencia temporal exacta (`code 15` siguiendo al `update_subscription get_snapshot`) necesita el log. El mecanismo de cuelgue, sin embargo, está en el código sea cual sea el disparador.

---

## Veredicto del fix

| Opción | ¿Es parte del fix? | Estado |
|---|---|---|
| (a) Buffering en bootstrap inicial | ✅ Sí | Hecho (Part A, en main) |
| (b) Recovery que no converge | ✅ Sí | **Abierto** — Part B (congelada, pendiente brief) |
| (c) "Manejar saltos de seq por-ticker" | ❌ No | Mal planteado: los saltos por-ticker son normales (seq global por sid) |

**Conclusión:** el fix completo es **(a) + (b)**. (a) previene el desync de bootstrap; (b) garantiza que, si ocurre cualquier desync, el sistema realmente se recupere en vez de congelarse. (c) no aplica.

## Próximos pasos

1. Auditoría retroactiva de Part A (PR de review en frío).
2. Brief de Arquitectura para Part B (Q3) — timeout por `req_id`, retry exponencial, limpieza/escalación de `_recovering`, snapshots por-ticker vs masivos. **El `code 15` provino del canal WebSocket, no HTTP**: el timeout debe ubicarse sobre el path de mensajería asíncrona del WS, no sobre un request HTTP. Revisión adversarial estricta antes de implementar.
3. `USE_ORDERBOOK_MANAGER_V2` permanece `False` indefinidamente; V1 estable acumulando datos (Fase 1 del roadmap).
