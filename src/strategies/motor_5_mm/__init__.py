"""
Motor 5 — Market Maker (F1: SHADOW).

Cotiza bid/ask hipotéticos alrededor del fair value de Motor 2 (consenso sportsbook sin
vig, vía FairValueBook) contra el book real de Kalshi, y registra fills HIPOTÉTICOS con
una regla conservadora de cruce estricto. CERO órdenes en F1: el executor no existe
todavía (se construye en F2) — no hay ninguna ruta de código que llegue a place_order.

Plan por fases y gates: docs/motor_5_market_maker_plan_fases.md. Transición F1→F2 la
decide Noel con ≥14 días de tracker (mm_quotes / mm_shadow_fills / mm_funnel_snapshots).

Piezas (patrón Motor 3: lógica pura testeable + engine supervisado):
  - quoter.py       PURO: fair + book + inventario → QuoteSet (o skip con motivo)
  - shadow_fill.py  PURO: quote resting del tick anterior + book actual → fills hipotéticos
  - inventory.py    posición neta simulada por ticker + PnL mark-to-market neto de fees
  - engine.py       Motor5Engine: loop supervisado, funnel por ciclo, persistencia
"""
