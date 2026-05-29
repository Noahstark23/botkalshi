# KALSHI_BOT_CONTEXT.md

**Versión:** 1.5
**Última actualización:** Mayo 28, 2026
**Owner:** Noel Pineda (sole founder)
**Repo:** kalshi-bot (privado, GitHub — Noahstark23/botkalshi)

**Cambios v1.4 → v1.5 (2026-05-28):**
- **Lección 9 NUEVA:** Dos activaciones fallidas de V2 (25-may, 27-may), dos
  diagnósticos prematuros, contención disciplinada en ambas. Causa raíz NO
  resuelta — pendiente tercer discovery con stack traces del attempt #2.
- Fixes ed7b7ac + b9abaa0 mergeados: size=0 filter, seq order swap, dispatcher
  logging. Solo el logging fix validado como efectivo en producción.h
- Sección 11: deuda técnica viva — V2 sigue no apto para producción, causa
  raíz abierta.
- Sección 12.5 sin cambios (runbook validado empíricamente 2 veces).
- **Lección 10 PENDIENTE:** WS zombie / degradación escalonada 28-may. Discovery completado (event loop compartido, SQLite singleton, writes síncronos); fix propuesto pendiente de implementación.

**Cambios v1.3 → v1.4 (2026-05-24):**
- Sección 4 actualizada: Motor 1 reflejando el estado real (matemática
  arbitraje completa, RiskManager con tests E2E, OrderbookState Día 1
  mergeado, OrderbookManagerV2 mergeado y dormant detrás de flag).
- Sección 6 corregida: rate limit real (200 reads/sec en producción, no
  100), eliminada referencia a `Retry-After` que Kalshi no envía.
- Sección 9 consolidada: Lecciones 1-7 con fechas reales y referencias a
  fixes mergeados.
- Sección 10 actualizada: roadmap refleja lo realmente completado.
- Sección 11 actualizada: deuda técnica viva (parse_price_to_cents float
  vs Decimal; audit pendiente de `asyncio.gather` residuales).
- **Sección 12.5 NUEVA:** Runbook de activación de OrderbookManagerV2.
- **Sección 14 NUEVA:** Workflow operativo (modelo de roles 1+2).

---

## 1. RESUMEN EJECUTIVO

Bot de trading algorítmico para **Kalshi prediction markets** (regulado por CFTC).

**Tesis del sistema:** El edge en prediction markets en 2026 NO viene de predecir resultados (mercados son ~98% eficientes). Viene de detectar ineficiencias matemáticas estructurales que duran segundos a minutos, antes de que market makers institucionales las capturen.

**Bankroll target:** $2,500 ($300 inicial → escala hasta $2,000 si el sistema valida)
**ROI realista 2026:** 1-2% mensual (no 3-5% como en 2024)
**Mes 1 esperado:** -1% a 0% (calibración)
**Función estratégica:** Ingreso pasivo complementario, NO plan principal de capital

---

## 2. STACK TÉCNICO (DECIDIDO)

### Backend
- **Lenguaje:** Python 3.12
- **HTTP async:** httpx
- **WebSocket:** websockets library (pin `>=13.0,<17.0` — ver Lección 7)
- **Crypto:** cryptography (RSA-PSS para Kalshi auth)
- **Validación:** pydantic v2 + pydantic-settings
- **ORM:** SQLModel (patrón `select() + s.exec()`, NUNCA `.query()`)
- **API local:** FastAPI (health + admin endpoints en puerto 8080)
- **Logging:** Loguru
- **Scheduler:** APScheduler
- **Telegram alerts:** python-telegram-bot

### Storage
- **DB:** SQLite local en volumen Docker persistente
- **Backup:** snapshot cada 6 horas vía cron en VPS
- **NO usamos Postgres** — overkill para volumen del bot

### Infrastructure
- **Container:** Docker multi-stage
- **Orquestación:** Coolify en DigitalOcean VPS
- **VPS actual:** mismo droplet donde corre n8n.movarlensiu.com
- **Acceso remoto:** Coolify dashboard (web + mobile-friendly)

### Tests
- pytest + pytest-asyncio
- 264+ tests mergeados (mayo 2026). Coverage real en módulos críticos:
  signer, fees, kelly, arbitrage, risk manager, orderbook, executor,
  orderbook_manager_v2.

---

## 3. ARQUITECTURA

```
┌─────────────────────────────────────────────────┐
│  DigitalOcean VPS                               │
│  └─ Coolify                                     │
│       └─ kalshi-bot                             │
│            ├─ Health server (FastAPI :8080)     │
│            ├─ Production runner (asyncio)       │
│            │   ├─ WebSocket → Kalshi feed       │
│            │   ├─ REST client → Kalshi orders   │
│            │   ├─ DataCapture (snapshots+events)│
│            │   ├─ OrderbookManagerV2 (dormant)  │
│            │   ├─ Strategy engines (Motor 1)    │
│            │   ├─ Risk manager (activo)         │
│            │   └─ Telegram alerts               │
│            └─ SQLite (volumen persistente)      │
└─────────────────────────────────────────────────┘
                   │
                   ▼
              Kalshi API
              (US East datacenter)
```

### Módulos
```
src/
├── auth/         RSA-PSS signing
├── clients/      REST + WS clients de Kalshi
├── storage/      SQLModel models
├── strategies/
│   ├── data_capture.py      ACTIVO: snapshots + persistencia
│   └── motor_1_arbitrage/
│       ├── orderbook.py            OrderbookState (Día 1, mergeado)
│       ├── orderbook_manager_v2.py V2 (mergeado, dormant detrás de flag)
│       ├── orderbook_manager.py    V1 (dormant, será eliminado en sprint Motor 1)
│       ├── detector.py             Matemática de arbitraje (mergeado)
│       └── executor.py             Rollback automático (mergeado)
├── math/         fees, kelly, arbitrage (completo y testeado)
├── risk/         Risk manager (completo, tests E2E)
├── monitoring/   Health endpoints, Telegram, BotState
└── utils/        Config, logging
```

---

## 4. LOS 3 MOTORES DE TRADING

### Motor 1: Arbitraje Intra-Kalshi
**Tesis:** En cada evento con N outcomes, sus precios deberían sumar ≥100¢. Si suman <100¢ después de comisión, arbitraje matemático garantizado.

