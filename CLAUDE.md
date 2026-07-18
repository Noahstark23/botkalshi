# botkalshi — bot de trading algorítmico sobre Kalshi

Bot 24/7 con **dinero real** sobre mercados de predicción de Kalshi (deportes principalmente).
Corre en un container de Coolify (auto-deploy al mergear a main); la config vive en env vars
(Pydantic `Settings`, fail-fast al arranque). SQLite (`/app/data/trades.db`) como única DB.
**Todo error acá cuesta plata.**

## Estructura del proyecto

```
src/
├── runner.py                    # ProductionRunner: orquesta TODAS las tasks (una por servicio)
├── clients/
│   ├── kalshi_rest.py           # REST auth firmada (get_balance/get_event/place_order/…)
│   ├── kalshi_ws.py             # WebSocket con reconexión + suscripciones re-aplicables
│   └── odds_api.py              # The Odds API (commence_time AWARE UTC)
├── math/
│   ├── arbitrage.py             # detect_binary_arb / detect_multi_outcome_arb
│   ├── fees.py                  # kalshi_fee_cents — fórmula OFICIAL (corregida 2026-07-01;
│   │                            #   ceil POR ORDEN: medirla a count=1 sobreestima por contrato)
│   ├── kelly.py                 # quarter_kelly (M2 lo abandonó → flat sizing)
│   └── no_vig.py                # implied_prob / remove_vig_multiplicative
├── risk/
│   └── manager.py               # RiskManager: capital dinámico (cash real × factor, piso/techo,
│                                #   hard-cap $5k), stop-losses max(capital×%, piso USD), breach
│                                #   diario SOFT (auto-recupera al rollover UTC), kill-switch
├── storage/
│   ├── models.py                # Trade / PortfolioPosition / RiskEvent / EdgeWindow (kind=
│   │                            #   binary|multi_outcome|consensus|linemove) / Motor2Funnel-
│   │                            #   Snapshot / OperationalState (kill-switch) / auto_vacuum
│   ├── maintenance.py           # retención de diagnóstico + wal_checkpoint + incremental_vacuum
│   └── disk_guard.py            # DiskGuard: lazo CERRADO de disco (warn→poda; critical→
│                                #   descarta telemetría; trading JAMÁS se gatea)
├── strategies/
│   ├── data_capture.py          # discovery + feed WS + host del OrderbookManagerV2
│   ├── fair_value_book.py       # canal fair M2 → M5/M6 (publish por ciclo, solo odds live)
│   ├── motor_1_arbitrage/       # M1: arb binario intra-ticker (VER VEREDICTO ABAJO)
│   │   └── orderbook_manager_v2.py  # recovery WS + circuit breaker + CUARENTENA en desync
│   │                                #   (mark_stale + recovery; nunca servir un book divergido)
│   ├── motor_2_consensus/       # M2: direccional vs consenso no-vig; funnel persistido;
│   │                            #   burst polling pre-kickoff; fee del edge al count real (flag)
│   ├── motor_3_clv/             # M3: SALIDAS (TP/trailing/T-30); jamás vende pata de hedge
│   ├── motor_5_mm/              # M5: market maker (F1 shadow); quotes RESTING = su riesgo
│   ├── motor_6_linemove/        # M6: line-move follower (F1 shadow); pasajero del ciclo M2
│   └── motor_rest_arb/          # REST: arb multi-outcome (universo TUNABLE por env) +
│                                #   SettlementPoller (settlea TODOS los motores)
├── analytics/                   # daily_pnl / analyst_loop / shadow_fee_recalc
└── monitoring/
    ├── health.py                # FastAPI /status + BotState (estado runtime compartido)
    ├── command_center.py        # Telegram C1 READ-ONLY: /incidentes /salud /funnel /pnl
    │                            #   /posiciones /disco (línea roja: nada muta por chat)
    ├── dashboard.py             # /dashboard + dispatch del centro de comando
    └── telegram_alerts.py       # send_alert + alert_* (best-effort SIEMPRE)

scripts/                         # read-only + operación (correr EN el container salvo indicación)
├── disk_triage.py               # triage de disco (default instantáneo; --bytes escanea)
├── rebuild_db.py                # reclaim one-time (bot PARADO; verifica sagradas; no swapea)
├── host_janitor.sh              # cron del HOST: prune Docker + logs (JAMÁS --volumes)
├── clear_kill_switch.py         # ÚNICA forma de levantar el kill-switch persistente
└── diag_motor2_funnel.py        # veredicto de señales=0 (DB gratis / --live gasta cuota)
tests/                           # pytest espejo de src/ (~1175+, asyncio auto)
.claude/skills/                  # botkalshi (protocolo) + una skill POR MOTOR + operacion-disco-db
                                 # + riesgo-capital + monitoreo-alertas — LEER la del área tocada
```

