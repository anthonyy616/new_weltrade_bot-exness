"""
Session Logger for Trading Bot

Provides comprehensive logging of all bot activity for transparency, 
debugging, and protection against user error blame.

Logs are stored per-user in: logs/users/{user_id}/sessions/
Each session creates a human-readable text file with timestamps.
"""

import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional


class SessionLogger:
    """
    Logs all trading activity for a user session.
    
    Creates human-readable log files that track:
    - Configuration changes
    - Button clicks (start, stop, terminate)
    - Trade executions (buy/sell with details)
    - TP/SL hits
    - Session start/end
    """
    
    def __init__(self, user_id: str):
        self.user_id = user_id
        self.session_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
        # [FIX] Use absolute path relative to project root to avoid CWD issues
        # core/session_logger.py -> core -> root -> logs
        root_dir = Path(__file__).resolve().parent.parent
        self.log_dir = root_dir / "logs" / "users" / user_id / "sessions"
        
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log_file = self.log_dir / f"session_{self.session_id}.txt"
        self.trade_count = 0
        self.session_started = False
        
        print(f"[SESSION] Logging to: {self.log_file}")
    
    def _write(self, text: str):
        """Append text to the log file."""
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(text)
    
    def _timestamp(self) -> str:
        """Get current timestamp in readable format."""
        return datetime.now().strftime("%H:%M:%S")
    
    def start_session(self):
        """Write session header."""
        if self.session_started:
            return
        
        header = f"""=====================================
SESSION: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
USER ID: {self.user_id}
=====================================

"""
        self._write(header)
        self.session_started = True
    
    def log(self, message: str):
        """Log a generic message with timestamp."""
        self.start_session()
        self._write(f"[{self._timestamp()}] {message}\n")
    
    def log_config(self, config: Dict[str, Any]):
        """Log configuration in readable format."""
        self.start_session()
        self._write(f"\n[{self._timestamp()}] CONFIG LOADED\n")
        
        # Log global settings
        global_cfg = config.get("global", {})
        if global_cfg:
            self._write(f"  Global Settings:\n")
            for key, val in global_cfg.items():
                self._write(f"    - {key}: {val}\n")
        
        # Log enabled symbols
        symbols = config.get("symbols", {})
        enabled = [sym for sym, cfg in symbols.items() if cfg.get("enabled")]
        if enabled:
            self._write(f"  Enabled Symbols: {', '.join(enabled)}\n")
            for sym in enabled:
                cfg = symbols[sym]
                self._write(f"    {sym}:\n")
                self._write(f"      - Spread: {cfg.get('spread', 20)}\n")
                self._write(f"      - Max Pairs: {cfg.get('max_pairs', 5)}\n")
                self._write(f"      - Max Positions: {cfg.get('max_positions', 5)}\n")
                self._write(f"      - Lot Sizes: {cfg.get('lot_sizes', [0.01])}\n")
        self._write("\n")
    
    def log_button(self, button_name: str, details: str = ""):
        """Log a button click event."""
        self.start_session()
        msg = f"BUTTON: {button_name}"
        if details:
            msg += f" ({details})"
        self._write(f"[{self._timestamp()}] {msg}\n")
    
    def log_trade(self, symbol: str, pair_idx: int, direction: str, 
                  price: float, lot: float, trade_num: int, ticket: int = 0):
        """Log a trade execution."""
        self.start_session()
        self.trade_count += 1
        self._write(f"""[{self._timestamp()}] TRADE #{self.trade_count}
  Symbol: {symbol}
  Pair: {pair_idx}
  Direction: {direction.upper()}
  Price: {price:.2f}
  Lot: {lot}
  Trade #: {trade_num}
  Ticket: {ticket}

""")
    
    def log_tp_sl(self, symbol: str, pair_idx: int, direction: str, 
                  result: str, profit: float = 0, C: int = 0, status: str = ""):
        """Log TP or SL hit with pair status for debugging."""
        self.start_session()
        result_type = "TP HIT" if result == "tp" else "SL HIT"
        profit_str = f"+${profit:.2f}" if profit > 0 else f"${profit:.2f}"
        
        # Concise summary line for quick debugging
        leg = "B" if direction.upper() == "BUY" else "S"
        status_str = f", Status: {status}" if status else ""
        self._write(f"[{self._timestamp()}] {result_type} for {leg}{pair_idx}, C={C}{status_str}\n")
        
        # Detailed breakdown
        self._write(f"""  Symbol: {symbol}
  Pair: {pair_idx}
  Direction: {direction.upper()}
  Profit: {profit_str}

""")
    
    def log_terminate(self, symbol: str, positions_closed: int):
        """Log a terminate/nuclear reset event."""
        self.start_session()
        self._write(f"[{self._timestamp()}] TERMINATED: {symbol} - {positions_closed} positions closed\n")
    
    def end_session(self, reason: str = "User stopped"):
        """Write session footer."""
        self.start_session()
        duration = "Unknown"  # Could calculate if we tracked start time
        self._write(f"""
[{self._timestamp()}] SESSION ENDED
  Reason: {reason}
  Total Trades: {self.trade_count}
=====================================
""")
    
    def get_sessions(self) -> List[Dict[str, str]]:
        """Get list of all session files for this user."""
        sessions = []
        if self.log_dir.exists():
            for file in sorted(self.log_dir.glob("session_*.txt"), reverse=True):
                sessions.append({
                    "id": file.stem,
                    "name": file.name,
                    "path": str(file)
                })
        return sessions
    
    def get_session_content(self, session_id: str) -> Optional[str]:
        """Get contents of a specific session log."""
        log_path = self.log_dir / f"{session_id}.txt"
        if log_path.exists():
            return log_path.read_text(encoding="utf-8")
        return None