**Estado actual (mayo 24, 2026):**
- ✅ `src/math/arbitrage.py`: `detect_binary_arb` + `detect_multi_outcome_arb` mergeados con tests completos.
- ✅ `src/math/fees.py`: fórmula real Kalshi `ceil(7 * count * price * (100-price) / 1_000_000)` mergeada y testeada.
- ✅ `src/math/kelly.py`: Quarter Kelly con cap 5%.
- ✅ `src/risk/manager.py`: stop-loss daily/weekly/monthly (calendario UTC), tests E2E completos.
- ✅ `src/strategies/motor_1_arbitrage/orderbook.py`: OrderbookState puro (Día 1).
- ✅ `src/strategies/motor_1_arbitrage/orderbook_manager_v2.py`: gap detection por sid + books por ticker, recovery, alerts Telegram con throttle. 15/15 tests pasan. **DORMANT** detrás de `USE_ORDERBOOK_MANAGER_V2=False`.
- ✅ `src/strategies/motor_1_arbitrage/executor.py`: FOK paralelo + rollback sell-to-1¢, circuit breaker tras 3 rollbacks/hora.
- 🔜 **Pendiente:** activar V2 en producción (ventana agendada mayo 25 PM).
- 🔜 **Pendiente:** wirear detector con V2 para que detecte oportunidades reales (Día 3 del sprint).

**Frecuencia esperada 2026:** 1-3 oportunidades/semana en mercados nicho (políticos, weather, eventos culturales). En MLB/NBA mainstream casi cero — los HFT institucionales los matan en milisegundos.

**Sizing:** Máximo de liquidez disponible, hasta 100 contratos por outcome.

**Riesgo:** 0% si se ejecuta correctamente (riesgo real es ejecución parcial — rollback automático mitiga).

**ROI esperado por trade:** 0.5-2%
**ROI mensual del motor:** 0.3-0.8%

### Motor 2: Kalshi vs Sportsbook Consensus (EL MÁS RENTABLE)
**Tesis:** Pinnacle (o consenso DK/FD/MGM) es el benchmark más cercano al "precio justo". Cuando Kalshi se desvía >3pp después de remover vig, hay edge real.

**Implementación:**
- The Odds API ($30-60/mes) para data de sportsbooks consenso
- (Pinnacle puede no estar accesible desde California — backup plan necesario)
- Algoritmo no-vig para limpiar comisión del sportsbook
- Comparación contra Kalshi YES/NO bid-ask

**Foco:** Mercados nicho donde institucionales no compiten (volumen <$50k/mercado), deportes menores, eventos políticos/culturales.

**Sizing:** ¼ Kelly con cap en 5% capital activo por trade.

**ROI mensual del motor:** 0.8-1.5%

### Motor 3: Closing Line Value (CLV)
**Tesis:** Las líneas se mueven entre apertura y cierre por dinero sharp. Comprar al abrir + vender 30 min antes del cierre captura el movimiento direccional sin esperar resultado.

**Foco:** Mercados retail-pesados (NFL Sunday, eventos políticos virales) donde el flujo retail desbalancea precios temporalmente.

**Sizing:** Más conservador, 2-3% capital por trade.

---

## 5. GUARDRAILS Y POLÍTICAS DE OPERACIÓN

1. **`TRADING_ENABLED=false` por defecto.** El bot opera en producción
   contra la API real solo para captura de datos. Trading desactivado.

2. **Fase actual: Fase 1 (data capture).** Acumulando snapshots reales de
   40+ markets sin tradear. Toda activación posterior requiere checklist.

3. **Activación de `TRADING_ENABLED` requiere TODO lo siguiente:**
   - [ ] Mínimo 7 días corriendo sin crashes ni reinicios anómalos.
   - [ ] `tracked_markets > 50` estable durante esos 7 días.
   - [ ] DB con `market_snapshots` y `orderbook_events` poblados continuamente.
   - [ ] Logs muestran arbitrajes detectados (aunque no se ejecuten).
   - [ ] `OrderbookManagerV2` activo y sin gaps anómalos por 72h+.
   - [ ] RiskManager validado con tests de integración (✅ completo).
   - [ ] Capital activo confirmado a $300 (no más en Fase 2).
   - [ ] Decisión documentada en commit message del cambio de la flag.

4. **Cualquier cambio a parámetros de risk** (stop-loss, exposure, sizing,
   Kelly fraction) requiere aprobación explícita de Noel.

5. **Después de modificación de risk params**, mínimo 48h de monitoring antes
   de cambios adicionales.

### Decisiones técnicas del Risk Manager (mayo 2026)

1. **Stop-loss windows: calendario, no rolling.**
   - Daily: desde 00:00 UTC del día actual
   - Weekly: desde 00:00 UTC del lunes de la semana actual
   - Monthly: desde 00:00 UTC del día 1 del mes actual

   Razón: alinea con cómo Kalshi reporta PnL en su dashboard nativo.

   Trade-off conocido: en las transiciones de periodo el contador resetea
   (viernes 23:59 → sábado 00:01 → contador diario a cero). Mitigación:
   los 3 timeframes operan juntos; un breach semanal o mensual sigue
   disparando aunque el diario haya reseteado.

2. **Naive UTC datetimes consistentes** en queries y writes de `Trade.settled_at`
   y campos análogos.

   Patrón obligatorio para todos los writes:
   ```python
   datetime.now(UTC).replace(tzinfo=None)
   ```

   NUNCA usar:
   - `datetime.utcnow()` — deprecated en Python 3.12+
   - `datetime.now()` sin argumento — usa local timezone
   - `datetime.now(UTC)` directamente como write — produce aware datetime que
     SQLite guarda como string ISO, retornado como naive en reads → mismatch

3. **Reconcile en boot tolerante a fallas.**
   El bloque de reconciliación de trades huérfanos está envuelto en
   try/except. Si Kalshi está caído al arranque, el bot sigue corriendo — la
   captura de datos continúa funcionando y el error se registra en
   `BotState.last_error` para visibilidad en `/status`.

---

## 6. KALSHI API SPECIFICS

### Authentication
- **RSA-PSS** signing requerido (NO HMAC tradicional)
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
- Mensaje a firmar: `{timestamp_ms}{METHOD}{path}` (path incluye query string en GET)
- Llave RSA 4096 mínimo, en formato PEM
- Llaves diferentes para demo y producción (en Fase 1 usamos las de producción)

