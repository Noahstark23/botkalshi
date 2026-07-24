"""
EventExposureTracker — guard de exposición DIRECCIONAL por EVENTO para Motor 1 (Bug 2,
incidente 2026-07-07).

EL BUG: los tickers hijos de un mismo partido (KXMLBGAME-...HOUWSH-HOU y ...HOUWSH-WSH) son
mercados DISTINTOS pero el MISMO evento real. Motor 1 arbeó cada uno de forma independiente;
los residuales de netting/fills parciales se acumularon en la MISMA dirección (long-HOU vía
yes@-HOU y vía no@-WSH) hasta $135 de cost basis direccional sin que nadie lo sumara.

EL GUARD: antes de colocar un arb NUEVO, si la exposición direccional YA ACUMULADA del evento
supera MAX_EVENT_DIRECTIONAL_EXPOSURE_USD, el arb se aborta entero (sin colocar patas). No se
chequea por-pata: un arb completo netea a cero — lo que este guard corta es SEGUIR operando un
evento que ya arrastra residual direccional (huérfanas, netting asimétrico).

DOS FUENTES, se toma el MÁXIMO (conservador — sobrecontar solo frena arbs antes, nunca deja
pasar de más):
  - En memoria: fills registrados por ESTE proceso (reacción inmediata intra-burst; el
    incidente colocó 29 trades/min — el sync de DB de 60s llega tarde).
  - DB portfolio_positions: la verdad de Kalshi (incluye netting/parciales que nuestros fills
    no ven), sincronizada por el PortfolioPoller de Motor 3 cada 60s. Si Motor 3 está apagado
    la tabla puede estar stale → la memoria del proceso sigue protegiendo el burst actual.

MODELO de dirección (2-way; para N-way sobrecuenta = conservador):
  yes en hijo X apuesta "gana X"; no en hijo X apuesta "no gana X" (≈ gana el rival en 2-way).
  Por hijo se netean los pares yes/no (un arb intra-ticker completo aporta CERO). La exposición
  del evento = máx sobre direcciones de la suma de costos UNHEDGED que apuestan a esa dirección.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger
from sqlmodel import select

from src.storage.models import PortfolioPosition, get_session


def event_ticker_of(market_ticker: str) -> str:
    """Evento (partido) de un market ticker: todo menos el sufijo de outcome.
    'KXMLBGAME-26JUL061845HOUWSH-HOU' → 'KXMLBGAME-26JUL061845HOUWSH'."""
    return market_ticker.rsplit("-", 1)[0] if "-" in market_ticker else market_ticker


@dataclass
class _ChildBook:
    """Fills en memoria de UN ticker hijo (solo de este proceso)."""

    yes_count: int = 0
    yes_cost_cents: int = 0
    no_count: int = 0
    no_cost_cents: int = 0

    def unhedged(self) -> tuple[float, float]:
        """(usd_unhedged_yes, usd_unhedged_no): lo que queda tras netear pares yes/no al costo
        promedio de cada lado. Un arb completo (yes==no) devuelve (0, 0)."""
        hedged = min(self.yes_count, self.no_count)
        uy = self.yes_count - hedged
        un = self.no_count - hedged
        avg_yes = (self.yes_cost_cents / self.yes_count) if self.yes_count else 0.0
        avg_no = (self.no_cost_cents / self.no_count) if self.no_count else 0.0
        return uy * avg_yes / 100.0, un * avg_no / 100.0


@dataclass
class EventExposureTracker:
    """Rastrea exposición direccional por evento y decide si un arb nuevo puede colocarse."""

    max_exposure_usd: float
    _books: dict[str, dict[str, _ChildBook]] = field(default_factory=dict)  # event → child → book
    _alerted_events: set[str] = field(default_factory=set)  # throttle Telegram: 1×/evento

    def record_fill(self, market_ticker: str, side: str, count: int, price_cents: int) -> None:
        """Registra un BUY fillado de este proceso (la fuente rápida intra-burst)."""
        event = event_ticker_of(market_ticker)
        book = self._books.setdefault(event, {}).setdefault(market_ticker, _ChildBook())
        if side == "yes":
            book.yes_count += count
            book.yes_cost_cents += count * price_cents
        else:
            book.no_count += count
            book.no_cost_cents += count * price_cents

    def _inmem_exposure_usd(self, event: str) -> float:
        children = self._books.get(event)
        if not children:
            return 0.0
        # Por dirección: pro-X = unhedged_yes(X) + Σ_{y≠X} unhedged_no(y). Máx sobre X, y el
        # espejo (anti-X) para cubrir eventos de un solo hijo con NO unhedged.
        u = {t: b.unhedged() for t, b in children.items()}
        best = 0.0
        for x in u:
            pro_x = u[x][0] + sum(u[y][1] for y in u if y != x)
            anti_x = u[x][1] + sum(u[y][0] for y in u if y != x)
            best = max(best, pro_x, anti_x)
        return best

    def _db_exposure_usd(self, event: str) -> float:
        """Verdad de Kalshi vía portfolio_positions (netting/parciales incluidos). Kalshi netea
        yes/no por market → cada fila YA es residual direccional; exposure_cents = capital en
        riesgo. Best-effort: error de DB → 0 (la memoria del proceso sigue cubriendo)."""
        try:
            with get_session() as s:
                rows = list(s.exec(select(PortfolioPosition)))
        except Exception as exc:
            logger.warning(f"event_exposure.db_error: {exc} → solo memoria de proceso")
            return 0.0
        pro: dict[str, float] = {}
        anti: dict[str, float] = {}
        for p in rows:
            if event_ticker_of(p.ticker) != event or p.count <= 0:
                continue
            usd = (p.exposure_cents if p.exposure_cents is not None else p.count * 100) / 100.0
            (pro if p.side == "yes" else anti)[p.ticker] = usd
        tickers = set(pro) | set(anti)
        best = 0.0
        for x in tickers:
            pro_x = pro.get(x, 0.0) + sum(v for t, v in anti.items() if t != x)
            anti_x = anti.get(x, 0.0) + sum(v for t, v in pro.items() if t != x)
            best = max(best, pro_x, anti_x)
        return best

    def exposure_usd(self, event: str) -> float:
        """Máximo de ambas fuentes (conservador)."""
        return max(self._inmem_exposure_usd(event), self._db_exposure_usd(event))

    def check_new_arb(self, market_tickers: list[str]) -> tuple[bool, float, str]:
        """(allowed, exposure_usd, event) para un arb nuevo. Rechaza si CUALQUIER evento
        involucrado ya supera el cap. `should_alert(event)` decide el Telegram (1×/evento)."""
        for ticker in market_tickers:
            event = event_ticker_of(ticker)
            exposure = self.exposure_usd(event)
            if exposure > self.max_exposure_usd:
                return False, exposure, event
        return True, 0.0, ""

    def should_alert(self, event: str) -> bool:
        """True la PRIMERA vez que este evento bloquea (throttle: no spamear por burst)."""
        if event in self._alerted_events:
            return False
        self._alerted_events.add(event)
        return True
