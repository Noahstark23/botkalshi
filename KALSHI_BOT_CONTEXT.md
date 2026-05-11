# KALSHI_BOT_CONTEXT.md

**Versión:** 1.1
**Última actualización:** Mayo 9, 2026
**Owner:** Noel Pineda (sole founder)
**Repo:** kalshi-bot (privado, GitHub)

**Cambios v1.0 → v1.1:**
- Fase 1 ahora opera en `KALSHI_ENV=production` con `TRADING_ENABLED=false`
  (decisión 2026-05-09: data sintética de demo insuficiente para entrenar Motor 1).
- Checklist explícito para activación de `TRADING_ENABLED`.
- Roadmap de Semana 1 actualizado con fixes de resilience aplicados.
- Anti-patterns expandidos.

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
- **WebSocket:** websockets library
- **Crypto:** cryptography (RSA-PSS para Kalshi auth)
- **Validación:** pydantic v2 + pydantic-settings
- **ORM:** SQLModel
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
- Coverage mínimo target: 60% en módulos críticos (auth, sizing, risk)

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
│            │   ├─ Strategy engines (3)          │
│            │   ├─ Risk manager                  │
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
├── strategies/   Motores de trading (vacío hasta Semana 2)
├── risk/         Risk manager (vacío hasta Semana 4)
├── monitoring/   Health endpoints, Telegram
└── utils/        Config, logging
```

---

## 4. LOS 3 MOTORES DE TRADING

### Motor 1: Arbitraje Intra-Kalshi
**Tesis:** En cada evento con N outcomes, sus precios deberían sumar ≥100¢. Si suman <100¢ después de comisión, arbitraje matemático garantizado.

**Frecuencia esperada 2026:** 1-3 oportunidades/semana en mercados nicho (políticos, weather, eventos culturales). En MLB/NBA mainstream casi cero — los HFT institucionales los matan en milisegundos.

**Sizing:** Máximo de liquidez disponible, hasta 100 contratos por outcome.

**Riesgo:** 0% si se ejecuta correctamente (riesgo real es ejecución parcial — necesita rollback).

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

**ROI mensual del motor:** 0.3-0.7%

### Combinado: 1.5-3% mensual realista

---

## 5. RISK MANAGEMENT (HARDCODED - NO TOCAR SIN APROBACIÓN EXPLÍCITA)

### Stop-loss escalonado
| Nivel | Umbral | Acción |
|---|---|---|
| Diario | -3% capital activo | Pausa automática 24h |
| Semanal | -8% capital activo | Pausa 7 días + Telegram alert |
| Mensual | -15% capital activo | Kill-switch total + revisión código |

### Caps de exposición
- **Sizing máximo por trade:** 5% capital activo (cap absoluto independiente de Kelly)
- **Exposure simultáneo total:** 25% capital activo
- **Posiciones simultáneas máximas:** 10
- **Kelly fraction:** 0.25 (¼ Kelly, NO full Kelly)

### Capital management
- **Capital activo en Kalshi:** escalado según validación
  - **Fase 1 (días 1-30):** `KALSHI_ENV=production` con `TRADING_ENABLED=false`.
    Captura de data REAL con orderbooks reales de producción. Sin trading.
    Capital activo $0. Razón: demo de Kalshi tiene data sintética insuficiente
    para entrenar Motor 1 (arbitraje requiere ineficiencias reales, no simuladas).
  - **Fase 2 (días 31-60):** `TRADING_ENABLED=true` tras 7+ días de captura
    estable y detección de oportunidades funcionando. Capital $300.
  - **Fase 3 (días 61-90):** $800 si Fase 2 cierra positivo.
  - **Fase 4 (día 91+):** hasta $2,000 si Fase 3 cierra positivo.
- **Reserva en cuenta bancaria:** siempre $500 mínimo.
- **Riesgo aceptado en Fase 1:** activar `TRADING_ENABLED=true` por error
  mientras estamos sobre API production tradearía dinero real sin validación
  previa. Mitigación: cambio de la flag requiere checklist explícito (abajo).

### Reglas de operación
1. **NO trades automáticos** hasta Semana 2 del roadmap.
2. **`TRADING_ENABLED=true`** solo después de validación explícita. El bot
   opera sobre API production desde Fase 1, pero la flag bloquea ejecución
   real hasta cumplir el checklist.
3. **Activación de `TRADING_ENABLED` requiere TODO lo siguiente:**
   - [ ] Mínimo 7 días corriendo sin crashes ni reinicios anómalos.
   - [ ] `tracked_markets > 50` estable durante esos 7 días.
   - [ ] DB con `market_snapshots` y `orderbook_events` poblados continuamente.
   - [ ] Logs muestran arbitrajes detectados (aunque no se ejecuten).
   - [ ] RiskManager validado con tests de integración (no solo unitarios).
   - [ ] Capital activo confirmado a $300 (no más en Fase 2).
   - [ ] Decisión documentada en commit message del cambio de la flag.
4. **Cualquier cambio a parámetros de risk** (stop-loss, exposure, sizing,
   Kelly fraction) requiere aprobación explícita de Noel.
5. **Después de modificación de risk params**, mínimo 48h de monitoring antes
   de cambios adicionales.

---

## 6. KALSHI API SPECIFICS

### Authentication
- **RSA-PSS** signing requerido (NO HMAC tradicional)
- Headers: `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`
- Mensaje a firmar: `{timestamp_ms}{METHOD}{path}` (path incluye query string en GET)
- Llave RSA 4096 mínimo, en formato PEM
- Llaves diferentes para demo y producción (en Fase 1 usamos las de producción)

### Endpoints clave
- **REST base:** `https://demo-api.kalshi.co/trade-api/v2` (demo, NO usado en Fase 1)
- **REST base:** `https://api.elections.kalshi.com/trade-api/v2` (producción, ACTIVO)
- **WS prod:** `wss://api.elections.kalshi.com/trade-api/ws/v2`

