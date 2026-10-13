"""Recibos con SQLite temporal real y páginas ficticias; ninguna red/credencial."""

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from src.control.financial_receipts import FinancialReceiptStore, SourceBinding
from src.control.permissions import Admission, _Meta, _Model
from src.storage.financial_fence import (
    MAX_SEQUENCE,
    TRACKED,
    TRIGGERS,
    accept_reviewed_schema,
    digest,
    read_fence,
)
from src.storage.models import OperationalState, PortfolioPosition, RiskCommitment, Trade

NOW = 1791900000000
URL = "https://demo-api.kalshi.co/trade-api/v2"
KEY_ID = "fictitious-key-id"


class Clock:
    wall = NOW
    mono = 10000


class Client:
    base_url = URL
    _client = SimpleNamespace(base_url=URL)
    settings = SimpleNamespace(KALSHI_ENV="demo", KALSHI_API_KEY_ID=KEY_ID)
    signer = SimpleNamespace(api_key_id=KEY_ID)

    def __init__(self, positions=None, orders=None):
        self.positions = positions if positions is not None else [{"positions": [], "cursor": ""}]
        self.orders = orders if orders is not None else [{"orders": [], "cursor": ""}]
        self.calls = []

    async def get_positions(self, **kwargs):
        self.calls.append(("positions", kwargs))
        return self.positions.pop(0)

    async def get_orders(self, **kwargs):
        self.calls.append(("orders", kwargs))
        return self.orders.pop(0)


def order(**overrides):
    return dict(
        ticker="KXTEST",
        order_id="O1",
        client_order_id="m5mm-test",
        side="yes",
        action="buy",
        status="resting",
        initial_count_fp="1.00",
        fill_count_fp="0.25",
        remaining_count_fp="0.75",
        yes_price_dollars="0.4500",
        **overrides,
    )


