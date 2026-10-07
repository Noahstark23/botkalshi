"""Replay SINTÉTICO Odds→M2→M5, con y sin filtro inicial Jev; nunca usa APIs.

python -m scripts.jev_ecosystem_replay --cases casos.json \
    --judgments juicios.json --output resultado_nuevo.json

Ejecutar en un proceso dedicado. Vacía el entorno antes de importar el bot,
desactiva dotenv, bloquea sockets y constructores de clientes reales, y usa una
SQLite temporal por brazo. No ejecuta run(), executors ni settlement real.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import socket
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MAX_BYTES = 512 * 1024
MAX_SCENARIOS = 24
MAX_TICKS = 32


def _read_json(path: Path) -> tuple[dict, str]:
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("archivo excede presupuesto")
    value = json.loads(raw)
    if type(value) is not dict:
        raise ValueError("se requiere objeto JSON")
    return value, hashlib.sha256(raw).hexdigest()


def load_inputs(cases: Path, judgments: Path) -> tuple[dict, dict, str, str]:
    # Este módulo no importa el bot; tampoco llama al cliente Jev que define su CLI.
    from scripts.jev_ecosystem_judgments import INSTRUCTIONS, JEV_MODEL, decision

    fixture, fixture_hash = _read_json(cases)
    report, judgments_hash = _read_json(judgments)
    if fixture.get("schema") != "jev_ecosystem_v1":
        raise ValueError("schema de escenarios desconocido")
    if report.get("schema") != "jev_ecosystem_judgments_v1":
        raise ValueError("schema de juicios desconocido")
    if report.get("fixture_sha256") != fixture_hash:
        raise ValueError("fixture_sha256 no coincide")
    if report.get("prompt_sha256") != hashlib.sha256(INSTRUCTIONS.encode()).hexdigest():
        raise ValueError("prompt_sha256 no coincide")
    if report.get("model") != JEV_MODEL or report.get("repeats") != 2:
        raise ValueError("modelo o repeticiones no coinciden")
    news = fixture.get("news")
    rows = report.get("news_results")
    if type(news) is not dict or type(rows) is not dict or set(news) != set(rows):
        raise ValueError("noticias y juicios no coinciden")
    for row in rows.values():
        if type(row) is not dict or type(row.get("probabilities")) is not list:
            raise ValueError("probabilidades inválidas")
        probabilities = row["probabilities"]
        if len(probabilities) > 2 or any(
            type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
            for p in probabilities
        ):
            raise ValueError("probabilidades inválidas")
        if row.get("decision") != decision(probabilities):
            raise ValueError("decisión no coincide con probabilidades")
    scenarios = fixture.get("scenarios")
    if type(scenarios) is not list or not 1 <= len(scenarios) <= MAX_SCENARIOS:
        raise ValueError("cantidad de escenarios inválida")
    identifiers = set()
    for scenario in scenarios:
        if type(scenario) is not dict or type(scenario.get("id")) is not str:
            raise ValueError("escenario inválido")
        if not scenario["id"] or scenario["id"] in identifiers:
            raise ValueError("id de escenario vacío o duplicado")
        identifiers.add(scenario["id"])
        if scenario.get("news_id") not in news:
            raise ValueError("noticia de escenario desconocida")
        ticks = scenario.get("ticks")
        if type(ticks) is not list or not 1 <= len(ticks) <= MAX_TICKS:
            raise ValueError("cantidad de ticks inválida")
        previous = -1
        for tick in ticks:
            if type(tick) is not dict:
                raise ValueError("tick inválido")
            offset = tick.get("offset_sec")
            if type(offset) is not int or not previous < offset <= 3600:
                raise ValueError("reloj debe avanzar, con horizonte máximo de una hora")
            previous = offset
            fair = tick.get("fair_prob", 0.5)
            if type(fair) not in (int, float) or not math.isfinite(fair) or not 0 < fair < 1:
                raise ValueError("fair sintético inválido")
            bid, ask = tick.get("yes_bid"), tick.get("yes_ask")
            if not (
                bid is None
                and ask is None
                or type(bid) is int
                and type(ask) is int
                and 0 <= bid < ask <= 100
            ):
                raise ValueError("BBO debe ser bilateral válido o completamente ausente")
            try:
                depth = Decimal(str(tick.get("depth", "100")))
            except InvalidOperation as exc:
                raise ValueError("profundidad inválida") from exc
            if not depth.is_finite() or not 0 < depth <= 1_000_000:
                raise ValueError("profundidad inválida")
        if ticks[0]["offset_sec"] != 0:
            raise ValueError("primer tick debe tener offset cero")
    return fixture, report, fixture_hash, judgments_hash


def _deny(*args, **kwargs):
    raise RuntimeError("REPLAY_OFFLINE: red y clientes reales prohibidos")


@contextmanager
def offline_process():
    """Defensa previa a imports; asyncio usa socketpair local sin connect/sendto."""
    sys.dont_write_bytecode = True
    os.environ.clear()
    os.environ.update(TRADING_ENABLED="false", PYTHONDONTWRITEBYTECODE="1")
    with ExitStack() as stack:
        for method in ("connect", "connect_ex", "sendto"):
            stack.enter_context(patch.object(socket.socket, method, _deny))
        if hasattr(socket.socket, "sendmsg"):
            stack.enter_context(patch.object(socket.socket, "sendmsg", _deny))
        stack.enter_context(patch.object(socket, "create_connection", _deny))
        stack.enter_context(patch.object(socket, "getaddrinfo", _deny))
        # Importar config no instancia Settings. Desactivar .env ANTES de get_settings.
        import src.utils.config as config

        stack.enter_context(patch.dict(config.Settings.model_config, {"env_file": None}))
        settings = config.Settings.model_construct(
            KALSHI_API_KEY_ID="offline-placeholder",
            KALSHI_PRIVATE_KEY_PATH=Path("/nonexistent/jev-offline-key"),
            TRADING_ENABLED=False,
            DATABASE_URL="sqlite:///:memory:",
        )
        stack.enter_context(patch.object(config, "get_settings", lambda: settings))
        from src.clients.kalshi_rest import KalshiRestClient
        from src.clients.odds_api import OddsAPIClient

        stack.enter_context(patch.object(KalshiRestClient, "__init__", _deny))
        stack.enter_context(patch.object(OddsAPIClient, "__init__", _deny))
        yield


class FakeReadClient:
    """Sólo tres lecturas ficticias; no tiene métodos de órdenes ni credenciales."""

    def __init__(self, ticker: str, event: str):
        self.ticker, self.event = ticker, event
        self.frame: dict = {}
        self.calls = {"get_orderbook": 0, "get_series": 0, "get_event": 0}

    async def get_orderbook(self, ticker: str) -> dict:
        if ticker != self.ticker:
            raise ValueError("ticker fuera del replay")
        self.calls["get_orderbook"] += 1
        bid, ask = self.frame["yes_bid"], self.frame["yes_ask"]
        depth = self.frame.get("depth", "100")
        return {
            "orderbook": {
                "yes": [[bid, depth]] if bid is not None else [],
                "no": [[100 - ask, depth]] if ask is not None else [],
            }
        }

    async def get_series(self, series: str) -> dict:
        if series != "KXMLBGAME":
            raise ValueError("serie fuera del replay")
        self.calls["get_series"] += 1
        return {
            "series": {
                "ticker": series,
                "fee_type": "quadratic_with_maker_fees",
                "fee_multiplier": 0.5,
            }
        }

    async def get_event(self, event: str) -> dict:
        if event != self.event:
            raise ValueError("evento fuera del replay")
        self.calls["get_event"] += 1
        return {
            "event": {
                "event_ticker": event,
                "series_ticker": "KXMLBGAME",
                "fee_type_override": None,
                "fee_multiplier_override": None,
            }
        }


async def replay_arm(scenario: dict, *, admitted: bool, arm: str, directory: Path) -> dict:
    from sqlmodel import SQLModel, create_engine, select

    import src.storage.models as models
    import src.strategies.motor_5_mm.engine as engine_module
    import src.strategies.motor_5_mm.fee_policy as fee_module
    from scripts.jev_ecosystem_inputs import BASE_TIME, EVENT_TICKER, TARGET_TICKER, compute_fairs
    from src.strategies.fair_value_book import FairValueBook

    result = {
        "admitted_before_first_quote": admitted,
        "quotes": [],
        "fills": [],
        "ticks": [],
        "inventory_contracts": 0,
        "cash_cents": 0,
        "fees_cents": 0,
        "mark_cents": None,
        "mtm_cents": 0,
        "realized_cents": 0,
        "epoch_status": "not_started",
        "valid": True,
        "missing_book": False,
        "fake_read_calls": {},
        "pending_markouts": 0,
    }
    if not admitted:
        return result
    # Un archivo nuevo por brazo impide contaminación de ledger y cohortes.
    database = create_engine(f"sqlite:///{directory}/arm.db")
    SQLModel.metadata.create_all(database)
    clock = SimpleNamespace(now=BASE_TIME, seconds=0)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now.astimezone(tz) if tz else clock.now.replace(tzinfo=None)

    fake_time = SimpleNamespace(monotonic=lambda: float(clock.seconds))
    client = FakeReadClient(TARGET_TICKER, EVENT_TICKER)
    FairValueBook.clear()
    try:
        with (
            patch.object(models, "_engine", database),
            # Los defaults del ledger deben compartir el reloj del motor y sus marks.
            patch.object(models, "datetime", FrozenDatetime),
            patch.object(engine_module, "datetime", FrozenDatetime),
            patch.object(engine_module, "time", fake_time),
            patch.object(fee_module, "datetime", FrozenDatetime),
            patch.object(fee_module, "time", fake_time),
        ):
            engine = engine_module.Motor5Engine(
                max_tickers=1,
                series_csv="KXMLBGAME",
                experiment_label=f"replay-{arm}",
                half_spread_cents=5,
                quote_size_contracts=1,
                max_inventory_contracts=50,
                fair_ttl_sec=600,
                require_pregame=True,
                kickoff_buffer_sec=120,
                trading_enabled=False,
                fees_as_maker=True,
                jump_retreat_cents=5,
                # Metadata exigida por F1 maker; settings y ejecución siguen apagados.
                exchange_environment="production",
                fair_min_books=3,
                fair_max_book_age_min=15,
                fair_odds_regions="synthetic",
                fair_sport_keys_config="baseball_mlb",
                fair_cache_ttl_sec=60,
            )
            engine._client = client
            engine._fee_policy = fee_module.SeriesFeePolicy(client)
            assert engine._executor is None and engine._reconciler is None
            engine._begin_experiment_run()
            quote_offsets: dict[int, int] = {}
            for frame in scenario["ticks"]:
                clock.seconds = frame["offset_sec"]
                clock.now = BASE_TIME + timedelta(seconds=clock.seconds)
                client.frame = frame
                # Lista positiva: M2 recibe el presente, jamás los ticks futuros/etiquetas.
                m2 = compute_fairs(
                    {
                        "id": scenario["id"],
                        "odds_mode": scenario["odds_mode"],
                        "fair_prob": frame.get("fair_prob", 0.5),
                        "yes_bid": frame["yes_bid"],
                        "yes_ask": frame["yes_ask"],
                    },
                    clock.now,
                )
                selected = (
                    {TARGET_TICKER: m2["fairs"][TARGET_TICKER]}
                    if (TARGET_TICKER in m2["fairs"])
                    else {}
                )
                FairValueBook.publish(
                    selected,
                    now=clock.now,
                    commence_times=m2["commence_times"],
                    event_tickers=m2["event_tickers"],
                    provenances=m2["provenances"],
                )
                await engine._tick()
                if frame["yes_bid"] is None:
                    result["missing_book"] = True
                with models.get_session() as session:
                    for row in session.exec(select(models.MMQuote)):
                        quote_offsets.setdefault(row.id, clock.seconds)
                result["ticks"].append(
                    {
                        "offset_sec": clock.seconds,
                        "observed_at": clock.now.isoformat(),
                        "m2_fairs": selected,
                        "m2_signal_count": m2["signal_count"],
                        "live_quote": (
                            {"bid_cents": q.bid_cents, "ask_cents": q.ask_cents, "size": q.size}
                            if (q := engine._live_quotes.get(TARGET_TICKER))
                            else None
                        ),
                        "inventory_contracts": int(engine._inventory.net(TARGET_TICKER)),
                        "mark_cents": engine._last_marks.get(TARGET_TICKER),
                    }
                )
            # Misma última foto: observa el último intervalo, no inventa una salida.
            await engine._finalize_shadow_exposure()
            engine._close_experiment_run_clean()
            with models.get_session() as session:
                result["quotes"] = [
                    dict(row.model_dump(mode="json"), replay_offset_sec=quote_offsets.get(row.id))
                    for row in session.exec(select(models.MMQuote))
                ]
                result["fills"] = [
                    row.model_dump(mode="json") for row in session.exec(select(models.MMShadowFill))
                ]
                epoch = session.get(models.MMExperimentRun, engine._experiment_run_id)
                result["epoch_status"] = epoch.status
                result["epoch_reason"] = epoch.reason
            position = engine._inventory.positions.get(TARGET_TICKER)
            if position:
                result.update(
                    inventory_contracts=int(position.net_contracts),
                    cash_cents=int(position.cash_cents),
                    fees_cents=position.fees_cents,
                )
            mark = engine._last_marks.get(TARGET_TICKER)
            result["mark_cents"] = mark
            result["valid"] = result["epoch_status"] == "clean" and not result["missing_book"]
            flat = result["inventory_contracts"] == 0
            result["realized_cents"] = (
                result["cash_cents"] - result["fees_cents"] if flat and result["valid"] else None
            )
            # No usar el fallback neutral50 de total_mtm_cents si no hay mark observable.
            result["mtm_cents"] = (
                result["realized_cents"]
                if flat
                else position.mtm_cents(mark)
                if mark is not None and result["valid"]
                else None
            )
            result["fake_read_calls"] = client.calls
            result["pending_markouts"] = len(engine._markouts_pendientes)
            assert engine._executor is None and engine._reconciler is None
            assert not engine._live_quotes
    finally:
        FairValueBook.clear()
        database.dispose()
    return result


def run_replay(cases: Path, judgments: Path, output: Path) -> dict:
    if output.exists():
        raise FileExistsError("output ya existe; no se sobrescribe")
    cases, judgments, output = cases.resolve(), judgments.resolve(), output.absolute()
    with offline_process():
        fixture, decisions, fixture_hash, judgments_hash = load_inputs(cases, judgments)
        report = {
            "schema": "jev_ecosystem_replay_v1",
            "data_kind": "synthetic_not_historical",
            "fixture_sha256": fixture_hash,
            "judgments_sha256": judgments_hash,
            "model": decisions["model"],
            "prompt_sha256": decisions["prompt_sha256"],
            "worker_api_calls": 0,
            "financial_orders": 0,
            "mechanism": "real M2 detector, M5 Engine._tick, SeriesFeePolicy, shadow ledger",
            "limits": [
                "Fills son inferencias sintéticas por cruce estricto con depth cap1; sin cola real.",
                "MTM abierto no descuenta fee de salida; realizado sólo cuando net=0 y cohorte válida.",
                "No mide rentabilidad: caminos elegidos a mano, sin latencia ni cuenta de mercado.",
                "Jev sólo aporta filtro inicial; el worker no autentica ni vuelve a consultar juicios.",
                "production en ledger es metadata exigida por maker F1, nunca conexión a producción.",
                "No ejecuta daemon, reconciler ni settlement; DB temporal destruida tras exportar.",
            ],
            "scenarios": [],
        }
        with tempfile.TemporaryDirectory(prefix="jev-ecosystem-offline-") as temporary:
            for index, scenario in enumerate(fixture["scenarios"]):
                gate = decisions["news_results"][scenario["news_id"]]["decision"]
                row = {"id": scenario["id"], "news_id": scenario["news_id"], "jev_decision": gate}
                for arm, admitted in (("baseline", True), ("jev", gate == "ALLOW")):
                    directory = Path(temporary) / f"{index}-{arm}"
                    directory.mkdir()
                    row[arm] = asyncio.run(
                        replay_arm(
                            scenario,
                            admitted=admitted,
                            arm=arm,
                            directory=directory,
                        )
                    )
                report["scenarios"].append(row)
        encoded = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with output.open("x") as stream:
            stream.write(encoded)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_replay(args.cases, args.judgments, args.output)
    print(
        json.dumps(
            {
                "scenarios": len(result["scenarios"]),
                "worker_api_calls": 0,
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()
