"""QA local: entorno vacío, DB temporal y conexiones limitadas a loopback.

Ejecutar con el Python de desarrollo del proyecto. No inicia servicios operativos,
lee claves reales ni envía órdenes. Los tests WebSocket crean servidores locales.
Argumentos opcionales: rutas/nodos pytest relativos al checkout candidato.
"""

import ipaddress
import os
import socket
import sys
import tempfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
qa_tempdir = tempfile.gettempdir()
os.environ.clear()
os.environ.update(
    PATH="/usr/bin:/bin:/usr/sbin:/sbin",
    DATABASE_URL=f"sqlite:///{tempfile.mkdtemp(prefix='kalshi-qa-')}/test.db",
    TRADING_ENABLED="false",
    TMPDIR=qa_tempdir,
    PYTHONDONTWRITEBYTECODE="1",
)
sys.dont_write_bytecode = True
os.chdir(root)
sys.path.insert(0, str(root))


def allow_host(host):
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise RuntimeError("QA: conexión externa bloqueada")


original_socket = socket.socket
original_getaddrinfo = socket.getaddrinfo
original_connection = socket.create_connection


class LocalSocket(original_socket):
    def connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            allow_host(address[0])
        return super().connect(address)

    def connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            allow_host(address[0])
        return super().connect_ex(address)


def local_getaddrinfo(host, *args, **kwargs):
    allow_host(host)
    return original_getaddrinfo(host, *args, **kwargs)


def local_connection(address, *args, **kwargs):
    allow_host(address[0])
    return original_connection(address, *args, **kwargs)


socket.socket = LocalSocket
socket.getaddrinfo = local_getaddrinfo
socket.create_connection = local_connection

from src.utils.config import Settings  # noqa: E402 -- aislamiento antes de importar la app

Settings.model_config["env_file"] = None

import pytest  # noqa: E402

raise SystemExit(
    pytest.main(
        [
            "-q",
            "-o",
            "addopts=",
            "--tb=short",
            "--import-mode=importlib",
            "-p",
            "no:cacheprovider",
            *(
                sys.argv[1:]
                or [
                    "tests",
                    "infra/digitalocean-shadow/tests",
                    "infra/digitalocean-shadow/test_collector.py",
                ]
            ),
        ]
    )
)
