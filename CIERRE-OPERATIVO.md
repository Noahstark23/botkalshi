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
| C1 — cierre contable y recuperación | **PARCIAL — recuperación PASS, cierre PENDING** | **Recuperación:** regresión 6.3 reproducida y corregida. 5 pruebas nuevas en `tests/strategies/motor_5_mm/test_rehidratacion_comision.py`; 130 pruebas de M5 verdes; suite completa **1.680 passed**. **Cierre contable (6.4/6.5):** no abordado en esta sesión. |
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

Agravante que lo hace urgente: el multiplicador maker de KXMLBGAME sigue **en disputa** (el
código afirma 0.5 desde 2026-08-07; el PDF oficial verificado el 13-ago dice 1). Mientras esa
pregunta esté abierta, recalcular comisiones en cada arranque es lo peor que se puede hacer.

### Registro de validación

| Hora (UTC) | Comando | Resultado |
|---|---|---|
| 22-sep ~17:54 | `pytest tests/strategies/motor_5_mm/ -q` (base `21088dc9`) | 125 passed |
| 22-sep ~17:55 | `pytest tests/.../test_rehidratacion_comision.py -q` (antes del fix) | **4 failed, 1 passed** — regresión reproducida |
| 22-sep ~17:57 | idem (después del fix) | 5 passed |
| 22-sep ~17:58 | `pytest -q` (suite completa) | **1.680 passed**, 0 fallos, 0 omitidos |
| 22-sep ~17:59 | `ruff check src tests` + `ruff format --check src tests` | limpio |

Alcance de red: ninguna prueba salió a red. No se tocó la DB de producción. No se instaló ni
actualizó ninguna dependencia.

Un test previo (`test_engine.py::test_rehidrata_inventario_de_la_misma_cohorte`) pineaba el
comportamiento laxo: persistía la fila **sin** `fee_effective_cents` y solo afirmaba
`net_contracts`. Se actualizó con nota de **⚠️ CAMBIO SEMÁNTICO DELIBERADO** y ahora afirma
también la comisión. No se redujo el alcance de ninguna prueba para hacerla pasar; no hay
`skip` nuevos.

## Lo que falta, con la intervención exacta

1. **`m5_ledger_bridge.py`** — pegá el archivo (o publicalo en una rama). Sin él no se cierra
   6.1 ni 6.2, y reescribirlo a ciegas destruiría trabajo local no validado.
2. **C3 completo** — requiere una sesión con acceso al droplet. Esta no lo tiene y no hay
   forma autorizada de obtenerlo desde acá.
3. **El multiplicador maker de KXMLBGAME** — la fuente del 0.5. Es el dato más barato de
   conseguir y el que más mueve la aritmética del gate.

## Advertencia operativa vigente, ajena a este encargo

El bot de producción lleva **~33 días caído** (crash-loop desde el 20-ago) y la clave API
`BotCoolify` expuesta ~102 días en repo público **sigue sin revocar**. Nada de este encargo
lo toca: es simulación. Pero ninguna de las dos cosas se arregla sola.
