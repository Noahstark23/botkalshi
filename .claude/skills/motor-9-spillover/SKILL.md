---
name: motor-9-spillover
description: Motor 9 — Derrame. ⚫ ARCHIVADO 2026-07-29 (el mid da +2.69¢ pero el EJECUTABLE da −2.42¢ neto: el spread se lo come) — NO escribir F3; usar al tocar el shadow que sigue midiendo mid-vs-exec. La tesis se decide con la tabla, no con intuición.
---

> ## ⚫ VEREDICTO FINAL: ARCHIVADO — el spread se lo come (2026-07-29)
> El derrame EXISTE en el mid (+2.69¢ a T+60, t=8.2, n=518) pero NO sobrevive al precio
> ejecutable: la serie `spillover_exec` (ask de entrada → bid de salida − fees roundtrip)
> dio **neto −2.42¢/contrato, t=−2.5, 19% positivos sobre n=43**. Mismo final que REST
> arb: detectado ≠ capturable. El trigger clave que lo confirmó: la huérfana de COLSD
> nació de un edge LEGÍTIMO de 7.14pp con slippage — no de un fantasma. **NO escribir su
> F3**; el shadow puede seguir midiendo (la DIFERENCIA mid-vs-exec es la medida del costo
> del spread en este universo). Revivir exige que la serie exec cambie de signo con n
> nuevo — no un re-cálculo de la misma muestra.

# Motor 9 — Derrame (spillover) entre hermanos del mismo evento

## Tesis (2026-07-18 — nace de la auditoría de rentabilidad)

En un evento multi-outcome la probabilidad se conserva: si el outcome A salta +8¢, sus
HERMANOS deben ajustar a la baja. **Si el ajuste llega con REZAGO, la ventana entre el salto
y el ajuste es capturable** comprando el lado correcto del hermano. Si el mercado ajusta
instantáneo, no hay nada — y el shadow lo MIDE, no lo supone.

Contexto: la auditoría 2026-07-18 dejó al proyecto sin fuente de alpha comprobada (M2 sin
edge, REST inejecutable, M6 mudo, M1 ruido). Las únicas búsquedas vivas son microestructura:
M8 (flujo) y este M9 (propagación). Ambos son instrumentos de medición, no promesas.

## Por qué es auto-validante (el corazón del diseño)

El mid del hermano se captura **EN el instante del trigger** (mid0). Lo que se mide después
(T+60/T+120) es exactamente lo que se movió DESPUÉS del salto — la parte capturable, por
construcción. Ajuste instantáneo/anticipado → follow ≈ 0 → la tesis se archiva sola.

## Mapa

```
src/strategies/motor_9_spillover/
├── detector.py   # SpilloverTracker PURO: historia de mids por ticker (deque con tope),
│                 #   salto = mid_now − mid_más_viejo_en_ventana; cooldown por ticker
└── shadow.py     # Motor9SpilloverShadow: trigger → mid0 de cada hermano → follow-through
                  #   T+60/T+120 firmado desde la dirección ESPERADA (inversa del salto)
                  #   → EdgeWindow kind="spillover". Cooldown por EVENTO (anti-cascada).
```

Wiring: pasajero del handler de deltas de `data_capture` (junto a M8, cero API extra).
Hermanos: `DataCaptureService._siblings_of` (tickers trackeados del mismo event_key).
OJO: el handler corre ANTES que el del V2 → el mid va UN delta rezagado (inmaterial para
saltos de ≥5¢ en 60s; documentado en el hook).

## Mapping EdgeWindow (reuso de campos del Motor REST, patrón M8)

- `kind="spillover"` · `market_ticker` = **HERMANO** (el candidato rezagado, donde se compraría)
- `edge_pct` = move del trigger en ¢ (firmado) · `count` = int(move, clamp ±99)
- `gross_spread_cents` = follow del hermano a T+60 (¢, **firmado desde la dirección esperada**)
- `magnitude_cents` = follow a T+120 · `leg_states` = `src=<sufijo del que saltó>` (forense)

Follow **> 0** = el hermano ajustó como la conservación predice, DESPUÉS del trigger
(derrame rezagado = capturable). **~0** = instantáneo/sin propagación. **< 0** = al revés.

## Config (todo tunable por env)

- `MOTOR_9_SPILLOVER_ENABLED` (default false) — F1: solo observa y mide.
- `MOTOR_9_TRIGGER_MOVE_CENTS` (5.0) · `MOTOR_9_WINDOW_SEC` (60) — qué es "un salto".
- `MOTOR_9_COOLDOWN_SEC` (300) — por ticker Y por evento (el ajuste del hermano dispararía
  un trigger espejo; medir el eco contaminaría la muestra).
- Requiere el manager V2 activo (mids en memoria); books stale/cuarentena → drop, no basura.

## Gate F2 (escrito de antemano — decidir con la tabla)

> **CERRADO**: el gate del MID dio VERDE el 28-jul (+2.69¢, t=8.2) y por eso se
> construyó el gate EJECUTABLE (`spillover_exec`, ask+fees) — que dio ROJO el 29-jul
> (−2.42¢ neto, t=−2.5). El veredicto de arriba manda; esto queda como registro de
> cómo se decidió.

Con **n ≥ 30 mediciones**: leer con `scripts/diag_edge_shadow.py` (sección spillover).
- follow60 medio **> +1¢ con t-stat > 2** → derrame rezagado real → diseñar F3 (executor
  Capa A doble-flag, one-per-event COMPARTIDO con quien toque el mismo evento, stake flat).
- follow60 medio **~0** → el mercado ajusta instantáneo → **archivar (resultado válido, ~$0)**.
- follow60 medio **< 0** → tesis invertida; NO operar "contrarian" sin re-derivar por qué.

## Riesgos distintivos (repetir SIEMPRE al proponer F3)

1. **El hermano puede ser el LÍDER**: si B ya se movió y A es el eco, comprar B es comprar
   el pico. El cooldown por evento mitiga el eco, no elimina la ambigüedad de quién lideró.
2. **Books finos**: el follow medido en el mid puede no ser ejecutable al ask (la lección
   del Motor REST: edge en detección ≠ edge capturable). F3 exigiría medir contra el ask.
3. **Mid un delta rezagado** (orden de handlers) — documentado, inmaterial al umbral actual.
4. Todo lo transversal: fee real `kalshi_fee_cents` a ambas puntas, techo anti-fantasma.

## Fases

- **F1 (actual)**: shadow auto-validante. El módulo NI IMPORTA el cliente de órdenes
  (`test_module_cannot_place_orders`).
- **F2**: veredicto con `diag_edge_shadow.py` sobre n≥30. Archivar si no rinde.
- **F3**: SOLO con F2 verde + decisión explícita del operador (riesgos repetidos).
