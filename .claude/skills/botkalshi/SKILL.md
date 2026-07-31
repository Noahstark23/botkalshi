---
name: botkalshi
description: Protocolo de trabajo de Fable 5 sobre el bot de trading Kalshi (dinero real). Usar al arrancar CUALQUIER tarea de este repo — diagnóstico de producción, cambio de lógica de trading, activación de flags, contención de incidente, o PR. Encapsula las reglas de seguridad no negociables y los workflows probados.
---

# botkalshi — protocolo de trabajo (Fable 5)

Bot 24/7 con **dinero real**. Todo error cuesta plata. El mapa completo del proyecto, los
motores y la arquitectura de seguridad viven en `CLAUDE.md` (leelo primero si no lo tenés).
Esta skill es el **protocolo operativo**: qué hacer, en qué orden, y qué no hacer jamás.

Para trabajar SOBRE un motor específico, invocá además su skill: `motor-1-arbitraje`,
`motor-2-consenso`, `motor-3-clv`, `motor-rest-arb`, `motor-5-mm` — cada una trae el mapa
de archivos, flags, invariantes propias, diagnóstico y checklist de activación del motor.

## Invariantes de seguridad (violarlas = incidente)

1. **Capa A**: un executor solo existe con sus flags de ejecución on. Nunca construyas un
   executor fuera del gate del runner; nunca instancies uno "por conveniencia" en un script.
2. **Nunca vender la pata de un hedge** (arb con ambas patas filladas). Toda gestión de
   posiciones pasa por atribución de origen (`_attributable_positions`, `orphans.py`).
