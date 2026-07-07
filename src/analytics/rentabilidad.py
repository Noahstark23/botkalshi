"""
Scoreboard de RENTABILIDAD por motor (pedido del operador 2026-07-07: "por qué no ha
estado ganando desde sus inicios").

Complementa loss_audit.py (que cuantifica los mecanismos de pérdida del fee bug): esto
responde la pregunta de arriba hacia abajo — ¿qué motor gana/pierde, con qué win rate y
profit factor, en qué meses, a qué precios de entrada, y con qué tamaño de trade?

La métrica clave que faltaba es la del TAMAÑO: kalshi_fee_cents redondea ceil AL CENT
POR TRADE — en un trade de 1-3 contratos el redondeo puede ser >30% del fee teórico y
comerse un edge de 3-4pp entero. Si la distribución de counts está concentrada en
trades chicos, el bot pierde por granularidad aunque la señal sea buena.

Helpers PUROS (sin red; reciben filas Trade ya cargadas). El CLI:
scripts/audit_rentabilidad.py. Convenciones: cents enteros; settled_at naive UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from src.analytics.loss_audit import _effective_count, _effective_price, real_entry_fee_cents
from src.math.fees import kalshi_fee_cents


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _settled_with_pnl(rows) -> list:
    return [r for r in rows if r.status == "settled" and r.pnl_cents is not None]


# =====================================================
# Resumen por motor
# =====================================================


@dataclass(slots=True)
class ResumenMotor:
    strategy: str
    n: int = 0
    wins: int = 0
    pnl_cents: int = 0
    gross_win_cents: int = 0  # suma de pnl de las ganadoras
    gross_loss_cents: int = 0  # suma ABSOLUTA de pnl de las perdedoras
    fees_reales_cents: int = 0  # fee de entrada recomputado con la fórmula corregida

    @property
    def win_pct(self) -> float:
        return 100.0 * self.wins / self.n if self.n else 0.0

    @property
    def avg_win_cents(self) -> float:
        return self.gross_win_cents / self.wins if self.wins else 0.0

    @property
    def avg_loss_cents(self) -> float:
        losses = self.n - self.wins
        return self.gross_loss_cents / losses if losses else 0.0

    @property
    def profit_factor(self) -> float:
        """>1 = gana. gross ganado / gross perdido (inf si no hay perdedoras)."""
        if self.gross_loss_cents == 0:
            return float("inf") if self.gross_win_cents > 0 else 0.0
        return self.gross_win_cents / self.gross_loss_cents

    @property
    def expectancy_cents(self) -> float:
        """PnL medio por trade settled — la respuesta corta de '¿este motor gana?'."""
        return self.pnl_cents / self.n if self.n else 0.0


def resumen_por_motor(rows) -> dict[str, ResumenMotor]:
    out: dict[str, ResumenMotor] = {}
    for r in _settled_with_pnl(rows):
        agg = out.setdefault(r.strategy, ResumenMotor(strategy=r.strategy))
        agg.n += 1
        agg.pnl_cents += r.pnl_cents
        if r.pnl_cents > 0:
            agg.wins += 1
            agg.gross_win_cents += r.pnl_cents
        else:
            agg.gross_loss_cents += -r.pnl_cents
        agg.fees_reales_cents += real_entry_fee_cents(r)
    return out


# =====================================================
# Serie mensual (¿mejoró después de cada fix?)
# =====================================================


def pnl_mensual(rows) -> dict[str, dict[str, int]]:
    """{ 'AAAA-MM': {strategy: pnl_cents} } por settled_at — para ver si la curva
    cambió de pendiente después de los fixes (fees 07-01, salidas, flat sizing)."""
    out: dict[str, dict[str, int]] = {}
    for r in _settled_with_pnl(rows):
        when = _naive(r.settled_at) if r.settled_at else _naive(r.placed_at)
        key = f"{when.year:04d}-{when.month:02d}"
        out.setdefault(key, {}).setdefault(r.strategy, 0)
        out[key][r.strategy] += r.pnl_cents
    return dict(sorted(out.items()))


# =====================================================
# Buckets por precio de entrada (favorite-longshot)
# =====================================================


@dataclass(slots=True)
class BucketPrecio:
    label: str
    n: int = 0
    wins: int = 0
    pnl_cents: int = 0

    @property
    def win_pct(self) -> float:
        return 100.0 * self.wins / self.n if self.n else 0.0


def buckets_por_precio(rows, *, cortes: tuple[int, ...] = (40, 60, 80)) -> list[BucketPrecio]:
    """PnL por precio de ENTRADA — la generalización del hallazgo underdog de M2
    (<40c sangró): ¿el patrón se repite en otros motores/rangos?"""
    labels = (
        [f"<{cortes[0]}c"]
        + [f"{lo}-{hi - 1}c" for lo, hi in zip(cortes, cortes[1:], strict=False)]
        + [f">={cortes[-1]}c"]
    )
    buckets = [BucketPrecio(label=lb) for lb in labels]

    def _bucket(price: int) -> BucketPrecio:
        for i, c in enumerate(cortes):
            if price < c:
                return buckets[i]
        return buckets[-1]

    for r in _settled_with_pnl(rows):
        b = _bucket(_effective_price(r))
        b.n += 1
        if r.pnl_cents > 0:
            b.wins += 1
        b.pnl_cents += r.pnl_cents
    return buckets


# =====================================================
# Granularidad: el ceil del fee vs el tamaño del trade
# =====================================================


@dataclass(slots=True)
class Granularidad:
    label: str  # rango de contratos por trade
    n: int = 0
    pnl_cents: int = 0
    fee_teorico_x100: int = 0  # fee sin redondeo ×100 (para no perder precisión)
    fee_real_cents: int = 0  # fee con ceil por trade (lo que Kalshi cobra)

    @property
    def sobrecosto_redondeo_cents(self) -> float:
        """Cuánto fee EXTRA pagó este rango solo por el ceil por trade."""
        return self.fee_real_cents - self.fee_teorico_x100 / 100.0


def granularidad_fee(rows, *, cortes: tuple[int, ...] = (5, 20, 100)) -> list[Granularidad]:
    """Distribución del tamaño de trade y el sobrecosto del redondeo del fee.

    fee teórico = 7·C·P·(100−P)/10_000 (sin ceil); fee real = kalshi_fee_cents (ceil por
    trade). En counts chicos el ceil domina: si la masa de trades está en <5 contratos,
    el bot paga un 'impuesto de granularidad' que ningún edge de 3pp sobrevive."""
    labels = (
        [f"<={cortes[0]}"]
        + [f"{lo + 1}-{hi}" for lo, hi in zip(cortes, cortes[1:], strict=False)]
        + [f">{cortes[-1]}"]
    )
    buckets = [Granularidad(label=lb) for lb in labels]

    def _bucket(count: int) -> Granularidad:
        for i, c in enumerate(cortes):
            if count <= c:
                return buckets[i]
        return buckets[-1]

    for r in _settled_with_pnl(rows):
        count = _effective_count(r)
        price = _effective_price(r)
        g = _bucket(count)
        g.n += 1
        g.pnl_cents += r.pnl_cents
        g.fee_teorico_x100 += 7 * count * price * (100 - price) // 100
        g.fee_real_cents += kalshi_fee_cents(count, price)
    return buckets


# =====================================================
# Veredicto imprimible
# =====================================================


@dataclass(slots=True)
class Veredicto:
    lineas: list[str] = field(default_factory=list)


def veredicto(rows) -> Veredicto:
    """Las conclusiones automáticas defendibles con la data cargada."""
    v = Veredicto()
    por_motor = resumen_por_motor(rows)
    if not por_motor:
        v.lineas.append("Sin filas settled con pnl — no hay base para veredicto todavía.")
        return v
    for s, m in sorted(por_motor.items(), key=lambda kv: kv[1].pnl_cents):
        v.lineas.append(
            f"{s}: {m.n} trades, win {m.win_pct:.0f}%, expectancy "
            f"{m.expectancy_cents:+.1f}c/trade, PF {m.profit_factor:.2f}, "
            f"pnl total ${m.pnl_cents / 100:+.2f} (fees reales ${m.fees_reales_cents / 100:.2f})"
        )
    grano = granularidad_fee(rows)
    chico = grano[0]
    if chico.n:
        v.lineas.append(
            f"Trades de <=5 contratos: {chico.n} — sobrecosto de redondeo de fee "
            f"${chico.sobrecosto_redondeo_cents / 100:+.2f} (impuesto de granularidad)"
        )
    return v


# =====================================================
# Pares M1 — la vista correcta para una estrategia hedged
# =====================================================

_ARB_ID_RE = re.compile(r"arb_id=([0-9a-fA-F-]{36})")


@dataclass(slots=True)
class ParM1:
    ticker: str
    paired: int  # contratos emparejados (min de ambas patas)
    p_yes: int
    p_no: int
    via: str  # "arb_id" (filas nuevas) | "ventana" (legacy, ticker + 10s)

    @property
    def gross_cents(self) -> int:
        return (100 - self.p_yes - self.p_no) * self.paired

    @property
    def fees_cents(self) -> int:
        return kalshi_fee_cents(self.paired, self.p_yes) + kalshi_fee_cents(self.paired, self.p_no)

    @property
    def net_cents(self) -> int:
        """Neto del PAR a fees reales — independiente de qué lado gane el partido."""
        return self.gross_cents - self.fees_cents


@dataclass(slots=True)
class ResumenPares:
    pares: list[ParM1] = field(default_factory=list)
    sin_par: int = 0  # patas BUY de M1 sin gemela (huérfanas/rollback)
    sin_par_pnl_cents: int = 0  # pnl settled de esas patas: el costo OPERACIONAL

    @property
    def net_cents(self) -> int:
        return sum(p.net_cents for p in self.pares)

    @property
    def perdedores(self) -> int:
        """Pares con net ≤ 0 a fees reales = perdedores DETERMINÍSTICOS al colocarse."""
        return sum(1 for p in self.pares if p.net_cents <= 0)

    @property
    def contratos(self) -> int:
        return sum(p.paired for p in self.pares)


def pares_motor1(rows, *, pair_window_sec: float = 10.0) -> ResumenPares:
    """Empareja las patas BUY de motor_1_arbitrage en pares yes/no hedged.

    Corrección de lectura 2026-07-07: los buckets por PATA son un artefacto en una
    estrategia hedged (una pata gana y la otra pierde POR CONSTRUCCIÓN — el bucket
    <40c con win 1.8% son las patas baratas cuyas gemelas caras ganan el 99%). La
    unidad económica de M1 es el PAR: net = (100 − p_yes − p_no)·paired − ambas fees.

    Emparejamiento en dos pasadas, consistente con loss_audit.audit_motor1_arbs:
      1. arb_id compartido en notes (filas post 2026-07-02, exacto).
      2. Legacy sin arb_id: mismo ticker + placed_at dentro de pair_window_sec.
    Lo que queda sin par son huérfanas/rollbacks: su pnl settled se reporta APARTE
    (costo operacional, no del edge de la estrategia).
    """
    m1 = [
        r
        for r in rows
        if r.strategy == "motor_1_arbitrage"
        and r.action == "buy"
        and r.status in ("filled", "settled")
    ]
    out = ResumenPares()
    usados: set[int] = set()

    # Pasada 1 — arb_id exacto.
    por_arb: dict[str, list] = {}
    for r in m1:
        m = _ARB_ID_RE.search(r.notes or "")
        if m:
            por_arb.setdefault(m.group(1), []).append(r)
    for legs in por_arb.values():
        yes = next((r for r in legs if r.side == "yes"), None)
        no = next((r for r in legs if r.side == "no"), None)
        if yes is None or no is None:
            continue  # pata sola con arb_id → queda para el conteo de huérfanas
        usados.add(id(yes))
        usados.add(id(no))
        out.pares.append(
            ParM1(
                ticker=yes.ticker,
                paired=min(_effective_count(yes), _effective_count(no)),
                p_yes=_effective_price(yes),
                p_no=_effective_price(no),
                via="arb_id",
            )
        )

    # Pasada 2 — legacy por ticker + ventana temporal (mismo criterio que loss_audit).
    restantes = sorted(
        (r for r in m1 if id(r) not in usados), key=lambda r: (r.ticker, _naive(r.placed_at))
    )
    ventana = timedelta(seconds=pair_window_sec)
    for i, a in enumerate(restantes):
        if id(a) in usados or a.side != "yes":
            continue
        for b in restantes[i + 1 :]:
            if id(b) in usados or b.ticker != a.ticker or b.side != "no":
                continue
            if abs(_naive(b.placed_at) - _naive(a.placed_at)) > ventana:
                break
            usados.add(id(a))
            usados.add(id(b))
            out.pares.append(
                ParM1(
                    ticker=a.ticker,
                    paired=min(_effective_count(a), _effective_count(b)),
                    p_yes=_effective_price(a),
                    p_no=_effective_price(b),
                    via="ventana",
                )
            )
            break

    for r in m1:
        if id(r) not in usados:
            out.sin_par += 1
            if r.status == "settled" and r.pnl_cents is not None:
                out.sin_par_pnl_cents += r.pnl_cents
    return out
