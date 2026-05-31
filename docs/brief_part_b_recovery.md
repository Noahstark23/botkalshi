# Brief de Arquitectura — Part B: convergencia de recovery en V2 (Q3)

> **ESTADO: BORRADOR-INSUMO de Claude Code. NO ES EL BRIEF FORMAL.**
> Por directiva del CTO, el brief formal lo presenta **Claude Project** y **debe pasar revisión adversarial estricta antes de escribir una sola línea de código**. Este documento es material de arranque, no autorización para implementar.
> **PROHIBIDO escribir código de recovery hasta aprobación adversarial.**
> `USE_ORDERBOOK_MANAGER_V2` permanece `False` indefinidamente.

## 1. Problema (Q3, confirmado por código)

El recovery de V2 se **congela permanentemente** ante una falla. Evidencia de código (`orderbook_manager_v2.py`):

- `_recovering` se limpia **solo** en `_handle_recovery_snapshot:300`, y únicamente cuando llegan los recovery snapshots de **todos** los tickers del sid.
- Si esos snapshots **no vuelven**, `_pending_snapshot_requests` nunca se vacía → `_recovering` nunca se limpia → todos los deltas del sid se bufferean para siempre → `books_initialized=0` (observado en attempt #3 a T+6min).
- `_start_recovery:274-278` pide el `get_snapshot` para **todos** los tickers del sid de una vez → un único fallo bloquea el sid entero.
- **No hay timeout, retry ni limpieza ante error** (grep: cero `timeout`/`retry` en el manager).
- Los mensajes WS `error` se manejan en `:127-131` con solo `logger.error`/`record_error`; **no se correlacionan** con el `req_id` del recovery pendiente.

## 2. Ubicación correcta del timeout: canal WebSocket, NO HTTP

El `code 15 "Action required"` del attempt #3 provino del **canal WebSocket** (es la respuesta al comando `update_subscription action=get_snapshot` enviado por `self._ws.send_command(...)`), **no de un request HTTP**. 

**Implicación de diseño (estricta):** el timeout/retry/limpieza debe vivir sobre el **path de mensajería asíncrona del WS**, correlacionado al `req_id` que devuelve `send_command`. No debe colocarse sobre ningún cliente HTTP (`KalshiRestClient`), que es un canal distinto y ajeno a este flujo. Confundir los canales pondría el timeout en el lugar equivocado y no cubriría el fallo real.

## 3. Componentes del diseño propuesto (a validar)

1. **Timeout rastreable por `req_id`.** Al enviar el `get_snapshot` en `_start_recovery`, registrar un deadline asociado al `req_id` (p.ej. `asyncio.create_task` con sleep, o un dict `req_id -> deadline` chequeado por un supervisor). Si los snapshots de ese `req_id` no llegan en `T`, el recovery se marca fallido.
2. **Correlación de errores WS al recovery pendiente.** El handler de `error` (`:127-131`) debe poder vincular un `code 15` (u otro) a un `req_id`/sid en recovery y disparar el path de fallo. `[requiere protocolo/log]` confirmar si el `error` de Kalshi incluye el `id` del comando; si no, el timeout (componente 1) es el disparador primario.
3. **Reintentos exponenciales acotados.** Ante fallo, reintentar el `get_snapshot` con backoff exponencial (p.ej. 1s, 2s, 4s…) hasta `N` intentos.
4. **Limpieza/escalación de `_recovering`.** Tras agotar reintentos: limpiar `_recovering` y `_pending_snapshot_requests` del sid (para no dejarlo congelado), marcar tickers, alertar a Telegram, y definir el fallback (¿dejar el sid stale?, ¿deshabilitar V2 en caliente y caer a V1?).
5. **Snapshots por-ticker vs masivos.** Evaluar pedir `get_snapshot` por ticker (o en lotes chicos) para que un rechazo de un ticker no bloquee el sid completo. Trade-off: granularidad/robustez vs volumen de mensajes y rate-limit de Kalshi.

## 4. Checklist de revisión adversarial (el brief formal debe sobrevivir esto)

- **Carrera timeout vs llegada tardía:** el recovery snapshot llega justo cuando el timeout dispara → no aplicar doble ni corromper estado.
- **Re-entrancy:** múltiples `_start_recovery` para el mismo sid; múltiples `req_id` solapados.
- **Reintentos no acotados / tormenta:** garantizar tope y backoff; no amplificar un incidente de Kalshi.
- **Interacción con Part A:** el `_bootstrap_buffer` y el `_pending_deltas` de recovery deben tener semántica clara y no perder ni duplicar deltas al drenar.
- **Interacción con el flag dormant:** nada de esto corre con `USE_ORDERBOOK_MANAGER_V2=False`; confirmar que el supervisor/timeout no se lance fuera de V2.
- **Escalación correcta:** ¿qué hace el bot si V2 no converge? Caer a V1 sin perder el feed (coordinar con el watchdog de PR #1).
- **Plan de tests:** timeout dispara y limpia; retry converge; retry agota → escalación; llegada tardía post-timeout; por-ticker vs masivo.
- **`[requiere log]`:** semántica exacta del `code 15` (¿lleva `id`?, ¿es por-comando o global?) — confirmar con el log de attempt #3 / docs de protocolo Kalshi antes de fijar el mecanismo de correlación.

## 5. Fuera de alcance de este brief

- Implementación (congelada).
- Reactivación de V2 / cambio de flag (se mantiene `False`).
- Part A (ya en main; auditoría retroactiva en PR aparte).

## 6. Próximo paso

Claude Project toma este insumo, produce el brief formal, lo somete a **revisión adversarial estricta**. Solo con aprobación explícita se autoriza escribir código de Part B, vía el workflow estricto de la Sección 14 (rama + PR + review).