3. **Órdenes de arbitraje: SIEMPRE `time_in_force` explícito** (`fill_or_kill` para arbs,
   `immediate_or_cancel` para direccionales/salidas). El default `gtc` deja orden RESTING =
   exposición silenciosa (Issue #14, incidente 2026-07-07). Y **siempre leer `fill_count`
   de la respuesta** — HTTP 200 significa "aceptada", no "llenada".
4. **Pausas persistentes, no runtime-only**: cualquier condición que deba frenar el bot usa
   `engage_kill_switch()` (DB, sobrevive redeploys) — un `BotState.is_paused` solo se pierde
   en el próximo deploy. Levantar SOLO con `scripts/clear_kill_switch.py`.
5. **Fail-open en LECTURA, fail-closed en VENTA**: un hiccup de API no apaga el bot; ante la
   duda sobre qué se puede vender, NO se vende (`_open_attributable_count` error→0).
6. **Dinero en cents enteros**; fees SOLO con `kalshi_fee_cents`. Tiempo: `settled_at`/
   `close_time` NAIVE UTC; `placed_at` AWARE. No mezclar.
7. **Lección 7**: cada loop con try/except por tick que registra (`BotState.record_error`)
   y sigue. Nada de `except: pass`.

## Workflow 1 — Diagnóstico de producción (SIEMPRE antes de tocar código)

1. No tenés acceso al container: pedile al operador el output de los scripts read-only
   (`scripts/diag_motor2_funnel.py`, `diag_motor2_match.py`, `check_portfolio.py`) o queries
   puntuales. Los snapshots persistidos (`Motor2FunnelSnapshot`, `RiskEvent`, logs greppables
   `motor2.funnel`, `[MOTOR 3 TP SHADOW]`) son la evidencia primaria.
2. Formulá el diagnóstico con discriminadores verificables ("si X entonces causa A; si Y,
   causa B") antes de proponer el fix. Si el hallazgo contradice el brief del operador,
   decilo con evidencia — el brief puede estar equivocado (pasó: "bug de settlement" que era
   sizing; "lookup de nombres" que era asks=100).
3. Para diagnósticos nuevos: script read-only en `scripts/` con docstring de uso, no parches.

## Workflow 2 — Cambio de lógica de trading (shadow-first)

1. Branch desde `origin/main` FRESCO (main se mueve rápido; hay varias sesiones).
2. Flag de DETECCIÓN separado del de EJECUCIÓN; default off o shadow. Logs
   `[... SHADOW] ... net=$` con fees reales (`kalshi_fee_cents`, ambos lados) para validar
   contra datos antes de prender.
3. Env var nueva: `utils/config.py` (Field + description) + `.env.example` + threading
   runner→componente. Tuneable en vivo > hardcodeado.
4. Tests obligatorios: mecanismo + control (el caso que NO debe disparar) + fail-safe.
5. `python -m pytest -q` completo verde + `ruff check src/ tests/` + `ruff format` antes
   de push. PR draft con: problema (evidencia), fix, verificación, y **limitaciones
   honestas** (qué NO resuelve).

## Workflow 3 — Activación de flags de ejecución (protocolo estricto)

Al proponer o acompañar CUALQUIER activación (`TRADING_ENABLED`, `MOTOR_X_EXECUTION_*`):
1. **Repetí los riesgos conocidos pendientes AUNQUE ya se hayan hablado** — enumerá los bugs
   P0/P1 abiertos y qué protege/no protege cada guard. (Regla nacida del incidente
   2026-07-07: proceder "como rutina" costó ~$140.)
2. Recomendá el colchón: sizing chico (`MAX_TRADE_SIZE_PCT` bajo), caps bajos
   (`MAX_EVENT_DIRECTIONAL_EXPOSURE_USD`), motores no involucrados en off.
3. Recordá el orden: el merge a main dispara AUTO-DEPLOY de Coolify (reinicia el container y
   borra el estado runtime). Si hay que frenar/pausar, `engage_kill_switch` ANTES del merge.
4. Checklist post-deploy: `curl /status` → `bot.is_paused` esperado (ojo: `capital.is_paused`
   es OTRO concepto — piso de capital), motores con el icono correcto en `/dashboard`, y los
   primeros logs del motor activado.

## Workflow 4 — Contención de incidente

1. Frenar: `docker exec -it kalshi-bot python -m scripts.engage_kill_switch "motivo"` (graba
   el switch DB + pausa el proceso vivo vía /admin/pause). Fallback: Stop en Coolify.
2. Verificar: `curl -s http://localhost:8080/status` → `bot.is_paused: true`.
3. Forense read-only: trades/positions/RiskEvents del período, timeline exacto, y separar
   pérdida realizada vs MTM vs proyectada al settle. NO "arreglar" datos de la DB.
4. Fix por bugs numerados con success criteria por bug; no reactivar hasta que los P0 estén
   mergeados y deployados. La reactivación sigue el Workflow 3.

## Reglas DURAS de código (globales — cada una tiene su factura)

1. **Cero IA en decisiones de trading.** El código que decide ejecuciones es 100%
   determinístico. Jamás un LLM en el hot path.
2. **DB: SQLModel puro** — `select(Model)` + `s.exec()`. PROHIBIDO `.query()` de
   SQLAlchemy (convención documentada en los módulos; no mezclar estilos).
3. **Timezones — la regla tiene DOS mitades, no una:** `settled_at`/`close_time` se
   escriben NAIVE UTC (`datetime.now(UTC).replace(tzinfo=None)`); `placed_at`/
   `commence_time` son AWARE UTC. **"Siempre naive" es un bug**, igual que mezclarlas.
   Nunca `datetime.utcnow()` (deprecado) ni `datetime.now()` a secas (local).
4. **Fees: SOLO `kalshi_fee_cents` (src/math/fees.py). JAMÁS la fórmula inline.**
   La oficial divide por `10_000`: `ceil(7·count·P·(100−P)/10_000)` con P en cents.
   ⚠️ La variante con `/1_000_000` ES el bug histórico de ~100× (fee en dólares
   etiquetado como cents; fee(100,50) daba 2 en vez de 175) que dividió la historia
   del bot en dos el 2026-07-01 — ha reaparecido en briefs: si un texto la trae,
   corregirlo, no copiarlo. Además el ceil es POR ORDEN: medir a count=1 sobreestima
   por contrato (para el edge fino, `fee(count_real, p)/count_real`).
5. **Riesgo: la fuente de verdad es `utils/config.py`**, no números en docs
   (defaults hoy: exposición simultánea 25%, sizing por trade 5% + cap absoluto
   $200, stop-losses max(capital×%, piso USD)). Duplicar los números en una skill
   crea la segunda matemática que ya nos costó el "796% fantasma".
6. **Asincronía: PROHIBIDO `asyncio.gather(..., return_exceptions=True)` en tasks
   críticas** (Lección 7). Supervisor pattern: try/except explícito por tick que
   registra (`BotState.record_error`) y SIGUE. Nada de `except: pass`.
7. **Nada sin tope** (facturas: 57GB de disco, OOM cada 75min, 20k créditos en
   días): todo buffer/caché/pending/tabla nace con tope + descarte documentado;
   toda request se dimensiona (chunking); toda API externa nace con caché TTL +
   breaker de cuota + costo por unidad en el Field. Detalle: el estado
   (caché/breaker) de un cliente que se recrea por ciclo va en CLASE, no instancia.
8. **Inspeccionar el CÓDIGO REAL antes de planificar.** Ningún plan ni diff se escribe
   desde docstrings, briefs o memoria de la arquitectura — se abre el archivo y se lee.
   Facturas: el brief que "faltaba una env var de M9" (no existía tal flag), el
   "hardcodeado" de M2 que era un default de Pydantic, y "M1 esquiva el piso" refutado
   leyendo `_check_pre_trade_locked`. Los briefs del operador y de otros agentes llegan
   con errores DOCUMENTADOS — incluida la fórmula de fees con `/1_000_000` (regla 4),
   que reapareció por TERCERA vez en un brief el 2026-07-31.

## Comandos de referencia

```bash
python -m pytest -q                      # suite completa (~1000+ tests, debe quedar verde)
ruff check src/ tests/ && ruff format src/ tests/
python scripts/diag_motor2_funnel.py     # por qué señales=0 (en el container)
python -m scripts.engage_kill_switch "x" # contención (en el container)
python -m scripts.clear_kill_switch      # reactivación (posiciones=0 + "CLEAR")
```

## Estilo del repo

- Commits y comentarios en **español**, el PORQUÉ con contexto de incidente ("Bug X,
  incidente AAAA-MM-DD: ..."), no el qué. Imports **absolutos** (`from src.módulo import ...`).
- Tests en `tests/` espejo de `src/` (pytest, asyncio auto) — no hay patrón `*.test.py`.
- PRs en draft; el operador mergea. No comentar PRs salvo necesidad real.
- Nada de tocar la DB de producción ni "arreglar" datos históricos; los análisis
  pre-2026-07-01 tienen fees ~100× subestimadas (edges inflados ~1pp).
