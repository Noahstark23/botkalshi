"""
Tests del wiring del Motor REST (shadow) en DataCaptureService._register_ws_handlers.

Verifica que el engine se instancia/registra SOLO con MOTOR_REST_ENABLED=True,
y nunca con el flag en False. El muro de ejecución (TRADING_ENABLED) es
independiente: el shadow corre con MOTOR_REST_ENABLED=True + TRADING_ENABLED=False.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.strategies.data_capture import DataCaptureService


def _service_with_flags(
    *, motor_rest: bool, trading: bool = False, with_client: bool = False
) -> DataCaptureService:
    settings = MagicMock()
    settings.USE_ORDERBOOK_MANAGER_V2 = False
    settings.MOTOR_1_ARBITRAGE_ENABLED = False
    settings.MOTOR_REST_ENABLED = motor_rest
    settings.TRADING_ENABLED = trading
    rest_client = MagicMock() if with_client else None
    # Mockear get_settings en ambos módulos: data_capture y el engine (que lo llama en su __init__).
    # RiskManager también llama get_settings en su __init__ (path live) → mockearlo ahí.
    with patch("src.strategies.data_capture.get_settings", return_value=settings), patch(
        "src.strategies.data_capture.KalshiWebSocket"
    ), patch("src.strategies.motor_rest_arb.engine.get_settings", return_value=settings), patch(
        "src.risk.manager.get_settings", return_value=settings
    ):
        svc = DataCaptureService(rest_client=rest_client)
        svc.ws = MagicMock()
        # _register_ws_handlers se llama dentro del with para que el engine vea el mock.
        svc._register_ws_handlers()
        return svc


def test_motor_rest_wired_when_flag_enabled():
    """MOTOR_REST_ENABLED=True → engine instanciado y handler de ticker registrado."""
    svc = _service_with_flags(motor_rest=True, trading=False)

    assert svc._rest_engine is not None
    # El handler del engine quedó registrado en el canal "ticker".
    ticker_handlers = [
        call.args for call in svc.ws.on.call_args_list if call.args and call.args[0] == "ticker"
    ]
    assert any(h[1] == svc._rest_engine.on_ticker for h in ticker_handlers)


def test_motor_rest_not_wired_when_flag_disabled():
    """MOTOR_REST_ENABLED=False → engine NO se instancia (queda None)."""
    svc = _service_with_flags(motor_rest=False)

    assert svc._rest_engine is None
    # Ningún handler de ticker es de un RestArbEngine.
    registered = [call.args for call in svc.ws.on.call_args_list]
    # El handler base _on_ticker sí está; el del engine no.
    assert ("ticker", svc._on_ticker) in [(a[0], a[1]) for a in registered if len(a) >= 2]


def test_motor_rest_does_not_touch_execution_in_shadow():
    """Shadow (MOTOR_REST_ENABLED=True, TRADING_ENABLED=False): el engine NO puede ejecutar."""
    svc = _service_with_flags(motor_rest=True, trading=False)

    # Capa A: el engine existe pero su path de ejecución no se construyó.
    engine = svc._rest_engine
    assert engine is not None
    assert engine._executor is None
    assert engine._risk_manager is None
    assert not hasattr(engine, "place_order")


def test_motor_rest_builds_executor_when_live_with_client():
    """Capa A: TRADING_ENABLED=true + cliente presente → executor + risk_manager construidos."""
    svc = _service_with_flags(motor_rest=True, trading=True, with_client=True)

    engine = svc._rest_engine
    assert engine is not None
    assert engine._executor is not None
    assert engine._risk_manager is not None


def test_motor_rest_stays_shadow_when_live_but_no_client():
    """Capa A (guard): TRADING_ENABLED=true pero SIN cliente → se queda en shadow (executor=None)."""
    svc = _service_with_flags(motor_rest=True, trading=True, with_client=False)

    engine = svc._rest_engine
    assert engine is not None
    assert engine._executor is None
    assert engine._risk_manager is None
