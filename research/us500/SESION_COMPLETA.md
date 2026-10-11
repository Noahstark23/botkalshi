# Ventana pública US500 con presupuesto persistente

`us500_window_capture.py` captura una ventana **planificada** con una muestra cada minuto. Es un recolector público independiente: no importa el bot deportivo, no carga claves, no consulta cuentas, no genera señales ni envía órdenes. No instala temporizadores ni se inicia al importar el módulo.

La cobertura de una ventana no prueba una sesión bursátil completa, independencia estadística ni rentabilidad. Si `next_close_ts` no está disponible, no se deduce el cierre a partir de un calendario o del final elegido. Un cierre realmente recibido queda en la evidencia y termina la captura; los minutos restantes se declaran no recolectados.

## Plan inmutable

El JSON contiene exactamente estos campos. El ejemplo define solamente la **forma**; quien ejecute la recolección debe fijar fechas y presupuesto explícitos antes de empezar:

```json
{
  "schema": "us500-public-window-plan-v1",
  "collection_id": "identificador-de-la-coleccion",
  "environment": "public",
  "ticker": "KXUS500PERP",
  "start_ms": 1791744840000,
  "end_ms": 1791745020000,
  "deadline_ms": 1791745020000,
  "cadence_ms": 60000,
  "max_slots": 3,
  "max_gets": 6,
  "max_storage_bytes": 268435456
}
```

`start_ms` es inclusivo y `end_ms` exclusivo, ambos alineados a minuto UTC. Los slots son `[start,end)` a 60.000 ms. Se exige `start < end <= deadline <= start + 26 horas`, como máximo 1.561 slots, y exactamente dos GET presupuestados por slot. El límite temporal y la cadencia permiten hasta 1.560 slots efectivos en 26 horas. `max_storage_bytes` no puede superar 256 MiB y debe cubrir el overhead del journal más la captura máxima de un slot.

La base guarda el JSON canónico y su SHA-256. Reabrir el mismo directorio con otro plan se rechaza. Crear otro directorio representa otra colección y otro presupuesto; no es una forma de ampliar esta colección. El módulo sólo admite el host/ticker público fijo de `PublicReader("public")`, sin Demo ni URLs arbitrarias.

## API y un solo paso

```python
from us500_window_capture import WindowCapture

with WindowCapture(directory, plan) as capture:
    result = capture.step(now_ms)
    # Opcional: estado público compacto para lectores, bajo el mismo lock.
    capture.write_status(at_ms=now_ms)
```

`reader_factory` y `clock_ms` son inyectables para tests. `snapshot()` no hace red; verifica los artefactos y devuelve estado. Una pérdida/corrupción detectada pausa durablemente la colección, por lo que para observación estrictamente de sólo lectura se debe leer `status.json`, no abrir una nueva instancia.

```text
python3 us500_window_capture.py --plan PLAN.json --directory DIRECTORIO --once
```

El CLI ejecuta un paso y guarda `status.json` atómicamente, con `status_written_at_ms`, presupuesto, conteos y flags de alcance; nunca incluye los cuerpos de mercado. El archivo `status.pending` es un temporal fijo, acotado y reemplazable tras una caída. Las capturas históricas no se sobrescriben.

Un temporizador externo puede invocar el mismo comando, por ejemplo a los cinco segundos de cada minuto. Debe usar un límite total de proceso (el diseño de despliegue prevé `TimeoutStartSec=25`) y ninguna repetición automática del slot. El timeout de socket de `PublicReader` es de cinco segundos, pero no es un deadline total de la transferencia; el límite del supervisor externo es parte del contrato de operación. Este módulo no instala ese supervisor.

## Qué ocurre en cada slot

1. Antes del inicio: espera, sin GET ni consumo de presupuesto. Una invocación tardía marca slots vencidos `MISSED`; no consulta datos retrospectivos para rellenarlos.
2. El lock `flock` del directorio impide dos instancias. El slot pasa a `IN_PROGRESS` y consume **dos GET reservados** en SQLite, confirmado antes de crear el lector o consultar mercado.
3. Cada intento GET incrementa su contador y confirma la transacción antes de HTTP. No hay transacción SQLite abierta durante la solicitud. Se comprueban slot actual y deadline antes de cada GET.
4. Se consulta mercado. Sólo si está abierto y su identidad es válida se consulta libro. Cierre consume un intento y conserva el segundo GET como presupuesto reservado no usado; no se devuelve ni reutiliza.
5. Se guardan cuerpos recibidos y hashes incluso si faltan datos o son inválidos. `validate_snapshot` exige libro, identidad, timestamps y mark vigente; clasifica `USABLE` o `INVALID`, sin inventar profundidad o marks. Un fallo de book conserva el market recibido.
6. Se publica un archivo exclusivo `slot-NNNN.json`, con JSON canónico compacto, hash de sus bytes y tamaño en el journal. Sólo después se confirma el resultado del slot.

Una caída después de reservar, después de HTTP o después de escribir el archivo deja `INTERRUPTED_UNKNOWN` al recuperar. No se intenta de nuevo ese slot y se conserva cualquier archivo parcial/huérfano. Sólo pueden continuar slots futuros, y la cobertura completa ya no se cumple. Cierre de mercado, deadline, almacenamiento insuficiente o pérdida de evidencia detienen la colección.

El presupuesto de almacenamiento deja sitio para una respuesta doble máxima antes de consultar red, más el tamaño acotado de SQLite, su rollback journal y el estado. Todos los archivos históricos con hash guardado se verifican al reabrir, antes de avanzar y al obtener el snapshot. Archivo perdido, symlink, tamaño distinto o SHA modificado produce `EVIDENCE_INTEGRITY_FAILURE`, sin nuevas lecturas. No se borra evidencia para recuperar capacidad.

## Interpretación del estado

- `reserved_gets` es el presupuesto consumido de forma irreversible; `attempted_gets` incluye solicitudes que pudieron fallar o quedar ambiguas. Ninguno significa respuestas exitosas.
- `slot_counts` es la clasificación al guardar, explicitada por `slot_counts_meaning`. `retained_artifacts_integrity_checked` informa si siguen presentes y coinciden con sus hashes.
- `full_planned_slot_coverage=true` exige todos los slots previstos clasificados `USABLE` y artefactos verificados. No equivale a trayectoria continua entre capturas.
- `continuous_market_history_verified`, `source_authenticity_verified`, `independence_verified`, `strategy_validated` y `execution_available` permanecen falsos; `actual_orders` permanece cero.
- `status=INCOMPLETE` conserva motivos y huecos. El estado o fecha de un archivo local no certifica por sí solo que el temporizador continúe vivo; el observador debe revisar la antigüedad de `status_written_at_ms` y el servicio externo.

La captura no ensambla ni ejecuta automáticamente un replay: falta aportar funding/costes explícitos, aplicar los controles del ensamblador y el protocolo `EVALUACION_INDEPENDIENTE.md`. Los tests de este módulo emplean reloj y transporte inventados, con sockets bloqueados; no añaden muestras de mercado.
