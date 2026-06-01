# Brief de Arquitectura — Part B: convergencia de recovery en V2 (Q3)

**ESTADO: DOCUMENTACIÓN POST-HOC del código implementado (PR #11), alineada para revisión adversarial en frío.** El código de Part B ya existe en `feat/v2-recovery-supervisor` (PR #11, draft). Este brief documenta **lo que el código hace**, no una propuesta a futuro. `USE_ORDERBOOK_MANAGER_V2` permanece `False`; el supervisor solo corre cuando se active V2 (ventana de validación en vivo, pendiente).

## 0. Correcciones respecto al brief-borrador original

El borrador previo de esta directiva contenía 3 valores que eran **bugs del brief, no del código**. El código de PR #11 implementa la versión correcta; este documento queda alineado a él:

| Punto | Brief-borrador (incorrecto) | Código PR #11 (correcto) | Por qué |
|---|---|---|---|
| Cap de RAM | `len(_pending_deltas[ticker]) > 1000` | `len(_bootstrap_buffer[ticker]) > 1000` | `_pending_deltas` se indexa por **sid** (int), no por ticker → índice inexistente. Y la cola que crece sin techo en bootstrap es `_bootstrap_buffer[ticker]`, no la de recovery. |
| Guarda None-safe | `if _books.get(ticker) is None: return` | `if ticker in self._evicted: return` | `.get()` da `None` también para tickers **nuevos** (key ausente) → la guarda literal descartaría el primer delta de todo ticker nuevo y rompería el bootstrap. El set `_evicted` distingue "evictado" de "nunca visto". `book=None` se conserva para None-safety. |
| Tick supervisor | 5s | 1s (`SUPERVISOR_TICK_SEC`) | Con timeout de recovery de 10s, tick 1s detecta el vencimiento con ≤1s de slop vs hasta 5s. Calibrable. |

## 1. Problema (Q3)

El recovery de V2 se congela permanentemente ante fallas: `_recovering`/`_pending_snapshot_requests` solo se limpian cuando llegan **todos** los recovery snapshots; si no vuelven, el sid queda buffereando para siempre (`books_initialized=0`, attempt #3). Sin timeout, retry ni limpieza. Request masivo expone el sid entero a un único fallo.

## 2. Reglas de diseño aprobadas

### (a) Intercepción de `code 15` (WS control message)
Si el manager intercepta `{"code": 15, "msg": "Action required"}` por el WS, **no** se trata como falla local de un ticker (eso rompería el aislamiento). El manager:
1. Aborta todas las tareas de recovery en curso (limpia `_recovering`, `_pending_snapshot_requests`, deadlines y contadores de retry).
2. Fuerza la caída controlada del socket (`ws.force_reconnect()`), para que el **supervisor del socket principal (`run()`) asuma la reconexión global**.
3. Esa reconexión **regenera el handshake RSA-PSS completo** por construcción (confirmado en discovery read-only: `signer.sign()` se invoca dentro de `_connect_and_listen`, fresco por conexión; no hay token cacheado). → **No** se inyecta regeneración de firma adicional en el re-socket.

### (b) Estado de evicción seguro (None-safety)
La degradación a V1 pasivo pone `self._books[ticker] = None`. Para evitar `AttributeError` en caliente, `handle_message` lleva una guarda al inicio.
> **Refinamiento sobre el pseudocódigo original:** la guarda literal `if self._books.get(ticker) is None` descartaría también el primer mensaje de cualquier ticker **nuevo** (`.get()` devuelve `None` para keys ausentes), rompiendo el bootstrap. Se usa un set explícito `self._evicted` para distinguir *evictado* (degradado a propósito) de *nunca visto*. La guarda es `if ticker in self._evicted: return`. Se mantiene `_books[ticker] = None` para que cualquier acceso directo sea None-safe.

Deltas de un ticker evictado se descartan en V2; `data_capture` (REST) sigue guardando en SQLite. El **ciclo de discovery diario (00:00 UTC)** es el único que re-inicializa el estado limpiando `_evicted`.

### (c) Circuit breaker por volumen
Si el buffer pre-snapshot de un ticker supera **1000** mensajes antes de que el supervisor de recovery (timer 10s) despierte, se ejecuta **evicción inmediata** a modo pasivo V1, para blindar la RAM del droplet. Valor sujeto a calibración con logs de prod en mercados de alta frecuencia.
> El cap se aplica sobre el **buffer pre-snapshot por ticker** (`_bootstrap_buffer[ticker]`), que es la cola que crece sin techo si el snapshot inicial nunca llega — la deuda detectada en la auditoría de Part A.

## 3. Supervisor de recovery

- Loop async de fondo (`_recovery_supervisor`), tick cada `SUPERVISOR_TICK_SEC` (1s).
- Trackea cada `req_id` de recovery con un **deadline de 10s** (`RECOVERY_TIMEOUT_SEC`).
- Al vencer un `req_id` sin que lleguen sus snapshots: incrementa el contador de retry del sid; si `retries <= 3` (`RECOVERY_MAX_RETRIES`), re-emite `get_snapshot` (nuevo `req_id`, nuevo deadline) para los tickers aún pendientes; si supera 3, **evicta** esos tickers a modo pasivo y limpia el estado de recovery del sid.
- Lógica de "un tick" factorizada en `_check_recovery_timeouts()` (testeable sin el loop infinito).

## 4. Constantes

| Constante | Valor | Rol |
|---|---|---|
| `RECOVERY_TIMEOUT_SEC` | 10.0 | deadline por `req_id` de recovery |
| `RECOVERY_MAX_RETRIES` | 3 | reintentos antes de evicción |
| `BOOTSTRAP_BUFFER_CAP` | 1000 | cap del buffer pre-snapshot por ticker |
| `SUPERVISOR_TICK_SEC` | 1.0 | período del loop supervisor |

## 5. Tests (mandatorios)

1. Recovery snapshot que excede 10s → incrementa retry y re-emite.
2. Ticker que supera 1000 mensajes en buffer pre-snapshot → evicción inmediata.
3. `code 15` → aborta recoveries + fuerza desconexión del socket.

## 6. Fuera de alcance / invariantes

- `USE_ORDERBOOK_MANAGER_V2=False` (dormant); el supervisor solo corre cuando V2 se active vía wiring. El código no se ejecuta en prod hasta la ventana de validación.
- No se reactiva V2 en este cambio.
