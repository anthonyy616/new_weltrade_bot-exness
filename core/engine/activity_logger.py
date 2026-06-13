"""
Activity Logger for Pair Strategy Engine

Logs all trading activity to downloadable files in plain English.
Designed to be readable by anyone — no technical jargon.
"""

import os
from datetime import datetime
from typing import Optional

# Friendly names for position legs
LEG_NAMES = {
      "CenterBuy": "Center Buy (Startup)",
      "CenterSell": "Center Sell (Startup)",
      "PairBuy": "Pair Buy",
      "PairSell": "Pair Sell",
      "SingleBuy": "Single Buy",
      "SingleSell": "Single Sell",
  }


class ActivityLogger:
    """
    Per-symbol activity logging with timestamped, downloadable files.

    Log files stored in: logs/users/{user_id}/sessions/{symbol}_{date}.log
    """

    def __init__(self, symbol: str, user_id: str = "default", session_logger=None):
        self.symbol = symbol
        self.user_id = user_id
        self.session_logger = session_logger

        # [FIX] Use absolute path relative to project root to avoid CWD issues
        # core/engine/activity_logger.py -> core/engine -> core -> root -> logs
        from pathlib import Path
        root_dir = Path(__file__).resolve().parent.parent.parent
        self.log_dir = root_dir / "logs" / "users" / user_id / "sessions"

        # Ensure directory exists
        os.makedirs(self.log_dir, exist_ok=True)

        # Generate filename with date
        date_str = datetime.now().strftime("%Y-%m-%d")
        safe_symbol = symbol.replace(" ", "_")
        # Prefix with 'activity_' so we can distinguish from session logs
        self.log_file = self.log_dir / f"activity_{safe_symbol}_{date_str}.log"

    # Grid-Level Transitions
    def log_grid_activation(self, level_name: str, price: float):
      """Log when a new grid level becomes active"""
      self._write(f"Grid level activated: {level_name} @ {price:.5f}")
    # ========================
    # INTERNAL LOGGING METHODS

    def _friendly_leg(self, leg: str) -> str:
        """Convert leg code to friendly name"""
        return LEG_NAMES.get(leg, leg)

    def _friendly_direction(self, direction: str) -> str:
        """Convert direction to friendly name"""
        return "BUY" if direction == "buy" else "SELL"

    def _write(self, entry: str):
        """Write timestamped entry to log file"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        line = f"  {timestamp}  {entry}\n"

        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(line)

        # Also print to console
        print(f"[{self.symbol}] {entry}")

        # Also write to session log if available
        if self.session_logger:
            self.session_logger.log(f"[{self.symbol}] {entry}")

    def _write_header(self, text: str):
        """Write a prominent section header"""
        border = "=" * 60
        self._write(border)
        self._write(f"  {text}")
        self._write(border)

    def _write_separator(self):
        """Write a light separator between events"""
        self._write("-" * 40)

    # ========================
    # FIRE EVENTS
    # ========================

    def log_fire(self, cycle: int, leg_name: str, price: float, lot: float,
                 tp: float, sl: float, ticket: int = 0):
        """Log a position opening (atomic fire)"""
        friendly = self._friendly_leg(leg_name)
        
        self._write(
            f"Opened {friendly} @ {price:.5f}  |  Lot: {lot:.2f}"
        )

    def log_second_fire(self, cycle: int, price: float):
        """Log the second atomic fire (grid distance reached)"""
        self._write_separator()
        self._write(
            f"Price moved to {price:.5f} — grid distance reached. Opening 2nd pair..."
        )

    # ========================
    # TP/SL EVENTS
    # ========================

    def log_tp_hit(self, ticket: int, leg: str, tp_price: float,
                   realized_pnl: float, action: str = "", triggered_reset: bool = False):
        """Log a take profit hit
        
        Args:
            triggered_reset: If True, indicates this closure triggered a nuclear reset
        """
        friendly = self._friendly_leg(leg)
        result = "profit" if realized_pnl >= 0 else "loss"
        action_str = f" {action}" if action else ""
        reset_status = " - **Nuclear reset triggered**" if triggered_reset else " - Grid continues"
        self._write(
            f"{friendly} hit TP{action_str} @ {tp_price:.5f}  |  "
            f"Result: ${realized_pnl:+.2f} ({result}){reset_status}"
        )

    def log_sl_hit(self, ticket: int, leg: str, sl_price: float,
                   realized_pnl: float, action: str = "", triggered_reset: bool = False):
        """Log a stop loss hit
        
        Args:
            triggered_reset: If True, indicates this closure triggered a nuclear reset
        """
        friendly = self._friendly_leg(leg)
        action_str = f" {action}" if action else ""
        reset_status = " - **Nuclear reset triggered**" if triggered_reset else " - Grid continues"
        self._write(
            f"{friendly} hit SL{action_str} @ {sl_price:.5f}  |  "
            f"Result: ${realized_pnl:+.2f} (loss){reset_status}"
        )

    def log_single_buy_opened(self, cycle: int, price: float, lot: float,
                               tp: float, sl: float, ticket: int = 0):
        """Log recovery single buy opening (legacy — kept for compatibility)"""
        self._write(
            f"Opened Recovery BUY @ {price:.5f}  |  Lot: {lot:.2f}"
        )

    # ========================
    # LIQUIDATION PRICE EVENTS (legacy — kept for compatibility)
    # ========================

    def log_liquidation_calc(self, profit_price: float, loss_price: float,
                             net_lots: float, realized_pnl: float):
        """Log calculated liquidation prices"""
        self._write(
            f"Calculated exit prices — Profit target at: {profit_price:.2f}  |  "
            f"Loss limit at: {loss_price:.2f}  |  Running P&L: ${realized_pnl:.2f}"
        )

    # ========================
    # THRESHOLD EVENTS (legacy — kept for compatibility)
    # ========================

    def log_threshold_hit(self, threshold_type: str, price: float,
                          total_pnl: float):
        """Log when max profit/loss threshold is hit"""
        friendly_type = {
            "MAX_PROFIT": "Maximum profit target",
            "MAX_LOSS": "Maximum loss limit",
        }.get(threshold_type, threshold_type)

        self._write(
            f"{friendly_type} reached at price {price:.2f}  |  "
            f"Total P&L: ${total_pnl:+.2f}"
        )

    # ========================
    # RESET/LIFECYCLE EVENTS
    # ========================

    def log_reset(self, old_cycle: int, new_cycle: int, reason: str,
                  total_pnl: float):
        """Log nuclear reset and restart"""
        friendly_reasons = {
            "ALL_CLOSED": "All trades closed naturally",
            "PROTECTION_DISTANCE": "Price reversed past protection level — safety reset",
            "SINGLE_FIRE_CLOSED": "Recovery trade completed (TP or SL hit)",
            "MAX_PROFIT": "Maximum profit target reached",
            "MAX_LOSS": "Maximum loss limit reached",
            "VOLATILITY_RESET": "Volatility/slippage threshold exceeded — automatic safety reset",
        }
        friendly_reason = friendly_reasons.get(reason, reason)

        self._write_separator()
        self._write(
            f"Cycle #{old_cycle} ended  |  Reason: {friendly_reason}  |  "
            f"Cycle P&L: ${total_pnl:+.2f}"
        )
        self._write(f"Starting new cycle #{new_cycle}...")
        self._write_separator()

    def log_graceful_stop(self, cycle: int, reason: str):
        """Log graceful stop activation"""
        self._write(
            "Graceful stop requested — bot will stop after all open trades close."
        )

    def log_start(self, cycle: int, start_price: float):
        """Log strategy start"""
        self._write_header(
            f"CYCLE #{cycle} STARTED  |  {self.symbol}  |  Entry price: {start_price:.2f}"
        )

    def log_stop(self, cycle: int, reason: str = "manual"):
        """Log strategy stop"""
        friendly_reasons = {
            "manual": "Manually stopped by user",
            "graceful_stop_immediate": "Graceful stop — no open trades, stopped immediately",
            "all_closed_graceful_stop": "Graceful stop — all trades closed, bot stopped",
            "graceful_stop_complete": "Graceful stop completed",
        }
        friendly_reason = friendly_reasons.get(reason, reason)
        self._write_header(f"BOT STOPPED  |  {friendly_reason}")

    # ========================
    # INFO/DEBUG
    # ========================

    def log_info(self, message: str):
        """Log general info message"""
        self._write(message)

    def log_error(self, message: str):
        """Log error message"""
        self._write(f"ERROR: {message}")

    def log_phase_transition(self, old_phase: str, new_phase: str):
        """Log phase state transition"""
        friendly_phases = {
            "IDLE": "Idle",
            "FIRST_FIRE": "Opening first pair of trades",
            "AWAITING_SECOND": "Waiting for price to reach grid distance",
            "PAIRS_COMPLETE": "All 4 trades open — monitoring triggers",
            "PAIRS_COMPLETE (partial)": "Partial trades open (graceful stop active)",
            "MONITORING": "Recovery trade placed — waiting for outcome",
            "RESETTING": "Resetting for new cycle",
        }
        new_friendly = friendly_phases.get(new_phase, new_phase)
        self._write(f"Status: {new_friendly}")
