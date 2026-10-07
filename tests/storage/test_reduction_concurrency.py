"""Dos workers no pueden reservar/vender dos veces el mismo BUY atribuible."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from fractions import Fraction
from unittest.mock import Mock

import pytest
from sqlmodel import create_engine, select

import src.storage.models as models
import src.storage.reductions as reductions
from src.storage.quantities import filled_fields, open_quantity, requested_quantity


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path}/concurrent.db",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    models.SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(models, "_engine", engine)
    deny = Mock(side_effect=AssertionError("prueba offline: red prohibida"))
    monkeypatch.setattr("socket.socket.connect", deny)
    monkeypatch.setattr("socket.socket.connect_ex", deny)
    monkeypatch.setattr("socket.create_connection", deny)
    with models.get_session() as session:
        session.add(
            models.Trade(
                client_order_id="buy",
                ticker="T",
                side="yes",
                action="buy",
                strategy="motor_2_consensus",
                price_cents=50,
                fill_price_cents=50,
                fees_cents=2,
                status="filled",
                **filled_fields(Fraction(1)),
            )
        )
        session.commit()
    yield
    engine.dispose()


def race_at_read(monkeypatch, first_call, second_call, *, read_method, query_fragment):
    """Fuerza al segundo worker a disputar el lock durante la lectura del primero.

    Con BEGIN IMMEDIATE espera antes de leer. Sin esa serialización ambos leen
    el estado previo: el reproducer también falla si se elimina sólo el BEGIN.
    No se usan sleeps ni se obliga al segundo a adquirir un lock ya retenido.
    """
    original_session = models.get_session
    first_read = threading.Event()
    second_attempt = threading.Event()
    local = threading.local()

    class ReadResult:
        def __init__(self, result):
            self.result = result

        def __getattr__(self, name):
            attr = getattr(self.result, name)
            if name != read_method:
                return attr

            def read(*args, **kwargs):
                value = attr(*args, **kwargs)
                if local.role == "first":
                    first_read.set()
                    assert second_attempt.wait(3), "el segundo worker no intentó entrar"
                else:
                    second_attempt.set()
                return value

            return read

    class SessionProxy:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        def exec(self, statement, *args, **kwargs):
            sql = str(statement)
            if local.role == "second" and sql.strip().upper() == "BEGIN IMMEDIATE":
                second_attempt.set()
            result = self.session.exec(statement, *args, **kwargs)
            return ReadResult(result) if query_fragment in sql else result

    @contextmanager
    def instrumented_session():
        with original_session() as session:
            yield SessionProxy(session)

    monkeypatch.setattr(reductions, "get_session", instrumented_session)

    def run(role, call):
        local.role = role
        try:
            call()
            return "ok"
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run, "first", first_call)
        assert first_read.wait(3), "el primer worker no alcanzó la lectura"
        second = pool.submit(run, "second", second_call)
        return first.result(timeout=8), second.result(timeout=8)


def test_concurrent_reductions_reserve_original_once(isolated_ledger, monkeypatch):
    outcomes = race_at_read(
        monkeypatch,
        lambda: reductions.persist_reduction("buy", "sell-a", Fraction(1), 50),
        lambda: reductions.persist_reduction("buy", "sell-b", Fraction(1), 50),
        read_method="first",
        query_fragment="trades.status =",
    )
    assert outcomes == ("ok", "rollback anterior sin resolver; prohibido revender")
    with models.get_session() as session:
        pending = list(session.exec(select(models.Trade).where(models.Trade.status == "pending")))
        original = session.exec(
            select(models.Trade).where(models.Trade.client_order_id == "buy")
        ).one()
        assert len(pending) == 1
        assert requested_quantity(pending[0]) == open_quantity(original) == 1
        assert pending[0].notes == "reduction_for=buy"


def test_concurrent_finalize_replay_splits_original_once(isolated_ledger, monkeypatch):
    reductions.persist_reduction("buy", "sell", Fraction(1, 4), 50)
    outcomes = race_at_read(
        monkeypatch,
        lambda: reductions.finalize_reduction("sell", Fraction(1, 4), "order-sell"),
        lambda: reductions.finalize_reduction("sell", Fraction(1, 4), "order-sell"),
        read_method="one",
        query_fragment="trades.client_order_id =",
    )
    assert outcomes == ("ok", "ok")
    with models.get_session() as session:
        rows = list(session.exec(select(models.Trade)))
        original = next(row for row in rows if row.client_order_id == "buy")
        closed_buys = [row for row in rows if row.action == "buy" and row.status == "settled"]
        sells = [row for row in rows if row.action == "sell"]
        assert open_quantity(original) == Fraction(3, 4)
        assert len(closed_buys) == len(sells) == 1
        assert closed_buys[0].filled_count_fp == sells[0].filled_count_fp == "0.25"
        assert sells[0].status == "settled"
        assert sells[0].kalshi_order_id == "order-sell"
