# US500 V2 — simulación, sesiones y control local

Candidato de revisión del 11 de octubre de 2026. Circuito implementado: capturas públicas → validación → hipótesis momentum congelada → ejecución ficticia con una captura posterior → ledger persistente → informe y reinicio. Incluye controles de Telegram **para US500 paper**. No contiene transporte de órdenes reales, credenciales Kalshi, integración con el bot deportivo ni cambios de producción.

La rama conserva su nombre `codex/us500-paper-v1-20261011`; V2 identifica esta revisión del módulo, no un despliegue. La base V1 `0fd3bf7` y sus evidencias históricas permanecen accesibles en Git. `MANIFIESTO_V2.json` verifica los archivos actuales; `MANIFIESTO_V1.json` describe exclusivamente la entrega anterior.

## Verificación reproducible

Python 3.10–3.14, biblioteca estándar. Desde `research/us500`:

```sh
python3 verify_package.py
python3 verify_offline.py
python3 run_demo.py --directory /tmp/us500-v2-demo-nuevo
python3 run_control_demo.py --directory /tmp/us500-v2-botones-nuevo
```

Elegí directorios nuevos: no se sobrescriben. `verify_offline.py` ejecuta toda la suite y ambos recorridos con DNS y conexiones/envíos de sockets bloqueados. El workflow `.github/workflows/us500-paper.yml` incluye el módulo explícitamente en CI; no instala el bot, consume secretos, construye despliegues ni envía órdenes.

El recorrido original continúa dando USD100 → USD98.6643 bajo sus supuestos, sin posición ni reservas al terminar. El recorrido de botones prueba activación confirmada → posición ficticia → pausa → bloqueo de continuación → reactivación confirmada → cierre ficticio/reinicio → pausa final. No contacta Telegram.

## Componentes

| Archivo | Función |
|---|---|
| `us500_engine.py` | Ficha/libro públicos, unidades, hashes, validación y escenarios |
| `us500_history.py` | Trades, velas y funding paginados; cobertura e integridad |
| `us500_session.py`, `SESIONES.md` | Ensamble de capturas/costes y colector con presupuesto durable |
| `us500_ledger.py` | Cash, coste acumulado, posición, PnL, fees, reservas y margen hipotético |
| `us500_runner.py` | Momentum congelado, ejecución ficticia posterior, replay y permiso de control |
| `us500_control.py` | Dueño/chat exactos, confirmación única, pausa persistente y permisos por evento |
| `us500_telegram.py`, `TELEGRAM_PAPER.md` | Transporte explícito, cursor/outbox, respuestas ambiguas y operación del menú |
| `run_demo.py`, `run_control_demo.py` | Recorridos offline reproducibles |
| `VERIFICACION_V2.md` | Qué se verificó y qué queda abierto |

## Modelo y continuidad

`momentum-v1` compara midpoints separados por `lookback-1` observaciones. No se optimizó con los ejemplos. Una señal sólo puede usar un libro solicitado después de la observación que la generó. Reversión: cerrar primero; abrir el sentido contrario requiere otra observación. Spread, paso, tick, datos, nocional y margen limitan cada propuesta.

Los fills hipotéticos recorren profundidad visible, aplican slippage adverso y redondean al tick. El remanente se cancela bajo un supuesto IOC local. No se reproducen cola, latencia, liquidez futura ni resultado de una orden desconocida del exchange. UNKNOWN en el ledger pausa y conserva reservas hasta un evento terminal explícito.

El nocional es cantidad API × precio, sin multiplicar otra vez el tamaño del contrato. El coste agregado de posición es la base contable; el precio promedio es informativo. Así, cerrar posiciones con promedios periódicos no usa un promedio redondeado como fuente del PnL. Cash = inicial + realizado − fees + funding − costes; equity agrega PnL latente.

Cada frame requiere funding y coste de infraestructura explícitos. Se admiten dos campos opcionales:

```json
{
  "variation_margin_settlement_price": "13.05",
  "margin_rates": {
    "initial_margin_rate": "0.90",
    "maintenance_margin_rate": "0.45"
  }
}
```

Estos campos se agregan al frame que ya contiene captura, tiempo y costes. Orden: mark → variation margin opcional → tasas opcionales → funding → costes → eventual ejecución ficticia. Variation margin realiza el PnL y rebasa el coste, conserva equity al mismo mark y no refresca su antigüedad. `variation_margin_usd` está **incluido** en `realized_pnl_usd`; no se suma dos veces. Tasas: `0 < mantenimiento <= inicial <= 1`. Un aumento revalúa reservas; una bajada no libera reservas abiertas/UNKNOWN. Todos son eventos idempotentes con parámetros de escenario; no constituyen el modelo de liquidación Kalshi ni condiciones verificadas de cuenta.

Ante breach, gap o UNKNOWN queda una pausa durable, sin fill inventado de liquidación ni cierre automático al final del archivo. Habilitar paper por Telegram no borra esas pausas. La pausa operativa detiene eventos futuros; conserva posición y reservas existentes.

La DB fija configuración, versión del modelo y hashes del prefijo. Puede extenderse una sesión, sin alterar su historia. Las bases del runner V1 se rechazan antes de escribir: V2 necesita una DB nueva y no migra implícitamente contabilidad histórica. No descartar una base con exposición residual; conservarla y revisar su conciliación con la versión que la creó. Los mismos eventos V2 se reejecutan sin duplicar dinero. Una base vinculada a un control exige ese mismo control incluso al invocar el CLI directamente.

Límites: 4.096 frames, 100.000 eventos, 8.192 órdenes ficticias por ledger; no se borra historia para volver a habilitar capacidad. El colector público reserva GET antes de enviarlos y conserva el presupuesto al reiniciar; un cierre del mercado detiene la captura antes de consultar el libro.

## Estado y pendientes externos

V2 completa las piezas locales anteriores y su integración offline. La consulta pública nueva devolvió HTTP200 y mercado cerrado; se registró 1 GET y pausa. No aporta una sesión abierta ni resultados prospectivos.

Quedan abiertos: permisos Perps de la cuenta, condiciones contractuales y económicas aplicables, datos completos de sesiones abiertas, evaluación independiente, conciliación de cuenta y ejecutor real. La comunicación real del menú tampoco se ha instalado o comprobado en Telegram. No hay evidencia de rentabilidad ni declaración de preparación para dinero real.

Jev no decide señales ni mueve dinero; no se consumió Jev ni Odds durante V2. M2/M5 siguen en su rama separada y fuera de esta integración. Los documentos y logs V1 se conservan como históricos, no como prueba de la nueva revisión.