### Endpoints clave
- **REST base:** `https://api.elections.kalshi.com/trade-api/v2` (producción, ACTIVO)
- **WS prod:** `wss://api.elections.kalshi.com/trade-api/ws/v2`
- **Demo:** `https://demo-api.kalshi.co/trade-api/v2` (NO usado en Fase 1)

### Rate limits
- **200 reads/sec** REST en producción
- WS sin límite específico pero respeta backpressure
- **Importante:** Kalshi NO envía header `Retry-After` (confirmado empírica-
  mente, Lección 5). El cliente usa exponential backoff con cap=60s sin
  parsear ese header.

### Comisiones (CRÍTICO para cálculo de edge)
- **~7% sobre profit aproximación high-level** (para Kelly sizing rough).
- **Fórmula real (USAR para Motor 1 / arbitraje):**
  `fee_cents = ceil(7 * count * price_yes_cents * (100 - price_yes_cents) / 1_000_000)`
  (Integer arithmetic. Mergeado en `src/math/fees.py`.)
- Aplica solo a trades rentables.
- **Cualquier cálculo de edge en arbitraje debe usar la fórmula real, no la
  aproximación.** En arbitrajes con profit gross de pocos centavos, la
  diferencia decide si hay edge o no.

### Shape de mensajes WebSocket (2026)
- **orderbook_snapshot:** `{"market_ticker": str, "msg": {...}}`. El payload
  expone `yes_dollars_fp` / `no_dollars_fp` como primary (legacy `yes` / `no`
  como fallback).
- **orderbook_delta:** incluye `seq` y `previous_seq` para validación de
  secuencia. Validación estricta — gap → `SidGapError` → recovery.

### Demo vs Producción
- Demo da $10,000 virtuales pero data SINTÉTICA (insuficiente para entrenar
  Motor 1 — los arbitrajes en demo serían artificiales).
- Production: data y liquidez reales, comisiones reales.
- **Decisión 2026-05-09:** operamos en production con `TRADING_ENABLED=false`
  desde Fase 1 para acumular data real sin tradear.

---

## 7. CONTEXTO LEGAL Y REGULATORIO

### Status legal
- **Kalshi es CFTC-regulated** (Commodity Futures Trading Commission)
- **Trading algorítmico permitido** explícitamente en Terms of Service
- **No es gambling regulado por estado** — es un derivative market federal
- **API oficial pública** disponible para todos los usuarios

### Jurisdicción del operador
- Noel reside en California (Oakland) actualmente
- Plan: relocación a Nicaragua (Estelí) abril/mayo 2027
- **Kalshi opera en California** — sin issue jurisdiccional desde US
- **Desde Nicaragua:** verificar acceso. CFTC products típicamente accesibles internacionalmente pero requiere verificación.

### Tax implications
- **1099 emitido por Kalshi** si ganancias >$600/año
- **Reportable como capital gains** en US tax return
- **No es foreign income** — plataforma estadounidense regulada
- Mantener registro completo de trades en SQLite para tax season

### Constraints técnicos derivados
- The Odds API alternativa a Pinnacle (Pinnacle puede no estar accesible desde CA)
- Si IP geo cambia (VPN, viaje), puede triggerear KYC re-verification
- VPS en US East simplifica todo lo geográfico

---

## 8. DECISIONES TÉCNICAS DEFERIDAS

| Decisión | Trigger | Notas |
|---|---|---|
| Migrar SQLite → Postgres | >100k trades/mes | No anticipado en primer año |
| Multi-VPS (HA setup) | Generando >$200/mes | Solo si retornos justifican costo |
| Agregar Polymarket | Después de 6 meses Kalshi exitoso | Diferente regulación, evaluar |
| Market making strategy | Después de motor 1-3 estables | Estrategia más rentable pero compleja |
| ML/AI offline para análisis | Tener >1000 trades de data | NUNCA en hot path de decisiones |
| Co-locación cerca de Kalshi servers | >$1000/mes profit | Solo si latencia <5ms paga |
| Migración a aiosqlite + WAL | Si lag de DB pasa de 30ms | Refactor mayor, posponer |
| Eliminar `orderbook_manager.py` (V1) | Sprint de activación Motor 1 | Aún referenciado por runner/detector |

---

## 9. LECCIONES APRENDIDAS

### Lección 1 (Mayo 2026): Modelos públicos de probabilidad están atrasados
**Contexto:** Experimentamos con consejos de Claude usando numberFire, Sportytrader, Dimers para detectar edges en MLB y Europa League.

**Resultado:** 1 acierto de 6 recomendaciones. Pérdida de $50 sobre $200 bankroll en 48 horas.

**Causa raíz:** Los modelos cuantitativos públicos no procesan información en tiempo real (lineups del día, lesiones, dinero sharp). El mercado de Kalshi sí. Cuando hay disonancia, el mercado tiene razón ~80% del tiempo.

**Decisión derivada:** El bot NO usa modelos públicos como input. Solo usa:
1. Precios de Kalshi mismo (arbitraje matemático)
2. Sportsbook consensus en tiempo real (no modelos predictivos)
3. Movimiento de líneas (CLV)

**Anti-patrón confirmado:** "Modelo dice X, mercado dice Y, hay edge en X" es ilusión la mayoría del tiempo.

### Lección 2: LLMs alucinan números
**Confirmado experimentalmente:** Claude calculó edges de "+11pp" que en realidad eran +6pp o negativos. La aritmética de probabilidades + Kelly criterion en lenguaje natural produce errores.

**Decisión derivada:** Cero LLMs en hot path de trading. Solo código Python determinístico.

### Lección 3: El edge en 2026 es marginal, no transformador
Mercados de Kalshi son competidos por institucionales (Susquehanna, etc.) y bots retail SaaS ($9/mes commodity). El edge realista para retail con bot custom: 1-2% mensual, no 3-5%.

**Decisión derivada:** Bankroll inicial $300 no $2500. Validar antes de escalar. Si no funciona, parar a tiempo.

