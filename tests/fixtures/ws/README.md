# WS Protocol Fixtures

Captured from Kalshi 2026-05-19 WS probe (inspect_ws.py).

Each file represents one message from the Kalshi WebSocket feed.

| File | Type | Description |
|------|------|-------------|
| subscribed.json | subscribed | Subscription confirmation. Has `sid` but **no** `seq` (probe H5) |
| snapshot_seq_1.json | orderbook_snapshot | Initial snapshot after subscribe (2026 shape) |
| delta_seq_7.json | orderbook_delta | Incremental update, 2026 shape (`price_dollars`/`delta_fp`) |
| get_snapshot_response.json | orderbook_snapshot | Recovery snapshot with `id` echo for WS correlation |
| add_markets_ok.json | ok | Command acknowledgement (`id` echo) |
| unknown_command_error.json | error | Error response for unknown command (`code: 5`) |

## Key probe findings used here

- **H2**: `update_subscription/get_snapshot` confirmed working. Response includes `id` echo.
- **H5**: `subscribed` messages have `sid` but **no** `seq`. Gap-detection must skip them.
- **2026 shape**: `orderbook_delta` uses `price_dollars` + `delta_fp` (not `price`/`delta`).
