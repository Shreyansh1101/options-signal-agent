"""Phone notifications: ntfy (always) + Telegram (once connected).

Telegram is set up without touching the server: the user publishes
    telegram <bot token>
to their private ntfy topic; the live agent picks it up (see `poll_commands`), finds the chat
the user opened with the bot, and stores both in the state folder.
"""
from __future__ import annotations

import html
import json
import os
import re
import time
from pathlib import Path

import requests

from .config import Secrets

SENT: list[dict] = []  # everything sent this run (used by tests and the run summary)
TG_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
TOKEN_RX = re.compile(r"^\s*telegram\s+(\d{5,}:[A-Za-z0-9_-]{30,})\s*$", re.I)


# ── small settings file next to the agent state ──────────────
def _settings_path() -> Path:
    from .state import STATE_PATH
    return Path(os.environ.get("NOTIFY_SETTINGS_PATH", STATE_PATH.parent / "notify.json"))


def load_settings() -> dict:
    p = _settings_path()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def save_settings(s: dict) -> None:
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(s))
    os.chmod(tmp, 0o600)
    tmp.replace(p)


# ── Telegram ─────────────────────────────────────────────────
def _tg_token(secrets: Secrets, settings: dict) -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or settings.get("telegram_token", "")


def tg_discover_chat(token: str) -> int | None:
    """Chat id of the most recent private message sent to the bot (the user tapping Start / saying hi)."""
    try:
        r = requests.get(f"{TG_API}/bot{token}/getUpdates", params={"timeout": 0}, timeout=15)
        r.raise_for_status()
        chats = [u.get("message", {}).get("chat", {}) for u in r.json().get("result", [])]
        chats = [c for c in chats if c.get("type") == "private"]
        return chats[-1]["id"] if chats else None
    except Exception as e:
        print(f"[telegram] getUpdates failed: {e}")
        return None


def tg_send(token: str, chat_id: int, title: str, message: str, buttons: list[dict] | None = None,
            silent: bool = False) -> bool:
    text = f"<b>{html.escape(title)}</b>\n\n{html.escape(message)}"
    payload = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
               "disable_web_page_preview": True, "disable_notification": silent}
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": b["label"], "url": b["url"]} for b in buttons[:3]]]}
    try:
        r = requests.post(f"{TG_API}/bot{token}/sendMessage", json=payload, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] send failed: {e}")
        return False


def _telegram(secrets: Secrets, title: str, message: str, priority: int, actions: list[dict] | None) -> bool:
    settings = load_settings()
    token = _tg_token(secrets, settings)
    if not token:
        return False
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip() or settings.get("telegram_chat_id")
    if not chat:
        chat = tg_discover_chat(token)
        if not chat:
            return False
        settings["telegram_chat_id"] = chat
        save_settings(settings)
    return tg_send(token, chat, title, message, actions, silent=priority <= 2)


# ── public API ───────────────────────────────────────────────
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
    ok = False
    if secrets.ntfy_topic:
        try:
            r = requests.post(secrets.ntfy_server, json=payload, timeout=15)
            r.raise_for_status()
            ok = True
        except Exception as e:
            print(f"[notify] ntfy failed: {e}")
    else:
        print("[notify] NTFY_TOPIC not set.")
    try:
        ok = _telegram(secrets, title, message, priority, actions) or ok
    except Exception as e:
        print(f"[telegram] {e}")
    return ok


def poll_commands(secrets: Secrets) -> list[str]:
    """Read new messages on the ntfy topic and act on setup commands. Returns what was done."""
    if not secrets.ntfy_topic:
        return []
    settings = load_settings()
    since = settings.get("ntfy_since") or "12h"   # ntfy keeps messages ~12h
    try:
        r = requests.get(f"{secrets.ntfy_server}/{secrets.ntfy_topic}/json",
                         params={"poll": "1", "since": since}, timeout=20)
        r.raise_for_status()
        lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    except Exception as e:
        print(f"[commands] poll failed: {e}")
        return []
    done = []
    for m in lines:
        if m.get("event") != "message":
            continue
        settings["ntfy_since"] = m.get("id") or settings.get("ntfy_since")
        text = (m.get("message") or "").strip()
        hit = TOKEN_RX.match(text)
        if hit:
            token = hit.group(1)
            settings["telegram_token"] = token
            settings.pop("telegram_chat_id", None)
            chat = tg_discover_chat(token)
            if chat:
                settings["telegram_chat_id"] = chat
            save_settings(settings)
            if chat and tg_send(token, chat, "✅ Telegram connected",
                                "Your options signal alerts will arrive here from now on (ntfy keeps working too)."):
                done.append("telegram connected")
                _ntfy_only(secrets, "✅ Telegram connected", "Check Telegram — a test message just arrived there.")
            else:
                done.append("telegram token saved, chat not found")
                _ntfy_only(secrets, "⚠️ Telegram: almost there",
                           "Got your bot key, but couldn't find your chat. Open your bot in Telegram, tap Start "
                           "(or send it 'hi'), then publish the same telegram message here again.")
        elif text.lower() in {"telegram off", "telegram stop"}:
            settings.pop("telegram_token", None)
            settings.pop("telegram_chat_id", None)
            done.append("telegram off")
            _ntfy_only(secrets, "Telegram alerts turned off", "Alerts will only go to ntfy.")
        elif text.lower() in {"telegram test", "test"}:
            ok = _telegram(secrets, "🔔 Test alert", "If you see this on your lock screen, alerts are working.", 4, None)
            done.append("test sent" if ok else "test failed")
    save_settings(settings)
    return done


def _ntfy_only(secrets: Secrets, title: str, message: str) -> None:
    try:
        requests.post(secrets.ntfy_server, json={"topic": secrets.ntfy_topic, "title": title, "message": message},
                      timeout=15)
    except Exception as e:
        print(f"[notify] ntfy failed: {e}")


def chart_actions(symbol: str, news_url: str | None = None) -> list[dict]:
    acts = [{"action": "view", "label": "Chart", "url": f"https://www.tradingview.com/chart/?symbol={symbol}"}]
    if news_url:
        acts.append({"action": "view", "label": "News", "url": news_url})
    acts.append({"action": "view", "label": "Alpaca", "url": "https://app.alpaca.markets/"})
    return acts
