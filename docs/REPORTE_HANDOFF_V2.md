# REPORTE DE HANDOFF — Estado de la línea de trabajo (V2 / botkalshi)

**Generado:** 2026-06-01 por Claude Code, a pedido del owner.
**Propósito:** recuperar la línea de trabajo y dar a ambos agentes (Claude Project / Claude Code) una base de verdad única para no re-ejecutar trabajo ya hecho.

---

## 0. TL;DR — dónde estamos parados

- **Producción:** V1 estable, `TRADING_ENABLED=False`, `USE_ORDERBOOK_MANAGER_V2=False`. Captura REST + WS poblando SQLite. Sano.
- **`main` = `49231da`** e incluye, mergeado: watchdog WS (PR #1), instrumentación diagnóstica V2 (PR #2), Part A bootstrap buffer (PR #4).
- **Part B (recovery con convergencia) está IMPLEMENTADA en PR #11 pero CONGELADA** (draft, sin merge). Tiene **2 gaps bloqueantes** ya identificados y diseñados.
- **El trabajo de "documentar gaps + diseñar (b) y (c)" YA ESTÁ COMPLETO** (PR #12). La última directiva pedía esto — ya está hecho. **No re-ejecutar.**
- **Próximo paso real:** revisión adversarial de los 2 diseños en PR #12 → si pasan, implementar en turno dedicado (levantando la prohibición sobre el manager, flag siempre False).

---

## 1. Estado de PRs (verificado vía API, 2026-06-01)

| PR | Título | Estado | Qué es |
|---|---|---|---|
| **#11** | Part B — supervisor de recovery | **draft, CONGELADO** | Código de convergencia. Espera review en frío + cierre de 2 gaps antes de cualquier merge/activación. |
| **#12** | Gaps bloqueantes + diseños (b)/(c) | **draft** | Doc read-only. **Contiene los 2 diseños a auditar.** Este es el foco actual. |
| #6 | Brief-insumo Part B | draft | Borrador inicial del brief. Redundante con el brief ya consolidado; **candidato a cerrar**. |
| #5 | Discovery 4 | draft | Doc del 4º discovery. Conservar o mergear. |
| #3 | Attempt #3 (update Lección 9) | draft | Doc. Conservar o mergear. |

**Cerrados sin merge:** #7 (Decimal refactor — descartado, scope creep), #8 y #10 (duplicados), #9 (acta retroactiva Part A), #1/#2/#4 (mergeados a main).

> ⚠️ **Deuda de PRs:** hay 5 PRs draft abiertos, varios redundantes (#3/#5/#6 son docs que podrían consolidarse). Recomendación: decidir mergear o cerrar los docs para bajar ruido. Las ramas remotas no se pueden borrar desde este entorno (proxy git lo rechaza) — limpieza desde la UI de GitHub.

---

## 2. Part B — qué está hecho y qué falta (los 2 gaps)

**Implementado en PR #11** (`orderbook_manager_v2.py`, 23/23 tests, ruff+mypy limpios):
- (a) Intercepción `code 15` → aborta recoveries + `force_reconnect()`. ✅ funciona.
- (b-lógica) Evicción None-safe con set `_evicted` + guarda en `handle_message`. ✅
- (c-lógica) Circuit breaker `_bootstrap_buffer[ticker] > 1000` → evicción. ✅
- Supervisor: `_recovery_supervisor` (tick 1s) + `_check_recovery_timeouts` (timeout 10s, 3 retries, luego evicción). ✅

**Gaps bloqueantes (diseñados en PR #12, NO implementados):**

### Gap (b) — el supervisor no está vivo
- `_recovery_supervisor()` existe pero **nadie lo lanza** en `data_capture.py` → la convergencia no corre aunque se active el flag.
- Si corriera suelto y muriera, **no hay anti-zombie** → muerte silenciosa (Lección 7).
- **Diseño (en PR #12):** wrapper `_run_recovery_supervised` (fail-loud, gemelo de `_run_ws_supervised`) + task condicional (`if self._v2_manager is not None`) integrada al `asyncio.wait(FIRST_COMPLETED)` existente → su muerte despierta el `wait`, cancela pendientes, y el runner **reinicia limpio** (comportamiento A, no "bot caído"). Vive en `data_capture.py`, NO en el manager.

### Gap (c) — reintegración de evictados inexistente
- Nada saca tickers de `_evicted` en runtime → un ticker evictado queda inútil hasta reinicio manual.
- **Diseño aprobado (en PR #12): cooldown determinista 3600s** (reemplaza el "00:00 UTC" del docstring; mejor: reusa el supervisor, reintegra 1h tras cada evicción, patrón del TTL de `last_error`). Flujo de 4 pasos:
  1. `_evicted: set` → `_evicted_cooldowns: dict[str, float]` (ticker → deadline `monotonic`).
  2. `_evict_ticker`: `_evicted_cooldowns[ticker] = monotonic() + 3600`.
  3. Guarda en `handle_message`: `if ticker in _evicted_cooldowns: return` (membresía explícita — protege tickers nuevos).
  4. Limpieza en el tick de 1s: saca los vencidos, borra `book`, reentran por bootstrap de Part A.
- **Depende de Gap (b):** sin supervisor corriendo, la limpieza no ocurre. Se cierran juntos.

---

## 3. Lo que la última directiva pedía vs realidad

La directiva "DOCUMENTACIÓN DE GAPS + DISEÑO READ-ONLY" (y su versión corregida por Gemini) pide trabajo que **ya está completo**:

| Pedido | Estado |
|---|---|
| Cerrar PR #7 | ✅ ya cerrado (no reabrir) |
| Alinear brief a PR #11 | ✅ commit `0bd88f9` |
| Crear `part_b_gaps_pendientes.md` | ✅ existe, PR #12 |
| Diseño anti-zombie (b) con comportamiento de recuperación A | ✅ ya especifica "runner reinicia limpio", no "bot caído" |
| Cooldown determinista (c) en vez de 00:00 UTC | ✅ ya reescrito, 4 pasos |

**Conclusión: no hay nada nuevo que documentar. Re-mandar la directiva produce duplicados.**

---

## 4. Decisiones de diseño ya cerradas (no reabrir sin motivo)

- `seq` es **global por sid**, no por ticker → "gap por-ticker" es normal, NO se detecta como error. (Discovery 4)
- `code 15` viene del **canal WS**, no HTTP → timeout/abort sobre el path WS. Re-auth RSA-PSS se hereda del loop `run()` (firma fresca por conexión); NO inyectar re-auth en V2.
- Cooldown determinista 3600s `monotonic` > "00:00 UTC" (sin cron, reintegra por-ticker).
- Guarda de evicción por **membresía en contenedor explícito**, NUNCA `book is None` (rompería tickers nuevos).

---

## 5. PRÓXIMO PASO (lo único productivo ahora)

**NO** otra directiva de documentar. Una de estas dos:

1. **Revisión adversarial de los diseños (b) y (c) en PR #12.** ¿Pasan? Si hay hueco, marcarlo → se ajusta el doc. Si pasan → aprobados.
2. **Si se aprueban:** autorizar levantar la prohibición sobre `orderbook_manager_v2.py` + `data_capture.py` e **implementar (b) y (c) en un turno dedicado** (branch nueva, PR para review, flag SIEMPRE False, tests). Ese es el cierre de Part B.

**Invariante mandatorio durante todo esto:** `USE_ORDERBOOK_MANAGER_V2=False`. PR #11 no se mergea para activar V2 hasta que (b) y (c) estén implementados, testeados y revisados.

---

## 6. Causa del ruido (para no repetirlo)

El patrón que rompió la línea de trabajo: directivas que mezclaban **diseño + implementación en el mismo turno** con "EXECUTION MODE / ZERO DEBATE", saltando el gate de review. Eso produjo Part A y Part B (PR #11) como código-antes-del-gate. La tanda reciente de directivas **ya corrigió esto** (pide diseño read-only, separa de implementación). Mantener: **diseño → revisión adversarial → (turno separado) implementación → PR → review**.
