"""
Arranque del MES DE OPERACIÓN en un solo comando (2026-07-29).

Reemplaza la coreografía manual (mirar /status, contar posiciones, correr
clear_kill_switch, revisar 20 flags a ojo) por UNA corrida que verifica todo y
reporta el veredicto. El gatillo final sigue siendo HUMANO: sin `--arrancar` +
la confirmación tipeada, este script NO muta nada.

Uso (en el container):
    # 1) Diagnóstico READ-ONLY — no toca nada. Lo puede correr cualquiera/agente:
    docker exec kalshi-bot python -m scripts.arranque_mes

    # 2) Arranque real — pide confirmación tipeada, la escribe una PERSONA:
    docker exec -it kalshi-bot python -m scripts.arranque_mes --arrancar

Qué verifica, en orden (aborta al primer bloqueante):
    1. Flags del mes: compara el env EFECTIVO contra la lista canónica y lista
       los que faltan (con el valor esperado, listo para pegar en Coolify).
    2. Posiciones abiertas en Kalshi (lectura autenticada, sin operar).
    3. Kill-switch persistente: si está puesto, exige posiciones=0 para limpiarlo.
    4. Capital efectivo y su piso (un capital bajo el piso pausa entradas: el mes
       no arrancaría igual).
    5. Salud del feed: books inicializados, sids deshabilitados, incoherencias.

Lo que este script NO hace, por diseño:
    - NO cambia env vars (viven en Coolify; solo puede REPORTAR diferencias).
    - NO coloca ni cancela órdenes. NO cierra posiciones.
    - NO limpia el kill-switch sin `--arrancar` Y la confirmación tipeada.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from src.storage import models
from src.utils.config import get_settings

#: Flags canónicos del mes de operación (evidencia y racional en el PR del plan).
#: `None` como valor esperado = "cualquier valor sirve, solo se reporta".
FLAGS_DEL_MES: dict[str, Any] = {
    "TRADING_ENABLED": True,
    "MOTOR_1_ARBITRAGE_ENABLED": True,
    "MOTOR_1_EXECUTION_ENABLED": True,
    # Detección (no ejecución) de la banda fina 1.0-1.5pp: se MIDE con settlements
    # reales sin pagar por averiguarlo. El piso de ejecución de M1 queda en 1.5.
    "MIN_EDGE_PCT": 1.0,
    "MOTOR_2_SPORTSBOOK_ENABLED": True,
    "MOTOR_2_ENTRY_EXECUTION_ENABLED": True,
    "MOTOR_2_EXECUTION_ENABLED": True,
    "MOTOR_2_MIN_EDGE_PCT": 1.0,
    "MOTOR_2_MAX_STAKE_PCT": 1.0,
    "MOTOR_2_TAKE_PROFIT_ENABLED": True,
    "MOTOR_3_CLV_ENABLED": True,
    "MOTOR_3_EXECUTION_ENABLED": True,
    "MOTOR_3_MANAGES_ORPHANS": True,
    "MOTOR_REST_ENABLED": True,
    "MOTOR_REST_EXECUTION_ENABLED": True,
    "MOTOR_8_OFI_ENABLED": True,
    "MOTOR_9_SPILLOVER_ENABLED": True,
    "MAX_TRADE_SIZE_PCT": 3.0,
    "MAX_EVENT_DIRECTIONAL_EXPOSURE_USD": 15.0,
    "MAX_DAILY_LOSS_FLOOR_USD": 25.0,
}

CONFIRMACION = "ARRANCAR EL MES"


def _fmt_env(value: Any) -> str:
    """Valor como lo espera un archivo .env (bools en minúscula, floats sin ruido)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(value)
    return str(value)


def revisar_flags(settings: Any) -> tuple[list[str], list[str]]:
    """(faltantes, ok) comparando el env EFECTIVO contra FLAGS_DEL_MES.

    'Faltante' = el valor efectivo NO coincide con el esperado (incluye el caso de la
    variable ausente en Coolify, que aplica el default de Pydantic — la trampa
    documentada: ausente en el env ≠ inexistente en el código)."""
    faltantes: list[str] = []
    ok: list[str] = []
    for nombre, esperado in FLAGS_DEL_MES.items():
        actual = getattr(settings, nombre, None)
        if actual is None:
            faltantes.append(f"{nombre}={_fmt_env(esperado)}   # el campo no existe en Settings")
            continue
        if esperado is None or actual == esperado:
            ok.append(nombre)
        else:
            faltantes.append(f"{nombre}={_fmt_env(esperado)}   # actual: {_fmt_env(actual)}")
    return faltantes, ok


def _position_count(p: dict) -> int:
    """Igual que clear_kill_switch: `position_fp` es string fixed-point ('-1.00')."""
    raw = p.get("position_fp", p.get("position"))
    try:
        return int(round(float(raw))) if raw is not None else 0
    except (ValueError, TypeError):
        return 0


async def _posiciones_abiertas() -> list[dict] | None:
    """Posiciones con cantidad != 0. None si la API no se pudo leer (NO se asume vacío:
    'no sé' jamás debe habilitar un arranque)."""
    from src.clients.kalshi_rest import KalshiRestClient

    try:
        async with KalshiRestClient() as client:
            resp = await client.get_positions()
    except Exception as exc:  # noqa: BLE001 — se reporta, no se traga
        print(f"   ⚠️  No se pudo leer posiciones de Kalshi: {type(exc).__name__}: {exc}")
        return None
    positions = resp.get("market_positions", resp.get("positions", []))
    return [p for p in positions if _position_count(p) != 0]


def _leer_posiciones() -> list[dict] | None:
    """Seam SÍNCRONO sobre la lectura async (así main() no mezcla asyncio con el flujo de
    decisión, y los tests inyectan acá sin parchear el event loop)."""
    return asyncio.run(_posiciones_abiertas())


