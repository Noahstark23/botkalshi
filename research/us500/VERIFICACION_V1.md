# Verificación US500 V1 — 2026-10-11

## Ingeniería local

- 124 pruebas unittest aprobadas con Python 3.14.8. `evidence/TESTS.log` conserva la ejecución integrada final.
- Ruff 0.14.0: cero hallazgos; formato comprobado. Los logs finales se guardan junto a las pruebas.
- Core público/escenarios: 48 tests anteriores conservados, más regresión de granularidad API en runner.
- Ledger: 22 pruebas de long/short, cierre/reversal/parciales, reservas, UNKNOWN, funding, redondeos, riesgo, límites, transacciones y reinicio.
- Historia: 31 pruebas de endpoints/rangos, cursor, ventanas, datos vacíos, inconsistencias, hash y límites.
- Runner: 23 pruebas de señales sin datos futuros, ejecución posterior, costes, persistencia y entrada corrupta. Incluye fallo inyectado entre commits, inicialización interrumpida, replay idempotente, prefijo modificado, DB ajena intacta, reuso de captura y riesgo en el último fill.
- La revisión independiente identificó problemas de granularidad, marks fuera del tick, funding sin posición y captura reutilizada. Todos tienen corrección y prueba.

## Ejemplo sintético de extremo a extremo

`run_demo.py` conserva su ejecución en `evidence/OFFLINE_DEMO/`:

| Concepto | Resultado |
|---|---:|
| Capital ficticio inicial | USD100 |
| Cash ficticio final | USD98.6643 |
| PnL neto ficticio | **−USD1.3357** |
| Fees supuestas | USD0.1893 |
| Funding asumido | USD0 |
| Infraestructura supuesta | USD0.010 |
| Eventos únicos | 42 |
| Posición residual | 0 |
| Reserva residual | 0 |
| Replay después de reinicio | Idéntico |

El resultado negativo se conserva. No es un backtest de cotizaciones observadas ni mide rentabilidad de la estrategia. Los parámetros son fixtures para verificar software, no límites de inversión recomendados.

## Observación pública conectada

Se hicieron cinco GET anónimos de datos públicos: ficha, libro, trades, velas y funding. Sin credenciales, endpoints de cuenta, órdenes ni reintentos. Rango histórico: una hora hasta 2026-10-11T19:20:29.661Z. `evidence/PUBLIC_CAPTURE_SUMMARY.json` conserva el detalle.

- Ficha/libro: respuestas recibidas; análisis `INCOMPLETE_OR_UNUSABLE / MARKET_CLOSED`.
- Trades/velas/funding: una página completa por recurso, **cero registros** en el rango.
- Ninguna captura se convirtió en señal o fill. `history_sufficient=false`; muestras independientes liquidadas: **0**.
- No se infiere que no exista historia en otros rangos. Las velas vacías no se completaron artificialmente.
- Estos recursos históricos no contienen un libro y mark contemporáneos suficientes para fabricar los frames del runner.

## Entrega y límites

Código y pruebas locales completos para la versión de simulación definida en README. No se ejecutó CI remoto, no se verificó una cuenta Perps ni se certificó el modelo económico de Kalshi. No se activaron servicios ni se modificó la rama M2/M5 publicada. La suite de 5.023 tests del candidato M2/M5 es evidencia histórica distinta y no se suma a estas 124 pruebas.

`MANIFIESTO_V1.json` identifica todos los archivos de entrega mediante SHA256; `verify_package.py` verifica el contenido sin cargar código de trading ni usar red. El manifiesto verifica integridad local, no autenticidad del exchange ni rentabilidad.
