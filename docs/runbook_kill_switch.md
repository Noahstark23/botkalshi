# Runbook: kill-switch a las 3am (pata expuesta, rollback no llenó)

**Para:** Noel, operando solo, posiblemente recién despertado. Pasos cortos, en orden.
**Cuándo usarlo:** sonó la alerta urgente de Telegram:
`Motor REST KILL-SWITCH: rollback no se llenó, posición abierta ['KX...']`

**Qué significa:** el bot compró una pata de un arbitraje, la otra no llenó, intentó
liquidar la pata llena con sell a 1¢ (3 intentos) y **no se llenó** → tenés una
**posición direccional abierta** en el ticker de la alerta. El executor quedó **pausado**
(no abre posiciones nuevas).

---

## Paso 0 — NO reinicies nada

- **NO redeployes, NO reinicies el contenedor, NO toques flags todavía.**
- La pausa del executor vive **EN MEMORIA**: un restart la **borra** y el motor volvería a
  operar como si nada. El estado actual (pausado) es el estado seguro.

## Paso 1 — Confirmá la exposición real (2 minutos)

Opción A (la simple): **Kalshi UI** → login → **Portfolio → Positions** → buscá el ticker
de la alerta. Anotá: lado (YES/NO), contratos, precio promedio.

Opción B (desde el host, sin UI):
```bash
docker exec -it kalshi-bot python -c "
import asyncio
from src.clients.kalshi_rest import KalshiRestClient
async def main():
    async with KalshiRestClient() as c:
        r = await c.get_positions()
        for p in r.get('market_positions', r.get('positions', [])):
            if p.get('position'): print(p)
asyncio.run(main())"
```

- **Si NO hay posición** (la alerta fue carrera/falso positivo): saltá al Paso 4 igual
  (apagar y entender por qué alertó).
- **Si hay posición:** seguí al Paso 2.

## Paso 2 — Cerrá la exposición a mano (Kalshi UI)

En el market del ticker, vendé lo que tengas (orden **limit, lado que poseés, action=sell**):

1. Mirá el book: ¿hay bids? → vendé **al bid actual** (no a 1¢: vos podés decidir el precio,
   el bot no podía esperar).
2. ¿Book vacío / sin bids? Decidí entre:
   - **Esperar liquidez** (dejá la sell limit puesta a un precio razonable), o
   - **Dejar correr a settlement** si el evento resuelve pronto y el precio de tu lado lo
     justifica (una pata de arb comprada barata puede incluso ganar sola).
3. Anotá el PnL realizado de la salida (lo vas a necesitar para el post-mortem).

## Paso 3 — Confirmá que el bot está pausado

- En los logs (Coolify → stdout): si el motor detecta otro arb vas a ver
  `rest_exec.rejected reason=circuit_breaker_paused` — eso ES la pausa funcionando.
- ⚠️ **Gap conocido:** este kill-switch NO setea `BotState.is_paused` (el `/status` puede
  decir que no está pausado). La pausa real es interna al `RestExecutor`. No te confíes
  del `/status` para esto.

## Paso 4 — Apagá el trading ANTES de investigar

En Coolify: **`TRADING_ENABLED=false`** → redeploy.

- Esto vuelve a **shadow puro por construcción** (el executor ni se construye) y convierte
  la pausa en-memoria en un estado **persistente** (sobrevive restarts).
- La detección/captura sigue corriendo (no perdés data).

## Paso 5 — NO reactivar hasta entender la causa

Post-mortem mínimo antes de volver a prender:

1. **Logs:** buscá la secuencia `motor_rest.exec.outcome` → `rest_exec.rollback.not_filled`
   → `rest_exec.kill_switch`. ¿Por qué no llenó el rollback? (¿book vacío? ¿mercado
   suspendido? ¿error de API?)
2. **DB:** la fila de `edge_windows` del trade (tiene `leg_states`, `kill_switch_fired=1`,
   `rollback_filled=0`, `cycle_latency_ms`).
3. **Posición:** confirmá con `get_positions` (Paso 1, opción B) que quedó en CERO.

**Checklist de reactivación (todos sí, si no NO se prende):**
- [ ] Causa entendida y escrita (una línea alcanza).
- [ ] Posición confirmada cerrada (`get_positions` limpio).
- [ ] Fix o mitigación aplicada (o decisión explícita de aceptar el riesgo).
- [ ] Backup fresco de la DB.
- [ ] Recién ahí: `TRADING_ENABLED=true` + redeploy.

---

## Referencia rápida de síntomas en Telegram

| Mensaje | Significa | Acción |
|---|---|---|
| `✅ Motor REST: Arb ejecutado` | Fill completo, cero exposición | Nada — es el caso feliz |
| `Motor REST: rollback ejecutado ... recuperada: True` | Hubo pata huérfana pero se cerró sola | Revisar logs cuando puedas; no urgente |
| `Motor REST: rollback ejecutado ... recuperada: False` + `KILL-SWITCH` | **Pata abierta sin cerrar** | **ESTE runbook, ahora** |
| `Motor REST: orden rechazada — circuit breaker pausado` | 3 rollbacks en 1h → motor pausado solo | Paso 4 (apagar) + post-mortem; sin urgencia de posición |
| `Risk Event: kill_switch` (stop-loss) | Límite diario/semanal/mensual tocado | Bot pausado vía BotState; investigar PnL |
