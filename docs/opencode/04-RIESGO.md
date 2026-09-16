# 04 — Política del bank experimental

Política propuesta por el operador; no autorización de operaciones. Los límites son decisiones de riesgo, no garantía de ejecución de stops, liquidez o pérdida máxima de instrumentos no acotados.

## Constantes y estado

| Control | Regla |
|---|---|
| Referencia nominal única | USD 200 para toda la prueba. |
| Unidad inicial máxima | USD 2, nunca USD 2 por cada contrato de una misma tesis. |
| Tamaño habitual | Media unidad: inicialmente USD 1, incluidos costes. |
| Máximo conjunto por tesis | Una unidad para partido/evento/tesis correlacionada. |
| Máximo de riesgo abierto | Tres unidades; inicialmente USD 6. |
| Nuevo riesgo comprometido diario | Tres unidades por día de California; no reciclar ganancias o cierres del día. |
| Pausa semanal | Pérdida neta semanal confirmada de USD 12; revisión antes de nuevas propuestas reales. |
| Pausa acumulada | Pérdida neta acumulada de USD 20; también considerar peor caso de todo lo abierto antes de proponer. |

La unidad se reduce a `min(2 USD, 1% del capital conciliado)` si cae el capital y no sube automáticamente por ganancias. Persistir la unidad aprobada: una recuperación/reinicio no autoriza ratchet ascendente. Redondear el presupuesto hacia abajo a la unidad monetaria operable. No préstamos, margen prestado, apalancamiento, Kelly, martingala, reposición de pérdidas, promediar a la baja, perseguir cuotas o aumentar por rachas.

La prueba se mantiene separada de obligaciones, vivienda, reserva, capital empresarial y cartera patrimonial. No constituye financiación de ninguna de esas metas. La revisión metodológica a 30 días desde inicio confirmado no obliga a operar, no valida estadísticamente una ventaja y no permite aumentar capital por sí sola.

## Requisitos anteriores a cualquier elegibilidad

El modo real, apertura y fondos separados deben estar confirmados; no pedir nueva confirmación si ya existe evidencia vigente. Se exige conciliación con manifiesto, continuidad, timestamps válidos/con zona, cobertura de endpoints, órdenes abiertas/UNKNOWN y alcance de cuenta. El guard rechaza fuente vacía o arbitraria, fechas futuras/viejas y metadatos inconsistentes. La antigüedad máxima es un parámetro conservador documentado y probado según el caso; no un supuesto oculto.

Los totales diarios se derivan de compromisos reales auditables en America/Los_Angeles, no de una cifra editable sin procedencia. Guardar zona IANA para cambios DST. La ventana semanal debe fijarse explícitamente; propuesta a aprobar: lunes 00:00 hasta el lunes siguiente en esa zona. Hasta fijarla no certificar headroom real semanal. Las pausas son persistentes hasta revisión registrada; no se borran al reiniciar o cambiar de día/semana.

Sin conciliación confiable, `real_entry_eligible=false` y presupuesto autorizado para NUEVAS PROPUESTAS REALES cero; esto no afirma efectivo real cero. Mantener `execution_authorized=false` y `order_capability_present=false` incluso con todos los datos disponibles.

## Cálculo de headroom

Sea u la unidad vigente no aumentada, O el peor riesgo abierto total incluyendo pendientes/UNKNOWN, D el nuevo riesgo comprometido del día y T el riesgo de la tesis correlacionada. Validar las tres bases antes de restar. La agrupación no se resuelve sólo por ticker: activos diferentes pueden expresar la misma tesis. Sin mapa fiable, no asumir independencia ni crédito por cobertura.

Para calcular el piso experimental sin contar dos veces pérdidas, definir P como el P&L realizado acumulado neto del ledger de prueba y O como la pérdida adicional posible del remanente abierto con sus costes correctamente asignados. Exigir `P - O - nuevo_riesgo >= -20`. Publicar P&L no realizado aparte; no incluirlo en P y volver a restar el coste abierto completo. Fees ya realizados o imputados al remanente se descuentan una sola vez. Si no puede reconciliarse esta base, bloquear.

El presupuesto candidato no excede el mínimo entre u, `3u-O`, `3u-D`, `u-T`, `20+P-O` y efectivo verdaderamente libre atribuible a la prueba. Cualquier pausa manda sobre el cálculo. Reservas deben tratarse según la semántica del balance para no descontarlas dos veces. Registrar reservas de propuesta de forma separada a compromisos reales; no convertir una recomendación en transacción.

## Cantidad y costes

En compra simple completamente financiada con payout contractual USD 1: elegir la mayor cantidad entera n tal que `costo_compra(n) + fee_entrada(n) + otros_costes_pertinentes(n) <= presupuesto`. Usar ask real y profundidad para esa cantidad, tarifa/versionado/redondeo por orden, no multiplicar una comisión unitaria asumida. Si el mínimo/tarifas no encajan, DESCARTAR, no elevar el límite.

Break-even hasta resolución = coste neto total / payout total. EV(n,p) = p * payout total - coste total sólo con p identificado y análisis de sensibilidad. Una probabilidad de acertar al vencimiento no demuestra probabilidad de vender con beneficio antes. Simular latencia, liquidez y ambas comisiones para cierre anticipado; no llamar fill a un cruce teórico.

Acciones/oro/cripto spot se investigan sin forzar reparto de los USD 200. Perpetuos, futuros, FX apalancado, opciones y cortos quedan sólo investigación/simulación. En precios continuos distinguir capital, nocional, margen, pérdida al stop y peor escenario; un stop no garantiza la pérdida presupuestada.
