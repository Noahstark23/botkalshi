---
name: motor-3-clv
description: Motor 3 — gestión de SALIDAS (take-profit, trailing stop, T-30) sobre posiciones abiertas; NO abre posiciones. Usar al tocar la atribución de posiciones, los helpers de salida, el PortfolioPoller, las huérfanas de Motor 1 o el settle closed_by_clv. La regla de oro: jamás vender la pata de un hedge — toda la skill orbita alrededor de eso.
---

# Motor 3 — CLV (solo cierra, nunca abre)

Vende posiciones abiertas cuando conviene: take-profit por precio (bid ≥ umbral),
trailing stop (retroceso desde el pico, solo en ganancia) y salida por tiempo (T-30min
del cierre). Su riesgo NO es de mercado — es **vender la pata de un hedge** de Motor 1
y romper un payout garantizado. Protocolo general: skill `botkalshi`.

## Mapa

```
src/strategies/motor_3_clv/
├── engine.py           # atribución de posiciones + detección + (venta si live)
├── executor.py         # SELL IOC + intent pre-red + _settle_originals
│                       #   (closed_by_clv=True + pnl real en la fila BUY original)
├── orphans.py          # motor1_orphan_buys: huérfanas de M1 gestionables — NUNCA hedges
├── take_profit.py      # helper PURO (sin red/DB) — bid ≥ umbral
├── trailing_stop.py    # helper PURO — retroceso desde pico, solo en ganancia
└── poller.py           # PortfolioPoller: sync portfolio_positions desde Kalshi (60s)
scripts/diag_motor3_clv.py / calibrar_take_profit.py
```

## Flags

| Var | Default | Qué gatea |
|---|---|---|
| `MOTOR_3_CLV_ENABLED` | false | el motor corre (detecta + loguea `[MOTOR 3 TP SHADOW]`) |
| `MOTOR_3_EXECUTION_ENABLED` | false | la VENTA real. **Capa C no frena sells** → esta Capa A es la ÚNICA protección |
| `MOTOR_3_TAKE_PROFIT_ENABLED` / `_CENTS` (90) | false | detección TP (la venta la sigue gateando EXECUTION) |
| `MOTOR_3_TRAILING_ENABLED` / `_DROP_CENTS` (5) | false | detección trailing |
| `MOTOR_3_MANAGES_ORPHANS` | false | también gestiona huérfanas de Motor 1 (Bug 4) |

Shadow = ENABLED true + EXECUTION false: loguea `[... SHADOW] net=$` con fees reales de
ambos lados — así se calibra el umbral antes de prender.

## La regla de oro: atribución estricta

- `_attributable_positions`: una posición solo es gestionable si sus BUYs abiertos son de
  la PROPIA estrategia (o huérfanas M1 con el flag). El count vendible se CAPEA al count
  atribuible — la posición neta 433 del incidente eran 412 hedged + 21 huérfanas: se
  venden 21, jamás 433.
- `orphans.py::motor1_orphan_buys`: una pata es huérfana SOLO si su `arb_id` no tiene
  hermana `filled` viva. Par hedged completo → intocable. Sin `arb_id` en notes →
  conservador: NO es huérfana.
- **Fail-closed en venta**: `_open_attributable_count` con error de DB → 0 (no se vende).
  Lecturas (poller, orderbook) fail-open: un hiccup no apaga el motor.

## El settle (no duplicar PnL)

- El SELL va IOC con intent pre-red (fila `pending` ANTES de la red; `_reconcile_pending_sells`
  resuelve respuestas perdidas — sin esto hubo re-venta fantasma + PnL doble).
- `_settle_originals`: al fillar el sell, las filas BUY originales pasan a `settled` con
  `closed_by_clv=True` y el pnl REAL (venta − compra − ambas fees). FIFO con split si el
  sell cubre parcialmente. El SettlementPoller **saltea** filas `closed_by_clv` — esa es
  la barrera anti doble-conteo: no la rompas.
- Tiempo: `close_time`/`settled_at` NAIVE UTC (comparar con
  `datetime.now(UTC).replace(tzinfo=None)`).

## Diagnóstico

- ¿Por qué no vendió? Orden de descarte: ¿posición atribuible? (strategy de los BUYs,
  hedge completo, closed_by_clv previo) → ¿flag de detección on? → ¿umbral alcanzado?
  (`[MOTOR 3 TP SHADOW]` en logs) → ¿EXECUTION on? → ¿bid con liquidez al momento?
- ¿Vendió de más / algo raro? Mirar `closed_by_clv`, notes del sell (`-clvexit`), y el
  split de filas. `scripts/calibrar_take_profit.py` para elegir umbral con data.
