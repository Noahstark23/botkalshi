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
| C1 — fila M5, comisión y estado de reserva verificados | **PASS — nivel componente local** | Candidato recibido como texto del operador (SHA-256 `77d850fc…dbdf4`, idéntico al consignado), incorporado **verbatim** (`52e5e8f`), pasada mecánica de ruff con AST verificado idéntico (`9bdff14`), y arreglo (`3f2c401`). 21 pruebas del bridge + 8 del banco: **17 de 21 pasaban con el candidato intacto; las 4 que fallaban reproducían 6.2** (liberada reportada como `RESERVED`, repetición activa indistinguible, banco inexistente creado en un rechazo). Suite de research **352 OK**. Acredita un componente probado localmente con fixtures, **no** el productor ni el circuito. |
| C1 — cierre contable y recuperación | **PASS — nivel componente local** | **Recuperación (6.3):** comisión grabada reproducida y validada; 12 casos; suite principal **1.687 passed**. **Cierre contable (6.4/6.5, `c59b8e5`):** una sola fuente de eventos en la SQLite del banco, proyección derivada en cada lectura; los 4 oráculos sobre USD 200 dan exacto (+0.10 → 200.10; −0.07 → 199.93; +0.59 → 200.59; −0.41 → 199.59); repetición, reinicio, cierre parcial, inversión de signo, concurrencia, migración y fallo entre escrituras cubiertos. 24 tests; **8/8 mutaciones detectadas**. Suite de research **376 OK**. Todavía **no** conectado a ningún motor: el M5 real no escribe eventos. |
| C2 — política única de riesgo (opción A corregida) | **PASS — nivel componente local** | `risk_policy.py` (`deaca89`): unidad = min(USD 2, 1 % del capital simulado) truncada al centavo; habitual = media unidad; tope por operación y por tesis = 1 unidad; abierto y diario = 3 unidades (nunca > USD 6); pausas USD 12 semanal y USD 20 del experimento. `risk_guard` y `bank_batch_review` llaman a la misma función. `REFERENCE_UNIT` sigue en 2; no se fijó 6. 13 pruebas propias. |
| C2 — admisión previa, reserva, fill y evento contable | **PASS — nivel componente local** | `c6cb010`: `admit_proposal` decide y reserva en una sola transacción contra el estado compartido de todos los orígenes; el fill cita la admisión y consume su riesgo admitido **acumulado** (los fills parciales de una cotización no reservan dos veces); períodos por fecha del hecho en America/Los_Angeles; un fill sin admisión, con admisión rechazada/retirada o por encima de lo admitido **se registra** con incumplimiento. Replay almacenado validado fila por fila (`4985afa`). 39 pruebas nuevas; 13 mutaciones de regla detectadas. |
| C2 — ruta M5 en research (cohorte `m5-research-rest-v1`) | **PASS — nivel componente local** | `f2397e8`, por decisión del propietario («Simular M5 en research»). Captura → review estricto de M1 → candidata (`compute_quote`) → `admit_proposal` → `activate_admission` → fill **solo** en observaciones posteriores (`record_quote_observation`, atómico) → evento contable. 41 pruebas de aceptación (lista del CTO completa, más controles); 15 de 16 mutaciones detectadas. Flag `BOTKALSHI_M5_RESEARCH_ENABLED` **apagado por defecto**. Sin productor de fair en el droplet → el estado real sería `BLOCKED_NO_FAIR`. |
| C2 — M1 en el presupuesto común | **PENDING** | M1 sigue en research con su atribución (review por ciclo), pero todavía **no** llama a `admit_proposal`: no emite propuestas con riesgo, solo diagnóstico. |
| C2 — Radar | **NOT_CONNECTED** | No hay productor en el código. No se sustituye por señales inventadas. |
| C2 — M2 referencia, M3 salidas simuladas, reporte único | **PENDING** | No abordado. |
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
disputa". Ya no: ChatGPT leyó la fuente primaria pública (revisión compartida por el
operador), `GET /series/KXMLBGAME`, que publica
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
| 22-sep, 3ª iteración, base `05713bf` | `python3 -m unittest -v test_collector` (archivo hermano, fuera de `tests/`) | **6 OK** — mismos IDs que el log P3A |
| 22-sep, 3ª iteración, candidato `52e5e8f` | `python3 -m unittest tests.test_m5_ledger_bridge` | **17 OK, 3 failures, 1 error** — 6.2 reproducido |
| 22-sep, 3ª iteración, `3f2c401` | idem | **21 OK** |
| 22-sep, 3ª iteración, `3f2c401` | `python3 -m unittest tests.test_simulation_bank_outcome` | **8 OK** (incluye 2 de concurrencia con hilos) |
| 22-sep, 3ª iteración, `3f2c401` | `unittest discover -s tests` en `infra/digitalocean-shadow/` | **352 OK** (= 323 + 21 + 8), 0 omitidos |
| 22-sep, 3ª iteración, `3f2c401` | `pytest -q` (suite principal) | **1.687 passed** — `src/` y `tests/` sin cambios desde `05713bf` |
| 22-sep, 4ª iteración, `c59b8e5` | `unittest tests.test_simulation_ledger` | **24 OK** |
| 22-sep, 4ª iteración, `c59b8e5` | 8 mutaciones deliberadas del ledger (sin liberar al quedar plano, liberar en parcial, comisión ×2, comisión ×0, LIFO, liquidar sin posición, admisión ciega a pérdidas, saltar validación de vínculo) | **8/8 rompen tests**. LIFO sobrevivía hasta agregar el caso de dos lotes |
| 22-sep, 4ª iteración, `c59b8e5` | `unittest discover -s tests` en `infra/digitalocean-shadow/` | **376 OK** (= 352 + 24) |
| 22-sep, 4ª iteración, `c59b8e5` | `unittest test_collector` | **6 OK** |
| 22-sep, 4ª iteración | ruff `infra/` base vs HEAD | 58 = 58; test nuevo limpio |
| 22-sep, 5ª iteración, `4985afa` | `unittest discover -s tests` en `infra/digitalocean-shadow/` (re-medido en worktree del commit) | **379 OK** |
| 22-sep, 5ª iteración, `deaca89` | idem | **393 OK** |
| 22-sep, 5ª iteración, `c6cb010` | idem | **432 OK** (39 de `test_simulation_admission`) |
| 22-sep, 5ª iteración, `c6cb010` | `unittest test_collector` | **6 OK** |
| 22-sep, 5ª iteración, `c6cb010` | 14 mutaciones de la admisión (habitual→unidad, sin tope de tesis, tesis ignorada, semana desde domingo, días UTC, exceso fuera del diario, cobertura no acumulada, reserva liberada que cubre, propuesta vieja aceptada, exposición sin reserva ignorada, sin tope diario, sin pausa semanal, techo del experimento sin riesgo abierto) | **13/13 reales rompen tests**; la 14ª no aplicó (patrón inexistente) y no se cuenta |
| 22-sep, 5ª iteración, `c6cb010` | `pytest -q` (suite principal) | **1.689 passed** (+2 de `test_dependencias_espejadas`, `10911ac`) |
| 22-sep, 5ª iteración, `c6cb010` | `ruff check src tests` + `ruff format --check src tests` | limpio |
| 22-sep, 5ª iteración, `c6cb010` | ruff `infra/` base `21088dc9` vs HEAD, mismo comando | **63 = 63**. El 58 de la fila anterior no se reprodujo con este comando; lo que se compara es base contra HEAD. `simulation_bank.py` ya estaba sin formato en la base: no se reformateó (sin formato masivo) |
| 22-sep, 6ª iteración, `f2397e8` | `unittest tests.test_m5_research_sim` | **41 OK** |
| 22-sep, 6ª iteración, `f2397e8` | `unittest discover -s tests` en `infra/digitalocean-shadow/` | **476 OK** |
| 22-sep, 6ª iteración, `f2397e8` | `unittest test_collector` | **6 OK** |
| 22-sep, 6ª iteración, antes del arreglo | `env -i /usr/bin/python3 -S -c "import research_runner"` | **`ModuleNotFoundError: No module named 'src'`**: la rama apilada no arrancaba con el python del sistema (el venv lo tapaba) |
| 22-sep, 6ª iteración, `f2397e8` | idem | importa (también `m5_research_sim`); fijado por test |
| 22-sep, 6ª iteración, `f2397e8` | 16 mutaciones (banco: misma observación, observación anterior, plano libera pendiente, activa con reserva liberada, retirada sigue llenando, observación re-evaluada; adaptador: riesgo bilateral sumado, precisión incompatible, fair vencido, sin TTL, sin barrido, replay reactiva, re-cotiza en el ciclo del retiro, evalúa la generadora, brecha sin incertidumbre, fee inventada) | **15/16 detectadas**. Sobrevive «replay reactiva» del adaptador: el banco la rechaza igual (`WITHDRAWN`); es una defensa redundante, no un hueco |
| 22-sep, 6ª iteración, `f2397e8` | ruff `infra/` | **63 = 63** vs base; archivos nuevos limpios y formateados |

