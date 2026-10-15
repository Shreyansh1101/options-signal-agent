"""Kalshi prediction-market signals.

Three independent strategies, all read-only (you place trades yourself in the Kalshi app):

1. INDEX FAIR VALUE  — S&P 500 / Nasdaq-100 range & threshold markets. Fair probability from the
   live index level (SPY/QQQ × a ratio calibrated on Kalshi's own last settlement) and volatility.
2. NEWS FORECASTS    — Economics/Fed, politics, world, sports. Claude (with web search) estimates the
   odds; the estimate is shrunk toward the market price before any edge is claimed, and a daily
   budget caps API spend.
3. MISPRICING SCAN   — mutually-exclusive events whose outcome prices don't add up (NO-basket arbitrage,
   or YES-basket on ladders that cover every outcome).

Every open signal is tracked: SELL alerts when the edge is captured, the thesis breaks, or a stop hits,
and a SETTLED alert with the result.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from .notify import get_anthropic_key, push

ET = ZoneInfo("America/New_York")
KALSHI_API = os.environ.get("KALSHI_API", "https://api.elections.kalshi.com/trade-api/v2")
SETTLED = {"determined", "finalized", "amended", "settled"}


def _f(x) -> float | None:
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def market_link(series: str, event: str | None, title: str = "") -> str:
    """Kalshi event page: kalshi.com/markets/<series>/<slug>/<event> (the slug is cosmetic; the event ticker decides)."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60] or "market"
    if not event:
        return f"https://kalshi.com/markets/{series.lower()}"
    return f"https://kalshi.com/markets/{series.lower()}/{slug}/{event.lower()}"


def ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ─────────────────────────────────────────────────────────────
class KalshiClient:
    """Public (no-login) market data."""

    def __init__(self, base: str = KALSHI_API, session: requests.Session | None = None, pause: float = 0.06):
        self.base, self.s, self.pause = base.rstrip("/"), session or requests.Session(), pause

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(4):
            r = self.s.get(f"{self.base}{path}", params=params, timeout=20)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            time.sleep(self.pause)
            return r.json()
        raise RuntimeError(f"Kalshi API kept failing for {path}")

    def _paged(self, path: str, key: str, params: dict, max_pages: int) -> list[dict]:
        out, cursor = [], ""
        for _ in range(max_pages):
            p = dict(params, cursor=cursor) if cursor else params
            j = self._get(path, p)
            out.extend(j.get(key) or [])
            cursor = j.get("cursor") or ""
            if not cursor:
                break
        return out

    def open_markets(self, max_pages: int = 40) -> list[dict]:
        return self._paged("/markets", "markets", {"status": "open", "limit": 1000, "mve_filter": "exclude"}, max_pages)

    def open_events(self, max_pages: int = 40) -> list[dict]:
        return self._paged("/events", "events", {"status": "open", "limit": 200}, max_pages)

    def events(self, series_ticker: str, status: str = "open", limit: int = 20) -> list[dict]:
        j = self._get("/events", {"series_ticker": series_ticker, "status": status, "limit": limit,
                                  "with_nested_markets": "true"})
        return j.get("events") or []

    def markets(self, tickers: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(tickers), 100):
            j = self._get("/markets", {"tickers": ",".join(tickers[i:i + 100]), "limit": 1000})
            out.extend(j.get("markets") or [])
        return out

    def series(self) -> list[dict]:
        return self._get("/series", {}).get("series") or []


def norm_market(m: dict, event: dict | None = None) -> dict:
    """Flatten a Kalshi market into the fields the strategies use (prices in dollars 0–1)."""
    yb, ya = _f(m.get("yes_bid_dollars")), _f(m.get("yes_ask_dollars"))
    nb, na = _f(m.get("no_bid_dollars")), _f(m.get("no_ask_dollars"))
    if na is None and yb is not None:
        na = round(1 - yb, 4)
    if nb is None and ya is not None:
        nb = round(1 - ya, 4)
    ev = event or {}
    return {
        "ticker": m.get("ticker"), "event": m.get("event_ticker") or ev.get("event_ticker"),
        "series": ev.get("series_ticker") or (m.get("event_ticker") or "").split("-")[0],
        "event_title": ev.get("title", ""), "sub": m.get("yes_sub_title") or m.get("subtitle") or m.get("title") or "",
        "rules": (m.get("rules_primary") or "")[:400],
        "yes_bid": yb or 0.0, "yes_ask": ya if ya is not None else 1.0,
        "no_bid": nb or 0.0, "no_ask": na if na is not None else 1.0,
        "yes_bid_size": _f(m.get("yes_bid_size_fp")) or 0.0, "yes_ask_size": _f(m.get("yes_ask_size_fp")) or 0.0,
        "last": _f(m.get("last_price_dollars")), "vol24": _f(m.get("volume_24h_fp")) or 0.0,
        "oi": _f(m.get("open_interest_fp")) or 0.0,
        "close": _ts(m.get("close_time")), "status": m.get("status", ""), "result": m.get("result") or "",
        "expiration_value": _f(m.get("expiration_value")),
        "strike_type": m.get("strike_type"), "floor": _f(m.get("floor_strike")), "cap": _f(m.get("cap_strike")),
    }


def mid(m: dict) -> float:
    return (m["yes_bid"] + m["yes_ask"]) / 2


def fee_coef(series: str, fee_mult: float | None = None) -> float:
    if series.startswith("KXINX") or series.startswith("KXNASDAQ100"):
        return 0.035                                   # Kalshi charges half on S&P / Nasdaq markets
    return 0.07 * (fee_mult if fee_mult else 1.0)


def fee_per_contract(price: float, coef: float, n: int = 10) -> float:
    """Taker fee per contract when buying n contracts at `price` (Kalshi rounds the order fee up to the cent)."""
    return math.ceil(coef * n * price * (1 - price) * 100 - 1e-9) / 100 / n


