# Motor 4 — Arbitraje cross-venue Kalshi ↔ Polymarket · Diseño **Fase 0 (read-only)**

> **Estado:** insumo de diseño. NO implementa nada. Fase 0 es **solo medición read-only**:
> cero cripto, cero wallet, cero capital, cero órdenes en ningún venue.
>
> **Para qué sirve este doc:** decidir **con datos** si Motor 4 vale la pena *antes* de
> construir infra de ejecución on-chain. Las dos preguntas que Fase 0 responde:
> 1. ¿Cuántos arbs reales aparecen y de qué magnitud **neta** (post fees/gas/slippage)?
> 2. ¿"El mismo evento" **resuelve igual** en los dos venues? (el número que mata o aprueba el motor)

---

## 1. La tesis (por qué un segundo venue)

Intra-Kalshi el arbitraje es **raro**: un solo libro tiende a ser eficiente (por eso Motor
REST casi no encuentra cruces). **Dos libros independientes** (Kalshi y Polymarket) sí
divergen de verdad sobre el mismo evento.

Valor estratégico para el proyecto:
- **Baja varianza:** un arb es profit *lockeado* cuando funciona (uno de los dos lados paga
  $1), a diferencia de Motor 2 que es direccional (+EV pero pierde apuestas individuales).
- **No correlacionado con Motor 2:** gana aunque Motor 2 tenga una mala semana → **suaviza
  el ingreso mensual**, justo lo que hace falta para un objetivo estable.

> Combinar un motor +EV de volumen (Motor 2) con un motor de arb de baja varianza (Motor 4)
> es la receta clásica para estabilizar el P&L. Pero **solo si las trampas de abajo se miden
> primero.**

---

## 2. El mecanismo (qué es Motor 4)

Ambos venues listan mercados **binarios YES/NO** sobre el mismo outcome. Si:

```
comprar YES en el venue barato  +  comprar NO en el otro venue  <  $1  (neto de costos)
```

→ **profit lockeado**: pase lo que pase, uno de los dos contratos paga $1 y pagaste menos.

Reusa la matemática que ya existe (`src/math/arbitrage.py::detect_binary_arb`), pero **las
dos patas viven en venues distintos** → no hay ejecución atómica (ver §6 y §11).

---

## 3. Polymarket — lo que importa para Fase 0

- Mercados **binarios** (tokens YES/NO), precio en **USDC 0–1** (dólares, no centavos), sobre
  Polygon.
- **CLAVE:** los precios y el order book son **públicos** (Gamma API / CLOB API) → se **LEEN
  sin wallet ni cripto**. Eso es lo que hace posible una Fase 0 read-only de verdad.
  `[requiere verificación]` endpoints exactos (Gamma `gamma-api.polymarket.com` vs CLOB
  `clob.polymarket.com`), rate limits, y shape del order book.
- Identificadores: `condition_id` por mercado, `token_id` por lado (YES/NO).
- Diferencias con Kalshi a normalizar: **precio 0–1 USDC** (×100 → centavos), **fee distinto**
  (`[requiere verificación]`: Polymarket históricamente 0% trading fee — confirmar el actual),
  y **settlement on-chain** (irrelevante en Fase 0, crítico en Fase 1+).

---

## 4. Arquitectura Fase 0 (read-only, shadow)

Reusa los patrones ya probados del proyecto. Nada nuevo conceptualmente:

| Componente | Qué hace | Reusa de |
|---|---|---|
| **`PolymarketSource`** (nuevo) | Pollea precios públicos de Polymarket → `{evento → outcomes con bid/ask USDC}`. **NO toca wallet.** | El patrón de `sources.py` de Motor 2 (fuentes pluggables) |
| **Cross-venue matcher** | Empareja evento/outcome Kalshi ↔ Polymarket | `normalize_name` + `TEAM_ALIASES` del matcher de Motor 2 |
| **Cross-venue arb detector** | Por par matcheado, computa el arb en las dos direcciones + aplica modelo de costos | `detect_binary_arb` (adaptado a unidades) |
| **Shadow recorder** | Graba cada oportunidad en una tabla nueva `CrossVenueWindow` (análoga a `EdgeWindow`) | El patrón de grabación shadow de todos los motores |
| **Runner task** | Loop supervisado (Lección 7: sin `gather(return_exceptions)`), gateado por `MOTOR_4_ENABLED` (default `False`) | `_run_motorX` de los otros motores |

**Invariante estructural (como en todos los motores):** en Fase 0 **no se construye ningún
path de ejecución** — no existe. El motor solo lee, computa y graba.

---

## 5. La matemática del arb cross-venue

Normalizar ambos venues a **centavos** (probabilidad × 100):

- Kalshi: `yes_ask_cents`, `no_ask_cents` (o el ask sintético por complemento, igual que Motor
  REST: `no_ask = 100 − yes_bid`).
- Polymarket: `yes_ask_usdc × 100`, `no_ask_usdc × 100`.

Hay **dos** combinaciones cross-venue por par:
```
A) YES@Kalshi + NO@Polymarket   < 100 − costos_totales  → arb
B) YES@Polymarket + NO@Kalshi   < 100 − costos_totales  → arb
```
La profundidad ejecutable = `min(depth de las dos patas)` (idéntico a `detect_binary_arb`).
Se toma la mejor de A/B si alguna supera el umbral neto.

---

## 6. El riesgo #1: **resolución** (lo que Fase 0 DEBE medir)

> **"Mismo evento" NO garantiza "misma resolución".** Este es el asesino del arb cross-venue
> y la razón principal de que Fase 0 sea solo medición.

