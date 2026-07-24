"""
Motor 8 F1 SHADOW — OFI auto-validante.

Verifica: el tracker puro (baseline maduro, z-score, ventana rodante, cooldown), el
shadow (señal → medición T+30/T+60 → EdgeWindow kind=ofi con moves firmados desde la
presión; book caído → drop con grace; best-effort total), y el guard ESTRUCTURAL (el
paquete no importa el cliente de órdenes).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.strategies.motor_8_ofi.detector import OfiTracker
from src.strategies.motor_8_ofi.shadow import Motor8OfiShadow


@pytest.fixture(autouse=True)
def sqlite_engine(tmp_path):
    """DB real temporal como singleton de models (el shadow persiste EdgeWindow)."""
    db = tmp_path / "m8.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


T = "KXMLBGAME-26JUL15NYMPHI-NYM"


def _tracker(**kw) -> OfiTracker:
    defaults = {"window_sec": 60.0, "z_min": 3.0, "min_baseline": 20, "cooldown_sec": 120.0}
    defaults.update(kw)
    return OfiTracker(**defaults)


def _warm(tr: OfiTracker, n: int = 30, start: float = 0.0) -> float:
    """Baseline maduro con flujo balanceado (+1/−1 alternado) → media ~0, std chica."""
    t = start
    for i in range(n):
        tr.observe(T, "yes" if i % 2 == 0 else "no", 1, t)
        t += 1.0
    return t


# ── Tracker (puro) ───────────────────────────────────────────────────────────


def test_immature_baseline_never_signals():
    """CONTROL de madurez: sin historia suficiente, ni un spike enorme señala."""
    tr = _tracker(min_baseline=50)
    assert tr.observe(T, "yes", 100_000, 0.0) is None


def test_anomalous_yes_flow_signals_with_pressure():
    """MECANISMO: baseline balanceado + spike comprador de YES → señal con presión YES
    y z alto."""
    tr = _tracker()
    t = _warm(tr)
    sig = tr.observe(T, "yes", 500, t)
    assert sig is not None and sig.pressure == "YES" and sig.zscore > 3.0


def test_no_side_flow_signals_no_pressure():
    tr = _tracker()
    t = _warm(tr)
    sig = tr.observe(T, "no", 500, t)
    assert sig is not None and sig.pressure == "NO"


def test_balanced_flow_is_noise():
    """CONTROL: flujo balanceado sostenido no dispara (|z| < umbral)."""
    tr = _tracker()
    t = _warm(tr, n=60)
    assert tr.observe(T, "yes", 1, t) is None


def test_cooldown_suppresses_signal_burst():
    """Anti-ráfaga: tras una señal, el mismo episodio no re-señala hasta el cooldown."""
    tr = _tracker(cooldown_sec=120.0)
    t = _warm(tr)
    assert tr.observe(T, "yes", 500, t) is not None
    assert tr.observe(T, "yes", 500, t + 1) is None  # silenciado
    sig = tr.observe(T, "yes", 500, t + 121)  # cooldown vencido
    assert sig is not None


def test_window_evicts_old_flow():
    """Ventana rodante: el flujo más viejo que window_sec sale de la suma (el OFI de un
    spike viejo no contamina el presente)."""
    tr = _tracker(window_sec=60.0, min_baseline=5, z_min=3.0)
    tr.observe(T, "yes", 1000, 0.0)  # spike que quedará fuera de la ventana
    t = _warm(tr, n=20, start=100.0)  # 100s después: el spike ya se evictó
    sig = tr.observe(T, "no", 500, t)
    assert sig is not None and sig.pressure == "NO"  # el spike viejo no tapa la presión actual


# ── Shadow (auto-medición + persistencia + best-effort) ──────────────────────


def _shadow(mids: dict[str, float | None]) -> Motor8OfiShadow:
    return Motor8OfiShadow(
        lambda t: mids.get(t),
        window_sec=60.0,
        z_min=3.0,
        min_baseline=20,
        cooldown_sec=1000.0,
    )


def _ofi_windows() -> list[models.EdgeWindow]:
    with models.get_session() as s:
        return [w for w in s.exec(select(models.EdgeWindow)).all() if w.kind == "ofi"]


def _drive(sh: Motor8OfiShadow, start: float = 0.0) -> float:
    """Baseline + spike YES → una señal a t; devuelve t."""
    t = start
    for i in range(30):
        sh.observe_delta(T, "yes" if i % 2 == 0 else "no", 1, now=t)
        t += 1.0
    sh.observe_delta(T, "yes", 500, now=t)  # señal (mid0 capturado acá)
    return t


def test_signal_measures_and_persists_signed_moves():
    """MECANISMO completo: señal (presión YES, mid0=50) → a T+30 mid=53 → a T+60 mid=55
    → EdgeWindow kind=ofi con move30=+3, move60=+5 (momentum ganó) y z en edge_pct."""
    mids = {T: 50.0}
    sh = _shadow(mids)
    t0 = _drive(sh)
    mids[T] = 53.0
    sh.observe_delta(T, "yes", 1, now=t0 + 31)  # empuja la medición T+30
    mids[T] = 55.0
    sh.observe_delta(T, "no", 1, now=t0 + 61)  # empuja T+60 → persiste

    wins = _ofi_windows()
    assert len(wins) == 1
    w = wins[0]
    assert w.gross_spread_cents == 3 and w.magnitude_cents == 5  # firmado desde presión YES
    assert w.edge_pct is not None and w.edge_pct > 3.0  # z-score
    assert sh.stats()["measured"] == 1


def test_no_pressure_signal_signs_moves_from_pressure():
    """Con presión NO y el mid CAYENDO, el move firmado desde la presión es POSITIVO
    (el precio siguió a la presión)."""
    mids = {T: 50.0}
    sh = _shadow(mids)
    t = 0.0
    for i in range(30):
        sh.observe_delta(T, "yes" if i % 2 == 0 else "no", 1, now=t)
        t += 1.0
    sh.observe_delta(T, "no", 500, now=t)  # presión NO, mid0=50
    mids[T] = 46.0
    sh.observe_delta(T, "yes", 1, now=t + 31)
    sh.observe_delta(T, "yes", 1, now=t + 61)
    w = _ofi_windows()[0]
    assert w.magnitude_cents == 4  # (46−50)×(−1) = +4 → momentum de la presión NO


def test_dead_book_drops_signal_without_result():
    """FAIL-SAFE: si el book no da mid durante toda la gracia, la señal se DESCARTA
    (mejor sin dato que con dato basura) y se cuenta en stats."""
    mids = {T: 50.0}
    sh = _shadow(mids)
    t0 = _drive(sh)
    mids[T] = None  # book en cuarentena/stale desde ya
    sh.observe_delta(T, "yes", 1, now=t0 + 31)
    sh.observe_delta(T, "yes", 1, now=t0 + 130)  # pasó la gracia (120s)
    assert _ofi_windows() == []
    assert sh.stats()["dropped"] == 1 and sh.stats()["pending"] == 0


def test_signal_without_mid0_never_enters_experiment():
    """Sin mid al momento de la señal (book caído) → ni siquiera entra al experimento."""
    sh = _shadow({})  # mid siempre None
    t0 = _drive(sh)
    assert sh.stats()["signals"] == 0 and sh.stats()["dropped"] >= 1
    assert t0 > 0


def test_observe_delta_is_best_effort():
    """FAIL-SAFE total: un tracker roto no propaga — la captura del feed sigue viva."""
    sh = _shadow({T: 50.0})
    with patch.object(sh, "_tracker") as broken:
        broken.observe.side_effect = RuntimeError("boom")
        sh.observe_delta(T, "yes", 1, now=0.0)  # no raise


# ── Guard ESTRUCTURAL de F1 ──────────────────────────────────────────────────


def test_module_cannot_place_orders():
    """F1: el paquete de M8 no importa el cliente de órdenes. Quien quiera F3 pasa por
    el diseño completo (Capa A + RiskManager), no por acá."""
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[3] / "src" / "strategies" / "motor_8_ofi"
    for f in pkg.glob("*.py"):
        body = f.read_text()
        assert "kalshi_rest" not in body, f"{f.name} importa el cliente REST"
        assert "place_order" not in body, f"{f.name} referencia la API de órdenes"
        assert "KalshiRestClient" not in body, f"{f.name} referencia el cliente de órdenes"