### Lección 4 (Mayo 9, 2026): Excepciones tragadas en arranque matan silenciosamente
**Contexto:** Primer deploy en Coolify. data_capture muere a los 7 segundos del arranque por 429 too_many_requests. Las excepciones se logueaban a `debug` y `run()` hacía `return` definitivo en vez de reintentar. Container vivo (health server seguía respondiendo) pero motor de captura muerto. DB con 0 rows después de 1.45h.

**Causa raíz:** Combo de tres bugs latentes:
1. Sin retry de discovery en `data_capture.run()`.
2. Excepciones de Kalshi capturadas en nivel `debug` (invisible en logs INFO).
3. Sin warm-up al arranque para evitar rate limits acumulados de deploys previos.

**Decisión derivada:**
- Toda llamada externa al arranque debe tener retry con backoff exponencial.
- Excepciones en discovery van a nivel `warning`, no `debug`.
- `/status` endpoint expone `capture_running`, `ws_connected`, `last_ws_message`
  para detectar muertes silenciosas.

### Lección 5 (Mayo 12, 2026): Loop perpetuo de 429s por Retry-After inexistente
**Contexto:** 560 errores 429 consecutivos en 19 horas. 0 markets descubiertos.
`last_error: null` en `/status` (el campo no se actualizaba al agotar retries).

**Causa raíz:** Triple combo:
1. El cliente tenía código para parsear `Retry-After` header de Kalshi — pero Kalshi NO
   envía ese header. El código caía al `else: pass` y dormía 0s, causando loop tight
   de 429s sin exponential backoff real.
2. Discovery hacía 9 requests en burst (un prefix por serie): KXMLB, KXNBA, KXNHL...
   sin ninguna pausa entre ellos. El backoff estaba en `max=10s` (insuficiente).
3. `_record_api_error` no existía: `BotState.last_error` nunca se actualizaba al
   agotar retries, por eso `/status` mostraba `last_error: null` con 560 errores.

**Fixes aplicados (2026-05-12):**
- Eliminado todo el parsing de `Retry-After` del cliente REST.
- `wait_exponential(max=10)` → `wait_exponential(max=60)`.
- `_record_api_error()` helper en `kalshi_rest.py` actualiza `BotState` al agotar retries.
- `asyncio.sleep(2.0)` entre prefixes en `_discover_markets`.
- `TARGET_SERIES_PREFIXES` reducido a 2 (KXMLB, KXNBA) temporalmente.
- Script `scripts/diagnose_kalshi.py` standalone para debug de conectividad.

### Lección 6 (Mayo 16-17, 2026): Misclasificación de 401 como 429 + diagnóstico contra host equivocado
**Contexto:** Bloqueo productivo de 48h con loop de "429s" interminables y
`/status` sin errores visibles. El bot reportaba healthy mientras estaba
incapaz de hacer ningún request real.

**Causa raíz:** Combo de tres bugs:
1. `_classify_error` en `kalshi_rest.py` mapeaba 401 de proxy-migration de
   Kalshi como 429 (rate limit), entrando en retry loop infinito sin avanzar.
2. `KalshiAuthError` no llamaba `BotState.record_error()`, por lo que el
   401 mal-clasificado nunca aparecía en `/status` — falla invisible al
   operador.
3. `scripts/diagnose_kalshi.py` tenía hardcoded un host deprecated, por lo
   que el diagnóstico salía limpio contra un servidor que no era el real,
   confirmando falsamente que "Kalshi está bien".

**Decisión derivada:**
- `_classify_error` siempre verifica el status code antes de mapear.
- Toda excepción de Kalshi (auth, rate, server, client) registra a
  `BotState.record_error()`. No hay path silencioso.
- Script de diagnóstico lee el host del `.env`, nunca hardcoded.
- **Patrón confirmado:** "el diagnóstico está limpio" ≠ "el sistema está
  sano" — siempre verificar que el diagnóstico esté apuntando al sistema
  real, no a un fantasma.

### Lección 7 (Mayo 13-14, 2026): asyncio.gather traga excepciones + websockets API change

**Contexto:** WS jamás conectó por 11h. 226 intentos consecutivos con TypeError
idéntico. last_error: null durante todo el episodio. Container "healthy"
mientras orderbook_events permanecía en 0. REST capture funcionando en paralelo
ocultaba la falla a nivel de operador.

**Causa raíz técnica:** `extra_headers` kwarg renombrado a `additional_headers`
en `websockets` 13.0+. pyproject.toml declaraba `websockets>=12.0` sin pin
superior, pip instaló 14.x+ al rebuild de Docker, código quedó incompatible.

**Causa raíz arquitectónica (más importante):** `data_capture.py:run()` usaba
`asyncio.gather(ws.run(), snapshots(), return_exceptions=True)`. La excepción
de ws.run() fue capturada como result y descartada. snapshots() siguió corriendo
solo, dando la ilusión de "capture_running: true". **TERCERA vez del mismo
patrón (lecciones 4, 6, 7).**

**Decisión derivada:**
- PROHIBIDO `asyncio.gather(..., return_exceptions=True)` para tareas críticas.
  Usar supervisor pattern explícito que captura, reporta a BotState.record_error,
  y re-leva.
- Pin de dependencias críticas: `websockets>=13.0,<17.0`. Aplicar mismo patrón
  a httpx, websockets, pydantic en próximos releases.
- BotState.ws_connected refleja estado real de conexión, validado por heartbeat.
- N fallos consecutivos (≥5) → Telegram alert obligatorio.
- Tests de regresión específicos para versión de librerías críticas.

**Anti-patrón confirmado por 3ra vez:** "el bot dice que está corriendo" ≠
"el bot está corriendo". El monitor SIEMPRE valida contra estado real, nunca
contra flag interna sin contraste empírico.

### Lección 8 (Mayo 24, 2026): Discovery primero, planning después
**Contexto:** Sprint de "Día 2 OrderbookFeed" planeado y briefed a Gemini.
Después de 3 rondas de revisión adversarial, Claude Code descubrió que
`OrderbookManagerV2` ya existía en el repo, mergeado, dormant detrás de un
flag, cubriendo 90% del scope que Día 2 prometía construir. Tres semanas
de planning sobre código que ya existía.

**Causa raíz:** Capa de planning (Claude Project) escribió briefs detallados
asumiendo el estado del repo basándose en docstrings desactualizados de
módulos vecinos, sin pedir a Claude Code un dump empírico del estado real.

