"""
FairValueBook — registry in-memory compartido de fair values (Motor 5 F1, plan §1.2).

Motor 2 calcula el fair (consenso sportsbook sin vig) por market_ticker en cada ciclo del
poller, pero el valor vivía solo dentro de find_signals. El Motor 5 (market maker) necesita
consumirlo como precio de referencia. Este registry es el canal:

  - Motor 2 PUBLICA {market_ticker → fair_prob} al final de cada ciclo con odds REALES
    (con la fuente fake no se publica — un fair de fixture no es precio de referencia).
  - Motor 5 CONSUME con TTL explícito (MOTOR_MM_FAIR_TTL_SEC): un fair más viejo que el
    TTL no existe (sin fair fresco → no se cotiza ese ticker; funnel: skip_stale_fair).

Patrón ClassVar (como BotState): estado de proceso, sin persistencia — el fair se recalcula
en ~300s y sobrevivir restarts no aporta (un fair rehidratado estaría stale igual).
Asyncio single-thread: sin locks (publish/fresh corren en el mismo event loop).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class FairValue:
    fair_prob: float  # [0.0, 1.0], post no-vig (shape de ConsensusSignal.odds_api_fair_prob)
    computed_at: datetime  # aware UTC


class FairValueBook:
    """Registry proceso-global: market_ticker → FairValue. Publica Motor 2, consume Motor 5."""

    _book: ClassVar[dict[str, FairValue]] = {}

    @classmethod
    def publish(cls, fairs: dict[str, float], *, now: datetime | None = None) -> None:
        """Upsert de los fairs del ciclo. NO borra tickers ausentes: un partido que este
        ciclo no matcheó (odds API parcial) conserva su último fair y expira por TTL."""
        now = now or datetime.now(UTC)
        for ticker, prob in fairs.items():
            cls._book[ticker] = FairValue(fair_prob=prob, computed_at=now)

    @classmethod
    def fresh(cls, ttl_sec: float, *, now: datetime | None = None) -> dict[str, FairValue]:
        """Los fairs con edad ≤ ttl_sec. Purga los vencidos (el book no crece sin tope)."""
        now = now or datetime.now(UTC)
        expired = [
            t for t, fv in cls._book.items() if (now - fv.computed_at).total_seconds() > ttl_sec
        ]
        for t in expired:
            del cls._book[t]
        return dict(cls._book)

    @classmethod
    def size(cls) -> int:
        return len(cls._book)

    @classmethod
    def clear(cls) -> None:
        """Solo para tests."""
        cls._book.clear()
