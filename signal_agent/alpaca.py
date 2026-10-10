"""Minimal Alpaca REST client (free 'Basic' plan: IEX stock feed + indicative options feed).

Plain HTTP via `requests` so it has no heavy SDK dependency and is easy to fake in tests.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
import requests

DATA_URL = "https://data.alpaca.markets"
TRADING_URL = "https://paper-api.alpaca.markets"  # paper keys work for market data too


class AlpacaError(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlpacaClient:
    def __init__(self, key: str, secret: str, session: requests.Session | None = None):
        if not key or not secret:
            raise AlpacaError("Missing Alpaca keys (APCA_API_KEY_ID / APCA_API_SECRET_KEY).")
        self.s = session or requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    # ── low level ────────────────────────────────────────────
    def _get(self, url: str, params: dict | None = None) -> dict | list:
        for attempt in range(4):
            r = self.s.get(url, params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 401 or r.status_code == 403:
                raise AlpacaError(f"Alpaca rejected the keys ({r.status_code}): {r.text[:200]}")
            if not r.ok:
                raise AlpacaError(f"{r.status_code} for {url}: {r.text[:300]}")
            return r.json()
        raise AlpacaError(f"Alpaca kept failing for {url} (rate limit or outage)")

    # ── market status ────────────────────────────────────────
    def clock(self) -> dict:
        return self._get(f"{TRADING_URL}/v2/clock")

    def is_trading_day(self, day: str) -> bool:
        cal = self._get(f"{TRADING_URL}/v2/calendar", {"start": day, "end": day})
        return any(c.get("date") == day for c in cal or [])

    # ── screener ─────────────────────────────────────────────
    def most_actives(self, top: int) -> list[dict]:
        if top <= 0:
            return []
        j = self._get(f"{DATA_URL}/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": min(top, 100)})
        return j.get("most_actives", [])

    def movers(self, top: int) -> dict:
        if top <= 0:
            return {"gainers": [], "losers": []}
        j = self._get(f"{DATA_URL}/v1beta1/screener/stocks/movers", {"top": min(top, 50)})
        return {"gainers": j.get("gainers", []), "losers": j.get("losers", [])}

    # ── stocks ───────────────────────────────────────────────
    def bars(self, symbols: list[str], timeframe: str, start: datetime, end: datetime | None = None,
             max_pages: int = 20) -> dict[str, pd.DataFrame]:
        """Returns {symbol: DataFrame[t(UTC), o, h, l, c, v]} sorted by time."""
        out: dict[str, list] = {}
        params = {
            "symbols": ",".join(symbols), "timeframe": timeframe, "start": _iso(start),
            "limit": 10000, "feed": "iex", "adjustment": "split", "sort": "asc",
        }
        if end:
            params["end"] = _iso(end)
        for _ in range(max_pages):
            j = self._get(f"{DATA_URL}/v2/stocks/bars", params)
            for sym, rows in (j.get("bars") or {}).items():
                out.setdefault(sym, []).extend(rows)
            tok = j.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok
        frames = {}
        for sym, rows in out.items():
            df = pd.DataFrame(rows)
            if df.empty:
                continue
            df["t"] = pd.to_datetime(df["t"], utc=True)
            frames[sym] = df[["t", "o", "h", "l", "c", "v"]].sort_values("t").reset_index(drop=True)
        return frames

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        j = self._get(f"{DATA_URL}/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": "iex"})
        snaps = j.get("snapshots", j) if isinstance(j, dict) else {}
        prices = {}
        for sym, snap in snaps.items():
            if not isinstance(snap, dict):
                continue
            p = (snap.get("latestTrade") or {}).get("p") or (snap.get("minuteBar") or {}).get("c")
            if p:
                prices[sym] = float(p)
        return prices

    # ── news ─────────────────────────────────────────────────
    def news(self, symbols: list[str], start: datetime, end: datetime | None = None, max_pages: int = 4) -> list[dict]:
        params = {"symbols": ",".join(symbols), "start": _iso(start), "limit": 50, "sort": "desc"}
        if end:
            params["end"] = _iso(end)
        items: list[dict] = []
        for _ in range(max_pages):
            j = self._get(f"{DATA_URL}/v1beta1/news", params)
            items.extend(j.get("news", []))
            tok = j.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok
        return items

    # ── options ──────────────────────────────────────────────
    def option_chain(self, underlying: str, opt_type: str, exp_gte: str, exp_lte: str,
                     strike_gte: float, strike_lte: float, max_pages: int = 3) -> dict[str, dict]:
        params = {
            "feed": "indicative", "type": opt_type, "limit": 1000,
            "expiration_date_gte": exp_gte, "expiration_date_lte": exp_lte,
            "strike_price_gte": round(strike_gte, 2), "strike_price_lte": round(strike_lte, 2),
        }
        snaps: dict[str, dict] = {}
        for _ in range(max_pages):
            j = self._get(f"{DATA_URL}/v1beta1/options/snapshots/{underlying}", params)
            snaps.update(j.get("snapshots") or {})
            tok = j.get("next_page_token")
            if not tok:
                break
            params["page_token"] = tok
        return snaps

    def option_snapshots(self, contract_symbols: list[str]) -> dict[str, dict]:
        if not contract_symbols:
            return {}
        j = self._get(f"{DATA_URL}/v1beta1/options/snapshots",
                      {"symbols": ",".join(contract_symbols), "feed": "indicative"})
        return j.get("snapshots") or {}