## Los motores — reglas claras y veredictos CON DATOS

| Motor | Qué hace | Veredicto vigente (no re-litigar sin datos nuevos) |
|---|---|---|
| M1 arb binario | yes+no mismo ticker < $1 (WS) | ⚫ **No es alpha (auditoría 2026-07-18).** +$33.63 en jun→jul sobre 517 trades = +$0.065/trade, win 48.9%: ruido alrededor de cero. Y el +$33 es UN día — el incidente 07-07 (186 trades liquidando favorable); sin él, +$3.7, y las 2 últimas semanas gotean negativo (07-09/12/18 todos en rojo). 0 ventanas binarias en TODA la historia (requiere book auto-cruzado). **Vigilar, NO proteger; su volumen es riesgo operativo por retorno cero.** |
| M2 consenso | direccional vs fair no-vig de sportsbooks | 🔴 **SIN edge estructural (auditoría 2026-07-18): −$432.95, el 100% de la sangría del proyecto.** El edge techa en 0.15pp (p99=0.13pp) sobre 35.129 detecciones — CERO por encima de 2pp — contra un umbral de 3pp: el consenso de sportsbooks es idéntico a Kalshi, no hay ineficiencia. Win 54% pero avg_loss ≫ avg_win. No se arregla con código → **apagar entradas (`MOTOR_2_ENTRY_EXECUTION_ENABLED=false`)**. Flags: `MOTOR_2_SPORTSBOOK_ENABLED` (corre) / `_ENTRY_EXECUTION_ENABLED` (ENTRADAS) / `_EXECUTION_ENABLED` (solo VENTAS) — **no confundirlos**. |
| M3 CLV | NO abre — cierra (TP/trailing/T-30) | ✅ Estable. Regla de oro: jamás vender la pata de un hedge (atribución estricta). |
| M5 MM | quotes GTC post_only alrededor del fair | F1 shadow. Riesgo distintivo: quote RESTING sin gestión. No saltear fases. |
| M6 line-move | compra la dirección del salto del consenso que Kalshi no digirió | ⚫ **MUDO (auditoría 2026-07-18): 0 filas `linemove` en un mes** — el fair no se mueve ≥3pp con Kalshi rezagado en este universo. Candidato a archivar. (Nota: recién cableado real 2026-07-17 vía #173; darle una ventana corta antes de sepultar, pero la tesis luce muerta.) |
| REST arb | multi-outcome winner-take-all ≥3 patas + settlement | ⚫ **INEJECUTABLE (auditoría 2026-07-18): edge real 3.13pp en detección, pero 73% rollback (11/15) — Caso A confirmado por `leg_states`.** El FOK no llena 3-4 patas atómicamente en books finos (no hay depth simultáneo); el hard-first (#85) protege 1 pata, las baratas se disparan en paralelo sin protección mutua. La pérdida en DB (−$31.89) SOBREESTIMA la real (registro pesimista a 1¢), pero el motor no puede capturar su edge. **Ningún diff lo arregla; apagar ejecución (`MOTOR_REST_EXECUTION_ENABLED=false`).** Path binario ya estaba muerto; ahora el multi también. |

**Regla transversal:** un motor selectivo que casi no opera con edge negativo ESTÁ funcionando
bien (186 trades/6min fue el incidente, no el objetivo). El sizing lo gobierna el capital
DINÁMICO (cash real de Kalshi × factor) — `ACTIVE_CAPITAL_USD` es solo fallback de boot.

## Arquitectura de seguridad (NO debilitar jamás)

1. **Capa A** — el executor de cada motor SOLO se construye con `TRADING_ENABLED` &&
   `MOTOR_X_EXECUTION_ENABLED`. En shadow es `None`: estructuralmente incapaz. En F1 de un
   motor nuevo se lleva al extremo: el módulo NI IMPORTA el cliente de órdenes (test-guard).
2. **Capa C** — `place_order` bloquea `buy` con `TRADING_ENABLED=false`. Los `sell` NO los
   frena → la Capa A es la única protección de los motores de salida.
3. **Kill-switch persistente** (`operational_state`) — sobrevive redeploys; el boot re-hidrata.
   SOLO se levanta con `clear_kill_switch.py`. No auto-resetea por diseño.
4. **Stop-losses a escala** — límite = `max(capital×%, piso USD)` (pisos 20/40/60: con capital
   chico el % puro daba $5.40/día = ruido que apagaba todo). Breach DIARIO = soft (pausa
   entradas, auto-recupera al rollover UTC); semanal/mensual = kill-switch persistente. Orden
   de evaluación mensual→semanal→diario (lo severo nunca queda tapado por lo soft).
5. **Cuarentena de books** — un desync (`new_qty<0`) marca el book stale + recovery del sid.
   NUNCA servir precios de un book divergido (2026-07-12: precios fantasma → fills parciales
   → rollbacks → breaker; 9 manual_resume fueron parches sin causa raíz).
6. **DiskGuard (lazo cerrado)** — warn <5GB: alerta+poda; critical <2GB: descarta TELEMETRÍA.
   El estado de trading jamás se gatea. + retención 6h + janitor del host por cron.
7. **Guards de entrada** — M1: pre-check balance + cap direccional por EVENTO. M2: one-per-
   event, banda (min,max], techo anti-fantasma, underdog filter. Arbs hedged netean por arb_id.
8. **Telegram** — Nivel 0 read-only (`command_center.py`). Línea roja: kill-switch, flags de
   ejecución y órdenes JAMÁS van por chat. Chats no autorizados: silencio total.

## Reglas de ESCALABILIDAD (cómo se agrega el Motor N)

Patrón validado con el Motor 6 — seguirlo SIEMPRE:

1. **Tesis con datos primero.** El motor nace de una observación medida (funnel, EdgeWindow,
   settlements), no de una intuición. Si no hay dato que la respalde, instrumentar primero.
2. **F1 SHADOW estructural**: paquete propio `motor_N_*/` con detector PURO (sin red/DB) +
   shadow que loguea `[MOTOR N SHADOW] net=$` con fees reales y persiste `EdgeWindow
   kind="<propio>"` SOLO con datos live. El módulo NO importa el cliente de órdenes y un
   test-guard (`test_module_cannot_place_orders`) lo hace cumplir.
3. **Pasajero antes que fetch nuevo**: si otro motor ya trae los datos (quotes, fair, odds),
   engancharse a su ciclo con doble best-effort (el pasajero JAMÁS rompe al host). Cero
   requests extra hasta que F2 justifique lo contrario.
4. **F2 = gates de datos escritos de antemano**: frecuencia de señales, ROI simulado contra
   settlements, ratio señal/fantasma. **Archivar un motor que no rinde es un resultado
   válido y barato** — el costo de F1 es ~$0.
5. **F3 solo con F2 verde + decisión explícita del operador (riesgos repetidos)**: executor
   con Capa A doble-flag, `RiskManager.check_and_reserve`, stake flat chico, techo
   anti-fantasma, y **one-per-event COMPARTIDO** con los motores correlacionados (dos motores
   sobre el mismo evento no duplican exposición).
6. **Config, no hardcode**: todo umbral/universo en `utils/config.py` (Field con description)
   + `.env.example` + threading runner→componente. Tuneable en vivo > redeploy.
7. **Skill propia** en `.claude/skills/motor-N-*/` con: tesis, mapa, banda, fases, gates y
   riesgos distintivos. La sesión futura no debe redescubrir nada.
8. **Telemetría con presupuesto**: toda tabla nueva de diagnóstico entra a `_RETENTION_DAYS`
   (maintenance) y respeta `DiskGuard.diagnostics_allowed()`. Nadie escribe sin tope
   (orderbook_events llenó 57GB).

## Cómo debe trabajar Claude (Fable 5) en este repo

**Principios no negociables:**

1. **VERIFICAR ANTES DE IMPLEMENTAR.** Los briefs (del operador, de otros agentes, de tools)
   llegan desactualizados o equivocados con frecuencia documentada: flags "faltantes" que ya
   estaban, fixes ya mergeados, causas raíz erradas. Leer el código y los datos REALES
   primero; si el hallazgo contradice el brief, decirlo con evidencia. Dos agentes pueden
   tener razón sobre momentos distintos — reconciliar por línea de tiempo antes de acusar.
2. **Diagnóstico read-only ANTES de tocar código.** Funnel snapshots, `edge_windows`,
   RiskEvents, logs greppables, centro de comando de Telegram, scripts diag_*. No hay acceso
   directo al container: pedir outputs al operador con comandos exactos.
3. **Decisiones de trading con NÚMEROS.** Cambiar un umbral/banda exige la distribución
   medida (ej.: no bajar `MIN_EDGE` si el máximo histórico es negativo — no produciría nada
   más que riesgo). Las decisiones ya tomadas con datos no se re-litigan sin datos nuevos.
4. **Al proponer una ACTIVACIÓN, repetir SIEMPRE los riesgos conocidos** aunque ya se hayan
   hablado, y recomendar colchón (sizing chico, cap bajo, motor en off). Lección 2026-07-07:
   proceder "como rutina" con bugs P0 conocidos costó ~$140 reales.
5. **Nunca vender la pata de un hedge.** Atribuir origen y excluir arbs con ambas patas vivas.
6. **Fail-safe direccional:** LECTURA falla abierta (un hiccup no apaga el bot — DiskGuard,
   balance); VENTA/CIERRE falla cerrada (ante la duda, NO vender); un book CORRUPTO falla
   cerrado (cuarentena: mejor ciego que fantasma).
7. **Lección 7:** cada loop registra (`BotState.record_error`) y SIGUE; nada de `except: pass`.
   Los pasajeros de un ciclo (M6 en M2) llevan doble best-effort.
8. **Convenciones:** `settled_at`/`close_time` NAIVE UTC; `placed_at`/`commence_time` AWARE
   UTC — no mezclar. Dinero en cents enteros. Fees SIEMPRE `kalshi_fee_cents` (pre-2026-07-01
   estaba ~100× subestimada: todo análisis histórico previo tiene edges inflados).
9. **SQLite en producción:** DELETE no achica el archivo; VACUUM necesita ~2× disco; dbstat
   escanea todo (colgó con 57GB); `mode=ro` puede no ver el WAL sin checkpoint; tras swap de
   DB como root → `chown 1000:1000` o "readonly database". Runbook completo: skill
   `operacion-disco-db`.

**Flujo de trabajo:**

- **Leer la skill del área ANTES de tocar** (`.claude/skills/`): protocolo general
  (`botkalshi`), la del motor, o la transversal (disco/riesgo/monitoreo).
- Branch por cambio desde `origin/main` FRESCO (main se mueve rápido: varias sesiones).
- Tests para todo: mecanismo + control (lo que NO debe disparar) + fail-safe. Suite completa
  verde + `ruff check` + `ruff format` antes de push.
- Commits/comentarios en español: el PORQUÉ con contexto de incidente, no el qué.
- PR draft con: problema (evidencia), fix, verificación, y **limitaciones honestas** (qué NO
  resuelve). Nuevas features de trading: shadow-first, flag default off.
- No tocar la DB de producción; scripts nuevos read-only y con docstring de uso.

## Incidentes clave (contexto para decisiones)

- **2026-05-28** — recovery WS atascada → books stale. Fixes: routing por sid, circuit breaker
  de recovery, no aplicar deltas sobre stale.
- **2026-06-28** — M2 −$390: sin brazo de salida, doble exposición, Kelly sobre edges falsos,
  fair inflado → exit_engine, one-per-event, flat sizing, consenso des-inflado.
- **2026-07-01** — fee oficial corregida (~100×) + fix semanal en borde de mes. **Divide la
  historia en dos**: los "edges" de 43-64pp pre-fix eran artefactos (0/8 trades ganados).
- **2026-07-07** — M1 186 trades/6min; huérfanas + residual direccional (~$135) + pausa que un
  redeploy borraba → Bugs 1-4 (pre-check balance, EventExposureTracker, kill-switch
  persistente al 1er rollback abortado, M3 gestiona huérfanas).
- **2026-07-10** — disco lleno: `orderbook_events` sin tope (57GB) + WAL + cruft de Docker en
  el host → gate PERSIST default off, retención, DiskGuard, rebuild_db, janitor. DELETE no
  libera disco; el fix fue reconstruir.
- **2026-07-12** — (a) breaker de M1 por fills parciales: books desincronizados servían
  precios fantasma; 9 manual_resume sin causa raíz → cuarentena en desync. (b) stop-loss
  diario $5.40 con capital $180 = ruido que apagaba todo → pisos USD + breach diario soft.
  (c) auditoría de motores: M1 sin señal, M2 dormido por flag de ENTRADAS, REST con universo
  hardcodeado → los veredictos de la tabla de motores.
- **2026-07-17/18** — (a) OOM crash-loop (container 1GB al 99.99%, kill cada ~75min):
  `_bootstrap_buffer` por ticker SIN TOPE — el sid grande cuyo `get_snapshot` masivo se
  dropeaba dejaba 223 books sin inicializar y el feed llenaba la RAM. Fix triple: chunking
  del recovery (request masiva → lotes de 50), cap `deque(maxlen)` del bootstrap buffer, y
  observabilidad de V2 en `/status` (`sids_disabled`/`bootstrap_capped_tickers`). Misma
  lección que el disco: nada sin tope. (b) **AUDITORÍA DE RENTABILIDAD jun→jul (PnL real,
  no detección)** → los veredictos ⚫/🔴 de la tabla. Neto del mes: **−$431, el 100% es M2**.
  Probado con SQL, no sugerido: M2 edge techa 0.15pp/35k muestras (sin edge); M1 +$33 es
  el incidente 07-07 y gotea negativo desde (no alpha); REST 73% rollback por `leg_states`
  Caso A (edge real, inejecutable en books finos multi-pata); M6 0 señales/mes (mudo); M8
  única promesa viva (p50 +3.18pp, n=130, recién destapado en mercados líquidos con el fix
  del sid=1). **Conclusión honesta: hoy el proyecto no tiene fuente de alpha comprobada;
  apagar M2+REST lleva de −$431 a ~$0 (parar la hemorragia, no rentabilidad). La única
  hipótesis viva es M8 — se decide con más muestra, no con un diff.** Queries que lo prueban:
  PnL/motor por día (`GROUP BY strategy, date(settled_at)`), distribución de edge de M2
  (`motor2_funnel_snapshots`), y `SELECT leg_states FROM edge_windows WHERE kind='multi_outcome'`
  (0 ERROR_RED = descarta bug de red; todo FILL/KILL = books finos).
