"""Strategy orchestration for grid-bounce engine."""

# pyright: reportAttributeAccessIssue=false

from typing import Any, Dict, List, Set
import asyncio
import time
from core.engine.grid_bounce_strategy_engine import GridBounceStrategyEngine as GridStrategy
from core.session_logger import SessionLogger


class StrategyOrchestrator:
    """
    Per-user orchestrator that manages multiple strategies (one per symbol).
    Works with the new multi-asset config structure.
    Includes session logging for transparency and debugging.
    """
    
    def __init__(self, config_manager, user_id: str = "default"):
        self.config_manager = config_manager
        self.user_id = user_id
        # Map symbol -> GridStrategy
        self.strategies: Dict[str, GridStrategy] = {}
        self.active_symbols: Set[str] = set()
        
        # Session Logger for history tracking
        self.session_logger = SessionLogger(user_id)
        
        # Initialize
        self.update_strategies()

    @property
    def config(self):
        """Pass-through to config manager for the API"""
        return self.config_manager.get_config()

    def update_strategies(self):
        """
        Syncs active strategies with the configuration.
        Spawns new bots for enabled symbols, removes disabled ones.
        """
        # Get enabled symbols from new config structure
        enabled_symbols = set(self.config_manager.get_enabled_symbols())
        current_symbols = set(self.strategies.keys())

        # 1. Remove disabled symbols
        to_remove = current_symbols - enabled_symbols
        for sym in to_remove:
            print(f"[ORCHESTRATOR] Stopping Strategy: {sym}")
            strategy = self.strategies.get(sym)
            if strategy and strategy.running:
                # Best-effort graceful stop before dropping the reference.
                asyncio.create_task(strategy.stop())
            del self.strategies[sym]

        # 2. Add newly enabled symbols
        to_add = enabled_symbols - current_symbols
        for sym in to_add:
            sym_config = self.config_manager.get_symbol_config(sym)
            if sym_config:
                print(f"[ORCHESTRATOR] Spawning Strategy: {sym}")
                strategy = GridStrategy(self.config_manager, sym, self.user_id, session_logger=self.session_logger)
                self.strategies[sym] = strategy

        self.active_symbols = enabled_symbols

    async def start(self):
        """Start all enabled strategies"""
        self.update_strategies()
        self.session_logger.log_button("Start All")
        tasks = [bot.start() for bot in self.strategies.values()]
        if tasks:
            await asyncio.gather(*tasks)

    async def stop(self):
        """Stop all strategies (graceful - completes open pairs)"""
        self.session_logger.log_button("Graceful Stop All")
        tasks = [bot.stop() for bot in self.strategies.values()]
        if tasks:
            await asyncio.gather(*tasks)

    async def start_symbol(self, symbol: str):
        """Start a specific symbol strategy"""
        if symbol not in self.strategies:
            sym_config = self.config_manager.get_symbol_config(symbol)
            if sym_config and sym_config.get('enabled', False):
                print(f"[ORCHESTRATOR] Spawning Strategy: {symbol}")
                strategy = GridStrategy(self.config_manager, symbol, self.user_id, session_logger=self.session_logger)
                self.strategies[symbol] = strategy
                self.active_symbols.add(symbol)
        
        if symbol in self.strategies:
            self.session_logger.log_button(f"Start {symbol}")
            await self.strategies[symbol].start()

    async def stop_symbol(self, symbol: str):
        """Stop a specific symbol strategy (graceful)"""
        if symbol in self.strategies:
            self.session_logger.log_button(f"Stop {symbol}")
            await self.strategies[symbol].stop()
            if not self.strategies[symbol].running:
                del self.strategies[symbol]
                self.active_symbols.discard(symbol)

    async def terminate_symbol(self, symbol: str):
        """
        Nuclear reset - close all positions for a symbol immediately.
        Calls terminate() on the strategy which closes all positions and resets grid.
        """
        print(f"[TERMINATE] Starting terminate for {symbol}")
        if symbol in self.strategies:
            self.session_logger.log_button(f"Terminate {symbol}")
            await self.strategies[symbol].terminate()
            del self.strategies[symbol]
            self.active_symbols.discard(symbol)
            print(f"[TERMINATE] {symbol}: Strategy terminated and removed.")
        else:
            print(f"[TERMINATE] {symbol}: Strategy not found in active strategies.")

    async def terminate_all(self):
        """
        Nuclear reset - close all positions for ALL active symbols.
        Robust implementation: Continues even if one strategy fails.
        """
        print(f"[TERMINATE ALL] Terminating {len(self.strategies)} symbols...")
        self.session_logger.log_button("Terminate All")
        
        if not self.strategies:
            print("[TERMINATE ALL] No active strategies to terminate.")
            return
        
        # [FIX] Define safe wrapper to ensure all tasks attempt to run
        async def safe_terminate(name, strat):
            try:
                await strat.terminate()
                close_fn = getattr(strat, "close", None)
                if close_fn is not None:
                    await close_fn()
                return True
            except Exception as e:
                print(f"[ERROR] Failed to terminate {name}: {e}")
                return False

        tasks = [safe_terminate(name, strategy) for name, strategy in self.strategies.items()]
        
        if tasks:
            # Use return_exceptions=True implicitly via safe wrapper
            # But utilizing gather ensures parallel execution
            await asyncio.gather(*tasks)
        
        self.strategies.clear()
        self.active_symbols.clear()
        
        # [NUCLEAR FALLBACK] Scan entire account for ANY remaining positions and close them
        # This handles orphaned positions from symbols that are no longer in 'strategies'
        import MetaTrader5 as mt5
        all_positions = mt5.positions_get()
        if all_positions:
            print(f"[TERMINATE ALL] Found {len(all_positions)} residual positions on account. Closing (Nuclear)...")
            count = 0
            for pos in all_positions:
                # Construct generic close request
                tick = mt5.symbol_info_tick(pos.symbol)
                if not tick:
                    continue
                
                close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                close_price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask
                
                request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": pos.symbol,
                    "position": pos.ticket,
                    "volume": pos.volume,
                    "type": close_type,
                    "price": close_price,
                    "deviation": 50,
                    "magic": pos.magic,
                    "comment": "Terminate-All",
                }
                
                res = mt5.order_send(request)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    count += 1
                else:
                    print(f"[ERROR] Failed to close orphan {pos.ticket} ({pos.symbol}): {res.comment if res else 'Unknown'}")
            print(f"[TERMINATE ALL] Cleaned up {count} residual positions.")

        print("[TERMINATE ALL] All strategies terminated (or attempted).")

    async def close(self):
        """Release any open resources held by strategies and repositories."""
        tasks = []
        for strategy in self.strategies.values():
            close_fn = getattr(strategy, "close", None)
            if close_fn is not None:
                tasks.append(close_fn())

        if tasks:
            await asyncio.gather(*tasks)

        self.strategies.clear()
        self.active_symbols.clear()

    async def start_ticker(self):
        """
        Called when config updates. Re-syncs strategies and notifies them.
        """
        self.update_strategies()
        tasks = [bot.start_ticker() for bot in self.strategies.values()]
        if tasks:
            await asyncio.gather(*tasks)

    async def on_external_tick(self, symbol, tick_data):
        """Routes the tick to the specific strategy for this symbol."""
        if symbol in self.strategies:
            await self.strategies[symbol].on_external_tick(tick_data)

    def get_active_symbols(self) -> List[str]:
        """Returns symbols that are currently active AND running."""
        return [sym for sym, strategy in self.strategies.items() if strategy.running]

    def get_status(self) -> Dict[str, Any]:
        """
        Returns status for all active strategies.
        For multi-asset, returns per-symbol status in a 'strategies' dict.
        """
        if not self.strategies:
            return {
                "running": False,
                "graceful_stop": False,
                "current_price": 0,
                "open_positions": 0,
                "step": 0,
                "iteration": 0,
                "is_resetting": False,
                "strategies": {}
            }

        # Aggregate stats
        total_positions = 0
        running_any = False
        is_resetting_any = False
        graceful_stop_any = False
        per_symbol_status = {}
        
        for symbol, bot in self.strategies.items():
            s = bot.get_status()
            per_symbol_status[symbol] = s
            total_positions += s.get('open_positions', 0)
            if s.get('running', False):
                running_any = True
            if s.get('is_resetting', False):
                is_resetting_any = True
            if s.get('graceful_stop', False):
                graceful_stop_any = True
        
        # For backward compatibility, use first bot for single-value fields
        first_bot = list(self.strategies.values())[0] if self.strategies else None
        first_status = first_bot.get_status() if first_bot else {}

        return {
            "running": running_any,
            "graceful_stop": graceful_stop_any,
            "current_price": first_bot.current_price if first_bot else 0,
            "open_positions": total_positions,
            "step": first_status.get('step', 0),
            "iteration": first_status.get('iteration', 0),
            "is_resetting": is_resetting_any,
            "active_count": len(self.strategies),
            "strategies": per_symbol_status
        }