@pytest.fixture
def setup(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    SQLModel.metadata.create_all(engine)
    _Model.metadata.create_all(engine)
    clock = Clock()
    binding = SourceBinding(
        "demo", URL, hashlib.sha256(KEY_ID.encode()).hexdigest(), "a" * 64, 3, "b" * 40, "c" * 32
    )
    store = FinancialReceiptStore(
        engine, binding, wall=lambda: clock.wall, monotonic=lambda: clock.mono
    )
    store.provision(epoch="d" * 32)
    yield store, clock
    engine.dispose()


def read(store):
    with Session(store.engine) as s:
        s.exec(text("BEGIN IMMEDIATE"))
        return store.read(s)


def execute(store, sql):
    with store.engine.begin() as conn:
        conn.exec_driver_sql(sql)


def fence(store):
    with Session(store.engine) as s:
        s.exec(text("BEGIN"))
        return read_fence(s.connection().connection.driver_connection)


async def test_complete_both_sources_same_receipt_and_original_times(setup):
    store, clock = setup
    client = Client(
        [
            {"market_positions": [{"ticker": "P", "position_fp": "-1.25"}], "cursor": "A"},
            {"positions": [], "cursor": ""},
        ],
        [{"orders": [order()], "cursor": ""}],
    )
    result = await store.collect(client)
    clock.wall += 5000
    clock.mono += 5000
    assert read(store) == result
    assert result["started_at_ms"] == NOW
    assert result["remote_atomic"] is result["account_identity_verified"] is False
    assert result["historical_orders_complete"] is False
    assert result["collections"]["positions"]["rows"] == [{"ticker": "P", "position_fp": "-1.25"}]
    assert result["collections"]["orders"]["rows"][0]["fill_count_fp"] == "0.25"
    assert all(kwargs["subaccount"] == 3 for _, kwargs in client.calls)
    assert client.calls[1][1]["cursor"] == "A"
    assert len(result["collections"]["positions"]["pages"]) == 2
    assert fence(store).sequence == 0  # caché/recibo no simulan escrituras financieras


@pytest.mark.parametrize(
    "page,code",
    [
        ({"positions": []}, "CURSOR_INVALID"),
        ({"positions": [], "cursor": None}, "CURSOR_INVALID"),
        ({"positions": [], "cursor": " "}, "CURSOR_INVALID"),
        ({"positions": [], "cursor": "x" * 4097}, "CURSOR_INVALID"),
        ({"positions": None, "cursor": ""}, "PAGE_INVALID"),
        ({"positions": [{"ticker": "P", "position_fp": "NaN"}], "cursor": ""}, "QUANTITY_INVALID"),
        ({"positions": [{"ticker": " P", "position_fp": "1.00"}], "cursor": ""}, "ROW_INVALID"),
        ({"market_positions": [], "positions": [{}], "cursor": ""}, "LIST_CONTRADICTION"),
        (
            {
                "positions": [{"ticker": "P", "position_fp": "1.00", "subaccount_number": 0}],
                "cursor": "",
            },
            "SUBACCOUNT_MISMATCH",
        ),
        (
            {"positions": [{"ticker": "P", "position_fp": "1.00"}] * 101, "cursor": ""},
            "PAGE_INVALID",
        ),
    ],
)
async def test_failed_collection_invalidates_old_receipt_without_changing_ledger(setup, page, code):
    store, _ = setup
    await store.collect(Client())
    before = fence(store)
    with pytest.raises(ValueError, match=code):
        await store.collect(Client([page]))
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        read(store)
    assert fence(store) == before


@pytest.mark.parametrize(
    "change,code",
    [
        ({"fill_count_fp": "1.01"}, "QUANTITY_CONTRADICTION"),
        ({"remaining_count_fp": "0.00"}, "ORDER_CONTRADICTION"),
        ({"status": "executed"}, "ORDER_CONTRADICTION"),
        ({"status": "unknown"}, "ORDER_INVALID"),
        ({"order_id": ""}, "ORDER_INVALID"),
        ({"client_order_id": None}, "ORDER_INVALID"),
        ({"yes_price": 46}, "PRICE_CONTRADICTION"),
        ({"yes_price_dollars": "NaN"}, "PRICE_INVALID"),
        ({"yes_price_dollars": "0.00001"}, "PRICE_INVALID"),
        ({"subaccount_number": True}, "SUBACCOUNT_MISMATCH"),
    ],
)
async def test_invalid_order_never_publishes(setup, change, code):
    store, _ = setup
    value = order()
    value.update(change)
    with pytest.raises(ValueError, match=code):
        await store.collect(Client(orders=[{"orders": [value], "cursor": ""}]))
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        read(store)


@pytest.mark.parametrize("kind", ["positions", "orders"])
async def test_duplicate_identity_across_pages(setup, kind):
    store, _ = setup
    row = {"ticker": "P", "position_fp": "1.00"} if kind == "positions" else order()
    pages = [{kind: [row], "cursor": "A"}, {kind: [row], "cursor": ""}]
    with pytest.raises(ValueError, match="DUPLICATE_IDENTITY"):
        await store.collect(Client(**{kind: pages}))


async def test_cursor_cycle_and_page_budget_are_global(setup, monkeypatch):
    store, _ = setup
    with pytest.raises(ValueError, match="CURSOR_CYCLE"):
        await store.collect(Client([{"positions": [], "cursor": "A"}] * 2))
    monkeypatch.setattr("src.control.financial_receipts.MAX_PAGES", 1)
    client = Client()
    with pytest.raises(ValueError, match="PAGE_LIMIT"):
        await store.collect(client)
    assert len(client.calls) == 1


async def test_cancelled_read_and_network_failure_invalidate_previous(setup):
    store, _ = setup
    for error in (asyncio.CancelledError(), OSError("fictitious")):
        await store.collect(Client())
        client = Client()

        async def fail(_error=error, **_kwargs):
            raise _error

        client.get_orders = fail
        with pytest.raises(type(error)):
            await store.collect(client)
        with pytest.raises(ValueError, match="UNAVAILABLE"):
            read(store)


@pytest.mark.parametrize("wall_delta,mono_delta", [(30001, 0), (0, 30001), (-1, 0), (0, -1)])
async def test_dual_clock_rejects_expiry_and_regression(setup, wall_delta, mono_delta):
    store, clock = setup
    await store.collect(Client())
    clock.wall += wall_delta
    clock.mono += mono_delta
    with pytest.raises(ValueError, match="CLOCK_INVALID"):
        read(store)


@pytest.mark.parametrize(
    "field,value",
    [
        ("instance_id", "e" * 32),
        ("subaccount", 0),
        ("source_sha", "e" * 40),
        ("account_evidence_sha256", "e" * 64),
    ],
)
async def test_rebinding_never_reuses_receipt(setup, field, value):
    store, clock = setup
    await store.collect(Client())
    other = FinancialReceiptStore(
        store.engine,
        replace(store.binding, **{field: value}),
        wall=lambda: clock.wall,
        monotonic=lambda: clock.mono,
    )
    with pytest.raises(ValueError, match="BINDING_CHANGED"):
        read(other)
    with pytest.raises(ValueError, match="BINDING_CHANGED"):
        other.provision(epoch="d" * 32)


async def test_aba_between_pages_invalidates_even_when_final_data_unchanged(setup):
    store, _ = setup
    execute(
        store,
        "INSERT INTO operational_state(key,value,reason,updated_at) VALUES('test','off','test','2026-10-13')",
    )
    before = fence(store)
    client = Client()

    async def mutate(**_kwargs):
        execute(store, "UPDATE operational_state SET value='on' WHERE key='test'")
        execute(store, "UPDATE operational_state SET value='off' WHERE key='test'")
        return {"orders": [], "cursor": ""}

    client.get_orders = mutate
    with pytest.raises(ValueError, match="LEDGER_CHANGED"):
        await store.collect(client)
    assert fence(store).sequence == before.sequence + 2


async def test_change_after_receipt_never_passes_borrowed_session(setup):
    store, _ = setup
    await store.collect(Client())
    execute(
        store,
        "INSERT INTO operational_state(key,value,reason,updated_at) VALUES('test','off','test','2026-10-13')",
    )
    with pytest.raises(ValueError, match="LEDGER_CHANGED"):
        read(store)


async def test_read_does_not_autoflush_or_commit_pending_objects(setup):
    store, _ = setup
    await store.collect(Client())
    with Session(store.engine) as s:
        s.exec(text("BEGIN IMMEDIATE"))
        s.add(OperationalState(key="new", value="engaged", reason="test"))
        with pytest.raises(ValueError, match="UNFLUSHED_SESSION"):
            store.read(s)
        assert len(s.new) == 1
        s.rollback()
    with Session(store.engine) as s:
        assert s.exec(select(OperationalState)).all() == []
    assert read(store)["generation"] == 1


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM financial_fence",
        "UPDATE financial_fence SET seq=0",
        "INSERT OR REPLACE INTO financial_fence SELECT * FROM financial_fence",
        "UPDATE financial_fence SET epoch='other',seq=seq+1",
    ],
)
def test_fence_cannot_be_reset_deleted_replaced_or_rebound(setup, sql):
    store, _ = setup
    with pytest.raises(Exception, match="FENCE_.*FORBIDDEN"):
        execute(store, sql)
    assert fence(store).sequence == 0


