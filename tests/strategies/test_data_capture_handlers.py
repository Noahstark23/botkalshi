"""Tests del handler de orderbook_delta y orderbook_snapshot con shape 2026 (fixed-point)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.monitoring.health import BotState
from src.strategies.data_capture import (
    DataCaptureService,
    _top_bid,
    parse_price_to_cents,
    parse_size,
    rest_orderbook_sides,
)


@pytest.fixture(autouse=True)
def reset_botstate():
    BotState.last_error = None
    BotState.last_error_at = None
    yield
    BotState.last_error = None
    BotState.last_error_at = None


@pytest.fixture
def service():
    with (
        patch("src.strategies.data_capture.get_settings"),
        patch("src.strategies.data_capture.KalshiWebSocket"),
    ):
        svc = DataCaptureService()
        return svc


def test_ensure_v2_manager_publishes_botstate_early_and_idempotent():
    """_ensure_v2_manager crea la instancia + la publica en BotState ANTES del discovery (cierra la
    carrera del /status instance=missing) y es idempotente."""
    with (
        patch("src.strategies.data_capture.get_settings") as gs,
        patch("src.strategies.data_capture.KalshiWebSocket"),
    ):
        s = gs.return_value
        s.USE_ORDERBOOK_MANAGER_V2 = True
        s.MOTOR_1_ARBITRAGE_ENABLED = False
        s.ORDERBOOK_V2_RECOVERY_TIMEOUT_SEC = 30.0
        s.ORDERBOOK_V2_MAX_RECOVERY_BUFFER = 25000
        s.ORDERBOOK_V2_RECOVERY_CHUNK_SIZE = 50
        s.ORDERBOOK_V2_BOOTSTRAP_BUFFER_CAP = 1000
        s.ORDERBOOK_V2_RECOVERY_BACKOFF_BASE_SEC = 30.0
        s.ORDERBOOK_V2_RECOVERY_BACKOFF_FACTOR = 4.0
        s.ORDERBOOK_V2_RECOVERY_BACKOFF_CAP_SEC = 1800.0
        svc = DataCaptureService()
        assert svc._v2_manager is None

        svc._ensure_v2_manager()
        assert svc._v2_manager is not None
        assert BotState.v2_manager is svc._v2_manager

        first = svc._v2_manager
        svc._ensure_v2_manager()  # idempotente: no recrea
        assert svc._v2_manager is first
    BotState.v2_manager = None  # limpieza (estado de CLASE)


def test_ensure_v2_manager_noop_when_disabled():
    """Con V2 y Motor 1 apagados, no se crea nada (path idéntico al previo)."""
    with (
        patch("src.strategies.data_capture.get_settings") as gs,
        patch("src.strategies.data_capture.KalshiWebSocket"),
    ):
        s = gs.return_value
        s.USE_ORDERBOOK_MANAGER_V2 = False
        s.MOTOR_1_ARBITRAGE_ENABLED = False
        svc = DataCaptureService()
        svc._ensure_v2_manager()
        assert svc._v2_manager is None


# =====================================================
# parse_price_to_cents
# =====================================================


def test_parse_price_to_cents_dollar_string():
    assert parse_price_to_cents("0.2700") == 27
    assert parse_price_to_cents("0.5000") == 50
    assert parse_price_to_cents("0.9900") == 99
    assert parse_price_to_cents("0.0100") == 1


def test_parse_price_to_cents_integer_backward_compat():
    assert parse_price_to_cents(45) == 45
    assert parse_price_to_cents(0) == 0
    assert parse_price_to_cents(99) == 99


def test_parse_price_to_cents_float():
    assert parse_price_to_cents(0.27) == 27
    assert parse_price_to_cents(0.50) == 50


def test_parse_price_to_cents_invalid():
    assert parse_price_to_cents(None) is None
    assert parse_price_to_cents("not a number") is None
    assert parse_price_to_cents("") is None


# =====================================================
# parse_size
# =====================================================


def test_parse_size_string():
    assert parse_size("100.00") == 100
    assert parse_size("-100.00") == -100
    assert parse_size("502000.00") == 502000


def test_parse_size_integer():
    assert parse_size(10) == 10
    assert parse_size(-50) == -50


def test_parse_size_invalid():
    assert parse_size(None) is None
    assert parse_size("bad") is None


# =====================================================
# rest_orderbook_sides — normalizador del REST /orderbook (incidente 2026-07-15:
# la API migró a orderbook_fp + yes_dollars/no_dollars y dejó ciegos a M5 y a los
# brazos de salida de M2/M3)
# =====================================================


def test_rest_sides_new_shape_2026_07():
    """MECANISMO: el shape real capturado por el diag de #170 (160 líneas, books con
    ~$500k resting que parseaban vacío)."""
    ob = {
        "orderbook_fp": {
            "yes_dollars": [["0.5400", "558383.77"], ["0.5300", "1000.00"]],
            "no_dollars": [["0.4500", "2000.00"]],
        }
    }
    sides = rest_orderbook_sides(ob)
    assert _top_bid(sides["yes"]) == (54, 558384)
    assert _top_bid(sides["no"]) == (45, 2000)


def test_rest_sides_legacy_shape_unchanged():
    """CONTROL: el shape histórico (orderbook + yes/no en cents int) sigue igual."""
    ob = {"orderbook": {"yes": [[42, 50]], "no": [[55, 30]]}}
    sides = rest_orderbook_sides(ob)
    assert _top_bid(sides["yes"]) == (42, 50)
    assert _top_bid(sides["no"]) == (55, 30)


def test_rest_sides_ws_fp_variant_and_unwrapped():
    """CONTROL: la variante *_dollars_fp (estilo WS) y un book ya desenvuelto también
    normalizan — cualquier generación, mismas salidas."""
    assert _top_bid(
        rest_orderbook_sides({"orderbook": {"yes_dollars_fp": [["0.4200", "50.00"]]}})["yes"]
    ) == (42, 50)
    assert _top_bid(rest_orderbook_sides({"yes": [[42, 50]]})["yes"]) == (42, 50)


def test_rest_sides_fail_safe_on_garbage():
    """FAIL-SAFE: input raro (None, no-dict, wrapper con basura, lados no-lista) →
    lados vacíos, jamás excepción (Lección 7: un hiccup no rompe el tick)."""
    for garbage in (None, [], "x", {"orderbook_fp": None}, {"orderbook": {"yes": "no-list"}}):
        sides = rest_orderbook_sides(garbage)
        assert sides == {"yes": [], "no": []}


# =====================================================
# _on_orderbook_delta — shape 2026 (fixed-point dollar strings)
# =====================================================


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_delta_handler_processes_dollar_string_shape(mock_session, service):
    """Shape 2026: price y delta como strings dollar fixed-point."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": 100,
        "msg": {
            "market_ticker": "KXMLB-26-LAD",
            "side": "yes",
            "price": "0.2700",
            "delta": "-100.00",
        },
    }

    await service._on_orderbook_delta(msg)

    assert mock_db.add.called
    added = mock_db.add.call_args[0][0]
    assert added.ticker == "KXMLB-26-LAD"
    assert added.side == "yes"
    assert added.price_cents == 27
    assert added.delta == -100


