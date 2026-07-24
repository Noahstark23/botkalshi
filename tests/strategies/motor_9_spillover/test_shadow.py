"""
Motor 9 "Derrame" — F1 SHADOW auto-validante (tesis de la auditoría 2026-07-18).

Verifica: el tracker puro (salto en ventana, cooldown, eviction), el shadow (trigger →
mid0 de los hermanos AL instante → follow-through T+60/T+120 firmado desde la dirección
ESPERADA = inversa del salto → EdgeWindow kind=spillover; anti-cascada por evento;
best-effort total), y el guard ESTRUCTURAL (el paquete no importa el cliente de órdenes).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import select

import src.storage.models as models
from src.strategies.motor_9_spillover.detector import SpilloverTracker
from src.strategies.motor_9_spillover.shadow import Motor9SpilloverShadow, event_key_of


@pytest.fixture(autouse=True)
def sqlite_engine(tmp_path):
    """DB real temporal como singleton de models (el shadow persiste EdgeWindow)."""
    db = tmp_path / "m9.db"
    settings = MagicMock()
    settings.DATABASE_URL = f"sqlite:///{db}"
    models._engine = None
    with patch("src.storage.models.get_settings", return_value=settings):
        engine = models.get_engine()
    models.SQLModel.metadata.create_all(engine)
    yield engine
    models._engine = None


EVENT = "KXMLBGAME-26JUL19HOUWSH"
A = f"{EVENT}-HOU"  # el que salta
B = f"{EVENT}-WSH"  # el hermano (candidato rezagado)


def _tracker(**kw) -> SpilloverTracker:
    defaults = {"trigger_move_cents": 5.0, "window_sec": 60.0, "cooldown_sec": 300.0}
    defaults.update(kw)
    return SpilloverTracker(**defaults)


# ── Detector (puro) ──────────────────────────────────────────────────────────


def test_jump_within_window_triggers_signed():
    """MECANISMO: 50→56 en la ventana → trigger con move firmado +6."""
    tr = _tracker()
    assert tr.observe(A, 50.0, 0.0) is None  # primer punto: sin referencia
    trig = tr.observe(A, 56.0, 10.0)
    assert trig is not None and trig.move_cents == pytest.approx(6.0)
    assert trig.mid_ref == pytest.approx(50.0)


def test_slow_drift_is_not_a_jump():
    """CONTROL: la misma distancia recorrida FUERA de la ventana no dispara (la
    referencia rueda con la ventana — deriva lenta ≠ salto)."""
    tr = _tracker(window_sec=60.0)
    t = 0.0
    for mid in (50.0, 51.0, 52.0, 53.0, 54.0, 55.0, 56.0):  # +1¢ cada 70s
        assert tr.observe(A, mid, t) is None
        t += 70.0


def test_cooldown_suppresses_retrigger():
    """Anti-ráfaga por ticker: el mismo episodio no re-dispara hasta el cooldown. Tras
    vencer, hace falta una referencia FRESCA dentro de la ventana (la vieja ya rodó)."""
    tr = _tracker(cooldown_sec=300.0)
    tr.observe(A, 50.0, 0.0)
    assert tr.observe(A, 56.0, 10.0) is not None
    assert tr.observe(A, 62.0, 20.0) is None  # silenciado
    assert tr.observe(A, 50.0, 305.0) is None  # referencia fresca post-cooldown (sin salto)
    assert tr.observe(A, 70.0, 320.0) is not None  # salto nuevo → dispara


def test_down_jump_triggers_negative():
    tr = _tracker()
    tr.observe(A, 50.0, 0.0)
    trig = tr.observe(A, 43.0, 5.0)
    assert trig is not None and trig.move_cents == pytest.approx(-7.0)


def test_event_key_of():
    assert event_key_of(A) == EVENT
    assert event_key_of("SINGUION") == "SINGUION"


# ── Shadow (auto-medición + persistencia + anti-cascada + best-effort) ───────


def _shadow(mids: dict[str, float | None], siblings: dict[str, set[str]]) -> Motor9SpilloverShadow:
    return Motor9SpilloverShadow(
        lambda t: mids.get(t),
        lambda t: siblings.get(t, set()),
        trigger_move_cents=5.0,
        window_sec=60.0,
        cooldown_sec=300.0,
    )


def _spillover_windows() -> list[models.EdgeWindow]:
    with models.get_session() as s:
        return [w for w in s.exec(select(models.EdgeWindow)).all() if w.kind == "spillover"]


def _drive_trigger(sh: Motor9SpilloverShadow, mids: dict, up_to: float = 56.0) -> float:
    """A: 50 → up_to (salto) con B quieto en 40. Devuelve t del trigger."""
    mids[A], mids[B] = 50.0, 40.0
    sh.observe(A, now=0.0)
    mids[A] = up_to
    sh.observe(A, now=10.0)  # trigger acá (mid0 de B capturado = 40)
    return 10.0


def test_spillover_measured_and_signed_from_expected_direction():
    """MECANISMO completo: A salta +6¢ → B (mid0=40) DEBERÍA bajar. B baja a 37 →
    follow firmado POSITIVO (+3 a T+60, +3 a T+120): derrame rezagado = capturable."""
    mids: dict[str, float | None] = {}
    siblings = {A: {B}, B: {A}}
    sh = _shadow(mids, siblings)
    t0 = _drive_trigger(sh, mids)
    mids[B] = 37.0  # el hermano ajusta DESPUÉS del trigger
    sh.observe(A, now=t0 + 61)  # empuja T+60
    sh.observe(A, now=t0 + 121)  # empuja T+120 → persiste

    wins = _spillover_windows()
    assert len(wins) == 1
    w = wins[0]
    assert w.market_ticker == B  # la fila es del HERMANO (donde se compraría)
    assert w.gross_spread_cents == 3 and w.magnitude_cents == 3  # follow esperado +3¢
    assert w.edge_pct == pytest.approx(6.0)  # el move del trigger, firmado
    assert w.leg_states == "src=HOU"  # forense: quién saltó
    assert sh.stats()["measured"] == 1


def test_sibling_moving_wrong_way_is_negative():
    """CONTROL de signo (el error que costaría plata): A sube y B TAMBIÉN sube →
    follow firmado NEGATIVO (la tesis de conservación falló en ese caso)."""
    mids: dict[str, float | None] = {}
    sh = _shadow(mids, {A: {B}, B: {A}})
    t0 = _drive_trigger(sh, mids)
    mids[B] = 44.0  # sube 4 en vez de bajar
    sh.observe(A, now=t0 + 61)
    sh.observe(A, now=t0 + 121)
    w = _spillover_windows()[0]
    assert w.gross_spread_cents == -4 and w.magnitude_cents == -4


def test_event_cooldown_blocks_mirror_cascade():
    """ANTI-CASCADA: el ajuste de B tras el trigger de A es en sí un salto — NO debe
    re-disparar como trigger nuevo del mismo evento (mediría el eco, no el derrame)."""
    mids: dict[str, float | None] = {}
    sh = _shadow(mids, {A: {B}, B: {A}})
    t0 = _drive_trigger(sh, mids)
    # B ajusta fuerte (−6¢): su propio tracker dispararía, el cooldown de EVENTO lo frena.
    sh.observe(B, now=t0 + 5)
    mids[B] = 34.0
    sh.observe(B, now=t0 + 20)
    assert sh.stats()["triggers"] == 1  # solo el de A


def test_dead_sibling_book_drops_without_result():
    """FAIL-SAFE: hermano sin mid durante toda la gracia → la medición se DESCARTA
    (mejor sin dato que con dato basura)."""
    mids: dict[str, float | None] = {}
    sh = _shadow(mids, {A: {B}, B: {A}})
    t0 = _drive_trigger(sh, mids)
    mids[B] = None  # book del hermano cae tras el trigger
    sh.observe(A, now=t0 + 61)
    sh.observe(A, now=t0 + 250)  # pasó la gracia (240)
    assert _spillover_windows() == []
    assert sh.stats()["dropped"] == 1 and sh.stats()["pending"] == 0


def test_trigger_without_siblings_never_enters_experiment():
    """Evento con un solo market trackeado: no hay a quién derramar → sin experimento."""
    mids: dict[str, float | None] = {}
    sh = _shadow(mids, {})  # sin hermanos
    _drive_trigger(sh, mids)
    assert sh.stats()["triggers"] == 0
    assert _spillover_windows() == []


def test_pending_is_capped():
    """Nada sin tope (lección OOM): las mediciones pendientes se acotan a MAX_PENDING;
    al tope se descarta la más vieja (contada en dropped). Cooldown corto para acumular
    triggers ANTES de que maduren las mediciones (T+120)."""
    mids: dict[str, float | None] = {A: 50.0, B: 40.0}
    sh = Motor9SpilloverShadow(
        lambda t: mids.get(t),
        lambda t: {A: {B}, B: {A}}.get(t, set()),
        trigger_move_cents=5.0,
        window_sec=60.0,
        cooldown_sec=5.0,  # corto: 3 triggers en <60s (nada madura todavía)
    )
    sh.MAX_PENDING = 2
    # Osilación 50↔58 cada 10s: dispara a t=10, 30, 50 (el retorno a 50 netea a 0 vs la
    # referencia vieja, así que solo los saltos hacia arriba disparan).
    for t, mid in (
        (0.0, 50.0),
        (10.0, 58.0),
        (20.0, 50.0),
        (30.0, 58.0),
        (40.0, 50.0),
        (50.0, 58.0),
    ):
        mids[A] = mid
        sh.observe(A, now=t)
    assert sh.stats()["triggers"] == 3
    assert sh.stats()["pending"] == 2  # capado
    assert sh.stats()["dropped"] >= 1  # la más vieja descartada


def test_observe_is_best_effort():
    """FAIL-SAFE total: un tracker roto no propaga — la captura del feed sigue viva."""
    mids: dict[str, float | None] = {A: 50.0}
    sh = _shadow(mids, {A: {B}})
    with patch.object(sh, "_tracker") as broken:
        broken.observe.side_effect = RuntimeError("boom")
        sh.observe(A, now=0.0)  # no raise


# ── Guard ESTRUCTURAL de F1 ──────────────────────────────────────────────────


def test_module_cannot_place_orders():
    """F1: el paquete de M9 no importa el cliente de órdenes. Quien quiera F3 pasa por
    el diseño completo (Capa A + RiskManager), no por acá."""
    from pathlib import Path

    pkg = Path(__file__).resolve().parents[3] / "src" / "strategies" / "motor_9_spillover"
    for f in pkg.glob("*.py"):
        body = f.read_text()
        assert "kalshi_rest" not in body, f"{f.name} importa el cliente REST"
        assert "place_order" not in body, f"{f.name} referencia la API de órdenes"
        assert "KalshiRestClient" not in body, f"{f.name} referencia el cliente de órdenes"
