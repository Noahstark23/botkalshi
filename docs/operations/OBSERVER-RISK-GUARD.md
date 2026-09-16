# Observer + guard de riesgo

Fecha: 2026-09-16. Este componente es de investigación y control; no coloca órdenes.

## Archivos de salida

- `/var/lib/botkalshi-research/coverage.json`: evidencia del barrido público Kalshi (páginas, cursores, truncación, mercados vistos y orderbooks obtenidos).
- `/var/lib/botkalshi-research/risk-status.json`: estado calculado de la política experimental.
- `/var/lib/botkalshi-research/risk-input.json`: entrada opcional de conciliación. No se crea con saldos inventados.
- `/var/lib/botkalshi-research/research.sqlite3`: observaciones durables del collector existente.

## Estado fail-closed

Si `risk-input.json` falta, es inválido o no contiene una conciliación explícita, el estado debe ser `PENDING_RECONCILIATION` o `INVALID_INPUT_FAIL_CLOSED`, con `real_entry_eligible=false` y `new_risk_headroom_usd=0.00`.

`execution_authorized` y `order_capability_present` permanecen `false` incluso cuando el estado reconciliado está dentro de los límites. El guard calcula; no ejecuta.

## Política conservada

- Capital único de referencia: USD 200.
- Unidad: `min(USD 2, 1% del capital reconciliado)`; nunca aumenta automáticamente por ganancias.
- Máximo abierto: 3 unidades.
- Máximo de nuevo riesgo diario de California: 3 unidades.
- Pausa por pérdida semanal neta confirmada: USD 12.
- Pausa por pérdida acumulada neta confirmada: USD 20.
- Cero préstamos, margen, martingala o reposición automática de pérdidas.

## Despliegue

Usar el instalador inmutable ya existente sobre el Droplet dedicado y pasar el SHA completo aprobado de esta rama/PR. El instalador ejecuta la suite `infra/digitalocean-shadow/tests/` antes de reiniciar el servicio y se detiene si no obtiene captura nueva verificable.

No desplegar en el servidor de Nortex. No copiar claves o estados de cuenta al repositorio. No convertir recomendaciones en registros de operaciones: una operación real requiere fill/confirmación conciliable.

## Criterio de cobertura

El observer recorre cursores hasta agotarlos o hasta un límite explícito. Si alcanza un límite de páginas/orderbooks o encuentra `close_time` inválido, `coverage.json` lo declara. Un barrido truncado no debe presentarse después como “mercado completo revisado”.
