# Prueba de US$200 — semana del 5 al 11 de octubre de 2026

Noel indicó que vive en Estados Unidos y destinará US$200 a esta prueba. Antes
indicó una pérdida máxima diaria de US$200. Con ese capital, ese máximo permitiría
perder la asignación entera en un día: no es un objetivo de consumo ni motivo para
elevar los stops existentes. El depósito y la habilitación de la cuenta no se han
verificado. Este candidato local mantiene apagadas todas las órdenes.
El 5/10 Noel confirmó expresamente que todavía no ha hecho el depósito.

## Entrega técnica actual

- Copia aislada `botkalshi-pilot-week200-20261005`, rama
  `codex/pilot-week200-safety-20261005`, base `2e8636e`. Se conservaron los 22
  archivos del paquete anterior sin modificar el checkout original. El remoto
  `origin` de esta copia es otro repositorio local sin referencia `main`; no se
  presenta la base como la última versión de GitHub ni como el código verificado
  del proceso remoto.
- En producción, las entradas requieren saldo observado válido con antigüedad
  máxima de 600 segundos (configurable hacia abajo). Aplica también al modo de
  capital estático. Lecturas fallidas conservan el último dato para diagnóstico,
  pero no renuevan su antigüedad. Fechas futuras y saldos no finitos bloquean.
- Los dólares precisos de `balance_dollars` tienen prioridad; campos ausentes,
  inválidos o contradictorios no se convierten a cero. Las cantidades legacy
  en centavos sólo son fallback cuando falta el campo de dólares.
- El saldo crudo limita el suavizado para que un retiro no conserve capacidad
  ficticia. Los depósitos posteriores no elevan el techo propuesto de US$200.
- Una venta sin restricción ya no atraviesa el apagado global. Las salidas de
  M1, M3 (también usado por M2) y REST solicitan `reduce_only=true` con IOC. Las
  ventas M5 que pueden abrir riesgo siguen necesitando trading habilitado.
  La guarda se revalida tras esperar el límite de solicitudes. Esto conserva la
  atribución existente; no demuestra por sí solo que todo el inventario sea propio.
- M5 detiene nuevas cotizaciones y registra un incidente persistente si falla la
  lectura de fills. Un fill sin cantidad ejecutada válida ya no se sustituye por
  la cantidad solicitada; sólo se marca procesado después de aplicar inventario.
- En el arranque de ejecución, M5 no carga inventario ni mediciones de fills
  simulados. Se reprodujo la contaminación de 6 contratos confirmados con 9
  simulados, que daban un neto incorrecto de 15. Las cohortes de ejecución tienen
  identidad separada; la identidad histórica shadow se conserva.
- El ledger M5 representa cantidades exactas en centésimas de contrato. Recupera
  compras, ventas y ejecuciones parciales después de reiniciar sin duplicarlas.
  Una ejecución confirmada durante una cancelación obliga a recalcular inventario
  y cotización antes del reemplazo. Una orden pendiente conserva la reserva de
  riesgo completa, aunque sólo se haya ejecutado una fracción.
- La reconstrucción del inventario es atómica: una fila inválida, una cantidad
  regresiva, una identidad cambiada o una fila desaparecida bloquean nuevas
  cotizaciones sin publicar inventario incompleto. Los incidentes y cuarentenas
  anteriores no se levantan automáticamente al incorporar soporte de fracciones.
- Los campos precisos se agregan con migraciones idempotentes sin reescribir filas
  anteriores. El PnL calculado conserva cuatro decimales de dólar para precios en
  centavos y cantidades en centésimas; los lectores legacy reciben centavos
  redondeados hacia abajo. Esto no acredita costos, comisiones ni beneficios reales
  de la cuenta: todavía requieren conciliación con Kalshi.
- Se integró el reporte de lectura de `b176507`: saldo antes/después, órdenes,
  posiciones, fills y liquidaciones con subcuenta explícita y paginación acotada.
  También conserva la separación de depósitos y beneficio en la medición.
- `infra/pilot-week200/capture.env.example` agrega `KXNFLGAME` a la captura y
  contempla MLB/NFL en los filtros. Todos los motores y flags de órdenes siguen
  apagados. No incluye claves ni habilita consumo de The Odds API.
- No se reutilizó el experimento Bank200: ese perfil es exclusivo de M1 y no
  autoriza M5. Tampoco se habilitaron dos instancias sobre el mismo capital.

La consulta pública del 5/10 devolvió HTTP 200, 16 eventos NFL y 7 MLB abiertos,
sin cursor adicional. Son conteos de eventos en ese momento, no señales ni
oportunidades rentables. La captura de ambos deportes no valida ninguna estrategia.

## Verificación del candidato

- macOS, Python 3.12.14: **2.370 pruebas aprobadas**, 31 advertencias
  (`macos-fractional-suite.log`).
- Ubuntu 24.04.5 AMD64, Python 3.12.3, emulado localmente: **2.368 aprobadas,
  2 omitidas**, 31 advertencias (`ubuntu-fractional-suite.log`). Dependencias de
  `infra/digitalocean-runtime/requirements.lock` instaladas con verificación de
  hashes; todas sus versiones contrastadas tras instalar las herramientas de QA.
  La ejecución de tests no tuvo red ni credenciales. Las dos pruebas omitidas
  requieren Git y aprobaron en macOS. Imagen de QA `7b06d970af41`.
