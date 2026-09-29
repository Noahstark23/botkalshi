# 06. Datos, contratos y conexión con la IA

## Paquete de evidencia propuesto

Cada paquete debe conservar: schema_version, packet_id/hash, generated_at UTC, corte local, commit/config/policy hashes, experimento, categoría, proveedor, event_ticker, market_ticker, estado del evento y hora de inicio. Identificar 90 minutos/clasificación, F5/juego completo y umbral exacto de cada contrato. Regla, fuente de resolución, cierre/expiración y payout llevan URL/ID, hash y hora de observación.

Cotización: lado/intención, bid/ask, profundidad, timestamp de proveedor, received_at, secuencia y edad. Precios como strings decimales; null es desconocido, no cero. Guardar price_ranges actuales. Cotizaciones históricas sirven para replay, no como entrada presente.

Referencia probabilística: proveedor, método, versión, hora, resultados mutuamente excluyentes y tratamiento de margen. Dos referencias del mismo feed no son independientes. El promedio de varias respuestas IA no constituye validación ni nuevos modelos independientes. Mantener evidencia contraria y datos omitidos.

Datos deportivos adicionales: abridor/opener/restricción de innings, alineación confirmada vs prevista, carga reciente del bullpen; bajas/sanciones/descanso/localía/once en fútbol; disponibilidad, spread exacto y condiciones en football. Si faltan, explicitarlo antes de jerarquizar candidatas.

Datos financieros: instrumento negociable exacto, acceso real, spread/liquidez/horizonte, comunicación del emisor, calendario macro oficial y costo del producto. No sintetizar RSI, medias, funding, OI o velas inexistentes. El adaptador no puede llamar acción/ETF/forex a un contrato binario sobre ese subyacente.

## Versión 1 incluida

`assessment.schema.json` limita la respuesta IA a WAIT, REJECT o REVIEW y exige referencias al paquete. `assessment.synthetic.json` muestra abstención sin inventar probabilidad. `policy.reference.json` declara una política experimental de referencia; **el runner existente todavía no la consume**.

El esquema valida estructura, no hechos ni autorización. La implementación añadirá comprobación semántica: evidencia existe en el paquete, ticker coincide, no hay referencias futuras, no hay contradicción entre evidencia y conclusión y la fecha permite reevaluar. Los mensajes/refusals inválidos producen WAIT con reason_code, nunca fallback a compra.

## Conexión A: exportación para ChatGPT, elegida al inicio

El bot producirá JSON/MD saneados. El operador los comparte en una sesión; ChatGPT analiza fuentes y devuelve una evaluación estructurada. No hay daemon invocando esta conversación, ni vigilancia continua. Este camino aprovecha el uso interactivo de la suscripción sin afirmar que incluya API. Los archivos compartidos no contienen secretos ni ledger personal completo.

## Conexión B: MCP de solo lectura, posterior

Funciones propuestas: `get_coverage(day)`, `get_research_packet(packet_id)`, `get_experiment_report(experiment_id)`, `get_system_health()`. No endpoint de ejecución ni consulta SQL o URL libre. El servidor retorna datos saneados con fecha y límites de cobertura. Autenticación, mínimos privilegios y compatibilidad real con la cuenta deben comprobarse antes de habilitarlo. No se ha instalado ni conectado un MCP mediante esta entrega.

## Conexión C: API, opcional y presupuestada

La API se factura aparte de ChatGPT. Inicialmente está apagada y el presupuesto adicional autorizado es cero. Una futura aprobación podrá fijar, por ejemplo, un techo de USD 5/mes para todo el componente IA; es propuesta, no tarifa ni garantía de suficientes llamadas.

Modelo y precios se seleccionarán con documentación vigente, no por el nombre comercial de esta conversación. Conservar model_id, prompt_hash, schema_version, tokens, tool costs, latency y reportes de error. Cache por paquete/hash; lote pequeño de finalistas; invalidación de caché por nueva evidencia; timeouts, límites de salida y retries acotados.

Reservar costo máximo antes de la llamada y registrar consumo real después. Las alertas de facturación del proveedor no sustituyen un limitador propio; puede haber consumo en vuelo. Agotamiento del presupuesto deja el análisis IA pendiente, sin cambiar el riesgo ni usar otra clave.

## Medir utilidad sin sesgo

Comparar baseline M5 intacto con evaluación IA registrada aparte sobre el mismo universo y corte temporal. Medir omisiones, evidencia incorrecta, frescura, tasa de abstención, costo y cambios de selección fuera de muestra. Solo medir probabilidades con Brier/log-loss/calibración cuando se emitan estimaciones predefinidas; no inventarlas para llenar métricas. No convertir abstenciones en predicciones acertadas ni ocultar candidatos perdedores.
