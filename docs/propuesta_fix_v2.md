# Propuesta de fix — V2 bootstrap desync (`OrderbookDesyncError` en activación)

**Autor:** Análisis read-only (Principal Engineer)
**Fecha:** 2026-05-30
**Estado:** Propuesta de diseño. **NO** implementada. V2 permanece dormant (`USE_ORDERBOOK_MANAGER_V2=False`).
**Alcance:** Diagnóstico + pseudo-código. Cero cambios a código de producción o flags.

---

## 0. Alcance y honestidad sobre la evidencia

**No tuve acceso al log crudo `data/v2_attempt3_20260530_174849.log`.** Ese archivo está en `data/` (gitignoreado, `.gitignore:92-94`) y vive en el volumen Docker del host de producción, no en el entorno de análisis. No leí las 390 líneas; no voy a inventar contenido de líneas que no vi.

Este análisis se construye sobre dos fuentes que **sí** son autoritativas:

1. **El código completo** (`orderbook_manager_v2.py`, `orderbook.py`) — leído íntegro.
2. **Los valores del diagnóstico ya capturados** por la instrumentación (provistos por el owner):
   `msg_seq=186 state_seq=184 side=yes price_cents=3 delta_size=-13 bucket_qty_pre_delta=2`
   → `2 + (-13) = -11` → `OrderbookDesyncError` (`KXMLB-26-PHI`, T+37s, bootstrap).

Donde la conclusión dependa de algo que solo el stream crudo puede confirmar, está marcado explícitamente como **[requiere evidencia adicional]**.

---

## 1. Hallazgo que reencuadra el problema: `seq` es global-por-sid, NO por-ticker

Antes de diseñar nada, hay que fijar la semántica de `seq`, porque define qué fixes son válidos.

Cita textual, `orderbook.py:265-268` (docstring de `apply_delta`):

> *"Garantía de este método: `new_seq > self.sequence` (monotonicidad local). Garantía que NO ofrece: detección de gaps entre mensajes WS consecutivos. Esa responsabilidad pertenece a OrderbookManager, que **conoce el seq global por sid** y puede coordinar recovery entre todos los tickers."*

Corroborado por el código del manager:
- `_last_seq_by_sid` (gap detection) es **por sid**, no por ticker (`orderbook_manager_v2.py:160-168`).
- `_drain_buffer` toma `max_seq` de las sequences de **todos los tickers del sid** como baseline (`orderbook_manager_v2.py:303-311`) — solo tiene sentido si distintos tickers tienen sequences distintas dentro de un mismo espacio numérico global.
- `apply_delta` acepta cualquier salto hacia adelante: el único guard es `new_seq <= self._sequence` → `raise` (`orderbook.py` guard de monotonicidad). `new_seq == self._sequence + 1` **no** se exige.

**Consecuencia directa:** un mismo `sid` agrupa muchos tickers (en producción, ~38 markets). El `seq` es un contador **global del sid**, consumido por los deltas de *todos* los tickers entremezclados. Por lo tanto, la sequence local de un ticker individual es **naturalmente dispersa**: para `KXMLB-26-PHI`, ir de `184` a `186` es **esperable y correcto** si el `seq=185` perteneció a *otro* ticker del mismo sid.

### 1.1. Implicación crítica para el fix pedido

La tarea pedía *"no aplicar ningún delta si hay un gap local en el ticker"*. **Ese mecanismo literal es incorrecto y rompería V2:** como los `seq` son globales-por-sid, casi **todos** los deltas normales presentan un "gap local por ticker" (los `seq` intermedios son de otros tickers). Detectar gaps por-ticker daría falsos positivos masivos y mandaría a recovery permanente.

El *espíritu* de la instrucción es correcto (no aplicar deltas sobre un book incompleto). La realización correcta de ese espíritu **no** es contigüidad por-ticker; es **garantizar que nada se descarta durante el bootstrap** y apoyarse en la contigüidad **a nivel de sid** (que el manager ya sabe detectar) para garantizar completitud. Ver §3.

---

## 2. Root cause: ventana de bootstrap sin integridad ("init queue" que descarta, no encola)

El nombre "init queue" de la tarea es preciso, pero hoy **esa cola no existe durante el bootstrap inicial**: los deltas pre-snapshot se **descartan**, no se encolan.

### 2.1. La línea culpable

`orderbook_manager_v2.py:372-377` (`_apply_delta_msg`):

```python
state = self._books.get(ticker)
if state is None or not state.is_initialized:
    logger.warning(
        f"Delta for uninitialized/stale ticker {ticker} seq={seq} — skipping"
    )
    return            # ← el delta se PIERDE para siempre
```

Un delta que llega para un ticker cuyo snapshot inicial **todavía no se procesó** se **descarta con `return`**. Si el `seq` de ese delta es **mayor** que el `seq` del snapshot que llegará después, el delta **no es redundante** — es una actualización real que debía aplicarse encima del snapshot, y queda perdida.

