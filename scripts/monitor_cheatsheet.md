# V2 Activation Monitor — Cheat Sheet
**Date:** 2026-05-25 | **Window:** 2–3h continuous supervision

---

## T+0: Activation Steps

1. Coolify → Configuration → Environment Variables → add `USE_ORDERBOOK_MANAGER_V2=true`
2. Keep `MOTOR_1_ARBITRAGE_ENABLED=false` and `TRADING_ENABLED=false` — **DO NOT touch these**
3. Redeploy (~2 min)
4. **Must see in logs within 30s of boot:**
   ```
   OrderbookManagerV2 registered (data-capture only, no Motor 1)
   ```
   If this line is absent after 60s → abort, rollback immediately.

---

## Success Criteria (T+2h — ALL must pass)

1. Zero new `ERROR` logs related to orderbook / manager / V2
2. `SidGapError` rate sustained **< 5/min** (momentary spikes OK; median matters)
3. `_take_snapshots` completing **40/40** every ~5 min (no regression from current baseline)

---

## Rollback Criteria — ANY triggers immediate rollback without discussion

1. More than 3 errors **not** of type `SidGapError` in 10 min
2. `SidGapError` sustained **> 20/min** for more than 5 min
3. `tracked_markets` drops below **35**
4. `/status` returns `capture_running=false` or `ws_connected=false` for more than 60s
5. Any `CRITICAL` log or any exception not present before activation

---

## Rollback Procedure (target: < 5 min end-to-end)

1. Coolify → Environment Variables → `USE_ORDERBOOK_MANAGER_V2=false`
2. Redeploy
3. Verify logs show **no** V2 registration message
4. Confirm `/status` returns `"orderbook_manager_v2": {"enabled": false}`

---

## Grep Patterns (paste into Coolify log filter)

```bash
grep "SidGapError"
grep "OrderbookManagerV2"
grep "recovery"
grep -i "error\|critical\|exception"
grep "Snapshots:"          # must show 40/40 every ~5 min
```

---

## /status V2 Block — Expected Healthy State

```json
"orderbook_manager_v2": {
  "enabled": true,
  "books_initialized": 40,        // reaches ~40 within T+1 min
  "sids_tracked": 1,              // typically 1 sid for all markets
  "sids_recovering": 0,           // must be 0 in steady state
  "gaps_last_60s": 0,             // spikes to 1-5 OK; sustained >5 = alert
  "last_gap_at": null             // or ISO timestamp of last gap
}
```

Poll every 5 min via: `curl http://<host>:18080/status | python3 -m json.tool | grep -A8 orderbook_manager_v2`

---

## DB Backup (execute in Coolify terminal BEFORE touching flag)

> **Why not `cp`:** `cp` on a live SQLite file with concurrent writers (~26 rows/sec from
> snapshot REST path) can produce a torn, unrecoverable copy. Use SQLite's online backup
> API instead — it is atomic and safe with active writers.

```bash
# Step 1 — atomic backup via SQLite online backup API
docker exec kalshi-bot sqlite3 /app/data/trades.db \
  ".backup /app/data/trades_backup_$(date +%Y%m%d_%H%M%S).db"

# Step 2 — verify file exists
docker exec kalshi-bot ls -lh /app/data/trades_backup_*.db

# Step 3 — integrity check (must return "ok" before proceeding)
docker exec kalshi-bot sqlite3 /app/data/trades_backup_*.db "PRAGMA integrity_check;"
```

If Step 3 does not return `ok`: repeat Steps 1–3 before touching the flag.

---

## Key Invariants During Window

- `MOTOR_1_ARBITRAGE_ENABLED` stays **false** — V2 is wired by data_capture, not Motor 1
- `TRADING_ENABLED` stays **false** — read-only observation only
- If Telegram alert fires: check `/status` immediately, compare `gaps_last_60s` to baseline
- If `sids_recovering > 0` for > 60s sustained: note ticker + timestamp, not a rollback trigger alone