def _estado_capital() -> dict[str, Any]:
    from src.risk.manager import RiskManager

    try:
        return RiskManager.capital_status()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def _estado_feed() -> dict[str, Any]:
    """Salud del feed desde el manager V2 vivo (si este proceso lo tiene). En una corrida
    por `docker exec` el proceso es OTRO, así que normalmente no hay instancia: se dice."""
    from src.monitoring.health import BotState

    mgr = BotState.v2_manager
    if mgr is None:
        return {"instancia": "no visible desde este proceso (usar /status del bot)"}
    s = mgr.stats()
    return {
        "books_initialized": s.get("initialized_tickers"),
        "stale_tickers": s.get("stale_tickers"),
        "sids_disabled": sorted(getattr(mgr, "_recovery_disabled_sids", [])),
        "incoherent_quarantines_total": s.get("incoherent_quarantines_total"),
        "stale_snapshots_ignored_total": s.get("stale_snapshots_ignored_total"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--arrancar",
        action="store_true",
        help="habilita las MUTACIONES (limpiar el kill-switch). Pide confirmación tipeada.",
    )
    args = ap.parse_args(argv)

    settings = get_settings()
    print("=" * 68)
    print(
        f"ARRANQUE DEL MES — modo: {'ARRANCAR (muta)' if args.arrancar else 'CHEQUEO (read-only)'}"
    )
    print("=" * 68)

    # ── 1. Flags ────────────────────────────────────────────────────────────
    faltantes, ok = revisar_flags(settings)
    print(f"\n1) FLAGS DEL MES: {len(ok)}/{len(FLAGS_DEL_MES)} en el valor esperado")
    if faltantes:
        print("   ⛔ Faltan (pegar en Coolify → Environment Variables → producción):")
        for linea in faltantes:
            print(f"      {linea}")
    else:
        print("   ✅ Todos los flags del mes están aplicados.")

    # ── 2. Posiciones ───────────────────────────────────────────────────────
    print("\n2) POSICIONES ABIERTAS EN KALSHI")
    posiciones = _leer_posiciones()
    if posiciones is None:
        print("   ⚠️  DESCONOCIDO (la API no respondió) — no se puede limpiar el kill-switch.")
    elif posiciones:
        print(f"   ⚠️  {len(posiciones)} posición(es) abierta(s):")
        for p in posiciones:
            print(f"      - {p.get('ticker')}: {_position_count(p)}")
    else:
        print("   ✅ Sin posiciones abiertas.")

    # ── 3. Kill-switch ──────────────────────────────────────────────────────
    engaged, razon = models.kill_switch_engaged()
    print("\n3) KILL-SWITCH PERSISTENTE")
    print(f"   {'⛔ ENGAGED' if engaged else '✅ libre'}{f' — {razon}' if engaged else ''}")

    # ── 4. Capital ──────────────────────────────────────────────────────────
    cap = _estado_capital()
    print("\n4) CAPITAL")
    print(f"   {cap}")
    if cap.get("is_paused"):
        print(
            "   ⚠️  capital.is_paused=True: el cash está BAJO el piso "
            f"(CAPITAL_FLOOR_USD={settings.CAPITAL_FLOOR_USD}) → NINGÚN motor abre "
            "posiciones nuevas, aunque los flags estén en true."
        )

    # ── 5. Feed ─────────────────────────────────────────────────────────────
    print("\n5) FEED (V2)")
    print(f"   {_estado_feed()}")

    # ── Veredicto ───────────────────────────────────────────────────────────
    print("\n" + "-" * 68)
    bloqueantes: list[str] = []
    if faltantes:
        bloqueantes.append(f"{len(faltantes)} flag(s) sin aplicar")
    if engaged:
        bloqueantes.append("kill-switch engaged")
    if cap.get("is_paused"):
        bloqueantes.append("capital bajo el piso")

    if not args.arrancar:
        if bloqueantes:
            print(f"CHEQUEO: el mes NO puede arrancar todavía — {', '.join(bloqueantes)}.")
            print("Corré de nuevo con --arrancar cuando quieras limpiar el kill-switch.")
        else:
            print("CHEQUEO: todo listo. Corré con --arrancar para el gatillo final.")
        return 0

    # ── Mutación (solo con --arrancar) ──────────────────────────────────────
    if not engaged:
        print("Kill-switch ya libre: no hay nada que limpiar.")
        if bloqueantes:
            print(f"OJO, quedan bloqueantes: {', '.join(bloqueantes)}.")
        return 0
    if posiciones is None:
        print("⛔ ABORT: no se pudo verificar posiciones. No se limpia el kill-switch a ciegas.")
        return 1
    if posiciones:
        print("⛔ ABORT: hay posiciones abiertas. Cerralas/verificá el settlement primero.")
        return 1

    print(f"Para limpiar el kill-switch, escribí exactamente: {CONFIRMACION}")
    try:
        tecleado = input("> ").strip()
    except EOFError:
        print("⛔ ABORT: sin TTY (¿falta -it en docker exec?). No se limpió nada.")
        return 1
    if tecleado != CONFIRMACION:
        print("Cancelado. El kill-switch sigue engaged.")
        return 1

    models.clear_kill_switch()
    print("🔓 Kill-switch LIMPIADO.")
    if faltantes:
        print(
            f"⚠️  Todavía faltan {len(faltantes)} flag(s): pegalos en Coolify y REDEPLOYÁ. "
            "Hasta entonces el mes corre con la config vieja."
        )
    else:
        print("REDEPLOYÁ para que la Capa A reconstruya los executors con los flags del mes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