# ── index model ─────────────────────────────────────────────
def trading_minutes(now: datetime, close: datetime) -> float:
    if close <= now:
        return 0.0
    a, b = now.astimezone(ET), close.astimezone(ET)
    total, d = 0.0, a.date()
    while d <= b.date():
        if d.weekday() < 5:
            so = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
            sc = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
            lo, hi = max(so, a), min(sc, b)
            if hi > lo:
                total += (hi - lo).total_seconds() / 60
        d += timedelta(days=1)
    return total


def prob_yes(m: dict, S: float, sigma: float, minutes: float, year_minutes: float = 390 * 252) -> float | None:
    """Lognormal (zero-drift) probability that the market resolves YES.
    `year_minutes`: 390×252 for stock-market time, 525,600 for 24/7 crypto."""
    T = max(minutes, 0.5) / year_minutes
    sd = max(sigma * math.sqrt(T), 0.0004)              # never assume less than ~0.04% of movement
    below = lambda K: ncdf((math.log(K / S) + 0.5 * sd * sd) / sd)
    st, lo, hi = m["strike_type"], m["floor"], m["cap"]
    if st in ("greater", "greater_or_equal") and lo:
        return 1 - below(lo)
    if st in ("less", "less_or_equal") and hi:
        return below(hi)
    if st == "between" and lo and hi:
        return max(0.0, below(hi) - below(lo))
    return None


def ladder_complete(ms: list[dict], tol: float = 0.01) -> bool:
    """True when the markets form a gap-free ladder: a 'below X' tail, contiguous ranges, an 'above Y' tail."""
    lows = [m for m in ms if m["strike_type"] in ("less", "less_or_equal") and m["cap"]]
    highs = [m for m in ms if m["strike_type"] in ("greater", "greater_or_equal") and m["floor"]]
    mids = sorted((m for m in ms if m["strike_type"] == "between" and m["floor"] is not None and m["cap"] is not None),
                  key=lambda m: m["floor"])
    if len(lows) != 1 or len(highs) != 1 or len(lows) + len(highs) + len(mids) != len(ms):
        return False
    edge = lows[0]["cap"]
    for m in mids:
        if abs(m["floor"] - edge) > tol:
            return False
        edge = m["cap"]
    return abs(highs[0]["floor"] - edge) <= 1.0     # 'between' caps end in .9999, the top tail starts at the next unit


def fit_ladder(ms: list[dict], S0: float, minutes: float) -> tuple[float, float, float] | None:
    """Robust least-squares fit of (level, vol) to an event's mid prices (worst point ignored). Returns (S, sigma, rmse)."""
    mids = np.array([mid(m) for m in ms])
    T = max(minutes, 0.5) / (390 * 252)
    best = None
    S_grid = S0 * (1 + np.linspace(-0.03, 0.03, 61))
    v_grid = np.exp(np.linspace(np.log(0.03), np.log(1.2), 40))
    for _ in range(2):                                   # coarse grid, then a finer one around the best point
        for S in S_grid:
            for v in v_grid:
                pr = np.array([prob_yes(m, S, v, minutes) or 0.0 for m in ms])
                sq = np.sort((pr - mids) ** 2)[:-1]       # trimmed: ignore the single worst point (the candidate)
                err = float(np.sqrt(np.mean(sq)))
                if best is None or err < best[2]:
                    best = (float(S), float(v), err)
        S, v, _e = best
        sd = max(v * math.sqrt(T), 0.0004)
        S_grid = S * (1 + np.linspace(-sd, sd, 21))
        v_grid = v * np.exp(np.linspace(-0.3, 0.3, 21))
    return best


def realized_vol(bars5: pd.DataFrame | None) -> float | None:
    if bars5 is None or len(bars5) < 40:
        return None
    d = bars5.copy()
    d["et"] = d["t"].dt.tz_convert(ET)
    tod = d["et"].dt.time
    d = d[(tod >= datetime.strptime("09:30", "%H:%M").time()) & (tod < datetime.strptime("16:00", "%H:%M").time())]
    d = d.tail(78 * 3)
    r = np.log(d["c"]).diff().dropna()
    if len(r) < 30:
        return None
    return float(r.std() * math.sqrt(78 * 252))


class CryptoPrices:
    """Spot price + realised volatility from Coinbase's public API (one of the exchanges in CF Benchmarks' index)."""
    BASE = os.environ.get("COINBASE_API", "https://api.exchange.coinbase.com")

    def __init__(self, session: requests.Session | None = None):
        self.s = session or requests.Session()
        self._px: dict[str, tuple[float, float]] = {}
        self._vol: dict[str, tuple[float, float]] = {}

    def price(self, coin: str) -> float | None:
        hit = self._px.get(coin)
        if hit and time.time() - hit[0] < 10:
            return hit[1]
        try:
            r = self.s.get(f"{self.BASE}/products/{coin}-USD/ticker", timeout=10)
            r.raise_for_status()
            p = float(r.json()["price"])
        except Exception as e:
            print(f"[crypto] {coin} price failed: {e!r}")
            return None
        self._px[coin] = (time.time(), p)
        return p

    def vol(self, coin: str) -> float | None:
        """Annualised volatility from the last ~5 hours of 1-minute candles."""
        hit = self._vol.get(coin)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
        try:
            r = self.s.get(f"{self.BASE}/products/{coin}-USD/candles", params={"granularity": 60}, timeout=10)
            r.raise_for_status()
            closes = [c[4] for c in sorted(r.json(), key=lambda c: c[0])]
            rets = np.diff(np.log(closes))
            v = float(np.std(rets) * math.sqrt(525600)) if len(rets) > 60 else None
        except Exception as e:
            print(f"[crypto] {coin} vol failed: {e!r}")
            return None
        if v:
            self._vol[coin] = (time.time(), v)
        return v


