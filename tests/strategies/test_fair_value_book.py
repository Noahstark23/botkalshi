"""FairValueBook — canal Motor 2 → Motor 5 con TTL (plan Motor 5 §1.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.strategies.fair_value_book import FairValueBook

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_book():
    FairValueBook.clear()
    yield
    FairValueBook.clear()


def test_publish_and_fresh_within_ttl():
    FairValueBook.publish({"T-A": 0.62, "T-B": 0.38}, now=NOW)
    fresh = FairValueBook.fresh(600, now=NOW + timedelta(seconds=300))
    assert set(fresh) == {"T-A", "T-B"}
    assert fresh["T-A"].fair_prob == 0.62


def test_stale_entries_expire_and_are_purged():
    FairValueBook.publish({"T-A": 0.62}, now=NOW)
    assert FairValueBook.fresh(600, now=NOW + timedelta(seconds=601)) == {}
    assert FairValueBook.size() == 0  # purga: el book no crece sin tope


def test_republish_refreshes_timestamp():
    FairValueBook.publish({"T-A": 0.62}, now=NOW)
    FairValueBook.publish({"T-A": 0.65}, now=NOW + timedelta(seconds=500))
    fresh = FairValueBook.fresh(600, now=NOW + timedelta(seconds=900))
    assert fresh["T-A"].fair_prob == 0.65


def test_absent_ticker_keeps_last_fair_until_ttl():
    """Un ciclo que no matchea el partido (odds API parcial) NO borra su fair: expira por
    TTL, no por ausencia."""
    FairValueBook.publish({"T-A": 0.62, "T-B": 0.38}, now=NOW)
    FairValueBook.publish({"T-A": 0.63}, now=NOW + timedelta(seconds=300))
    fresh = FairValueBook.fresh(600, now=NOW + timedelta(seconds=500))
    assert set(fresh) == {"T-A", "T-B"}


def test_dual_module_identity_shares_one_book():
    """Regresión del incidente 2026-07-09 (P0): si el módulo se carga bajo dos claves de
    sys.modules (`src.strategies.fair_value_book` y `strategies.fair_value_book` cuando
    /app/src cuela en PYTHONPATH), Python crea DOS objetos-clase distintos. ANTES: cada uno
    tenía su propio ClassVar `_book` → Motor 2 publicaba en uno y Motor 5 leía el otro vacío
    (`fair_book publicados=24` pero `fair_fresh=0`). AHORA: el store vive anclado a `sys`, así
    que las dos copias comparten UN solo libro.

    Se simula cargando el MISMO archivo fuente como un módulo con otro nombre → segundo
    objeto-clase, exactamente el modo de falla real."""
    import importlib.util
    import sys

    import src.strategies.fair_value_book as canonical

    spec = importlib.util.spec_from_file_location("dup_fair_value_book", canonical.__file__)
    assert spec is not None and spec.loader is not None
    dup = importlib.util.module_from_spec(spec)
    # Registrar la segunda clave en sys.modules ANTES de ejecutar: así ocurre el bug real
    # (ambas claves presentes) y el dataclass con slots resuelve su módulo al introspeccionar.
    sys.modules[spec.name] = dup
    try:
        spec.loader.exec_module(dup)

        fvb_canonical = canonical.FairValueBook
        fvb_dup = dup.FairValueBook
        assert fvb_canonical is not fvb_dup  # dos clases distintas — el corazón del bug

        # Motor 2 publica por una copia; Motor 5 (la otra copia) DEBE ver el mismo fair.
        fvb_canonical.publish({"T-A": 0.62}, now=NOW)
        assert fvb_dup.size() == 1
        fresh = fvb_dup.fresh(600, now=NOW)
        assert fresh["T-A"].fair_prob == 0.62
    finally:
        del sys.modules[spec.name]
