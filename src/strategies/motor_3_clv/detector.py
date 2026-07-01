"""
CLV Detector (Motor 3, FASE 2) — lógica de time-series PURA para disparar la salida.

El PLAN nominal es salir cuando faltan entre 28 y 30 minutos para el cierre del mercado.
Desde el fix de auditoría 2026-07-01, la salida sigue DEBIDA hasta el cierre si la
ventana nominal se perdió (tick lento, outage, executor fallando): abandonar la posición
a resolución es exactamente el destino que este motor existe para evitar. El anti-spam
del piso de 28 min vive en el nivel de LOG (detect_and_log), no en la decisión.

SHADOW: el detector solo LOGUEA; NO ejecuta nada. El executor (FASE 3) y el lock
por-ticker viven aparte. Funciones puras (testeable sin red ni DB).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from loguru import logger

from src.storage.models import PortfolioPosition

# Ventana de salida NOMINAL: el plan es salir cuando close_time − now ∈ [FLOOR, CEIL].
CLV_EXIT_CEIL = timedelta(minutes=30)  # condición primaria: T-30 del cierre
CLV_EXIT_FLOOR = timedelta(minutes=28)  # borde del plan nominal (hoy solo granularidad de LOG)
_ZERO = timedelta(0)


def clv_exit_due(position: PortfolioPosition, now: datetime) -> bool:
    """
    True si la posición debe salir: 0 < (close_time − now) <= CLV_EXIT_CEIL.

    FALLBACK (fix auditoría 2026-07-01): la versión anterior exigía remaining >= FLOOR
    (28 min) y devolvía False PARA SIEMPRE debajo — pero el espaciado real entre
    evaluaciones es 60s + duración del tick (sync + retries + orderbooks), así que un
    tick lento o un outage que pisara la ventana [28,30] abandonaba la posición a
    resolución en silencio. Ahora la salida sigue debida hasta el cierre; una vez
    vendida, la posición desaparece (purge del poller + filtro de atribución) y el
    disparo cesa solo.

    - close_time None (aún no resuelto por el poller) → False (no se puede decidir).
    - remaining > 30 min → aún no (mercado lejos del cierre).
    - remaining <= 0 (cierre alcanzado / mercado cerrado) → False.

    `now` debe ser NAIVE UTC (igual que close_time) para no lanzar TypeError.
    """
    if position.close_time is None:
        return False
    remaining = position.close_time - now
    return _ZERO < remaining <= CLV_EXIT_CEIL


def scan_exits(positions: list[PortfolioPosition], now: datetime) -> list[PortfolioPosition]:
    """Subconjunto de posiciones cuya salida CLV está debida ahora."""
    return [p for p in positions if clv_exit_due(p, now)]


def detect_and_log(positions: list[PortfolioPosition], now: datetime) -> list[PortfolioPosition]:
    """
    SHADOW: detecta las salidas CLV debidas y las LOGUEA (no ejecuta). Devuelve la lista
    para que el engine/executor (FASE 3) la consuma cuando MOTOR_3_CLV_ENABLED lo permita.

    Anti-spam: dentro de la ventana nominal [28,30] loguea a INFO (2-3 líneas por
    posición); en el tramo de fallback (<28 min) baja a DEBUG — la DECISIÓN devuelta es
    idéntica en ambos tramos.
    """
    due = scan_exits(positions, now)
    for p in due:
        remaining = p.close_time - now  # type: ignore[operator]  # due ⇒ close_time != None
        log = logger.info if remaining >= CLV_EXIT_FLOOR else logger.debug
        log(f"[MOTOR 3 SHADOW] CLV Exit detectado para {p.ticker}, {p.count} contratos.")
    return due


@dataclass(frozen=True, slots=True)
class ClvScanSummary:
    """Foto de por qué Motor 3 dispara (o no) en un tick. Pura/testeable; sin red ni DB."""

    total: int  # posiciones sincronizadas desde la cartera
    with_close_time: int  # con close_time resuelto (sin esto NO se puede decidir)
    without_close_time: int  # close_time None → el poller aún no las resolvió
    due: int  # 0 < remaining <= 30min → salida debida AHORA (incluye fallback <28min)
    pre_window: int  # remaining > 30min → todavía lejos del cierre
    past_window: int  # remaining <= 0 → mercado ya cerrado (sin salida posible)
    nearest_ticker: str | None  # posición más próxima a entrar/dentro de la ventana
    nearest_remaining_min: float | None  # minutos hasta su cierre (None si ninguna aplica)

    def one_line(self) -> str:
        nxt = (
            f"{self.nearest_ticker} en {self.nearest_remaining_min:.1f}min"
            if self.nearest_ticker is not None
            else "—"
        )
        return (
            f"posiciones={self.total} con_close_time={self.with_close_time} "
            f"sin_close_time={self.without_close_time} | ventana(<=30m): "
            f"debidas={self.due} pre={self.pre_window} cerradas={self.past_window} "
            f"| próxima salida: {nxt}"
        )


def summarize_exits(positions: list[PortfolioPosition], now: datetime) -> ClvScanSummary:
    """
    Desglose de por qué Motor 3 dispara o no: cuántas posiciones hay, cuántas tienen
    close_time, cuántas están debidas/lejos/cerradas, y cuál es la más próxima a salir.
    Permite VERIFICAR en logs que el motor evalúa posiciones reales de Motor 2 y cuándo
    será la próxima salida — en vez del silencio total cuando no hay nada debido.
    """
    with_ct = [p for p in positions if p.close_time is not None]
    due = pre = past = 0
    nearest_ticker: str | None = None
    nearest_remaining: timedelta | None = None
    for p in with_ct:
        remaining = p.close_time - now  # type: ignore[operator]  # close_time != None aquí
        if remaining > CLV_EXIT_CEIL:
            pre += 1
        elif remaining <= _ZERO:
            past += 1
            continue  # mercado ya cerrado: no compite por "próxima salida"
        else:
            due += 1
        # "próxima salida": la posición no-cerrada con menor tiempo restante.
        if nearest_remaining is None or remaining < nearest_remaining:
            nearest_remaining = remaining
            nearest_ticker = p.ticker
    return ClvScanSummary(
        total=len(positions),
        with_close_time=len(with_ct),
        without_close_time=len(positions) - len(with_ct),
        due=due,
        pre_window=pre,
        past_window=past,
        nearest_ticker=nearest_ticker,
        nearest_remaining_min=(
            nearest_remaining.total_seconds() / 60 if nearest_remaining is not None else None
        ),
    )
