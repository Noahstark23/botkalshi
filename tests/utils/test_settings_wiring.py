"""
Wiring runner↔Settings (incidente 2026-07-14, deploy #2 del mismo flag).

El merge remoto de #155 dejó el CONSUMO de MOTOR_2_FEE_AT_STAKE_COUNT en runner/poller
pero perdió la DECLARACIÓN del Field en Settings → AttributeError al boot de M2, que el
CI no atrapó porque los tests instanciaban el poller directo (nunca el camino
runner → settings.X). Este test cierra la clase entera de bugs: escanea TODO src/ por
referencias `settings.NOMBRE_EN_MAYUSCULAS` y exige que cada una exista como campo
declarado de Settings. Un flag a medio cablear rompe el CI, no producción.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.utils.config import Settings

SRC = Path(__file__).resolve().parents[2] / "src"
# settings.NOMBRE (self.settings.X, get_settings().X, s.settings.X…) — solo MAYÚSCULAS:
# los helpers/properties en minúscula (telegram_configured) no son Fields y quedan fuera.
_REF = re.compile(r"\bsettings\.([A-Z][A-Z0-9_]*)\b")


def test_every_settings_reference_in_src_is_a_declared_field():
    declared = set(Settings.model_fields)
    missing: dict[str, list[str]] = {}
    for py in SRC.rglob("*.py"):
        for lineno, line in enumerate(py.read_text().splitlines(), 1):
            for attr in _REF.findall(line):
                if attr not in declared:
                    missing.setdefault(attr, []).append(f"{py.relative_to(SRC)}:{lineno}")
    assert not missing, (
        "Referencias a Settings SIN campo declarado (flag a medio cablear → AttributeError "
        f"en producción): {missing}"
    )


def test_regression_motor2_fee_at_stake_count_field_exists():
    """El campo exacto que se perdió DOS veces (merge de #155): nunca más sin declaración."""
    assert "MOTOR_2_FEE_AT_STAKE_COUNT" in Settings.model_fields
    assert Settings.model_fields["MOTOR_2_FEE_AT_STAKE_COUNT"].default is False
