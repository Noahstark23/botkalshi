"""Build a visibly synthetic paper-session fixture; never fetches market data."""

from decimal import Decimal
from us500_engine import HOSTS, SCHEMA, body_digest

NOW = 1791744840000


def session(prices=None, *, environment="demo", quantity="2", depth="10"):
    if prices is None:
        prices = [
            "13",
            "13.1",
            "13.2",
            "13.1",
            "13",
            "12.9",
            "13.1",
            "13.2",
            "13.2",
            "13.2",
        ]
    host, ticker = HOSTS[environment]
    frames = []
    for i, price in enumerate(prices):
        at = NOW + i * 1000
        p = Decimal(price)
        market = {
            "ticker": ticker,
            "status": "active",
            "schedule": {"is_open": True},
            "tick_size": "0.0001",
            "contract_size": "0.001000",
            "underlying_multiplier": "1.000000",
            "fractional_trading_enabled": False,
            "reference_price": {"price": price, "ts_ms": at},
            "liquidation_mark_price": {"price": price, "ts_ms": at},
        }
        book = {
            "bids": [[format(p - Decimal("0.01"), "f"), depth]],
            "asks": [[format(p + Decimal("0.01"), "f"), depth]],
        }
        observations = {}
        for resource, body, suffix in (
            ("market", {"market": market}, ""),
            ("orderbook", {"orderbook": book}, "/orderbook?depth=20"),
        ):
            observations[resource] = {
                "url": f"{host}/trade-api/v2/margin/markets/{ticker}{suffix}",
                "http_status": 200,
                "requested_at_ms": at - 20,
                "received_at_ms": at,
                "canonical_body_sha256": body_digest(body),
                "body": body,
            }
        frames.append(
            {
                "at_ms": at,
                "snapshot": {
                    "schema": SCHEMA,
                    "environment": environment,
                    "ticker": ticker,
                    "synthetic": True,
                    "authenticated": False,
                    "atomic_snapshot": False,
                    "observations": observations,
                },
                "funding_cashflow_usd": "0",
                "infrastructure_cost_usd": "0.001",
            }
        )
    return {
        "schema": "us500-paper-session-v1",
        "run_id": "synthetic-example-v1",
        "mode": "paper",
        "ledger": {
            "initial_cash_usd": "100",
            "initial_margin_rate": "1",
            "maintenance_margin_rate": "0.5",
            "fee_bps": "12",
            "fee_quantum_usd": "0.0001",
            "max_notional_usd": "50",
            "max_loss_usd": "10",
            "max_gap_ms": 5000,
            "quantity_step": "1",
            "tick_size": "0.0001",
        },
        "strategy": {
            "name": "momentum-v1",
            "lookback": 2,
            "threshold_bps": "10",
            "quantity": quantity,
            "max_spread_bps": "30",
            "slippage_bps": "1",
            "assumptions_note": "Synthetic prices, fictitious USD100, full-notional initial margin, fixed hypothetical rates. Zero funding is explicitly assumed over this nine-second fixture; fees and USD0.001 per observation are invented cost parameters for tests, not account terms.",
        },
        "frames": frames,
    }