# =====================================================
# Gate PERSIST_ORDERBOOK_EVENTS (incidente disco-lleno 2026-07-10)
# =====================================================


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_orderbook_events_not_persisted_when_flag_off(mock_session, service):
    """Con PERSIST_ORDERBOOK_EVENTS=false (default), un delta válido NO escribe fila (los
    books viven en memoria; era la tabla que llenó 54G)."""
    service.settings.PERSIST_ORDERBOOK_EVENTS = False
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": "KXMLB-26-LAD",
            "side": "yes",
            "price": "0.2700",
            "delta": "-100.00",
        },
    }
    await service._on_orderbook_delta(msg)

    assert not mock_db.add.called  # CERO inserts con el flag off
    assert BotState.last_error is None  # y no es un error: es el comportamiento esperado


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_orderbook_events_persisted_when_flag_on(mock_session, service):
    """CONTROL: con el flag ON, sí escribe (debug puntual, acotado por la retención de 2d)."""
    service.settings.PERSIST_ORDERBOOK_EVENTS = True
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": "KXMLB-26-LAD",
            "side": "yes",
            "price": "0.2700",
            "delta": "-100.00",
        },
    }
    await service._on_orderbook_delta(msg)

    assert mock_db.add.called


# =====================================================
# _on_orderbook_delta — shape viejo (backward compat)
# =====================================================


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_delta_handler_processes_integer_shape_backward_compat(mock_session, service):
    """Shape viejo con ints directos sigue funcionando."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": "KXNBA-26-LAL",
            "side": "no",
            "price": 45,
            "delta": 10,
        },
    }

    await service._on_orderbook_delta(msg)

    assert mock_db.add.called
    added = mock_db.add.call_args[0][0]
    assert added.ticker == "KXNBA-26-LAL"
    assert added.side == "no"
    assert added.price_cents == 45
    assert added.delta == 10


# =====================================================
# _on_orderbook_delta — shape desconocido → record_error
# =====================================================


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_delta_handler_unknown_shape_records_error(mock_session, service):
    """Si Kalshi cambia el shape otra vez, el handler debe RECORD_ERROR visible."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": "KXMLB-26-LAD",
            # falta side, price, delta — shape inesperado
            "alien_field": "???",
        },
    }

    await service._on_orderbook_delta(msg)

    # No inserto nada
    assert not mock_db.add.called
    # Pero registro error visible
    assert BotState.last_error is not None
    assert (
        "unknown shape" in BotState.last_error.lower() or "missing" in BotState.last_error.lower()
    )


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_delta_handler_missing_ticker_records_error(mock_session, service):
    """Mensaje sin market_ticker tambien es shape desconocido."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_delta",
        "msg": {
            "side": "yes",
            "price": "0.2700",
            "delta": "10.00",
            # sin market_ticker
        },
    }

    await service._on_orderbook_delta(msg)

    assert not mock_db.add.called
    assert BotState.last_error is not None


# =====================================================
# _on_orderbook_snapshot
# =====================================================


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_snapshot_handler_extracts_top_of_book(mock_session, service):
    """Snapshot debe extraer top-of-book correctamente y persistir."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXMLB-26-LAD",
            "yes_dollars_fp": [
                ["0.2700", "100.00"],
                ["0.2600", "50.00"],
                ["0.2500", "200.00"],
            ],
            "no_dollars_fp": [
                ["0.7000", "75.00"],
                ["0.6800", "120.00"],
            ],
        },
    }

    await service._on_orderbook_snapshot(msg)

    assert mock_db.add.called
    added = mock_db.add.call_args[0][0]
    assert added.ticker == "KXMLB-26-LAD"
    # Best yes bid: 27c (mayor precio de la lista)
    assert added.yes_bid == 27
    # Best no bid: 70c
    assert added.no_bid == 70
    # yes_ask = 100 - no_bid = 30
    assert added.yes_ask == 30
    # no_ask = 100 - yes_bid = 73
    assert added.no_ask == 73


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_snapshot_handler_empty_levels_handled(mock_session, service):
    """Snapshot con levels vacios no debe crashear."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXMLB-26-ZERO",
            "yes_dollars_fp": [],
            "no_dollars_fp": [],
        },
    }

    await service._on_orderbook_snapshot(msg)

    # Inserto con zeros, no crasheo
    assert mock_db.add.called
    added = mock_db.add.call_args[0][0]
    assert added.ticker == "KXMLB-26-ZERO"
    assert added.yes_bid == 0
    assert added.no_bid == 0


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_snapshot_handler_fallback_to_old_yes_no_fields(mock_session, service):
    """Fallback a campos 'yes'/'no' si yes_dollars_fp no esta presente."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXNBA-26-LAL",
            "yes": [[45, 100], [40, 50]],  # shape viejo: [price_cents, size]
            "no": [[55, 200]],
        },
    }

    await service._on_orderbook_snapshot(msg)

    assert mock_db.add.called
    added = mock_db.add.call_args[0][0]
    assert added.yes_bid == 45  # mayor precio viejo
    assert added.no_bid == 55


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_snapshot_handler_no_ticker_skips_silently(mock_session, service):
    """Snapshot sin ticker no crashea ni persiste."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_snapshot",
        "msg": {
            # sin market_ticker
            "yes_dollars_fp": [["0.5000", "100.00"]],
        },
    }

    await service._on_orderbook_snapshot(msg)

    assert not mock_db.add.called
    assert BotState.last_error is None


# =====================================================
# Shape logging (una sola vez por sesion)
# =====================================================


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_delta_shape_logged_only_once(mock_session, service):
    """El flag _delta_shape_logged previene logs repetidos."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_delta",
        "msg": {"market_ticker": "X", "side": "yes", "price": 50, "delta": 1},
    }

    assert service._delta_shape_logged is False
    await service._on_orderbook_delta(msg)
    assert service._delta_shape_logged is True
    # Segunda llamada: flag ya true, no re-logea
    await service._on_orderbook_delta(msg)
    assert service._delta_shape_logged is True


