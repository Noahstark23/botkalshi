from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("account_reader", BASE / "account_reader.py")
ar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ar)


class FakeClient:
    def __init__(self):
        self.position_calls = []
        self.fill_calls = []

    async def get_balance(self):
        return {"balance": 20000, "updated_ts": "2026-09-20T15:59:00Z"}

    async def get_positions(self, *, limit, cursor=None):
        self.position_calls.append((limit, cursor))
        if cursor is None:
            return {
                "market_positions": [{"ticker": "KX-SYN", "position": "2"}],
                "cursor": "p2",
            }
        return {"market_positions": [], "cursor": ""}

    async def get_fills(self, *, limit, cursor=None):
        self.fill_calls.append((limit, cursor))
        if cursor is None:
            return {
                "fills": [
                    {
                        "trade_id": "trade-1",
                        "ticker": "KX-SYN",
                        "side": "yes",
                        "action": "buy",
                        "count": "2",
                        "price": "0.40",
                    }
                ],
                "cursor": "f2",
            }
        return {"fills": [], "cursor": None}


class AccountReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_collects_all_pages_but_grants_no_authority(self):
        client = FakeClient()
        result = await ar.collect_account_snapshot(
            client,
            now=datetime(2026, 9, 20, 16, 0, tzinfo=UTC),
        )
        self.assertEqual(result["balance_usd"], "200.00")
        self.assertEqual(result["positions_count"], 1)
        self.assertEqual(result["fills_count"], 1)
        self.assertEqual(client.position_calls, [(100, None), (100, "p2")])
        self.assertEqual(client.fill_calls, [(100, None), (100, "f2")])
        self.assertFalse(result["reconciled"])
        self.assertFalse(result["execution_authorized"])

    async def test_repeated_cursor_blocks_incomplete_snapshot(self):
        class Broken(FakeClient):
            async def get_positions(self, *, limit, cursor=None):
                return {"market_positions": [], "cursor": "same"}

        with self.assertRaises(ar.AccountReadError):
            await ar.collect_account_snapshot(Broken())