**Decisión derivada:**
- Antes de escribir cualquier brief de implementación, Claude Project pide
  a Claude Code 5 min de discovery: listar archivos, dump de signatures,
  grep de importadores. El brief sale DESPUÉS de ver el estado real.
- Los docstrings de módulos pueden quedar como fósiles cuando el código
  alrededor evoluciona. No son fuente de verdad para planning. El código
  real lo es.

**Anti-patrón confirmado:** "según el docstring..." ≠ "según el código actual..."

---

### Lección 9 (Mayo 25-27, 2026): Dos activaciones fallidas de V2, dos diagnósticos prematuros, contención disciplinada en ambas

**Estado de causa raíz: NO RESUELTA al cierre de esta entrada.** Esta lección
documenta dos intentos fallidos de activar OrderbookManagerV2 y dos hipótesis
de causa raíz que resultaron incorrectas o incompletas. El diagnóstico real
queda pendiente de un tercer discovery con la evidencia nueva (stack traces
completos) capturada en el attempt #2.

**Contexto — Attempt #1 (25-may):** Activación de V2 siguiendo runbook 12.5.
T+5min: ráfaga de 19 errores `delta produces qty<0` en 179ms. T+15min: segunda
ráfaga de ≥8 errores, magnitudes hasta -6247, tickers distintos. Rollback a
T+25min en ~6min. 87 errores totales en 27min. Stack traces perdidos
(`NoneType: None`) por `return_exceptions=True` + `logger.exception` sin
`exc_info` explícito.

**Contexto — Attempt #2 (27-may):** Tras fix ed7b7ac (size=0 filter + seq
order swap + dispatcher logging fix), revisión sobria del diff, 4 tests verdes
e inspección de paths de excepción confirmando cobertura. Re-activación.
**Primer error a T+2.7s del primer snapshot** (`KXMLB-26-ATL at 10c: delta
produces qty=-3108 < 0`), seguido de gap CRITICAL (38 tickers stale) y server
error de Kalshi (`code 15 "Action required"`). Rollback en ~4min. El patrón
estructural reapareció **con el fix aplicado**.

**Lo que cada diagnóstico afirmó vs. lo que la realidad mostró:**

| Diagnóstico | Afirmó | Refutado por |
|---|---|---|
| Post-attempt #1 (v1) | "Feed corruption de Kalshi, causa externa" | Segundo discovery encontró bug interno (size=0) |
| Discovery dirigido | "H1: size=0 filter es la causa, fix de ~5 líneas" | Attempt #2: el fix se aplicó y el bug reapareció |
| Estado actual | Causa raíz desconocida | (pendiente tercer discovery) |

**Causas técnicas identificadas (reales pero NO suficientes):**

1. **Discrepancia de convención `size=0`** entre `_parse_fp_levels` (no
   filtraba) y `OrderbookState.apply_snapshot` (filtraba con `if size > 0`).
   Bug real, corregido en ed7b7ac. **Pero no era la causa del incidente** —
   el attempt #2 falló igual con el filtro aplicado.

2. **Orden de operaciones invertido** en `handle_message`:
   `_last_seq_by_sid[sid]` se actualizaba antes de `state.apply_delta()`,
   convirtiendo un error puntual en cascada. Bug real, corregido. Contribuye
   a la propagación pero no es el origen del primer `qty<0`.

3. **Stack traces silenciados** en `kalshi_ws._dispatch`. `asyncio.gather(
   return_exceptions=True)` captura la excepción como objeto, vaciando
   `sys.exc_info()` en el contexto de `logger.exception`. Corregido con
   `logger.opt(exception=r)`. **Este fix SÍ funcionó** — el attempt #2
   produjo stack traces completos que el attempt #1 no tuvo. Es la única
   pieza de los tres fixes que cumplió su propósito.

**La causa raíz que sigue abierta:** En el attempt #2, el primer error fue
`KXMLB-26-ATL at 10c: qty=-3108` a T+2.7s. Pendiente de verificar en los logs
preservados: ¿ese price (10c) tenía size>0 en el snapshot WS inicial? Si sí,
H1 (size=0) está completamente refutada y el bug es otro. Si el snapshot tenía
el level con size válido pero el delta posterior produjo qty negativo de todas
formas, el problema está en cómo V2 aplica deltas sobre estado válido, no en
cómo filtra snapshots. Logs preservados:
`data/rollback_v2_attempt2_20260527_154809.log` (949 KB).

**Causa raíz arquitectónica (validada, esta sí):** El bot NO crashea ante
estos errores — los trata como recoverable y sigue "vivo" con estado
degradado. En attempt #2, `bot_runs.crash_reason=None` para el run de V2:
12min de errores, cero crash. Esto es el patrón estructural de Lección 7
("el bot dice que está corriendo" ≠ "el bot está corriendo") aplicado a
**estado mutable in-memory** en lugar de conexión WS. V2 introdujo el primer
componente con state mutable en producción, y el sistema no distingue entre
"handler independiente que falló" (tolerable) y "state machine que se
corrompió" (no tolerable). Esta es la lección estructural más sólida del
incidente.

**Decisiones derivadas:**

1. **Un diagnóstico no validado contra producción es una hipótesis, no una
   causa raíz — sin importar cuánto rigor lo respalde.** El fix de size=0
   pasó 4 tests, revisión sobria del diff, inspección de paths de excepción,
   y aun así no era la causa. La única validación real de un fix de
   producción es la producción. Tests verdes ≠ bug resuelto.

2. **Operadores con estado mutable necesitan tratamiento de error distinto a
   handlers idempotentes.** `return_exceptions=True` es correcto para DB
   writers; para una state machine, un error debe marcar el estado como
   corrupto y forzar recovery, no tragarse silenciosamente y seguir operando.

3. **Runbook 12.5 + línea defensiva T+5min funcionan.** Dos activaciones,
   dos rollbacks limpios (<5min ambos), cero daño operativo, V1 intacto. El
   sistema de contención es sólido aunque V2 no lo sea.

4. **El logging fix se valida solo: el attempt #2 capturó lo que el attempt
   #1 perdió.** Mantener este patrón (`logger.opt(exception=r)`) para todo
   handler en contexto `gather`.

