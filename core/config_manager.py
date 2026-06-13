import json
import os
from typing import Dict, Any, List, Optional

# All available trading symbols
AVAILABLE_SYMBOLS = [
    # FX Indices
    "FX Vol 20", "FX Vol 40", "FX Vol 60", "FX Vol 80", "FX Vol 99",
    # SFX Indices
    "SFX Vol 20", "SFX Vol 40", "SFX Vol 60", "SFX Vol 80", "SFX Vol 99",
    # FlipX Indices
    "FlipX 1", "FlipX 2", "FlipX 3", "FlipX 4", "FlipX 5",
    # PainX Indices
    "PainX 400", "PainX 600", "PainX 800", "PainX 999", "PainX 1200",
    # GainX Indices
    "GainX 400", "GainX 600", "GainX 800", "GainX 999", "GainX 1200",
    # Other Indices
    "SwitchX 600", "SwitchX 1200", "SwitchX 1800", "BreakX 1200", "BreakX 1800"
]

MAX_POSITION_LIMIT = 60

def get_default_symbol_config() -> Dict[str, Any]:
      return {
          "enabled": False,
          "grid_distance": 50.0,       # Pips between grid levels
          "tp_pips": 150.0,            # TP distance for all positions (global)
          "sl_pips": 200.0,            # SL distance for all positions (global)
          # Second-entry (directional single) TP/SL overrides (per-symbol, global)
          "second_entry_buy_tp_pips": 150.0,
          "second_entry_buy_sl_pips": 200.0,
          "second_entry_sell_tp_pips": 150.0,
          "second_entry_sell_sl_pips": 200.0,
          # Sets: number of position sets (default 1)
          "sets": 1,
          # Sets configuration: array of {lot sizes, max_positions} per set
          "sets_config": [
              {
                  "pair_buy_lots": [0.01, 0.01],
                  "pair_sell_lots": [0.01, 0.01],
                  "single_lots": [0.01],
                  "max_positions": 3,
              }
          ],
          # Legacy fields (kept for backward compat, not used if sets_config exists)
          "pair_buy_lots": [0.01, 0.01],
          "pair_sell_lots": [0.01, 0.01],
          "single_lots": [0.01],
          "max_positions": 3,          # Effective positions; must be multiple of 3 (3..60)
      }

