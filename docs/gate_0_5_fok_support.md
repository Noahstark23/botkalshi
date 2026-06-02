# Gate 0.5 — Soporte de FOK/IOC en la API de Kalshi (read-only, RESUELTO)

**Fecha:** 2026-06-01
**Método:** doc oficial de Kalshi + verificación cruzada contra múltiples repos de producción en GitHub (no se pudo fetchear la doc directamente — ReadMe/docs.kalshi.com devuelven 403 a fetch automatizado — pero la evidencia convergente es concluyente).
**Propósito:** definir el ejecutor del Motor REST (FOK nativo vs IOC+verificación). NUNCA limit+rollback (bug de la Tarea 1).

## Resultado: Kalshi SOPORTA FOK e IOC nativos vía `time_in_force`

El endpoint `POST /portfolio/orders` (CreateOrder) acepta el campo **`time_in_force`** con estos valores enum (strings completos, no abreviaturas):

| `time_in_force` | Comportamiento |
|---|---|
| **`"fill_or_kill"`** | **FOK** — se ejecuta completa de inmediato o se cancela entera. **Cero resting, cero exposición parcial.** |
| **`"immediate_or_cancel"`** | **IOC** — ejecuta lo que pueda de inmediato, cancela el resto. No puede combinarse con `expiration_ts`. |
| `"good_till_canceled"` | GTC — resting hasta cancelar (o hasta `expiration_ts` si se da). **Este es el modo del bug actual.** |

Notas de la doc oficial:
- Si se especifica **`buy_max_cost`**, la orden adopta comportamiento **FOK automáticamente**.
- `immediate_or_cancel` **no** se combina con `expiration_ts`.
- "GTT es un tipo de ejecución interno, NO un valor válido de `time_in_force` en la API."

## Evidencia (convergente)

1. **Doc oficial:** `https://docs.kalshi.com/api-reference/orders/create-order` — `time_in_force` con valor `"fill_or_kill"` en el ejemplo de request; `immediate_or_cancel` documentado.
2. **Repos de producción que lo confirman (GitHub code search):**
   - `betaclone1/rec_io` → `KALSHI_TIME_IN_FORCE_VALUES = frozenset(("fill_or_kill", "immediate_or_cancel", "good_till_canceled"))` — el enum exacto.
   - `SagnikBhadra/Arbitrage-Betting` → `{"type": "limit", "time_in_force": "fill_or_kill"}` — **FOK + limit en un bot de arbitraje** (caso idéntico al nuestro).
   - `Quant-Liam/...` → `"time_in_force": "fill_or_kill", "buy_max_cost": ...` — confirma el auto-FOK con buy_max_cost.
   - `Cole-Godfrey/kore`, `austi20/NBABets-v2` → `time_in_force="immediate_or_cancel"`.
   - `mgaruccio/kalshi-trader` → comentario "Use full time_in_force strings, not abbreviations".

## Sintaxis del payload (para el ejecutor del Motor REST)

```json
{
  "ticker": "KX...",
  "action": "buy",
  "side": "yes",
  "count": 10,
  "type": "limit",
  "yes_price": 42,
  "time_in_force": "fill_or_kill",
  "client_order_id": "uuid..."
}
```
- `type` sigue siendo `"limit"` (con `yes_price`/`no_price`); lo que cambia el comportamiento es **`time_in_force: "fill_or_kill"`**.
- El `KalshiRestClient.place_order` actual (`src/clients/kalshi_rest.py`) **NO expone `time_in_force`** — habrá que agregar el parámetro (cambio menor, en su turno de implementación).

## Decisión para el ejecutor del Motor REST (queda fijada)

**(A) FOK nativo en ambas patas** — viable y confirmado. Es la recomendación de `motor_rest_design.md §4.1`: cada pata como `time_in_force="fill_or_kill"`. Si una no se llena completa, se cancela sola → **cero exposición direccional, sin rollback necesario**.

→ El fallback (C) IOC+verificación queda como plan B solo si en testing real FOK resulta problemático (p.ej. baja tasa de fill en books finos). Pero (A) está disponible y es el camino primario.
→ **(B) limit+rollback queda descartado** (es el bug de la Tarea 1 / Issue #14).

## Pendiente de implementación (no ahora)
- Agregar `time_in_force` a `KalshiRestClient.place_order`.
- Verificación final contra la cuenta demo real al implementar (confirmar que el envío FOK se acepta y se comporta como esperado) — `[verificar en demo]`.

## Gobernanza
Read-only. Nada implementado. `place_order` no modificado. V2 archivado, flags en False.