@pytest.mark.parametrize("action", ["INSERT", "UPDATE", "DELETE"])
@pytest.mark.parametrize("table", TRACKED)
def test_all_financial_tables_have_each_exact_trigger(setup, table, action):
    store, _ = setup
    name = f"financial_fence_{table}_{action.lower()}"
    execute(store, f"DROP TRIGGER {name}")
    with pytest.raises(ValueError, match="TRIGGERS_CHANGED"):
        fence(store)
    with pytest.raises(ValueError, match="TRIGGERS_CHANGED"):
        store.provision(epoch="d" * 32)


async def test_recreated_trigger_between_acquisition_invalidates_schema_version(setup):
    store, _ = setup
    client = Client()

    async def change(**_kwargs):
        name = "financial_fence_trades_update"
        execute(store, f"DROP TRIGGER {name}")
        execute(store, TRIGGERS[name])
        return {"orders": [], "cursor": ""}

    client.get_orders = change
    with pytest.raises(ValueError, match="LEDGER_CHANGED"):
        await store.collect(client)


def test_transaction_rollback_does_not_invalidate_committed_revision(setup):
    store, _ = setup
    before = fence(store)
    with Session(store.engine) as s:
        s.exec(text("BEGIN IMMEDIATE"))
        db = s.connection().connection.driver_connection
        db.execute(
            "INSERT INTO operational_state(key,value,reason,updated_at) VALUES('test','off','test','2026-10-13')"
        )
        assert read_fence(db).sequence == before.sequence + 1
        s.rollback()
    assert fence(store) == before


def test_exhaustion_blocks_receipts_but_allows_protective_writes(setup):
    store, _ = setup
    # Emular una base que llegó al límite sin gastar 2**63 escrituras.
    execute(store, "DROP TRIGGER financial_fence_monotonic")
    execute(store, f"UPDATE financial_fence SET seq={MAX_SEQUENCE - 1}")
    execute(store, TRIGGERS["financial_fence_monotonic"])
    execute(
        store,
        "INSERT INTO operational_state(key,value,reason,updated_at) VALUES('kill_switch','engaged','test','2026-10-13')",
    )
    with pytest.raises(ValueError, match="EXHAUSTED"):
        fence(store)
    execute(store, "UPDATE operational_state SET reason='still protected'")


