"""Phone push notifications via ntfy (free iPhone/Android app, no account needed)."""
from __future__ import annotations

import requests

from .config import Secrets

SENT: list[dict] = []  # everything sent this run (used by tests and the run summary)


def push(secrets: Secrets, title: str, message: str, tags: list[str] | None = None,
         priority: int = 3, click: str | None = None, actions: list[dict] | None = None) -> bool:
    payload = {"topic": secrets.ntfy_topic, "title": title, "message": message,
               "tags": tags or [], "priority": priority}
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions[:3]
    SENT.append(payload)
    print(f"\n──── NOTIFY: {title}\n{message}\n────")
    if secrets.dry_run:
        return True
    if not secrets.ntfy_topic:
        print("[notify] NTFY_TOPIC not set — notification printed only.")
        return False
    try:
        r = requests.post(secrets.ntfy_server, json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[notify] failed: {e}")
        return False


def chart_actions(symbol: str, news_url: str | None = None) -> list[dict]:
    acts = [{"action": "view", "label": "Chart", "url": f"https://www.tradingview.com/chart/?symbol={symbol}"}]
    if news_url:
        acts.append({"action": "view", "label": "News", "url": news_url})
    acts.append({"action": "view", "label": "Alpaca", "url": "https://app.alpaca.markets/"})
    return acts
