# Continuidad de una ventana pública US500 en tramos

`us500_chain_gate.py` es un control de **sólo lectura** previo al recolector. Lee JSON acotado, verifica identidad, presupuesto y estado anterior, y responde si se puede invocar el tramo solicitado. No hace red, no abre SQLite, no instancia `WindowCapture`, no escribe estado y no inicia procesos. No contiene cuentas, credenciales, órdenes o activación real.

El primer plan instalado permanece intacto. Cada tramo posterior tiene su propio plan, directorio, journal y presupuesto; no se reabre una base terminal para extenderla. El gate no permite gestionar `part01`, porque ese tramo pertenece al temporizador original.

## Manifiesto congelado y alcance del presupuesto

El esquema `us500-public-chain-v1` exige exactamente: `schema`, `chain_id`, `market_open_ms`, `observed_close_ms`, `discovery_gets`, `max_gets`, `segments`. Cada segmento contiene exactamente `name`, `state_directory`, `plan`.

- Nombres consecutivos `part01` a `part08`, como máximo ocho. El estado de `part01` vive en `session`; los siguientes, en su propio `partNN`. No se admiten rutas libres ni nombres repetidos.
- Cada plan embebido pasa `validate_plan`: público exclusivamente, cadencia de 60 segundos, ventana acotada, GETs y almacenamiento limitados. Los `collection_id` son únicos y quedan fijados por el manifiesto.
- Tramos contiguos: el primero comienza en `market_open_ms`, cada inicio coincide con el final exclusivo anterior y el último termina en `observed_close_ms + 60000`. Solapamientos y huecos **entre planes** se rechazan; los huecos de datos observados se conservan por separado.
- `discovery_gets=1` representa la consulta pública externa de descubrimiento declarada por el coordinador. `max_gets` debe coincidir exactamente con la suma de todos los planes más esa consulta y no superar 15.000. Este límite corresponde a esta cadena, no a todas las consultas históricas del proyecto.
- Los hashes se recalculan desde los planes completos. Cambiar sólo un nombre, fecha, presupuesto o parámetro genera una identidad distinta. Un hash no acredita autenticidad de origen.

El plan de cinco tramos comunicado por el coordinador usa apertura declarada `1791756000000` y cierre declarado `1792184400000`; el final exclusivo incorpora un slot para observar ese cierre:

| Tramo | Inicio UTC | Final exclusivo UTC | Slots | GET presupuestados |
|---|---|---|---:|---:|
| part01 | 11-oct-2026 22:00 | 12-oct-2026 23:00 | 1.500 | 3.000 |
| part02 | 12-oct-2026 23:00 | 14-oct-2026 00:00 | 1.500 | 3.000 |
| part03 | 14-oct-2026 00:00 | 15-oct-2026 01:00 | 1.500 | 3.000 |
| part04 | 15-oct-2026 01:00 | 16-oct-2026 02:00 | 1.500 | 3.000 |
| part05 | 16-oct-2026 02:00 | 16-oct-2026 21:01 | 1.141 | 2.282 |

Son 7.141 slots, 14.282 GET reservables y una consulta declarada de descubrimiento: **14.283 GET** como presupuesto total. Esto describe planes; no afirma solicitudes realizadas, respuestas válidas ni cobertura conseguida. El gate no vuelve a consultar la fecha de cierre anunciada ni la autentica.

## CLI para condición de un servicio

```text
python3 us500_chain_gate.py \
  --chain /etc/botkalshi-us500-data/chain.json \
  --segment part02 \
  --plan /etc/botkalshi-us500-data/plans/part02.json \
  --state-root /var/lib/botkalshi-us500-data
```

`--plan` es obligatorio: el archivo externo que usará el recolector debe ser exactamente el plan embebido del segmento. Un plan diferente se rechaza aunque ambos sean válidos de forma aislada. La instalación debe usar ese mismo archivo al lanzar el recolector; el gate no controla comandos posteriores arbitrarios.

Salida 0 permite la invocación; 1 significa espera o bloqueo; 2 indica datos/configuración/lectura inválidos. La salida JSON no contiene rutas de errores, payloads originales ni excepciones crudas. `gate(chain, segment, external_plan, state_root, now_ms=...)` permite comprobar el resultado offline, con reloj explícito.

Sólo se permite `[segment.start_ms, segment.end_ms + 60000)`. Antes del inicio no se leen los estados previos. El minuto adicional final tiene `local_finalization_only=true`: permite consolidar huecos mediante una última invocación local, por ejemplo `end_ms+5s`. El recolector comprueba su propio end/deadline antes de cualquier GET, por lo que esa invocación no consulta mercado. Después de ese minuto se bloquea.

