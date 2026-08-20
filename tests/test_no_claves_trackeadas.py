"""
Guard de secretos: NINGÚN archivo trackeado puede ser (ni contener) una clave privada.

POR QUÉ EXISTE (incidente 2026-08-19): config/kalshi_private_key.pem estuvo VERSIONADO
102 días en un repo público — el commit 0706226 borró las líneas *.pem del .gitignore a
propósito para embeber la clave en la imagen ("easier deployment") y nadie lo vio hasta
una auditoría externa. La clave de la cuenta de dinero real tuvo que revocarse. Un
.gitignore se puede volver a editar; este test convierte la recurrencia en CI ROJO.

Corre sobre `git ls-files` (la verdad de lo trackeado, no del working tree). Si git no
está disponible (p.ej. dentro de la imagen, que no lleva .git), el test se SALTA: el
guard vive donde importa — el CI de cada PR, que siempre tiene checkout con .git.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Armado por concatenación para que ESTE archivo no se auto-detecte como clave.
_MARCADORES = tuple(
    ("-----BEGIN " + tipo + " KEY-----").encode()
    for tipo in ("RSA PRIVATE", "PRIVATE", "EC PRIVATE", "OPENSSH PRIVATE", "DSA PRIVATE")
)
# Extensiones que jamás deberían estar trackeadas (espejo del bloque CRITICAL del .gitignore).
_EXTENSIONES_PROHIBIDAS = (".pem", ".key", ".p12")


def _archivos_trackeados() -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pytest.skip("git no disponible (imagen sin .git) — el guard corre en CI")
    return out.stdout.splitlines()


def test_ningun_archivo_de_clave_trackeado():
    """Ni .pem ni .key ni .p12 en el índice de git — la extensión sola ya es un incidente."""
    prohibidos = [f for f in _archivos_trackeados() if f.lower().endswith(_EXTENSIONES_PROHIBIDAS)]
    assert prohibidos == [], (
        f"Archivos de clave TRACKEADOS: {prohibidos}. Sacarlos del índice (git rm --cached), "
        "rotar la clave (se presume comprometida) y verificar el .gitignore. "
        "Ver incidente 2026-08-19 en el docstring."
    )


def test_ningun_contenido_de_clave_privada_trackeado():
    """El contenido manda sobre la extensión: una clave renombrada a .txt sigue siendo clave."""
    infractores: list[str] = []
    for nombre in _archivos_trackeados():
        ruta = REPO_ROOT / nombre
        # El working tree puede no tener el archivo (borrado local); el índice es la verdad,
        # pero leer el blob por git cat-file por archivo es caro — el checkout de CI está
        # siempre limpio, así que leer el working tree es equivalente donde el guard corre.
        if not ruta.is_file() or ruta.stat().st_size > 1_000_000:
            continue
        try:
            contenido = ruta.read_bytes()
        except OSError:
            continue
        if any(m in contenido for m in _MARCADORES):
            infractores.append(nombre)
    assert infractores == [], (
        f"Contenido de clave privada en archivos trackeados: {infractores}. "
        "Rotar la clave YA (se presume comprometida) y sacar el archivo del índice."
    )