async def test_rebuild_preserves_counter_receipt_and_triggers(setup, monkeypatch):
    from scripts import rebuild_db

    store, _ = setup
    execute(
        store,
        "INSERT INTO operational_state(key,value,reason,updated_at) VALUES('test','off','test','2026-10-13')",
    )
    await store.collect(Client())
    before = fence(store)
    monkeypatch.setenv("DATABASE_URL", str(store.engine.url))
    assert rebuild_db.main() == 0
    with sqlite3.connect(str(store.path) + ".rebuilt") as db:
        db.execute("BEGIN IMMEDIATE")
        copied = read_fence(db)
        assert copied.sequence == before.sequence and copied.epoch == before.epoch
        assert (
            dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")) == TRIGGERS
        )
        payload = db.execute("SELECT payload FROM financial_receipt_state").fetchone()[0]
        assert json.loads(payload)["generation"] == 1
        db.execute("UPDATE operational_state SET value='on'")
        assert read_fence(db).sequence == before.sequence + 1
    assert read(store)["generation"] == 1


async def test_initial_zero_does_not_purge_existing_positions_or_unknown_orders(setup):
    store, _ = setup
    with Session(store.engine) as s:
        s.add(PortfolioPosition(ticker="KXTEST", side="yes", count=2))
        s.add(
            Trade(
                client_order_id="unknown",
                ticker="KXTEST",
                side="yes",
                action="buy",
                count=1,
                price_cents=66,
                strategy="motor_2_consensus",
                status="pending",
            )
        )
        s.commit()
    await store.collect(Client())
    assert read(store)["collections"]["orders"]["rows"] == []
    with Session(store.engine) as s:
        row = s.exec(select(Trade)).one()
        assert row.status == "pending" and row.price_cents == 66
        assert s.exec(select(PortfolioPosition)).one().count == 2


@pytest.mark.parametrize(
    "model", [Trade, RiskCommitment, PortfolioPosition, Admission, OperationalState]
)
def test_fence_observes_actual_insert_update_delete_with_borrowed_transaction(setup, model):
    store, _ = setup
    rows = {
        Trade: Trade(
            client_order_id="coid",
            ticker="KXTEST",
            side="yes",
            action="buy",
            count=1,
            price_cents=66,
            strategy="motor_2_consensus",
        ),
        RiskCommitment: RiskCommitment(
            client_order_id="coid",
            group_key="G",
            ticker="KXTEST",
            event_ticker="KXTEST",
            strategy="motor_2_consensus",
            action="buy",
            side="yes",
            count_fp="1.00",
            price_cents=66,
            risk_cents=66,
            fee_reserve_cents=1,
        ),
        PortfolioPosition: PortfolioPosition(ticker="KXTEST", side="yes", count=1),
        Admission: Admission(
            client_order_id="coid",
            scope="m2.entries",
            permission_revision=1,
            command_id="e" * 32,
            source_sha="b" * 40,
            instance_id="c" * 32,
            limits_sha256="f" * 64,
            admitted_at_ms=NOW,
            intent_digest="a" * 64,
        ),
        OperationalState: OperationalState(key="kill_switch", value="engaged", reason="test"),
    }
    with Session(store.engine) as s:
        s.exec(text("BEGIN IMMEDIATE"))
        db = s.connection().connection.driver_connection
        before = read_fence(db)
        row = rows[model]
        s.add(row)
        s.flush()
        assert read_fence(db).sequence == before.sequence + 1
        # UPDATE sin cambio de valor también invalida: no depende del resultado
        # final ni puede perder un ABA real entre dos transacciones.
        column = (
            "key" if model is OperationalState else "ticker" if model is not Admission else "scope"
        )
        s.exec(text(f"UPDATE {model.__tablename__} SET {column}={column}"))
        assert read_fence(db).sequence == before.sequence + 2
        s.delete(row)
        s.flush()
        assert read_fence(db).sequence == before.sequence + 3
        s.commit()


