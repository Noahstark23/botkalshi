"""
Unit test del contador `size_real` del heartbeat del Motor REST.

QUÉ PRUEBA: la LÓGICA DE CONTEO — el parser real de `trigger.py`
(`_parse_size_float` + las keys reales) más la regla replicada
`bid_size > 0 AND ask_size > 0`. Demuestra que `size_real X/200` BAJA cuando hay
ticks malos, o sea que el `200/200` del heartbeat significa "size real" y no
"contador clavado".

QUÉ NO PRUEBA: NO ejecuta `engine.on_ticker` (evita instanciar settings/DB). La
regla de conteo está REPLICADA acá en `_counts_as_real`; si la regla del engine
cambia, esta réplica hay que actualizarla A MANO — este test NO detecta
automáticamente una divergencia entre la regla del engine y esta copia. Cubre el
parser real + la lógica de conteo, no el engine en sí.

SHAPE REAL: los sizes vienen como strings fixed-point ("8.73", "0.00"), o como
None (campo presente pero null), o el campo está ausente — los tres casos vistos
en producción. El test enfrenta al parser con esos tipos reales, no con int/float.

Corre con pytest o, sin pytest (no está en la imagen de runtime de prod), directo:
    python3 tests/test_motor_rest_size_counter.py
"""
from __future__ import annotations

from typing import Any

from src.strategies.motor_rest_arb.trigger import (
    _YES_ASK_SIZE_KEYS,
    _YES_BID_SIZE_KEYS,
    _parse_size_float,
)

# Centinela para "el campo NO existe en el payload" (distinto de estar presente y
# valer None). Refleja los UCL flacos con el lado YES ausente que vimos en prod.
_ABSENT = object()


def _make_tick(bid: Any, ask: Any) -> dict[str, Any]:
    """
    Construye el dict de un tick con el shape real (keys `*_size_fp`).

    Si un valor es `_ABSENT`, la key se OMITE (campo ausente). Cualquier otro valor
    —incluido None y strings fp como "8.73"/"0.00"— se setea tal cual para que el
    parser lo enfrente como en producción.
    """
    tick: dict[str, Any] = {}
    if bid is not _ABSENT:
        tick["yes_bid_size_fp"] = bid
    if ask is not _ABSENT:
        tick["yes_ask_size_fp"] = ask
    return tick


def _counts_as_real(tick: dict[str, Any]) -> bool:
    """Replica la regla de conteo del engine para NO instanciar settings/DB.

    LIMITACIÓN: esto REPLICA la regla (bid_size > 0 AND ask_size > 0), no ejecuta
    engine.on_ticker. Si la regla del engine CAMBIA, hay que actualizar esta réplica
    manualmente — este test NO captura automáticamente una divergencia entre la regla
    del engine y esta copia. Prueba la lógica de conteo + el parser real, no el engine.
    """
    bid_size = _parse_size_float(tick, _YES_BID_SIZE_KEYS)
    ask_size = _parse_size_float(tick, _YES_ASK_SIZE_KEYS)
    return bid_size > 0 and ask_size > 0


def test_parser_discrimina_cada_tipo_de_size() -> None:
    """El parser+regla distingue size real de los distintos modos de 'malo'."""
    # Ambos lados con size real (strings fp) → cuenta.
    assert _counts_as_real(_make_tick("8.73", "9.10")) is True
    # Sizes chicos pero > 0 → cuentan (la regla es >0, no min_depth).
    assert _counts_as_real(_make_tick("0.50", "0.0010")) is True

    # Size cero como string fp ("0.00") → NO cuenta.
    assert _counts_as_real(_make_tick("0.00", "5.00")) is False
    assert _counts_as_real(_make_tick("5.00", "0.00")) is False
    # Campo presente pero None → NO cuenta.
    assert _counts_as_real(_make_tick(None, "5.00")) is False
    assert _counts_as_real(_make_tick("5.00", None)) is False
    # Campo ausente → NO cuenta.
    assert _counts_as_real(_make_tick(_ABSENT, "5.00")) is False
    assert _counts_as_real(_make_tick("5.00", _ABSENT)) is False
    # String vacío (caso límite real) → NO cuenta y NO tira excepción.
    assert _counts_as_real(_make_tick("", "5.00")) is False


def test_contador_no_esta_clavado_en_200() -> None:
    """Ventana de 200 con 3 ticks malos → 197/200 (el contador NO está clavado)."""
    ticks: list[dict[str, Any]] = [_make_tick("8.73", "9.10") for _ in range(197)]
    # 3 malos, uno de cada modo: string "0.00", None, campo ausente.
    ticks.append(_make_tick("0.00", "5.00"))
    ticks.append(_make_tick(None, "5.00"))
    ticks.append(_make_tick(_ABSENT, "5.00"))

    assert len(ticks) == 200
    size_real = sum(1 for t in ticks if _counts_as_real(t))
    assert size_real == 197


def test_ventana_toda_eficiente_da_200() -> None:
    """Control: 200 ticks con size real → 200/200."""
    ticks = [_make_tick("8.73", "9.10") for _ in range(200)]
    assert sum(1 for t in ticks if _counts_as_real(t)) == 200


def test_ventana_toda_rota_da_0() -> None:
    """Control: 200 ticks con size None → 0/200 (parser/shape roto)."""
    ticks = [_make_tick(None, None) for _ in range(200)]
    assert sum(1 for t in ticks if _counts_as_real(t)) == 0


if __name__ == "__main__":
    # Runner stdlib: corre sin pytest (no está en la imagen de runtime de prod).
    _failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print(f"PASS {_name}")
            except AssertionError as _exc:
                _failures += 1
                print(f"FAIL {_name}: {_exc}")
    raise SystemExit(1 if _failures else 0)