Entorno de todas las filas: Linux x86_64, Python 3.12.3, venv del repo, sockets bloqueados en
los tests de research. Nivel acreditado: **componente probado localmente**. No hay prueba de
integración del circuito ni de despliegue.

**Qué acredita cada número, y qué no** (corrección del operador: los conteos no se sustituyen):

- **1.687** es la suite principal. `pyproject.toml:56` fija `testpaths = ["tests"]`, así que
  **no incluye** la suite de research. Las 1.680 que reporté antes tampoco la incluían; lo
  presenté como "suite completa" y no lo era.
- **323** fue la suite de research con `unittest discover -s tests`. **Corrección:** dije que la
  diferencia con las 329 del registro P3A "no estaba explicada" — la causa era mi propio
  comando. `discover -s tests` excluye el archivo hermano `infra/digitalocean-shadow/test_collector.py`,
  que tiene 6 pruebas (señalado por el operador). Las corrí aparte en este entorno: los **6 OK**,
  con los mismos identificadores que el log P3A. No se reetiqueta "329 OK" por suma: son dos
  selecciones distintas, corridas y registradas por separado.
- **Ruff** verde cubre solo `src/` y `tests/` — lo mismo que el CI. Sobre
  `infra/digitalocean-shadow/`: **58 errores y 27 archivos sin formato**, medido **igual** en la
  base `21088dc9` y en `3f2c401` con la misma configuración (worktree de la base). Es deuda
  previa, no introducida acá; los 3 archivos nuevos quedan limpios y el único error en un
  archivo tocado (`simulation_bank.py:38`, I001) está en un bloque de imports no modificado.
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

