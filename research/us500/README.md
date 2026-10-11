# US500 V1 — motor financiero de simulación local

Entrega del 11 de octubre de 2026. Esta versión completa el circuito local: datos públicos → validación → hipótesis determinista → ejecución ficticia → contabilidad persistente → informe y replay tras reinicio. Está separado de M2/M5 y no importa BotKalshi, cuentas, configuración global ni secretos. No contiene un transporte de órdenes reales. `execution_available=false` es un hecho del código, no un flag que active trading.

## Ejecutarlo

Python 3.10 o posterior, biblioteca estándar. No hay dependencias de ejecución. Desde esta carpeta, usá una ruta nueva:

```sh
python3 -m unittest discover -s tests -v
python3 run_demo.py --directory mi-prueba-us500
```

El demo genera diez capturas sintéticas, procesa un prefijo, continúa con el resto, reinicia y comprueba igualdad completa. Conserva el input, reportes, SQLite y resumen. No sobrescribe carpetas existentes.

Para repetir una sesión archivada sobre su misma DB:

```sh
python3 us500_runner.py --input mi-prueba-us500/SESSION_SYNTHETIC.json --database mi-prueba-us500/paper.sqlite3 --output mi-prueba-us500/REPLAY_ADICIONAL.json
```

Los mismos eventos no vuelven a afectar cash/posición. Una sesión puede extenderse agregando frames; cambiar parámetros, truncar o alterar el prefijo se rechaza. No se admite reutilizar bases históricas ni enlaces simbólicos.

Captura puntual de ficha/libro públicos:

```sh
python3 us500_engine.py capture --environment public --output observacion-nueva.json
```

Históricos públicos, con tiempos epoch UTC en milisegundos elegidos explícitamente:

```sh
python3 us500_history.py capture-history --environment public --resource trades --start-ms 1791742829661 --end-ms 1791746429661 --max-pages 1 --output trades-nuevos.json
python3 us500_history.py verify --input trades-nuevos.json --output revision-historico.json
```

Ese rango corresponde al ensayo archivado, no a una solicitud automática del momento actual. Recursos: `trades`, `candles` (un minuto), `funding`. Demo usa su propio host/ticker. La captura consulta datos públicos; no accede a ninguna cuenta ni demuestra habilitación de Perps. Cada cliente tiene presupuesto de peticiones, sin reintentos. Timeout de socket no equivale a plazo total garantizado.

## Componentes

| Archivo | Función |
|---|---|
| `us500_engine.py` | Captura pública, normalización, aritmética de escenarios y procedencia |
| `us500_history.py` | Paginación/ventanas históricas, hashes crudos y canónicos, cobertura y huecos |
| `us500_ledger.py` | Posición firmada, PnL, costes, margen hipotético, reservas y SQLite transaccional |
| `us500_runner.py` | Regla congelada, ejecución ficticia en observación posterior, límites y replay durable |
| `example_session.py`, `run_demo.py` | Fixture sintético reproducible y ensayo de continuidad |
| `tests/` | Pruebas offline; no necesitan claves ni servicios externos |
| `CONTRATO_Y_LIMITES.md` | Fuentes oficiales, unidades y diferencias frente al exchange |
| `HISTORICAL_API.md` | Contrato de ingestión y significado de cobertura |
| `evidence/` | Logs, lectura pública puntual y resultados ficticios |

`INVESTIGACION_Y_PLAN.md` y `OBSERVACION_PUBLICA_2026-10-11.json` se conservaron desde v0.1: son documentos históricos. Este README y VERIFICACION_V1.md describen el estado nuevo. El paquete portable anterior tampoco se modifica.

## Regla y modelo de ejecución

`momentum-v1` compara el midpoint actual con el de `lookback-1` observaciones atrás. Por encima/debajo de un umbral explícito propone una posición larga/corta de cantidad configurada; dentro de la banda propone cero. No se optimizaron parámetros con los ejemplos. No es una recomendación de inversión ni una regla validada económicamente.

Una señal sólo puede usar liquidez de una captura solicitada después de la observación que la generó. Reusar una captura o superponer su intervalo bloquea la sesión. Una reversión primero cierra y sólo una observación posterior puede abrir el otro sentido. Spread, tick, paso de cantidad, datos, margen y nocional limitan cada propuesta. Las cantidades corresponden a unidades API; el nocional es cantidad por precio, sin multiplicar `contract_size` de nuevo.

Las ejecuciones hipotéticas recorren profundidad visible, agregan slippage adverso y redondean al tick. Si falta profundidad, el modelo produce fill parcial y cancela ficticiamente el resto (supuesto IOC local). No predice colas maker, latencia ni desaparición de liquidez. El ledger también permite representar UNKNOWN: pausa y conserva la reserva hasta un evento terminal explícito. No extrapolar la cancelación ficticia a una orden real desconocida.

El ledger cobra fees supuestas por fill, redondeadas hacia arriba; reserva suficiente para fragmentos mínimos. Cash = inicial + PnL realizado − fees + funding − infraestructura. Equity agrega PnL latente. Funding es obligatorio por intervalo, incluso si se asume cero; no se estima desde un promedio de primas ni se cobra si no hay posición. Un pago distinto de cero con posición cero se rechaza.

El mark de liquidación se valida separado del midpoint; puede estar fuera del tick negociable. Márgenes inicial/mantenimiento son porcentajes hipotéticos constantes de nocional, **no el cálculo dependiente del riesgo del exchange**. Ante breach, gap o UNKNOWN se pausa de forma durable y se informa exposición residual. No hay fill ficticio automático de liquidación ni reanudación automática. El cierre al final del archivo tampoco se fabrica. Los cambios de margen intrasesión y ciclos reales de variation margin no están implementados.

Cada replay primero construye el plan completo en memoria y después lo aplica con IDs idempotentes. La DB fija configuración y hashes del prefijo antes de contabilizar; un lock excluye escritores simultáneos del runner. La inicialización del manifiesto es atómica; las mutaciones del ledger son transacciones SQLite. Máximos: 4.096 frames, 100.000 eventos por DB, 8.192 órdenes totales y 20 páginas por captura histórica; llegar al límite no elimina historia ni libera reservas.

## Alcance de cierre

La versión V1 queda terminada **como motor local de simulación y evaluación**, con una regla reproducible y contabilidad de futuros. La lectura pública realizada devolvió mercado cerrado e históricos vacíos para la hora consultada. No existe aún una muestra de estrategia independiente ni una sesión observada completa para medir resultados económicos.

Antes de una integración real quedan por verificar: habilitación propia Perps, parámetros de cuenta, especificación contractual vigente, libro/marks/funding durante sesiones abiertas y evaluación fuera de muestra. El ejecutor real, WebSocket autenticado, conciliación de cuenta y liquidación del venue quedan fuera de esta entrega. No se presenta como motor listo para dinero real.

Jev es opcional para contrastar afirmaciones con documentos; no participa en señales, cálculos ni ejecución. Se aplicó la skill TypeSafe al diseño, pero no se hicieron llamadas al servicio ni se implementó un adaptador Jev en este paquete. The Odds API no aporta datos para este futuro financiero.

La rama `codex/live-readiness-20261006` en `c1431286…` contiene el candidato anterior M2/M5. **US500 V1 se entrega en ZIP aparte; todavía no está en esa rama.** No hubo otro push, PR, merge, despliegue, acceso a producción ni consumo de cuotas Odds/Jev durante esta implementación.
