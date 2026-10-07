# Preparación verificable para un piloto

Meta solicitada el 6 de octubre de 2026. Este documento no autoriza órdenes reales.
El titular confirmó residencia en EE. UU. y cuenta verificada; avisará si deja EE. UU.
No se recibió ni se necesita su número de seguro social.

## Estado de los cuatro puntos

| Punto | Evidencia de cierre exigida | Estado |
|---|---|---|
| Ejecutor y riesgo | Reproducciones corregidas; suite completa, revisión independiente y versión identificada | Candidato aislado verificado: 2.785 pruebas pasan, Ruff limpio; aún no desplegado |
| Integración demo | API de demo: create/cancel/reinicio/conciliación; fills parciales y fallos inyectados; ninguna orden desconocida | Credenciales recibidas, pero autenticación rechazada; prueba acotada preparada |
| Ventaja neta | Datos prospectivos, costes y ejecución verificables, muestra distinguida de fotos repetidas | No demostrada: cero operaciones simuladas; REST observado con margen negativo |
| Cuenta y límites | Lectura completa, permisos, capital disponible y límites concretos separados de fondos simulados | Lectura OK, US$0,4962; permisos read/write; propuesta concreta preparada, sin financiación ni flags aplicados |

Los puntos no se convierten a aprobado por cumplir horas, pasar tests o añadir Jev.
La ausencia de oportunidad puede ser el resultado correcto del estudio; no se cambia
el umbral para producir operaciones. No hay un pronóstico de rentabilidad.

## Cuenta: lectura del 7 de octubre, 05:29 UTC

Entorno production, subcuenta 0, ventana de movimientos recientes de 24 horas.
Balance US$0,4962, portfolio reportado cero, posiciones no cero 0, resting orders 0.
Paginaciones completas de posiciones/órdenes/fills/settlements, sin errores.
Consulta separada GET /api_keys: clave actual encontrada, scopes read y write;
sin restricción de subcuenta informada. No se probó ningún permiso enviando órdenes.
La clave privada actual no coincide con el blob privado encontrado en el historial
Git local examinado. Esto no certifica que no existan otras copias o filtraciones.

Evidencia privada conservada fuera del repo en el paquete de trabajo:
`CUENTA_READONLY_2026-10-06.json`, `PERMISOS_CUENTA_2026-10-06.json`.

## Medición prospectiva observada

Corte 05:31 UTC: REST, 15 ciclos desde 05:17, seis eventos distintos, 90 fotos evaluadas.
Margen después de comisiones: entre −9 y −7 centavos por conjunto. Cero propuestas.
Esas 90 fotos no son 90 oportunidades independientes. M1 último ciclo: 30 libros, todos
sin arbitraje. M5: 64 ciclos desde 04:29, ninguna propuesta; bloqueos por fair ausente,
fair vencido y precisión de libros no admitida. No constituye evidencia de rendimiento
de M5, porque no se llegó a ejecutar su hipótesis.

Banco único simulado US$200, reservas cero, riesgo cero, resultado realizado cero,
ningún evento FILL/SETTLEMENT. La integridad del banco resultó OK.
`MEDICION_PREPARACION_2026-10-06.json` conserva recuentos y horas.

Segundo corte 05:58 UTC: REST 42 ciclos, los mismos seis eventos, 252 evaluaciones
sin margen (−9 a −7 centavos), cero propuestas. M5 91 ciclos, cero evaluaciones
elegibles y propuestas. M1: 30 libros, todos sin arbitraje. Banco simulado US$200,
riesgo/reservas/PnL cero. El informe `MEDICION_PREPARACION_CORTE2_2026-10-06.json`
incluye los bloqueos acumulados; algunos preceden la versión nueva del simulador.

Investigación actualizada a `df502844dcabc5cd3c8f0f1a53037798190f7372` a las 05:46 UTC:
la profundidad fraccionaria de M5 se conserva exacta y se redondea hacia abajo sólo
al determinar cuántos contratos enteros admite una simulación. Profundidad 0,75 no
puede inventar un fill de 1. Precios subcentavo siguen bloqueados en esa ruta legacy.
Pasaron 595 pruebas tanto en Mac como en Ubuntu. A las 05:49 los ciclos M5/REST seguían
OK, cero reinicios, reservas y resultados cero. Runtime técnico sin cambio de PID;
los siete flags de órdenes continuaron false. Configuración y cuota de Odds intactas.
La cuota observada posteriormente era 3 créditos reservados de un presupuesto de 10.

