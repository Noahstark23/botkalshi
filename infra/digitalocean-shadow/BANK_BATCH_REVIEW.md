# Revisión conjunta del bank: M1, M5 y Radar

## Estado y alcance

Implementación **offline, de solo lectura, NO desplegada**, relacionada con la
issue #256 y su corrección de alcance. Añade `bank_batch_review.py` y pruebas;
no modifica `risk_guard.py`, el collector, los motores, el bridge, servicios,
instaladores, workflows, credenciales ni políticas de producción.

El módulo revisa un lote de propuestas declaradas de M1, M5 y RADAR contra un
solo presupuesto hipotético. M2 sigue siendo una fuente de referencias y M3
no se convierte aquí en una nueva estrategia de entrada. Aceptar `origin=M1`
NO implementa ni valida su detector, sus libros o su executor.

**No es un nuevo ledger, un conciliador, una reserva persistente ni un gate
para dinero real.** No abre conexiones ni escribe archivos. No importa clientes
o executors. La CLI lee un archivo acotado y devuelve JSON por stdout. Reutiliza
las constantes y la validación del `risk_guard.py` existente, manteniéndolo intacto.

## Contrato de entrada

Objeto cerrado `schema_version=botkalshi-bank-batch-input-v1` con:

- `snapshot_id`: identificador del snapshot declarado; debe coincidir en cada propuesta.
- `risk`: los ocho campos existentes del guard: `mode`, `source`,
  `capital_reconciled_at`, `capital_reconciled_usd`, `open_risk_usd`,
  `today_new_risk_usd`, `week_net_pnl_usd`, `cumulative_net_pnl_usd`.
  Los cuatro indicadores de autoridad/evidencia son opcionales y, si existen,
  solo se admite el booleano literal `false`.
- `risk_day`, `week_start`: fecha local y lunes inicial de la semana de riesgo
  en America/Los_Angeles. No se deducen a partir del día UTC.
- `cash_available_usd`: efectivo declarado para NUEVO riesgo, una vez apartadas
  las reservas existentes. No es la suma de saldos de varias cuentas.
- `unit_ceiling_usd`: techo de unidad declarado, en centavos enteros, positivo
  y no superior al límite del guard. El revisor no lo aumenta por ganancias;
  puede reducir la unidad cuando disminuye el capital. Su continuidad entre
  ejecuciones requiere una política persistente externa: aquí no se acredita.
- `paused`: booleano explícito. Una pausa existente no se levanta por P&L positivo.
- `thesis_open_risk_usd`: desglose completo por tesis; su suma debe coincidir
  EXACTAMENTE con el riesgo abierto total. El mapeo de correlaciones debe venir
  del conciliador; este módulo no lo descubre ni verifica económicamente.
- `proposals`: hasta 100 objetos con `proposal_id`, `origin` (M1/M5/RADAR),
  `thesis_id`, `snapshot_id`, `observed_at`, `risk_usd`, `includes_all_costs=true`.
  El importe es riesgo máximo solicitado con TODOS los costos incluidos, no
  precio, cantidad, valor al mid ni beneficio esperado. La veracidad de esa
  estimación queda pendiente de una fuente validada.

Los montos deben ser strings decimales acotados: hasta nueve dígitos enteros y
seis decimales. No se aceptan floats, bools, notación exponencial, NaN, infinito,
negativos fuera del P&L ni pérdida silenciosa de precisión. Se utilizan enteros
en microdólares para que, por ejemplo, 2.000001 no se redondee hacia 2.00.

El snapshot y las propuestas deben tener zona horaria y antigüedad entre cero y
180 segundos. Este límite es **del nuevo revisor**, no una modificación del
límite de 24 horas del guard anterior ni una garantía de frescura de mercado.
No actualizar una fecha antigua para hacer pasar evidencia vieja.

`source=confirmed-fill-ledger` y `mode=REAL_SEPARATED` continúan siendo
DECLARACIONES: no se promueven a datos bancarios/exchange verificados.
Un hash identifica los bytes lógicos de entrada, no prueba su autenticidad.

## Funcionamiento

Primero se valida TODO el lote. Una fila inválida al final no deja aceptaciones
parciales. Se calcula la unidad a partir del menor valor entre el techo declarado
y el 1% del capital, redondeado hacia abajo a centavos, conservando las constantes
de parada del guard. Los límites globales abierto/diario son tres unidades.