# =====================================================
# Regression: payload REAL capturado de Kalshi prod
# (via scripts/inspect_ws.py --verbose, 2026-05-16)
# =====================================================

# Payload literal capturado contra wss://api.elections.kalshi.com/trade-api/ws/v2
REAL_DELTA_KALSHI_2026 = {
    "type": "orderbook_delta",
    "sid": 1,
    "seq": 37,
    "msg": {
        "market_ticker": "KXNBA-26-NYK",
        "market_id": "532ab8e8-d5dd-4139-9a1b-0f634d2d1d2e",
        "price_dollars": "0.8500",
        "delta_fp": "-2500.00",
        "side": "no",
        "ts": "2026-05-15T15:08:25.405575Z",
        "ts_ms": 1778857705405,
    },
}

REAL_DELTA_KALSHI_2026_YES = {
    "type": "orderbook_delta",
    "sid": 1,
    "seq": 38,
    "msg": {
        "market_ticker": "KXNBA-26-NYK",
        "market_id": "532ab8e8-d5dd-4139-9a1b-0f634d2d1d2e",
        "price_dollars": "0.1200",
        "delta_fp": "53.00",
        "side": "yes",
        "ts": "2026-05-16T17:14:32.925429Z",
        "ts_ms": 1778951672925,
    },
}


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_delta_real_kalshi_2026_no_side(mock_session, service):
    """Payload literal capturado de produccion: side=no, price_dollars=0.8500, delta_fp=-2500."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    await service._on_orderbook_delta(REAL_DELTA_KALSHI_2026)

    assert mock_db.add.called, "Debe persistir el evento — era el bug que causaba 0 rows"
    added = mock_db.add.call_args[0][0]
    assert added.ticker == "KXNBA-26-NYK"
    assert added.side == "no"
    assert added.price_cents == 85  # "0.8500" * 100 = 85
    assert added.delta == -2500  # "-2500.00" -> -2500
    assert BotState.last_error is None


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_delta_real_kalshi_2026_yes_side(mock_session, service):
    """Payload de produccion: side=yes, price_dollars=0.1200, delta_fp=53.00."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    await service._on_orderbook_delta(REAL_DELTA_KALSHI_2026_YES)

    assert mock_db.add.called
    added = mock_db.add.call_args[0][0]
    assert added.ticker == "KXNBA-26-NYK"
    assert added.side == "yes"
    assert added.price_cents == 12  # "0.1200" * 100 = 12
    assert added.delta == 53


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_delta_real_2026_no_warning_emitted(mock_session, service):
    """El fix no debe emitir WARNING para el shape real de Kalshi."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    await service._on_orderbook_delta(REAL_DELTA_KALSHI_2026)

    # Si hubiera WARNING se registraria en BotState.last_error
    assert BotState.last_error is None


# =====================================================
# Backpressure del DiskGuard (lazo cerrado, incidente 2026-07-10)
# =====================================================


@pytest.fixture
def disk_critical():
    """Simula disco en CRITICAL (estado de CLASE del DiskGuard) y lo restaura al salir."""
    from src.storage.disk_guard import CRITICAL, DiskGuard

    DiskGuard._state = CRITICAL
    yield
    DiskGuard.reset()


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_disk_critical_sheds_orderbook_events_even_with_flag_on(
    mock_session, service, disk_critical
):
    """Con disco CRITICAL, ni siquiera PERSIST_ORDERBOOK_EVENTS=true escribe: la telemetría
    se descarta (backpressure) para no comerse el disco que el trading necesita."""
    service.settings.PERSIST_ORDERBOOK_EVENTS = True
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": "KXMLB-26-LAD",
            "side": "yes",
            "price": "0.2700",
            "delta": "-100.00",
        },
    }
    await service._on_orderbook_delta(msg)

    assert not mock_db.add.called  # CERO inserts en critical
    assert BotState.last_error is None  # descartar telemetría NO es un error


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_disk_critical_sheds_market_snapshots(mock_session, service, disk_critical):
    """Con disco CRITICAL, el snapshot WS tampoco persiste MarketSnapshot (diagnóstico)."""
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXMLB-26-LAD",
            "yes_dollars_fp": [["0.2700", "100.00"]],
            "no_dollars_fp": [["0.7000", "75.00"]],
        },
    }
    await service._on_orderbook_snapshot(msg)

    assert not mock_db.add.called


@pytest.mark.asyncio
@patch("src.strategies.data_capture.get_session")
async def test_disk_ok_snapshot_still_persists(mock_session, service):
    """CONTROL: con disco OK (default), el snapshot persiste normal — el guard no rompe
    el path feliz."""
    from src.storage.disk_guard import DiskGuard

    DiskGuard.reset()
    mock_db = MagicMock()
    mock_session.return_value.__enter__ = MagicMock(return_value=mock_db)
    mock_session.return_value.__exit__ = MagicMock(return_value=False)

    msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXMLB-26-LAD",
            "yes_dollars_fp": [["0.2700", "100.00"]],
            "no_dollars_fp": [["0.7000", "75.00"]],
        },
    }
    await service._on_orderbook_snapshot(msg)

    assert mock_db.add.called
