"""
Las dependencias de runtime existen en DOS listas — `pyproject.toml` (lo que instala CI) y
el `Dockerfile` (lo que instala el build de PRODUCCIÓN, que no lee pyproject). Este test
exige que digan exactamente lo mismo.

POR QUÉ EXISTE (2026-09-22): `sqlmodel>=0.0.16` sin techo en las dos listas. Desde la 0.0.45
sqlmodel rechaza datetimes naive ("Datetime values must have timezone information"), y el
repo guarda `settled_at`/`close_time` NAIVE UTC por convención. El CI de #251 pasó de verde
a 149 fallos sin un solo cambio de código (reproducido: 0.0.44 → 1.685 passed; 0.0.45 y
0.0.46 → 149 failed + 3 errors). El techo tuvo que ir en los dos archivos — si alguien lo
corrige en uno solo, CI queda verde y producción se rompe igual en el próximo build. Una
lista duplicada sin guard es exactamente cómo se abre esa brecha.

Correr sobre texto (sin red, sin instalar nada): tomllib + regex del bloque `pip install`.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _name(spec: str) -> str:
    return re.split(r"[<>=!~\[ ;]", spec, maxsplit=1)[0].lower()


def _pyproject_deps() -> dict[str, str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    return {_name(s): s for s in deps}


def _dockerfile_deps() -> dict[str, str]:
    text = (ROOT / "Dockerfile").read_text()
    start = text.index("pip install --no-cache-dir \\")
    block = text[start:]
    # El bloque termina en la primera línea que no continúa con backslash.
    lines = []
    for line in block.splitlines():
        lines.append(line)
        if not line.rstrip().endswith("\\"):
            break
    return {_name(s): s for s in re.findall(r'"([^"]+)"', "\n".join(lines))}


def test_dockerfile_y_pyproject_declaran_las_mismas_dependencias():
    py, dk = _pyproject_deps(), _dockerfile_deps()
    assert set(py) == set(dk), {
        "solo en pyproject": sorted(set(py) - set(dk)),
        "solo en Dockerfile": sorted(set(dk) - set(py)),
    }
    distintas = {k: (py[k], dk[k]) for k in py if py[k] != dk[k]}
    assert distintas == {}, (
        f"pyproject y Dockerfile difieren: {distintas}. CI instala una cosa y producción otra."
    )


def test_sqlmodel_tiene_techo_por_debajo_de_0_0_45():
    """El techo concreto del incidente, en los DOS archivos."""
    for origen, deps in (("pyproject", _pyproject_deps()), ("Dockerfile", _dockerfile_deps())):
        spec = deps["sqlmodel"]
        assert "<0.0.45" in spec.replace(" ", ""), (
            f"{origen}: {spec!r} sin techo — sqlmodel ≥0.0.45 rechaza los datetimes naive "
            "que el repo guarda por convención"
        )
