# Estrategias del bot — cómo funciona cada motor y cómo hace dinero

> Mapa de referencia de las estrategias de `botkalshi`. Estado al **2026-06-16**.
> Pensado para releer y orientar decisiones (qué encender, dónde está el dinero).

## Reality check: qué hay construido de verdad

| Motor | ¿Construido? | Estrategia | Estado |
|---|---|---|---|
| **Motor 1** (`motor_1_arbitrage`) | ✅ sí | Arbitraje (riesgo casi nulo) | shadow |
| **Motor REST** (`motor_rest_arb`) | ✅ sí — el arb más desarrollado | Arbitraje (binario + multi-outcome) | shadow, auditado |
| **Motor 2** (`motor_2_consensus`) | ✅ sí | Direccional +EV vs casas | shadow, auditado — **MLB conectado** |
| **Motor 3** (CLV) | ❌ **NO existe** | Closing Line Value | solo el flag `MOTOR_3_CLV_ENABLED`, sin código |

En la práctica son **2 estrategias reales** (arbitraje + consenso) repartidas en 3 motores construidos. Motor 3 es una idea anotada, nada más.

Todo corre en **shadow puro** hoy: `TRADING_ENABLED=false` + muro de 3 capas (executor solo con trading on; shadow nunca ejecuta; `place_order` bloquea entradas con el flag off). Cero capital expuesto por diseño.

---

## 💰 Estrategia 1 — ARBITRAJE (Motor 1 + Motor REST)

**Cómo funciona:** comprar **todos los resultados** de un mercado cuando sus precios suman **menos de 100¢**. Como exactamente uno paga 100¢, la diferencia es ganancia fija.

> Ejemplo 1X2: comprar Gana/Empata/Gana a 32+33+30 = 95¢ → pase lo que pase cobras
> 100¢ → **+5¢ garantizados** (menos fees).

**Cómo hace dinero:** del **spread**, no de predecir. El pago es fijo sin importar el resultado → riesgo direccional **cero** si la orden llena.

**Lo malo:**
- Edge **fino** (~2% neto; las fees se comen ~4% del bruto).
- Oportunidades **escasas y efímeras** — el Mundial las inflaba; al acabar, casi desaparecen.
- Motor 1 = solo binario (yes+no del mismo mercado, vía WebSocket). **Motor REST** = binario **y** multi-outcome (1X2/winner, WS-detección + REST-ejecución) — es el que generó las 653 ventanas ejecutables del Mundial.

**Salvaguardas:** cap anti-fantasma `MOTOR_REST_MAX_EDGE_PCT` (un edge >10% en un 1X2 es casi siempre una pata sin precio / mercado a medio resolver → se descarta), check de frescura de quotes, grupo completo.

**Estado:** auditado (RiskManager lo ve, settlement real, fill sensor validado en API viva). **Problema de fondo:** se queda sin combustible cuando acaba el Mundial.

---

## 💰 Estrategia 2 — CONSENSO / SPORTSBOOK (Motor 2) ← play actual

**Cómo funciona:** compara el precio de **Kalshi** contra la **probabilidad justa** del consenso de casas de apuestas (Pinnacle y cía., las más afiladas), vía **The Odds API** (suscripción ~$30/mes, ya pagada). Cuando Kalshi se desvía del consenso → apuesta el lado mal preciado.

**Cómo hace dinero:** las casas profesionales son **muy eficientes**; cuando Kalshi les lleva la contraria, normalmente la casa tiene razón → cada apuesta es **+EV**. Se gana **en el agregado de muchas apuestas** (ley de grandes números), no en cada una.

**Lo malo:**
- Es **DIRECCIONAL** → una apuesta individual **puede perder** (no es profit bloqueado como el arb). Varianza alta a tamaño chico.
- Requiere la API paga (la única que justifica ese gasto).

**Salvaguardas:** `MAX_PLAUSIBLE_EDGE=0.15` (descarta edges monstruosos como stale/in-play), no-vig multiplicativo para el fair-value, matcher de igualdad exacta de conjuntos (imposible cruzar equipos de la misma ciudad, ej. Mets↔Yankees), gate `is_live` (jamás apuesta sobre el fixture fake), RiskManager (cap por trade + exposición + stop-loss) + settlement.

**Estado:** auditado de punta a punta. **MLB conectado** (2026-06-16): 30 equipos en alias, `matched=35`, ~14 señales/ciclo en banda sana 3–11pp. Es **diario y sostenible** (MLB juega todos los días) → es el camino.

---

## ❌ Estrategia 3 — CLV (Motor 3): no existe

CLV = *Closing Line Value*: apostar temprano y capturar el movimiento de la línea hacia tu apuesta para el cierre. Concepto válido, pero **no hay código** — solo el flag `MOTOR_3_CLV_ENABLED`. Sería un proyecto nuevo.

---

## La verdad sobre cuánto dinero

Ninguna estrategia es "rica rápido" — **el retorno escala con el CAPITAL, no con lo listo del bot**, porque los edges son finos:

| Capital | Arbitraje (~2%/op) | Consenso (+EV, más volumen) |
|---|---|---|
| **$100** | centavos/op — sirve para *probar*, no para ganar | centavos/op |
| **cuatro cifras** | dólares/op | dólares/op, más oportunidades (MLB diario) |

- **Arbitraje:** casi sin riesgo, pero pocas oportunidades (y atado a eventos con spread).
- **Consenso:** +EV con varianza; más oportunidades, pero necesita **volumen + capital** para que el +EV se materialice.

### El plan que tiene sentido

1. **Motor 2 sobre MLB** (usa la API, es diario) en shadow.
2. Confirmar **consistencia multi-día** (el Analyst Loop ya la trackea).
3. **Canary chico real** ($ mínimo) → validar la cadena con dinero real.
4. Si corre limpia → **escalar capital** (ahí está el dinero de verdad).
5. **Arbitraje (Motor REST)** queda como complemento de bajo riesgo cuando haya eventos con spread.

### Gates antes de capital real (Motor 2 + MLB)

```
✅ Matching MLB (30 equipos, shared-city resuelto)
✅ Señal real generándose (shadow)
✅ Anti-fantasma (MAX_PLAUSIBLE_EDGE)
✅ Cable de ejecución auditado (executor + poller + muro 3 capas)
✅ RiskManager + settlement integrados
⏳ CONSISTENCIA MULTI-DÍA  ← lo único que falta
🐤 canary $ real → escalar
```
