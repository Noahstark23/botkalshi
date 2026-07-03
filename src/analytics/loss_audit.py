"""
Auditoría de PÉRDIDAS ESTRUCTURALES (pedido del operador 2026-07-03: "es imposible que
hayamos perdido — analiza toda la estructura").

La hipótesis a testear no es "mala suerte": es si el sistema tenía mecanismos que
GARANTIZABAN pérdida por construcción. Tres, todos anclados a la línea de tiempo del
fee bug (kalshi_fee_cents dividía por 1e6 en vez de 1e4 — ~100× subestimado — hasta el
fix 0dbf9b7, 2026-07-01T21:02Z):

  M1 — ARBS FEE-NEGATIVOS: detect_binary_arb calcula net = gross − fees AL DETECTAR.
       Con la fee ~100× subestimada, un arb de gross 2-3¢/contrato parecía net-positivo
       cuando las fees REALES (~1.5-2¢ por pata) lo hacían PERDEDOR DETERMINÍSTICO:
       el bot ejecutaba trades matemáticamente garantizados a perder, etiquetados como
       ganancia garantizada. (Ej. real: par 243×243 del 30-jun, gross $7.29, fees
       reales ~$8.26 → −$0.97 asegurados al momento del place.)

  M2 — UMBRAL EFECTIVAMENTE BRUTO: el edge "neto" de Motor 2 restaba una fee de ~0 →
       el umbral de 3pp neto era en la práctica 3pp BRUTO; señales marginales que las
       fees reales volvían negativas se ejecutaban igual. (El phantom edge ≥8pp es un
       mecanismo APARTE — miscalibración del consenso — ya cortado por el techo.)

  DB — PnL REGISTRADO SOBREESTIMADO: fees_cents se grabó con la fórmula buggy → el
       pnl_cents de las filas pre-fix es MEJOR que la realidad (Kalshi cobró la fee
       real). El "fee drag oculto" = Σ(fee_real − fee_registrada) explica parte del
       gap entre la DB y el cash real de la cuenta.

Todo read-only y por COTAS honestas donde falten datos. El CLI:
scripts/audit_perdidas_estructurales.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from src.analytics.shadow_fee_recalc import FEE_FIX_AT_DEFAULT
from src.math.fees import kalshi_fee_cents


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _effective_count(row) -> int:
    fc = getattr(row, "filled_count", None)
    return fc if (row.status in ("filled", "settled") and fc is not None) else row.count


def _effective_price(row) -> int:
    return row.fill_price_cents or row.price_cents


def real_entry_fee_cents(row) -> int:
    """Fee REAL (fórmula corregida) de la entrada de una fila, con su count efectivo."""
    return kalshi_fee_cents(_effective_count(row), _effective_price(row))


# =====================================================
# DB — fee drag oculto (PnL registrado vs realidad)
# =====================================================


@dataclass(slots=True)
class FeeDrag:
    rows: int = 0
    recorded_fees_cents: int = 0
    real_fees_cents: int = 0

    @property
    def drag_cents(self) -> int:
        """Cuánto MEJOR se ve la DB que la realidad (fees reales − registradas)."""
        return self.real_fees_cents - self.recorded_fees_cents


def fee_drag_by_strategy(rows, *, fee_fix_at: datetime = FEE_FIX_AT_DEFAULT) -> dict[str, FeeDrag]:
    """Filas filled/settled colocadas ANTES del fix: la fee registrada era buggy.
    Solo cuenta la fee de ENTRADA (las de salida/settlement se aproximan aparte —
    dirección del sesgo: este drag es una COTA INFERIOR del drag total."""
    out: dict[str, FeeDrag] = {}
    for row in rows:
        if row.status not in ("filled", "settled"):
            continue
        if _naive(row.placed_at) >= fee_fix_at:
            continue  # post-fix: la fee registrada ya es real
        agg = out.setdefault(row.strategy, FeeDrag())
        agg.rows += 1
        agg.recorded_fees_cents += row.fees_cents or 0
        agg.real_fees_cents += real_entry_fee_cents(row)
    return out


# =====================================================
# M1 — arbs ejecutados que eran perdedores determinísticos
# =====================================================


@dataclass(slots=True)
class ArbAudit:
    groups: int = 0
    deterministic_losers: int = 0  # net REAL ≤ 0 al momento del place (garantizado)
    paired_contracts: int = 0
    gross_cents: int = 0
    real_fees_cents: int = 0

    @property
    def net_real_cents(self) -> int:
        return self.gross_cents - self.real_fees_cents


def audit_motor1_arbs(rows, *, pair_window_sec: float = 10.0) -> ArbAudit:
    """Agrupa las patas BUY de motor_1_arbitrage en pares yes/no del MISMO ticker
    colocados juntos (las legacy no tienen arb_id: se emparejan por cercanía temporal,
    ambas patas se insertan en el mismo commit). Para cada par HEDGED:

        net_real = (100 − p_yes − p_no)·paired − fee_real(paired, p_yes) − fee_real(paired, p_no)

    net_real ≤ 0 ⇒ el trade estaba PERDIDO al momento de colocarse, sin importar el
    resultado del partido — la firma del fee bug en Motor 1.
    """
    m1 = sorted(
        (
            r
            for r in rows
            if r.strategy == "motor_1_arbitrage"
            and r.action == "buy"
            and r.status in ("filled", "settled")
        ),
        key=lambda r: (r.ticker, _naive(r.placed_at)),
    )
    audit = ArbAudit()
    window = timedelta(seconds=pair_window_sec)
    used: set[int] = set()
    for i, a in enumerate(m1):
        if id(a) in used or a.side != "yes":
            continue
        for b in m1[i + 1 :]:
            if id(b) in used or b.ticker != a.ticker or b.side != "no":
                continue
            if abs(_naive(b.placed_at) - _naive(a.placed_at)) > window:
                break
            used.add(id(a))
            used.add(id(b))
            paired = min(_effective_count(a), _effective_count(b))
            p_yes, p_no = _effective_price(a), _effective_price(b)
            gross = (100 - p_yes - p_no) * paired
            fees = kalshi_fee_cents(paired, p_yes) + kalshi_fee_cents(paired, p_no)
            audit.groups += 1
            audit.paired_contracts += paired
            audit.gross_cents += gross
            audit.real_fees_cents += fees
            if gross - fees <= 0:
                audit.deterministic_losers += 1
            break
    return audit


# =====================================================
# M2 — buckets de edge con fees REALES
# =====================================================


@dataclass(slots=True)
class Bucket:
    label: str
    n: int = 0
    wins: int = 0
    pnl_recorded_cents: int = 0
    pnl_real_cents: int = 0  # registrado − drag de fee de entrada de esa fila

    @property
    def win_pct(self) -> float:
        return 100.0 * self.wins / self.n if self.n else 0.0


def motor2_buckets(
    rows,
    *,
    edges: tuple[float, ...] = (5.0, 8.0, 11.0),
    fee_fix_at: datetime = FEE_FIX_AT_DEFAULT,
) -> list[Bucket]:
    """La tabla del operador, corregida por fees reales. Solo filas settled de
    motor_2_consensus con pnl_cents. El ajuste aplica el drag de la fee de ENTRADA a
    filas pre-fix (la de salida/settlement no está desglosada por fila — cota)."""
    labels = (
        [f"<{edges[0]:g}%"]
        + [f"{lo:g}-{hi:g}%" for lo, hi in zip(edges, edges[1:], strict=False)]
        + [f">={edges[-1]:g}%"]
    )
    buckets = [Bucket(label=lb) for lb in labels]

    def _bucket_for(edge_pct: float) -> Bucket:
        for i, e in enumerate(edges):
            if edge_pct < e:
                return buckets[i]
        return buckets[-1]

    for row in rows:
        if row.strategy != "motor_2_consensus" or row.status != "settled":
            continue
        if row.pnl_cents is None or row.estimated_edge_pct is None:
            continue
        # edge en la DB puede estar como fracción (0.05) o pp (5.0) según la época.
        edge_pp = (
            row.estimated_edge_pct * 100
            if row.estimated_edge_pct <= 1.0
            else row.estimated_edge_pct
        )
        b = _bucket_for(edge_pp)
        b.n += 1
        if row.pnl_cents > 0:
            b.wins += 1
        b.pnl_recorded_cents += row.pnl_cents
        drag = 0
        if _naive(row.placed_at) < fee_fix_at:
            drag = real_entry_fee_cents(row) - (row.fees_cents or 0)
        b.pnl_real_cents += row.pnl_cents - drag
    return buckets
