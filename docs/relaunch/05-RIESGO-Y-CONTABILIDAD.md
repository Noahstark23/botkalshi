# 05. Riesgo, reservas y contabilidad

## Política de referencia, no balance

Capital inicial de referencia USD 200 para todo el experimento. Saldo real, modalidad y capital libre aún requieren confirmación/conciliación. No registrar 200 como un activo nuevo, no usar rachas previas como curva auditada y no tratar transferencias bancarias a/de Kalshi como P&L.

Unidad `u=min(2, 0.01*capital_conciliado)`. No subirla automáticamente por ganancias. Mientras no haya capital conciliado, solo dimensionamiento ilustrativo SIMULADO, unidad de referencia 2. Habitual 0.5u; máximo 1u por tesis correlacionada, incluidos costos. Exposición abierta total 3u; nuevo riesgo comprometido diario 3u en America/Los_Angeles. No reciclar ganancias ni cierres del día para multiplicar este presupuesto.

Pausa al alcanzar pérdida semanal neta confirmada USD 12. Pausa de la prueba ante pérdida neta acumulada USD 20: equivale a 180 solo sin flujos. No abrir algo que, perdiendo junto con todo lo abierto, exceda ese límite. Aportes/retiros no resetean ni mejoran el resultado. Reanudar tras pausa exige revisión explícita.

Sin préstamos, margen, martingala, promedio a la baja, persecución de precios, Kelly o aumento por racha. Sin perps, futuros, opciones, forex apalancado o ventas en corto reales. La primera fase de mecánica, si se autoriza posteriormente, usa contratos simples totalmente financiados. M5 no se salta estos límites por denominarse market making.

## Reserva determinista

Antes de proponer una cantidad, el snapshot debe ser completo, reciente y con balance/órdenes/fills conciliados. Incluir órdenes pendientes, resting, parcialmente llenadas, cancelación solicitada y UNKNOWN; no dar crédito a coberturas no verificadas. Agrupar mismo evento y otras tesis comunes; dos quotes opuestas pueden reservar riesgo simultáneo.

Presupuesto permitido = máximo de cero y mínimo entre objetivo de la propuesta, headroom de tesis, abierto, diario, efectivo utilizable y margen hasta la pérdida experimental. Para el último: `20 + PnL_realizado_neto_acumulado - perdida_maxima_abierta`, con convenciones que no descuenten fees dos veces. Si PnL o riesgo abierto son desconocidos, permiso REAL = ninguno.

La reserva y el contador diario deben actualizarse atómicamente por un escritor; múltiples evaluaciones no pueden gastar el mismo headroom. Distinguir intención no enviada (reserva provisional) de riesgo aceptado/comprometido. Una cancelación confirmada libera exposición abierta; no vuelve a habilitar automáticamente el gasto diario ya comprometido.

## Precio y cantidad

Usar ask y profundidad del lado comprado, tick grid y tarifa observada por producto/serie/evento. No fijar globalmente 2 centavos por contrato. No suponer descuento maker ni dividir la tarifa por dos para mejorar el experimento. Con fracciones/subcentavos, conservar precisión y redondeos/rebates del proveedor.

Para compra simple financiada, calcular la mayor cantidad entera n tal que `n*ask + fees(n,ask) + otros_costos_maximos <= presupuesto`. Si no cabe un contrato, n=0. La orden inicial puede ser entera, pero sus fills pueden ser fraccionarios: no usar int para conciliar. Emplear Decimal o unidades escaladas documentadas.

Con payout total n USD: break-even = costo_total/n; EV = p*n - costo_total, solo con probabilidad identificada y sensibilidad. No convertir una noticia o acuerdo entre agentes en una probabilidad calibrada. Un contrato sobre oro o BTC no es posesión de oro o BTC.

En M5, ask YES que abre NO cuesta `1-precio_yes`; vender para cerrar un YES existente tiene otra semántica. No compartir una fórmula ciega entre aperturas y reducciones. Los stops de activos continuos no garantizan la pérdida presupuestada.

## Ledger

Separar presupuesto nominal, efectivo verificado, fondos reservados, costo de posiciones, liquidación verificable, P&L realizado/no realizado, comisiones, financiación, aportes/retiros y costos de infraestructura. Mantener REAL, SIMULADO y observaciones sin ejecución distintos.

Una operación REAL exige ID, fecha/hora, ticker exacto, lado/intención, cantidad y precio ejecutados, costos, capital comprometido, estado y fuente. Sin continuidad, no reconstruir una curva por resultados deportivos.

A resolución: P&L = cobro total - costo total inicial - costos posteriores; el cobro ya incluye capital devuelto. Venta anticipada: ingreso neto de venta - adquisición. Valor al mid no es liquidación ejecutable. Fees estimadas no son fees realizadas. Un error de lectura jamás significa cero posiciones.

Guardar entradas de ledger inmutables; corregir con eventos compensatorios. Paginación completa y deduplicación por IDs del exchange. Reconciliar también fills de órdenes que desaparecieron de la primera página. El reinicio no libera reservas hasta resolver el estado.
