# CIERRE-OPERATIVO — estado verificado

**Sesión:** agente de código en la nube (Claude Code remoto), 22-sep-2026
**Base:** `work/issue256-m5-sim-risk-20260922` @ `21088dc9` (PR #263, abierto, draft, sin merge)
**Entorno:** Linux x86_64, Python 3.12.3, venv del repo. **No** es el entorno del registro
P3A (Python 3.14.7 / macOS arm64), así que los conteos de pruebas no son comparables fila a fila.

Esta matriz se llena con pruebas ejecutadas, no con intenciones. Lo que esta sesión **no
pudo alcanzar** figura como BLOCKED con su causa, no como pendiente genérico.

## Matriz

| Elemento | Estado | Evidencia y fecha |
|---|---|---|
| C1 — fila M5, comisión y estado de reserva verificados | **BLOCKED** | `infra/digitalocean-shadow/m5_ledger_bridge.py` **no existe en la rama publicada** (`ls` del directorio, 22-sep: está `m5_bank_bridge.py`, no `m5_ledger_bridge.py`). El candidato vive solo en la Mac, fuera del alcance de esta sesión. Sin el archivo no se revisa 6.1 ni 6.2 sin reescribir a ciegas trabajo local. |
| C1 — cierre contable y recuperación | **PARCIAL — recuperación PASS, cierre PENDING** | **Recuperación:** regresión 6.3 reproducida y corregida, más la validación de contrato que señaló el operador. 12 casos en `tests/strategies/motor_5_mm/test_rehidratacion_comision.py`; suite principal **1.687 passed**; suite de research **323 OK** (aparte). **Cierre contable (6.4/6.5):** no abordado. |
| C2 — M1 conectado y alcance explícito | **PENDING** | No abordado. |
| C2 — M2, M5, M3 y Radar conectados | **PENDING** | No abordado. |
| C2 — capital común, resultados y reporte | **PENDING** | No abordado. |
| C3 — acceso, respaldo y rollback | **BLOCKED** | Esta sesión es un contenedor efímero en la nube sin acceso SSH ni consola al droplet `botkalshi-research-sfo3`. No se intentó ni se simuló. |
| C3 — SHA desplegado y diez ciclos públicos | **BLOCKED** | Misma causa. Sin lectura nueva del host, el release `435d1d9b` sigue siendo referencia histórica, no estado actual. |
| C3 — reinicio preserva estado | **BLOCKED** | Misma causa. |
| Supervisión de ChatGPT | **NOT_CONNECTED** | No se verificó ninguna lectura del informe desde el cliente del propietario. |
| Órdenes y movimientos reales | **NOT_ENABLED** | Sin cambios: no se tocó ningún flag de ejecución, ni `TRADING_ENABLED`, ni ejecutores. |

## C1/6.3 — la regresión corregida, con su número

`_rehidratar_inventory()` (engine.py:379) reconstruía la cohorte con
`apply_fill(..., fee_multiplier=row.fee_multiplier or 1)`, y `apply_fill` **recalcula** la
comisión con el modo vivo del engine (`_fees_as_maker`). La fila ya guardaba el hecho
histórico en `fee_effective_cents` + `fee_model` — el productor los escribe
(`_persist_fill`, engine.py:1362-1378) — y la recuperación los descartaba.

Dos defectos en una línea:

1. **`or 1` sustituye en silencio.** Un `fee_multiplier` ausente (`None`) o cero (falsy)
   pasaba a ser el multiplicador **completo**. Misma clase que la fee ~100× de 2026-07-01.
2. **Recalcular reescribe el pasado.** Cohorte grabada como *taker* rehidratada por un
   engine en modo maker: **comisión real 2¢ reconstruida como 1¢** — 50% de subestimación
   al reiniciar. Rompe el oráculo "Reinicio: igual resultado que ejecución ininterrumpida".

Arreglo: `apply_fill` acepta `fee_cents_recorded` y lo usa verbatim; la rehidratación pasa
`row.fee_effective_cents` y **bloquea** (`Motor5DataIntegrityError`, nombrando la fila) si
falta. Ninguna cohorte se "arregla" rellenando defaults.

**Corrección (22-sep, revisión del operador):** antes escribí que el multiplicador estaba "en
disputa". Ya no: el operador leyó la fuente primaria, `GET /series/KXMLBGAME`, que publica
`fee_multiplier: 0.5` (`quadratic_with_maker_fees`, `last_updated_ts: 2026-09-16`). Esta
sesión **no pudo verificarlo por su cuenta** — el proxy de salida denegó la conexión a
`api.elections.kalshi.com` por política de la organización, y no se reintentó por otra vía.

Lo que esa fuente establece es la tarifa **actual** de la serie, no la que regía en cada
fecha: `last_updated_ts` no es fecha de entrada en vigor, y Kalshi documenta overrides por
evento. Eso **refuerza** el arreglo en vez de debilitarlo: la tarifa de hoy no puede
reescribir el pasado, así que la comisión grabada manda. Y **no** debe fijarse 0.5 en código
de forma permanente: hay que resolver la comisión por evento y momento.

### Segunda iteración — reproducir no es aceptar cualquier valor

El operador señaló que `fee_cents_recorded` se usaba verbatim sin validar tipo ni signo:
bloquear `None` no es rechazar un dato corrupto. Reproducido con **6 pruebas que fallaban**
(negativo, REAL, TEXT —SQLite los guarda sin quejarse—, booleano, e inventario parcial ante
una fila corrupta en medio). Arreglo: `validar_fee_registrada()` exige entero ≥ 0 y excluye
`bool` antes del chequeo de `int` (en Python `isinstance(True, int)` es True). El rechazo
ocurre **antes** del `setdefault`, sin dejar entradas vacías. La rehidratación valida la
cohorte **entera** antes de aplicar una sola fila. El cero documentado sigue siendo válido.

### Registro de validación

| Hora (UTC) | Comando | Resultado |
|---|---|---|
| 22-sep ~17:54 | `pytest tests/strategies/motor_5_mm/ -q` (base `21088dc9`) | 125 passed |
| 22-sep ~17:55 | `pytest tests/.../test_rehidratacion_comision.py -q` (antes del fix) | **4 failed, 1 passed** — regresión reproducida |
| 22-sep ~17:57 | idem (después del fix) | 5 passed |
| 22-sep ~17:58 | `pytest -q` (suite principal, `testpaths = ["tests"]`) | 1.680 passed |
| 22-sep ~17:59 | `ruff check src tests` + `ruff format --check src tests` | limpio |
| 22-sep, 2ª iteración | pruebas de validación de contrato **antes** del fix | **6 failed, 6 passed** — hallazgo del operador reproducido |
| 22-sep, 2ª iteración | idem **después** | 12 passed |
| 22-sep, 2ª iteración | `pytest -q` (suite principal) | **1.687 passed**, 0 fallos, 0 omitidos |
| 22-sep, 2ª iteración | `python3 -m unittest discover -s tests` en `infra/digitalocean-shadow/` | **323 OK**, 0 omitidos |

**Qué acredita cada número, y qué no** (corrección del operador: los conteos no se sustituyen):

- **1.687** es la suite principal. `pyproject.toml:56` fija `testpaths = ["tests"]`, así que
  **no incluye** la suite de research. Las 1.680 que reporté antes tampoco la incluían; lo
  presenté como "suite completa" y no lo era.
- **323** es la suite de research, corrida aparte con `unittest`. El registro P3A dice **329**
  en el mismo commit `21088dc9` (Python 3.14.7 / macOS). Acá son 323 en Python 3.12.3 / Linux,
  sin omitidos. **La diferencia de 6 no está explicada** y no se le atribuye causa sin evidencia.
- **Ruff** verde cubre solo `src/` y `tests/` — lo mismo que el CI. Sobre
  `infra/digitalocean-shadow/`: **58 errores y 27 archivos sin formato**. Deuda previa que el
  CI no ve; no se tocó en este cambio (serían 27 archivos ajenos).
- **La fase Docker del CI no se verificó**: hay CLI pero no daemon
  (`/var/run/docker.sock` no existe). No se levantó uno.
- **El CI de GitHub no corrió en #264**: `ci.yml` filtra `pull_request: branches: [main]` y el
  PR apunta a la rama de #263. Es CI ausente, no fallida — y vale igual para #257→#263.

Alcance de red: ninguna prueba salió a red. No se tocó la DB de producción. No se instaló ni
actualizó ninguna dependencia.

Un test previo (`test_engine.py::test_rehidrata_inventario_de_la_misma_cohorte`) pineaba el
comportamiento laxo: persistía la fila **sin** `fee_effective_cents` y solo afirmaba
`net_contracts`. Se actualizó con nota de **⚠️ CAMBIO SEMÁNTICO DELIBERADO** y ahora afirma
también la comisión. No se redujo el alcance de ninguna prueba para hacerla pasar; no hay
`skip` nuevos.

## Lo que falta, con la intervención exacta

1. **`m5_ledger_bridge.py`** — el operador confirmó que existe en su Mac
   (`botkalshi-issue256-m5-ledger-verify/infra/digitalocean-shadow/`), sin validar. Son tres
   ubicaciones distintas: la Mac, este contenedor en la nube y el droplet; dar acceso al
   repositorio no conecta las otras dos. **Intervención:** publicarlo en una rama del repo.
   Sin eso no se cierra 6.1 ni 6.2, y reconstruirlo a ciegas destruiría trabajo no validado.
2. **C3 completo** — requiere una sesión con acceso al droplet. Esta no lo tiene y no hay
   forma autorizada de obtenerlo desde acá.
3. **Resolver la comisión por evento y fecha** — la fuente actual de la serie ya está (0.5,
   leída por el operador). Falta el mecanismo que resuelva `fee_multiplier_override` por
   evento y el valor vigente en cada momento, en vez de un multiplicador fijo en código. Hoy
   `maker_fee_multiplier_for_ticker` (`src/math/fees.py`) codifica un corte fijo al
   2026-08-07: consistente con el 0.5 actual para la serie, pero no mira overrides por evento.

## Advertencia operativa vigente, ajena a este encargo

El bot de producción lleva **~33 días caído** (crash-loop desde el 20-ago) y la clave API
`BotCoolify` expuesta ~102 días en repo público **sigue sin revocar**. Nada de este encargo
lo toca: es simulación. Pero ninguna de las dos cosas se arregla sola.