### 2.2. Por qué el bootstrap no lo protege (a diferencia del recovery)

El buffer-and-drain **solo se activa en recovery**, no en el bootstrap inicial. En `handle_message`:

- Buffering: `if sid in self._recovering:` (`:155`). Durante el bootstrap inicial el sid **no** está en `_recovering` (no hubo gap aún) → los deltas **no** se bufferean.
- Gap detection: `if sid in self._last_seq_by_sid:` (`:160`). Antes del primer apply exitoso, `_last_seq_by_sid[sid]` no existe → los **primeros** mensajes del sid **saltean la detección de gap** por completo (no hay rama `else`).
- `_last_seq_by_sid[sid]` recién se setea en `:174`, *después* de un apply exitoso.

Es decir, hay una ventana entre "subscribe" y "primer book inicializado + baseline de sid establecido" donde V2: (a) descarta deltas pre-snapshot (`:372-377`) y (b) no corre detección de gaps. **Ventana de bootstrap sin integridad.**

### 2.3. Reconstrucción del crash de `KXMLB-26-PHI` (T+37s)

Mecanismo dominante, consistente con `state_seq=184, msg_seq=186, bucket_qty_pre_delta=2, delta_size=-13`:

1. `seq=185` → delta de PHI (un **add** a `3c`, p.ej. `+13`) llega **antes** de que se procese el snapshot de PHI. `state is None / not initialized` → **descartado** (`:372-377`). **Aquí se pierde el "185".**
2. `seq=184` → snapshot de PHI se aplica (`apply_snapshot`, `orderbook.py:248-256`): book = estado @184, con `3c = 2`.
3. `seq=186` → delta de PHI (`-13`) se aplica sobre el book sub-construido: `2 + (-13) = -11 < 0` → `OrderbookDesyncError` (`orderbook.py`).

Si en cambio el `185` perdido hubiera entrado (`3c: 2 → 15`), el `186` daría `15 - 13 = 2` ≥ 0: **sin crash**.

> **[requiere evidencia adicional]** Confirmar que `seq=185` llevaba `market_ticker=KXMLB-26-PHI` requiere el stream WS crudo de ese instante. La instrumentación actual solo loguea el delta que crashea (`186`), no el `185`, y los deltas normales no se persisten. Hipótesis alternativa: si `185` fue de *otro* ticker, entonces `184→186` es normal y el `-11` sería una inconsistencia genuina del feed Kalshi (o un snapshot sub-construido por la misma ventana de bootstrap). **Ambas variantes comparten la misma clase de causa**: V2 aplica un delta sin garantizar que el book local del ticker esté completo hasta ese punto. El fix de §3 cierra las dos.

### 2.4. El label "feed corruption" es del código, no diagnóstico

`orderbook.py:50,65` hardcodea `"(feed corruption)"` en el mensaje de `OrderbookDesyncError`. Ya marcado como sesgo de atribución-externa en Lección 9. `qty<0` indica **desync** (interno o externo); acá la evidencia apunta a la ventana de bootstrap interna.

---

## 3. Fix propuesto (pseudo-código)

Dos partes. La **Parte A** es el fix de la "init queue" pedido (cierra el agujero de root cause). La **Parte B** es defensa en profundidad recomendada (separada; decisión del owner).

### Parte A — Bootstrap buffer-and-drain (no descartar deltas pre-snapshot)

Generalizar el buffer-and-drain (hoy solo en recovery) para cubrir también la fase pre-snapshot del bootstrap.

```text
# Estado nuevo en __init__:
self._bootstrap_buffer: dict[str, list[dict]] = {}   # por ticker

# En handle_message, rama orderbook_delta, ANTES de _apply_delta_msg:
state = self._books.get(ticker)
if state is None or not state.is_initialized:
    # NO descartar. Encolar hasta que el snapshot del ticker fije baseline.
    self._bootstrap_buffer.setdefault(ticker, []).append(raw_msg)
    return
# (si está initialized, sigue el flujo normal y aplica el delta)

# En _apply_snapshot_msg, INMEDIATAMENTE DESPUÉS de apply_snapshot(...):
self._drain_bootstrap_buffer(ticker, snapshot_seq=seq)

def _drain_bootstrap_buffer(ticker, snapshot_seq):
    buffered = self._bootstrap_buffer.pop(ticker, [])
    buffered.sort(key=lambda m: m["seq"])
    for m in buffered:
        if m["seq"] <= snapshot_seq:
            continue                 # ya incluido en el snapshot → descartar OK
        self._apply_delta_msg(m)     # aplicar en orden, seq creciente
```

