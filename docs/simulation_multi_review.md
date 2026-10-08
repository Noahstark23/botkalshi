# Validación conjunta en simulación

Esta ampliación es un instrumento de investigación local. El perfil
`EXPERIMENT_DYNAMIC_MULTI_ENABLED` está apagado por defecto y su validación rechaza
`TRADING_ENABLED` y todos los flags de ejecución. No incluye instalación remota,
servicio autónomo, instrucciones de activación ni autorización de órdenes reales.
Los perfiles anteriores se conservan y deben evaluarse por separado.

## Qué se comprueba

- M1, M2 y M5 comparten reservas durables en la base temporal de las pruebas.
  La reserva original se conserva tras cancelación, ejecución parcial y reinicio;
  no se recupera presupuesto diario por cancelar una intención.
- La admisión serializada en SQLite comprueba los límites globales, la atribución
  de inventario a un motor, las pausas y las comisiones observadas. Las pruebas
  usan dinero ficticio y transportes simulados, incluyendo carreras entre procesos.
- Las valoraciones de posiciones exigen identidad, cotización bilateral y tarifa
  recientes. Los datos ausentes o contradictorios invalidan la valoración previa.
- Una cuota compartida conserva las fechas del proveedor. Un archivo inexistente,
  inválido o vencido no provoca consultas adicionales ni un reemplazo con datos
  ficticios. La configuración no admite simultáneamente snapshot y clave Odds.
- El exportador de datos públicos conserva una lista cerrada de campos; no copia
  cuentas, claves, diarios de capital ni metadatos arbitrarios. No instala servicios.

El alcance del perfil experimental es MLB. Otros deportes, salidas anticipadas
M2, motores adicionales y funcionamiento conectado autónomo quedan fuera de esta
validación. La caducidad de los datos sigue aplicándose durante cualquier intervalo
sin actualización; no se prolonga para obtener más actividad simulada.

## Jev

[Jev](jev_market_review.md) es una herramienta separada para revisar semántica.
No participa en decisiones de órdenes, tamaños, capital, precios ni rentabilidad.
Pregunta por conflicto y correspondencia de forma independiente; una respuesta
incierta, datos faltantes o fallos del proveedor producen `REVIEW`.

El journal confirma la reserva de cada llamada antes de enviarla. El mismo estado
reutiliza su resultado, y un intento desconocido o fallido no se reenvía. Las claves
permanecen fuera del repositorio. Una clasificación tipada no autentica la fuente
ni demuestra la verdad del contenido.

## Límites de la evidencia

Las pruebas verifican comportamiento del código bajo escenarios concretos. No
demuestran fills externos, conciliación de una cuenta real, equivalencia completa
de reglas de proveedores ni ventaja económica. Tampoco resuelven retrospectivamente
incidentes Demo registrados antes de esta ampliación.

El informe externo `MULTI_JEV_CIERRE_2026-10-08.md` conserva SHA, suite integrada,
consultas reales a TypeSafe, coste estimado y los casos que quedaron en revisión.
No debe describirse este resultado como una activación de apuestas con dinero real.
