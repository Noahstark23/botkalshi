"""
Núcleo PURO del fair de consenso de Motor 2 — sin loguru, pydantic ni red.

Extraído de `detector.py` el 2026-09-22 SIN cambiar la matemática: el servicio de
research (droplet, `/usr/bin/python3` sin venv) necesita el MISMO fair que M2 publica
para M5, y `detector.py` arrastra pydantic/httpx/loguru. Copiarlo habría creado dos
fórmulas que divergen. `detector._consensus_fair_probs`, `_h2h_outcome_names` y
`_select_candidate` delegan acá y conservan su logging; los tests de M2 lo fijan.

Entrada: cualquier objeto con la forma de `OddsEvent` (atributos `bookmakers[].key`,
`.last_update`, `.markets[].key`, `.markets[].outcomes[].name/.price`,
`commence_time`). Salida: datos, nunca logs.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from datetime import UTC, datetime
from statistics import median
from typing import Any

from src.math.no_vig import implied_prob, remove_vig_multiplicative
from src.strategies.motor_2_consensus.matcher import (
    canonical_name,
    parse_event_key_start,
    start_time_et,
)


def h2h_outcome_names(odds_event: Any) -> list[str]:
    """Set de referencia = los outcomes del set canónico MÁS FRECUENTE entre las casas
    con h2h (auditoría rentabilidad 2026-07-07: antes se tomaba la PRIMERA casa del
    array — si esa listaba un set atípico (nombre sin alias, 2-way donde el resto es
    3-way), TODAS las demás se descartaban por 'set distinto' y el evento moría en
    reject_no_fair, dependiendo del orden arbitrario del JSON de la API)."""
    sets_vistos: Counter[frozenset[str]] = Counter()
    primero_por_set: dict[frozenset[str], list[str]] = {}
    for bk in odds_event.bookmakers:
        for mk in bk.markets:
            if mk.key == "h2h" and mk.outcomes:
                names = [o.name for o in mk.outcomes]
                key = frozenset(canonical_name(n) for n in names)
                sets_vistos[key] += 1
                primero_por_set.setdefault(key, names)
    if not sets_vistos:
        return []
    ganador = sets_vistos.most_common(1)[0][0]
    return primero_por_set[ganador]


def consensus_fair_probs(
    odds_event: Any,
    *,
    min_books: int = 1,
    max_book_age_min: float | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    """
    (fair por outcome canónico, estadísticas). Fair = MEDIANA de la prob implícita de
    cada bookmaker (consenso) sin vig (multiplicativo). {} si no hay h2h usable, si el
    consenso lo forman menos de `min_books` casas, o si todas las líneas están vencidas.

    Auditoría rentabilidad 2026-07-07 (tres endurecimientos de la MEDICIÓN):
      - MEDIANA en vez de media simple: una sola casa soft/desviada movía el fair
        entero (la media equiponderada no tiene resistencia a outliers; la mediana sí).
      - min_books: el único gate previo era >=2 OUTCOMES — UNA casa podía formar el
        "consenso" entero. Con pocas casas el fair hereda el shading recreativo y el
        edge medido puede ser 100% ruido (n_books solo se logueaba).
      - Frescura: con un límite configurado, una línea congelada o sin last_update se
        descarta. Sin timestamp no existe evidencia para una cohorte científica.

    Las estadísticas llevan los timestamps ORIGINALES de las casas aceptadas
    (`oldest_book_update`/`newest_book_update`): la fecha del fair es la de sus datos,
    nunca la de la descarga ni la de la exportación.
    """
    stats: dict[str, Any] = {
        "skipped_books": 0,
        "stale_books": 0,
        "unknown_age_books": 0,
        "n_books": 0,
        "min_books": min_books,
        "reason": None,
        "bookmaker_keys": (),
        "oldest_book_update": None,
        "newest_book_update": None,
    }
    # Set de REFERENCIA (set canónico más frecuente entre casas). Cada casa entra al
    # consenso solo con el set COMPLETO e IDÉNTICO (deuda auditoría 2026-07-01: agregar
    # outcomes sueltos de casas parciales normalizaba el no-vig sobre el conjunto
    # equivocado → fair inflado, p.ej. 1X2 sin Draw daba A=0.60/B=0.40 vs ~0.50/0.33/0.17).
    reference = {canonical_name(n) for n in h2h_outcome_names(odds_event)}
    if len(reference) < 2:
        stats["reason"] = "no_h2h_reference"
        return {}, stats
    probs_por_outcome: dict[str, list[float]] = {}
    book_keys: set[str] = set()
    accepted_updates: list[datetime] = []
    ref_now = now or datetime.now(UTC)
    for bk in odds_event.bookmakers:
        if max_book_age_min is not None:
            if bk.last_update is None:
                stats["unknown_age_books"] += 1
                continue
            age_min = (ref_now - bk.last_update).total_seconds() / 60.0
            if age_min < 0 or age_min > max_book_age_min:
                stats["stale_books"] += 1
                continue  # línea vencida: no aporta información fresca al consenso
        for mk in bk.markets:
            if mk.key != "h2h":
                continue
            probs: dict[str, float] = {}
            for o in mk.outcomes:
                # Cuota inválida → el set de esta casa queda incompleto → se descarta abajo.
                with contextlib.suppress(ValueError):
                    probs[canonical_name(o.name)] = implied_prob(o.price)
            if set(probs) != reference:
                stats["skipped_books"] += 1
                continue  # set distinto/incompleto → esta casa NO entra al consenso
            book_keys.add(bk.key)
            if bk.last_update is not None:
                accepted_updates.append(bk.last_update)
            for cn, p in probs.items():
                probs_por_outcome.setdefault(cn, []).append(p)
    stats["n_books"] = len(book_keys)
    if len(probs_por_outcome) < 2:
        stats["reason"] = "no_usable_books"
        return {}, stats
    if len(book_keys) < min_books:
        stats["reason"] = "too_few_books"
        return {}, stats
    names = list(probs_por_outcome)
    med_implied = [median(probs_por_outcome[n]) for n in names]
    fair = remove_vig_multiplicative(med_implied)
    stats.update(
        bookmaker_keys=tuple(sorted(book_keys)),
        oldest_book_update=min(accepted_updates) if accepted_updates else None,
        newest_book_update=max(accepted_updates) if accepted_updates else None,
    )
    return dict(zip(names, fair, strict=True)), stats


def select_candidate(
    event_key: str, candidates: list[Any], now: datetime
) -> tuple[Any | None, str | None]:
    """Elige el ÚNICO odds event que corresponde al evento Kalshi, o (None, motivo).

    Reglas conservadoras (emparejar el partido equivocado es la falla catastrófica):
      1. Si el event_key trae fecha ("...-26JUN27..."), el candidato debe empezar ESE día
         en hora del Este. Mata el bug de series MLB: el evento Kalshi del miércoles ya no
         matchea la línea del martes.
      2. >1 candidato en la fecha (doubleheader): desambigua por la hora ET embebida en el
         key ("...1610..."); sin hora única que coincida → 'ambiguous' (descarta ambos).
      3. Key sin fecha parseable: solo se acepta candidato ÚNICO; 2+ → 'ambiguous'.
      4. El elegido debe ser PRE-MATCH (commence_time > now); si ya arrancó → 'started'
         (precios in-play de Kalshi vs línea de odds = comparación inválida).

    Motivos → embudo: reject_date | reject_ambiguous | reject_started. (None, None) si
    no había candidatos (la clasificación cae al best_reason por overlap).
    """
    if not candidates:
        return None, None
    parsed = parse_event_key_start(event_key)
    if parsed is not None:
        key_date, key_hhmm = parsed
        dated = [oe for oe in candidates if start_time_et(oe.commence_time).date() == key_date]
        if not dated:
            return None, "date"
        if len(dated) == 1:
            chosen = dated[0]
        else:
            timed = []
            if key_hhmm is not None:
                timed = [
                    oe
                    for oe in dated
                    if (
                        start_time_et(oe.commence_time).hour,
                        start_time_et(oe.commence_time).minute,
                    )
                    == key_hhmm
                ]
            if len(timed) != 1:
                return None, "ambiguous"
            chosen = timed[0]
    else:
        if len(candidates) > 1:
            return None, "ambiguous"
        chosen = candidates[0]
    if chosen.commence_time <= now:
        return None, "started"
    return chosen, None
