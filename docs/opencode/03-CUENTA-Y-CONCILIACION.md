# 03 — Lectura de cuenta y conciliación

Estado de esta entrega: especificación para implementación y fixtures; cuenta real no conectada ni conciliada por estos MD.

## Apertura de la prueba

USD 200 es el único capital inicial de referencia, no saldo existente ni autorización de fondeo. `trial_mode`, `confirmed_start_at`, `initial_funding_confirmed` y el alcance de la cuenta permanecen pendientes hasta evidencia del operador. No crear un registro REAL con ceros para suplir campos faltantes. Mantener historiales REAL y SIMULATION físicamente o lógicamente separados, sin conversiones implícitas.

El ledger de prueba comienza con la primera operación confirmada posterior a su apertura. Datos anteriores pueden servir para reconciliar la cuenta, pero no se incorporan a la rentabilidad de la nueva prueba. Si una cuenta mezcla fondos/posiciones previos, modelar atribución y reservas sin repartir arbitrariamente. No abrir ni transferir a una subcuenta desde este sistema.

## Adaptador de lectura propuesto

Rutas relativas a `/trade-api/v2`, siempre GET, sujetas a validación actual de permisos y esquema:

| Ruta | Uso |
|---|---|
| `/portfolio/balance` | Efectivo disponible, valoración del proveedor y timestamp; verificar unidades y alcance. |
| `/portfolio/positions` | Posiciones de mercado/evento y exposición; paginación completa. |
| `/portfolio/orders` | Órdenes, pendientes y parciales, sin crear/cancelar/modificar. |
| `/portfolio/fills` | Ejecuciones, precios, cantidades y fees observados. |
| `/portfolio/settlements` | Cobros de resolución; no equivalen a beneficio íntegro. |
| `/historical/cutoff`, `/historical/fills`, `/historical/orders` | Completar historia más antigua cuando el corte del proveedor lo requiera. |

Fuentes S4-S9. El host externo recomendado y el host compartido compatible se validan contra la documentación vigente; no migrar configuración automáticamente. Para depósitos/retiros y transferencias, descubrir sólo endpoints de lectura documentados y autorizados antes de implementarlos. Mientras falten, declararlos pendientes; no inferirlos de P&L ni inventar un endpoint. Los ejemplos de documentación no son datos de cuenta.

Balance expone unidades distintas, incluidos campos en centavos y strings decimales. No sumar balance y portfolio_value hasta verificar sus semánticas exactas. Scope, subaccount y exchange_index deben corresponder en todos los endpoints; sus nombres de parámetros/campos pueden variar. No sumar dos veces una agregación global y sus desgloses.

## Recolección consistente

Registrar ventana, endpoint, versión, cursor agotado, cantidad, errores, timestamp de fuente/recepción y hash por lote. Deduplicar con identidad estable incluyendo alcance de cuenta/exchange y fill_id, no sólo ticker o fecha. Completar tanto historia reciente como histórica sin huecos ni duplicación en el borde.

Las lecturas paginadas pueden cambiar mientras se recorren. Obtener watermarks y controles antes/después, y repetir acotadamente si hay actividad concurrente. Si no puede garantizarse coherencia, conservar los datos pero emitir PARTIAL/STALE/UNKNOWN, no RECONCILED. No usar un batch truncado como saldo definitivo.

## Ledger y comprobación

Cada ejecución documentada requiere identificador, fecha/hora, instrumento exacto, lado/acción, cantidad, precio ejecutado, costes, capital comprometido, estado y fuente. Separar orden propuesta, orden reportada y fill. Una cancelación solicitada/fallida no libera reservas; un timeout deja UNKNOWN hasta confirmación. Una señal de salida o un resultado deportivo no cierra contablemente una posición.

Mantener eventos auditables append-only y correcciones compensatorias con trazabilidad. Tratar fills parciales, ventas parciales, cobros, comisiones, correcciones/reversiones, aportes/retiros y reinicios idempotentes. Preservar `count_fp` y montos con Decimal; no truncar posiciones reales fraccionarias aunque las nuevas propuestas del experimento sean enteras.

Para una posición totalmente resuelta: P&L neto = cobro total - costo total de adquisición - costes adicionales no incluidos. El cobro incluye devolución de capital. Para venta: ingreso neto - base de adquisición asignada. En parciales, asignar costes consistentemente y conservar remanente y riesgo. No deducir otra vez el spread cuando los precios efectivos ya lo contienen. Flujos externos no son rendimiento.

## Resumen privado requerido

Exponer por separado presupuesto nominal, efectivo verificado, reservas, coste de posiciones, valor de liquidación realizable por profundidad y fecha, P&L realizado/no realizado, comisiones, financiación, aportes/retiros y último capital conciliado. No reutilizar el mismo dólar en categorías sumadas ni identificar coste con valor realizable. Campos desconocidos permanecen null/NO_VERIFICADO con motivo.

El reconciliador entrega un manifiesto de evidencia y discrepancias; el guard sólo consume snapshots verificados y vigentes. Un JSON manual con totales sirve como entrada declarada, no como conciliación automática. La aprobación final requiere comparar el ledger reconstruido con la cuenta, sin tolerancias que oculten pérdidas. Conservar precisión del proveedor y documentar cualquier tolerancia de redondeo.
