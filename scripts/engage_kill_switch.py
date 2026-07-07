"""
Engage MANUAL del kill-switch persistente (contención inmediata).

Espejo de clear_kill_switch.py. Lo graba 'engaged' en operational_state (sobrevive
redeploys: el boot re-hidrata la pausa vía _rehydrate_kill_switch) Y pausa el proceso
ACTUAL en runtime si el health server responde (POST /admin/pause). Nació de la
contención del incidente 2026-07-07: la pausa runtime del circuit breaker se perdió
con el auto-deploy del merge y no había forma de un solo comando de frenar persistente.

Solo se levanta con scripts/clear_kill_switch.py (verifica posiciones=0 + "CLEAR").

Uso (en el host):
    docker exec -it kalshi-bot python -m scripts.engage_kill_switch "motivo de la pausa"
"""

from __future__ import annotations

import sys

import httpx

from src.storage import models
from src.utils.config import get_settings


def main() -> int:
    reason = " ".join(sys.argv[1:]).strip() or "engage manual (sin motivo)"

    engaged, prev = models.kill_switch_engaged()
    if engaged:
        print(f"Kill-switch YA estaba engaged: {prev!r} — no se pisa el motivo original.")
    else:
        models.engage_kill_switch(f"manual: {reason}")
        print(f"✅ Kill-switch PERSISTENTE engaged: {reason!r} (sobrevive redeploys).")

    # Best-effort: pausar también el proceso VIVO (la persistencia solo aplica al próximo boot).
    s = get_settings()
    url = f"http://127.0.0.1:{s.HEALTH_PORT}/admin/pause"
    try:
        resp = httpx.post(url, params={"reason": f"engage_kill_switch: {reason}"}, timeout=5.0)
        print(f"✅ Proceso actual pausado vía /admin/pause ({resp.status_code}).")
    except Exception as exc:
        print(
            f"⚠️ No se pudo pausar el proceso VIVO ({type(exc).__name__}: {exc}).\n"
            "   La pausa persistente quedó grabada igual — pero el proceso actual SIGUE\n"
            "   operando hasta un restart. Usá el Stop de Coolify o el endpoint /admin/pause."
        )
    print("Para levantar la pausa: python -m scripts.clear_kill_switch (posiciones=0 + CLEAR).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