### Rate limits
- 100 req/sec REST en producción
- WS sin límite específico pero respeta backpressure
- **Importante:** al arranque del container puede topar 429 si hay rate limit
  acumulado de despliegues previos. El cliente respeta header `Retry-After`.

### Comisiones (CRÍTICO para cálculo de edge)
- **~7% sobre profit aproximación high-level** (para Kelly sizing rough).
- **Fórmula real (USAR para Motor 1 / arbitraje):**
  `fee_cents = ceil(0.07 * count * price_yes_cents * (100 - price_yes_cents) / 10000)`
- Aplica solo a trades rentables.
- **Cualquier cálculo de edge en arbitraje debe usar la fórmula real, no la
  aproximación.** En arbitrajes con profit gross de pocos centavos, la
  diferencia decide si hay edge o no.

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

### Decisiones que tomaremos cuando aplique (no ahora)

| Decisión | Trigger | Notas |
|---|---|---|
| Migrar SQLite → Postgres | >100k trades/mes | No anticipado en primer año |
| Multi-VPS (HA setup) | Generando >$200/mes | Solo si retornos justifican costo |
| Agregar Polymarket | Después de 6 meses Kalshi exitoso | Diferente regulación, evaluar |
| Market making strategy | Después de motor 1-3 estables | Estrategia más rentable pero compleja |
| ML/AI offline para análisis | Tener >1000 trades de data | NUNCA en hot path de decisiones |
| Co-locación cerca de Kalshi servers | >$1000/mes profit | Solo si latencia <5ms paga |
| Migración a aiosqlite + WAL | Si lag de DB pasa de 30ms | Refactor mayor, posponer |
| Multi-outcome arbitrage en Motor 1 | Después de v1 binario validada | Más complejo de ejecutar |

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
- Cliente REST respeta `Retry-After` header de Kalshi.
- `/status` endpoint expone `capture_running`, `ws_connected`, `last_ws_message`
  para detectar muertes silenciosas.

---

## 10. ROADMAP TÉCNICO

### Semana 1 — Infrastructure ✅ COMPLETADO
- [x] Auth RSA-PSS (3/3 tests pasan)
- [x] Cliente REST async con retries
- [x] Cliente WebSocket con reconexión
- [x] SQLite schema (5 tablas)
- [x] Health server FastAPI
- [x] Dockerfile multi-stage
- [x] docker-compose.yml para Coolify
- [x] Documentación de deploy
- [x] Deploy en Coolify VPS
- [x] Fixes de resilience aplicados (data_capture retry, 429 handling, /status visibility)