Las propuestas se revisan en el orden recibido, sin ranking de oportunidades.
Cada solicitud cabe completa o se rechaza: no se ajustan precios o cantidades.
Se consideran simultáneamente el margen por tesis, abierto, diario, efectivo y
pérdida experimental. El riesgo hipotético de una fila se descuenta de los
márgenes de las siguientes, sin otorgar un presupuesto completo a cada motor.

Se normalizan decimales antes de deduplicar por `proposal_id`. Un ID idéntico
con contenido idéntico cuenta una sola vez; contenido conflictivo bloquea todo
el lote. IDs diferentes para una misma operación externa NO se detectan por
adivinación: la atribución y los IDs canónicos corresponden al conciliador.

## Salida y límites obligatorios

Estado `REVIEW_ONLY` o `BLOCKED`. Por fila se devuelve `FITS_HYPOTHETICALLY` o
`DOES_NOT_FIT`, con importes y motivos. Los totales por origen son **riesgo
hipotético asignado en esta revisión**, NO posiciones ni P&L atribuido.

Siempre:

```json
{
  "evidence_state": "DECLARATIVE_NOT_VERIFIED",
  "execution_authorized": false,
  "order_capability_present": false,
  "real_entry_eligible": false,
  "reservations_persisted": false,
  "new_risk_headroom_usd": "0.000000"
}
```

**Dos procesos o dos invocaciones pueden revisar el mismo capital:** este
componente no tiene locks, DB ni reservas transaccionales. No usar su salida
como autorización ni afirmar que impide el doble gasto real. El supervisor
tampoco queda conectado por producir este JSON. Ningún motor está arrancado.

Los datos desconocidos bloquean; cero significa cero declarado, no ausencia de
lectura. No se asume que la suma del riesgo equivale al valor del portafolio.
Un error al leer la entrada produce JSON bloqueado y exit code 2, sin ruta a
órdenes. La CLI rechaza archivos mayores a 64 kB, claves JSON duplicadas, FIFOs
y symlinks finales en Linux; no es un sandbox general para paths no confiables.

## Ejecución y pruebas

Desde un checkout que contenga la dependencia `risk_guard.py`:

```bash
python3 -I -m unittest discover \
  -s infra/digitalocean-shadow/tests -p 'test_bank_batch_review.py' -v

python3 infra/digitalocean-shadow/bank_batch_review.py \
  --input /ruta/privada/bank-batch-input.json
```

No ejecutar estas instrucciones con datos personales para luego subir la salida
a GitHub. No hay instalador ni timer nuevo. No requiere APIs pagadas.

Validación realizada en Python 3.13.5/Linux del entorno de trabajo, NO en el
Droplet: **45 tests nuevos y 9 tests originales del guard: 54 aprobados**.
Se bloquearon `socket.connect`, `socket.connect_ex` y `socket.create_connection`
durante la suite. Un test recorre 500 lotes sintéticos de 20 propuestas: esos
500 casos NO son 500 tests adicionales ni ejecuciones de mercado.

También se probaron cuatro modificaciones defectuosas aisladas (sin conservarlas):
ignorar el consumo global del lote, ignorar la tesis compartida, contar un ID
repetido dos veces e ignorar el efectivo. Las cuatro hicieron fallar los tests.
Esto es una comprobación acotada, no una auditoría exhaustiva de seguridad.

Las dependencias recuperadas por GitHub se verificaron con Git blob SHA:

- `risk_guard.py`: `757e2aac15406bd8274e95fcced0ecedcc51eb61`.
- `cycle_contract.py`: `80cdba78de347c726fae7b30523781c01d9d15b9`.
- `tests/test_risk_guard.py`: `7eafd9ded188cd283f7bd6c026a25dd1b594442f`.

Base de código: `55f203393706bede9fb94e90867dc6f978d9b3db`.
No se ejecutaron la suite legacy completa, Docker, systemd o el servidor remoto.
No se midió rentabilidad ni se conciliaron saldos reales. No hubo despliegue.

## Siguiente integración pendiente

Conectar este reporte a una copia autorizada de la evidencia, conservando origen,
IDs de órdenes/fills y riesgo pendiente. Después, diseñar/revisar la reserva
transaccional en el ledger existente (un solo escritor, idempotencia y recuperación),
sin crear un ledger financiero paralelo. La habilitación del supervisor de solo
lectura requiere un canal privado real y verificación de recepción/frescura.
La validación de libros y la reconstrucción económica de M1 siguen pendientes.

Referencias oficiales consultadas para preservar precisión, no para acreditar
conexión ni ejecución:

- https://docs.kalshi.com/getting_started/fixed_point_migration
- https://docs.kalshi.com/getting_started/orderbook_responses
