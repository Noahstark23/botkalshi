# Validación conjunta M1/M2/M5 en Demo, sólo lectura

`scripts/demo_multi_readiness.py` arranca el `ProductionRunner` del candidato
en un proceso aislado, con SQLite temporal. Lee exclusivamente credenciales
Demo de `~/.config/botkalshi/demo`. No carga `.env`, claves de producción,
The Odds API ni Jev. Sin `--run` imprime el plan sin abrir red ni leer claves.

El modo `MOTOR_MM_DEMO_READONLY_ENABLED` es optativo y falso por defecto.
Exige entorno Demo, trading apagado y todos los flags de ejecución apagados;
el resto de los controles F1 permanece obligatorio. M5 etiqueta sus datos como
`demo-readonly-v1-bbo-depth`, con cohorte distinta de producción.

El worker admite sólo GET del host REST oficial de Demo y suscripciones del
host WebSocket oficial de Demo. Bloquea constructores de ejecutores y productores
Odds/Jev. Cada corrida dura 60–90 segundos, con máximo 60 HTTP, tres conexiones
WS, 30 comandos WS, 5.000 frames y 20 mercados. El padre tiene timeout adicional
y verifica un esquema cerrado del informe antes de guardarlo.

Los datos de Odds provienen de un directorio local explícito. Se copia una sola
versión de `odds-latest.json` (MLB) o `odds-americanfootball_nfl-latest.json` (NFL),
con sus bytes y fechas originales, a la carpeta
temporal del worker. La frescura máxima del archivo y de sus libros es 600s;
el fair de M5 mantiene TTL de 360s. Un archivo copiado recientemente puede estar
vencido. No se modifica el reloj ni se renuevan fechas para aprobar la prueba.
El hash permite identificar la versión; no autentica al proveedor.

Ejemplo del comando de lectura (rutas de entrada y salida a elegir):

```sh
python -I scripts/demo_multi_readiness.py --run --seconds 90 \
  --snapshot-dir /ruta/absoluta/cache --report /ruta/nueva/informe.json
```

`--series KXNFLGAME` permite una cohorte NFL exclusivamente en Demo de lectura.
La excepción conserva los topes del perfil conjunto, exige el deporte exacto
`americanfootball_nfl` y mantiene apagadas todas las ejecuciones. El perfil
conjunto de producción continúa limitado a MLB. Universitario no está incorporado
al matcher ni a los snapshots locales: su presencia en el catálogo no acredita
compatibilidad con M2/M5.

Opcionalmente, `--event-ticker KXNFLGAME-26OCT11PHIJAC` limita el ensayo a un
evento identificado previamente en Demo. Se obtiene su metadata por GET y se
verifican serie e identidad; una discrepancia bloquea el descubrimiento. El
informe v2 registra `selection.discovery=explicit_event`: esto comprueba el
recorrido del evento seleccionado, **no el descubrimiento general del catálogo**.
Es útil cuando Demo antepone copias antiguas a eventos actuales. No se modifica
el catálogo ni se crean mercados u órdenes. El padre exige que la selección
devuelta coincida exactamente con sus argumentos.

El informe conserva contadores del embudo M2 (coincidencias, nombres y ausencia
de consenso) y de rechazo por tarifa M5. Las tarifas originales siguen sujetas
a `SeriesFeePolicy`; el modo Demo no reemplaza ni rebaja esa comprobación.

La etiqueta satisfactoria es `HYBRID_READ_ONLY`. Requiere arranque, servicios
supervisados, descubrimiento/libros, saldo, ticks M1, ciclo M2 y salidas,
ticks M5, publicación de fairs, propuestas shadow, ledger financiero vacío,
apagado limpio y ausencia de errores o violaciones de los límites. Una corrida
de 90s sólo alcanza un ciclo ordinario de M2; no acredita continuidad.
No valida órdenes, fills financieros, rentabilidad ni conciliación de la cuenta.
El IOC desconocido preexistente conserva su tratamiento independiente.

## Observación del 9 de octubre de 2026 UTC

El ensayo de las 06:16 UTC arrancó los siete servicios, completó diez GET
HTTP 200 y se apagó correctamente. Hubo 60 ticks M1, un ciclo M2, dos ciclos
de salidas y dos ciclos M5. El ledger financiero permaneció vacío.
El catálogo consultado no aportó mercados: no se inició WS, no hubo libros,
fairs ni propuestas. Resultado conservado: `INCOMPLETE`.

Una consulta pública separada a las 06:18:46 UTC confirmó que
`GET /events?series_ticker=KXMLBGAME&status=open&limit=20` devolvió HTTP 200,
cero eventos y cursor vacío. Esta evidencia se limita a esa serie, filtro y
momento. No se sustituyó el catálogo Demo por datos de producción ni se
relajaron filtros para obtener propuestas.

Las pruebas de `tests/scripts/test_demo_multi_readiness.py` usan transportes y
partidos sintéticos para comprobar tanto el recorrido con coincidencia como
los rechazos por fecha, casas, fees y restricciones de ejecución. Son evidencia
offline y se contabilizan por separado del ensayo conectado anterior.

## Cohorte NFL del 9 de octubre de 2026, 17:35 UTC

Se seleccionó `KXNFLGAME-26OCT11PHIJAC`, identificado en el catálogo público Demo.
La prueba de 90s arrancó los siete servicios, obtuvo nueve GET HTTP 200,
130 frames WS y dos libros inicializados; tuvo 60 ticks M1, un ciclo M2,
dos ciclos de salidas y dos ciclos M5. Apagado limpio, sin errores observados,
sin violaciones de los límites y con cero filas financieras. El snapshot NFL
conservó sus bytes: 26 eventos, capturados a las 17:33:02 UTC y edad inicial 136s.

Resultado: `INCOMPLETE`, por cero fairs M2 y cero propuestas M5. La reproducción
local de los nombres observados mostró `Jacksonville` y `Philadelphia` frente a
`Jacksonville Jaguars` y `Philadelphia Eagles`. El matcher global transforma
`Philadelphia` en `philadelphia phillies`; no obtiene coincidencia. La ampliación
del harness no modifica ese matcher ni acredita onboarding NFL completo.

La consulta pública separada de la serie NFL devolvió `fee_type=quadratic` y
`fee_multiplier=1`; la política actual M5 exige `quadratic_with_maker_fees`.
El test negativo confirma el rechazo. Esta etapa **no se alcanzó en el ensayo
conectado**, porque faltó el fair: el contador conectado `skip_fee_policy=0`
no significa que la tarifa sea compatible. No se sustituyó metadata para aprobar.
