"""Run the entire isolated module suite and a fresh demo with sockets denied."""

import json
import socket
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch


def denied(*args, **kwargs):
    raise AssertionError("NETWORK_FORBIDDEN_IN_OFFLINE_VERIFICATION")


def main():
    root = Path(__file__).resolve().parent
    with ExitStack() as guards:
        for target in (
            "socket.create_connection",
            "socket.getaddrinfo",
            "socket.gethostbyname",
            "socket.gethostbyname_ex",
            "socket.gethostbyaddr",
        ):
            guards.enter_context(patch(target, denied))
        for method in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg"):
            if hasattr(socket.socket, method):
                guards.enter_context(patch.object(socket.socket, method, denied))
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            return 1
        from run_control_demo import run as run_control
        from run_demo import run

        with tempfile.TemporaryDirectory(prefix="us500-offline-") as folder:
            summary = run(Path(folder) / "smoke")
            control_summary = run_control(Path(folder) / "controls")
        print(
            json.dumps(
                {
                    "tests_run": result.testsRun,
                    "network_denied": True,
                    "restart_smoke": summary,
                    "control_smoke": control_summary,
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
