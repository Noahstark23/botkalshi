"""Adquisiciones completas opcionales; ningún permiso se deriva de estas solas.

Un único recibo contiene su caché saneada de posiciones y órdenes recientes.
No modifica PortfolioPosition, trades, reservas ni UNKNOWN; no sustituye el
archivo histórico, la atribución, identidad verificada de cuenta o GateFacts.
Sin cableado al runner ni provisión automática. Nunca envía/cancela órdenes.
"""

import asyncio
import hashlib
import json
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlmodel import Session

from src.control.wire import _hex, _integer
from src.math.quantities import api_count, fixed_count, signed_fixed_count
from src.storage.financial_fence import TRACKED, Fence, digest, provision_fence, read_fence, require

MAX_BYTES = 8 * 1024 * 1024
MAX_PAGES = 100  # presupuesto total entre ambas colecciones, no por colección
PAGE_SIZE = 100
MAX_AGE_MS = 30000
DDL = (
    "CREATE TABLE financial_receipt_state (id INTEGER PRIMARY KEY CHECK(id=1), "
    "binding TEXT NOT NULL, generation INTEGER NOT NULL, last_wall_ms INTEGER NOT NULL, "
    "status TEXT NOT NULL, payload TEXT, payload_sha256 TEXT, binding_json TEXT NOT NULL)"
)


def _json(value, *, max_bytes=None):
    try:
        result = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("RECEIPT_INVALID_JSON") from None
    require(
        len(result.encode()) <= (MAX_BYTES if max_bytes is None else max_bytes),
        "RECEIPT_BYTE_LIMIT",
    )
    return result


@dataclass(frozen=True)
class SourceBinding:
    environment: str
    rest_url: str
    credential_sha256: str  # hash del identificador, nunca de la clave privada
    account_evidence_sha256: str  # referencia explícita; este módulo no la autentica
    subaccount: int
    source_sha: str
    instance_id: str  # nonce nuevo por arranque; no se reutiliza para recuperar

    def validate(self):
        require(self.environment in {"demo", "production"}, "RECEIPT_BINDING_INVALID")
        require(type(self.rest_url) is str, "RECEIPT_BINDING_INVALID")
        url = urlsplit(self.rest_url)
        require(
            url.scheme == "https"
            and url.hostname
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment
            and url.path == "/trade-api/v2",
            "RECEIPT_BINDING_INVALID",
        )
        require(
            _hex(self.credential_sha256, 64)
            and _hex(self.account_evidence_sha256, 64)
            and _hex(self.source_sha, 40)
            and _hex(self.instance_id, 32)
            and _integer(self.subaccount, 0, 2**31 - 1),
            "RECEIPT_BINDING_INVALID",
        )
        return digest(asdict(self))

    def check_client(self, client):
        # Comprobar también el transporte: cambiar sólo base_url en el wrapper
        # no acredita que httpx y el firmante sigan usando la misma identidad.
        require(
            client.base_url == self.rest_url
            and str(client._client.base_url).rstrip("/") == self.rest_url
            and self.environment == client.settings.KALSHI_ENV
            and client.signer.api_key_id == client.settings.KALSHI_API_KEY_ID
            and hashlib.sha256(client.settings.KALSHI_API_KEY_ID.encode()).hexdigest()
            == self.credential_sha256,
            "RECEIPT_CLIENT_BINDING_CHANGED",
        )


def _identifier(value):
    return type(value) is str and 0 < len(value) <= 200 and value == value.strip()


