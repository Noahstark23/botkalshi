# Candidato local M1 con capital dinámico

**Este paquete es LOCAL y NO está instalado.** La plantilla deja trading,
estudio M1 y todas las ejecuciones apagados. No contiene credenciales, no inicia
servicios ni autoriza órdenes. Los perfiles Bank50 y Bank200 quedan deshabilitados.

El capital proviene del cash disponible observado en Kalshi, no de un bank fijo.
`ACTIVE_CAPITAL_USD=100.43` es sólo referencia de diagnóstico; no acredita fondos
ni permite entradas cuando falta saldo fresco. La plantilla deja
`EXPERIMENT_BANK_CONFIRMED_USD=0`, pero DynamicM1 ignora ese campo legado: tampoco
lo compara si conserva 49.49 o 100.43. No exige un reconocimiento de monto ni que
el saldo real sea cero, 50 o 100.43.

El perfil conserva factor de seguridad 90%, suavizado cero, refresco cada 30s y
caducidad de 60s. El piso efectivo 40.5 equivale a un cash mínimo de 45 para abrir
entradas con ese factor. El techo 5000 es el backstop de producción preexistente,
independiente del saldo; no es un depósito, objetivo de capital ni permiso para
arriesgar ese importe. Por debajo del piso, sin saldo o con lectura vencida se
bloquean nuevas entradas.

Los controles absolutos son independientes del cash: hasta US$1 por operación y
evento, US$3 abierto y de nuevos compromisos diarios, frenos de pérdida semanal
US$3 y acumulada US$5. Los topes porcentuales pueden reducir esos márgenes. La
política está implementada en Settings/RiskManager, no en esta plantilla. Los
depósitos no amplían esos topes absolutos. Ya no hay presupuesto histórico de
compromisos de US$50. Estos frenos no garantizan pérdidas máximas: existen fills
parciales, costes de rollback, comisiones y posiciones abiertas.

`EXPERIMENT_START_AT` queda vacío deliberadamente. Debe definirse una fecha válida
con timezone para validar el candidato; una fecha de preparación no demuestra
que haya habido operaciones. No cambiar el inicio para eludir consumo, pérdidas
o pausas persistentes. No se modifica ni se elimina el historial de la DB.

El preflight se ejecuta desde el WorkingDirectory del release mediante
`python -m scripts.check_dynamic_m1_deployment`. Devuelve JSON y código 0 para
configuración válida, 2 para rechazo. Usa `Settings(_env_file=None)` del entorno
efectivo y rechaza cualquier `.env` en cwd, incluso enlaces rotos, sin leerlo.
Settings puede comprobar la existencia de la ruta PEM o validar material dado
por entorno; la salida no contiene valores privados. No consulta DB, saldo ni red.
Se reutilizan las constantes de hosts, DB y servicios del preflight Bank50, sin
usar su validación de bank/ack50 ni alterar su contrato.

Se exige production con hosts canónicos, DB fija, health en 127.0.0.1:18080,
proveedores Odds/Telegram vacíos, otros motores y gestores apagados. M1 puede
estudiar sin ejecución; una configuración futura de ejecución requiere ambos
flags M1 y trading explícitos. El preflight nunca los activa. PASS no acredita
autenticación, tarifas efectivas, conciliación ni rentabilidad.

La unidad propuesta conserva usuario, DB, WorkingDirectory, puerto, ExecStart y
hardening del runtime existente. Sólo cambia descripción y ExecStartPre.
`STANDBY_REVIEWED` conserva su nombre legado por compatibilidad; no se crea otro
marcador de aprobación. No ejecutar dos runtimes sobre la misma DB. Ningún paso
de este trabajo instala esta unidad, reinicia el servidor ni levanta una pausa.

Antes de cualquier instalación posterior se requieren revisión del candidato,
pruebas offline integradas, configuración efectiva apagada, copia consistente
y verificación de migraciones. Los pendientes de tarifas, ejecución y resultados
de mercado siguen separados de este cambio de configuración.
