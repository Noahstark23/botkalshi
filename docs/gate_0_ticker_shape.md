# Gate 0 — Shape del mensaje `ticker` (read-only, RESUELTO: PASA)

**Fecha:** 2026-06-01
**Resultado:** el `ticker` del WS de Kalshi **expone BBO** → el trigger por spread del Motor REST es **VIABLE**. El diseño sobrevive el gate.
**Método:** captura en shadow puro (`scripts/capture_ticker_shape.py`), suscripción solo-`ticker`, sin operar.

## Qué trae el mensaje `ticker`

- **`yes_bid_dollars` / `yes_ask_dollars`** — BBO del lado YES (fixed-point dollar strings, shape 2026).
- **sizes** asociados a cada nivel del BBO.
- El **lado NO se deriva** (Kalshi binario: YES y NO son complementarios):
  - `no_bid = 100 − yes_ask`
  - `no_ask = 100 − yes_bid`

## Implicaciones para el trigger (§2 de `motor_rest_design.md`)

1. **La condición de arb se computa de UN SOLO mensaje `ticker` por mercado.** No hace falta GET de orderbook para *detectar* el spread elegible — el ticker ya trae yes_bid/yes_ask, y el lado NO se deriva. El spread crudo (`yes_ask + no_ask` vs 100, con NO derivado) sale directo del ticker. (El GET REST sigue siendo necesario para *confirmar* profundidad/sizes reales y *ejecutar* — el ticker es la campana, el REST es la verdad.)
2. **Los sizes del ticker permiten filtrar profundidad EN EL TRIGGER.** Se puede descartar antes de pedir el orderbook un spread que tenga BBO atractivo pero size irrisorio → menos GETs inútiles, menos presión sobre el throttle/429 (§2.1).
3. **Unidades:** `*_dollars` = fixed-point strings → reusar `parse_price_to_cents` para llevar a cents (mismo parser del resto del sistema).

## Estado de gates tras esto

| Gate | Estado |
|---|---|
| **Gate 0** (shape ticker — trae BBO) | ✅ **PASA** |
| **Gate 0.5** (FOK nativo — `time_in_force="fill_or_kill"`) | ✅ **PASA** (`docs/gate_0_5_fok_support.md`) |

**Ambos bloqueantes cerrados.** El siguiente paso autorizable (en su turno) es el diseño del ejecutor FOK del Motor REST → review → implementación.

## Pendientes anotados (NO bloquean el diseño, pero sí la activación)

1. **Re-confirmar shape sobre SOCCER cuando abran mercados del Mundial.** La captura se hizo sobre mercado(s) disponible(s) (NBA). El shape del `ticker` *debería* ser genérico entre deportes, pero el Motor REST es para soccer → check rápido de 30s sobre un mercado del Mundial cuando abra, para confirmar que `yes_bid_dollars`/`yes_ask_dollars`+sizes vienen igual. Bajo riesgo, pero verificar antes de operar soccer.

2. **Medir la CADENCIA de actualización del ticker bajo carga** — *cuánto* tarda Kalshi en empujar un `ticker` nuevo cuando el BBO se mueve. Esto define si la detección **reacciona a tiempo**: si el ticker se actualiza cada, p.ej., 500ms, una ventana de arb de 200ms se pierde aunque el trigger sea instantáneo. **Se mide junto con el RTT bajo carga** (`bench_rest_rtt.py`) en mercado activo, horario pico. Es la otra mitad de la latencia total de decisión (cadencia-ticker + RTT-REST + parse). Pendiente de medición en vivo; no se puede estimar sin datos reales.

## Gobernanza
Read-only. Nada implementado. V2 archivado, `USE_ORDERBOOK_MANAGER_V2=False`, `TRADING_ENABLED=False`.
