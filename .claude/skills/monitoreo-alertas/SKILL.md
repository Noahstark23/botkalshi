---
name: monitoreo-alertas
description: Capa de observabilidad del bot — BotState (estado runtime compartido), endpoints /health /ready /status, alertas de Telegram (best-effort SIEMPRE), dashboard on-demand, memory monitor y las alertas del DiskGuard. Usar al agregar visibilidad, diagnosticar "el bot no responde", tocar health.py/telegram_alerts.py, o decidir qué/cuándo alertar.
---

# Monitoreo y alertas

`src/monitoring/` es la capa que deja VER el bot sin tocarlo. Regla madre: **la
observabilidad jamás tira el bot** — toda alerta y todo endpoint es best-effort;
un Telegram caído o un /status lento no pueden frenar un trade. Protocolo general:
skill `botkalshi`.

## Mapa

```
src/monitoring/
├── health.py           # FastAPI: /health /ready /status /stats /pause /resume + BotState
├── telegram_alerts.py  # send_alert + alert_* (startup/shutdown/risk/error/trade/bet)
├── dashboard.py        # /dashboard de Telegram on-demand (read-only)
└── memory_monitor.py   # alerta por uso de memoria (umbral %)
```

## BotState — el estado runtime compartido

Clase con atributos de CLASE (no instancia): heartbeat del WS, `last_error`/`record_error`,
referencia al OrderbookManagerV2, etc. Es el punto de encuentro entre las tasks del runner
y los endpoints. Reglas:

- **Todo loop registra y SIGUE** (Lección 7): `except` por tick → `BotState.record_error`
  + `logger.exception`, nunca `except: pass` ni dejar morir la task en silencio.
- Estado de clase = estado de PROCESO: los tests deben resetearlo (fixtures autouse).
- Igual patrón usan `RiskManager` (caché de balance) y `DiskGuard` (presión de disco).

## Telegram — cuándo alertar

`send_alert(msg, urgent=False)` es fire-and-forget con manejo interno de errores. Las
alertas existen para que el operador actúe: alertar TRANSICIONES, no estados (el DiskGuard
alerta ok→warn→critical→ok una vez por cambio, no cada tick — anti-spam). `urgent=True`
solo cuando el operador debe actuar YA (kill-switch, disco critical, rollback abortado).
Toda alerta nueva se envuelve en `suppress(Exception)` o equivalente: best-effort SIEMPRE.

## Endpoints

- `/health` liveness (Coolify health-check: si falla → unhealthy → restart loop; un bot
  "unhealthy" tras un swap de DB suele ser ownership — ver skill `operacion-disco-db`).
- `/ready` readiness; `/status` el panel completo (motores, capital efectivo, errores,
  books); `/pause` `/resume` operan el bot por HTTP.
- El dashboard de Telegram es READ-ONLY por diseño: comandos que MUTAN van por env vars
  o scripts del operador, no por chat.

## Centro de comando (C1, 2026-07-12) — `monitoring/command_center.py`

Comandos on-demand por el MISMO loop del dashboard (solo `TELEGRAM_CHAT_ID`; otros chats
= silencio total): `/incidentes` (RiskEvents), `/salud` (pausa, último error, books V2
stale/desync/recovery), `/funnel` (último Motor2FunnelSnapshot), `/pnl` (ventanas vs
límites de stop-loss con pisos), `/posiciones` (+ filled sin settle), `/disco`
(DiskGuard + tamaños DB), `/ayuda`. Cada builder: read-only, sesión propia, best-effort
(un builder roto responde el error en el chat, el loop sigue).

**Tiers del plan** (C1 implementado; C2/C3 diseñados, NO construidos): Nivel 0 = leer
(esto); Nivel 1 = `/pausar` (dirección segura, con RiskEvent de auditoría); Nivel 2 =
`/reanudar` con PRE-CHECKS automáticos (desyncs/rollbacks recientes, residuales sobre
cap) + token de un solo uso con TTL. **Línea roja innegociable**: levantar el
kill-switch persistente, prender flags de ejecución y colocar órdenes JAMÁS van por
chat — script/env + humano en el container, por diseño.

## Al agregar observabilidad nueva

1. ¿Es un loop? → task en el runner con try/except por tick + `record_error` + sigue.
2. ¿Alerta? → por transición, best-effort, con el dato accionable en el texto (números,
   no "algo pasó").
3. ¿Estado compartido? → atributo de clase con `reset()` para tests.
4. Nada de esta capa puede escribir estado de trading ni llamar `place_order`. Nunca.
5. ¿Check de salud o métrica nueva? → FALSABLE: qué estado del mundo lo pone en rojo,
   "no evaluable" ≠ verde, gracias acotadas con su porqué, y la frase "sube si y solo
   si X" escrita. Tres falsos-healthy ya costaron noches enteras (la regla completa y
   sus facturas: skills `diagnostics-recovery` y `desarrollo-bot` regla 12).
