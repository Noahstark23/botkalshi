"""Tests de compatibilidad de versión de websockets.

Regression tests para el bug del 14 mayo 2026: extra_headers → additional_headers.
Si alguno de estos tests falla, pyproject.toml tiene un pin incorrecto de websockets.
"""

import inspect

import pytest
import websockets


def test_websockets_version_supports_additional_headers():
    """Confirma que la versión instalada acepta additional_headers kwarg."""
    sig = inspect.signature(websockets.connect)
    params = sig.parameters

    assert "additional_headers" in params, (
        f"websockets {websockets.__version__} does not support additional_headers. "
        f"Required: >=13.0. Got params: {list(params.keys())}"
    )


def test_websockets_version_no_longer_supports_extra_headers():
    """
    Si esta aserción FALLA, pyproject.toml downgradeó a websockets <13.0.
    NO mergear sin verificar el pin en pyproject.toml y Dockerfile.
    """
    sig = inspect.signature(websockets.connect)
    params = sig.parameters

    if "extra_headers" in params:
        pytest.fail(
            f"websockets {websockets.__version__} still has deprecated extra_headers kwarg. "
            "Verify pyproject.toml pins >=13.0,<17.0"
        )


def test_websockets_version_is_at_least_13():
    """Pin mínimo verificado en tiempo de ejecución."""
    major, minor, *_ = websockets.__version__.split(".")
    version_tuple = (int(major), int(minor))
    assert version_tuple >= (13, 0), (
        f"websockets {websockets.__version__} < 13.0. Update pyproject.toml pin to >=13.0,<17.0"
    )
