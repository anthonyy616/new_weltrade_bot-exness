"""
Run State Persistence Module

Persists and restores trading bot run state across server restarts.
Ensures bots automatically resume trading after a crash.
"""

import json
import os
from typing import Dict, Any, Optional, List
from datetime import datetime

RUN_STATE_FILE = "run_state.json"


class RunStateManager:
    """
    Manages persistent run state for all users.
    
    State Structure:
    {
        "user_id_1": {
            "running": true,
            "active_symbols": ["FX Vol 20", "FX Vol 40"],
            "started_at": "2025-12-16T01:45:00",
            "last_updated": "2025-12-16T01:50:00"
        },
        ...
    }
    """
    
    def __init__(self, state_file: str = RUN_STATE_FILE):
        self.state_file = state_file
        self.state: Dict[str, Any] = {}
        self.load_state()
    
    def load_state(self):
        """Load run state from disk"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    self.state = json.load(f)
                print(f" Loaded run state: {len(self.state)} users")
            except Exception as e:
                print(f" Error loading run state: {e}")
                self.state = {}
        else:
            self.state = {}
    
    def save_state(self):
        """Persist run state to disk"""
        try:
            with open(self.state_file, 'w') as f:
                json.dump(self.state, f, indent=2)
        except Exception as e:
            print(f" Error saving run state: {e}")
    
    def set_running(self, user_id: str, active_symbols: List[str]):
        """Mark user's bot as running with specific symbols"""
        now = datetime.now().isoformat()
        
        if user_id not in self.state:
            self.state[user_id] = {"started_at": now}
        
        self.state[user_id].update({
            "running": True,
            "active_symbols": active_symbols,
            "last_updated": now
        })
        self.save_state()
        print(f"Saved run state: {user_id} → {active_symbols}")
    
    def set_stopped(self, user_id: str):
        """Mark user's bot as stopped"""
        if user_id in self.state:
            self.state[user_id]["running"] = False
            self.state[user_id]["last_updated"] = datetime.now().isoformat()
            self.save_state()
            print(f" Saved stopped state: {user_id}")
    
    def get_user_state(self, user_id: str) -> Optional[Dict[str, Any]]:
        """Get run state for a specific user"""
        return self.state.get(user_id)
    
    def was_running(self, user_id: str) -> bool:
        """Check if user's bot was running before restart"""
        user_state = self.state.get(user_id, {})
        return user_state.get("running", False)
    
    def get_active_symbols(self, user_id: str) -> List[str]:
        """Get list of symbols that were running for a user"""
        user_state = self.state.get(user_id, {})
        return user_state.get("active_symbols", [])
    
    def get_all_running_users(self) -> List[str]:
        """Get all user IDs that had running bots"""
        return [
            user_id for user_id, state in self.state.items()
            if state.get("running", False)
        ]


# Global singleton instance
run_state_manager = RunStateManager()
