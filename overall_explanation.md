# Weltrade Bot — Developer Architecture & Codebase Reference

## Table of Contents

1. [Repository Structure](#repository-structure)
2. [File-by-File Reference](#file-by-file-reference)
3. [Architectural Decisions](#architectural-decisions)
4. [Strategy Engine Deep Dive](#strategy-engine-deep-dive)
5. [Key Data Structures](#key-data-structures)
6. [Feature Implementation Notes](#feature-implementation-notes)
7. [Config Schema](#config-schema)
8. [Known Constraints & Edge Cases](#known-constraints--edge-cases)

---

## Repository Structure

```
weltrade-bot/
├── api/
│   └── server.py                  # FastAPI application, all HTTP endpoints
├── core/
│   ├── __init__.py
│   ├── bot_manager.py             # Per-user bot lifecycle management
│   ├── config_manager.py          # Config persistence and validation
│   ├── event_bus.py               # Async event pub/sub (legacy, not used in main loop)
│   ├── run_state.py               # Crash recovery state persistence
│   ├── session_logger.py          # Per-user session log files
│   ├── strategy_orchestrator.py   # Coordinates multiple symbols per user
│   ├── trading_engine.py          # MT5 tick loop, health monitoring
│   └── engine/
│       ├── activity_logger.py     # Per-symbol human-readable activity logs
│       └── grid_bounce_strategy_engine.py  # Core strategy logic
├── core/persistence/
│   └── repository.py              # SQLite state persistence (aiosqlite)
├── static/
│   ├── index.html                 # Entire frontend (vanilla JS + Tailwind)
│   └── styles.css                 # Global styles
├── db/
│   └── schema.sql                 # SQLite schema (grid_pairs, ticket_map, etc.)
├── logs/                          # Runtime log output (gitignored)
├── main.py                        # Entry point, uvicorn launcher
├── requirements.txt
├── .env.example
├── setup_env.bat                  # Windows credential setup script
└── open_port.bat                  # Windows firewall port-opening script
```

---

## File-by-File Reference

### `main.py`

Entry point. Configures rotating file logging and stdout/stderr `Tee` redirection so all terminal output is simultaneously written to `logs/terminal_output.log`. Launches uvicorn with the FastAPI app from `api/server.py`. Registers SIGINT/SIGTERM handlers for clean shutdown.

**Key decisions:**
- `Tee` class redirects both stdout and stderr to a file without losing console output. This exists because the VPS may not have a persistent terminal session and operators need a file they can inspect after the fact.
- Port defaults to `800` from `BOT_PORT` env var. Host defaults to `0.0.0.0` so it binds to all interfaces and is reachable remotely.

---

### `api/server.py`

FastAPI application. All HTTP routes are defined here. Manages the global `BotManager` and `TradingEngine` singletons.

**On startup:** Deletes the SQLite DB file if it exists (stale DB from a crashed session would cause position-tracking corruption on restart). Launches `trading_engine.start()` as an asyncio background task.

**Auth:** Supabase JWT tokens validated via `supabase.auth.get_user(token)`. Results are cached in a `TTLCache` (30 second TTL, 100 entries) to avoid hammering the Supabase API on every request during the 1-second polling cycle. Each token maps to a user ID which maps to a `StrategyOrchestrator` instance in `BotManager`.

**Multi-tenancy:** Every authenticated user gets their own isolated `StrategyOrchestrator`. Config files are named `config_{user_id}.json`. This means multiple users can run the bot simultaneously on the same VPS without interfering with each other. The SQLite DB (`db/grid_v3.db`) is shared, which is a known limitation — it works because each symbol has its own row and users typically trade different symbols.

**Pydantic models:** `SymbolConfig`, `GlobalConfig`, `ConfigUpdate`. The `ConfigUpdate` model accepts both `global_` (aliased from the JSON key `"global"`) and `global_settings` for backward compatibility with older frontend versions.

**Static file serving:** The `static/` directory is mounted at `/static`. The root route `/` serves `static/index.html` directly. Routes are defined before the static mount to prevent route shadowing.

**Key routes:**
- `GET /env` — Returns Supabase URL and key to the frontend (necessary because the frontend is served from the same origin and needs these to initialise the Supabase client).
- `POST /config` — Validates and persists config via `config_manager.update_config()`, then calls `orchestrator.start_ticker()` to sync strategies.
- `POST /control/start` — Calls `_prepare_fresh_session` (terminates existing strategies, closes repositories), deletes DB, optionally restarts the trading engine if stopped, then calls `orchestrator.start()`.
- `POST /control/terminate-all` — Terminates all strategies then also runs a nuclear fallback that scans the entire MT5 account for residual open positions and closes them all, regardless of whether they were tracked.
- `GET /status` — Aggregates status across all active strategies and returns a single dict.

---

### `core/bot_manager.py`

Maintains a `Dict[str, StrategyOrchestrator]` mapping user IDs to their orchestrator instances. On `get_or_create_bot`, if the user has no in-memory instance (e.g. after a server restart), a new `ConfigManager` and `StrategyOrchestrator` are created. Config is loaded from the user's JSON file so settings are restored. `start_ticker()` is called on the orchestrator to sync the strategy list with the config.

---

### `core/config_manager.py`

Manages per-user JSON config files. Config is a nested dict with a `global` block and a `symbols` block.

**Validation on `update_config`:**
- `grid_distance` clamped to minimum 1.0
- `tp_pips`, `sl_pips` clamped to minimum 1.0
- `second_entry_*_pips` clamped to range 1.0–5000.0
- `max_positions` must be a multiple of 3, clamped to range 3–`MAX_POSITION_LIMIT` (60)
- `volatility_tolerance` validated against allowed set: `"off"`, `"1.5"`, `"1.75"`, `"2.0"`, `"2.25"`, `"2.5"`
- Lot arrays padded/trimmed to match the required length for the given group count
- Legacy scalar lot fields (`pair_buy_lot`, etc.) kept in sync with Set 1 for backward compatibility with old configs

**Sets config:** Each symbol now supports multiple sets via `sets_config` array. Each set has its own `pair_buy_lots`, `pair_sell_lots`, `single_lots`, `max_positions`. When a set is added or modified, `update_config` normalises its arrays to the correct length. The legacy top-level lot fields are always kept equal to Set 1's values.

**Migration:** `load_config` detects old-format configs (where `symbols` was a list, not a dict) and migrates them to the new multi-asset format. The migrated config is immediately saved.

---

### `core/trading_engine.py`

The main async tick loop. Runs as a background asyncio task started at server startup.

**Tick loop architecture:** Rather than subscribing to MT5's native tick stream (which would require blocking calls), the engine polls `mt5.symbol_info_tick(symbol)` in a tight async loop with `asyncio.sleep(0)` between iterations. This yields control to other coroutines while achieving near-zero latency per tick.

**Active symbols:** The engine asks each `StrategyOrchestrator` for its active symbols on every iteration, then fetches ticks for only those symbols. If no symbols are active, it sleeps for 100ms to avoid a busy-wait.

**Health monitoring:** Every `HEALTH_CHECK_INTERVAL` (100) ticks, `mt5.terminal_info()` is called. If it returns None or `connected=False`, reconnection is attempted up to `MAX_RECONNECT_ATTEMPTS` (10) times with `RECONNECT_DELAY` (5 second) intervals between attempts.

**Session timeout:** If `max_runtime_minutes` is configured, the engine checks elapsed time on every tick and calls `strategy.stop()` (graceful stop) on all strategies when the limit is reached. A 5-minute hard stop failsafe is set — if graceful stop has not completed in 5 minutes, `engine.stop()` is forced.

**DB cleanup:** After a timeout-triggered graceful stop completes, a 5-minute async timer fires and deletes the SQLite DB. This is to ensure stale position data does not interfere with the next session.

---

### `core/strategy_orchestrator.py`

Per-user coordinator. Manages a `Dict[str, GridBounceStrategyEngine]` mapping symbol names to strategy instances.

**`update_strategies`:** Called whenever config changes. Adds new strategy instances for newly enabled symbols, removes instances for disabled symbols. Removal calls `strategy.stop()` best-effort before deleting the reference.

**`terminate_all`:** Iterates all strategies, calls `terminate()` on each, then runs a nuclear fallback via `mt5.positions_get()` to close any orphaned positions not tracked by any strategy instance.

**`get_status`:** Aggregates `running`, `open_positions`, `graceful_stop`, `is_resetting` across all strategies. Returns both aggregate values and a per-symbol `strategies` dict for the frontend.

---

### `core/session_logger.py`

Creates a text log file per user per session at `logs/users/{user_id}/sessions/session_{timestamp}.txt`. Logs button clicks, config loads, trade executions, TP/SL hits, and session end. Uses an absolute path derived from `__file__` to avoid CWD-relative path bugs when uvicorn changes the working directory.

---

### `core/engine/activity_logger.py`

Creates a per-symbol per-day activity log at `logs/users/{user_id}/sessions/activity_{symbol}_{date}.log`. Designed to be readable by non-technical users. All leg names are mapped to friendly strings via `LEG_NAMES`. Logs include: position opens, TP/SL hits with profit/loss amounts, resets with reason and PnL, graceful stop requests, phase transitions.

**`log_reset` friendly reasons** include `VOLATILITY_RESET` mapped to `"Volatility/slippage threshold exceeded — automatic safety reset"`.

**`log_tp_hit` / `log_sl_hit`** both accept `triggered_reset: bool` which appends either `"Nuclear reset triggered"` or `"Grid continues"` to the log line, making it easy to identify which closures caused resets.

---

### `core/engine/grid_bounce_strategy_engine.py`

The core strategy engine. Everything else exists to support this file.

See [Strategy Engine Deep Dive](#strategy-engine-deep-dive) below.

---

### `core/persistence/repository.py`

SQLite persistence via `aiosqlite`. Uses a single shared DB file `db/grid_v3.db`. Each `Repository` instance is bound to one symbol.

**Tables:**
- `symbol_state` — Phase, center price, iteration, cycle ID, anchor price, grid levels, position counter, metadata JSON blob.
- `grid_pairs` — Legacy table from the previous strategy version. Still present in schema for compatibility.
- `ticket_map` — Maps tickets to pair index, leg name, entry/TP/SL prices.
- `trade_history` — Append-only event log (fires, closes, resets).

**Migrations:** `initialize()` attempts `ALTER TABLE ADD COLUMN` for each column added after the initial schema. These are wrapped in try/except so they silently succeed if the column already exists and silently fail if it does not (preventing re-migration errors).

**Lifecycle:** `Repository` is lazily initialised on first `save_state` call. It is closed explicitly via `strategy.close()` which is called by the orchestrator's `terminate_all` and `close` methods. This is important because `aiosqlite` holds a file lock that would prevent the DB from being deleted during session cleanup.

---

### `static/index.html`

The entire frontend in a single file. Vanilla JavaScript with Tailwind CSS (loaded from CDN). No build step required.

**Key global state:**
- `currentConfig` — Mirror of the last config received from the server.
- `lotInitialBySafeId` — Per-asset initial lot values used when re-rendering lot input rows. Must be kept in sync when the copy tool writes values.
- `lastInteracted` — `{symbol, safeId}` of the last asset panel the user touched. Used by the copy tool to pre-select the most recently edited source.
- `autoSaveEnabled` — Persisted in `localStorage`. Controls whether `scheduleAutoSave` fires.

**Asset safeId:** Symbols like `"FX Vol 20"` are converted to `"FX_Vol_20"` for use in DOM element IDs. All input IDs follow the pattern `{fieldname}_{safeId}_set_{setIdx}_{index}`.

**`buildConfigPayload()`:** Single shared function used by both manual save and auto-save. Reads all DOM values and constructs the full config payload. Avoids duplication and ensures both paths send identical data.

**`simulateVolumeAccumulation(symbol, safeId)`:** Debounced at 600ms. Walks through the strategy's position accumulation pattern (center pair + net +2 per bounce) and totals open volume at each step. Flags the first field that causes a breach of `ASSET_VOLUME_LIMITS[symbol]`.

**Copy lots modal:** Two-step flow. Step 1 selects source (pre-selects `lastInteracted`). Step 2 selects targets. On confirm: reads source DOM values, applies to targets via `updateLotInputs` re-render + force-write, saves to backend immediately. Handles differing set counts via a third panel (`copy-diff-prompt`) with overwrite/match/skip options.

**Mobile tabs:** On small screens, the layout switches to a tab system (`view-dashboard`, `view-config`, `view-logs`) controlled by `switchTab()`.

---

## Architectural Decisions

### Why MT5 polling instead of event-driven ticks

MT5's Python API does not support async callbacks. All MT5 calls are synchronous and blocking. The engine wraps them in a tight `while True` loop with `asyncio.sleep(0)` to yield to other coroutines without adding artificial latency. Health checks are done every 100 ticks rather than every tick to keep the loop fast.

### Why a single SQLite file instead of per-symbol or per-user files

SQLite with `aiosqlite` gives us async non-blocking writes. A single file means one connection pool to manage. The tradeoff is that multiple users share the same DB, which works in practice because users typically trade different symbols. A future improvement would be per-user DB files.

### Why config is stored as JSON files instead of a database

Config changes are infrequent and the payload is small. JSON files are human-readable, easily backed up, and survive server restarts without any migration step. Each user gets their own `config_{user_id}.json` file, giving natural isolation without any database schema.

### Why the DB is deleted on every start

The SQLite DB stores in-flight position state (what tickets are open, what their TP/SL values are, what phase the cycle is in). If the server crashes mid-cycle, this data becomes stale — MT5 may have closed positions that the DB still shows as open. Rather than implementing a reconciliation system, the decision was to always start fresh and let MT5 be the source of truth for live positions. The DB is only meaningful during a running session.

### Why Supabase is used only for authentication

The auth layer needed to support multiple users accessing the same VPS-hosted server from different browsers without implementing a custom auth system. Supabase provides JWT-based auth with email/password out of the box. The bot itself has no dependency on Supabase for trading logic — removing Supabase would only break the login screen, not the trading engine.

### Why position type tagging (`pair` vs `single_custom`)

The strategy has a nuanced rule: unpaired single legs (the directional leg that faces the "wrong" direction on a bounce) should not trigger nuclear reset when they hit TP or SL, because they are expected to close independently. Only paired positions (which are directionally hedged) trigger reset. This was implemented by tagging every position in `ticket_map` with a `position_type` field (`pair` or `single_custom`) at open time, then checking it in `_check_position_drops`.

### Why center positions open without TP/SL

When the bot starts, it opens a BUY and SELL at the current price. At this point there is no second grid level yet, so it is impossible to know what TP/SL to assign — the values depend on the entry price of the second level's positions, which haven't been placed yet. Center positions are opened with `skip_tp_sl=True` and stops are added via `TRADE_ACTION_SLTP` after the second level activates and the reference prices are established.

### Why virtual TP/SL exists

Some brokers reject `TRADE_ACTION_SLTP` modifications. When this happens the engine falls back to software-side monitoring: it stores the intended TP/SL values in memory, checks them against the live price on every tick in `_check_virtual_stops`, and manually closes the position when the threshold is hit. This is less precise (subject to tick latency) but ensures positions are never left without any stop.

### Why TP/SL alignment exists

When a grid level accumulates multiple positions over several bounces (each bounce adds more to the same level), each new position is set to exactly match the TP/SL of the first position at that level and direction. This is done via `TRADE_ACTION_SLTP` modification immediately after the order fills. The purpose is to ensure all positions at a level close simultaneously when the price reaches the TP/SL, rather than closing in staggered fashion at different prices. The first position at each level/direction sets the reference via `_set_level_reference`; all subsequent positions are aligned to it via `_align_position_tp_sl`.

### Why lot splitting is iterative, not recursive

The max lot per asset is hardcoded and small (e.g. 1.0 for FX Vol 80). A user might enter 5.0. Recursive splitting would create a call stack proportional to the number of chunks. Iterative splitting with a cap of 20 chunks is safe against any input regardless of the max lot value and cannot cause stack overflow.

### Why split groups are tracked

When one order is split into N sub-orders (e.g. 4.0 lot → 2.0 + 2.0), each sub-order is a separate MT5 ticket. If one of them hits TP or SL, `_check_position_drops` would see only that one ticket disappear and might not trigger a reset (if the position counter logic doesn't decrement correctly) or might trigger multiple resets (one per ticket). Split group tracking solves this: all tickets from one split share a `split_group_id`, the group is treated as a single position for counter and reset purposes, and when one ticket closes, the others are force-closed before the reset fires.

### Why the spread is read live rather than hardcoded for volatility checks

Synthetic index spreads vary with market conditions. A hardcoded average could be significantly wrong at any given moment, causing either false resets (spread deduction too small) or missed resets (spread deduction too large). Reading `ask - bid` from the tick data on every call gives the actual current spread at zero cost.

### Why three layers of volatility checking

- **Layer 1 (pre-entry):** Catches the case where the market moved too far before orders were placed. No money at risk yet.
- **Layer 2 (post-fill):** Catches execution slippage — the market looked acceptable when Layer 1 ran but fill prices came back worse.
- **Layer 3 (continuous tick):** Catches mid-cycle slippage between entries — the market moves too far while positions are already open and no new entry is happening.

Each layer covers a window the others do not. All three together ensure no slippage event goes undetected regardless of when it occurs.

### Why `_last_known_spread` persists between ticks

If a tick arrives with an anomalous spread (zero, negative, or impossibly large), using it directly would cause a bad deduction in the volatility check. By only updating `_last_known_spread` when `raw_spread > 0`, the engine always uses the most recent valid spread reading rather than a bad sample.

---

## Strategy Engine Deep Dive

### State machine

```
IDLE → SINGLE_LEVEL → TWO_LEVELS → RESETTING → SINGLE_LEVEL (new cycle)
```

- **IDLE:** Bot not started. No positions.
- **SINGLE_LEVEL:** Center pair open. Waiting for price to move `grid_distance` in either direction.
- **TWO_LEVELS:** Two active levels. Bouncing between them, opening triples on each bounce.
- **RESETTING:** Nuclear reset in progress. All positions being closed. Transitions back to SINGLE_LEVEL at new center price unless `graceful_stop=True`, in which case transitions to IDLE.

### Major methods

**`start()`**
Opens center BUY and SELL at current mid price with `skip_tp_sl=True`. Sets phase to SINGLE_LEVEL. Position counter stays at 0 (center pair does not count toward max_positions).

**`on_external_tick(tick_data)`**
Main tick handler. Execution order:
1. Update `_last_known_spread`
2. `_check_virtual_stops` — manually close positions with virtual TP/SL that have hit their target
3. `_check_volatility_slippage` — Layer 3 volatility check
4. `_update_touch_flags` — latch TP/SL touch flags for all tracked tickets
5. `_check_position_drops` — detect MT5-closed positions, handle split groups, set `_position_drop_detected`
6. `_check_nuclear_reset_trigger` — if `_position_drop_detected`, trigger reset
7. `_check_grid_triggers` — check if price has moved grid distance, activate second level or bounce

**`_check_grid_triggers(ask, bid)`**
In SINGLE_LEVEL: checks if mid has moved `>= grid_distance` up or down from `grid_level_1.price`. Calls `_activate_second_level_up` or `_activate_second_level_down`.

In TWO_LEVELS: determines upper/lower level by comparing prices. Checks if mid has crossed the lower level (bounce down) or upper level (bounce up). The `last_move_direction` flag prevents double-triggering on the same level — it is set on each bounce and only cleared when price crosses to the other level.

**`_activate_second_level_down(ask, bid)`**
1. Close SELL at center (FIFO) via `_close_position`
2. Create `grid_level_2` at `center - grid_distance`, phase → TWO_LEVELS
3. Check max_positions, call `advance_to_next_set()` if needed
4. Call `_open_triple_positions` with direction="DOWN" (PairBuy + PairSell + SingleSell)
5. Add TP/SL to the surviving center BUY via `_add_tp_sl_to_position`
6. Apply startup cross-alignment to the new level via `_apply_startup_cross_alignment`
7. Increment position_counter += 3

**`_open_triple_positions(grid_level, ask, bid, direction)`**
1. Layer 1 volatility check (abort if threshold exceeded)
2. Calculate lot sizes for this stage
3. Open PairBuy, PairSell, Single via `_split_and_execute_orders`
4. For each returned ticket: align TP/SL via `_align_position_tp_sl`, register in `grid_level.positions` and `ticket_map`, init touch flags, log fire
5. Register split groups if any leg produced multiple tickets
6. Layer 2 volatility check (nuclear reset if any fill is out of range)
7. Increment `total_positions`

**`_split_and_execute_orders(direction, lot_size, leg_name, ...)`**
Looks up `MAX_LOT_PER_ASSET[symbol]`. If `lot_size <= max_lot`, calls `_execute_market_order` once and returns `[result]`. Otherwise splits iteratively into chunks of `min(remaining, max_lot)`, capped at 20 total chunks, returning a list of result tuples.

**`_execute_market_order(direction, lot_size, leg_name, target_price, ...)`**
Sends a market order to MT5. On `TRADE_RETCODE_INVALID_STOPS` (10016): fetches a fresh tick, recalculates SL using `MIN_STOP_PIPS_PER_ASSET[symbol] * point` from the fresh execution price, clamps to minimum stop distance, retries once. On retry success: logs and continues. On retry failure: logs error and returns zero tuple.

**`_align_position_tp_sl(ticket, direction, calculated_tp, calculated_sl, grid_level, position_type, ...)`**
Checks if a reference TP/SL exists for this direction/type at this grid level. If not, this position becomes the reference (sets `reference_buy_tp` etc. on `GridLevel`). If yes, modifies the position to match the reference via `TRADE_ACTION_SLTP`. If the modification fails, keeps the original values (position is slightly misaligned but still functional).

**`_apply_startup_cross_alignment(grid_level, direction, startup_sl_anchor, startup_tp_anchor)`**
After the first bounce, the surviving center position has TP/SL set. The cross-alignment ensures the new level's positions use the center position's SL as their TP (and vice versa for the SL). This is the core of the strategy's TP/SL symmetry. It retroactively re-aligns all positions already registered at the new level.

**`_check_position_drops(ask, bid)`**
Compares `set(ticket_map.keys())` against `set(pos.ticket for pos in mt5.positions_get(symbol))`. Dropped tickets are positions closed by MT5 (TP, SL, or manual). For each dropped ticket:
- Determines TP or SL via touch flags, with price-distance fallback
- Calculates realised PnL
- Checks `position_type`: only `pair` positions set `_position_drop_detected = True`
- Handles split groups: if the ticket belongs to a group, force-closes all other tickets in the group, then processes as a single event
- Removes from all tracking structures

**`advance_to_next_set()`**
Called when `position_counter >= max_positions`. If already on the final set, returns False (no more sets). Otherwise increments `current_set_index`, resets `position_counter = 0`, logs the set transition, returns True. Callers check the return value: True means continue opening, False means skip opening (terminal state for this cycle).

**`_nuclear_reset_and_restart(reason, total_pnl)`**
1. Set phase to RESETTING
2. Force-close all open positions for the symbol
3. Log reset via `activity_log.log_reset`
4. Call `_reset_state()` (creates fresh `StrategyState`, preserving only `cycle_count`)
5. If `graceful_stop=True`: set running=False, phase=IDLE, stop
6. Otherwise: set running=False and call `start()` to begin new cycle

---

## Key Data Structures

### `StrategyState` (dataclass)

```python
phase: str                        # IDLE / SINGLE_LEVEL / TWO_LEVELS / RESETTING
center_price: float               # Price at cycle start
grid_level_1: Optional[GridLevel] # Center level (startup)
grid_level_2: Optional[GridLevel] # Second level (activated on first bounce)
position_counter: int             # Positions opened in current set (toward max_positions)
total_positions: int              # Total currently open positions
current_set_index: int            # Active set index (0-based)
last_move_direction: str          # "UP_TO_UPPER" or "DOWN_TO_LOWER"
cycle_count: int                  # Incremented on each nuclear reset
realized_pnl: float               # Cumulative PnL for current cycle
ticket_map: Dict[int, dict]       # All tracked positions by ticket
ticket_touch_flags: Dict[int, dict]  # TP/SL touch latches by ticket
split_group_map: Dict[int, List[int]]  # group_id → list of tickets
```

### `GridLevel` (dataclass)

```python
price: float
active: bool
positions: Dict[int, dict]        # ticket → position info dict
reference_buy_tp: Optional[float]   # TP reference for pair BUY positions
reference_buy_sl: Optional[float]
reference_sell_tp: Optional[float]  # TP reference for pair SELL positions
reference_sell_sl: Optional[float]
reference_custom_buy_tp: Optional[float]   # TP reference for single_custom BUY
reference_custom_buy_sl: Optional[float]
reference_custom_sell_tp: Optional[float]  # TP reference for single_custom SELL
reference_custom_sell_sl: Optional[float]
```

### Position dict (stored in `ticket_map` and `grid_level.positions`)

```python
{
    'leg': str,             # CenterBuy / CenterSell / PairBuy / PairSell / SingleBuy / SingleSell
    'direction': str,       # 'buy' or 'sell'
    'entry': float,         # Actual fill price
    'tp': float,            # Effective TP price (0 if not yet set)
    'sl': float,            # Effective SL price (0 if not yet set)
    'lot': float,           # Lot size of this ticket
    'position_type': str,   # 'pair' or 'single_custom'
    'has_virtual_stops': bool,  # True if broker rejected SLTP modification
    'split_group_id': int,  # Only present if ticket belongs to a split group
}
```

---

## Feature Implementation Notes

### Deferred center TP/SL

Center positions open with `skip_tp_sl=True` (no SL/TP in the order request). After the second level activates and the first triple is open, `_add_tp_sl_to_position` is called on the surviving center position. If MT5 accepts the modification, real stops are set. If MT5 rejects it (broker restriction or minimum stop violation), the position is flagged `has_virtual_stops=True` and monitored in software via `_check_virtual_stops`.

### Touch flags

`_update_touch_flags` latches `tp_touched` and `sl_touched` in `ticket_touch_flags` when price crosses the TP or SL level. These flags are checked in `_check_position_drops` when a position disappears from MT5. The flags are more reliable than price-distance inference because by the time the position is confirmed closed, the price may have moved back. Fallback inference (comparing price distance to TP vs SL) is used only when both flags are False.

### FIFO closing

When bouncing, the bot closes the FIFO (first-in, first-out) position at the departing level. `get_buy_tickets()` and `get_sell_tickets()` on `GridLevel` return tickets in insertion order (dict maintains insertion order in Python 3.7+). The `[0]` element is always the oldest.

### Invalid stop retry

On `TRADE_RETCODE_INVALID_STOPS` from MT5, the retry fetches a fresh tick (stale tick from pre-order execution would cause wrong clamp values), recalculates SL as `exec_price ± (MIN_STOP_PIPS_PER_ASSET[symbol] * point)`, applies the minimum stop distance clamp, and retries once. TP is preserved exactly as the user configured.

---

## Config Schema

```json
{
  "global": {
    "max_runtime_minutes": 0,
    "volatility_tolerance": "off"
  },
  "symbols": {
    "FX Vol 20": {
      "enabled": false,
      "grid_distance": 50.0,
      "tp_pips": 150.0,
      "sl_pips": 200.0,
      "second_entry_buy_tp_pips": 150.0,
      "second_entry_buy_sl_pips": 200.0,
      "second_entry_sell_tp_pips": 150.0,
      "second_entry_sell_sl_pips": 200.0,
      "sets": 1,
      "sets_config": [
        {
          "pair_buy_lots": [0.01, 0.01],
          "pair_sell_lots": [0.01, 0.01],
          "single_lots": [0.01],
          "max_positions": 3
        }
      ],
      "pair_buy_lots": [0.01, 0.01],
      "pair_sell_lots": [0.01, 0.01],
      "single_lots": [0.01],
      "max_positions": 3
    }
  }
}
```

**Lot array sizing:** For a set with `max_positions = N`, `groups = N / 3`. `pair_buy_lots` and `pair_sell_lots` have `groups + 1` entries (center pair + one per group). `single_lots` has `groups` entries (one per group, no center single).

---

## Hardcoded Asset Constants

Three module-level dicts in `grid_bounce_strategy_engine.py`. Immutable at runtime.

```python
MAX_LOT_PER_ASSET = {
    "FX Vol 20": 7,  "FX Vol 40": 4,  "FX Vol 60": 5,
    "FX Vol 80": 1,  "FX Vol 99": 4,
    "SFX Vol 20": 5, "SFX Vol 40": 1, "SFX Vol 60": 1,
    "SFX Vol 80": 2, "SFX Vol 99": 2,
}

MIN_STOP_PIPS_PER_ASSET = {
    "FX Vol 20": 11, "FX Vol 40": 27, "FX Vol 60": 19,
    "FX Vol 80": 34, "FX Vol 99": 42,
    "SFX Vol 20": 21, "SFX Vol 40": 74, "SFX Vol 60": 59,
    "SFX Vol 80": 86, "SFX Vol 99": 18,
}
```

Assets not present default to max_lot=100, stop_pips=10.

Frontend constant in `index.html`:
```javascript
const ASSET_VOLUME_LIMITS = {
    "FX Vol 20": 14, "FX Vol 40": 6,  "FX Vol 60": 14,
    "FX Vol 80": 9,  "FX Vol 99": 6,
    "SFX Vol 20": 18,"SFX Vol 40": 7, "SFX Vol 60": 2,
    "SFX Vol 80": 2, "SFX Vol 99": 34,
};
```

---

## Known Constraints & Edge Cases

**SQLite shared across users:** Multiple users running the same symbol simultaneously will corrupt each other's state. In practice this does not happen but is worth noting.

**MT5 must be open and logged in:** The Python MT5 library communicates with the locally installed MT5 terminal via IPC. If MT5 is closed, all `mt5.*` calls return None. The engine handles this via health checks and reconnection attempts, but if MT5 is completely closed the engine will eventually crash and restart via watchdog (if configured).

**Nuclear reset after manual close:** If a user manually closes a position in MT5 while the bot is running, `_check_position_drops` detects the missing ticket and triggers a nuclear reset. This is intentional — the strategy's TP/SL symmetry is broken if a position disappears unexpectedly, and a fresh start is safer than attempting to continue.

**Position counter does not decrement for `single_custom`:** When a `single_custom` position closes (its own TP or SL), `position_counter` is not decremented. This is intentional — the counter tracks how many full triples have been opened in this set, and removing a single leg should not allow an extra triple to be opened.

**`last_move_direction` prevents double-triggering:** The TWO_LEVELS bounce check sets `last_move_direction` to `"UP_TO_UPPER"` or `"DOWN_TO_LOWER"` after each bounce. The next check for that direction is skipped until the price crosses to the other level. Without this, a single price level crossing would trigger the bounce handler on every subsequent tick until price moved away.

**Graceful stop completes the current cycle:** `graceful_stop=True` is checked in `_nuclear_reset_and_restart`. When the current cycle's next nuclear reset fires naturally (via TP/SL hit), instead of restarting, the engine sets `running=False` and `phase=IDLE`. This means a graceful stop can take as long as the current cycle runs.

**`_reset_state()` replaces the entire `StrategyState` object:** All position tracking, grid levels, touch flags, and split group maps are wiped. Only `cycle_count` is preserved. This is intentional — after a nuclear reset, there should be zero residual state from the previous cycle.