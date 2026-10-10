# Integración local del ejecutor y el simulador — 10-oct-2026

Base readiness: `7fc74a2af070ce8fe7eae940ea32293b2b89c1a1`.
Fuente simulador: `09e060435bec0d1b2321c1c2f5d68e5fd75fe10a`.
Resultado: candidato local integrado; no despliegue ni certificación de operación.

## Qué se integró

- Identidad M2 por ticker y aliases deportivos (`c7beb96`), más separación exacta
  NFL/NCAAF y nombres originales observados (`09e0604`).
- Herramientas y pruebas del simulador `infra/digitalocean-shadow`, incluido el
  banco compartido, liquidación parcial, reservas de quotes, reinicio y stream
  durable. Los instaladores y unidades se incorporaron como archivos; no se
  ejecutaron ni instalaron servicios.
- Política de tarifas M5 Demo de `a36540b` y sus pruebas. La adaptación conserva
  el modo Demo v1 previo y exige el segundo opt-in local
  `demo_football_fee_validation=True` para el modo de tarifas fútbol v2. No está
  conectado a env vars ni al runner; exige cliente de lectura explícito.
- Job `simulator-offline` en CI. Se ejecutó localmente su suite aislada; no se
  disparó GitHub Actions porque no hubo push.
- El informe del harness Demo declara `payoff_equivalence_verified=False` y
  `strategy_readiness=False`. Su validador rechaza que un worker cambie esos
  valores: arranque y quotes shadow no certifican la estrategia.

No se sustituyó el engine completo por la versión más antigua del simulador.
Permanecen iguales 14 archivos protegidos: runner, configuración, modelos,
cliente REST, cantidades, riesgo/exposición, ejecutores M2/M5, salidas M2,
conciliador M5, inventario M5, ledger M5 e incidentes. Dentro del engine M5,
28 métodos conservan el mismo árbol sintáctico, incluidos conciliación de fills,
controles de riesgo y cierre de sesión. La igualdad de código complementa las
pruebas, no demuestra operación del exchange.

## Verificación local

227 pruebas focalizadas aprobadas. Suite integrada: **4.874 pruebas y 104
subpruebas aprobadas**, 47 advertencias, 130,56 segundos. Ruff: cero hallazgos.
Formato de `src` y `tests`: 365 archivos correctos. `git diff --check` sin errores.
Las advertencias se conservan en el log y no se presentan como cero advertencias.

El runner aislado elimina entorno heredado, desactiva `.env`, usa SQLite temporal
y bloquea red externa. Los tests de WebSocket usan loopback. No se ejecutó el
harness conectado ni se consumieron cuotas Odds/Jev en esta integración.

## Auditoría de los cuatro objetivos

| Objetivo | Evidencia actual | Estado |
|---|---|---|
| Ejecutor y riesgo | Suite conjunta; conservación del candidato con cantidades/costes exactos, pausas, reservas y conciliación | Validación local aprobada; operación conectada aún pendiente |
| Ejecución, cancelación, parcial, reinicio y conciliación Demo | Pruebas sintéticas; última lectura específica del IOC de las 18:34 UTC sigue sin estado terminal | Incompleto: no confundir pruebas con fills observados |
| Muestra prospectiva neta de costes | Dos partidos con cuotas, de tres capturados; margen condicionado no positivo y cero resultados liquidados | Sin evidencia de ventaja ni muestra suficiente |
| Cuenta, saldo, permisos y límites | Lectura Demo anterior; perfil dinámico del candidato y sus límites verificados por tests | No prueba saldo/permisos actuales de producción ni un perfil real M2/M5 activable |

La restricción vigente del titular es no tocar producción. No se accedió a ella
para actualizar saldo o permisos. Los límites del perfil
`EXPERIMENT_DYNAMIC_MULTI_ENABLED` pertenecen explícitamente a offline/shadow:
90% del cash, saldo máximo de 60 segundos, sin suavizado, máximo US$1 por trade,
exposición global hasta 10%, máximo US$1 direccional por evento y subtope M5
US$3. Son techos del validador, no autorización ni garantía de ejecución. El
perfil exige todas las ejecuciones apagadas; no se eliminó esa restricción para
obtener un arranque verde. No se aplicó configuración a ninguna cuenta o servicio.

## Pendiente antes de declarar cierre

El seguimiento aprobado fue enviado a soporte; al leer el hilo en esta
continuación constaban cuatro mensajes y ninguna respuesta al nuevo seguimiento.
Se mantiene el IOC UNKNOWN y su reserva ficticia de 66¢. No se reenviaron órdenes.

Las capturas NFL/NCAAF conservan diferencias de liquidación conocidas, identidad
incompleta de reglamentos de casas y precios archivados. Esta integración no
transforma su consenso condicional en fair certificado. La validación positiva
M2→M5 y los fills parciales conectados siguen pendientes. No hay órdenes nuevas,
push, merge, despliegue, cambios de flags o reinicios de servicios.

El paquete externo `INTEGRACION_CANDIDATOS_2026-10-10` contiene logs, hashes,
matriz de conservación y resultado consolidado. La meta general permanece abierta.