# ─────────────────────────────────────────────────────────────
CLAUDE_SYSTEM = (
    "You are a careful, well-calibrated forecaster for prediction markets. Use web search to find the most "
    "recent, reliable information (official sources, major outlets, betting/consensus odds where relevant). "
    "Market prices already reflect public information, so only move away from them for concrete reasons. "
    "Never invent facts. Reply with ONLY a JSON object, no prose."
)


def claude_forecast(api_key: str, model: str, event_title: str, markets: list[dict], now: datetime,
                    headlines: list[str] | None = None, web_search: bool = True, timeout: int = 120) -> dict:
    """Ask Claude for YES probabilities. Returns {'probs': {ticker: (p, conf, reason)}, 'news': [...], 'searches': n}."""
    lines = []
    for m in markets[:10]:
        close = m["close"].astimezone(ET).strftime("%b %d %I:%M %p ET") if m["close"] else "?"
        lines.append(f'- {m["ticker"]}: "{m["sub"]}" | market YES price ≈ {mid(m):.2f} '
                     f'(bid {m["yes_bid"]:.2f} / ask {m["yes_ask"]:.2f}) | closes {close}\n  rules: {m["rules"][:300]}')
    prompt = (
        f"Current time: {now.astimezone(ET):%A %b %d %Y %I:%M %p} ET.\n"
        f"Event: {event_title}\nMarkets:\n" + "\n".join(lines) +
        ("\n\nRecent headlines we saw:\n" + "\n".join(f"- {h}" for h in (headlines or [])[:8]) if headlines else "") +
        "\n\nFor each market estimate the probability it resolves YES under its rules. Return JSON:\n"
        '{"markets": [{"ticker": "...", "probability": 0.00-1.00, "confidence": "low|medium|high", '
        '"reason": "<=25 words"}], "key_news": ["<=3 short bullets of the facts that matter most"]}'
    )
    body = {"model": model, "max_tokens": 1500, "system": CLAUDE_SYSTEM,
            "messages": [{"role": "user", "content": prompt}]}
    if web_search:
        body["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    hdr = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    r = requests.post("https://api.anthropic.com/v1/messages", headers=hdr, json=body, timeout=timeout)
    if r.status_code == 400 and web_search and "tool" in r.text.lower():
        return claude_forecast(api_key, model, event_title, markets, now, headlines, False, timeout)
    r.raise_for_status()
    j = r.json()
    texts = [b.get("text", "") for b in j.get("content", []) if b.get("type") == "text"]
    raw = texts[-1] if texts else ""
    for t in reversed(texts):
        if "{" in t:
            raw = t
            break
    data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    probs = {}
    for x in data.get("markets", []):
        try:
            p = min(0.99, max(0.01, float(x["probability"])))
            probs[x["ticker"]] = (p, str(x.get("confidence", "low")).lower(), str(x.get("reason", ""))[:200])
        except (KeyError, TypeError, ValueError):
            continue
    searches = (j.get("usage", {}).get("server_tool_use") or {}).get("web_search_requests", 0)
    return {"probs": probs, "news": [str(n)[:160] for n in data.get("key_news", [])][:3], "searches": searches}


# ─────────────────────────────────────────────────────────────
class KalshiEngine:
    def __init__(self, cfg: dict, secrets, prices, client: KalshiClient | None = None,
                 state_path: str | Path | None = None, claude_fn=claude_forecast, now_fn=None):
        self.cfg, self.secrets, self.prices = cfg, secrets, prices
        self.k = cfg["kalshi"]
        self.client = client or KalshiClient()
        self.claude_fn = claude_fn
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        from .state import STATE_PATH
        self.path = Path(state_path or os.environ.get("KALSHI_STATE_PATH", STATE_PATH.parent / "kalshi.json"))
        self.st = self._load()
        self.universe: dict[str, dict] = {}       # ticker → market
        self.events: dict[str, dict] = {}         # event_ticker → event meta
        self.series_meta: dict[str, dict] = {}
        self._last: dict[str, float] = {}
        self._ratio: dict[str, tuple[str, float]] = {}
        self._vol: dict[str, tuple[float, float]] = {}
        self.crypto = CryptoPrices()
        self._index_fair: dict[str, float] = {}
        self._crypto_fair: dict[str, float] = {}
        self.stats = {"errors": [], "last": {}}

    def _err(self, where: str, e: Exception) -> None:
        msg = f"{self.now().astimezone(ET):%H:%M} {where}: {e!r}"[:200]
        print(f"[kalshi] {msg}")
        self.stats["errors"] = (self.stats["errors"] + [msg])[-5:]

    # state ---------------------------------------------------
    def _load(self) -> dict:
        base = {"open": {}, "closed": [], "cooldown": {}, "analyses": {}, "budget": {}, "snaps": {}, "recap": ""}
        try:
            if self.path.exists():
                base.update(json.loads(self.path.read_text()))
        except Exception as e:
            print(f"[kalshi] state unreadable, starting fresh: {e}")
        return base

    def save(self) -> None:
        self.st["closed"] = self.st["closed"][-300:]
        cutoff = (self.now() - timedelta(hours=4)).timestamp()
        self.st["snaps"] = {k: [p for p in v if p[0] > cutoff][-30:] for k, v in self.st["snaps"].items()
                            if any(p[0] > cutoff for p in v)}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.st, default=str))
        tmp.replace(self.path)

    def _due(self, name: str, every_s: float) -> bool:
        t = time.time()
        if t - self._last.get(name, 0) >= every_s:
            self._last[name] = t
            return True
        return False

    def _cool(self, key: str) -> bool:
        cd = self.st["cooldown"].get(key)
        return bool(cd and self.now() < datetime.fromisoformat(cd))

    def _set_cool(self, key: str, minutes: float | None = None) -> None:
        self.st["cooldown"][key] = (self.now() + timedelta(minutes=minutes or self.k["risk"]["cooldown_minutes"])).isoformat()

    # main tick -----------------------------------------------
    def tick(self, market_open: bool) -> list[str]:
        if not self.k.get("enabled", True):
            return []
        done = []
        if self._due("universe", 600):
            try:
                self.refresh_universe()
                done.append(f"universe {len(self.universe)} markets")
                if self.k["mispricing"]["enabled"]:
                    done += self.scan_mispricing()
            except Exception as e:
                self._err("universe", e)
        if market_open and self.k["index"]["enabled"] and self._due("index", 55):
            try:
                done += self.scan_index()
            except Exception as e:
                self._err("index", e)
        if self.k.get("crypto", {}).get("enabled") and self._due("crypto", 30):
            try:
                done += self.scan_crypto()
            except Exception as e:
                self._err("crypto", e)
        if self.k["news"]["enabled"] and self.universe and self._due("news", 900):
            try:
                done += self.scan_news()
            except Exception as e:
                self._err("news", e)
        if self.st["open"] and self._due("exits", 55):
            try:
                done += self.manage_open()
            except Exception as e:
                self._err("exits", e)
        for d in done:
            self.stats["last"][d.split(" ")[0]] = f"{self.now().astimezone(ET):%a %H:%M} {d}"[:120]
        self.maybe_recap()
        self.save()
        return done

    # universe ------------------------------------------------
    def refresh_universe(self) -> None:
        if not self.series_meta or self._due("series", 6 * 3600):
            try:
                self.series_meta = {s.get("ticker"): s for s in self.client.series() if s.get("ticker")}
            except Exception as e:
                print(f"[kalshi] series list failed: {e!r}")
        evs = {e["event_ticker"]: e for e in self.client.open_events(self.k.get("max_pages", 40)) if e.get("event_ticker")}
        mk = {}
        for m in self.client.open_markets(self.k.get("max_pages", 40)):
            ev = evs.get(m.get("event_ticker"), {})
            n = norm_market(m, ev)
            if n["ticker"]:
                mk[n["ticker"]] = n
        self.universe, self.events = mk, evs
        ts = self.now().timestamp()
        for t, m in mk.items():
            if self._news_candidate(m):
                self.st["snaps"].setdefault(t, []).append([ts, round(mid(m), 3)])

    def category(self, m: dict) -> str:
        s = self.series_meta.get(m["series"], {})
        return s.get("category") or self.events.get(m["event"], {}).get("category") or ""

    def coef(self, m: dict) -> float:
        s = self.series_meta.get(m["series"], {})
        return fee_coef(m["series"], _f(s.get("fee_multiplier")))

    def link(self, m: dict) -> str:
        return market_link(m["series"], m["event"], m.get("event_title") or "")

    # sizing / alerts -----------------------------------------
    def _size(self, price: float) -> int:
        r = self.k["risk"]
        return max(1, min(int(r["max_contracts"]), int(r["risk_per_trade_usd"] // max(price, 0.01))))

    def _open_signal(self, kind: str, m: dict, side: str, price: float, fair: float, why: list[str],
                     extra: dict | None = None) -> dict:
        n = self._size(price)
        fee = fee_per_contract(price, self.coef(m), n)
        sig = {"id": f"{m['ticker']}-{side}-{self.now():%m%d%H%M}", "kind": kind, "ticker": m["ticker"],
               "event": m["event"], "series": m["series"], "side": side, "entry": round(price, 2),
               "fair": round(fair, 3), "qty": n, "fee": fee, "opened": self.now().isoformat(),
               "title": f'{m["event_title"]} — {m["sub"]}'.strip(" —"), "close": m["close"].isoformat() if m["close"] else None,
               **(extra or {})}
        edge = fair - price - fee
        close_txt = m["close"].astimezone(ET).strftime("%a %b %d %I:%M %p ET") if m["close"] else "?"
        msg = [sig["title"],
               f"Buy {side.upper()} at ≤ ${price:.2f}  (bid {m[side + '_bid']:.2f} / ask {m[side + '_ask']:.2f})",
               f"{'Model' if kind == 'index' else 'Fair'} value {fair:.2f} → edge +{edge * 100:.0f}¢ per contract after fees",
               f"Size: {n} contracts ≈ ${price * n:,.2f} (max loss ${price * n + fee * n:,.2f}, pays ${n:,.0f} if right)",
               f"Exit plan: sell near {fair:.2f}, or hold to settlement · closes {close_txt}",
               *why,
               f"Ticker: {m['ticker']}",
               "Check the live price in the Kalshi app before buying."]
        tag = {"index": "📈", "news": "📰", "crypto": "₿"}.get(kind, "🎯")
        push(self.secrets, f"{tag} KALSHI BUY {side.upper()} · {m['sub'][:60]}", "\n".join(msg),
             tags=["dart"], priority=5, click=self.link(m),
             actions=[{"action": "view", "label": "Open in Kalshi", "url": self.link(m)}])
        self.st["open"][sig["id"]] = sig
        self._set_cool(m["ticker"])
        return sig

    # 1) index fair value -------------------------------------
    def _index_level(self, proxy: str) -> tuple[float, float] | None:
        """(estimated index level, proxy price) using ratio = last settlement value / proxy close that day."""
        price = (self.prices.latest_prices([proxy]) or {}).get(proxy)
        if not price:
            return None
        today = self.now().astimezone(ET).date().isoformat()
        cached = self._ratio.get(proxy)
        if not cached or cached[0] != today:
            rs = self.k["index"]["ratio_series"].get(proxy)
            ratio = None
            try:
                evs = sorted(self.client.events(rs, status="settled", limit=10),
                             key=lambda e: e.get("strike_date") or "", reverse=True)
                daily = self.prices.bars([proxy], "1Day", self.now() - timedelta(days=20), self.now()).get(proxy)
                for e in evs:
                    sd = _ts(e.get("strike_date"))
                    vals = [_f(m.get("expiration_value")) for m in e.get("markets") or []]
                    vals = [v for v in vals if v]
                    if not sd or not vals or daily is None:
                        continue
                    day = sd.astimezone(ET).date()
                    row = daily[daily["t"].dt.tz_convert(ET).dt.date == day]
                    if len(row):
                        ratio = vals[0] / float(row["c"].iloc[-1])
                        break
            except Exception as e:
                print(f"[kalshi] ratio calibration failed for {proxy}: {e!r}")
            lo, hi = self.k["index"]["ratio_bounds"].get(proxy, [0, 1e9])
            if not ratio or not (lo <= ratio <= hi):
                print(f"[kalshi] no trustworthy {proxy} ratio ({ratio}) — skipping index markets")
                return None
            self._ratio[proxy] = (today, ratio)
        return price * self._ratio[proxy][1], price

    def _sigma(self, proxy: str, S_proxy: float) -> float:
        cached = self._vol.get(proxy)
        if cached and time.time() - cached[0] < 900:
            return cached[1]
        rv = realized_vol(self.prices.bars([proxy], "5Min", self.now() - timedelta(days=6), self.now()).get(proxy))
        iv = None
        try:
            today = self.now().astimezone(ET).date()
            ch = self.prices.option_chain(proxy, "call", today.isoformat(), (today + timedelta(days=7)).isoformat(),
                                          S_proxy * 0.99, S_proxy * 1.01)
            best = None
            for sym, snap in ch.items():
                ivx = snap.get("impliedVolatility")
                k = int(sym[-8:]) / 1000
                if ivx and 0.03 < ivx < 2:
                    key = (sym[len(proxy):len(proxy) + 6], abs(k - S_proxy))
                    if best is None or key < best[0]:
                        best = (key, ivx)
            iv = best[1] if best else None
        except Exception as e:
            print(f"[kalshi] IV lookup failed: {e!r}")
        cands = [v for v in (rv, iv * 0.9 if iv else None) if v]
        sigma = max(max(cands) if cands else 0.18, 0.08) * self.k["index"]["tail_factor"]
        self._vol[proxy] = (time.time(), sigma)
        return sigma

    def scan_index(self) -> list[str]:
        """Relative-value only: fit ONE smooth distribution (level + volatility) to the whole price ladder of an
        event, then flag a single contract that is badly out of line with its neighbours. The market sets the
        level and volatility, so our own (laggy, noisy) index/vol estimates can no longer bias the bets."""
        ix = self.k["index"]
        now = self.now()
        done, fair_cache = [], {}
        today = now.astimezone(ET).date().isoformat()
        sent_today = self.st.setdefault("index_day", {}).get(today, 0)
        open_events = {s["event"] for s in self.st["open"].values()}
        allowed = set(ix.get("strike_types", ["between"]))
        for series, proxy in ix["series"].items():
            try:
                evs = self.client.events(series, status="open", limit=50)
            except Exception:
                continue
            lvl = self._index_level(proxy) if evs else None
            if not lvl:
                continue
            S0 = lvl[0]
            for e in evs:
                ms = []
                for raw in e.get("markets") or []:
                    m = norm_market(raw, e)
                    if not m["close"] or prob_yes(m, S0, 0.2, 60) is None:
                        continue
                    m["mins"] = trading_minutes(now, m["close"])
                    if m["yes_ask"] - m["yes_bid"] <= ix["max_spread"] and 0 < m["yes_ask"] <= 1:
                        ms.append(m)
                if len(ms) < ix["min_fit_markets"]:
                    continue
                mins = ms[0]["mins"]
                if mins < ix["min_minutes_left"] or mins > ix["max_trading_minutes"]:
                    continue
                fit = fit_ladder(ms, S0, mins)
                if not fit:
                    continue
                S, sig, rmse = fit
                for m in ms:
                    fair_cache[m["ticker"]] = prob_yes(m, S, sig, mins)
                if rmse > ix["max_fit_error"] or e["event_ticker"] in open_events or self._cool("ev:" + e["event_ticker"]):
                    continue
                if sent_today >= ix["max_signals_per_day"] or len(self.st["open"]) >= self.k["risk"]["max_open"]:
                    continue
                best = None
                for m in ms:
                    if m["strike_type"] not in allowed or self._cool(m["ticker"]):
                        continue
                    p = fair_cache[m["ticker"]]
                    for side, fair, ask, size in (("yes", p, m["yes_ask"], m["yes_ask_size"]),
                                                  ("no", 1 - p, m["no_ask"], m["yes_bid_size"])):
                        if not (ix["min_price"] <= ask <= 0.95) or (size and size < ix["min_size"]):
                            continue
                        edge = fair - ask - fee_per_contract(ask, self.coef(m))
                        if edge >= ix["min_edge"] and (best is None or edge > best[0]):
                            best = (edge, m, side, ask, fair)
                if best:
                    edge, m, side, ask, fair = best
                    why = [f"Out of line with the rest of this ladder: market-implied S&P/Nasdaq level {S:,.0f}, "
                           f"vol {sig * 100:.0f}% (fit error {rmse * 100:.1f}¢) · {mins:.0f} trading min left"]
                    self._open_signal("index", m, side, ask, fair, why, {"proxy": proxy})
                    self._set_cool("ev:" + e["event_ticker"], 24 * 60)
                    open_events.add(e["event_ticker"])
                    sent_today += 1
                    self.st["index_day"] = {today: sent_today}
                    done.append(f"index {side} {m['ticker']} edge {edge:.2f}")
        self._index_fair = fair_cache
        return done

    # 1b) crypto fair value (24/7) ----------------------------
    def scan_crypto(self) -> list[str]:
        cx = self.k["crypto"]
        now = self.now()
        done, fair_cache = [], {}
        open_tickers = {s["ticker"] for s in self.st["open"].values()}
        for series, coin in cx["series"].items():
            try:
                evs = self.client.events(series, status="open", limit=50)
            except Exception:
                continue
            if not evs:
                continue
            S = self.crypto.price(coin)
            vol = self.crypto.vol(coin)
            if not S or not vol:
                continue
            sigma = max(vol, cx["min_vol"]) * cx["tail_factor"]
            for e in evs:
                best = None
                for raw in e.get("markets") or []:
                    m = norm_market(raw, e)
                    if not m["close"] or m["status"] not in ("active", "open", ""):
                        continue
                    mins = (m["close"] - now).total_seconds() / 60
                    if mins < cx["min_minutes_left"] or mins > cx["max_minutes"]:
                        continue
                    p = prob_yes(m, S, sigma, mins, 525600)
                    if p is None:
                        continue
                    fair_cache[m["ticker"]] = p
                    if m["ticker"] in open_tickers or self._cool(m["ticker"]):
                        continue
                    for side, fair, ask, size in (("yes", p, m["yes_ask"], m["yes_ask_size"]),
                                                  ("no", 1 - p, m["no_ask"], m["yes_bid_size"])):
                        if not (0.03 <= ask <= 0.97) or (m["yes_ask"] - m["yes_bid"]) > cx["max_spread"]:
                            continue
                        if size and size < cx["min_size"]:
                            continue
                        edge = fair - ask - fee_per_contract(ask, self.coef(m))
                        if edge >= cx["min_edge"] and (best is None or edge > best[0]):
                            best = (edge, m, side, ask, fair, mins)
                hour_ago = (now - timedelta(hours=1)).isoformat()
                recent = sum(1 for x in list(self.st["open"].values()) + self.st["closed"][-50:]
                             if x["kind"] == "crypto" and x["opened"] >= hour_ago)
                if best and len(self.st["open"]) < self.k["risk"]["max_open"] and recent < cx.get("max_per_hour", 3):
                    edge, m, side, ask, fair, mins = best
                    why = [f"{coin} now ${S:,.2f} (Coinbase) · vol {sigma * 100:.0f}%/yr · {mins:.0f} min left",
                           "Settles on the CF Benchmarks 60-second average — small differences from Coinbase are normal."]
                    self._open_signal("crypto", m, side, ask, fair, why, {"coin": coin})
                    open_tickers.add(m["ticker"])
                    done.append(f"crypto {side} {m['ticker']} edge {edge:.2f}")
        self._crypto_fair = fair_cache
        self.stats["crypto_markets"] = len(fair_cache)
        return done

    # 2) news / AI forecasts ----------------------------------
    def _news_candidate(self, m: dict) -> bool:
        nw = self.k["news"]
        cats = {c.lower() for c in self.k["categories"]}
        if self.category(m).lower() not in cats:
            return False
        if m["series"].startswith(("KXINX", "KXNASDAQ100")) or not m["close"]:
            return False
        days = (m["close"] - self.now()).total_seconds() / 86400
        max_days = nw["sports_max_days"] if self.category(m).lower() == "sports" else nw["max_days_to_close"]
        return (nw["min_hours_to_close"] / 24 <= days <= max_days and 0.06 <= mid(m) <= 0.94
                and (m["yes_ask"] - m["yes_bid"]) <= nw["max_spread"] and m["vol24"] >= nw["min_volume_24h"])

    def _budget_ok(self) -> bool:
        now_et = self.now().astimezone(ET)
        used = self.st["budget"].get(now_et.date().isoformat(), 0)
        cap = self.k["news"]["max_analyses_per_day"]
        pace = cap * min(1.0, (now_et.hour * 60 + now_et.minute) / 1440 + 0.34)   # spread through the day
        return used < cap and used < pace + 1

    def _spend(self) -> None:
        d = self.now().astimezone(ET).date().isoformat()
        self.st["budget"] = {d: self.st["budget"].get(d, 0) + 1}

    def _move(self, ticker: str, minutes: int = 60) -> float:
        pts = self.st["snaps"].get(ticker, [])
        if len(pts) < 2:
            return 0.0
        cut = self.now().timestamp() - minutes * 60
        old = [p for p in pts if p[0] <= cut] or pts[:1]
        return pts[-1][1] - old[-1][1]

    def _headlines(self, title: str) -> list[str]:
        cache = getattr(getattr(self.prices, "cache", None), "news", None)
        if not cache:
            return []
        words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{4,}", title)} - {"which", "there", "their", "about", "after", "before", "price", "market"}
        out = []
        for a in list(cache.values())[-500:]:
            h = a.get("headline", "")
            if len(words & {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{4,}", h)}) >= 2:
                out.append(h)
        return out[-8:]

    def scan_news(self) -> list[str]:
        nw = self.k["news"]
        key = get_anthropic_key(self.secrets)
        if not key or not self._budget_ok():
            return []
        by_event: dict[str, list[dict]] = {}
        for m in self.universe.values():
            if self._news_candidate(m):
                by_event.setdefault(m["event"], []).append(m)
        if not by_event:
            return []
        now = self.now()
        open_events = {s["event"] for s in self.st["open"].values() if s["kind"] == "news"}
        scored = []
        for ev, ms in by_event.items():
            last = self.st["analyses"].get(ev)
            moved = max(abs(self._move(m["ticker"])) for m in ms)
            if last:
                age_h = (now - datetime.fromisoformat(last["ts"])).total_seconds() / 3600
                since = max(abs(mid(m) - last["mids"].get(m["ticker"], mid(m))) for m in ms)
                if age_h < nw["reanalyze_hours"] and since < nw["reanalyze_move"]:
                    continue
            title = self.events.get(ev, {}).get("title") or ms[0]["event_title"]
            heads = self._headlines(title)
            score = moved * 20 + math.log10(1 + sum(m["vol24"] for m in ms)) + (2 if heads else 0) \
                + (3 if ev in open_events else 0) + (0.5 if not last else 0)
            scored.append((score, ev, ms, title, heads))
        if not scored:
            return []
        scored.sort(key=lambda x: -x[0])
        score, ev, ms, title, heads = scored[0]
        ms = sorted(ms, key=lambda m: -m["vol24"])[:8]
        self._spend()
        try:
            res = self.claude_fn(key, nw["model"], title, ms, now, heads, nw["web_search"])
        except Exception as e:
            print(f"[kalshi] Claude forecast failed: {e!r}")
            return ["claude error"]
        self.st["analyses"][ev] = {"ts": now.isoformat(), "mids": {m["ticker"]: round(mid(m), 3) for m in ms},
                                   "probs": {t: v[0] for t, v in res["probs"].items()},
                                   "conf": {t: v[1] for t, v in res["probs"].items()}}
        done = [f"analysed {ev}"]
        best = None
        for m in ms:
            if m["ticker"] not in res["probs"] or self._cool(m["ticker"]):
                continue
            p, conf, reason = res["probs"][m["ticker"]]
            w = nw["shrink"].get(conf, 0.0)
            pm = mid(m)
            fair = pm + w * (p - pm)
            for side, f, ask in (("yes", fair, m["yes_ask"]), ("no", 1 - fair, m["no_ask"])):
                if not 0.03 <= ask <= 0.95:
                    continue
                edge = f - ask - fee_per_contract(ask, self.coef(m))
                if edge >= nw["min_edge"] and (best is None or edge > best[0]):
                    best = (edge, m, side, ask, f, p, conf, reason)
        if best and len(self.st["open"]) < self.k["risk"]["max_open"]:
            edge, m, side, ask, f, p, conf, reason = best
            why = [f"Claude: {p * 100:.0f}% YES ({conf} confidence) vs market {mid(m) * 100:.0f}% — {reason}"]
            why += [f"• {n}" for n in res["news"]]
            self._open_signal("news", m, side, ask, f, why, {"ai_p": p, "conf": conf})
            done.append(f"news {side} {m['ticker']}")
        return done

    # 3) mispricing -------------------------------------------
    def scan_mispricing(self) -> list[str]:
        mp = self.k["mispricing"]
        groups: dict[str, list[dict]] = {}
        for m in self.universe.values():
            groups.setdefault(m["event"], []).append(m)
        done = []
        for ev, ms in groups.items():
            meta = self.events.get(ev, {})
            if not meta.get("mutually_exclusive") or len(ms) < 2 or self._cool("arb:" + ev):
                continue
            # NO basket: at most one outcome can be YES, so across k legs at least k-1 NOs pay $1.
            # Profit = Σ(1 - cost_i) - 1, so every liquid leg priced under $1 helps.
            legs = [m for m in ms if 0 < m["no_ask"] < 1 and (not m["yes_bid_size"] or m["yes_bid_size"] >= mp["min_size"])]
            if len(legs) >= 2:
                no_cost = sum(m["no_ask"] + fee_per_contract(m["no_ask"], self.coef(m)) for m in legs)
                if (len(legs) - 1) - no_cost >= mp["min_profit"]:
                    self._arb_alert(ev, meta, legs, "NO", no_cost, len(legs) - 1)
                    done.append(f"arb NO {ev}")
                    continue
            # YES basket: only when the listed outcomes provably cover every possibility (complete ladder)
            yes_ok = all(0 < m["yes_ask"] < 1 and (not m["yes_ask_size"] or m["yes_ask_size"] >= mp["min_size"]) for m in ms)
            if yes_ok and ladder_complete(ms):
                yes_cost = sum(m["yes_ask"] + fee_per_contract(m["yes_ask"], self.coef(m)) for m in ms)
                if 1 - yes_cost >= mp["min_profit"]:
                    self._arb_alert(ev, meta, ms, "YES", yes_cost, 1)
                    done.append(f"arb YES {ev}")
        return done

    def _arb_alert(self, ev: str, meta: dict, ms: list[dict], side: str, cost: float, payout: float) -> None:
        legs = "\n".join(f"  {side} {m['sub'][:40]} @ ${m[side.lower() + '_ask']:.2f}" for m in ms[:15])
        msg = (f"{meta.get('title', ev)}\nBuy {side} on every outcome ({len(ms)} legs):\n{legs}\n"
               f"Cost ≈ ${cost:.2f} incl. fees per set → pays ${payout:.0f} → +${payout - cost:.2f} per set\n"
               "Only worth it if EVERY leg fills at these prices — check the order book first. "
               + ("Check that the listed outcomes cover every possibility." if side == "YES" else
                  "Safe because at most one outcome can resolve YES."))
        url = market_link(ms[0]["series"], ev, meta.get("title", ""))
        push(self.secrets, f"⚖️ KALSHI MISPRICING · {meta.get('title', ev)[:50]}", msg, tags=["scales"], priority=4,
             click=url, actions=[{"action": "view", "label": "Open in Kalshi", "url": url}])
        self._set_cool("arb:" + ev, 180)

    # exits ---------------------------------------------------
    def manage_open(self) -> list[str]:
        tickers = sorted({s["ticker"] for s in self.st["open"].values()})
        fresh = {m["ticker"]: norm_market(m) for m in self.client.markets(tickers)}
        r = self.k["risk"]
        done = []
        for sid, s in list(self.st["open"].items()):
            m = fresh.get(s["ticker"])
            if not m:
                continue
            side = s["side"]
            if m["status"] in SETTLED or m["result"] in ("yes", "no"):
                if m["result"] not in ("yes", "no"):
                    continue
                won = m["result"] == side
                pnl = (1.0 if won else 0.0) - s["entry"] - s.get("fee", 0)
                self._close(sid, s, "settled " + m["result"].upper(), 1.0 if won else 0.0, pnl,
                            f"{'✅ Won' if won else '❌ Lost'} — settled {m['result'].upper()}")
                done.append(f"settled {s['ticker']}")
                continue
            bid = m[side + "_bid"]
            fair = s.get("fair")
            if s["kind"] in ("index", "crypto"):
                p = (self._index_fair if s["kind"] == "index" else self._crypto_fair).get(s["ticker"])
                if p is not None:
                    fair = p if side == "yes" else 1 - p
                    s["fair"] = round(fair, 3)
            elif s["kind"] == "news":
                a = self.st["analyses"].get(s["event"], {})
                if s["ticker"] in a.get("probs", {}):
                    w = self.k["news"]["shrink"].get(a["conf"].get(s["ticker"], "low"), 0)
                    pm = a["mids"].get(s["ticker"], mid(m))
                    pf = pm + w * (a["probs"][s["ticker"]] - pm)
                    fair = pf if side == "yes" else 1 - pf
            gain = bid - s["entry"]
            why = None
            if bid > 0 and gain >= r["take_profit"]:
                why = f"🎯 Take profit (+{gain * 100:.0f}¢)"
            elif bid > 0 and fair is not None and bid >= fair - 0.01 and gain >= 0.03:
                why = f"🎯 Edge captured — price {bid:.2f} reached fair value {fair:.2f}"
            elif fair is not None and fair < s["entry"] - r["thesis_stop"]:
                why = f"🔄 Thesis changed — fair value now {fair:.2f} vs entry {s['entry']:.2f}"
            elif bid > 0 and gain <= -r["stop_loss"]:
                why = f"🛑 Stop loss ({gain * 100:.0f}¢)"
            if why:
                self._close(sid, s, why, bid, gain - s.get("fee", 0), why)
                done.append(f"exit {s['ticker']}")
        return done

    def _close(self, sid: str, s: dict, reason: str, exit_px: float, pnl: float, headline: str) -> None:
        settled = reason.startswith("settled")
        msg = (f"{s['title']}\n{headline}\n"
               + ("" if settled else f"Sell {s['side'].upper()} at ≥ ${exit_px:.2f} (bid)\n")
               + f"Entry ${s['entry']:.2f} → {'settled' if settled else 'now'} ${exit_px:.2f}  ·  "
                 f"P&L {pnl * 100:+.0f}¢/contract ({pnl * s['qty']:+.2f} on {s['qty']})\nTicker: {s['ticker']}")
        title = ("🏁 KALSHI SETTLED" if settled else f"KALSHI SELL {s['side'].upper()}") + f" · {s['title'][-60:]}"
        push(self.secrets, title, msg, tags=["checkered_flag" if settled else "moneybag"],
             priority=3 if settled else 5, click=market_link(s["series"], s.get("event"), s.get("title", "")),
             actions=[{"action": "view", "label": "Open in Kalshi",
                       "url": market_link(s["series"], s.get("event"), s.get("title", ""))}])
        s.update({"closed": self.now().isoformat(), "exit": exit_px, "pnl": round(pnl, 4), "reason": reason})
        self.st["closed"].append(s)
        del self.st["open"][sid]

    def status_text(self) -> str:
        day = self.now().astimezone(ET).date().isoformat()
        cands = sum(1 for m in self.universe.values() if self._news_candidate(m))
        cats: dict[str, int] = {}
        for m in self.universe.values():
            c = self.category(m) or "?"
            cats[c] = cats.get(c, 0) + 1
        top = ", ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda kv: -kv[1])[:6])
        lines = [f"Markets loaded: {len(self.universe):,} ({top})",
                 f"News candidates: {cands} · Claude analyses today {self.st['budget'].get(day, 0)}/"
                 f"{self.k['news']['max_analyses_per_day']} · key {'connected' if get_anthropic_key(self.secrets) else 'MISSING'}",
                 f"Crypto contracts priced: {self.stats.get('crypto_markets', 0)} · index contracts priced: {len(self._index_fair)}",
                 f"Open Kalshi signals: {len(self.st['open'])} · closed so far: {len(self.st['closed'])}"]
        if self.stats["last"]:
            lines.append("Recent: " + " | ".join(list(self.stats["last"].values())[-4:]))
        if self.stats["errors"]:
            lines.append("Errors: " + " | ".join(self.stats["errors"][-3:]))
        return "\n".join(lines)

    # recap ---------------------------------------------------
    def maybe_recap(self) -> None:
        now_et = self.now().astimezone(ET)
        day = now_et.date().isoformat()
        if now_et.hour < 20 or self.st.get("recap") == day:
            return
        self.st["recap"] = day
        closed = [c for c in self.st["closed"] if c.get("closed") and
                  datetime.fromisoformat(c["closed"]).astimezone(ET).date().isoformat() == day]
        opened = [c for c in list(self.st["open"].values()) + self.st["closed"]
                  if datetime.fromisoformat(c["opened"]).astimezone(ET).date().isoformat() == day]
        allc = [c["pnl"] for c in self.st["closed"] if c.get("pnl") is not None]
        lines = [f"New: {len(opened)} · closed: {len(closed)} · still open: {len(self.st['open'])}"]
        lines += [f"  {c['title'][-45:]} {c['side'].upper()}: {c['pnl'] * 100:+.0f}¢ ({c['reason'][:30]})" for c in closed]
        if allc:
            wins = sum(1 for x in allc if x > 0)
            lines.append(f"Track record: {len(allc)} signals · win rate {wins / len(allc) * 100:.0f}% · "
                         f"avg {sum(allc) / len(allc) * 100:+.1f}¢/contract")
            v2 = [c for c in self.st["closed"] if c.get("pnl") is not None and c["opened"] >= self.k.get("since", "")]
            for kind in ("index", "news", "crypto"):
                xs = [c["pnl"] for c in v2 if c["kind"] == kind]
                if xs:
                    lines.append(f"  {kind} (new model): {len(xs)} · avg {sum(xs) / len(xs) * 100:+.1f}¢")
        lines.append(f"Claude analyses used today: {self.st['budget'].get(day, 0)}/{self.k['news']['max_analyses_per_day']}")
        lines.append(f"Watching {len(self.universe):,} Kalshi markets · {self.stats.get('crypto_markets', 0)} crypto contracts priced")
        push(self.secrets, f"📊 Kalshi recap · {now_et:%a %b %d}", "\n".join(lines), tags=["bar_chart"], priority=2)