El cambio de huella de los libros crea un corte de versión: reproducir capturas
antiguas puede producir `CONTRADICTORY_REPLAY`. No se reescriben los diarios anteriores.

## Demo: separación y prueba acotada

La [demo oficial](https://docs.kalshi.com/getting_started/demo_env) usa credenciales y
fondos ficticios separados. Tras indicar que aún no tenía cuenta, el titular guardó
credenciales y confirmó que identificador y archivo pertenecían a una misma clave demo.
Puede seguir la [guía de creación y fondos de prueba](https://help.kalshi.com/en/articles/13823775-creating-and-using-a-demo-account).
El formulario `conectar_kalshi_demo_local.py`, fuera del repo, guarda un identificador
y un archivo RSA privado en `~/.config/botkalshi/demo` con permisos 0600.
La versión actual valida primero mediante un único GET de saldo al host demo fijo;
no envía órdenes ni guarda parejas rechazadas. La pareja inicial quedó preservada
en una carpeta privada fuera del repo; no se revocó ninguna clave.

Lectura y réplica independiente del ejemplo oficial: HTTP 401 con
`INCORRECT_API_KEY_SIGNATURE` en ambos hosts oficiales de demo. Ruta firmada y real
coinciden, reloj a menos de un segundo del servidor, firma RSA-PSS/SHA-256 verificada
localmente. Esto valida la construcción criptográfica, no la asociación de la clave
pública con el identificador en Kalshi. Se solicitó una pareja nueva mediante el
formulario local. Evidencias: `DEMO_LECTURA_2026-10-06.json` y
`DEMO_FIRMA_DIAGNOSTICO_2026-10-06.json`. Ninguna orden demo enviada todavía.

`scripts/demo_execution_probe.py` fija el host oficial demo, ignora .env/proxies,
permite como máximo una intención post-only de un contrato a 1 centavo y cancela
sólo la orden identificada como propia. Persiste intención antes de red, requiere
`--recover` para conciliar un intento anterior y jamás infiere cancelación por ausencia.
Un cliente nuevo verifica el estado después del cancel. Informa cualquier fill residual.

Sin `--execute` únicamente consulta; con `--recover` no crea órdenes nuevas.
Sus tests con clientes falsos prueban guards, estado desconocido y fill parcial 0,25;
eso no se presenta como una ejecución observada en Kalshi Demo ni como integración
del ProductionRunner. El circuito completo del runner sigue siendo un gate separado.

58 pruebas offline cubren además el punto de entrada real con RSA temporal,
cliente/signer heredados y transporte HTTP simulado: respuesta plana Create V2,
consulta paginada, cancel, nueva conexión y recuperación sin segundo POST. GET exige
identidad completa y cantidad inicial de 1; datos contradictorios bloquean el cierre.

## Correcciones del candidato y sus límites

Verificación completa final en Mac: 2.785 pruebas pasan en 71,44 segundos, con
37 warnings (deprecaciones, fork de pruebas multiproceso y advertencia de coroutine
en una prueba de book). Ruff check/format sobre 323 archivos y git diff --check,
sin errores. Ejecución con entorno vacío, .env deshabilitado, SQLite temporal y
conexiones externas bloqueadas. Evidencia: `PREPARACION_SUITE_FINAL_2026-10-06.log`
y `.xml`. Esta suite no es una prueba de órdenes demo ni un despliegue del ejecutor.
El runtime del servidor conserva su versión anterior y todos sus flags de órdenes apagados.

M5 retira cotizaciones cuando el cash deja de ser válido, conserva la reserva hasta
confirmar cancelación y refresca marcas de posiciones fuera del universo actual.
La pérdida latente incorpora fees/costes observados y cantidades fraccionarias exactas;
cuando `UNREALIZED_STOP_ENABLED` está activado, marcas, fees o cantidades
ausentes/contradictorias impiden entradas nuevas. El flag sigue apagado por defecto.
Una marca BBO permite valoración, pero no demuestra profundidad suficiente para liquidar.

M1/M2/M3/REST mantienen cantidades exactas, reserva completa ante respuestas no terminales
y piso de fill monotónico. Las ventas de reducción se reservan atómicamente y sus
reintentos no vuelven a partir una posición. Costes FP se reparten conservando microUSD;
el cierre final consume el fee observado completo. La recuperación del arranque hace
GET y actualiza contabilidad sin construir un executor fuera de sus flags.

Los datos legacy filled/settled sin campo FP mantienen compatibilidad histórica;
filas nuevas con cantidades FP nunca deducen un fill completo sólo del status.
La conciliación al arranque consulta una página de órdenes: cualquier intent no
encontrado queda sin resolver y pausa; no se declara cancelado por ausencia.

La exactitud del MTM no convierte toda la cadena en un extracto de ganancias reales.
Algunas rutas legacy de settlement y telemetría todavía no consumen las columnas FP:
usan precio límite/promedio entero y fees redondeadas hacia arriba o calculadas incluso
cuando existen costes FP observados. Los agregados y stops realizados usan centavos
enteros. El settlement común de M5 conserva esa limitación. Las cifras de rendimiento deben
contrastarse con fills/fees y saldo del exchange, distinguiendo estimaciones.

La revisión adicional encontró que el presupuesto diario podía reponerse al reducir
una fila y que el cap por evento no sumaba la nueva propuesta. Se corrigieron con
compromisos inmutables por intent y reserva atómica de M1. El cap combina exposición
del ledger y portfolio por identidad, sumando identidades distintas; no toma el
máximo global entre fuentes que podrían contener posiciones diferentes.
La tabla de compromisos se conserva al reconstruir SQLite; splits mantienen el origen
sin generar otro consumo ni reponerlo. Datos legacy posteriores al inicio sin evidencia
original bloquean el experimento. 19 pruebas nuevas cubren estos controles, tres con
procesos concurrentes. Revisión independiente: 64 pruebas pasan. Los controles
negativos restauraron los defectos sólo en memoria y detectaron 4+1+2 fallos esperados.

## Propuesta concreta de límites del primer piloto

El banco simulado utiliza US$200, US$1 habitual por conjunto, US$2 por evento y US$6
de exposición simultánea/riesgo nuevo diario. Son reglas del experimento, no fondos
ni instrucciones de depósito. El límite de riesgo nuevo diario tampoco es un stop
de pérdida realizada. Antes de un piloto del titular se deben definir por separado
capital financiado, pérdida máxima, exposición, tamaño, universo y condición de parada.
El saldo actual no soporta tratar US$200 como capital disponible.

Propuesta para revisión, sujeta a cerrar demo y evidencia; no se aplicó configuración
ni se solicitó depositar fondos:

| Control | Propuesta compatible con el perfil M1 |
|---|---|
| Capital de referencia | US$200, pendiente de decisión y financiación del titular |
| Capital efectivo inicial con cash de US$200 | US$180 por factor de seguridad 90% |
| Tamaño máximo por operación | Hasta US$1,80 inicialmente; disminuye con el capital efectivo |
| Exposición simultánea | Hasta US$5,40 inicialmente, contando reservas y fees |
| Riesgo nuevo diario | Hasta US$5,40 inicialmente; cancelación/split no devuelven presupuesto |
| Cap absoluto por evento | US$2, contando la propuesta nueva y exposición existente |
| Freno de pérdida semanal realizada | US$12; pausa persistente |
| Freno acumulado del experimento | US$20; pausa persistente |
| Entrada ante datos desconocidos | Rechazada; conciliación y gestión de salidas permanecen separadas |
| Motores con entradas | Sólo M1; M2/REST/M5 continúan en investigación |

Los stops se verifican al admitir riesgo nuevo; no son órdenes de liquidación ni
garantías de pérdida máxima al centavo. El PnL realizado conserva las limitaciones
contables legacy descritas arriba. El tamaño habitual calculado por policy sería
US$0,90, pero ese campo por sí solo no es un límite obligatorio del ejecutor.

Los stops generales a ese capital serían US$20 diario, US$40 semanal y US$60 mensual;
el perfil añade los frenos semanales/acumulados más estrictos descritos en la tabla.
Rolling y MTM permanecen desactivados por defecto. Su implementación corregida no
significa que estén configurados en el servidor ni que los demás motores hereden
el perfil M1. No se modificó ningún flag para presentar esta propuesta.

El perfil existente `EXPERIMENT_BANK200_ENABLED` es exclusivamente M1: no habilita
REST/M5. Con cash confirmado US$200 y factor de seguridad 90%, el capital efectivo
es US$180, el techo por orden US$1,80 y 3% equivale a US$5,40. No son US$2/US$6 reales
sin cambiar ese factor. La caída de cash por debajo de US$200 ya impide entradas por
el piso efectivo US$180. Esta sensibilidad debe medirse en demo antes de recomendar
un perfil financiado; no se cambia el factor ni se pide un depósito para eludirla.

Si el titular avisa que dejará EE. UU., la parada debe impedir nuevas entradas,
conciliar órdenes pendientes y mostrar posiciones residuales; detener un proceso no
liquida contratos existentes. Las operaciones financieras reales corresponden al titular.
