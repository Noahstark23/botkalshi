# M5 Demo: validación de tarifas sin órdenes

En el candidato readiness, el modo de tarifas requiere conjuntamente
`demo_readonly_validation=True` y `demo_football_fee_validation=True`. Este
segundo parámetro es optativo, falso por defecto y no está conectado a variables
de entorno ni al runner. Exige `trading_enabled=False`, entorno `demo`, modelo
maker, tamaño 1, una única serie revisada (NFL o NCAAF) y un cliente de lectura
explícito. Los replays utilizan objetos locales sin red ni métodos de órdenes
y DB temporales.

El modo `demo_readonly_validation=True` anterior conserva su cohorte v1 y
política de tarifas, usada por el harness histórico MLB/NFL. Sólo la combinación
de ambos parámetros selecciona la cohorte v2 descrita aquí. El modelo no
certifica reglas equivalentes: los informes del harness declaran por separado
`payoff_equivalence_verified=False` y `strategy_readiness=False` y el validador
del proceso padre rechaza un worker que altere esos valores.

## Interpretación de la tarifa

Las capturas Demo del 10-oct-2026 declaran `quadratic`, multiplicador 1, en
`KXNFLGAME` y `KXNCAAFGAME`. El [schema oficial de Series](https://docs.kalshi.com/api-reference/market/get-series)
distingue `quadratic` de `quadratic_with_maker_fees`. La [tabla oficial](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
separa tarifas generales y maker. Las excepciones de producción no se copian a Demo.

La observación conserva `multiplier=1` como dato fuente; `maker_multiplier=0` es
el multiplicador efectivo maker para `quadratic`. Un override completo del evento
puede seleccionar `quadratic_with_maker_fees`, caso en que se usa su multiplicador.
Tipos desconocidos, serie no revisada, identidad contradictoria, base distinta,
override parcial o multiplicador inválido siguen rechazándose. Un cambio de tarifa
mientras una quote hipotética está expuesta invalida el intervalo antes de inferir
un fill. El modo default conserva sus aserciones previas y rechazos de Demo.

El quoter, inventario y fee efectiva persistida usan el multiplicador maker;
la columna de comparación taker y el multiplicador fuente se conservan. Un reinicio
reconstruye el mismo inventario y rechaza fees/procedencia contradictorias. Los
markouts de otras cohortes no se recuperan como si fueran de este ensayo.

Los cálculos con comisión positiva mantienen el modelo conservador en centavos
del simulador. **No equivalen al cálculo de comisión realmente cobrada** con
precisión subcentavo y acumulador de rebates descrito en la
[documentación de redondeo](https://docs.kalshi.com/getting_started/fee_rounding).
El caso maker gratuito probado usa precios en centavos y cantidad entera: no
genera redondeo de ingresos. Precios o cantidades subcentavo/fraccionales requieren
otro ensayo; no se declaran validados por éste.

## Evidencia y límites

La métrica separada `demo-readonly-v2-fee-schedule` y un ID de experimento distinto
evitan promover estos resultados como F1 de producción. Los tests usan fair y
movimiento de libro sintéticos, con cero red, para comprobar:

- admisión de metadata de ambas series y overrides;
- arranque sin ejecutor, flags incompatibles y default operativo sin cambios;
- fills hipotéticos de compra/venta, comisiones durables y reinicio;
- cambios de tarifa durante exposición y corrupción del historial;
- reserva previa en el banco compartido, fill parcial de quote bilateral,
  cancelación del remanente y liquidación/repetición sin duplicar fondos.

El replay externo verifica los hashes del archivo capturado y aplica la política
actual a los tres eventos archivados. Es una prueba de compatibilidad del código,
no una nueva lectura de tarifas ni prueba de fills en el exchange. M2 continúa sin
fair certificado por diferencias de reglas; el modo nuevo no certifica ese fair ni
demuestra edge. No se conectó el feed Odds al motor: sólo se le suministró fair
sintético en tests. El IOC histórico permanece pendiente de evidencia autoritativa.