def _row(kind, row, subaccount):
    require(type(row) is dict and _identifier(row.get("ticker")), "RECEIPT_ROW_INVALID")
    if "subaccount_number" in row:
        require(
            type(row["subaccount_number"]) is int and row["subaccount_number"] == subaccount,
            "RECEIPT_SUBACCOUNT_MISMATCH",
        )
    if kind == "positions":
        quantity = api_count(row, "position", signed=True)
        require(quantity is not None, "RECEIPT_QUANTITY_INVALID")
        return {"ticker": row["ticker"], "position_fp": signed_fixed_count(quantity)}
    require(
        _identifier(row.get("order_id"))
        and _identifier(row.get("client_order_id"))
        and row.get("side") in {"yes", "no"}
        and row.get("action") in {"buy", "sell"}
        and row.get("status") in {"resting", "executed", "canceled", "cancelled"},
        "RECEIPT_ORDER_INVALID",
    )
    initial, fill, remaining = [
        api_count(row, name) for name in ("initial_count", "fill_count", "remaining_count")
    ]
    require(
        initial is not None and initial > 0 and fill is not None and remaining is not None,
        "RECEIPT_QUANTITY_INVALID",
    )
    require(fill + remaining <= initial, "RECEIPT_QUANTITY_CONTRADICTION")
    if row["status"] == "resting":
        require(remaining > 0 and fill + remaining == initial, "RECEIPT_ORDER_CONTRADICTION")
    else:
        require(remaining == 0, "RECEIPT_ORDER_CONTRADICTION")
        if row["status"] == "executed":
            require(fill == initial, "RECEIPT_ORDER_CONTRADICTION")
    name = row["side"] + "_price"
    try:
        legacy, dollars = row.get(name), row.get(name + "_dollars")
        require(legacy is None or _integer(legacy, 0, 100), "RECEIPT_PRICE_INVALID")
        require(
            dollars is None or type(dollars) is str and len(dollars) <= 16, "RECEIPT_PRICE_INVALID"
        )
        price = Decimal(dollars) if dollars is not None else Decimal(legacy) / 100
        require(
            price.is_finite() and 0 <= price <= 1 and price.as_tuple().exponent >= -4,
            "RECEIPT_PRICE_INVALID",
        )
        require(legacy is None or price * 100 == legacy, "RECEIPT_PRICE_CONTRADICTION")
    except (InvalidOperation, TypeError):
        raise ValueError("RECEIPT_PRICE_INVALID") from None
    return {
        **{
            key: row[key]
            for key in ("ticker", "order_id", "client_order_id", "side", "action", "status")
        },
        "initial_count_fp": fixed_count(initial),
        "fill_count_fp": fixed_count(fill),
        "remaining_count_fp": fixed_count(remaining),
        name + "_dollars": format(price, ".4f"),
    }


