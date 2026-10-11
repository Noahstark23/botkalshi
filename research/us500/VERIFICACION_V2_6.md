# US500 V2.6 — continuidad de captura pública

Revisión del 11 de octubre de 2026 sobre `a4bcf57f1ce484a5e554b2ace37eab5162ef54e9`.

## Cambio y motivo

La primera captura abierta de KXUS500PERP informó apertura el 11 de octubre a las
22:00 UTC y próximo cierre el 16 de octubre a las 21:00 UTC: 119 horas. Una ventana
acotada de 25 horas no cubre ese intervalo. El nuevo gate permite encadenar planes
contiguos y explícitos, conservando sin modificación el primer plan, su presupuesto
y su base de datos. El código de captura ya instalado no cambió.

`us500_chain_gate.py` sólo lee JSON acotado: manifiesto, plan externo y estados
previos. Rechaza solapamientos, huecos entre planes, presupuestos incoherentes,
planes distintos de los registrados, estados inciertos/cerrados, fallos de
integridad declarados, identidades de instrumento previas contradictorias y
permisos inseguros. No abre SQLite, no instancia el capturador y no realiza red.

La plantilla `deploy/botkalshi-us500-data@.service` ejecuta ese gate como condición
antes del capturador, con usuario propio, límites de recursos y rutas financieras
inaccesibles. No incorpora timers concretos ni los instala por sí misma: la
instalación debe fijar fechas y presupuestos. El gate rechaza el primer tramo para
evitar duplicar el capturador original. Ver `SESION_EN_TRAMOS.md`.

## Correcciones reproducidas

- Estado que declaraba una muestra USABLE pero cero GET intentados: rechazado por
  contabilidad imposible. Cada USABLE exige al menos dos consultas intentadas.
- Dos estados anteriores con hashes de instrumento distintos: continuidad
  bloqueada. Esto no garantiza la identidad de una respuesta futura.

Ambos fallos se reprodujeron con estados de WindowCapture generado con transportes
ficticios y se verificaron de nuevo tras corregirlos. No se usaron consultas reales
para producir esos casos.

## Límites

El gate comprueba la coherencia de una atestación previa `status.json`; no vuelve
a verificar los artefactos ni autentica su origen. La auditoría final debe revisar
hashes, fechas reales, huecos y datos. Una muestra por minuto no es una historia
continua ni una estrategia validada. El cierre anunciado puede cambiar; ningún
tramo extiende automáticamente el deadline.

La cadena propuesta suma 7.141 slots y 14.282 GET de captura, más un GET de
descubrimiento: máximo global 14.283 y cinco límites de disco de 256 MiB. El último
slot sondea el cierre anunciado. No hay otra sesión automática después del plazo.
Si un finalizador previo aún no publicó estado terminal al comenzar el siguiente
tramo, éste espera; cualquier hueco queda explícito.

El replay existente admite 4.096 frames. No unir 7.141 muestras ni reiniciar el
capital por tramo para simular continuidad: falta una evaluación por tramos con
contabilidad continua, costes/funding verificados y auditoría independiente.

El panel desplegado V2.5.1 permanece separado de esta revisión. Su contrato de
telemetría está conectado, pero los hooks financieros no están instalados; los
campos que no publica el runtime siguen desconocidos. La revisión V2.6 no cambia
servicios financieros, flags, Telegram, main ni M2/M5. El ejemplo sintético negativo
se conserva y no demuestra rentabilidad.

## Verificación

Resultados y hashes de esta revisión se registran en `MANIFIESTO_V2.json` y
`evidence/V2_6/`. La configuración CI ejecuta la suite con red bloqueada y dos
recorridos en Python 3.10–3.14, más Ruff 0.14.0 y formato. Consultar el run del SHA
publicado antes de afirmar su resultado remoto. Los datos e informes operativos
externos no forman parte de este manifiesto del código.

Resultado local: **437 pruebas aprobadas y dos recorridos**, sin red. Las 25
pruebas nuevas cubren el gate, incluyendo permisos y límites. Ruff 0.14.0 sin
hallazgos; 37 archivos Python con formato verificado.
