from dataclasses import dataclass, field, asdict
from typing import Dict, Optional, Tuple, List, Any
import asyncio
import json
import time
import logging
import MetaTrader5 as mt5_module
from datetime import datetime

from core.engine.activity_logger import ActivityLogger
from core.persistence.repository import Repository

logger = logging.getLogger("pair_strategy")
mt5: Any = mt5_module

# --- Exness asset limits ---
# Exness has no per-asset volume cap; broker maximum is 200 lots for all symbols.
EXNESS_MAX_LOT = 200
# Stop level is always 0 on Exness — INVALID_STOPS retries are effectively unreachable,
# but the retry block is kept for safety with a conservative fallback of 0 extra pips.
EXNESS_FALLBACK_STOP_PIPS = 0



@dataclass
class GridLevel:
    """Represents a single grid level with its positions"""
    price: float
    active: bool = False
    
    # Position tracking (ticket -> {leg, direction, entry, tp, sl, lot})
    positions: Dict[int, dict] = field(default_factory=dict)
    
    def get_buy_tickets(self) -> List[int]:
        """Get all BUY tickets at this level (for FIFO closing)"""
        return [t for t, info in self.positions.items() if info['direction'] == 'buy']
    
    def get_sell_tickets(self) -> List[int]:
        """Get all SELL tickets at this level (for FIFO closing)"""
        return [t for t, info in self.positions.items() if info['direction'] == 'sell']


@dataclass
class StrategyState:
    """Complete state for Grid Bounce Strategy"""
    phase: str = "IDLE"  # IDLE, SINGLE_LEVEL, TWO_LEVELS, RESETTING
    
    # Grid configuration
    center_price: float = 0.0  # Initial startup price
    grid_level_1: Optional[GridLevel] = None  # First level (always center at startup)
    grid_level_2: Optional[GridLevel] = None  # Second level (activated on first move)
    
    # Position management
    position_counter: int = 0  # Counts toward max_positions (excludes initial 2)
    total_positions: int = 0   # Total open positions (for tracking)
    current_set_index: int = 0  # Current active set (0-based index into sets_config)
    
    # Movement tracking
    last_move_direction: str = ""  # "UP" or "DOWN"
    
    # Cycle tracking
    cycle_count: int = 0
    realized_pnl: float = 0.0
    
    # Ticket tracking (global across all levels)
    ticket_map: Dict[int, dict] = field(default_factory=dict)
    ticket_touch_flags: Dict[int, dict] = field(default_factory=dict)
    split_group_map: Dict[int, List[int]] = field(default_factory=dict)


