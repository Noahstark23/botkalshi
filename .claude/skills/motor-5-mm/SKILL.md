---
name: motor-5-mm
description: Motor 5 — market maker (quotes GTC post_only alrededor del fair de Motor 2) con fases F1 shadow / F2 demo / F3 producción. Usar al tocar el engine/executor/reconciler del MM, quotes resting, inventario, la llave F3, el canary cap o el runbook de activación. Las quotes RESTING son el riesgo distintivo: todo el diseño gira en torno a nunca perderles el rastro.
---

# Motor 5 — market maker (MM)

Cotiza bid/ask alrededor del fair de Motor 2 (`FairValueBook`) con órdenes **GTC
post_only** — el ÚNICO motor cuyo estado natural es tener órdenes RESTING en el book.
Eso invierte el problema: los demás motores temen al fill parcial; el MM teme a la
**quote viva que nadie gestiona**. Protocolo general: skill `botkalshi`.

## Fases (no saltearlas)

- **F1 SHADOW** (`MOTOR_MM_ENABLED=true`, sin execution): quotes hipotéticas, fills
  simulados por cruce estricto (`shadow_fill.py`), CERO órdenes. El engine ni construye
  executor.
- **F2 DEMO** (`+ MOTOR_MM_EXECUTION_ENABLED=true`, `KALSHI_ENV=demo`): órdenes reales
  contra demo. Valida MECÁNICA (place/cancel/reconcile), no edge.
- **F3 PRODUCCIÓN**: exige la llave `MOTOR_MM_F3_ACK='NOEL-OK-F3'` (sin ella el boot
  FALLA) y la secuencia del runbook — el ORDEN manda: 1) `scripts/motor5_smoke_test.py`
  contra producción (verificar el cancel), 2) girar la llave en el env, 3) redeploy
  supervisado (`docs/motor5_runbook_activacion.md`). Canary: `MOTOR_MM_MAX_EXPOSURE_USD`
  (100) es techo DURO del costo abierto pending+filled, aparte del headroom global.

## Mapa

```
src/strategies/motor_5_mm/
├── engine.py        # loop 60s: gates (kill-switch → cancel-all UNA vez / quotes_paused
│                    #   / reconcile) → fills → quote por ticker → funnel snapshot
│                    #   + cancel_all("shutdown") al salir (P0 2026-07-07)
├── executor.py      # place/cancel con intent pre-red, lock por ticker, corrupted-no-opera,
│                    #   canary cap, cancel_all batch
├── reconciler.py    # la VERDAD runtime: pending↔get_orders + fantasmas + HUÉRFANAS
│                    #   (resting no gestionadas por este proceso → cancel)
├── quoter.py        # compute_quote (half-spread, inventario, skips)
├── inventory.py     # InventoryBook (net por ticker, MTM)
├── shadow_fill.py   # fills hipotéticos F1 (cruce estricto)
└── fill_feed.py     # fast-path WS de fills → dispara reconcile
```

Config: `MOTOR_MM_MAX_TICKERS` (10), `HALF_SPREAD_CENTS` (3), `QUOTE_SIZE_CONTRACTS`
(10), `MAX_INVENTORY_CONTRACTS` (50, al tope solo se cotiza el lado que reduce),
`FAIR_TTL_SEC` (600 — sin fair fresco no hay universo).

## Invariantes (Lección 9 y el ciclo de vida de una quote)

1. **El estado lo muta SOLO el loop del engine, secuencialmente.** Excepción en un
   ticker → su quote viva se descarta y re-sincroniza el próximo tick — nunca "seguir
   operando" con estado dudoso.
2. **Intent pre-red**: fila `pending` ANTES de `place_order`. Error ambiguo (5xx/timeout)
   → fila queda pending + ticker CORRUPTO; error determinístico (4xx, ej. post_only
   cruzaría) → `cancelled`. **Corrupto no opera**: primero cancel-all del ticker, el
   reconciler des-corrompe cuando la verdad de la API queda limpia.
3. **Toda quote resting tiene exactamente un dueño.** Tres redes anti-huérfanas:
   (a) `cancel_all("shutdown")` al salir del loop; (b) el reconcile de cada tick recibe
   `live_coids` del executor y CANCELA toda resting nuestra que este proceso no gestiona
   (cubre kill -9/OOM donde (a) no corre); (c) fantasmas (resting en API sin fila DB) se
   cancelan + corrupto. En live, los fills vienen SOLO de la verdad del
   reconciler/cancel-response — jamás de la inferencia shadow ni del WS directo.
4. **Fills al inventario UNA vez por coid** (`_apply_settled_fills`, idempotente).
   El settlement de los fills lo hace el SettlementPoller (strategy `motor_5_mm` está
   en STRATEGIES; sells = lado opuesto a 100−P; `filled_count` = el fill real).
5. Pánico: kill-switch engaged → cancel-all UNA vez y gestión mínima sin cotizar.
   `set_mm_quotes_paused(True)` → deja de EMITIR quotes pero sigue gestionando las vivas.

## Diagnóstico

- Funnel por tick: `MMFunnelSnapshot` / log `motor5.funnel` (fair_fresh, quoted, skips,
  fills, inv_abs, mtm). `fair_fresh=0` sostenido = el poller de M2 no publica fair.
- Estado de quotes: `mm_quotes` (persistidas), `Trade` strategy `motor_5_mm`
  (pending=resting, filled+filled_count, cancelled), log `motor5.reconcile` (resting/
  filled/cancelled/phantom/orphan/discrep).
- P1s ABIERTOS (auditoría 2026-07-07 — repetir al proponer F3): reserva del ask
  subcontada ~7×, TTL del fair no distingue in-play, `get_orders(limit=200)` sin
  paginación en el reconciler.