async def test_permission_clock_pending_does_not_flush_or_block_financial_reader(setup):
    store, _ = setup
    await store.collect(Client())
    with Session(store.engine) as s:
        s.exec(text("BEGIN IMMEDIATE"))
        s.add(_Meta(binding_digest="f" * 64, last_seen_ms=NOW))
        assert store.read(s)["generation"] == 1
        assert len(s.new) == 1
        s.rollback()
    with Session(store.engine) as s:
        assert s.get(_Meta, 1) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("base_url", "https://wrong.example/trade-api/v2"),
        ("_client", SimpleNamespace(base_url="https://wrong.example/trade-api/v2")),
        ("settings", SimpleNamespace(KALSHI_ENV="production", KALSHI_API_KEY_ID=KEY_ID)),
        ("settings", SimpleNamespace(KALSHI_ENV="demo", KALSHI_API_KEY_ID="changed-fake")),
        ("signer", SimpleNamespace(api_key_id="changed-fake")),
    ],
)
async def test_binding_failure_invalidates_without_network(setup, field, value):
    store, _ = setup
    await store.collect(Client())
    client = Client()
    setattr(client, field, value)
    with pytest.raises(ValueError, match="CLIENT_BINDING_CHANGED"):
        await store.collect(client)
    assert client.calls == []
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        read(store)


async def test_schema_migration_blocks_old_and_new_receipts_until_review(setup):
    store, _ = setup
    await store.collect(Client())
    execute(store, "ALTER TABLE trades ADD COLUMN future_financial_field INTEGER")
    with pytest.raises(ValueError, match="TABLES_CHANGED"):
        read(store)
    with pytest.raises(ValueError, match="TABLES_CHANGED"):
        await store.collect(Client())


async def test_extra_trigger_cannot_silently_override_fence(setup):
    store, _ = setup
    execute(
        store, "CREATE TRIGGER unexpected BEFORE UPDATE ON trades BEGIN SELECT RAISE(IGNORE); END"
    )
    with pytest.raises(ValueError, match="TRIGGERS_CHANGED"):
        await store.collect(Client())


async def test_budget_timeout_and_bytes_leave_no_complete_receipt(setup, monkeypatch):
    store, _ = setup
    monkeypatch.setattr("src.control.financial_receipts.MAX_BYTES", 50)
    with pytest.raises(ValueError, match="BYTE_LIMIT"):
        await store.collect(Client([{"positions": [], "cursor": "", "extra": "x" * 50}]))
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        read(store)
    monkeypatch.setattr("src.control.financial_receipts.MAX_AGE_MS", 1)
    client = Client()

    async def wait(**_kwargs):
        await asyncio.Event().wait()

    client.get_positions = wait
    with pytest.raises(TimeoutError):
        await store.collect(client)
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        read(store)


async def test_superseded_collector_cannot_publish_or_destroy_new_receipt(setup):
    store, _ = setup
    started, release = asyncio.Event(), asyncio.Event()
    client = Client()

    async def wait(**_kwargs):
        started.set()
        await release.wait()
        return {"orders": [], "cursor": ""}

    client.get_orders = wait
    old = asyncio.create_task(store.collect(client))
    await started.wait()
    current = await store.collect(Client())
    release.set()
    with pytest.raises(ValueError, match="ATTEMPT_SUPERSEDED"):
        await old
    assert read(store) == current


async def test_write_failure_never_releases_unknown_or_says_complete(setup, monkeypatch):
    store, _ = setup
    await store.collect(Client())
    # Una falla sólo en la confirmación final conserva COLLECTING/FAILED; no
    # restauramos el recibo anterior ni modificamos datos financieros.
    real_db = store._db

    def fail_on_complete(s):
        db = real_db(s)
        db.execute(
            "CREATE TEMP TRIGGER IF NOT EXISTS reject_receipt BEFORE UPDATE ON financial_receipt_state "
            "WHEN NEW.status='COMPLETE' BEGIN SELECT RAISE(ABORT,'test failure'); END"
        )
        return db

    monkeypatch.setattr(store, "_db", fail_on_complete)
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        await store.collect(Client())
    monkeypatch.setattr(store, "_db", real_db)
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        read(store)


async def test_clock_regression_between_pages_rejects_even_above_acquisition_start(setup):
    store, clock = setup
    client = Client()

    async def positions(**_kwargs):
        clock.wall += 100
        clock.mono += 100
        return {"positions": [], "cursor": ""}

    async def orders(**_kwargs):
        clock.wall -= 1
        return {"orders": [], "cursor": ""}

    client.get_positions, client.get_orders = positions, orders
    with pytest.raises(ValueError, match="CLOCK_INVALID"):
        await store.collect(client)
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        read(store)