**Pendiente Noel antes de Semana 2:**
- [ ] Verificar `/health` retorna 200 después de fixes
- [ ] Verificar `/status` muestra `capture_running: true`, `tracked_markets > 50`
- [ ] Confirmar DB tiene rows en `market_snapshots` y `orderbook_events` (>0 después de 10 min)
- [ ] 24h+ de captura estable antes de retomar Motor 1
- [ ] Telegram chat_id corregido y `alert_startup` funciona
- [ ] Coolify healthcheck apunta a puerto 8080 (no 8000)

### Semana 2 — Motor 1: Arbitraje Intra-Kalshi (binario + multi-outcome)

**Decisiones tomadas (2026-05-09):**
- Scope v1: arbitraje binario (YES+NO) + multi-outcome (N markets en 1 event).
- Ejecución: FOK paralelo con `asyncio.gather` + rollback automático
  sell-to-market en partial fills.

**Fases internas (orden bloqueante):**
- [ ] Fase 1: `src/math/fees.py` (fee real Kalshi) + `src/math/kelly.py`
- [ ] Fase 2: `src/risk/manager.py` con persistencia SQLite (tabla `RiskState`)
- [ ] Fase 3: Migración `kalshi_rest.py` a API V2 fixed-point format
- [ ] Fase 4: `src/strategies/motor_1_arbitrage/detector.py` (binario + multi)
- [ ] Fase 5: `src/strategies/motor_1_arbitrage/executor.py` con rollback
- [ ] Fase 6: Integración con runner + WS subscription a `orderbook_delta`
- [ ] Fase 7: Demo testing 7 días con `TRADING_ENABLED=false` → activación

### Semana 3 — Motor 2: Kalshi vs Sportsbooks
- [ ] Cliente The Odds API
- [ ] Algoritmo no-vig
- [ ] Matcher de eventos cross-platform
- [ ] Quarter Kelly sizing
- [ ] Caps de risk manager integrados
- [ ] Live testing en demo con bankroll completo

### Semana 4 — Motor 3 + Hardening
- [ ] CLV strategy (open vs close pricing)
- [ ] Risk manager activo con kill-switches
- [ ] Telegram alerts (trades, P&L, errors)
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
| No hay retry exponencial en WS reconnect | Baja | Semana 4 |
| Tests cubren solo signer y config | Media | Agregar tests semana 2-3 |
| Sin alerting si data capture muere silenciosamente | Resuelto 2026-05-09 | `/status` ahora expone `capture_running` y `ws_connected` |
| SQLite sin VACUUM scheduled | Baja | Agregar a cron mensual |
| Sin metrics export (Prometheus, etc.) | Baja | No anticipado primer año |
| Operando en `KALSHI_ENV=production` sin validación previa en demo | Media | Mitigado por `TRADING_ENABLED=false` + checklist sección 5 |
| Telegram chat_id no validado en arranque | Baja | Validar al boot que `send_alert` funciona, sino warning |
| `data_capture` sin retry de discovery (bug arranque 2026-05-09) | Resuelto | Loop con backoff exponencial 5s→300s |
| Cliente REST no respetaba `Retry-After` en 429 | Resuelto | Parseado con cap de 30s |
| Migración API V2 fixed-point pendiente (`yes_price` integer deprecating) | Media | Fase 3 de Motor 1 |

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
muertes silenciosas (lección Mayo 9, 2026).

❌ **Aproximación de fee 7% flat en Motor 1:** La aproximación sirve para
sizing de Motor 2/3 (Kelly), pero arbitraje requiere la fórmula real de
Kalshi (`ceil(0.07 * count * price * (1-price))`). En profits chicos, la
diferencia decide si hay edge o no.

---

## 13. CONTACTO Y OWNERSHIP

- **Sole operator:** Noel Pineda
- **Repos:** GitHub privado
- **Hosting:** DigitalOcean VPS (compartido con Nortex stack)
- **Capital:** Personal, segregado en cuenta Kalshi separada
- **Tax filing:** US individual (1099 from Kalshi)

---

**FIN DE KALSHI_BOT_CONTEXT.md**
