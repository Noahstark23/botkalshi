"""
FairValueBook — registry in-memory compartido de fair values (Motor 5 F1, plan §1.2).

Motor 2 calcula el fair (consenso sportsbook sin vig) por market_ticker en cada ciclo del
poller, pero el valor vivía solo dentro de find_signals. El Motor 5 (market maker) necesita
consumirlo como precio de referencia. Este registry es el canal:

  - Motor 2 PUBLICA {market_ticker → fair_prob} al final de cada ciclo con odds REALES
    (con la fuente fake no se publica — un fair de fixture no es precio de referencia).
  - Motor 5 CONSUME con TTL explícito (MOTOR_MM_FAIR_TTL_SEC): un fair más viejo que el
    TTL no existe (sin fair fresco → no se cotiza ese ticker; funnel: skip_stale_fair).

Estado de proceso, sin persistencia — el fair se recalcula en ~300s y sobrevivir restarts
no aporta (un fair rehidratado estaría stale igual). Asyncio single-thread: sin locks
(publish/fresh corren en el mismo event loop).

INCIDENTE 2026-07-09 (P0, Motor 5 no cotizaba: `motor2.fair_book publicados=24` pero
`fair_fresh=0` sostenido): el store NO puede ser un ClassVar simple. Si el módulo se importa
bajo DOS claves de sys.modules — `src.strategies.fair_value_book` (import normal) y
`strategies.fair_value_book` (cuando /app/src cuela en PYTHONPATH además de /app) — Python
crea DOS objetos-clase con DOS ClassVar distintos: Motor 2 publica en uno y Motor 5 lee el
otro, vacío. La causa raíz es el PYTHONPATH del deploy (arreglar /app/src fuera del path),
pero el store se blinda acá: vive en un dict proceso-global anclado a `sys` (objeto único e
inmune a la doble carga del módulo), así cualquier copia de la clase comparte el MISMO libro.
Defensa en profundidad — aunque el path se re-rompa, el fair vuelve a fluir.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class FairValue:
    fair_prob: float  # [0.0, 1.0], post no-vig (shape de ConsensusSignal.odds_api_fair_prob)
    computed_at: datetime  # aware UTC


# Clave del store proceso-global. Vive en `sys` (SIEMPRE el mismo objeto sin importar cuántas
# veces se cargue este módulo) → un solo dict compartido por todas las copias de la clase.
# Versionada por si el shape de FairValue cambia entre deploys en caliente.
_GLOBAL_STORE_ATTR = "__botkalshi_fair_value_book_v1__"


def _shared_book() -> dict[str, FairValue]:
    """El dict proceso-global, creado a demanda. Inmune a la doble identidad del módulo
    (incidente 2026-07-09): distintas copias de la clase comparten este único dict."""
    book: dict[str, FairValue] | None = getattr(sys, _GLOBAL_STORE_ATTR, None)
    if book is None:
        book = {}
        setattr(sys, _GLOBAL_STORE_ATTR, book)
    return book


class FairValueBook:
    """Registry proceso-global: market_ticker → FairValue. Publica Motor 2, consume Motor 5.

    El estado vive en `_shared_book()` (anclado a sys), NO en un atributo de esta clase —
    ver el incidente 2026-07-09 en el docstring del módulo."""

    @classmethod
    def publish(cls, fairs: dict[str, float], *, now: datetime | None = None) -> None:
        """Upsert de los fairs del ciclo. NO borra tickers ausentes: un partido que este
        ciclo no matcheó (odds API parcial) conserva su último fair y expira por TTL."""
        now = now or datetime.now(UTC)
        book = _shared_book()
        for ticker, prob in fairs.items():
            book[ticker] = FairValue(fair_prob=prob, computed_at=now)

    @classmethod
    def fresh(cls, ttl_sec: float, *, now: datetime | None = None) -> dict[str, FairValue]:
        """Los fairs con edad ≤ ttl_sec. Purga los vencidos (el book no crece sin tope).

        `now` debe ser aware UTC (igual que computed_at) — mezclarlo con un naive lanza
        TypeError. El caller (engine) pasa datetime.now(UTC)."""
        now = now or datetime.now(UTC)
        book = _shared_book()
        expired = [t for t, fv in book.items() if (now - fv.computed_at).total_seconds() > ttl_sec]
        for t in expired:
            del book[t]
        return dict(book)

    @classmethod
    def size(cls) -> int:
        return len(_shared_book())

    @classmethod
    def clear(cls) -> None:
        """Solo para tests."""
        _shared_book().clear()