async def test_explicit_instance_transition_invalidates_old_and_allows_new_acquisition(setup):
    store, clock = setup
    await store.collect(Client())
    before = fence(store)
    other = FinancialReceiptStore(
        store.engine,
        replace(store.binding, instance_id="e" * 32, source_sha="f" * 40),
        wall=lambda: clock.wall,
        monotonic=lambda: clock.mono,
    )
    with pytest.raises(ValueError, match="REBIND_CONFLICT"):
        other.rebind_instance(previous=store.binding, expected_generation=0)
    assert read(store)["generation"] == 1
    other.rebind_instance(previous=store.binding, expected_generation=1)
    assert fence(other) == before
    with pytest.raises(ValueError, match="BINDING_CHANGED"):
        await store.collect(Client())
    with pytest.raises(ValueError, match="UNAVAILABLE"):
        read(other)
    assert (await other.collect(Client()))["generation"] == 3
    with pytest.raises(ValueError, match="BINDING_CHANGED"):
        read(store)


@pytest.mark.parametrize(
    "change",
    [
        {"subaccount": 4},
        {"credential_sha256": "e" * 64},
        {"account_evidence_sha256": "e" * 64},
        {"environment": "production"},
        {"rest_url": "https://wrong.example/trade-api/v2"},
    ],
)
async def test_instance_transition_never_changes_account_binding(setup, change):
    store, clock = setup
    await store.collect(Client())
    other = FinancialReceiptStore(
        store.engine,
        replace(store.binding, instance_id="e" * 32, **change),
        wall=lambda: clock.wall,
        monotonic=lambda: clock.mono,
    )
    with pytest.raises(ValueError, match="REBIND_INVALID"):
        other.rebind_instance(previous=store.binding, expected_generation=1)
    assert read(store)["generation"] == 1


async def test_old_inflight_collector_cannot_publish_after_explicit_rebinding(setup):
    store, clock = setup
    started, release = asyncio.Event(), asyncio.Event()
    client = Client()

    async def wait(**_kwargs):
        started.set()
        await release.wait()
        return {"orders": [], "cursor": ""}

    client.get_orders = wait
    old = asyncio.create_task(store.collect(client))
    await started.wait()
    other = FinancialReceiptStore(
        store.engine,
        replace(store.binding, instance_id="e" * 32),
        wall=lambda: clock.wall,
        monotonic=lambda: clock.mono,
    )
    other.rebind_instance(previous=store.binding, expected_generation=1)
    current = await other.collect(Client())
    release.set()
    with pytest.raises(ValueError, match="BINDING_CHANGED"):
        await old
    assert read(other) == current


async def test_reviewed_additive_migration_preserves_sequence_and_reacquires(setup):
    store, _ = setup
    await store.collect(Client())
    before = fence(store)
    with store.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        db = conn.connection.driver_connection
        db.execute("ALTER TABLE trades ADD COLUMN reviewed_test_field TEXT")
        # Sólo fixture: el ejecutor de una migración real debe traer el hash
        # revisado de antemano y no calcular/aprobar a ciegas el DB instalado.
        approved = digest(
            {
                k: v
                for k, v in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
                if k in TRACKED
            }
        )
        with pytest.raises(ValueError, match="MIGRATION_MISMATCH"):
            accept_reviewed_schema(db, previous=before, expected_tables_sha256="0" * 64)
        with pytest.raises(ValueError, match="TABLES_CHANGED"):
            read_fence(db)
        after = accept_reviewed_schema(db, previous=before, expected_tables_sha256=approved)
        assert after.sequence == before.sequence + 1 and after.epoch == before.epoch
        conn.commit()
    with pytest.raises(ValueError, match="LEDGER_CHANGED"):
        read(store)
    await store.collect(Client())
    assert read(store)["generation"] == 2


def test_reviewed_migration_rollback_restores_old_schema_and_fence(setup):
    store, _ = setup
    before = fence(store)
    with store.engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        db = conn.connection.driver_connection
        db.execute("ALTER TABLE trades ADD COLUMN reviewed_test_field TEXT")
        approved = digest(
            {
                k: v
                for k, v in db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
                if k in TRACKED
            }
        )
        accept_reviewed_schema(db, previous=before, expected_tables_sha256=approved)
        conn.rollback()
    assert fence(store) == before
