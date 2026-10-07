"""SIGKILL real y recuperación M5 en otro proceso, completamente offline.

El primer proceso muere durante un POST simulado aceptado pero sin respuesta.
El segundo sólo recibe observaciones ficticias: ausencia, fill parcial resting,
cancel regresivo y cancel terminal válido. Engine, executor, cliente REST,
conciliador, RiskManager y SQLite son los del candidato, sin reemplazar sus gates.
No prueba conexión demo, precios ejecutados reales ni un reinicio del host.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
TICKER = "KXMLBGAME-26OCT07PHINYM-PHI"
EVENT = "KXMLBGAME-26OCT07PHINYM"


def _write(path, value, *, append=False):
    """El marcador sobrevive al SIGKILL; no depende de buffers de Python."""
    target = path if append else path.with_suffix(path.suffix + ".writing")
    with target.open("a" if append else "x") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    if not append:
        # La existencia de ready significa contenido completo y fsync terminado,
        # nunca un archivo vacío observado entre open() y write().
        assert not path.exists()
        target.rename(path)


def _worker(mode: str, directory: Path):
    def forbidden(*args, **kwargs):
        raise AssertionError("proceso offline: red prohibida")

    def guarded_open(original):
        def guarded(file, *args, **kwargs):
            if isinstance(file, (str, Path)) and Path(file).name == ".env":
                raise AssertionError("proceso offline: dotenv prohibido")
            return original(file, *args, **kwargs)

        return guarded

    os.environ.clear()
    os.chdir(directory)
    with (
        patch("builtins.open", guarded_open(builtins.open)),
        patch("io.open", guarded_open(io.open)),
        patch.object(socket.socket, "connect", forbidden),
        patch.object(socket.socket, "connect_ex", forbidden),
        patch.object(socket, "create_connection", forbidden),
        patch.object(socket, "getaddrinfo", forbidden),
    ):
        # Importar dentro del perímetro cerrado, sin constructor de Settings ni PEM.
        import httpx
        from loguru import logger
        from sqlmodel import select

        import src.utils.config as config
        from src.clients.kalshi_rest import KalshiRestClient, _WriteThrottle
        from src.math.arbitrage import ArbLeg, ArbOpportunity
        from src.risk.manager import RiskManager
        from src.storage.models import OperationalState, Trade, get_session, init_db
        from src.strategies.fair_value_book import FairValueBook
        from src.strategies.motor_5_mm.engine import Motor5Engine
        from src.strategies.motor_5_mm.incidents import SESSION_KEY, incident_status

        logger.remove()
        settings = config.Settings.model_construct(
            DATABASE_URL=f"sqlite:///{directory / 'ledger.db'}",
            KALSHI_ENV="demo",
            TRADING_ENABLED=True,
            ACTIVE_CAPITAL_USD=1000,
            CAPITAL_CAP_USD=1000,
            CAPITAL_FLOOR_USD=100,
            CAPITAL_SAFETY_FACTOR_PCT=90,
            UNREALIZED_STOP_ENABLED=False,
            EXPERIMENT_BANK200_ENABLED=False,
        )
        config._settings = settings
        RiskManager._cached_capital_usd = 1000.0
        RiskManager._last_raw_balance_usd = 1000.0
        RiskManager._last_balance_at = datetime.now(UTC).replace(tzinfo=None)
        init_db()
        trace = directory / "transport.jsonl"
        phase = "create" if mode == "crash" else "absent"
        errors = []
        remote = None if mode == "crash" else json.loads((directory / "remote.json").read_text())

        def entry():
            return ArbOpportunity(
                legs=(ArbLeg("KXMLBGAME-OTHER-NYM", "yes", 45, 1, 1),),
                count=1,
                gross_profit_cents=0,
                fees_cents=0,
                net_profit_cents=0,
                edge_pct=0.0,
            )

        def snapshot():
            with get_session() as session:
                rows = list(session.exec(select(Trade)))
                assert len(rows) == 1
                row = rows[0]
                execution = session.get(OperationalState, SESSION_KEY)
                return {
                    "pid": os.getpid(),
                    "phase": phase,
                    "coid": row.client_order_id,
                    "order_id": row.kalshi_order_id,
                    "status": row.status,
                    "fill": row.filled_count_fp,
                    "reserve_usd": RiskManager()._get_current_exposure_usd(),
                    "session": execution.value,
                    "incident": incident_status()[0],
                }

        def observed():
            value = dict(remote)
            fill = "0.00" if phase == "regressive" else "0.25"
            value.update(
                status="resting" if phase == "partial" else "canceled",
                fill_count_fp=fill,
                remaining_count_fp="0.75" if phase == "partial" else "0.00",
                maker_fill_cost_dollars="0.112500",
                taker_fill_cost_dollars="0.000000",
                maker_fees_dollars="0.001250",
                taker_fees_dollars="0.000000",
            )
            return value

        async def handle(request):
            nonlocal remote
            path = request.url.path.removeprefix("/trade-api/v2")
            _write(
                trace,
                {"pid": os.getpid(), "method": request.method, "path": path, "phase": phase},
                append=True,
            )
            try:
                if request.method == "POST":
                    assert mode == "crash" and path == "/portfolio/events/orders"
                    body = json.loads(request.content)
                    before = snapshot()
                    before["entry_approved"] = (
                        await RiskManager().check_pre_trade(entry())
                    ).approved
                    assert before["status"] == "pending" and before["order_id"] is None
                    assert before["reserve_usd"] == pytest.approx(0.45)
                    assert before["entry_approved"]
                    assert body["side"] == "bid" and body["count"] == "1"
                    remote = {
                        "order_id": "offline-accepted",
                        "client_order_id": body["client_order_id"],
                        "ticker": TICKER,
                        "side": "yes",
                        "action": "buy",
                        "outcome_side": "yes",
                        "book_side": "bid",
                        "initial_count_fp": "1.00",
                        "exchange_index": 0,
                        "subaccount_number": 0,
                    }
                    _write(directory / "remote.json", remote)
                    _write(directory / "ready.json", before)
                    # El exchange ficticio aceptó; el ACK jamás llega. Sólo SIGKILL
                    # termina este await, sin ejecutar finally/cancel/reconciliación.
                    await asyncio.Event().wait()
                    raise AssertionError("el proceso destinado a SIGKILL continuó")
                if request.method == "GET" and path == "/portfolio/orders":
                    orders = [] if phase in {"create", "absent"} else [observed()]
                    return httpx.Response(200, json={"orders": orders, "cursor": ""})
                if request.method == "GET" and path == "/portfolio/orders/offline-accepted":
                    return httpx.Response(200, json={"order": observed()})
                if (
                    request.method == "DELETE"
                    and path == "/portfolio/events/orders/offline-accepted"
                ):
                    return httpx.Response(
                        200,
                        json={
                            "order_id": remote["order_id"],
                            "client_order_id": remote["client_order_id"],
                            "reduced_by": "0.75",
                        },
                    )
                if request.method == "GET" and path.endswith("/orderbook"):
                    return httpx.Response(
                        200, json={"orderbook": {"yes": [[40, 10]], "no": [[40, 10]]}}
                    )
                if request.method == "GET" and path == "/series/KXMLBGAME":
                    return httpx.Response(
                        200,
                        json={
                            "series": {
                                "fee_type": "quadratic_with_maker_fees",
                                "fee_multiplier": 0.5,
                            }
                        },
                    )
                if request.method == "GET" and path == f"/events/{EVENT}":
                    return httpx.Response(
                        200,
                        json={
                            "event": {
                                "event_ticker": EVENT,
                                "series_ticker": "KXMLBGAME",
                            }
                        },
                    )
                raise AssertionError(f"ruta offline no prevista: {request.method} {path}")
            except Exception as exc:
                # Los errores del fixture no se convierten en éxito por el manejo
                # conservador de excepciones del código real.
                errors.append(exc)
                raise

        class OfflineClient(KalshiRestClient):
            def __init__(self):
                self.settings = settings
                self.base_url = "https://offline.invalid/trade-api/v2"
                self.signer = SimpleNamespace(sign=lambda *args: {})
                self._client = None
                self._write_throttle = _WriteThrottle(rate_per_sec=10000, burst=10000)

            async def __aenter__(self):
                self._client = httpx.AsyncClient(
                    base_url=self.base_url,
                    transport=httpx.MockTransport(handle),
                    trust_env=False,
                    follow_redirects=False,
                )
                return self

        async def run():
            nonlocal phase
            stop = asyncio.Event()
            states = []
            sequence = iter(("partial", "regressive", "terminal", "replay"))

            class SteppedEngine(Motor5Engine):
                LOOP_INTERVAL_SEC = 0.001

                async def _tick(self):
                    nonlocal phase
                    await super()._tick()
                    if errors:
                        raise errors[0]
                    assert mode == "recover", "el POST sin ACK no debe terminar el primer tick"
                    state = snapshot()
                    decision = await RiskManager().check_pre_trade(entry())
                    state["entry_approved"] = decision.approved
                    state["entry_reason"] = decision.reason
                    states.append(state)
                    phase = next(sequence, "done")
                    if phase == "done":
                        stop.set()

            FairValueBook.publish({TICKER: 0.5}, event_tickers={TICKER: EVENT})
            engine = SteppedEngine(
                trading_enabled=True,
                risk_manager=RiskManager(),
                client_factory=OfflineClient,
                half_spread_cents=5,
                quote_size_contracts=1,
                series_csv="KXMLBGAME",
                mm_exposure_cap_usd=0.45,
                experiment_label="process-crash-offline",
            )
            await engine.run(stop)
            assert not errors
            assert len(states) == 5
            _write(directory / "recovered.json", {"states": states, "final": snapshot()})

        asyncio.run(run())


def _command(mode, directory):
    # -I elimina PYTHONPATH/user site; -B evita writes de bytecode al candidato.
    bootstrap = (
        "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
        "runpy.run_path(sys.argv[2],run_name='__main__')"
    )
    return [
        sys.executable,
        "-I",
        "-B",
        "-c",
        bootstrap,
        str(ROOT),
        str(Path(__file__).resolve()),
        mode,
        str(directory),
    ]


@pytest.mark.skipif(os.name != "posix", reason="el ensayo requiere SIGKILL de POSIX")
def test_sigkill_unknown_post_recovers_in_new_process_without_duplicate(tmp_path):
    """Prueba de frontera SO/SQLite: ni finally ni memoria del primer proceso sobreviven."""
    env = {"PATH": os.defpath}
    first = subprocess.Popen(
        _command("crash", tmp_path),
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 15
        ready = tmp_path / "ready.json"
        while not ready.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists() and first.poll() is None, "el worker no llegó al POST incierto"
        first.kill()  # SIGKILL real; no handlers ni finally del engine.
        _, stderr = first.communicate(timeout=5)
        assert first.returncode == -signal.SIGKILL, stderr.decode()
    finally:
        if first.poll() is None:
            first.kill()
        first.communicate(timeout=5)

    before = json.loads(ready.read_text())
    assert before["pid"] == first.pid and before["session"] == "active"
    assert before["fill"] is None and before["reserve_usd"] == pytest.approx(0.45)
    assert before["entry_approved"]
    with sqlite3.connect(tmp_path / "ledger.db") as db:
        assert db.execute("SELECT status, filled_count_fp FROM trades").fetchall() == [
            ("pending", None)
        ]
        assert db.execute(
            "SELECT value FROM operational_state WHERE key='mm_execution_session'"
        ).fetchone() == ("active",)

    recovered = subprocess.run(
        _command("recover", tmp_path),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert recovered.returncode == 0, recovered.stderr.decode()
    report = json.loads((tmp_path / "recovered.json").read_text())
    states = report["states"]
    assert [state["phase"] for state in states] == [
        "absent",
        "partial",
        "regressive",
        "terminal",
        "replay",
    ]
    assert all(state["pid"] != first.pid for state in states)
    assert len({state["pid"] for state in states}) == 1
    assert [state["fill"] for state in states] == [None, "0.25", "0.25", "0.25", "0.25"]
    assert [state["status"] for state in states] == ["pending"] * 3 + ["filled"] * 2
    # La reserva pending sigue íntegra; 0.25×45c terminal reserva ceil(11.25c)=12c.
    assert [state["reserve_usd"] for state in states] == pytest.approx([0.45] * 3 + [0.12] * 2)
    assert all(state["incident"] and not state["entry_approved"] for state in states)
    assert all(state["entry_reason"].startswith("Exposición M5 incierta:") for state in states)
    assert report["final"]["session"] == "clean" and report["final"]["incident"]
    assert {state["coid"] for state in states} == {before["coid"]}
    requests = [
        json.loads(line) for line in (tmp_path / "transport.jsonl").read_text().splitlines()
    ]
    assert [row["pid"] for row in requests if row["method"] == "POST"] == [first.pid]
    assert [row for row in requests if row["pid"] == first.pid][-1]["method"] == "POST"
    assert any(row["method"] == "DELETE" and row["phase"] == "partial" for row in requests)
    with sqlite3.connect(tmp_path / "ledger.db") as db:
        assert db.execute("SELECT COUNT(*) FROM trades").fetchone() == (1,)
        assert db.execute("SELECT status, filled_count_fp FROM trades").fetchone() == (
            "filled",
            "0.25",
        )


if __name__ == "__main__":
    _worker(sys.argv[3], Path(sys.argv[4]))