class ConfigManager:
    """
    Multi-Asset Configuration Manager
    
    Structure:
    {
        "global": {
            "max_runtime_minutes": 0
        },
        "symbols": {
            "FX Vol 20": { ...symbol config... },
            "FX Vol 40": { ...symbol config... },
            ...
        }
    }
    """
    
    def __init__(self, user_id: str = "default", config_file: str = "config.json"):
        self.user_id = user_id
        
        # If a specific user is logged in, use their unique config file
        if user_id and user_id != "default":
            self.config_file = f"config_{user_id}.json"
        else:
            self.config_file = config_file
            
        self.config: Dict[str, Any] = {}
        self.load_config()

    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    loaded = json.load(f)
                    
                # Check if it's the new multi-asset format
                # New format has "symbols" as a DICT, old format has it as a LIST
                symbols_data = loaded.get("symbols")
                is_new_format = isinstance(symbols_data, dict) and "global" in loaded
                
                if is_new_format:
                    self.config = loaded
                else:
                    # Migrate from old format (symbols is a list or missing)
                    print(f"[CONFIG] Migrating config to multi-asset format...")
                    self.config = self._migrate_old_config(loaded)
                    self.save_config()
                    
            except Exception as e:
                print(f"[CONFIG] Error loading config {self.config_file}: {e}")
                self.config = self._get_defaults()
        else:
            print(f"[CONFIG] Creating new config file: {self.config_file}")
            self.config = self._get_defaults()
            self.save_config()

    def _migrate_old_config(self, old_config: Dict[str, Any]) -> Dict[str, Any]:
        """Migrate from old single-asset config to new multi-asset format"""
        new_config = self._get_defaults()
        
        # Migrate global settings
        if "max_runtime_minutes" in old_config:
            new_config["global"]["max_runtime_minutes"] = old_config["max_runtime_minutes"]
        
        # Migrate old symbols to new format
        old_symbols = old_config.get("symbols", ["FX Vol 20"])
        for symbol in old_symbols:
            if symbol in new_config["symbols"]:
                sym_cfg = new_config["symbols"][symbol]
                sym_cfg["enabled"] = True
                sym_cfg["spread"] = old_config.get("spread", 20.0)
                sym_cfg["max_positions"] = old_config.get("max_positions", 5)
                sym_cfg["buy_stop_tp"] = old_config.get("buy_stop_tp", 50.0)
                sym_cfg["buy_stop_sl"] = old_config.get("buy_stop_sl", 75.0)
                sym_cfg["sell_stop_tp"] = old_config.get("sell_stop_tp", 50.0)
                sym_cfg["sell_stop_sl"] = old_config.get("sell_stop_sl", 75.0)
                sym_cfg["hedge_enabled"] = old_config.get("hedge_enabled", True)
                sym_cfg["hedge_lot_size"] = old_config.get("hedge_lot_size", 0.01)
                
                # Migrate lot sizes (old format was center_lot_first, etc.)
                max_pos = sym_cfg["max_positions"]
                groups = max(1, int(max_pos) // 3)
                # pair_* arrays: center + groups. single_* arrays: groups.
                base_pair_buy = old_config.get("pair_buy_lot", 0.01)
                base_pair_sell = old_config.get("pair_sell_lot", 0.01)
                base_single = old_config.get("single_lot", 0.01)
                sym_cfg["pair_buy_lots"] = [float(base_pair_buy)] * (groups + 1)
                sym_cfg["pair_sell_lots"] = [float(base_pair_sell)] * (groups + 1)
                sym_cfg["single_lots"] = [float(base_single)] * groups
                
        return new_config

    def save_config(self):
        try:
            with open(self.config_file, 'w') as f:
                json.dump(self.config, f, indent=4)
        except Exception as e:
            print(f" Error saving config: {e}")

    def update_config(self, new_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Update config with new values.
        Handles both flat updates and nested symbol updates.
        Supports the new sets_config structure for multi-set trading.
        """
        # Handle global settings
        if "global" in new_config:
            # Validate volatility_tolerance if provided
            global_updates = dict(new_config["global"])
            if "volatility_tolerance" in global_updates:
                val = global_updates.get("volatility_tolerance")
                if val not in ("off", "1.5", "1.75", "2.0", "2.25", "2.5"):
                    # reject invalid value by removing it
                    global_updates.pop("volatility_tolerance", None)
            self.config["global"].update(global_updates)
        
        # Handle symbol-specific settings
        if "symbols" in new_config:
            for symbol, sym_cfg in new_config["symbols"].items():
                if symbol in self.config["symbols"]:
                    # Merge provided fields (careful not to overwrite sets_config yet)
                    self.config["symbols"][symbol].update(sym_cfg)

                    # Validate grid_distance: must be > 0
                    grid_dist = self.config["symbols"][symbol].get("grid_distance", 50.0)
                    self.config["symbols"][symbol]["grid_distance"] = max(1.0, float(grid_dist))

                    # Validate tp_pips and sl_pips: must be > 0
                    tp = self.config["symbols"][symbol].get("tp_pips", 150.0)
                    sl = self.config["symbols"][symbol].get("sl_pips", 200.0)
                    self.config["symbols"][symbol]["tp_pips"] = max(1.0, float(tp))
                    self.config["symbols"][symbol]["sl_pips"] = max(1.0, float(sl))

                    # Validate and set second-entry (directional single) TP/SL values
                    seb_tp = sym_cfg.get("second_entry_buy_tp_pips", self.config["symbols"][symbol].get("second_entry_buy_tp_pips", 150.0))
                    seb_sl = sym_cfg.get("second_entry_buy_sl_pips", self.config["symbols"][symbol].get("second_entry_buy_sl_pips", 200.0))
                    ses_tp = sym_cfg.get("second_entry_sell_tp_pips", self.config["symbols"][symbol].get("second_entry_sell_tp_pips", 150.0))
                    ses_sl = sym_cfg.get("second_entry_sell_sl_pips", self.config["symbols"][symbol].get("second_entry_sell_sl_pips", 200.0))

                    # Clamp sensible ranges (1 - 5000 pips)
                    try:
                        self.config["symbols"][symbol]["second_entry_buy_tp_pips"] = max(1.0, min(5000.0, float(seb_tp)))
                    except Exception:
                        self.config["symbols"][symbol]["second_entry_buy_tp_pips"] = 150.0
                    try:
                        self.config["symbols"][symbol]["second_entry_buy_sl_pips"] = max(1.0, min(5000.0, float(seb_sl)))
                    except Exception:
                        self.config["symbols"][symbol]["second_entry_buy_sl_pips"] = 200.0
                    try:
                        self.config["symbols"][symbol]["second_entry_sell_tp_pips"] = max(1.0, min(5000.0, float(ses_tp)))
                    except Exception:
                        self.config["symbols"][symbol]["second_entry_sell_tp_pips"] = 150.0
                    try:
                        self.config["symbols"][symbol]["second_entry_sell_sl_pips"] = max(1.0, min(5000.0, float(ses_sl)))
                    except Exception:
                        self.config["symbols"][symbol]["second_entry_sell_sl_pips"] = 200.0

                    # Handle sets configuration
                    num_sets = int(sym_cfg.get("sets", self.config["symbols"][symbol].get("sets", 1)))
                    num_sets = max(1, min(10, num_sets))  # Clamp 1-10 sets
                    self.config["symbols"][symbol]["sets"] = num_sets
                    
                    # Initialize or rebuild sets_config
                    if "sets_config" in sym_cfg and isinstance(sym_cfg["sets_config"], list):
                        # User provided sets_config, use it (validate count)
                        sets_config = sym_cfg["sets_config"][:num_sets]
                    else:
                        # Rebuild sets_config from provided lot arrays or existing config
                        sets_config = self.config["symbols"][symbol].get("sets_config", [])
                    
                    # Ensure we have exactly num_sets entries
                    while len(sets_config) < num_sets:
                        # Add default or clone last set with new max_positions
                        if sets_config:
                            new_set = {
                                "pair_buy_lots": sets_config[-1]["pair_buy_lots"][:],
                                "pair_sell_lots": sets_config[-1]["pair_sell_lots"][:],
                                "single_lots": sets_config[-1]["single_lots"][:],
                                "max_positions": sets_config[-1]["max_positions"],
                            }
                        else:
                            new_set = {
                                "pair_buy_lots": [0.01, 0.01],
                                "pair_sell_lots": [0.01, 0.01],
                                "single_lots": [0.01],
                                "max_positions": 3,
                            }
                        sets_config.append(new_set)
                    
                    # Trim if needed
                    sets_config = sets_config[:num_sets]
                    
                    # Validate and normalize each set
                    for set_idx, set_cfg in enumerate(sets_config):
                        # Validate max_positions for this set
                        max_pos = int(set_cfg.get("max_positions", 3))
                        max_pos = max(3, min(MAX_POSITION_LIMIT, max_pos))
                        if max_pos % 3 != 0:
                            max_pos = (max_pos // 3) * 3  # Round down to nearest multiple of 3
                        set_cfg["max_positions"] = max_pos
                        
                        groups = max(1, max_pos // 3)
                        pair_len = groups + 1
                        
                        # Normalize pair_buy_lots
                        if "pair_buy_lots" in set_cfg and isinstance(set_cfg["pair_buy_lots"], list):
                            arr = [max(0.01, float(x)) for x in set_cfg["pair_buy_lots"]]
                        else:
                            arr = [max(0.01, float(set_cfg.get("pair_buy_lots", [0.01])[0] if isinstance(set_cfg.get("pair_buy_lots", [0.01]), list) else set_cfg.get("pair_buy_lots", 0.01)))]
                        if len(arr) < pair_len:
                            arr += [arr[-1]] * (pair_len - len(arr))
                        set_cfg["pair_buy_lots"] = arr[:pair_len]
                        
                        # Normalize pair_sell_lots
                        if "pair_sell_lots" in set_cfg and isinstance(set_cfg["pair_sell_lots"], list):
                            arr = [max(0.01, float(x)) for x in set_cfg["pair_sell_lots"]]
                        else:
                            arr = [max(0.01, float(set_cfg.get("pair_sell_lots", [0.01])[0] if isinstance(set_cfg.get("pair_sell_lots", [0.01]), list) else set_cfg.get("pair_sell_lots", 0.01)))]
                        if len(arr) < pair_len:
                            arr += [arr[-1]] * (pair_len - len(arr))
                        set_cfg["pair_sell_lots"] = arr[:pair_len]
                        
                        # Normalize single_lots
                        if "single_lots" in set_cfg and isinstance(set_cfg["single_lots"], list):
                            arr = [max(0.01, float(x)) for x in set_cfg["single_lots"]]
                        else:
                            arr = [max(0.01, float(set_cfg.get("single_lots", [0.01])[0] if isinstance(set_cfg.get("single_lots", [0.01]), list) else set_cfg.get("single_lots", 0.01)))]
                        if len(arr) < groups:
                            arr += [arr[-1]] * (groups - len(arr))
                        set_cfg["single_lots"] = arr[:groups]
                    
                    self.config["symbols"][symbol]["sets_config"] = sets_config
                    
                    # Keep legacy fields in sync with first set for backward compatibility
                    if sets_config:
                        first_set = sets_config[0]
                        self.config["symbols"][symbol]["pair_buy_lots"] = first_set["pair_buy_lots"][:]
                        self.config["symbols"][symbol]["pair_sell_lots"] = first_set["pair_sell_lots"][:]
                        self.config["symbols"][symbol]["single_lots"] = first_set["single_lots"][:]
                        self.config["symbols"][symbol]["max_positions"] = first_set["max_positions"]
        
        self.save_config()
        return self.config

    def get_config(self) -> Dict[str, Any]:
        return self.config
    
    def get_global_config(self) -> Dict[str, Any]:
        """Get global settings"""
        return self.config.get("global", {})
    
    def get_symbol_config(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get config for a specific symbol"""
        return self.config.get("symbols", {}).get(symbol)
    
    def get_set_config(self, symbol: str, set_index: int = 0) -> Optional[Dict[str, Any]]:
        """Get config for a specific set within a symbol"""
        sym_cfg = self.get_symbol_config(symbol)
        if not sym_cfg:
            return None
        sets_config = sym_cfg.get("sets_config", [])
        if not sets_config:
            return None
        # Clamp set_index to valid range
        set_index = max(0, min(set_index, len(sets_config) - 1))
        return sets_config[set_index]
    
    def get_enabled_symbols(self) -> List[str]:
        """Get list of symbols that are enabled"""
        enabled = []
        for symbol, cfg in self.config.get("symbols", {}).items():
            if cfg.get("enabled", False):
                enabled.append(symbol)
        return enabled
    
    def enable_symbol(self, symbol: str, enabled: bool = True):
        """Enable or disable a symbol"""
        if symbol in self.config.get("symbols", {}):
            self.config["symbols"][symbol]["enabled"] = enabled
            self.save_config()
    
    def _get_defaults(self) -> Dict[str, Any]:
        """Generate default multi-asset config structure"""
        return {
            "global": {
                "max_runtime_minutes": 0,
                "volatility_tolerance": "off"
            },
            "symbols": {
                symbol: get_default_symbol_config()
                for symbol in AVAILABLE_SYMBOLS
            }
        }
