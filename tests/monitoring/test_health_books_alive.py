"""
Check `books_alive` de /health (incidente 2026-07-31 — TERCER falso-healthy del endpoint).

El bot pasó 9.5 horas con tracked=229 / initialized=0 (espiral de recovery) mientras
/health decía healthy: ws_alive era true porque LLEGABAN mensajes... que se tiraban a un
loop de recovery que jamás convergía. Coolify no reinició nada y el operador se enteró
por Telegram (flood de sid_gap), no por el healthcheck.

Ahora: con captura corriendo y un manager V2 trackeando tickers, CERO books inicializados
sostenido más de BLIND_GRACE_SEC = unhealthy. La gracia cubre el warm-up normal (~2-5 min
post-subscribe). Fail-open de LECTURA: un error del stats() NO marca unhealthy.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.monitoring.health import BLIND_GRACE_SEC, BotState, app


@pytest.fixture(autouse=True)
def reset_bot_state():
    BotState.is_paused = False
    BotState.capture_running = True
    BotState.last_ws_message = datetime.now(UTC)  # ws_alive verde (no es lo que se testea acá)
    BotState.v2_manager = None
    BotState.books_blind_since = None
    yield
    BotState.capture_running = False
    BotState.last_ws_message = None
    BotState.v2_manager = None
    BotState.books_blind_since = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _manager(tracked: int, initialized: int) -> MagicMock:
    mgr = MagicMock()
    mgr.stats.return_value = {"tracked_tickers": tracked, "initialized_tickers": initialized}
    return mgr


def test_ciego_dentro_de_la_gracia_sigue_healthy(client):
    """Warm-up normal: tracked>0 con initialized=0 recién observado NO es unhealthy."""
    BotState.v2_manager = _manager(tracked=229, initialized=0)

    r = client.get("/health")

    assert r.json()["checks"]["books_alive"] is True
    assert r.json()["status"] == "healthy"
    assert BotState.books_blind_since is not None  # el reloj de ceguera arrancó


def test_fraccion_baja_es_ciego_aunque_no_sea_cero(client):
    """QUINTO FAIL-OPEN (2026-08-02): con 8/203 books el `initialized == 0` original
    daba verde con el bot funcionalmente ciego y la siembra latcheada 35 min. Un bit
    que se satisface con cualquier cosa > 0 no mide lo que su nombre promete."""
    BotState.v2_manager = _manager(tracked=203, initialized=8)
    BotState.books_blind_since = time.monotonic() - BLIND_GRACE_SEC - 1.0

    r = client.get("/health")

    assert r.status_code == 503
    assert r.json()["detail"]["checks"]["books_alive"] is False


def test_fraccion_sobre_el_umbral_es_sano(client):
    """CONTROL: settleos y cuarentenas transitorias (initialized alto pero < tracked)
    no disparan el check — la fracción tolera la operación normal."""
    BotState.v2_manager = _manager(tracked=203, initialized=180)

    r = client.get("/health")

    assert r.json()["checks"]["books_alive"] is True
    assert BotState.books_blind_since is None


def test_ciego_sostenido_mas_alla_de_la_gracia_es_unhealthy(client):
    """El caso del incidente: initialized=0 sostenido (9.5h reales; acá gracia+1s)."""
    BotState.v2_manager = _manager(tracked=229, initialized=0)
    BotState.books_blind_since = time.monotonic() - BLIND_GRACE_SEC - 1.0

    r = client.get("/health")

    assert r.status_code == 503  # Coolify reinicia por ESTE código, no por el body
    detail = r.json()["detail"]
    assert detail["checks"]["books_alive"] is False
    assert detail["status"] == "unhealthy"


def test_books_inicializados_resetea_el_reloj(client):
    """Cuando los books convergen, books_alive vuelve a True y el reloj se limpia — una
    ceguera FUTURA arranca su propia ventana de gracia (no hereda la vieja)."""
    BotState.v2_manager = _manager(tracked=229, initialized=213)
    BotState.books_blind_since = time.monotonic() - BLIND_GRACE_SEC - 1.0  # ceguera previa

    r = client.get("/health")

    assert r.json()["checks"]["books_alive"] is True
    assert r.json()["status"] == "healthy"
    assert BotState.books_blind_since is None


def test_sin_tickers_trackeados_no_es_ceguera(client):
    """tracked=0 (pre-discovery) no es un feed ciego — no hay nada que inicializar."""
    BotState.v2_manager = _manager(tracked=0, initialized=0)

    r = client.get("/health")

    assert r.json()["checks"]["books_alive"] is True
    assert BotState.books_blind_since is None


def test_error_del_stats_falla_abierto(client):
    """Fail-open de LECTURA (regla 6 del protocolo): un stats() roto no apaga el bot."""
    mgr = MagicMock()
    mgr.stats.side_effect = RuntimeError("boom")
    BotState.v2_manager = mgr

    r = client.get("/health")

    assert r.json()["checks"]["books_alive"] is True
    assert r.json()["status"] == "healthy"


def test_sin_manager_o_sin_captura_no_evalua(client):
    """Sin V2 (manager None) o sin captura corriendo, el check ni aparece — no hay
    señal que evaluar y un falso unhealthy reiniciaría el container en boot."""
    BotState.v2_manager = None
    assert "books_alive" not in client.get("/health").json()["checks"]

    BotState.v2_manager = _manager(tracked=229, initialized=0)
    BotState.capture_running = False
    assert "books_alive" not in client.get("/health").json()["checks"]
