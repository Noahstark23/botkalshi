# US500 V2.6 — continuidad de datos y telemetría explícita

V2.6 añade un gate de continuidad de sólo lectura para encadenar ventanas con
planes contiguos y un presupuesto global explícito. No inicia de nuevo un tramo
terminal ni acredita una sesión completa. Ver `SESION_EN_TRAMOS.md` y
`VERIFICACION_V2_6.md`. La captura y Telegram se despliegan por separado; los
SHAs instalados se conservan en el informe operativo.

La revisión V2.5 añadió `us500_window_capture.py`: una ventana inmutable de hasta
26 horas, un intento por minuto, presupuesto durable, recuperación sin repetir
slots y evidencia con hashes. Su servicio aislado no recibe credenciales ni envía
órdenes. Ver `SESION_COMPLETA.md` y `deploy/US500_DATA.md`. Instalar un recolector
no equivale a haber obtenido una sesión completa; se verifica después con horarios,
huecos y validez de las observaciones.

Telegram tiene lectura de estados y conteos de señales, sin activación real.
El nuevo contrato de telemetría por componente está preparado y su parser conectado
al panel; los hooks financieros **no están instalados**. Sin el bloque opcional
`motor_telemetry` siguen desconocidos los datos que el runtime no publica.
Ver `TELEMETRIA_PENDIENTE.md`. Los apartados V2.2/V2.3 siguientes son históricos.

Candidato de revisión del 11 de octubre de 2026. Circuito implementado: capturas públicas → validación → hipótesis momentum congelada → ejecución ficticia con una captura posterior → ledger persistente → informe y reinicio. Incluye controles de Telegram **para US500 paper**. No contiene transporte de órdenes reales, credenciales Kalshi, integración con el bot deportivo ni cambios de producción.

V2.2 amplía `/motores` con M1, M2, M3, M5, M6, REST, M8 y M9. Sus adaptadores están desconectados y los botones lo declaran; sólo US500 tiene permiso de replay operativo. Ver `PANEL_TODOS_MOTORES.md`. El catálogo no acredita que esos procesos estén encendidos o apagados.

V2.2.1 conserva el código seguro de fallo del poll en el supervisor. El seguimiento encontró detenido el receptor local; no se reanudó en ese seguimiento. El menú visible no acredita disponibilidad del proceso. Ver `DIAGNOSTICO_TELEGRAM.md`.

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
| `bot_motor_panel.py`, `PANEL_TODOS_MOTORES.md` | Catálogo completo, controles sin conexión identificados y contrato pendiente |
| `us500_telegram.py`, `us500_telegram_service.py`, `TELEGRAM_PAPER.md` | Transporte, cursor/outbox, supervisor local acotado y pausa al parar/fallar |
| `run_demo.py`, `run_control_demo.py` | Recorridos offline reproducibles |
| `VERIFICACION_V2.md`, `VERIFICACION_V2_1.md` | Evidencia histórica y correcciones de la revisión actual |

## Modelo y continuidad

`momentum-v1` compara midpoints separados por `lookback-1` observaciones. No se optimizó con los ejemplos. Una señal sólo puede usar un libro solicitado después de la observación que la generó. Reversión: cerrar primero; abrir el sentido contrario requiere otra observación. Spread, paso, tick, datos, nocional y margen limitan cada propuesta. Una observación con spread superior al máximo fija target=0: puede cerrar exposición en la captura posterior y provocar una reapertura posterior cuando vuelva a cumplir el umbral; no es una simple espera.

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

Quedan abiertos: permisos Perps de la cuenta, condiciones contractuales y económicas aplicables, datos completos de sesiones abiertas, evaluación independiente, conciliación de cuenta y ejecutor real. El menú se conectó y verificó en Telegram mediante un receptor local aislado; el alcance y la duración se registran en VERIFICACION_V2_1.md. El receptor no inicia una estrategia ni obtiene datos de mercado. No hay evidencia de rentabilidad ni declaración de preparación para dinero real.

Jev no decide señales ni mueve dinero; no se consumió Jev ni Odds durante V2. M2/M5 siguen en su rama separada y fuera de esta integración. Los documentos y logs V1 se conservan como históricos, no como prueba de la nueva revisión.


## Actualización V2.3 — receptor cloud y estados

La nueva instalación aislada está documentada en `TELEGRAM_CLOUD.md`. Permite operación
continua del receptor sin Mac, estados de sólo lectura del runtime y permiso paper
US500. Las descripciones anteriores del panel desconectado/local son históricas.
No incorpora activación real ni ejecutor automático. M6/M8/M9/REST carecen de señal
específica en el API actual; el panel lo declara como desconocido.

## Actualización V2.4 — trazabilidad y actividad registrada

Ver `VERIFICACION_V2_4.md` y `EVALUACION_INDEPENDIENTE.md`. El replay directo ya
contrasta identidad contractual completa y los importes con la evidencia del
ensamblador. El reporte separa procedencia declarada y cobertura; no certifica
autenticidad ni independencia. Telegram incorpora conteos de señales de
M6/REST/M8/M9 mediante el endpoint existente, con ventana UTC y caché. Sus tareas
y flags no expuestos siguen desconocidos. No se añadió activación real.
