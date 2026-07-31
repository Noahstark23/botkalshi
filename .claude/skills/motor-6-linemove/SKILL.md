---
name: motor-6-linemove
description: Motor 6 — line-move follower. ⚫ ARCHIVADO 2026-07-23 (0 señales en toda su vida útil) — NO evaluar activación; usar solo como registro del diseño o si aparece una fila linemove real que amerite re-abrir. Nació de los datos del funnel 2026-07-12: los edges reales de M2 son transitorios y viven en los saltos de línea.
---

> ## ⚫ VEREDICTO FINAL: ARCHIVADO (2026-07-23, ratificado 2026-07-29)
> **0 filas `linemove` en toda su vida útil**, incluso tras el cableado real de #173 y con
> el feed sano post-#187. El fair no se mueve ≥3pp con Kalshi rezagado en este universo.
> La tesis murió con datos, no con opinión. **NO revivir sin una observación nueva MEDIDA**
> (una fila linemove real, no una intuición). El resto de esta skill queda como registro
> del diseño. OJO: un brief del 2026-07-31 lo describió como "CLV Momentum (comprar al
> abrir, vender T-30)" — eso NO es M6 (es territorio de M3); no re-etiquetar.

# Motor 6 — line-move follower

**Tesis (probada por datos, no por intuición):** el funnel de M2 (14 días) mostró que los
edges reales post-fix son **raros y TRANSITORIOS** — 5.63pp (07-05) y 3.20pp (07-07)
duraron UN ciclo. Nacen cuando las casas mueven la línea (lineups, noticias) y Kalshi
tarda ciclos en seguirla. M2 compara la **foto** (fair estático vs ask, exige 3pp de
descuento — raro en mercado eficiente); M6 explota la **película**: el fair saltó
≥`MOVE_MIN_PP` entre dos ciclos y el ask de Kalshi todavía no acompañó → comprar la
dirección del salto. Protocolo general: skill `botkalshi`.

## Arquitectura (F1) — pasajero de M2, cero API extra

```
src/strategies/motor_6_linemove/
├── detector.py   # find_linemove_signals — PURO (quotes + fair_now + fair_prev → señales)
└── shadow.py     # Motor6LineMoveShadow: memoria del fair previo + log + EdgeWindow
```

- Se engancha DENTRO de `Motor2ShadowPoller.poll_once` (param `linemove`), consumiendo el
  MISMO `(kalshi_events, fair_out)` del ciclo → **cero requests extra** a Kalshi/Odds API.
- El pre-match está garantizado aguas arriba (el fair de M2 solo existe para eventos que
  no arrancaron). El burst pre-kickoff de M2 (300s→60s) le da a M6 deltas más finos justo
  donde los moves ocurren — los dos features se potencian.
- **Doble best-effort**: `observe()` atrapa todo internamente Y el hook del poller tiene su
  propio try/except — M6 jamás puede romper el ciclo del host (Lección 7).
- La foto previa se actualiza SIEMPRE (incluso en error): un delta nunca abarca dos ciclos
  (un salto acumulado parecería un move falso).

## La señal y su banda

```
move_pp = (fair_now − fair_prev) × 100        # con signo
side    = YES si move ≥ +MOVE_MIN_PP; NO si move ≤ −MOVE_MIN_PP
net_pp  = prob_lado×100 − ask − fee(1, ask)   # edge neto post-fee vs el ask ACTUAL
señal   ⟺ EDGE_MIN_PP ≤ net_pp ≤ MAX_EDGE_PP
```

- El **piso** filtra moves que Kalshi ya digirió (ask acompañó → neto chico).
- El **techo** es el anti-fantasma heredado (2026-06-16: un "edge" de 132% era una quote
  stale, no un regalo). NUNCA quitarlo.
- Fees SIEMPRE con `kalshi_fee_cents` (fórmula oficial post 2026-07-01).

## Env vars

`MOTOR_6_LINEMOVE_ENABLED` (off) · `MOTOR_6_MOVE_MIN_PP` (3.0) · `MOTOR_6_EDGE_MIN_PP`
(2.0) · `MOTOR_6_MAX_EDGE_PP` (10.0).

## Fases (NO saltearlas — lección de TODOS los incidentes)

- **F1 SHADOW (lo construido):** no existe executor; el módulo ni importa el cliente de
  órdenes (test-guard `test_module_cannot_place_orders` lo verifica). Salidas: log
  `[MOTOR 6 SHADOW] ... net=$` con fees reales + `EdgeWindow kind="linemove"` (solo con
  odds LIVE — señales del fixture fake no son data).
- **F2 VALIDACIÓN (gate de datos, mínimo 1-2 semanas de F1):**
  1. Frecuencia: ¿cuántas señales/día? (esperado: pocas — si son decenas, sospechar
     fantasmas y auditar contra settlements).
  2. ROI simulado: cruzar `edge_windows kind='linemove'` contra settlements reales
     (¿el lado comprado ganó?). Query análoga a la de calibración del TP de M3.
  3. Ratio señal/fantasma: ¿cuántas señales vienen de moves REALES de línea vs artefactos
     (books parciales, re-fetch con casas distintas)? Mirar los logs de consenso.
  → Si ROI simulado ≤ 0 o los fantasmas dominan: se ajusta la banda o se ARCHIVA el motor.
     Archivar un motor no rentable es un resultado válido y barato en F1.
- **F3 EJECUCIÓN (solo tras F2 verde + decisión explícita del operador con riesgos
  repetidos):** executor IOC taker estilo M2 con TODOS los guards heredados: flag doble
  (`TRADING_ENABLED` && `MOTOR_6_EXECUTION_ENABLED` — Capa A), RiskManager
  (`check_and_reserve`), stake flat chico, one-per-event COMPARTIDO con M2 (¡mismo evento,
  misma dirección — no duplicar exposición!), `underdog_filter`, dedup cross-ciclo.

## Riesgos distintivos (los que M2 no tiene)

1. **Perseguir ruido del consenso:** un "move" puede ser un artefacto de MEDICIÓN (en dos
   ciclos el set de casas del consenso cambió → el fair salta sin que la línea se haya
   movido). Mitigación F2: auditar el ratio; posible fix: exigir mismo reference-set entre
   fotos. ESTE es el riesgo #1 del motor.
2. **Momentum tardío:** si Kalshi ya ajustó a mitad del move, comprás el pico. El piso
   `EDGE_MIN_PP` mitiga; F2 lo mide de verdad.
3. **Doble exposición con M2:** un move grande también puede disparar señal M2 sobre el
   mismo outcome. En F3 el one-per-event debe ser compartido entre ambos motores.
4. La frecuencia de detección depende del intervalo del poller: sin burst (300s), la
   mayoría de los moves se pierde entre fotos.
