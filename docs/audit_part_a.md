# Auditoría retroactiva — Part A (bootstrap buffer)

**Commit auditado:** `49231da` — `feat(v2): bootstrap buffer-and-drain para deltas pre-snapshot (Parte A)`
**Estado:** ya mergeado a `main` (push directo, sin gate de review previo bajo el modo "FACTORY/ZERO DEBATE", ahora revocado por directiva del CTO).
**Propósito de este documento:** registro formal para la **revisión en frío de Noel**. Firmá tu aprobación (o objeciones) abajo o en el PR.
**Alcance en runtime:** V2 dormant (`USE_ORDERBOOK_MANAGER_V2=False`, `config.py:70`). El código de Part A **no se ejecuta** hasta que se prenda el flag.

---

## 1. Por qué se hizo (causa raíz)

Attempt #3 (30-may) crasheó en bootstrap: `KXMLB-26-PHI` 3c, `qty=-11`. La instrumentación capturó `msg_seq=186 state_seq=184 delta_size=-13 bucket_qty_pre_delta=2`.

Mecanismo: durante el bootstrap, un `orderbook_delta` que llegaba **antes** del snapshot inicial de un ticker se **descartaba** (`_apply_delta_msg` hacía `logger.warning("skipping"); return`). Como el `seq` es global por `sid`, ese delta podía tener `seq > snapshot_seq` (= una actualización real, no redundante). Al perderse, el book quedaba sub-construido; un delta posterior legítimo (`-13` sobre un bucket de `2`) lo llevaba a `qty<0` → `OrderbookDesyncError`.

## 2. Cambios exactos de control de flujo e inicialización

Archivo: `src/strategies/motor_1_arbitrage/orderbook_manager_v2.py`.

### 2.1. Inicialización (`__init__`)
```python
+ self._bootstrap_buffer: dict[str, list[dict]] = {}   # cola de deltas pre-snapshot, por ticker
```

### 2.2. `_apply_delta_msg` — de descartar a encolar (y cambia firma)
- **Antes** (`-> None`): si `state is None or not state.is_initialized` → `logger.warning(...skipping); return`. **El delta se perdía.**
- **Ahora** (`-> bool`):
  - Si el ticker no tiene snapshot inicial → `self._bootstrap_buffer.setdefault(ticker, []).append(raw_msg); return False` (**encola, no descarta**).
  - Si aplica con éxito → `return True` al final.
- **Impacto de control de flujo:** el retorno bool informa al caller si el delta se aplicó o se difirió.

### 2.3. `handle_message` — no avanzar el baseline del sid al diferir
- **Antes:** `self._apply_delta_msg(raw_msg)` y luego **siempre** `self._last_seq_by_sid[sid] = new_seq`.
- **Ahora:**
  ```python
  applied = self._apply_delta_msg(raw_msg)
  if applied:
      self._last_seq_by_sid[sid] = max(self._last_seq_by_sid.get(sid, 0), new_seq)
  ```
- **Por qué:** si el delta se encoló (no se aplicó), avanzar el baseline haría que el snapshot inicial posterior pareciera un gap → falso `SidGapError`. Al no avanzar, el snapshot entra normal.
- **Sutileza:** el avance pasó de asignación directa a `max(...)`, robusto a reordenamientos.

### 2.4. `_apply_snapshot_msg` — drenar tras inicializar
```python
+ self._drain_bootstrap_buffer(raw_msg["sid"], ticker, seq)   # al final, tras apply_snapshot
```

### 2.5. `_drain_bootstrap_buffer` (método nuevo)
```python
buffered = self._bootstrap_buffer.pop(ticker, [])
buffered.sort(key=lambda m: m["seq"])
for m in buffered:
    if m["seq"] <= snapshot_seq:
        continue                       # ya contenido en el snapshot → descartar OK
    self._apply_delta_msg(m)           # aplicar en orden
    if m["seq"] > self._last_seq_by_sid.get(sid, 0):
        self._last_seq_by_sid[sid] = m["seq"]
```
- Ordena por seq, descarta lo redundante (`<= snapshot_seq`), aplica el resto en orden, avanza el baseline del sid al mayor seq aplicado.

## 3. Tests añadidos

`tests/strategies/motor_1_arbitrage/test_orderbook_manager_v2.py`:
- `test_bootstrap_reordered_delta_before_snapshot`: delta `seq=2` (`+13`) antes del snapshot `seq=1` (bucket=2) → encolado y drenado a `15`; el `-13` posterior da `2`, sin desync. Reproduce y cierra el patrón de attempt #3.
- `test_bootstrap_buffer_discards_redundant_delta`: delta `seq=5` encolado, snapshot `seq=10` → `5 <= 10` descartado, bucket queda en valor de snapshot.

Resultado: **17→19 tests en el archivo, todos verdes.**

## 4. Lo que Part A NO hace (límites explícitos)

- **NO** corrige el recovery no-convergente (Q3): si un `get_snapshot` de recovery no retorna, `_recovering` sigue colgándose para siempre. Eso es **Part B** (congelada; pendiente de Brief de Arquitectura + revisión adversarial).
- **NO** toca flags ni reactiva V2.
- **NO** cambia el path de V1 (activo en producción).

## 5. Riesgo

- **Runtime:** nulo mientras `USE_ORDERBOOK_MANAGER_V2=False` (el manager no se instancia).
- **Gobernanza:** este es el punto auditado — código de state-machine entró a `main` sin review previo. Este documento + la firma de Noel cierran ese gate retroactivamente.

## 6. Verificación independiente sugerida (para la firma en frío)

1. `git show 49231da` — leer el diff completo.
2. Confirmar que ninguna rama de `_apply_delta_msg` descarta silenciosamente.
3. Confirmar que el buffer está acotado en el diseño futuro (hoy no tiene tope de tamaño — ver nota).
4. Correr `pytest tests/strategies/motor_1_arbitrage/test_orderbook_manager_v2.py`.

> **Nota de deuda detectada en la auditoría:** `_bootstrap_buffer` no tiene cota de tamaño. Si el snapshot inicial de un ticker nunca llega, su cola crece sin techo. Bajo impacto (V2 dormant; y en operación normal el snapshot llega), pero debe atarse en Part B (cota + re-snapshot forzado). Registrado para no perderlo.

---

## Firma de revisión (a completar por Noel)

- **Revisado por:** _________________
- **Fecha:** _________________
- **Veredicto:** [ ] Aprobado retroactivamente   [ ] Aprobado con observaciones   [ ] Rechazado / revertir
- **Comentarios:**