# core logic for managing the 2-grid bounce strategy
class GridBounceStrategyEngine:
    """
    2-Grid Level Bouncing Strategy Engine
    
    Lifecycle:
    1. Start at center → open initial BUY + SELL pair
    2. Wait for grid_distance move (up or down)
    3. On move: close opposite position at origin, open 3 new at destination
    4. Bounce between 2 levels until TP/SL nuclear reset
    5. Reset → restart from current price as new center
    """
    
    MAGIC_NUMBER = 123456
    
    def __init__(self, config_manager, symbol: str, user_id: str = "default", 
                 session_logger=None):
        self.config_manager = config_manager
        self.symbol = symbol
        self.user_id = user_id
        self.session_logger = session_logger
        
        self.state = StrategyState()
        self.running = False
        self.graceful_stop = False
        
        self.execution_lock = asyncio.Lock()
        self.activity_log = ActivityLogger(symbol, user_id, session_logger)
        self.repository: Optional[Repository] = None
        self._position_drop_detected = False
        self._last_known_spread = 0.0
    
    # Config accessors
    @property
    def config(self) -> Dict[str, Any]:
        return self.config_manager.get_symbol_config(self.symbol) or {}

    @property
    def symbol_suffix(self) -> str:
        """Returns the MT5 symbol suffix for the configured Exness account type."""
        from core.config_manager import ACCOUNT_TYPE_SUFFIX
        acct = self.config_manager.get_global_config().get('account_type', 'Standard')
        return ACCOUNT_TYPE_SUFFIX.get(acct, 'm')

    @property
    def mt5_symbol(self) -> str:
        """Full MT5 symbol with account-type suffix, with automatic fallback to plain base name."""
        suffix = self.symbol_suffix
        if suffix:
            candidate = self.symbol + suffix
            info = mt5.symbol_info(candidate)
            if info is not None:
                mt5.symbol_select(candidate, True)
                return candidate
        # Fallback: plain base name (Pro / Zero / Raw Spread accounts or if suffixed name not found)
        return self.symbol

    @property
    def point(self) -> float:
        """MT5 point size for this symbol, fetched live from the broker.
        Custom override for BTCUSD: 1 UI pip = $1.00 price movement."""
        if self.symbol in ["BTCUSD", "ETHUSD", "USTEC", "US500", "US30"]:
            return 1.0
        #so that 100 pips = 100$ movement, matching our own BTCUSD conversions e.g. 50000 -> 50100 for 100 pip move
        
        if self.symbol in ["GBPJPY", "EURJPY", "USDJPY"]:
            return 0.01
        #so that 10 pips = 1.0 movement, matching our own GBPJPY/EURJPY conventions e.g. 150.00 -> 150.10 for 10 pip move
        if self.symbol == "XAUUSD":
            return 0.1
            #so that 10 pips = $1.00 movement, matching our own XAUUSD conversions e.g. 5000 -> 5001 for 10 pip move
            
        # Default for standard currency pairs (EURUSD, GBPUSD, EURAUD, etc.)
        # 1 UI pip = 0.0001 price movement (e.g. 1.0000 -> 1.0010 for 10 pips)
        return 0.0001

    @property
    def grid_distance(self) -> float:
        return float(self.config.get('grid_distance', 50.0))
    
    @property
    def num_sets(self) -> int:
        """Get total number of sets configured"""
        return int(self.config.get('sets', 1))
    
    @property
    def current_set_config(self) -> Dict[str, Any]:
        """Get configuration for current active set"""
        set_idx = max(0, min(self.state.current_set_index, self.num_sets - 1))
        sets_config = self.config.get('sets_config', [])
        if not sets_config:
            # Fallback to single-set config (backward compat)
            return {
                'pair_buy_lots': self.config.get('pair_buy_lots', [0.01, 0.01]),
                'pair_sell_lots': self.config.get('pair_sell_lots', [0.01, 0.01]),
                'single_lots': self.config.get('single_lots', [0.01]),
                'max_positions': self.config.get('max_positions', 3),
            }
        return sets_config[set_idx] if set_idx < len(sets_config) else sets_config[-1]
    
    @property
    def max_positions(self) -> int:
        """Max positions for current set"""
        return int(self.current_set_config.get('max_positions', 3))

    @property
    def group_count(self) -> int:
        return max(1, self.max_positions // 3)

    @property
    def pair_buy_lots(self) -> List[float]:
        lots = self.current_set_config.get('pair_buy_lots')
        if isinstance(lots, list) and lots:
            parsed = [max(0.01, float(x)) for x in lots]
        else:
            parsed = [max(0.01, float(self.current_set_config.get('pair_buy_lot', 0.01)))]
        need = self.group_count + 1  # center + each 3-position group
        if len(parsed) < need:
            parsed += [parsed[-1]] * (need - len(parsed))
        return parsed[:need]

    @property
    def pair_sell_lots(self) -> List[float]:
        lots = self.current_set_config.get('pair_sell_lots')
        if isinstance(lots, list) and lots:
            parsed = [max(0.01, float(x)) for x in lots]
        else:
            parsed = [max(0.01, float(self.current_set_config.get('pair_sell_lot', 0.01)))]
        need = self.group_count + 1
        if len(parsed) < need:
            parsed += [parsed[-1]] * (need - len(parsed))
        return parsed[:need]

    @property
    def single_lots(self) -> List[float]:
        lots = self.current_set_config.get('single_lots')
        if isinstance(lots, list) and lots:
            parsed = [max(0.01, float(x)) for x in lots]
        else:
            parsed = [max(0.01, float(self.current_set_config.get('single_lot', 0.01)))]
        need = self.group_count
        if len(parsed) < need:
            parsed += [parsed[-1]] * (need - len(parsed))
        return parsed[:need]

    def _pair_buy_lot_for_stage(self, stage_idx: int) -> float:
        lots = self.pair_buy_lots
        idx = max(0, min(stage_idx, len(lots) - 1))
        return lots[idx]

    def _pair_sell_lot_for_stage(self, stage_idx: int) -> float:
        lots = self.pair_sell_lots
        idx = max(0, min(stage_idx, len(lots) - 1))
        return lots[idx]

    def _single_lot_for_group(self, group_idx: int) -> float:
        lots = self.single_lots
        idx = max(0, min(group_idx, len(lots) - 1))
        return lots[idx]
    
    @property
    def pair_buy_lot(self) -> float:
        return float(self.config.get('pair_buy_lot', 0.01))
    
    @property
    def pair_sell_lot(self) -> float:
        return float(self.config.get('pair_sell_lot', 0.01))
    
    @property
    def single_lot(self) -> float:
        return float(self.config.get('single_lot', 0.01))
    
    @property
    def tp_pips(self) -> float:
        return float(self.config.get('tp_pips', 150.0))
    
    @property
    def sl_pips(self) -> float:
        return float(self.config.get('sl_pips', 200.0))
    
    @property
    def second_entry_buy_tp_pips(self) -> float:
        """TP pips for unpaired BUY single trades (2nd entry system)"""
        return float(self.config.get('second_entry_buy_tp_pips', self.tp_pips))
    
    @property
    def second_entry_buy_sl_pips(self) -> float:
        """SL pips for unpaired BUY single trades (2nd entry system)"""
        return float(self.config.get('second_entry_buy_sl_pips', self.sl_pips))
    
    @property
    def second_entry_sell_tp_pips(self) -> float:
        """TP pips for unpaired SELL single trades (2nd entry system)"""
        return float(self.config.get('second_entry_sell_tp_pips', self.tp_pips))
    
    @property
    def second_entry_sell_sl_pips(self) -> float:
        """SL pips for unpaired SELL single trades (2nd entry system)"""
        return float(self.config.get('second_entry_sell_sl_pips', self.sl_pips))

    @property
    def current_price(self) -> float:
        tick = mt5.symbol_info_tick(self.mt5_symbol)
        if tick:
            return (tick.ask + tick.bid) / 2
        return self.state.center_price

    @property
    def volatility_tolerance_factor(self):
        val = self.config_manager.get_global_config().get('volatility_tolerance', 'off')
        mapping = {
            "1.5": 1.5,
            "1.75": 1.75,
            "2.0": 2.0,
            "2.25": 2.25,
            "2.5": 2.5
        }
        return mapping.get(val, None)

    def advance_to_next_set(self):
        """
        Advance to the next set when current set reaches max_positions.
        Advances sequentially without wrap-around.
        If already on the last set, no further advancement occurs.
        """
        if self.state.current_set_index >= (self.num_sets - 1):
            self.activity_log.log_info(
                f"Final set reached ({self.get_set_display()}); max positions hit. "
                "No further set rotation."
            )
            return False

        next_idx = self.state.current_set_index + 1
        if next_idx < self.num_sets:
            self.activity_log.log_info(
                f"Max positions for Set {self.state.current_set_index + 1} reached. "
                f"Advancing to Set {next_idx + 1}/{self.num_sets}"
            )
            self.state.current_set_index = next_idx
            self.state.position_counter = 0  # Reset counter for new set
            self.activity_log.log_info(
                f"Now active: {self.get_set_display()} | "
                f"max_positions={self.max_positions}, "
                f"pair_buy={self.pair_buy_lots}, pair_sell={self.pair_sell_lots}, single={self.single_lots}"
            )
            return True
        return False
    
    def get_set_display(self) -> str:
        """Get a display string showing current set info"""
        if self.num_sets > 1:
            return f"Set {self.state.current_set_index + 1}/{self.num_sets}"
        return ""

    async def start_ticker(self):
        """Compatibility hook for orchestrator config refreshes."""
        return None
    

#startup logic and main loop

    async def start(self):
        """
        Start strategy - open initial BUY + SELL at center price
        """
        if self.running:
            return
        
        self.running = True
        self.graceful_stop = False
        
        # Get current tick with retry to ensure valid prices
        tick = mt5.symbol_info_tick(self.mt5_symbol)
        retries = 0
        while (not tick or tick.ask <= 0 or tick.bid <= 0) and retries < 10:
            await asyncio.sleep(0.5)
            tick = mt5.symbol_info_tick(self.mt5_symbol)
            retries += 1
            
        if not tick or tick.ask <= 0 or tick.bid <= 0:
            self.activity_log.log_error("Failed to get valid tick for start. Market might be closed or symbol not subscribed.")
            self.running = False
            return
        
        center = (tick.ask + tick.bid) / 2
        self.state.center_price = center
        
        # Initialize center as grid_level_1
        self.state.grid_level_1 = GridLevel(price=center, active=True)
        
        self.activity_log.log_start(self.state.cycle_count, center)
        if self.num_sets > 1:
            self.activity_log.log_info(
                f"Starting strategy on {self.get_set_display()} | "
                f"max_positions={self.max_positions}, "
                f"pair_buy={self.pair_buy_lots}, pair_sell={self.pair_sell_lots}, single={self.single_lots}"
            )
        
        # Open initial pair at center (with max-lot splitting)
        center_buy_lot = self._pair_buy_lot_for_stage(0)
        center_sell_lot = self._pair_sell_lot_for_stage(0)
        buy_results = await self._split_and_execute_orders("buy", center_buy_lot, "CenterBuy", center, skip_tp_sl=True)
        sell_results = await self._split_and_execute_orders("sell", center_sell_lot, "CenterSell", center, skip_tp_sl=True)

        def _first(res):
            if res and len(res) > 0:
                return res[0]
            return (0, 0.0, 0.0, 0.0)

        buy_ticket, buy_entry, buy_tp, buy_sl = _first(buy_results)
        sell_ticket, sell_entry, sell_tp, sell_sl = _first(sell_results)
        self.activity_log.log_info(
            "Center positions opened without TP/SL (will be added after second entry)"
        )
        dist = self.grid_distance * self.point
        self.activity_log.log_info(
            f"Next triggers (SINGLE_LEVEL): UP at {(center + dist):.5f}, DOWN at {(center - dist):.5f}"
        )
        
        # Store in grid_level_1
        if buy_ticket:
            # register all split tickets for buy
            tickets = []
            for (tkt, entry, tp, sl) in buy_results:
                if not tkt:
                    continue
                self.state.grid_level_1.positions[tkt] = {
                    'leg': 'CenterBuy',
                    'direction': 'buy',
                    'entry': entry,
                    'tp': 0.0,
                    'sl': 0.0,
                    'lot': center_buy_lot,
                    'position_type': 'pair'
                }
                self.state.ticket_map[tkt] = self.state.grid_level_1.positions[tkt]
                self._init_touch_flags(tkt)
                self.activity_log.log_fire(
                    self.state.cycle_count, "CenterBuy", entry,
                    center_buy_lot, tp,
                    sl, tkt
                )
                tickets.append(tkt)
            # record split group
            if len(tickets) > 1:
                group_id = tickets[0]
                self.state.split_group_map[group_id] = list(tickets)
                for idd in tickets:
                    if idd in self.state.ticket_map:
                        self.state.ticket_map[idd]['split_group_id'] = group_id
        
        if sell_ticket:
            tickets = []
            for (tkt, entry, tp, sl) in sell_results:
                if not tkt:
                    continue
                self.state.grid_level_1.positions[tkt] = {
                    'leg': 'CenterSell',
                    'direction': 'sell',
                    'entry': entry,
                    'tp': 0.0,
                    'sl': 0.0,
                    'lot': center_sell_lot,
                    'position_type': 'pair'
                }
                self.state.ticket_map[tkt] = self.state.grid_level_1.positions[tkt]
                self._init_touch_flags(tkt)
                self.activity_log.log_fire(
                    self.state.cycle_count, "CenterSell", entry,
                    center_sell_lot, tp,
                    sl, tkt
                )
                tickets.append(tkt)
            if len(tickets) > 1:
                group_id = tickets[0]
                self.state.split_group_map[group_id] = list(tickets)
                for idd in tickets:
                    if idd in self.state.ticket_map:
                        self.state.ticket_map[idd]['split_group_id'] = group_id
        
        self.state.phase = "SINGLE_LEVEL"
        self.state.total_positions = 2
        # position_counter stays at 0 (these 2 don't count toward max)
        
        await self.save_state()

    #tick handler - same as old one

    async def on_external_tick(self, tick_data: dict):
        """
        Called by orchestrator on every tick
        """
        ask = tick_data.get('ask', 0.0)
        bid = tick_data.get('bid', 0.0)
        raw_spread = ask - bid
        if raw_spread > 0:
            self._last_known_spread = raw_spread

        if not self.running or self.state.phase == "IDLE":
            return
        
        if ask <= 0 or bid <= 0:
            return
        
        async with self.execution_lock:
            # 1. Volatility/slippage tolerant reset check (new)
            await self._check_volatility_slippage(ask, bid)

            # 2. Update touch flags FIRST (PRESERVED)
            self._update_touch_flags(ask, bid)
            
            # 2. Check position drops (TP/SL detection) (PRESERVED)
            await self._check_position_drops(ask, bid)
            
            # 3. Check if any position closed -> nuclear reset
            if await self._check_nuclear_reset_trigger():
                return  # Reset triggered, exit
            
            # 4. Check for grid distance triggers
            await self._check_grid_triggers(ask, bid)

    #grid distance trigger logic

    async def _check_grid_triggers(self, ask: float, bid: float):
        """
        Check if price has moved grid_distance from current level(s)
        and execute appropriate actions
        """
        if self.state.phase == "IDLE" or self.state.phase == "RESETTING":
            return
        
        mid = (ask + bid) / 2
        grid_dist = self.grid_distance * self.point
        
        # --- SINGLE LEVEL PHASE ---
        if self.state.phase == "SINGLE_LEVEL":
            if not self.state.grid_level_1:
                return
            center = self.state.grid_level_1.price
            
            # Check DOWN movement (center - grid_distance)
            if mid <= center - grid_dist:
                await self._activate_second_level_down(ask, bid)
                return
            
            # Check UP movement (center + grid_distance)
            if mid >= center + grid_dist:
                await self._activate_second_level_up(ask, bid)
                return
        
        # --- TWO LEVELS PHASE ---
        elif self.state.phase == "TWO_LEVELS":
            if not self.state.grid_level_1 or not self.state.grid_level_2:
                return

            level_1_price = self.state.grid_level_1.price
            level_2_price = self.state.grid_level_2.price
            
            # Determine which level is upper and which is lower
            upper_price = max(level_1_price, level_2_price)
            lower_price = min(level_1_price, level_2_price)
            
            upper_level = self.state.grid_level_1 if level_1_price == upper_price else self.state.grid_level_2
            lower_level = self.state.grid_level_1 if level_1_price == lower_price else self.state.grid_level_2
            
            # Check if moving DOWN (from upper to lower)
            if mid <= lower_price and self.state.last_move_direction != "DOWN_TO_LOWER":
                await self._bounce_down(upper_level, lower_level, ask, bid)
                return
            
            # Check if moving UP (from lower to upper)
            if mid >= upper_price and self.state.last_move_direction != "UP_TO_UPPER":
                await self._bounce_up(lower_level, upper_level, ask, bid)
                return


    async def _activate_second_level_down(self, ask: float, bid: float):
        """
        First grid distance hit - moving DOWN from center
        
        Actions:
        1. Close SELL at center (grid_level_1) - FIFO
        2. Activate grid_level_2 at (center - grid_distance)
        3. Open 3 positions at grid_level_2: Pair BS + Single SELL
        """
        center_level = self.state.grid_level_1
        if not center_level:
            return
        new_price = center_level.price - (self.grid_distance * self.point)
        
        self.activity_log.log_info(f"Moving DOWN: Grid distance reached at {new_price:.2f}")
        
        # Step 1: Close SELL at center (FIFO)
        sell_tickets = center_level.get_sell_tickets()
        if sell_tickets:
            oldest_sell = sell_tickets[0]  # FIFO
            if self._close_position(oldest_sell):
                self.activity_log.log_info(f"Closed SELL at center (ticket {oldest_sell})")
                self._remove_ticket_from_tracking(oldest_sell, center_level)
        
        # Step 2: Activate grid_level_2
        self.state.grid_level_2 = GridLevel(price=new_price, active=True)
        self.state.phase = "TWO_LEVELS"
        self.state.last_move_direction = "DOWN_TO_LOWER"
        
        self.activity_log.log_grid_activation("Lower Level", new_price)
        
        # Step 3: Check max_positions before opening
        if self.state.position_counter >= self.max_positions:
            # Try to advance to next set
            if not self.advance_to_next_set():
                self.activity_log.log_info(f"Max positions ({self.max_positions}) reached for all sets - skipping new opens")
                await self.save_state()
                return
            # Successfully advanced to next set, continue with opening
        
        # Open 3 positions at new level
        await self._open_triple_positions(
            self.state.grid_level_2, 
            ask, bid, 
            direction="DOWN"  # Opened because we moved down
        )

        self.state.position_counter += 3
        await self._apply_anchor_alignment()
        await self.save_state()


    async def _activate_second_level_up(self, ask: float, bid: float):
        """
        First grid distance hit - moving UP from center
        
        Actions:
        1. Close BUY at center (grid_level_1) - FIFO
        2. Activate grid_level_2 at (center + grid_distance)
        3. Open 3 positions at grid_level_2: Pair BS + Single BUY
        """
        center_level = self.state.grid_level_1
        if not center_level:
            return
        new_price = center_level.price + (self.grid_distance * self.point)
        
        self.activity_log.log_info(f"Moving UP: Grid distance reached at {new_price:.2f}")
        
        # Step 1: Close BUY at center (FIFO)
        buy_tickets = center_level.get_buy_tickets()
        if buy_tickets:
            oldest_buy = buy_tickets[0]  # FIFO
            if self._close_position(oldest_buy):
                self.activity_log.log_info(f"Closed BUY at center (ticket {oldest_buy})")
                self._remove_ticket_from_tracking(oldest_buy, center_level)
        
        # Step 2: Activate grid_level_2
        self.state.grid_level_2 = GridLevel(price=new_price, active=True)
        self.state.phase = "TWO_LEVELS"
        self.state.last_move_direction = "UP_TO_UPPER"
        
        self.activity_log.log_grid_activation("Upper Level", new_price)
        
        # Step 3: Check max_positions
        if self.state.position_counter >= self.max_positions:
            # Try to advance to next set
            if not self.advance_to_next_set():
                self.activity_log.log_info(f"Max positions ({self.max_positions}) reached for all sets - skipping new opens")
                await self.save_state()
                return
            # Successfully advanced to next set, continue with opening
        
        # Open 3 positions at new level
        await self._open_triple_positions(
            self.state.grid_level_2,
            ask, bid,
            direction="UP"  # Opened because we moved up
        )

        self.state.position_counter += 3
        await self._apply_anchor_alignment()
        await self.save_state()


    async def _bounce_down(self, upper_level: GridLevel, lower_level: GridLevel, 
                        ask: float, bid: float):
        """
        Bounce DOWN from upper level to lower level
        
        Actions:
        1. Close SELL at upper level (FIFO)
        2. Open 3 positions at lower level: Pair BS + Single SELL
        """
        self.activity_log.log_info(f"Bouncing DOWN to {lower_level.price:.2f}")
        
        # Step 1: Close SELL at upper (FIFO)
        sell_tickets = upper_level.get_sell_tickets()
        if sell_tickets:
            oldest_sell = sell_tickets[0]
            if self._close_position(oldest_sell):
                self.activity_log.log_info(f"Closed SELL at upper (ticket {oldest_sell})")
                self._remove_ticket_from_tracking(oldest_sell, upper_level)
        
        # Step 2: Check max_positions
        if self.state.position_counter >= self.max_positions:
            # Try to advance to next set
            if not self.advance_to_next_set():
                self.activity_log.log_info(f"Max positions ({self.max_positions}) reached for all sets - skipping new opens")
                self.state.last_move_direction = "DOWN_TO_LOWER"
                await self.save_state()
                return
            # Successfully advanced to next set, continue with opening
            self.activity_log.log_info(f"Advancing to next set - opening positions for {self.get_set_display()}")
        
        self.state.last_move_direction = "DOWN_TO_LOWER"
        
        # Step 3: Open 3 positions at lower
        await self._open_triple_positions(lower_level, ask, bid, direction="DOWN")
        
        self.state.position_counter += 3
        self.state.last_move_direction = "DOWN_TO_LOWER"
        await self.save_state()


    async def _bounce_up(self, lower_level: GridLevel, upper_level: GridLevel,
                        ask: float, bid: float):
        """
        Bounce UP from lower level to upper level
        
        Actions:
        1. Close BUY at lower level (FIFO)
        2. Open 3 positions at upper level: Pair BS + Single BUY
        """
        self.activity_log.log_info(f"Bouncing UP to {upper_level.price:.2f}")
        
        # Step 1: Close BUY at lower (FIFO)
        buy_tickets = lower_level.get_buy_tickets()
        if buy_tickets:
            oldest_buy = buy_tickets[0]
            if self._close_position(oldest_buy):
                self.activity_log.log_info(f"Closed BUY at lower (ticket {oldest_buy})")
                self._remove_ticket_from_tracking(oldest_buy, lower_level)
        
        # Step 2: Check max_positions
        if self.state.position_counter >= self.max_positions:
            # Try to advance to next set
            if not self.advance_to_next_set():
                self.activity_log.log_info(f"Max positions ({self.max_positions}) reached for all sets - skipping new opens")
                self.state.last_move_direction = "UP_TO_UPPER"
                await self.save_state()
                return
            # Successfully advanced to next set, continue with opening
            self.activity_log.log_info(f"Advancing to next set - opening positions for {self.get_set_display()}")
        
        self.state.last_move_direction = "UP_TO_UPPER"
        
        # Step 3: Open 3 positions at upper
        await self._open_triple_positions(upper_level, ask, bid, direction="UP")
        
        self.state.position_counter += 3
        self.state.last_move_direction = "UP_TO_UPPER"
        await self.save_state()


    #position opening helper (triple opens for grid activation and bounces)

    async def _open_triple_positions(self, grid_level: GridLevel, ask: float, bid: float,
                                    direction: str):
        """
        Open 3 positions at a grid level:
        - 1 Pair Buy
        - 1 Pair Sell
        - 1 Single (Buy if direction="UP", Sell if direction="DOWN")
        
        Args:
            grid_level: GridLevel object to store positions in
            ask, bid: Current prices
            direction: "UP" or "DOWN" (determines single trade direction)
        """
        # Pre-entry Volatility Check
        factor = self.volatility_tolerance_factor
        if factor is not None:
            mid = (ask + bid) / 2
            if self.state.grid_level_2 and self.state.grid_level_2.active:
                reference_level_price = self._get_nearest_level_price(mid)
            else:
                reference_level_price = self.state.grid_level_1.price if self.state.grid_level_1 else self.state.center_price
            
            adjusted_distance = self._adjusted_distance(mid, reference_level_price)
            threshold = float(self.grid_distance * self.point) * float(factor)
            
            if adjusted_distance >= threshold:
                self.activity_log.log_info(
                    f"VOLATILITY ABORT (pre-entry): Adjusted distance {adjusted_distance:.5f} from level {reference_level_price:.5f} "
                    f"exceeds threshold {threshold:.5f}. Aborting triple open and triggering nuclear reset."
                )
                self._position_drop_detected = False
                await self._nuclear_reset_and_restart("VOLATILITY_RESET", self.state.realized_pnl)
                return

        target_price = grid_level.price
        open_count = 0
        single_results = []
        
        # Stage index: 0=center pair, 1=first adaptive pair, 2=second adaptive pair...
        pair_stage = (self.state.position_counter // 3) + 1
        single_group = self.state.position_counter // 3

        pair_buy_lot = self._pair_buy_lot_for_stage(pair_stage)
        pair_sell_lot = self._pair_sell_lot_for_stage(pair_stage)
        single_lot = self._single_lot_for_group(single_group)

        if self.num_sets > 1:
            self.activity_log.log_info(
                f"Opening triple on {self.get_set_display()} | "
                f"counter={self.state.position_counter}/{self.max_positions} | "
                f"direction={direction} | pair_stage={pair_stage}, single_group={single_group}"
            )

        # Open Pair Buy
        # When direction="DOWN", this will be the unpaired buy (gets custom buy TP/SL)
        # When direction="UP", this is part of pair (uses global TP/SL)
        tp_override_buy = self.second_entry_buy_tp_pips if direction == "DOWN" else None
        sl_override_buy = self.second_entry_buy_sl_pips if direction == "DOWN" else None
        buy_results = await self._split_and_execute_orders(
            "buy", pair_buy_lot, "PairBuy", target_price,
            tp_pips_override=tp_override_buy,
            sl_pips_override=sl_override_buy
        )
        position_type_buy = 'single_custom' if direction == "DOWN" else 'pair'
        buy_tickets = []
        for (tkt, entry, tp, sl) in buy_results:
            if not tkt:
                continue
            open_count += 1
            grid_level.positions[tkt] = {
                'leg': 'PairBuy',
                'direction': 'buy',
                'entry': entry,
                'tp': tp,
                'sl': sl,
                'lot': pair_buy_lot,
                'position_type': position_type_buy
            }
            self.state.ticket_map[tkt] = grid_level.positions[tkt]
            self._init_touch_flags(tkt)
            self.activity_log.log_fire(
                self.state.cycle_count, "PairBuy", entry,
                pair_buy_lot, tp, sl, tkt
            )
            buy_tickets.append(tkt)
        if len(buy_tickets) > 1:
            group_id = buy_tickets[0]
            self.state.split_group_map[group_id] = list(buy_tickets)
            for idd in buy_tickets:
                if idd in self.state.ticket_map:
                    self.state.ticket_map[idd]['split_group_id'] = group_id

        # Open Pair Sell
        # When direction="UP", this will be the unpaired sell (gets custom sell TP/SL)
        # When direction="DOWN", this is part of pair (uses global TP/SL)
        tp_override_sell = self.second_entry_sell_tp_pips if direction == "UP" else None
        sl_override_sell = self.second_entry_sell_sl_pips if direction == "UP" else None
        sell_results = await self._split_and_execute_orders(
            "sell", pair_sell_lot, "PairSell", target_price,
            tp_pips_override=tp_override_sell,
            sl_pips_override=sl_override_sell
        )
        position_type_sell = 'single_custom' if direction == "UP" else 'pair'
        sell_tickets = []
        for (tkt, entry, tp, sl) in sell_results:
            if not tkt:
                continue
            open_count += 1
            grid_level.positions[tkt] = {
                'leg': 'PairSell',
                'direction': 'sell',
                'entry': entry,
                'tp': tp,
                'sl': sl,
                'lot': pair_sell_lot,
                'position_type': position_type_sell
            }
            self.state.ticket_map[tkt] = grid_level.positions[tkt]
            self._init_touch_flags(tkt)
            self.activity_log.log_fire(
                self.state.cycle_count, "PairSell", entry,
                pair_sell_lot, tp, sl, tkt
            )
            sell_tickets.append(tkt)
        if len(sell_tickets) > 1:
            group_id = sell_tickets[0]
            self.state.split_group_map[group_id] = list(sell_tickets)
            for idd in sell_tickets:
                if idd in self.state.ticket_map:
                    self.state.ticket_map[idd]['split_group_id'] = group_id

        # Open Single (direction-dependent)
        if direction == "UP":
            # Moving UP -> Single BUY (uses global TP/SL)
            single_results = await self._split_and_execute_orders(
                "buy", single_lot, "SingleBuy", target_price
            )
            single_tickets = []
            for (tkt, entry, tp, sl) in single_results:
                if not tkt:
                    continue
                open_count += 1
                grid_level.positions[tkt] = {
                    'leg': 'SingleBuy',
                    'direction': 'buy',
                    'entry': entry,
                    'tp': tp,
                    'sl': sl,
                    'lot': single_lot,
                    'position_type': 'pair'
                }
                self.state.ticket_map[tkt] = grid_level.positions[tkt]
                self._init_touch_flags(tkt)
                self.activity_log.log_fire(
                    self.state.cycle_count, "SingleBuy", entry,
                    single_lot, tp, sl, tkt
                )
                single_tickets.append(tkt)
            if len(single_tickets) > 1:
                group_id = single_tickets[0]
                self.state.split_group_map[group_id] = list(single_tickets)
                for idd in single_tickets:
                    if idd in self.state.ticket_map:
                        self.state.ticket_map[idd]['split_group_id'] = group_id

        elif direction == "DOWN":
            # Moving DOWN -> Single SELL (uses global TP/SL)
            single_results = await self._split_and_execute_orders(
                "sell", single_lot, "SingleSell", target_price
            )
            single_tickets = []
            for (tkt, entry, tp, sl) in single_results:
                if not tkt:
                    continue
                open_count += 1
                grid_level.positions[tkt] = {
                    'leg': 'SingleSell',
                    'direction': 'sell',
                    'entry': entry,
                    'tp': tp,
                    'sl': sl,
                    'lot': single_lot,
                    'position_type': 'pair'
                }
                self.state.ticket_map[tkt] = grid_level.positions[tkt]
                self._init_touch_flags(tkt)
                self.activity_log.log_fire(
                    self.state.cycle_count, "SingleSell", entry,
                    single_lot, tp, sl, tkt
                )
                single_tickets.append(tkt)
            if len(single_tickets) > 1:
                group_id = single_tickets[0]
                self.state.split_group_map[group_id] = list(single_tickets)
                for idd in single_tickets:
                    if idd in self.state.ticket_map:
                        self.state.ticket_map[idd]['split_group_id'] = group_id

        # Post-fill Volatility Check
        factor = self.volatility_tolerance_factor
        if factor is not None:
            # Collect all fill prices from the results of all three legs
            fill_prices = []
            for r_list in (buy_results, sell_results, single_results):
                for (tkt, entry, tp, sl) in r_list:
                    if tkt != 0:
                        fill_prices.append(entry)
            
            # Determine the reference level the same way as Layer 1
            mid = (ask + bid) / 2
            if self.state.grid_level_2 and self.state.grid_level_2.active:
                reference_level_price = self._get_nearest_level_price(mid)
            else:
                reference_level_price = self.state.grid_level_1.price if self.state.grid_level_1 else self.state.center_price
            
            threshold = float(self.grid_distance * self.point) * float(factor)
            for fill_price in fill_prices:
                adjusted_distance = self._adjusted_distance(fill_price, reference_level_price)
                if adjusted_distance >= threshold:
                    self.activity_log.log_info(
                        f"VOLATILITY ABORT (post-fill): Fill price {fill_price:.5f} adjusted distance {adjusted_distance:.5f} "
                        f"from level {reference_level_price:.5f} exceeds threshold {threshold:.5f}. Triggering nuclear reset."
                    )
                    self._position_drop_detected = False
                    await self._nuclear_reset_and_restart("VOLATILITY_RESET", self.state.realized_pnl)
                    return

        self.state.total_positions += open_count
    def _compute_anchors(self) -> tuple[float, float]:
        """
        Compute upper and lower TP/SL anchor prices from the two grid levels.

        Formula (always uses the configured sl_pips and self.point):
            upper_anchor = upper_level_price + (sl_pips * point)
            lower_anchor = lower_level_price - (sl_pips * point)

        Returns (upper_anchor, lower_anchor).
        Requires both grid_level_1 and grid_level_2 to be set.
        """
        level_1 = self.state.grid_level_1.price
        level_2 = self.state.grid_level_2.price
        upper = max(level_1, level_2)
        lower = min(level_1, level_2)
        sl_dist = float(self.sl_pips) * self.point
        return upper + sl_dist, lower - sl_dist

    async def _apply_anchor_alignment(self):
        """
        Apply anchor-based TP/SL to all PAIR positions currently tracked.
        Called once when the 2nd entry fires. Never called again until nuclear reset.

        Rules:
        - upper_anchor, lower_anchor computed from _compute_anchors()
        - BUY positions (position_type == 'pair'):  TP = upper_anchor, SL = lower_anchor
        - SELL positions (position_type == 'pair'): TP = lower_anchor, SL = upper_anchor
        - single_custom positions: NOT touched (keep their own TP/SL from fill price)
        - Sends TRADE_ACTION_SLTP to MT5 for each pair position
        - Updates tp and sl in both grid_level.positions[ticket] and state.ticket_map[ticket]
        """
        upper_anchor, lower_anchor = self._compute_anchors()

        self.activity_log.log_info(
            f"Applying anchor alignment: upper={upper_anchor:.5f}, lower={lower_anchor:.5f} "
            f"(sl_pips={self.sl_pips}, point={self.point})"
        )

        for ticket, info in list(self.state.ticket_map.items()):
            if not info:
                continue
            if info.get('position_type', 'pair') != 'pair':
                continue  # skip single_custom positions

            direction = info.get('direction', '')
            if direction == 'buy':
                new_tp = upper_anchor
                new_sl = lower_anchor
            else:
                new_tp = lower_anchor
                new_sl = upper_anchor

            # Send modification to MT5
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": self.mt5_symbol,
                "position": ticket,
                "tp": float(new_tp),
                "sl": float(new_sl),
            }
            result = mt5.order_send(request)
            if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                info['tp'] = new_tp
                info['sl'] = new_sl
                # Mirror into grid level containers
                for level in [self.state.grid_level_1, self.state.grid_level_2]:
                    if level and ticket in level.positions:
                        level.positions[ticket]['tp'] = new_tp
                        level.positions[ticket]['sl'] = new_sl
            else:
                error = result.comment if result else mt5.last_error()
                self.activity_log.log_error(
                    f"Anchor alignment failed for ticket {ticket} ({direction}): {error}"
                )

    #TP/SL detection helpers (Same as old logic)

    def _update_touch_flags(self, ask: float, bid: float):
        """
        PRESERVED FROM ORIGINAL - Latch touch flags when price crosses TP/SL
        """
        for ticket, info in list(self.state.ticket_map.items()):
            if not info:
                continue
            
            tp_price = info.get("tp", 0)
            sl_price = info.get("sl", 0)
            
            # Skip positions that have not yet received TP/SL (e.g. center pair
            # opened without stops, waiting for 2nd entry alignment to fire)
            if tp_price == 0.0 and sl_price == 0.0:
                continue
                
            direction = info.get("direction", "")
            
            flags = self.state.ticket_touch_flags.get(ticket)
            if flags is None:
                flags = {"tp_touched": False, "sl_touched": False}
                self.state.ticket_touch_flags[ticket] = flags
            
            if direction == "buy":
                if not flags['tp_touched'] and bid >= tp_price:
                    flags['tp_touched'] = True
                if not flags['sl_touched'] and bid <= sl_price:
                    flags['sl_touched'] = True
            else:  # sell
                if not flags['tp_touched'] and ask <= tp_price:
                    flags['tp_touched'] = True
                if not flags['sl_touched'] and ask >= sl_price:
                    flags['sl_touched'] = True


    async def _check_position_drops(self, ask: float, bid: float):
        """
        PRESERVED FROM ORIGINAL - Detect positions closed by MT5 (TP/SL hit)
        
        NEW BEHAVIOR: Selective nuclear reset based on position_type
        - Custom singles (position_type='single_custom') close without triggering reset
        - Pair positions (position_type='pair') trigger nuclear reset
        """
        positions = mt5.positions_get(symbol=self.mt5_symbol)
        current_tickets = set()
        if positions:
            for pos in positions:
                current_tickets.add(pos.ticket)
        
        tracked_tickets = set(self.state.ticket_map.keys())
        dropped = tracked_tickets - current_tickets
        
        processed_groups = set()
        for ticket in dropped:
            info = self.state.ticket_map.get(ticket)
            if not info:
                continue

            group_id = info.get('split_group_id')
            # Handle split-group closure as a single event
            if group_id:
                if group_id in processed_groups:
                    continue
                processed_groups.add(group_id)

                group_tickets = list(self.state.split_group_map.get(group_id, []))
                # Compute realized pnl for all tickets in group (closed or will be closed)
                group_realized = 0.0
                any_pair = False
                for t in group_tickets:
                    tinfo = self.state.ticket_map.get(t)
                    if not tinfo:
                        continue
                    leg = tinfo.get('leg', '')
                    direction = tinfo.get('direction', '')
                    entry = tinfo.get('entry', 0)
                    tp_price = tinfo.get('tp', 0)
                    sl_price = tinfo.get('sl', 0)
                    lot = tinfo.get('lot', 0)
                    position_type = tinfo.get('position_type', 'pair')
                    any_pair = any_pair or (position_type == 'pair')

                    # Determine TP/SL using touch flags when possible
                    flags = self.state.ticket_touch_flags.get(t, {})
                    is_tp = flags.get('tp_touched', False)
                    is_sl = flags.get('sl_touched', False)
                    if not is_tp and not is_sl:
                        check_price = bid if direction == 'buy' else ask
                        tp_dist = abs(check_price - tp_price)
                        sl_dist = abs(check_price - sl_price)
                        is_tp = tp_dist < sl_dist
                        is_sl = not is_tp

                    close_price = tp_price if is_tp else sl_price
                    if direction == 'buy':
                        group_realized += (close_price - entry) * lot
                    else:
                        group_realized += (entry - close_price) * lot

                # Close any remaining open tickets in the group
                for t in list(group_tickets):
                    if t in (current_tickets or set()):
                        # close via broker
                        try:
                            self._close_position(t)
                        except Exception:
                            self.activity_log.log_error(f"Failed to close split-group ticket {t}")
                # Log as single event
                self.state.realized_pnl += group_realized
                if any_pair:
                    self.activity_log.log_sl_hit(ticket, info.get('leg', ''), 0.0, group_realized, triggered_reset=True)
                    self._position_drop_detected = True
                else:
                    self.activity_log.log_sl_hit(ticket, info.get('leg', ''), 0.0, group_realized, triggered_reset=False)

                # Remove all tickets in group from tracking
                for t in list(group_tickets):
                    self._remove_ticket_from_all_levels(t)
                    self.state.total_positions = max(0, self.state.total_positions - 1)
                continue

            # Non-split ticket (original logic)
            leg = info.get("leg", "")
            direction = info.get("direction", "")
            entry = info.get("entry", 0)
            tp_price = info.get("tp", 0)
            sl_price = info.get("sl", 0)
            lot = info.get("lot", 0)
            position_type = info.get("position_type", "pair")  # Default to 'pair' for safety

            # Determine TP or SL using touch flags
            flags = self.state.ticket_touch_flags.get(ticket, {})
            is_tp = flags.get("tp_touched", False)
            is_sl = flags.get("sl_touched", False)

            # Fallback inference
            if not is_tp and not is_sl:
                check_price = bid if direction == "buy" else ask
                tp_dist = abs(check_price - tp_price)
                sl_dist = abs(check_price - sl_price)
                is_tp = tp_dist < sl_dist
                is_sl = not is_tp

            # Calculate PnL
            close_price = tp_price if is_tp else sl_price
            if direction == "buy":
                realized = (close_price - entry) * lot
            else:
                realized = (entry - close_price) * lot

            self.state.realized_pnl += realized

            # Determine if this closure triggers reset
            triggers_reset = (position_type == 'pair')

            # Log with reset trigger indicator
            if is_tp:
                self.activity_log.log_tp_hit(ticket, leg, close_price, realized, "", triggered_reset=triggers_reset)
            else:
                self.activity_log.log_sl_hit(ticket, leg, close_price, realized, triggered_reset=triggers_reset)

            # Remove from tracking
            self._remove_ticket_from_all_levels(ticket)

            # Decrement total (for both pair and custom singles)
            self.state.total_positions -= 1

            # Set reset flag ONLY for pair positions
            if triggers_reset:
                self._position_drop_detected = True
        
        if dropped:
            await self.save_state()

    # Nuclear reset check (SAME but modified for 2-level logic)

    async def _check_nuclear_reset_trigger(self) -> bool:
        """
        Check if ANY position was closed (TP or SL hit)
        If yes -> trigger nuclear reset
        
        Returns True if reset was triggered
        """
        # If any position dropped, _check_position_drops already handled logging
        # Now we just check if total_positions decreased
        
        if self._position_drop_detected:
            self.activity_log.log_info("Position closed via TP/SL - triggering nuclear reset")
            self._position_drop_detected = False
            await self._nuclear_reset_and_restart("TP_SL_HIT", self.state.realized_pnl)
            return True
        
        return False


    async def _nuclear_reset_and_restart(self, reason: str, total_pnl: float):
        """
        PRESERVED BUT MODIFIED FROM ORIGINAL
        
        Nuclear reset - close ALL positions, reset state, then:
        - If graceful_stop is True: stop completely
        - Otherwise: auto-restart new cycle at current price
        """
        old_cycle = self.state.cycle_count
        
        print(f"[RESET] {self.symbol}: Cycle {old_cycle} ended. Reason: {reason}, PnL: ${total_pnl:.2f}")
        
        self.state.phase = "RESETTING"
        self.activity_log.log_phase_transition("*", "RESETTING")
        
        # Close ALL positions
        positions = mt5.positions_get(symbol=self.mt5_symbol)
        closed_count = 0
        if positions:
            for pos in positions:
                if self._close_position(pos.ticket):
                    closed_count += 1
            print(f"[RESET] {self.symbol}: Closed {closed_count}/{len(positions)} positions")
        
        # Log reset
        self.activity_log.log_reset(old_cycle, old_cycle + 1, reason, total_pnl)
        
        # Reset state but increment cycle
        self._reset_state()
        self.state.cycle_count = old_cycle + 1
        
        # Check graceful stop
        if self.graceful_stop:
            self.running = False
            self.graceful_stop = False
            self.state.phase = "IDLE"
            self.activity_log.log_stop(self.state.cycle_count, "graceful_stop_complete")
            await self.save_state()
            print(f"[STOP] {self.symbol}: Graceful stop complete.")
            return
        
        # Auto-restart at CURRENT price (where TP/SL was hit)
        self.running = False  # Reset flag so start() doesn't exit early
        print(f"[RESTART] {self.symbol}: Starting new cycle {self.state.cycle_count}")
        await self.start()


    def _reset_state(self):
        """Reset state to defaults (except cycle_count)"""
        cycle = self.state.cycle_count
        self.state = StrategyState()
        self.state.cycle_count = cycle


    #Helper methods for order execution, position closing, and tracking management (SAME as old logic but adapted for new state structure)

    def _remove_ticket_from_tracking(self, ticket: int, grid_level: GridLevel):
        """Remove ticket from a specific grid level"""
        if ticket in grid_level.positions:
            del grid_level.positions[ticket]
        if ticket in self.state.ticket_map:
            del self.state.ticket_map[ticket]
        if ticket in self.state.ticket_touch_flags:
            del self.state.ticket_touch_flags[ticket]


    def _remove_ticket_from_all_levels(self, ticket: int):
        """Remove ticket from all grid levels and global tracking
        
        Position counter logic:
        - Pair positions: decrement position_counter (counts toward max_positions)
        - Custom single positions: DO NOT decrement position_counter (user requirement)
        - Center positions: always keep position_counter as-is
        """
        info = self.state.ticket_map.get(ticket)
        group_id = info.get('split_group_id') if info else None

        # Remove from any level containers
        if self.state.grid_level_1 and ticket in self.state.grid_level_1.positions:
            del self.state.grid_level_1.positions[ticket]
        if self.state.grid_level_2 and ticket in self.state.grid_level_2.positions:
            del self.state.grid_level_2.positions[ticket]

        # Remove from ticket tracking
        if ticket in self.state.ticket_map:
            del self.state.ticket_map[ticket]
        if ticket in self.state.ticket_touch_flags:
            del self.state.ticket_touch_flags[ticket]

        # If ticket belonged to a split group, only decrement position_counter when the last
        # ticket of the group is removed. Otherwise follow existing logic.
        if group_id:
            lst = self.state.split_group_map.get(group_id, [])
            if ticket in lst:
                try:
                    lst.remove(ticket)
                except ValueError:
                    pass
            if not lst:
                # last ticket removed -> decrement once for pair groups
                if info:
                    position_type = info.get('position_type', 'pair')
                    if position_type == 'pair' and self.state.position_counter > 0:
                        self.state.position_counter -= 1
                # cleanup map
                if group_id in self.state.split_group_map:
                    del self.state.split_group_map[group_id]
            else:
                # update stored list
                self.state.split_group_map[group_id] = lst
            return

        # Fallback: Only decrement position_counter for pair positions (not center, not custom singles)
        if info:
            leg = info.get("leg", "")
            position_type = info.get("position_type", "pair")
            # Center positions don't decrement position_counter
            if leg in {"CenterBuy", "CenterSell"}:
                pass  # Do nothing
            # Pair positions decrement position_counter
            elif position_type == "pair" and self.state.position_counter > 0:
                self.state.position_counter -= 1
            # Custom single positions DO NOT decrement position_counter (per user requirement)


    def _init_touch_flags(self, ticket: int):
        """Initialize touch flags for a new ticket"""
        self.state.ticket_touch_flags[ticket] = {
            "tp_touched": False,
            "sl_touched": False
        }


    def _close_position(self, ticket: int) -> bool:
        """
        PRESERVED FROM ORIGINAL - Close a single MT5 position
        """
        positions = mt5.positions_get(ticket=ticket)
        if not positions:
            return False
        
        pos = positions[0]
        tick = mt5.symbol_info_tick(self.mt5_symbol)
        if not tick:
            return False
        
        if pos.type == mt5.ORDER_TYPE_BUY:
            close_type = mt5.ORDER_TYPE_SELL
            close_price = tick.bid
        else:
            close_type = mt5.ORDER_TYPE_BUY
            close_price = tick.ask
        
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.mt5_symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": ticket,
            "price": close_price,
            "deviation": 50,
            "magic": self.MAGIC_NUMBER,
            "comment": "close",
            "type_filling": mt5.ORDER_FILLING_FOK
        }
        
        result = mt5.order_send(request)
        return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE


    async def _execute_market_order(self, direction: str, lot_size: float,
                                    leg_name: str, target_price: float,
                                    tp_pips_override: Optional[float] = None,
                                    sl_pips_override: Optional[float] = None,
                                    skip_tp_sl: bool = False) -> Tuple[int, float, float, float]:
        """
        PRESERVED FROM ORIGINAL (with minor modifications)
        Send market order to MT5, returns (ticket, entry_price, tp_price, sl_price)
        """
        tick = mt5.symbol_info_tick(self.mt5_symbol)
        if not tick:
            self.activity_log.log_error(f"No tick for {leg_name}")
            return 0, 0.0, 0.0, 0.0
        
        # Determine execution parameters
        if direction == "buy":
            exec_price = tick.ask
            order_type = mt5.ORDER_TYPE_BUY
            check_price = tick.bid
        else:
            exec_price = tick.bid
            order_type = mt5.ORDER_TYPE_SELL
            check_price = tick.ask

        if skip_tp_sl:
            tp = 0.0
            sl = 0.0
        else:
            # Convert user-input pips to price distance using MT5 point size
            # 1 pip = 1 MT5 point (fetched live from broker symbol_info)
            pt = self.point
            tp_pips = tp_pips_override if tp_pips_override is not None else self.tp_pips
            sl_pips = sl_pips_override if sl_pips_override is not None else self.sl_pips
            if direction == "buy":
                tp = exec_price + float(tp_pips) * pt
                sl = exec_price - float(sl_pips) * pt
            else:
                tp = exec_price - float(tp_pips) * pt
                sl = exec_price + float(sl_pips) * pt
        
        if not skip_tp_sl:
            # Stops level safety check (stop_level is 0 on Exness, but kept as a safety guard)
            symbol_info = mt5.symbol_info(self.mt5_symbol)
            if symbol_info:
                point = symbol_info.point
                stops_level = symbol_info.trade_stops_level  # 0 on Exness
                min_dist = stops_level * symbol_info.point
                
                if direction == "buy":
                    if sl > check_price - min_dist:
                        sl = check_price - min_dist
                    if tp < check_price + min_dist:
                        tp = check_price + min_dist
                else:
                    if sl < check_price + min_dist:
                        sl = check_price + min_dist
                    if tp > check_price - min_dist:
                        tp = check_price - min_dist
        
        # Snapshot existing tickets
        positions_before = mt5.positions_get(symbol=self.mt5_symbol)
        existing_tickets = set(pos.ticket for pos in positions_before) if positions_before else set()
        
        # Send order
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.mt5_symbol,
            "volume": float(lot_size),
            "type": order_type,
            "price": exec_price,
            "magic": self.MAGIC_NUMBER,
            "comment": f"{leg_name} C{self.state.cycle_count}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
            "deviation": 200
        }
        if not skip_tp_sl:
            request["sl"] = float(sl)
            request["tp"] = float(tp)
        
        result = mt5.order_send(request)
        
        # Handle MT5 response; on invalid stops, retry once using hardcoded per-asset stop level
        if result is None:
            error = mt5.last_error()
            self.activity_log.log_error(f"{leg_name} order failed: {error}")
            return 0, 0.0, 0.0, 0.0

        if result.retcode != mt5.TRADE_RETCODE_DONE:
            invalid_code = getattr(mt5, 'TRADE_RETCODE_INVALID_STOPS', 10016)
            if result.retcode == invalid_code:
                # On Exness stop_level=0, so this branch is effectively unreachable.
                # Kept as a safety net with a minimal fallback.
                stop_pips = EXNESS_FALLBACK_STOP_PIPS
                symbol_info = mt5.symbol_info(self.mt5_symbol)
                point = symbol_info.point if symbol_info else 0.00001
                fresh_tick = mt5.symbol_info_tick(self.mt5_symbol)
                retry_exec_price = (fresh_tick.ask if direction == 'buy' else fresh_tick.bid) if fresh_tick else exec_price
                if direction == 'buy':
                    new_sl = retry_exec_price - float(stop_pips) * point
                else:
                    new_sl = retry_exec_price + float(stop_pips) * point

                if symbol_info and not skip_tp_sl:
                    stops_level = symbol_info.trade_stops_level  # 0 on Exness
                    min_dist = stops_level * point
                    check_price = (fresh_tick.bid if direction == 'buy' else fresh_tick.ask) if fresh_tick else (tick.bid if direction == 'buy' else tick.ask)
                    if direction == 'buy':
                        if new_sl > check_price - min_dist:
                            new_sl = check_price - min_dist
                    else:
                        if new_sl < check_price + min_dist:
                            new_sl = check_price + min_dist

                retry_req = dict(request)
                retry_req['price'] = float(retry_exec_price)
                if not skip_tp_sl:
                    retry_req['sl'] = float(new_sl)
                    retry_req['tp'] = float(tp)

                retry_res = mt5.order_send(retry_req)
                if retry_res and retry_res.retcode == mt5.TRADE_RETCODE_DONE:
                    self.activity_log.log_info(f"{leg_name}: Order retried with hardcoded stop level ({stop_pips} pips) and succeeded")
                    result = retry_res
                else:
                    err = retry_res.comment if retry_res else mt5.last_error()
                    self.activity_log.log_error(f"{leg_name} order failed after retry: {err}")
                    return 0, 0.0, 0.0, 0.0
            else:
                error = result.comment
                self.activity_log.log_error(f"{leg_name} order failed: {error}")
                return 0, 0.0, 0.0, 0.0
        
        ticket = result.order
        
        # Wait for position to appear
        await asyncio.sleep(0.1)
        
        # Find new position
        positions_after = mt5.positions_get(symbol=self.mt5_symbol)
        actual_entry = exec_price
        actual_ticket = ticket
        
        if positions_after:
            for pos in positions_after:
                if pos.ticket not in existing_tickets:
                    actual_ticket = pos.ticket
                    actual_entry = pos.price_open
                    break
            else:
                for pos in positions_after:
                    if pos.ticket == ticket:
                        actual_ticket = pos.ticket
                        actual_entry = pos.price_open
                        break
        
        # Return the actual ticket, actual entry price, and final TP/SL used (post-clamp)
        return actual_ticket, actual_entry, float(tp), float(sl)


    async def _split_and_execute_orders(self, direction: str, lot_size: float,
                                       leg_name: str, target_price: float,
                                       tp_pips_override: Optional[float] = None,
                                       sl_pips_override: Optional[float] = None,
                                       skip_tp_sl: bool = False) -> List[Tuple[int, float, float, float]]:
        """
        Split large lots into multiple orders not exceeding EXNESS_MAX_LOT and execute sequentially.
        Returns list of (ticket, entry, tp, sl) tuples in call order.
        """
        max_lot = EXNESS_MAX_LOT  # Always 200 for all Exness symbols
        if lot_size <= max_lot:
            res = await self._execute_market_order(direction, lot_size, leg_name, target_price, tp_pips_override, sl_pips_override, skip_tp_sl)
            return [res]

        remaining = float(lot_size)
        chunks = []
        while remaining > 0 and len(chunks) < 20:
            chunk = min(remaining, float(max_lot))
            chunks.append(chunk)
            remaining -= chunk

        results = []
        for chunk in chunks:
            res = await self._execute_market_order(direction, chunk, leg_name, target_price, tp_pips_override, sl_pips_override, skip_tp_sl)
            results.append(res)

        return results

    def _get_nearest_level_price(self, mid: float) -> float:
        if self.state.grid_level_2 and self.state.grid_level_2.active:
            p1 = self.state.grid_level_1.price if self.state.grid_level_1 else self.state.center_price
            p2 = self.state.grid_level_2.price
            return p1 if abs(mid - p1) < abs(mid - p2) else p2
        elif self.state.grid_level_1:
            return self.state.grid_level_1.price
        return self.state.center_price

    def _adjusted_distance(self, price_a: float, price_b: float) -> float:
        return max(0.0, abs(price_a - price_b) - (self._last_known_spread / 2))

    async def _check_volatility_slippage(self, ask: float, bid: float):
        factor = self.volatility_tolerance_factor
        if factor is None:
            return
        if self.state.phase != "TWO_LEVELS":
            return

        mid = (ask + bid) / 2
        nearest_level_price = self._get_nearest_level_price(mid)
        adjusted_distance = self._adjusted_distance(mid, nearest_level_price)
        threshold = float(self.grid_distance * self.point) * float(factor)

        if adjusted_distance >= threshold:
            self.activity_log.log_info(
                f"VOLATILITY RESET: Adjusted distance {adjusted_distance:.5f} from nearest level {nearest_level_price:.5f} "
                f"(spread deduction: {self._last_known_spread / 2:.5f}) exceeds {factor}x threshold {threshold:.5f}. Triggering nuclear reset."
            )
            self._position_drop_detected = False
            await self._nuclear_reset_and_restart("VOLATILITY_RESET", self.state.realized_pnl)



    async def save_state(self):
        """Persist the current strategy state."""
        if self.repository is None:
            self.repository = Repository(self.symbol)
            await self.repository.initialize()

        metadata = json.dumps(
            {
                "phase": self.state.phase,
                "center_price": self.state.center_price,
                "grid_level_1": self.state.grid_level_1.price if self.state.grid_level_1 else 0.0,
                "grid_level_2": self.state.grid_level_2.price if self.state.grid_level_2 else 0.0,
                "position_counter": self.state.position_counter,
                "total_positions": self.state.total_positions,
                "last_move_direction": self.state.last_move_direction,
                "realized_pnl": self.state.realized_pnl,
            }
        )

        await self.repository.save_state(
            phase=self.state.phase,
            center_price=self.state.center_price,
            iteration=self.state.cycle_count,
            cycle_id=self.state.cycle_count,
            anchor_price=self.state.grid_level_1.price if self.state.grid_level_1 else 0.0,
            metadata=metadata,
        )


    #Graceful stop and position terminate (same as old logic)

    async def stop(self):
        """
        PRESERVED FROM ORIGINAL
        Graceful stop - complete current cycle before stopping
        """
        if not self.running:
            return
        
        print(f"[STOP] {self.symbol}: Graceful stop initiated.")
        self.graceful_stop = True
        self.activity_log.log_graceful_stop(self.state.cycle_count, "manual/timeout")
        
        # If idle or no positions, stop immediately
        if self.state.phase == "IDLE" or self.state.total_positions == 0:
            self.running = False
            self.activity_log.log_stop(self.state.cycle_count, "graceful_stop_immediate")
            await self.save_state()
            print(f"[STOP] {self.symbol}: Stopped immediately (no positions).")


    async def terminate(self):
        """
        PRESERVED FROM ORIGINAL
        Nuclear reset - close ALL positions immediately, don't restart
        """
        print(f"[TERMINATE] {self.symbol}: Closing ALL positions...")
        self.activity_log.log_info("TERMINATE: Closing all positions...")
        
        # Close all positions
        positions = mt5.positions_get(symbol=self.mt5_symbol)
        closed_count = 0
        if positions:
            for pos in positions:
                if self._close_position(pos.ticket):
                    closed_count += 1
        
        print(f"[TERMINATE] {self.symbol}: Closed {closed_count} positions.")
        self.activity_log.log_info(f"TERMINATE: Closed {closed_count} positions")
        
        # Full reset
        self._reset_state()
        self.running = False
        self.graceful_stop = False
        self.state.phase = "IDLE"
        self.state.cycle_count = 0
        
        await self.save_state()
        print(f"[TERMINATE] {self.symbol}: Terminated completely.")

    async def close(self):
        """Release persistent resources held by the strategy."""
        if self.repository is not None:
            await self.repository.close()
            self.repository = None


    #Status API

    def get_status(self) -> dict:
        """
        PRESERVED FROM ORIGINAL (with field updates)
        Return status dict for API polling
        """
        return {
            "running": self.running,
            "phase": self.state.phase,
            "cycle_count": self.state.cycle_count,
            "center_price": self.state.center_price,
            "grid_level_1_price": self.state.grid_level_1.price if self.state.grid_level_1 else 0,
            "grid_level_2_price": self.state.grid_level_2.price if self.state.grid_level_2 else 0,
            "open_positions": self.state.total_positions,
            "position_counter": self.state.position_counter,
            "max_positions": self.max_positions,
            "realized_pnl": self.state.realized_pnl,
            "graceful_stop": self.graceful_stop,
            "is_resetting": self.state.phase == "RESETTING",
            "step": self.state.cycle_count,
            "iteration": self.state.cycle_count,
        }
