# core/persistence/repository.py
import aiosqlite
import logging
import time
from typing import Dict, List, Any, Tuple, Optional
import os

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS symbol_state (
    symbol TEXT PRIMARY KEY,
    phase TEXT NOT NULL DEFAULT 'IDLE',
    center_price REAL DEFAULT 0.0,
    iteration INTEGER DEFAULT 0,
    last_update_time REAL DEFAULT 0,
    cycle_id INTEGER DEFAULT 0,
    anchor_price REAL DEFAULT 0.0,
    metadata TEXT DEFAULT '{}',
    grid_level_1 REAL DEFAULT 0.0,
    grid_level_2 REAL DEFAULT 0.0,
    active_grid_count INTEGER DEFAULT 1,
    position_counter INTEGER DEFAULT 0,
    last_direction TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS grid_pairs (
    symbol TEXT NOT NULL,
    pair_index INTEGER NOT NULL,
    buy_price REAL DEFAULT 0.0,
    sell_price REAL DEFAULT 0.0,
    buy_ticket INTEGER DEFAULT 0,
    sell_ticket INTEGER DEFAULT 0,
    buy_filled INTEGER DEFAULT 0,
    sell_filled INTEGER DEFAULT 0,
    buy_pending_ticket INTEGER DEFAULT 0,
    sell_pending_ticket INTEGER DEFAULT 0,
    trade_count INTEGER DEFAULT 0,
    next_action TEXT DEFAULT 'buy',
    is_reopened INTEGER DEFAULT 0,
    buy_in_zone INTEGER DEFAULT 0,
    sell_in_zone INTEGER DEFAULT 0,
    hedge_ticket INTEGER DEFAULT 0,
    hedge_direction TEXT,
    hedge_active INTEGER DEFAULT 0,
    locked_buy_entry REAL DEFAULT 0.0,
    locked_sell_entry REAL DEFAULT 0.0,
    tp_blocked INTEGER DEFAULT 0,
    group_id INTEGER DEFAULT 0,
    metadata TEXT DEFAULT '{}',
    PRIMARY KEY (symbol, pair_index)
);

CREATE TABLE IF NOT EXISTS ticket_map (
    ticket INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    cycle_id INTEGER DEFAULT 0,
    pair_index INTEGER DEFAULT 0,
    leg TEXT DEFAULT '',
    trade_count INTEGER DEFAULT 0,
    entry_price REAL DEFAULT 0.0,
    tp_price REAL DEFAULT 0.0,
    sl_price REAL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS trade_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    pair_index INTEGER DEFAULT 0,
    direction TEXT DEFAULT '',
    price REAL DEFAULT 0.0,
    lot_size REAL DEFAULT 0.0,
    ticket INTEGER DEFAULT 0,
    notes TEXT DEFAULT ''
);
"""

# Ensure db directory exists
os.makedirs("db", exist_ok=True)
DB_PATH = "db/grid_v3.db"

class Repository:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.db: Optional[aiosqlite.Connection] = None

    def _conn(self) -> aiosqlite.Connection:
        if self.db is None:
            raise RuntimeError("Repository is not initialized")
        return self.db

    async def initialize(self):
        """Connect and ensure schema exists."""
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        
        # Read schema file, but fall back to built-in bootstrap if it is missing.
        schema_path = os.path.join("db", "schema.sql")
        # Adjust path if running from root or core
        if not os.path.exists(schema_path):
             # Try absolute path based on project root assumption or relative
             current_dir = os.path.dirname(os.path.abspath(__file__))
             # core/persistence/ -> db/schema.sql? No, db is at root usually.
             # Assuming running from root:
             schema_path = "db/schema.sql"
        
        # Fallback to absolute path relative to this file if simple path fails
        if not os.path.exists(schema_path):
             # c:\...\core\persistence\..\..\db\schema.sql
             root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
             schema_path = os.path.join(root_dir, "db", "schema.sql")

        if os.path.exists(schema_path):
            with open(schema_path, "r", encoding="utf-8") as f:
                await self.db.executescript(f.read())
        else:
            await self.db.executescript(SCHEMA_SQL)
        
        # MIGRATION: Add tp_blocked column to grid_pairs if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE grid_pairs ADD COLUMN tp_blocked BOOLEAN DEFAULT 0")
            print(f"[REPOS] Migration: added 'tp_blocked' column to 'grid_pairs'")
        except Exception:
            pass

        # MIGRATION: Add group_id column to grid_pairs if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE grid_pairs ADD COLUMN group_id INTEGER DEFAULT 0")
            print(f"[REPOS] Migration: added 'group_id' column to 'grid_pairs'")
        except Exception:
            pass
            
        # MIGRATION: Add metadata column to symbol_state if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE symbol_state ADD COLUMN metadata TEXT DEFAULT '{}'")
            print(f"[REPOS] Migration: added 'metadata' column to 'symbol_state'")
        except Exception:
            pass

        # MIGRATION: Add metadata column to grid_pairs if it doesn't exist
        try:
            await self.db.execute("ALTER TABLE grid_pairs ADD COLUMN metadata TEXT DEFAULT '{}'")
            print(f"[REPOS] Migration: added 'metadata' column to 'grid_pairs'")
        except Exception:
            pass

        # MIGRATION: Add Grid Bounce state columns to symbol_state if missing
        for sql, label in [
            ("ALTER TABLE symbol_state ADD COLUMN center_price REAL DEFAULT 0.0", "center_price"),
            ("ALTER TABLE symbol_state ADD COLUMN grid_level_1 REAL DEFAULT 0.0", "grid_level_1"),
            ("ALTER TABLE symbol_state ADD COLUMN grid_level_2 REAL DEFAULT 0.0", "grid_level_2"),
            ("ALTER TABLE symbol_state ADD COLUMN active_grid_count INTEGER DEFAULT 1", "active_grid_count"),
            ("ALTER TABLE symbol_state ADD COLUMN position_counter INTEGER DEFAULT 0", "position_counter"),
            ("ALTER TABLE symbol_state ADD COLUMN last_direction TEXT DEFAULT ''", "last_direction"),
        ]:
            try:
                await self.db.execute(sql)
                print(f"[REPOS] Migration: added '{label}' column to 'symbol_state'")
            except Exception:
                pass
            
        await self.db.commit()

    async def get_state(self) -> Dict[str, Any]:
        """Load symbol-level state (phase, center_price, cycle_id, anchor_price)."""
        async with self._conn().execute(
            "SELECT * FROM symbol_state WHERE symbol = ?", (self.symbol,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return dict(row)
            return {}

    async def save_state(self, phase: str, center_price: float, iteration: int,
                         cycle_id: int = 0, anchor_price: float = 0.0, metadata: str = '{}'):
        """Upsert symbol state including cycle management fields."""
        await self._conn().execute(
            """
            INSERT INTO symbol_state (symbol, phase, center_price, iteration, last_update_time, cycle_id, anchor_price, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                phase=excluded.phase,
                center_price=excluded.center_price,
                iteration=excluded.iteration,
                last_update_time=excluded.last_update_time,
                cycle_id=excluded.cycle_id,
                anchor_price=excluded.anchor_price,
                metadata=excluded.metadata
            """,
            (self.symbol, phase, center_price, iteration, time.time(), cycle_id, anchor_price, metadata)
        )
        await self._conn().commit()

    async def get_pairs(self) -> List[Dict[str, Any]]:
        """Load all active pairs for this symbol."""
        async with self._conn().execute(
            "SELECT * FROM grid_pairs WHERE symbol = ?", (self.symbol,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def upsert_pair(self, pair_data: Dict[str, Any], metadata: str = '{}'):
        """Insert or Update a single pair (Atomic operation)."""
        # Extract fields from pair_data dict
        await self._conn().execute(
            """
            INSERT INTO grid_pairs (
                symbol, pair_index, buy_price, sell_price, 
                buy_ticket, sell_ticket, buy_filled, sell_filled,
                buy_pending_ticket, sell_pending_ticket,
                trade_count, next_action, is_reopened,
                buy_in_zone, sell_in_zone,
                hedge_ticket, hedge_direction, hedge_active,
                locked_buy_entry, locked_sell_entry, tp_blocked, group_id, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol, pair_index) DO UPDATE SET
                buy_price=excluded.buy_price,
                sell_price=excluded.sell_price,
                buy_ticket=excluded.buy_ticket,
                sell_ticket=excluded.sell_ticket,
                buy_filled=excluded.buy_filled,
                sell_filled=excluded.sell_filled,
                buy_pending_ticket=excluded.buy_pending_ticket,
                sell_pending_ticket=excluded.sell_pending_ticket,
                trade_count=excluded.trade_count,
                next_action=excluded.next_action,
                is_reopened=excluded.is_reopened,
                buy_in_zone=excluded.buy_in_zone,
                sell_in_zone=excluded.sell_in_zone,
                hedge_ticket=excluded.hedge_ticket,
                hedge_direction=excluded.hedge_direction,
                hedge_active=excluded.hedge_active,
                locked_buy_entry=excluded.locked_buy_entry,
                locked_sell_entry=excluded.locked_sell_entry,
                tp_blocked=excluded.tp_blocked,
                group_id=excluded.group_id,
                metadata=excluded.metadata
            """,
            (
                self.symbol, pair_data['index'], pair_data['buy_price'], pair_data['sell_price'],
                pair_data.get('buy_ticket', 0), pair_data.get('sell_ticket', 0),
                pair_data.get('buy_filled', 0), pair_data.get('sell_filled', 0),
                pair_data.get('buy_pending_ticket', 0), pair_data.get('sell_pending_ticket', 0),
                pair_data.get('trade_count', 0), pair_data.get('next_action', 'buy'),
                pair_data.get('is_reopened', 0), pair_data.get('buy_in_zone', 0),
                pair_data.get('sell_in_zone', 0),
                pair_data.get('hedge_ticket', 0),
                pair_data.get('hedge_direction', None),
                pair_data.get('hedge_active', 0),
                pair_data.get('locked_buy_entry', 0.0),
                pair_data.get('locked_sell_entry', 0.0),
                int(pair_data.get('tp_blocked', False)),
                pair_data.get('group_id', 0),
                metadata
            )
        )
        await self._conn().commit()

    async def delete_pair(self, pair_index: int):
        """Remove a pair (used in Leapfrog)."""
        await self._conn().execute(
            "DELETE FROM grid_pairs WHERE symbol = ? AND pair_index = ?",
            (self.symbol, pair_index)
        )
        await self._conn().commit()

    # ========================================================================
    # TICKET MAP (Groups + 3-Cap Strategy)
    # ========================================================================

    async def save_ticket(self, ticket: int, cycle_id: int, pair_index: int,
                          leg: str, trade_count: int = 0,
                          entry_price: float = 0.0, tp_price: float = 0.0, sl_price: float = 0.0):
        """Save ticket → (pair, leg, prices) mapping for deterministic TP/SL detection."""
        await self._conn().execute(
            """
            INSERT INTO ticket_map (ticket, symbol, cycle_id, pair_index, leg, trade_count, entry_price, tp_price, sl_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticket) DO UPDATE SET
                cycle_id=excluded.cycle_id,
                pair_index=excluded.pair_index,
                leg=excluded.leg,
                trade_count=excluded.trade_count,
                entry_price=excluded.entry_price,
                tp_price=excluded.tp_price,
                sl_price=excluded.sl_price
            """,
            (ticket, self.symbol, cycle_id, pair_index, leg, trade_count, entry_price, tp_price, sl_price)
        )
        await self._conn().commit()

    async def get_ticket_map(self) -> Dict[int, Tuple[int, str, float, float, float]]:
        """Load all ticket mappings for this symbol.

        Returns:
            Dict[ticket, (pair_index, leg, entry_price, tp_price, sl_price)]
        """
        async with self._conn().execute(
            "SELECT ticket, pair_index, leg, entry_price, tp_price, sl_price FROM ticket_map WHERE symbol = ?",
            (self.symbol,)
        ) as cursor:
            rows = await cursor.fetchall()
            return {row['ticket']: (row['pair_index'], row['leg'], row['entry_price'], row['tp_price'], row['sl_price']) for row in rows}

    async def delete_ticket(self, ticket: int):
        """Remove a ticket from the map (on position close)."""
        await self._conn().execute(
            "DELETE FROM ticket_map WHERE ticket = ?",
            (ticket,)
        )
        await self._conn().commit()

    async def clear_ticket_map(self):
        """Clear all tickets for this symbol (on fresh start)."""
        await self._conn().execute(
            "DELETE FROM ticket_map WHERE symbol = ?",
            (self.symbol,)
        )
        await self._conn().commit()

    # ========================================================================
    # TRADE HISTORY
    # ========================================================================

    async def log_trade(self, event: Dict[str, Any]):
        """Log a trade event to history table (Permanent storage)."""
        await self._conn().execute(
            """
            INSERT INTO trade_history (symbol, timestamp, event_type, pair_index, direction, price, lot_size, ticket, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.symbol, event['timestamp'], event['event_type'], 
                event['pair_index'], event['direction'], event['price'], 
                event['lot_size'], event['ticket'], event.get('notes', '')
            )
        )
        await self._conn().commit()

    async def close(self):
        if self.db:
            await self.db.close()
            self.db = None
