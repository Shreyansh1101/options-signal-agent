"""Settings: config.yaml merged over safe defaults, plus secrets from environment variables."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "universe": {
        "always_watch": ["SPY", "QQQ", "IWM"],
        "watchlist": [],
        "scanner_most_active": 25,
        "scanner_movers": 15,
        "max_symbols": 30,
        "min_price": 10,
        "max_price": 1500,
        "exclude": [],
    },
    "trading_window": {"start": "09:45", "end": "15:30"},
    "scoring": {
        "chart_weight": 0.6,
        "news_weight": 0.4,
        "no_news_factor": 0.85,
        "entry_threshold": 0.55,
        "confirm_cycles": 2,
        "news_veto": 0.25,
        "min_relative_volume": 1.0,
        "market_filter": True,
        "news_lookback_hours": 24,
        "news_half_life_hours": 6,
    },
    "contracts": {
        "min_days_to_expiry": 3,
        "target_days_to_expiry": 10,
        "max_days_to_expiry": 30,
        "min_delta": 0.35,
        "max_delta": 0.65,
        "max_spread_pct": 0.12,
        "min_premium": 0.30,
        "max_premium": 10.00,
    },
    "risk": {
        "risk_per_trade_usd": 100,
        "stop_loss_pct": 0.35,
        "take_profit_pct": 0.60,
        "breakeven_after_pct": 0.30,
        "flip_exit_score": 0.35,
        "max_hold_days": 5,
        "max_open_signals": 5,
        "cooldown_minutes": 90,
    },
    "claude_news": {"enabled": False, "model": "claude-haiku-5-5"},
    "live": {"confirm_cycles": 3, "rescan_on_news": True, "morning_check": True},
    "notifications": {"daily_summary": True, "quiet_skips": True},
    "kalshi": {
        "enabled": True, "categories": ["Economics", "Politics", "Elections", "World", "Sports"], "max_pages": 40,
        "index": {"enabled": True,
                  "series": {"KXINX": "SPY", "KXINXU": "SPY", "KXINXI": "SPY",
                             "KXNASDAQ100": "QQQ", "KXNASDAQ100U": "QQQ", "KXNASDAQ100I": "QQQ"},
                  "ratio_series": {"SPY": "KXINX", "QQQ": "KXNASDAQ100"},
                  "ratio_bounds": {"SPY": [9.0, 11.5], "QQQ": [35, 50]},
                  "min_edge": 0.06, "min_minutes_left": 20, "max_trading_minutes": 1170,
                  "max_spread": 0.06, "min_size": 5, "tail_factor": 1.15},
        "news": {"enabled": True, "model": "claude-haiku-5-5", "web_search": True, "max_analyses_per_day": 8,
                 "min_edge": 0.06, "shrink": {"high": 0.6, "medium": 0.4, "low": 0.0}, "min_volume_24h": 500,
                 "max_spread": 0.05, "min_hours_to_close": 2, "max_days_to_close": 30, "sports_max_days": 3,
                 "reanalyze_hours": 12, "reanalyze_move": 0.06},
        "mispricing": {"enabled": True, "min_profit": 0.02, "min_size": 10},
        "risk": {"risk_per_trade_usd": 25, "max_contracts": 100, "max_open": 6, "take_profit": 0.20,
                 "stop_loss": 0.25, "thesis_stop": 0.08, "cooldown_minutes": 120},
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.yaml"
    user = {}
    if path.exists():
        with open(path) as f:
            user = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, user)
    cfg["universe"]["always_watch"] = [s.upper() for s in cfg["universe"]["always_watch"] or []]
    cfg["universe"]["watchlist"] = [s.upper() for s in cfg["universe"]["watchlist"] or []]
    cfg["universe"]["exclude"] = {s.upper() for s in cfg["universe"]["exclude"] or []}
    return cfg


class Secrets:
    def __init__(self) -> None:
        self.alpaca_key = os.environ.get("APCA_API_KEY_ID", "").strip()
        self.alpaca_secret = os.environ.get("APCA_API_SECRET_KEY", "").strip()
        self.ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
        self.ntfy_server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").strip().rstrip("/")
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.dry_run = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