class FinancialReceiptStore:
    def __init__(self, engine, binding: SourceBinding, *, wall=None, monotonic=None):
        require(
            engine.dialect.name == "sqlite" and engine.url.database not in (None, ":memory:"),
            "RECEIPT_DURABLE_DATABASE_REQUIRED",
        )
        self.engine, self.binding = engine, binding
        self.binding_digest = binding.validate()
        self.binding_json = _json(asdict(binding), max_bytes=8192)
        self.path = Path(engine.url.database).absolute()
        self.identity = self._identity()
        self.wall = wall or (lambda: time.time_ns() // 1000000)
        self.monotonic = monotonic or (lambda: time.monotonic_ns() // 1000000)

    def _identity(self):
        require(self.path.is_file() and not self.path.is_symlink(), "RECEIPT_DATABASE_INVALID")
        st = self.path.stat()
        return st.st_dev, st.st_ino

    def _db(self, session):
        require(
            session.get_bind() is self.engine and self._identity() == self.identity,
            "RECEIPT_DATABASE_CHANGED",
        )
        db = session.connection().connection.driver_connection
        require(db.in_transaction, "RECEIPT_TRANSACTION_REQUIRED")
        # No hacer autoflush encubierto; el caller decide qué confirma antes de
        # observar. Entidades pendientes no pueden pasar por datos persistidos.
        require(
            not any(
                getattr(type(obj), "__tablename__", None) in TRACKED
                for obj in (*session.new, *session.dirty, *session.deleted)
            ),
            "RECEIPT_UNFLUSHED_SESSION",
        )
        return db

    def _state(self, db, *, expected_binding=None):
        schema = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='financial_receipt_state'"
        ).fetchone()
        require(schema is not None and schema[0] == DDL, "RECEIPT_SCHEMA_CHANGED")
        rows = db.execute(
            "SELECT binding,generation,last_wall_ms,status,payload,payload_sha256,binding_json "
            "FROM financial_receipt_state WHERE id=1"
        ).fetchall()
        require(len(rows) == 1, "RECEIPT_STATE_INVALID")
        state = rows[0]
        require(_integer(state[1]) and _integer(state[2], 946684800000), "RECEIPT_STATE_INVALID")
        require(type(state[6]) is str and len(state[6].encode()) <= 8192, "RECEIPT_BINDING_INVALID")
        try:
            stored = SourceBinding(**json.loads(state[6]))
            stored_digest = stored.validate()
        except (TypeError, ValueError, RecursionError):
            raise ValueError("RECEIPT_BINDING_INVALID") from None
        require(
            stored_digest == state[0] and _json(asdict(stored), max_bytes=8192) == state[6],
            "RECEIPT_BINDING_INVALID",
        )
        require(
            state[0] == (self.binding_digest if expected_binding is None else expected_binding),
            "RECEIPT_BINDING_CHANGED",
        )
        return state

    def rebind_instance(self, *, expected_previous_digest: str, expected_generation: int):
        """Transición explícita con escritores detenidos; nunca llamada al arrancar.

        Sólo cambia SHA/nonce. Cuenta/entorno/host/credencial/subcuenta requieren
        un procedimiento separado. Conserva secuencia, ledger y reloj de pared.
        """
        require(
            _hex(expected_previous_digest, 64) and _integer(expected_generation),
            "RECEIPT_REBIND_INVALID",
        )

        def stable(b):
            return {k: v for k, v in asdict(b).items() if k not in {"source_sha", "instance_id"}}

        with Session(self.engine) as s:
            s.exec(text("BEGIN IMMEDIATE"))
            db = self._db(s)
            # La identidad anterior viene de la DB validada, incluso si el proceso
            # murió sin conservar su nonce. El digest/generación son el CAS explícito.
            state = self._state(db, expected_binding=expected_previous_digest)
            previous = SourceBinding(**json.loads(state[6]))
            require(
                stable(previous) == stable(self.binding)
                and previous.instance_id != self.binding.instance_id,
                "RECEIPT_REBIND_INVALID",
            )
            read_fence(db)
            require(state[1] == expected_generation, "RECEIPT_REBIND_CONFLICT")
            require(state[1] < 2**53 - 1, "RECEIPT_CAPACITY_EXHAUSTED")
            now = self.wall()
            require(_integer(now, state[2]), "RECEIPT_CLOCK_INVALID")
            db.execute(
                "UPDATE financial_receipt_state SET binding=?,binding_json=?,generation=generation+1,"
                "last_wall_ms=?,status='EMPTY',payload=NULL,payload_sha256=NULL WHERE id=1",
                (self.binding_digest, self.binding_json, now),
            )
            s.commit()

    def provision(self, *, epoch):
        """Explícita y vacía: no repara recibos perdidos ni cambia el binding."""
        with Session(self.engine) as s:
            s.exec(text("BEGIN IMMEDIATE"))
            db = self._db(s)
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
            if "financial_receipt_state" in names or "financial_fence" in names:
                self._state(db)
                read_fence(db)
                require(read_fence(db).epoch == epoch, "FENCE_BINDING_CHANGED")
                return
            provision_fence(db, epoch=epoch)
            db.execute(DDL)
            now = self.wall()
            require(_integer(now, 946684800000), "RECEIPT_CLOCK_INVALID")
            db.execute(
                "INSERT INTO financial_receipt_state VALUES(1,?,0,?,'EMPTY',NULL,NULL,?)",
                (self.binding_digest, now, self.binding_json),
            )
            s.commit()

    def _start(self):
        with Session(self.engine) as s:
            s.exec(text("BEGIN IMMEDIATE"))
            db = self._db(s)
            state = self._state(db)
            fence = read_fence(db)
            wall, mono = self.wall(), self.monotonic()
            require(_integer(wall, state[2]) and _integer(mono), "RECEIPT_CLOCK_INVALID")
            require(state[1] < 2**53 - 1, "RECEIPT_CAPACITY_EXHAUSTED")
            generation = state[1] + 1
            # Invalidar antes de la primera request: fallo/cancelación no conserva
            # un recibo anterior como si el último barrido hubiese sido correcto.
            db.execute(
                "UPDATE financial_receipt_state SET generation=?,last_wall_ms=?,"
                "status='COLLECTING',payload=NULL,payload_sha256=NULL WHERE id=1",
                (generation, wall),
            )
            s.commit()
        return generation, fence, wall, mono

    def _failed(self, generation):
        with Session(self.engine) as s:
            s.exec(text("BEGIN IMMEDIATE"))
            db = self._db(s)
            self._state(db)
            db.execute(
                "UPDATE financial_receipt_state SET status='FAILED' "
                "WHERE id=1 AND generation=? AND status='COLLECTING'",
                (generation,),
            )
            s.commit()

    def _times(self, start_wall, start_mono):
        wall, mono = self.wall(), self.monotonic()
        require(
            _integer(wall)
            and _integer(mono)
            and 0 <= wall - start_wall <= MAX_AGE_MS
            and 0 <= mono - start_mono <= MAX_AGE_MS,
            "RECEIPT_STALE_OR_CLOCK_INVALID",
        )
        return wall, mono

    async def collect(self, client):
        """Dos recorridos GET acotados; no invocado por el runtime existente.

        Referencia de cuenta declarada != identidad verificada. Órdenes recientes
        completas != archivo histórico. El cliente conserva sus reintentos GET
        internos, pero todas las llamadas comparten deadline de 30 segundos.
        """
        generation, fence, start_wall, start_mono = self._start()
        try:
            self.binding.check_client(client)
            collections = {}
            page_count = 0
            byte_count = 0
            previous = (start_wall, start_mono)
            async with asyncio.timeout(MAX_AGE_MS / 1000):
                for kind in ("positions", "orders"):
                    rows, pages, cursors, identities, coids = [], [], set(), set(), set()
                    cursor = None
                    while True:
                        require(page_count < MAX_PAGES, "RECEIPT_PAGE_LIMIT")
                        self.binding.check_client(client)
                        before = self._times(*previous)
                        fetch = client.get_positions if kind == "positions" else client.get_orders
                        page = await fetch(
                            limit=PAGE_SIZE, cursor=cursor, subaccount=self.binding.subaccount
                        )
                        self.binding.check_client(client)
                        after = self._times(*before)
                        previous = after
                        self._times(start_wall, start_mono)
                        body = _json(page)
                        byte_count += len(body.encode())
                        require(byte_count <= MAX_BYTES, "RECEIPT_BYTE_LIMIT")
                        require(type(page) is dict, "RECEIPT_PAGE_INVALID")
                        raw = (
                            page.get("market_positions", page.get("positions"))
                            if kind == "positions"
                            else page.get("orders")
                        )
                        if (
                            kind == "positions"
                            and "positions" in page
                            and "market_positions" in page
                        ):
                            require(page["positions"] == raw, "RECEIPT_LIST_CONTRADICTION")
                        require(type(raw) is list and len(raw) <= PAGE_SIZE, "RECEIPT_PAGE_INVALID")
                        for row in raw:
                            normalized = _row(kind, row, self.binding.subaccount)
                            identity = normalized["ticker" if kind == "positions" else "order_id"]
                            require(identity not in identities, "RECEIPT_DUPLICATE_IDENTITY")
                            identities.add(identity)
                            if kind == "orders":
                                coid = normalized["client_order_id"]
                                require(coid not in coids, "RECEIPT_DUPLICATE_IDENTITY")
                                coids.add(coid)
                            rows.append(normalized)
                        following = page.get("cursor")
                        require(
                            type(following) is str
                            and len(following) <= 4096
                            and (not following or following.strip()),
                            "RECEIPT_CURSOR_INVALID",
                        )
                        require(not following or following not in cursors, "RECEIPT_CURSOR_CYCLE")
                        pages.append(
                            {
                                "requested_cursor_sha256": digest(cursor),
                                "next_cursor_sha256": digest(following),
                                "terminal": following == "",
                                "count": len(raw),
                                "body_sha256": digest(page),
                                "started_at_ms": before[0],
                                "finished_at_ms": after[0],
                            }
                        )
                        page_count += 1
                        if following == "":
                            break
                        cursors.add(following)
                        cursor = following
                    collections[kind] = {"rows": rows, "pages": pages, "rows_sha256": digest(rows)}
            end_wall, end_mono = self._times(*previous)
            self._times(start_wall, start_mono)
            value = {
                "binding": self.binding_digest,
                "generation": generation,
                "fence": asdict(fence),
                "started_at_ms": start_wall,
                "finished_at_ms": end_wall,
                "started_monotonic_ms": start_mono,
                "finished_monotonic_ms": end_mono,
                "remote_atomic": False,
                "account_identity_verified": False,
                "historical_orders_complete": False,
                "collections": collections,
            }
            payload = _json(value)
            with Session(self.engine) as s:
                s.exec(text("BEGIN IMMEDIATE"))
                db = self._db(s)
                state = self._state(db)
                require(
                    state[1] == generation and state[3] == "COLLECTING",
                    "RECEIPT_ATTEMPT_SUPERSEDED",
                )
                require(read_fence(db) == fence, "RECEIPT_LEDGER_CHANGED")
                commit_wall, _ = self._times(end_wall, end_mono)
                self._times(start_wall, start_mono)
                db.execute(
                    "UPDATE financial_receipt_state SET status='COMPLETE',last_wall_ms=?,"
                    "payload=?,payload_sha256=? WHERE id=1",
                    (commit_wall, payload, hashlib.sha256(payload.encode()).hexdigest()),
                )
                s.commit()
            return value
        except BaseException:
            with suppress(Exception):
                self._failed(generation)
            raise

    def read(self, session):
        """Lectura dentro del lock del caller: sin autoflush, commit o red.

        Toda modificación local invalida este recibo global estricto. No es aún
        un proveedor utilizable para admitir bid+ask M5 secuencialmente.
        """
        with session.no_autoflush:
            db = self._db(session)
            state = self._state(db)
            require(state[3] == "COMPLETE" and type(state[4]) is str, "RECEIPT_UNAVAILABLE")
            require(len(state[4].encode()) <= MAX_BYTES, "RECEIPT_BYTE_LIMIT")
            require(
                hashlib.sha256(state[4].encode()).hexdigest() == state[5],
                "RECEIPT_INTEGRITY_INVALID",
            )
            value = json.loads(state[4])
            require(
                value["binding"] == self.binding_digest and value["generation"] == state[1],
                "RECEIPT_INTEGRITY_INVALID",
            )
            require(read_fence(db) == Fence(**value["fence"]), "RECEIPT_LEDGER_CHANGED")
            wall, _ = self._times(value["started_at_ms"], value["started_monotonic_ms"])
            require(wall >= state[2], "RECEIPT_CLOCK_INVALID")
            return value
