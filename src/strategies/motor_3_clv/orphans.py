"""
Huérfanas de Motor 1 (Bug 4, incidente 2026-07-07) — helper compartido engine/executor.

Una HUÉRFANA es un BUY fillado de motor_1_arbitrage cuyo arb quedó SIN la pata hermana (la del
otro side del mismo arb_id no filló: rollback abortado, cancelada, error). Es exposición
direccional pura que nadie gestionaba: Motor 3 la ignoraba (origen desconocido) y quedaba a
merced del settle (pérdida máxima) o intervención manual.

REGLA DE ORO (misma por la que motor_rest_arb está excluido de los origins): un arb con AMBAS
patas filladas es un hedge de payout garantizado — vender una pata suelta rompe el hedge y
convierte profit lockeado en pérdida. Este helper solo declara huérfanas a las patas cuyo
arb_id NO tiene la pata del otro side fillada. Ante la duda (sin arb_id parseable pero con
hermanas del otro side en el mismo ticker), NO es huérfana.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.storage.models import Trade

_STRATEGY = "motor_1_arbitrage"


def arb_id_of(trade: Trade) -> str | None:
    """arb_id desde notes ('arb_id=<uuid>', patrón de _persist_intents). None si no está."""
    notes = trade.notes or ""
    for token in notes.split():
        if token.startswith("arb_id="):
            return token[len("arb_id=") :] or None
    return None


def motor1_orphan_buys(buys: Iterable[Trade]) -> list[Trade]:
    """Filtra a las patas HUÉRFANAS de Motor 1 (gestionables por Motor 3).

    Input: BUYs abiertos (action=buy, status=filled, closed_by_clv=False) de cualquier
    estrategia — el helper filtra motor_1_arbitrage y agrupa por arb_id.
    Output: solo las patas cuyo arb NO quedó hedged (un solo side fillado). Ambos sides
    fillados → hedge → se excluye el arb entero. Sin arb_id → conservador: NO huérfana."""
    m1 = [b for b in buys if b.strategy == _STRATEGY]
    by_arb: dict[str, list[Trade]] = {}
    for b in m1:
        aid = arb_id_of(b)
        if aid is None:
            continue  # sin arb_id no se puede probar orfandad → no gestionar (conservador)
        by_arb.setdefault(aid, []).append(b)

    orphans: list[Trade] = []
    for legs in by_arb.values():
        if len({leg.side for leg in legs}) >= 2:
            continue  # ambas patas vivas → hedge de payout garantizado → NO tocar
        orphans.extend(legs)
    return orphans