Casos donde el arb "sin riesgo" se vuelve **pérdida doble**:
- Partido suspendido/postergado: un venue anula (devuelve), el otro resuelve.
- Criterios distintos: tiempo extra, penales, "ganador del partido" vs "ganador incl.
  prórroga".
- Fuente oficial de resolución distinta entre venues.

Si un venue paga YES y el otro paga NO sobre lo que creías el mismo evento → compraste **los
dos lados perdedores**. No es arb, es −100%.

**Lo que Fase 0 captura:** por cada par matcheado, guarda el criterio de resolución de cada
venue, y **post-evento audita si resolvieron igual**. Ese **resolution-match-rate** es el
número que aprueba o mata Motor 4.

**Mitigación de diseño:** empezar SOLO por eventos de **resolución objetiva y compartida**
(resultado de un partido del Mundial / MLB) y **evitar** los ambiguos (métricas, in-play,
"antes de tal fecha", política con criterios difusos).

---

## 7. Modelo de costos (para el umbral neto)

Una oportunidad solo cuenta como arb si `magnitud_neta > suma de TODOS los costos + margen`:

- **Fee Kalshi:** `kalshi_fee_cents` (ya existe en el repo).
- **Fee Polymarket:** `[requiere verificación]` (históricamente 0%, confirmar).
- **Gas (Polygon):** bajo pero no cero; por orden on-chain.
- **Slippage:** los libros de Polymarket pueden ser más finos → la profundidad real importa.
- **Latencia:** dos venues + on-chain → la ventana puede cerrarse antes de fillear **ambas**
  patas. Fase 0 **mide cuánto duran las ventanas** para saber si son ejecutables.

---

## 8. Qué NO hace Fase 0 (invariantes de seguridad)

- ❌ NO wallet, NO USDC, NO firmas on-chain, NO órdenes en **ningún** venue.
- ❌ NO toca el capital de Kalshi ni `TRADING_ENABLED`.
- ✅ `MOTOR_4_ENABLED` default `False` → **no-op total en prod** (no toca nada).
- ✅ Es puramente: **leer precios públicos** de ambos venues + computar + grabar en SQLite.

→ Riesgo operativo de Fase 0 ≈ **cero** (es un lector + una tabla).

---

## 9. Entregables de Fase 0 (los números de decisión)

Tras N semanas de shadow, la tabla `CrossVenueWindow` + un script de análisis dan:

1. **Frecuencia:** ¿cuántos arbs netos > umbral por día/semana?
2. **Magnitud:** distribución del edge neto (¿son 0.5¢ o 5¢ por contrato?).
3. **Duración de ventana:** ¿hay tiempo real de fillear las dos patas?
4. **Resolution-match-rate:** de los eventos resueltos, ¿qué % resolvió **igual** en ambos
   venues? **(el número crítico)**
5. **Cobertura de matching:** ¿cuántos eventos se logran emparejar cross-venue?

---

## 10. Gate de decisión Fase 0 → Fase 1

Avanzar a construir ejecución **SOLO si**:
- Arbs **frecuentes** (no 1 por mes), **Y**
- Magnitud neta que **cubre costos + margen de latencia**, **Y**
- **Resolution-match-rate ≈ 100%** en la categoría elegida.

Si el resolution-match-rate es < ~100% → **STOP**, o restringir el motor a la única categoría
donde sí matchea. Mejor no construir Motor 4 que construirlo sobre un arb fantasma.

---

## 11. Fricciones que Fase 0 NO resuelve (para Fase 1+)

- **Infra cripto:** wallet, USDC, gas, firmas on-chain, manejo de llaves.
- **Capital partido** entre venues + rebalanceo USD ↔ USDC.
- **Regulatorio / geo:** Polymarket + usuarios US es legalmente complicado — **verificar
  legalidad/ToS para tu jurisdicción ANTES de Fase 1.** No es un detalle técnico.
- **Ejecución no atómica:** igual que el problema de Motor REST, pero **peor** (dos venues,
  uno on-chain con latencia). Necesitará un guardarraíl tipo **hard-leg-first** (#85)
  adaptado: asegurar la pata más lenta/cara (la on-chain) primero.

---

## 12. Preguntas abiertas `[requiere verificación]`

- Endpoints exactos de Polymarket (Gamma vs CLOB), shape del order book, rate limits.
- Fee real de Polymarket hoy.
- Geo/legal para la jurisdicción del operador.
- Cómo mapear `condition_id` de Polymarket ↔ `ticker` de Kalshi (¿por nombre de evento +
  matcher de equipos? ¿tabla manual al principio para deportes?).

---

## 13. Roadmap posterior (solo si Fase 0 aprueba)

1. **Fase 1:** infra de lectura on-chain + wallet (read-only primero).
2. **Fase 2:** ejecución con guardarraíl cross-venue (hard-leg-first adaptado) en demo / capital
   chico.
3. **Fase 3:** capital real, escalado.

---

## 14. Timing (la regla que no se negocia)

**No empezar Motor 4 hasta que el lado Kalshi esté probado rentable.** Hoy Motor 2 ni siquiera
está operando (falta redeployar el fix del endpoint V2) y no tiene una semana de P&L probado.
Construir un segundo venue + estrategia nueva + cripto antes de validar el primero es
multiplicar superficie de bug antes de saber si la base funciona.

**Orden sano:** (1) Motor 2 operando y validado semanas → (2) exprimir el Mundial → (3) recién
ahí ejecutar **esta Fase 0** (que es read-only y de bajo costo) → (4) decidir con sus números
si se construye la ejecución.

> Este documento queda listo en el cajón para el paso (3).