- La validación estática de servicio y timer del lector pasó con systemd 255 de
  Ubuntu, sin iniciar unidades. No acredita permisos, credenciales, recursos ni
  funcionamiento de esos servicios en el droplet.
- Ruff aprobó `src/`, `tests/`, el cálculo de backfill y los cuatro scripts del
  lector. El formato de los archivos de implementación modificados y
  `git diff --check` aprobaron. Las advertencias de pytest
  incluyen deprecaciones y una corrutina de alertas sin esperar en una prueba
  anterior de M1; no se presentan como resueltas.
- Los hashes de los 22 archivos previos se conservan en el checkout original.
  Los logs y manifiestos están en `../pilot-week200-evidence-20261005/`, hermano
  del checkout. No se copiaron credenciales, bases de producción ni `.env` real.

Estas pruebas verifican mecanismos locales y respuestas simuladas. No prueban
rentabilidad, aceptación de órdenes por Kalshi ni una activación real.

## Por qué se lee la cuenta

El chequeo mínimo operativo responde: cuánto efectivo hay, qué posiciones existen
y qué órdenes pueden ejecutarse todavía. No exige un informe contable escrito por
Noel. Evita usar dinero ya comprometido o ignorar órdenes anteriores. El log leído
el 5/10 mostraba US$0.49, frente a US$49.9825 en el informe del 30/9; no se atribuye
la diferencia a pérdidas sin movimientos y conciliación.

El alias autorizado `botkalshi-chatgpt` permite estado/logs, pero devolvió
`DENIED (read-only): operator-report`. No se cambió de identidad ni se eludió la
restricción. El paquete de reporte está listo para revisión local, no instalado.
La cuenta/subcuenta correcta y la ausencia de otro ejecutor deben comprobarse
antes de cualquier uso financiero.

## Trabajo restante para la prueba de esta semana

1. Confirmar el depósito acreditado y conciliar la cuenta mediante el lector.
   El titular debe habilitar el reporte autorizado o aportar la exportación de
   Kalshi. No se necesita compartir contraseñas ni llaves privadas por chat.
2. Verificar e instalar la versión destinada al servidor. La compatibilidad local
   con Ubuntu AMD64 y el lock ya se comprobó; falta probar el host real. La suite
   local usa transporte simulado y SQLite temporal; no equivale a aceptación de
   órdenes, ejecución demo, producción ni rentabilidad.
3. Conciliar la exposición, los precios y las comisiones reales de la cuenta.
   El soporte local de cantidades fraccionarias de M5 ya está implementado y
   probado; todavía no se instaló ni se contrastó con órdenes de esa cuenta.
   La [guía vigente de Kalshi](https://docs.kalshi.com/getting_started/fixed_point_migration)
   confirma que los fills pueden ser fraccionarios aunque la orden use contratos
   enteros. La consulta pública del 5/10 encontró 14 mercados MLB y 32 NFL;
   ninguno incluyó `fractional_trading_enabled`, cuya ausencia no prueba que las
   fracciones estén desactivadas. Se conserva `public-market-capabilities.json`.
   Los parsers legacy de otros motores todavía requieren revisión; el perfil mantiene sus órdenes
   apagadas. La guarda reduce_only limita la orden en el exchange, no convierte
   una conciliación incompleta en una cartera correcta.
4. Comprobar suministro real de cuotas, comisiones de cada serie y resultados
   netos de la estrategia elegida. M5 admite una cohorte/serie por proceso; no
   se configura MLB y NFL simultáneamente como si ya existiera gestión conjunta.
5. Preparar la revisión del titular con límites, condiciones de parada y la
   evidencia anterior. El titular realiza la activación financiera; este trabajo
   no envía órdenes ni habilita un servicio que las envíe.

US$200 es la asignación propuesta, no una garantía de pérdida máxima proporcionada
por este parche. Los stops heredados más estrictos siguen vigentes. No se eleva
el riesgo para forzar actividad ni se promete rentabilidad o una fecha de primera
operación antes de completar las verificaciones.

## Compatibilidad de los datos

Las migraciones se probaron sólo sobre bases temporales; no se tocó producción.
Una vez que una base contenga fills fraccionarios, no debe abrirse con ejecutores
anteriores que interpreten `filled_count=NULL` como la cantidad solicitada entera.
Un rollback exige una versión compatible y una recuperación de datos verificada;
volver al binario anterior sobre la misma base no es un rollback validado.

## Referencias verificadas

- [Creación V2 y reduce_only](https://docs.kalshi.com/api-reference/orders/create-order-v2):
  reduce_only limita por la posición del miembro y exige immediate_or_cancel.
- [Saldo](https://docs.kalshi.com/api-reference/portfolio/get-balance): dólares
  fixed-point, centavos legacy y ámbito de subcuenta/exchange.
- `../infra/operator-readonly/README.md`: lectura financiera y límites de su prueba.
- `../infra/pilot-week200/capture.env.example`: propuesta sin órdenes.
