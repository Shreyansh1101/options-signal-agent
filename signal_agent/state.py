"""Small JSON state file (open signals, history, cooldowns). Persisted between runs by the GitHub Actions cache."""
from __future__ import annotations

import json
import os
from pathlib import Path

from .config import ROOT

STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state" / "state.json"))


def empty_state() -> dict:
    return {"version": 1, "open": {}, "closed": [], "cooldown": {}, "scores": {},
            "news_cache": {}, "summary_sent": "", "last_scan": ""}


def load_state(path: Path = STATE_PATH) -> dict:
    st = empty_state()
    if path.exists():
        try:
            st.update(json.loads(path.read_text()))
        except Exception as e:
            print(f"[state] could not read {path}: {e} — starting fresh")
    return st


def save_state(st: dict, path: Path = STATE_PATH) -> None:
    st["closed"] = st["closed"][-500:]
    if len(st["news_cache"]) > 3000:
        st["news_cache"] = dict(list(st["news_cache"].items())[-2000:])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, default=str))
    tmp.replace(path)


def track_record(st: dict) -> dict:
    rs = [c["pnl_pct"] for c in st["closed"] if c.get("pnl_pct") is not None]
    if not rs:
        return {"trades": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    return {
        "trades": len(rs),
        "win_rate": len(wins) / len(rs),
        "avg": sum(rs) / len(rs),
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "best": max(rs), "worst": min(rs),
    }