1. **M5 research en el droplet (C3, requiere acceso autorizado).** Tres pasos del operador,
   en este orden, y ninguno es automático:
   (a) crear el banco ficticio con capital explícito
   (`simulation_bank.init_bank(ruta, initial_capital_usd="200.00")`). El runner **nunca** lo
   crea, y sin banco reporta `BLOCKED_NO_BANK`;
   (b) proveer `m5/inputs.json` (`botkalshi-m5-research-inputs-v1`) con el fair y la fee de
   cada evento, más su fuente y su fecha. Hoy **no hay productor**: sin él todo queda en
   `BLOCKED_NO_FAIR`. Producirlo con The Odds API sería un gasto nuevo y está fuera de
   alcance;
   (c) `BOTKALSHI_M5_RESEARCH_ENABLED=true`.
2. **M1 al presupuesto común**: hoy M1 solo diagnostica y no propone riesgo. Conectarlo
   requiere definir qué sería una propuesta de M1 (tamaño y tesis) — no se inventa.
2b. **Camino legacy** `reserve()` + `reservation_key`: sigue existiendo y no pasa por la
   política. `m5_ledger_bridge` reserva al observar el fill (C1); con C2 eso equivale a un fill
   sin admisión previa. Se retira o se reetiqueta cuando se conecte el productor, no antes.
3. **C2/7.3 — resuelto** por decisión del propietario (opción A corregida, `deaca89`).
4. **`fee_model` sin contrastar** en la recuperación de 6.3 (limitación abierta de #264).
5. **C3 completo** — requiere una sesión con acceso al droplet. Esta no lo tiene y no hay
   forma autorizada de obtenerlo desde acá.
6. **Resolver la comisión por evento y fecha** — la fuente actual de la serie ya está (0.5,
   leída por ChatGPT el 22-sep; esta sesión no pudo consultarla). Falta el mecanismo que resuelva `fee_multiplier_override` por
   evento y el valor vigente en cada momento, en vez de un multiplicador fijo en código. Hoy
   `maker_fee_multiplier_for_ticker` (`src/math/fees.py`) codifica un corte fijo al
   2026-08-07: consistente con el 0.5 actual para la serie, pero no mira overrides por evento.

## Historial operativo, ajeno a este encargo — NO es estado actual

Última observación que tuvo esta sesión, **21-ago-2026**: el container de producción en
crash-loop (`RestartCount 464`) y la clave `BotCoolify` sin revocar. **Nada de eso se volvió a
verificar desde entonces**, y esta sesión no tiene acceso al host ni a la cuenta: no puede
certificar el estado actual, ni favorable ni desfavorable. Figura como historial fechado, no
como afirmación presente. Tampoco se inspeccionaron secretos para comprobarlo.