Los temporizadores deben tener fechas concretas, límite total de proceso y ninguna repetición automática del slot. Este módulo no instala ni cambia los temporizadores, incluido el primero ya existente.

La planificación operativa usa el segundo `:05` de cada minuto y un finalizador a `end_ms+5s`. Si el finalizador anterior y el primer tick del siguiente coinciden y el estado previo sigue `ACTIVE`, el gate esperará al próximo minuto. Ese slot perdido queda como hueco explícito; no se lanza una recuperación manual ni se rellena retrospectivamente para forzar cobertura.

## Qué se exige a todos los tramos anteriores

No basta el inmediatamente anterior: se revisa `status.json` de **cada** tramo previo. Se exige esquema correcto, `collection_id` exacto, hash del plan, slots y presupuesto coherentes, y flags de alcance público sin afirmaciones de ejecución o autenticidad.

Los contadores deben sumar todos los slots previstos. Cada slot iniciado consume dos GET reservados; intentos y reserva no pueden exceder el presupuesto. Cada `USABLE` necesariamente hizo dos GET, por lo que se rechaza un estado que declare datos utilizables sin esos intentos. Los hashes de instrumento conocidos de los tramos anteriores deben coincidir. `null` sólo conserva identidad desconocida cuando no hay slots utilizables; no se convierte en igualdad probada.

Estados terminales admitidos:

| Estado | Motivo admitido |
|---|---|
| COMPLETE | ALL_SLOTS_FINISHED |
| INCOMPLETE | ALL_SLOTS_FINISHED, WINDOW_ENDED o DEADLINE_REACHED |

Se bloquean `ACTIVE`, cualquier slot `WAITING`, `IN_PROGRESS`, `INTERRUPTED_UNKNOWN` o `CLOSED`, cierre de mercado, retroceso del reloj, fallos de integridad, almacenamiento o escritura y cualquier otro motivo terminal. `retained_artifacts_integrity_checked` debe ser verdadero. El estado debe haberse escrito como mínimo al inicio del último slot anterior y no más de cinco segundos en el futuro respecto del reloj del gate.

Una terminalidad permitida con `INVALID`, `MISSED` o `NOT_COLLECTED` puede habilitar lecturas posteriores. El resultado conserva `prior_coverage_incomplete`, `prior_gap_counts` y `prior_terminal_states`; no borra ni corrige huecos. También conserva una terminalidad `INCOMPLETE` si todas las capturas quedaron guardadas antes de una caída y el tick final consolidó `WINDOW_ENDED`: se distingue el estado de control de la cobertura registrada.

## Permisos, límites y confianza

La carga del manifiesto admite hasta 128 KiB; cada plan/estado, hasta 16 KiB. Sólo se leen archivos regulares: symlinks y FIFOs se rechazan. Configuración y planes del CLI deben pertenecer a **root (uid 0)** y carecer de bits de escritura (por ejemplo root `0444`); un archivo `0444` propiedad del usuario del servicio se rechaza porque ese usuario podría cambiar sus permisos. Estados y directorios deben pertenecer a root o al usuario efectivo y no permitir escritura de grupo/otros (por ejemplo directorios del recolector `0700`, estados `0600`). El gate verifica permisos; no los modifica. La función Python `gate` recibe manifiesto/plan como objetos y sigue disponible para evaluación offline sin instalar archivos root-owned.

**El gate confía en la atestación previa de `status.json`.** No recalcula aquí hashes de capturas, no abre el journal y no verifica autenticidad actual del productor. Una alteración de artefactos después de emitirse el último status no se detecta mediante esta comprobación. Los permisos y hashes de planes reducen errores de configuración; no reemplazan una auditoría final independiente de los artefactos.

Comparar hashes de instrumentos previos tampoco verifica la identidad del próximo GET: ese dato aún no existe. El recolector valida cada captura en su propio tramo. Queda pendiente auditar conjuntamente cierre efectivamente observado, huecos, identidad entre todos los tramos, integridad retenida y costes explícitos antes de ensamblar una evaluación económica.

`full_session_verified`, `source_authenticity_verified`, `independence_verified`, `strategy_validated` y `execution_available` permanecen falsos; `actual_orders=0`. Permitir la siguiente lectura no acredita continuidad de mercado, ganancia, condiciones de cuenta ni preparación real.