**Por qué cierra la causa:** si (i) el stream del sid es contiguo a nivel sid — lo cual el manager **ya** verifica vía `_last_seq_by_sid` (`:160-168`) — y (ii) **nada** se descarta pre-snapshot (esta Parte A), entonces todo delta de PHI con `seq > snapshot_seq` se aplica, en orden, sobre el snapshot autoritativo. El book queda idéntico al de Kalshi. Tras esto, un `qty<0` solo puede venir de inconsistencia **genuina** del feed, no de un agujero interno.

**Notas de diseño:**
- Buffer **por ticker** (no por sid): el snapshot inicial de cada ticker llega de forma independiente; drenar por ticker evita aplicar deltas de un ticker aún no snapshotteado.
- Cota de memoria: limitar tamaño del buffer por ticker (p.ej. descartar y forzar re-snapshot si excede N) para no crecer sin techo si un snapshot nunca llega.
- Idempotencia con el snapshot: el guard `m["seq"] <= snapshot_seq` evita reaplicar lo ya contenido.

### Parte B — `qty<0` dispara recovery del ticker, no crash (defensa en profundidad)

Hoy `OrderbookDesyncError` **propaga** y dispara la cascada de recovery por gap-fantasma que (Lección 9) no converge (`books_initialized=0` a T+6min en attempt #3). Diseño más robusto: ante `qty<0` (que tras la Parte A solo debería pasar por inconsistencia genuina), marcar **ese ticker** stale y pedir un snapshot fresco solo para él, en vez de propagar.

```text
# En _apply_delta_msg, reemplazar el `raise` del bloque diagnóstico por:
except OrderbookDesyncError:
    <... logging diagnóstico existente, intacto ...>
    state.mark_stale()
    await self._request_ticker_resnapshot(ticker, sid)   # recovery acotado al ticker
    return   # no propagar; el book queda stale hasta que llegue el snapshot
```

> Parte B cambia control flow y toca el path de error; se propone **separada** de la Parte A y queda a criterio del owner. La Parte A es el fix mínimo y suficiente para la root cause de la "init queue".

---

## 4. Qué NO hacer

- ❌ **Detección de gap por-ticker / "no aplicar si falta seq local".** Inválido: los `seq` son globales-por-sid; los huecos por-ticker son normales (§1.1). Daría falsos positivos masivos.
- ❌ Tocar el guard de `apply_delta` para exigir `new_seq == self._sequence + 1`. Romper la sparsidad legítima.
- ❌ Ablandar el `qty<0` a un clamp a 0 silencioso. Ocultaría desyncs reales; el book quedaría mal sin señal.

---

## 5. Evidencia adicional para cerrar la ambigüedad de §2.3

Para confirmar de forma definitiva el dueño de `seq=185` (sin lo cual no se distingue "pre-snapshot drop interno" de "inconsistencia de feed"):

- Instrumentar (temporalmente) el log de **todos** los `market_ticker`+`seq` durante la ventana de bootstrap (primeros ~60s), nivel DEBUG, para reconstruir el orden de llegada por ticker.
- O capturar el stream WS crudo del bootstrap a archivo.

Con cualquiera de los dos se ve si el `185` de PHI llegó y fue descartado en `:372-377` (confirma Parte A como causa directa) o si nunca existió para PHI (apunta a feed/snapshot).

---

## 6. Plan de tests (cuando se implemente)

1. **Bootstrap reordenado:** snapshot `seq=184` de un ticker llega *después* de su delta `seq=185` → con Parte A, el `185` se bufferea y aplica; el book queda correcto; sin `qty<0`.
2. **Drop redundante:** delta `seq <= snapshot_seq` bufferizado → descartado en drain, no reaplicado.
3. **No-regresión sparsidad:** deltas de varios tickers en un sid con `seq` entremezclados (184=A, 185=B, 186=A) → todos se aplican sin falsos gaps.
4. **Cota de buffer:** si el snapshot de un ticker nunca llega, el buffer no crece sin techo (fuerza re-snapshot al exceder N).
5. **(Parte B)** `qty<0` genuino → ticker queda stale + re-snapshot pedido, sin propagación ni cascada de sid.

---

## 7. Resumen ejecutivo

- **Root cause:** ventana de bootstrap sin integridad. Los deltas pre-snapshot se **descartan** (`orderbook_manager_v2.py:372-377`) en lugar de encolarse; el buffer-and-drain solo existe para recovery, no para el bootstrap inicial. Un delta perdido deja el book sub-construido y un delta posterior legítimo lo lleva a `qty<0`.
- **El fix correcto NO es** detección de gap por-ticker (los `seq` son globales-por-sid; los huecos por-ticker son normales).
- **El fix correcto ES** bootstrap buffer-and-drain (Parte A): no descartar nada pre-snapshot; apoyarse en la contigüidad de sid existente. Opcional: `qty<0` → recovery acotado al ticker (Parte B).
- **Estado:** propuesta read-only. V2 sigue dormant. Próximo paso del owner: decidir implementación + capturar evidencia de §5 para cerrar la ambigüedad de §2.3.
