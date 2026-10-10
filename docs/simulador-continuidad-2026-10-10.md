# Liquidación parcial, flujo incremental y CI — 10 de octubre de 2026

## Cartera M2

`paper_engine.settle_market` admite exactamente una de dos formas de resultado:
`market_result="yes"/"no"` o `yes_payout_cents` entero de 0 a 100. El lado NO recibe
100 menos ese valor. Se paga solamente la cantidad efectivamente llenada; se
mantienen los costes y las comisiones de entrada. Un empate a 50¢ no equivale a
reembolsar el precio de compra. El simulador no estima el fair price de cancelación:
el valor debe venir explícito de la evidencia o del escenario ficticio.

Ejemplo de evento compatible con el replay y el consumidor incremental:

```json
{"type":"settle","ticker":"SIM-TEST","yes_payout_cents":50}
```

Los valores 0/100 conservan las tablas binarias anteriores. Los valores 1..99
usan una tabla aditiva `paper_partial_settlements`, sin reescribir resultados
históricos. Pago, PnL, liberación de reservas y resultado del mercado se confirman
en una sola transacción. M2 y M5 consultan el mismo resultado; una contradicción
se rechaza. Repetir un resultado no duplica dinero. Registros parciales sin
contabilidad consistente bloquean el resumen y nuevas admisiones.

Alcance numérico: cantidades enteras del simulador y pago entero por contrato
en centavos. No acepta pagos con fracciones de centavo ni los redondea.

## Flujo incremental local

`football_capture_import.load_capture` lee el formato de captura archivada
`captura.json` + `milestones-captura.json`, comprueba cada hash, impide rutas
externas/enlaces, conserva timestamps y distingue un universo parcialmente
paginado. Los hashes detectan cambios respecto del manifiesto; no autentican
por sí solos al proveedor. La entrada pública se vuelve a validar con los
contratos de identidad, kickoff, comisiones y antigüedad existentes.

`paper_stream.run_cycle` consume un JSONL que sólo admite nuevas líneas al final.
Cada DB pertenece a un stream y a un modo fijo:

- `CONNECTED_CAPTURE`: audita snapshots NFL/NCAAF, conserva razones de bloqueo
  y relojes de la captura. No publica fair ni genera señales o fills.
- `OFFLINE_SCENARIO`: recibe propuestas ficticias M2/M5 explícitas y usa su
  banco compartido, incluidos cancelación, pago parcial y pausa por estado UNKNOWN.

Antes de tocar la cartera guarda durablemente el payload y su hash como PENDING.
Después registra DONE. Si el proceso falla entre el efecto y el checkpoint,
reanuda exactamente la misma identidad y usa la idempotencia del ledger. Un error
impide consumir los siguientes eventos; no se borra ni se sustituye un pendiente.
Cambiar un prefijo, truncar el historial, cambiar el modo, adoptar una cartera con
actividad previa o ejecutar dos consumidores simultáneos produce rechazo.

Límites: máximo 100 eventos nuevos por ciclo, historial de hasta 1.000 eventos/
10 MB y máximo 1 MB por evento. Alcanzar un límite requiere rotación diseñada y
revisada; no trunca automáticamente. Un stream que falla necesita diagnóstico,
no edición de su intención pendiente. El bloqueo de archivo coordina consumidores
de este módulo; no reemplaza el control de acceso a SQLite ni bloquea escritores
externos que ignoren el protocolo. Debe usarse una DB nueva y aislada.

Desde el checkout, con JSONL ya verificado y capital exclusivamente ficticio:

```sh
python infra/digitalocean-shadow/paper_stream.py \
  --db /ruta/nueva/captura.sqlite3 --capital 100.00 \
  --stream-id captura-futbol --mode CONNECTED_CAPTURE \
  --input /ruta/capturas-verificadas.jsonl
```

La llamada procesa un lote y termina. Nuevas llamadas consumen el prefijo
ampliado. No hay daemon, servicio ni colector arrancado por este cambio; la captura
HTTP permanece separada, con sus presupuestos explícitos. No se rejuvenecen datos
históricos ni se presentan como nuevos resultados prospectivos.

## Integración continua

El nuevo job `simulator-offline` instala dependencias y ejecuta Ruff más
`scripts/test_simulator_isolated.py`. Incluye las pruebas root, las pruebas de
`infra/digitalocean-shadow/tests` y las del collector. El wrapper limpia el entorno,
deshabilita `.env`, usa SQLite temporal y bloquea DNS/conexiones externas dentro
del proceso Python; permite loopback para los tests WS. No es un sandbox del SO
para código hostil ni debe recibir secretos de CI.

No hay paso de despliegue ni secret configurado en ese job. El workflow conserva
sus disparadores; este cambio local no se publicó y no prueba una ejecución en
GitHub Actions. El comando del job sí se verifica localmente con la suite completa.

## Evidencia y límites

La reproducción archivada consume tres pares de la captura previa en dos lotes
(1 + 2) y procesa cero eventos al reiniciar. Los tres permanecen bloqueados por
semántica no equivalente/no revisada; uno además carece de quotes. Capital
ficticio: 100.00, reservas: 0.00, señales y fills: cero.

En otra DB se reproduce un escenario sintético M2/M5 con payout de 50¢:
M2 -2¢, M5 +9¢, saldo final 100.07 y reservas/posiciones residuales cero. El pago
fue impuesto por el escenario; esos +7¢ no son ventaja observada ni rentabilidad.
Las pruebas inyectan fallos entre cada efecto y su checkpoint, además de fallos
de escritura, resultados contradictorios, pérdida del índice compartido y pausas.

El 10-oct a las 17:37 UTC se consultó el IOC Demo original con ocho GET oficiales,
todos HTTP 200. Las colecciones recientes/históricas completas no lo muestran y
no se observaron posiciones abiertas. La reserva ficticia de 66¢ y el diario
original se mantienen intactos. La revisión del hilo de soporte conserva los
mismos tres mensajes; la respuesta genérica no resuelve esa solicitud concreta.
No se enviaron correos, órdenes ni cancelaciones.

Sigue pendiente la resolución específica del IOC, el recorrido conectado completo
con fills/conciliación y la equivalencia económica de reglas antes de transformar
cuotas NFL/NCAAF en valores de contrato. No se habilitó producción ni se consumió
Odds o Jev en esta continuación. Cero resultados independientes nuevos de estrategia.

Informe, DB nuevas, entradas, hashes, SHA final y resultados de pruebas:
`/Users/stark/Downloads/botkalshi-P0-cierre/SIMULADOR_CONTINUIDAD_2026-10-10/`.