5. **La urgencia de "sprint/roadmap" es un fantasma en proyecto solo-founder
   sin capital trabajando.** Antes del attempt #2, la presión de "estamos
   atrasados con el sprint" casi saltó la inspección de paths de excepción.
   No hay sprint real: no hay team, board, ni deadline contractual. Activar
   V2 hoy vs. en una semana cambia $0 de PnL. Cuando la métrica de urgencia
   empuja a peores decisiones técnicas, la métrica está mal calibrada.

**Anti-patrones confirmados:**

- **Atribución externa sin discovery propio primero** (attempt #1: "es el
  feed"). La hipótesis externa es la más cómoda y la menos verificable.
  Siempre la última en aceptarse. (Refuerzo de Lección 6.)

- **Confianza prematura en un fix no validado en producción** (attempt #2:
  "size=0 era la causa, fix de 5 líneas, listo"). Tercera confirmación del
  patrón "el diagnóstico limpio ≠ el sistema sano" (Lecciones 6, 8, 9).

- **Interpretar criterios de runbook con discreción en mitad de incidente**
  (attempt #1: clasificar la primera ráfaga como "no requiere rollback" con
  argumentos ad-hoc). Si el criterio numérico se cumple, el rollback se
  ejecuta.

- **Urgencia de roadmap como motor de decisión técnica** (pre-attempt #2).
  El "atraso" autoinfligido empujó a comprimir validación. La línea
  defensiva del runbook compensó, pero el patrón de decisión era el
  equivocado.

**Lo que sí funcionó (preservar):**

1. **Runbook 12.5 con criterios literales + línea defensiva T+5min.**
   Contuvo dos incidentes a <5min cada uno, cero impacto al capital.
2. **Capa adversarial (Claude Project) aplicando el runbook más estricto
   que el reporte operativo.** Frenó la activación apresurada del attempt
   #2 hasta cerrar la inspección.
3. **V1 como baseline no destructivo.** Mantener V1 corriendo durante el
   desarrollo de V2 permitió rollback instantáneo a estado conocido bueno,
   dos veces.
4. **Dispatcher logging fix.** Es el único de los tres fixes que cumplió:
   convirtió el attempt #2 de "ciego" (como attempt #1) a "con evidencia
   completa". Sin él, el tercer discovery arrancaría sin stack traces otra
   vez.

**Próximo paso (pendiente, NO ejecutar bajo presión):** Tercer discovery
dirigido sobre los logs preservados del attempt #2, con foco en el primer
error (`KXMLB-26-ATL at 10c`) y su snapshot WS correspondiente. El objetivo
es responder la pregunta abierta: ¿el bug está en el parsing del snapshot
(H1 parcial) o en la aplicación de deltas sobre estado válido (H nueva)?
La evidencia nueva (stack traces + raw snapshot logging) es suficiente para
responderlo de forma definitiva esta vez.

---

## 10. ROADMAP TÉCNICO

### Semana 1 — Infrastructure ✅ COMPLETADO
- [x] Auth RSA-PSS
- [x] Cliente REST async con retries
- [x] Cliente WebSocket con reconexión + pin de versión
- [x] SQLite schema
- [x] Health server FastAPI con `/status` rico
- [x] Dockerfile multi-stage
- [x] docker-compose.yml para Coolify
- [x] Deploy en Coolify VPS
- [x] Fixes de resilience (lecciones 4, 5, 6, 7 aplicadas)

### Semana 2 — Motor 1: Arbitraje Intra-Kalshi
**Estado actual (mayo 24, 2026):**
- [x] Fase 1: `src/math/fees.py` + `src/math/kelly.py`
- [x] Fase 2: `src/risk/manager.py` con stop-loss daily/weekly/monthly +
      tests E2E
- [x] Fase 3: Cliente REST con API V2 fixed-point support
- [x] Fase 4a: `src/strategies/motor_1_arbitrage/detector.py` (matemática:
      detect_binary_arb + detect_multi_outcome_arb con tests)
- [x] Fase 4b: `OrderbookState` (Día 1) puro en memoria con tests completos
- [x] Fase 4c: `OrderbookManagerV2` mergeado con 15/15 tests + alerts +
      visibilidad en /status (Mayo 24)
- [x] Fase 5: `src/strategies/motor_1_arbitrage/executor.py` con rollback
- [ ] Fase 6a: **Activar `USE_ORDERBOOK_MANAGER_V2=True`** (agendado:
      mayo 25 PM, ver Runbook 12.5)
- [ ] Fase 6b: Wirear detector con V2 (Día 3 del sprint, después de
      activación validada)
- [ ] Fase 7: Demo testing 7 días → checklist de activación
      `TRADING_ENABLED`

### Semana 3 — Motor 2: Kalshi vs Sportsbooks (NO INICIADO)
- [ ] Cliente The Odds API
- [ ] Algoritmo no-vig
- [ ] Matcher de eventos cross-platform
- [ ] Quarter Kelly sizing
- [ ] Caps de risk manager integrados
- [ ] Live testing en demo con bankroll completo

### Semana 4 — Motor 3 + Hardening (NO INICIADO)
- [ ] CLV strategy (open vs close pricing)
- [ ] Telegram alerts ampliados (trades, P&L)
- [ ] Dashboard local FastAPI
- [ ] Runbook de operación
- [ ] Backup automatizado SQLite

### Post-Semana 4
- [ ] Cumplir checklist de activación de `TRADING_ENABLED` (sección 5)
- [ ] Capital inicial real $300
- [ ] Monitoring tight primeros 14 días
- [ ] Decisión escalar a $800 (día 30)
- [ ] Decisión escalar a $2000 (día 60)

---

## 11. KNOWN TECHNICAL DEBT

| Item | Severidad | Fix esperado |
|---|---|---|
| `kalshi_python_sync` SDK no usado, hacemos requests manuales | Baja | Probablemente OK, evaluar después |
| ✅ RESUELTO 2026-05-14: WS reconnect sin retry exponencial | Resuelto | supervisor + escalación tras 5 fallos |
| ✅ RESUELTO 2026-05-14: websockets API incompatibility + gather tragando TypeError | Resuelto | additional_headers + supervisor pattern + heartbeat |
| ✅ RESUELTO 2026-05-17: `_classify_error` mapea 401 como 429 (Lección 6) | Resuelto | Fix mergeado, todas las excepciones loguean a BotState |
| ✅ RESUELTO 2026-05-24: `manager.py` + `manager_normalize.py` zombi en `motor_1_arbitrage/` | Resuelto | Eliminados (commit d7ce624) |
| `orderbook_manager.py` (V1) aún referenciado en `runner.py`, `detector.py`, `__init__.py` | Media | Eliminar en sprint de activación Motor 1 (cuando se migren los call-sites a V2) |
| Audit completo de TODOS los `asyncio.gather(return_exceptions=True)` residuales en codebase | Alta | Antes de `TRADING_ENABLED=true` |
| `parse_price_to_cents` en `data_capture.py` usa `round(float(v) * 100)` vs `Decimal` en `manager_normalize.py` (legacy) | Baja | Unificar a Decimal cuando se simplifique normalización post-V2 |
| Patrón `or` en normalizadores (`payload.get("seq") or payload.get("...") or msg.get("seq")`) enmascara valores `0` legítimos | Baja | Refactor a helper `_coalesce` que filtre por `is not None` |
| SQLite sin VACUUM scheduled | Baja | Agregar a cron mensual |
| Sin metrics export (Prometheus, etc.) | Baja | No anticipado primer año |
| Snapshots silenciosamente paran de escribir a DB (observado intermitente) | Media | Investigar lock SQLite o except:pass en `_take_snapshots` |
| Operando en `KALSHI_ENV=production` sin validación previa en demo | Media | Mitigado por `TRADING_ENABLED=false` + checklist sección 5 |

---

## 12. ANTI-PATTERNS A EVITAR

❌ **Predicción de mercados:** El bot NO predice ganadores. Solo detecta ineficiencias matemáticas.

❌ **Full Kelly:** Usar Kelly completo es matemáticamente óptimo en teoría pero quema bankrolls reales por estimaciones imperfectas de probabilidad.

❌ **Trading basado en modelos públicos predictivos:** numberFire, Sportytrader, Dimers — no usar como input.

❌ **LLMs en hot path de decisiones:** Lentos, caros, alucinan. OK para análisis offline mensual.

❌ **Optimizar para mercados líquidos populares:** MLB/NBA mainstream son territorio de institucionales. Foco en nicho.

❌ **Apostar contenido editorial sobre estrategia:** "Yankees ganan porque su rotación es sólida" no es input válido. Solo precios y matemática.

❌ **Recovery trading después de pérdidas:** Aumentar tamaño post-loss para "recuperar" es el patrón #1 de blow-up. Stop-losses son sagrados.

❌ **Cambio de risk params en caliente:** Cualquier modificación requiere aprobación + 48h monitoring.

❌ **Cambiar `TRADING_ENABLED` sin checklist:** En Fase 1 estamos sobre API
production. Activar la flag por descuido (al testear, al deployar, en una env
var copy-paste) tradearía dinero real sin validación. El cambio siempre
requiere los pasos del checklist de sección 5.

❌ **Asumir que demo y production se comportan igual:** Demo tiene data
sintética; production tiene flujos reales. Estrategias entrenadas en demo
pueden no funcionar en production y viceversa. Por eso operamos directamente
en production desde Fase 1 (con `TRADING_ENABLED=false` como guardia).

❌ **Tragar excepciones a nivel `debug`:** Si una llamada externa falla al
arranque, debe verse en logs INFO o WARNING. Capturar en `debug` esconde
muertes silenciosas (lección 4).

❌ **`asyncio.gather(..., return_exceptions=True)` en tareas críticas:** Las
excepciones se vuelven `results` y se descartan. Usar supervisor pattern
con try/except explícito (lección 7, confirmada 3 veces).

❌ **Aproximación de fee 7% flat en Motor 1:** La aproximación sirve para
sizing de Motor 2/3 (Kelly), pero arbitraje requiere la fórmula real de
Kalshi (`ceil(7 * count * price * (100-price) / 1_000_000)`). En profits
chicos, la diferencia decide si hay edge o no.

❌ **`datetime.utcnow()` o `datetime.now()` sin argumento:** SQLite no preserva
tz info. Usar `datetime.now(UTC).replace(tzinfo=None)` para todos los writes
(decisión Risk Manager).

❌ **SQLAlchemy `.query()`:** Usar `select() + s.exec()` (patrón SQLModel).
Recurring bug de Gemini en outputs.

❌ **`asyncio.create_task()` desde funciones sync:** Solo desde contexto async.
Recurring bug de Gemini.

❌ **Planificar sin discovery:** Asumir el estado del repo desde docstrings o
memoria. Lección 8: siempre pedir dump real antes de escribir briefs.

❌ **Diagnóstico contra host equivocado:** Verificar que el script de diagnóstico
apunta al sistema productivo real, no a fantasmas. Lección 6.

---

## 12.5 RUNBOOK — Activación de OrderbookManagerV2

**Ventana de activación:** Mayo 25, 2026 (tarde). Requiere ventana de
2-3h continuas de supervisión activa de Noel.

### Pre-flight checklist (obligatorio antes de tocar el flag)

- [ ] Bot healthy en `/status` por mínimo 1h consecutiva: `capture_running=true`,
      `ws_connected=true`, `tracked_markets>=40`, `last_error=null`.
- [ ] Ventana de 2-3h continuas confirmadas para supervisión activa.
- [ ] Telegram receiver verificado: enviar mensaje de test manual y confirmar
      recepción ANTES de tocar el flag.
- [ ] Backup DB manual ejecutado en los últimos 30 min (snapshot del volumen
      Docker, no esperar al cron de 6h).

### Activación

1. Coolify → Configuration → Environment Variables → settear
   `USE_ORDERBOOK_MANAGER_V2=true`.
2. Mantener `MOTOR_1_ARBITRAGE_ENABLED=false` y `TRADING_ENABLED=false`.
3. Redeploy (toma ~2 min).
4. Confirmar en logs de arranque: `OrderbookManagerV2 registered (data-capture only, no Motor 1)`.

### Criterios de ÉXITO (todos deben cumplirse en T+2h)

1. Cero `ERROR` nuevos en logs relacionados con orderbook/manager/V2.
2. `SidGapError` rate sostenido **< 5/min** (picos puntuales OK; lo que
   importa es la mediana).
3. `data_capture._take_snapshots` sigue completando 40/40 cada ~5 min
   (no regresión del path existente).

### Criterios de ROLLBACK INMEDIATO (cualquiera dispara rollback sin discusión)

1. Más de 3 errores no relacionados a `SidGapError` en 10 min.
2. `SidGapError` rate sostenido **> 20/min** por más de 5 min.
3. `tracked_markets` cae por debajo de 35 (regresión del path estable).
4. `/status` devuelve `capture_running=false` o `ws_connected=false` por
   más de 60s.
5. Cualquier `CRITICAL` o cualquier excepción que no estaba en producción
   antes.

### Procedimiento de rollback

1. Coolify → env var → `USE_ORDERBOOK_MANAGER_V2=false`.
2. Redeploy.
3. Verificar en logs ausencia del mensaje de registro de V2.
4. Confirmar `/status` vuelve al baseline anterior (`orderbook_manager_v2:
   {enabled: false}`).
5. **Tiempo objetivo end-to-end: < 5 min** desde detección a baseline
   restaurado.

### Grep patterns para diagnóstico rápido

```bash
# En Coolify logs del contenedor:
grep "SidGapError" 
grep "OrderbookManagerV2"
grep "recovery"
grep -i "error\|critical\|exception"
grep "Snapshots:"          # debe mostrar 40/40 cada ~5min
```

### Métricas en `/status` para baseline vs degradación

```json
"orderbook_manager_v2": {
  "enabled": true,
  "books_initialized": int,    // esperado: crece a 40 en T+1min
  "sids_tracked": int,         // esperado: 1-5 sids
  "sids_recovering": int,      // esperado: 0 en steady state
  "gaps_last_60s": int,        // esperado: 0-5 picos OK
  "last_gap_at": str | null    // ISO timestamp del último gap
}
```

### Post-activación

Si T+2h pasa limpio:
- Declarar éxito y dejar corriendo.
- Documentar observaciones reales (gap rate típico, books_initialized
  steady-state, etc.) en una sub-lección o ticket.
- Avanzar a Día 3 (wirear detector con V2 para detección real de
  oportunidades de arbitraje).

Si rollback ocurre:
- NO reintentar inmediatamente. Capturar logs completos.
- Analizar root cause con Claude Project antes de segundo intento.

---

## 13. CONTACTO Y OWNERSHIP

- **Sole operator:** Noel Pineda
- **Repos:** GitHub privado (Noahstark23/botkalshi)
- **Hosting:** DigitalOcean VPS (compartido con Nortex stack)
- **Capital:** Personal, segregado en cuenta Kalshi separada
- **Tax filing:** US individual (1099 from Kalshi)

---

## 14. WORKFLOW OPERATIVO

**Vigente desde mayo 24, 2026.** Reemplaza el workflow de 3 capas
homogéneo anterior. Calibrado por bucket de riesgo de la tarea.

### Roles

**Gemini** → CTO de visión estratégica. Conversaciones cada 2-4 semanas
para decisiones de high-level: activación de motores, agregar plataformas
(Polymarket), cambios al bankroll target, market making. Le pasamos el
contexto actualizado, opina, Claude Project cuestiona, Noel decide.

**Claude Project** → Planning táctico + decisión arquitectónica + revisión
adversarial cuando aplique. Escribe briefs para Claude Code en ~95% de
los casos. Reviews calibradas por bucket de severidad: **bloqueante** (no
mergea) vs **deuda** (ticket separado, mergea igual). No invento críticas
para justificar la capa.

**Claude Code** → Ejecución sobre la máquina local + Coolify. Implementa,
testea, deploya. NO hace recomendaciones operacionales sobre cuándo
activar features — esa decisión es de Noel.

**Noel** → Decisor final + revisor humano de cada PR antes de merge +
operador de Coolify + único con autoridad sobre activación de flags
críticos.

### Buckets de tareas

🟢 **Rutinaria** → Noel → Claude Code directo. Sin pasar por Claude Project.
- Limpieza de código zombi
- Agregar logs / alertas / métricas
- Configurar env vars
- Refactor de un archivo aislado bajo 100 LOC
- Documentación
- Bumps de dependencias menores

🟡 **Implementación táctica** → Noel → Claude Project (planning) → Claude
Code (ejecución) → Noel (revisión PR) → merge. Sin Gemini en el medio.
- Módulos nuevos aislados
- Endpoints nuevos
- Integraciones con clientes externos no-críticos
- Tests de cobertura
- Detector de arbitraje (Día 3)

🔴 **Crítica** → workflow completo de 3 capas (Gemini → Claude Project →
Claude Code → Noel).
- Cualquier cosa que toque `risk/manager.py`
- Cualquier cosa que toque `auth/`
- Lógica de sizing, Kelly, stop-loss
- Ejecutores de trades / rollback / partial fills
- Cambios al checklist de `TRADING_ENABLED`
- Activación de capital real

### Reglas mías que cambian

1. **Discovery primero, planning después.** Antes de escribir un brief de
   implementación, Claude Project pide a Claude Code 5 min de discovery del
   repo. Nunca asumir que un módulo no existe sin verificarlo. (Origen:
   Lección 8.)

2. **Reviews calibradas por bucket de severidad.** En tareas 🔴, mi review
   devuelve solo dos categorías: **bloqueante** (no mergea) y **deuda**
   (ticket separado, mergea igual). Cero "13 issues mezclados".

3. **No invento críticas para justificar la capa.** Si el código está bien,
   digo "está bien, mergeamos".

### Reglas de Noel para que el workflow funcione

1. **Noel decide el bucket al inicio de cada tarea.** "Esto es 🟢/🟡/🔴".
   Si no especifica, Claude Project asume 🟡 y arranca planning.

2. **No reabrir buckets a mitad de tarea.** Si arrancamos como 🟢 y a mitad
   descubrimos que toca algo crítico, paramos y reclasificamos a 🔴. Pero
   no oscilamos cada 3 mensajes.

3. **Si el workflow se siente lento, decirlo.** La calibración es un
   proceso, no un set-and-forget.

### Lo que NO cambia

- `TRADING_ENABLED` sigue en False hasta cumplir el checklist completo.
- Todos los anti-patterns de sección 12 siguen vigentes.
- Decisiones críticas siempre con human gate (Noel).
- Lecciones aprendidas se siguen documentando con causa raíz + decisión
  derivada + anti-patrón confirmado.

---

**FIN DE KALSHI_BOT_CONTEXT.md**
