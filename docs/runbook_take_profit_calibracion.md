# Runbook — Calibración del take-profit (Motor 3, FASE 1)

Cómo elegir `MOTOR_3_TAKE_PROFIT_CENTS` con datos reales, antes de encender la venta.

## Principio

El take-profit cierra una posición cuando el **bid del lado abierto ≥ umbral** (default 90¢),
asegurando ganancia antes de una remontada. El umbral correcto sale de **datos de shadow**, no de
una corazonada: se enciende la detección sin vender, se mira qué habría cerrado y a qué precio, y
recién entonces se activa la ejecución.

## Fuente primaria — logs shadow (la que manda)

1. En Coolify: `MOTOR_3_TAKE_PROFIT_ENABLED=true`, `MOTOR_3_EXECUTION_ENABLED=false`
   (shadow: detecta + loguea, **no vende**). `MOTOR_3_CLV_ENABLED=true` para que el engine corra.
2. Dejar correr unos días y juntar las líneas:
   ```
   [MOTOR 3 TP SHADOW] take_profit <ticker> <count>c side=<yes|no> bid=<X>c >= <umbral>c
   ```
   Cada línea es una posición que el take-profit **habría cerrado** al `bid` live de ese tick
   (el bid real del orderbook que evalúa Motor 3, vía `engine._current_bid`).
3. Mirar la distribución de `bid`: cuántas salidas a 90¢ vs 85¢ vs 80¢, y contrastar contra el
   resultado final de esas posiciones (¿se remontaron y perdieron, o habrían ganado más?). El
   umbral baja si muchas posiciones a 85–89¢ terminaron remontándose; sube si dejabas plata
   arriba de la mesa cerrando demasiado pronto.

## Fuente secundaria — script de snapshot (intuición rápida)

```
python scripts/calibrar_take_profit.py
```

Cruza las posiciones abiertas con el **último `market_snapshots`** de cada ticker y estima, para
umbrales 90/85/80¢, cuántas cerraría y el PnL aproximado (entry real de `trades.fill_price_cents`,
pata BUY filled, FIFO).

⚠️ **Aproximado.** `market_snapshots` se captura cada ~5 min; Motor 3 evalúa contra el orderbook
**live**, no contra ese snapshot. Útil como primer vistazo, no como decisión final — esa la dan
los logs shadow.

## Encender la venta

Cuando los datos shadow respalden un umbral, setear `MOTOR_3_TAKE_PROFIT_CENTS=<valor>` y recién
ahí `MOTOR_3_EXECUTION_ENABLED=true`. La venta sigue siendo IOC al bid (no garantiza fill completo;
el remanente se reintenta el próximo tick mientras la condición se mantenga).
