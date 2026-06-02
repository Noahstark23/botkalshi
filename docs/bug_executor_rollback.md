# BUG — executor.py: limit resting no dispara rollback → exposición direccional no detectada

**Severidad:** Alta (riesgo financiero) — **NO urgente** (nada opera hoy: `TRADING_ENABLED=False`, ningún motor activo).
**Alcance:** `src/strategies/motor_1_arbitrage/executor.py` (`ArbitrageExecutor.execute`). Afecta a **cualquier** consumidor del executor — hoy Motor 1; mañana cualquier motor que ejecute trades. **Independiente del Motor REST** (se descubrió diseñándolo, pero el bug ya existe).
**Estado:** documentado, sin fix. No tocar bajo `TRADING_ENABLED=False`; fix obligatorio **antes** de cualquier activación de trading real.

---

## Resumen en una línea

El rollback de arbitraje se dispara **solo si una pata lanza excepción**, pero las patas se colocan como órdenes **`limit`** que pueden **aceptarse sin llenarse** (quedar resting si el precio se movió). En ese caso el executor cree que el arb se completó cuando en realidad una pata quedó abierta → **posición direccional con capital real, no detectada**.

## Evidencia en código (`executor.py`, `execute()`)

1. Cada pata se coloca como **`order_type="limit"`** al `price_cents` de la `ArbLeg`:
   - `_place_leg` → `place_order(..., order_type="limit", yes_price=.../no_price=...)` (líneas ~360-369).
2. El éxito/fallo de cada pata se decide **solo por excepción** (líneas 183-190):
   ```python
   if isinstance(result, BaseException):
       ... failed = True            # ← única condición de "falló"
   else:
       filled.append((leg, coid))   # ← "aceptada" se trata como "llena"
   ```
3. Si ninguna pata lanzó excepción → `if not failed:` (línea 202) → "all legs filled", retorna `True`. El rollback (línea 213) **nunca se evalúa**.

## El gap

Una orden `limit` que **no cruza** (porque el book se movió en los ~100ms entre detección y envío) es **aceptada por Kalshi** (entra al libro como resting/parcial) y `place_order` **retorna OK, sin excepción**. El executor la cuenta como `filled`. Resultado:

- Pata YES se llena (cruzó), pata NO queda resting (no cruzó) → el executor cree "arb completo" → **el bot tiene una posición YES direccional abierta** que nadie cierra.
- El rollback (`_execute_iterative_rollback`), que existe y funciona bien, **no se invoca** porque `failed` quedó `False`.
- La exposición es **silenciosa**: ni log de error, ni rollback, ni alerta. Solo aparece cuando el mercado resuelve y la posición direccional gana o pierde.

## Por qué importa más allá del Motor REST

`ArbitrageExecutor` es el ejecutor de **Motor 1** (ya en el repo, dormant). Cualquier activación de trading que lo use hereda el bug. El arbitraje binario es "ganancia garantizada **solo si ambas patas se llenan**"; una pata sola convierte una estrategia de riesgo-cero en una apuesta direccional — exactamente lo que el arb pretende evitar.

## Direcciones de fix (a diseñar en su propio ticket, NO acá)

- **Verificar fill real, no "orden aceptada":** tras colocar, confirmar el `status`/`fill` de cada orden (campo de la respuesta de Kalshi o polling) antes de marcar `filled`. Si una pata no llenó completo → rollback de la(s) que sí.
- **Usar FOK/IOC en vez de `limit`:** una FOK se cancela si no se llena completa → no hay resting silencioso (es la misma decisión que el Motor REST tomó en su §4.1). Requiere confirmar soporte de FOK en la API (`[verificar]`).
- **Tratar "aceptada pero no llena" como fallo de pata** que dispara rollback.

## Relación con el Motor REST

El diseño del Motor REST (`docs/motor_rest_design.md` §4.1) ya **evita** este bug por diseño (FOK-ambas o IOC+verificación, ejecutor propio que NO reusa el de Motor 1 tal cual). Este ticket es para arreglar el executor **de Motor 1**, que sigue con el gap. Ambos comparten la lección: *"orden aceptada ≠ pata llena"*.

## Gobernanza
Documentación de hallazgo. Sin fix en este turno. `executor.py` no se modifica. Fix bloqueante antes de `TRADING_ENABLED=true` con Motor 1.
