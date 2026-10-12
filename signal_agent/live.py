"""Always-on live mode for a server.

• Streams 1-minute bars (IEX, up to 30 symbols on the free plan) and all breaking news over Alpaca websockets.
• Re-scores the universe every minute, right after each minute bar closes, and within seconds of breaking news.
• Falls back to REST polling automatically if a stream drops, so a hiccup never silences the agent.

Run:  python -m signal_agent live
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

from .agent import ET, scan, send_summary, write_step_summary
from .alpaca import AlpacaClient
from .config import Secrets, load_config
from .notify import poll_commands, push
from .state import load_state, save_state

STOCK_WS = os.environ.get("ALPACA_STOCK_WS", "wss://stream.data.alpaca.markets/v2/iex")
NEWS_WS = os.environ.get("ALPACA_NEWS_WS", "wss://stream.data.alpaca.markets/v1beta1/news")
MAX_STREAM_SYMBOLS = 30          # free-plan limit
FIVE = pd.Timedelta(minutes=5)


def log(*a):
    print(datetime.now(ET).strftime("%H:%M:%S"), *a, flush=True)


def _ts(x) -> pd.Timestamp:
    return pd.Timestamp(x).tz_convert("UTC") if pd.Timestamp(x).tzinfo else pd.Timestamp(x).tz_localize("UTC")


# ─────────────────────────────────────────────────────────────
class MarketCache:
    """Thread-safe store of 5-minute bars (built from streamed 1-minute bars), daily bars and news."""

    def __init__(self):
        self.lock = threading.RLock()
        self.five: dict[str, dict[pd.Timestamp, dict]] = {}      # sym → bucket → ohlcv
        self.minutes: dict[str, dict[pd.Timestamp, dict]] = {}   # sym → minute → ohlcv (recent only)
        self.daily: dict[str, pd.DataFrame] = {}
        self.daily_day: dict[str, str] = {}
        self.last_price: dict[str, float] = {}
        self.last_bar_at: dict[str, pd.Timestamp] = {}          # minute timestamp of last bar received
        self.news: dict[str, dict] = {}
        self.news_seeded: set[str] = set()
        self.seeded: set[str] = set()

    # bars ---------------------------------------------------
    def seed_five(self, sym: str, df: pd.DataFrame, upto: pd.Timestamp) -> None:
        with self.lock:
            b = self.five.setdefault(sym, {})
            for r in df.itertuples(index=False):
                t = _ts(r.t)
                if t < upto and t not in b:
                    b[t] = {"o": r.o, "h": r.h, "l": r.l, "c": r.c, "v": r.v}
            if len(df):
                self.last_price.setdefault(sym, float(df["c"].iloc[-1]))
            self.seeded.add(sym)

    def add_minute(self, sym: str, t, o, h, l, c, v) -> None:
        t = _ts(t)
        bucket = t.floor("5min")
        with self.lock:
            mins = self.minutes.setdefault(sym, {})
            mins[t] = {"o": o, "h": h, "l": l, "c": c, "v": v}
            inb = [mins[m] for m in sorted(mins) if m.floor("5min") == bucket]
            self.five.setdefault(sym, {})[bucket] = {
                "o": inb[0]["o"], "h": max(x["h"] for x in inb), "l": min(x["l"] for x in inb),
                "c": inb[-1]["c"], "v": sum(x["v"] for x in inb),
            }
            if t >= self.last_bar_at.get(sym, pd.Timestamp(0, tz="UTC")):
                self.last_bar_at[sym] = t
                self.last_price[sym] = float(c)
            cutoff = t - pd.Timedelta(minutes=15)
            for m in [m for m in mins if m < cutoff]:
                del mins[m]

    def frame(self, sym: str, start: pd.Timestamp) -> pd.DataFrame | None:
        with self.lock:
            b = self.five.get(sym)
            if not b:
                return None
            for k in [k for k in b if k < start - pd.Timedelta(days=2)]:
                del b[k]
            rows = [{"t": k, **v} for k, v in sorted(b.items()) if k >= start]
        return pd.DataFrame(rows) if rows else None

    # news ---------------------------------------------------
    def add_news(self, a: dict) -> None:
        with self.lock:
            self.news[str(a.get("id"))] = a
            if len(self.news) > 4000:
                for k in sorted(self.news, key=lambda k: self.news[k].get("created_at", ""))[:1000]:
                    del self.news[k]

    def news_for(self, symbols: list[str], start: datetime, end: datetime) -> list[dict]:
        want = set(symbols)
        s, e = start.isoformat(), end.isoformat()
        out = []
        with self.lock:
            for a in self.news.values():
                ca = a.get("created_at", "")
                try:
                    t = datetime.fromisoformat(ca.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if start <= t <= end and want.intersection(a.get("symbols", [])):
                    out.append(a)
        return sorted(out, key=lambda a: a.get("created_at", ""), reverse=True)


# ─────────────────────────────────────────────────────────────
class LiveClient:
    """Same interface the scanner uses, served from the stream cache (REST only for gaps and options)."""

    def __init__(self, rest: AlpacaClient, cache: MarketCache, now_fn=None):
        self.rest, self.cache = rest, cache
        self.now = now_fn or (lambda: datetime.now(timezone.utc))
        self._memo: dict[str, tuple[float, object]] = {}
        self.universe: list[str] = []
        self._last_news_poll = 0.0
        self.stream_ok = {"stocks": False, "news": False}

    def _cached(self, key, ttl, fn):
        hit = self._memo.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        self._memo[key] = (time.time(), val)
        return val

    # market status / screener (cached)
    def clock(self):
        return self._cached("clock", 30, self.rest.clock)

    def is_trading_day(self, day):
        return self._cached(f"td{day}", 3600, lambda: self.rest.is_trading_day(day))

    def movers(self, top):
        return self._cached(f"movers{top}", 300, lambda: self.rest.movers(top))

    def most_actives(self, top):
        return self._cached(f"actives{top}", 300, lambda: self.rest.most_actives(top))

    # bars
    def _ensure_seeded(self, symbols: list[str]) -> None:
        need = [s for s in symbols if s not in self.cache.seeded]
        if not need:
            return
        now = self.now()
        bucket = pd.Timestamp(now).floor("5min")
        log(f"seeding history for {len(need)} symbols")
        five = self.rest.bars(need, "5Min", now - timedelta(days=8), bucket.to_pydatetime())
        for s in need:
            self.cache.seed_five(s, five.get(s, pd.DataFrame(columns=["t", "o", "h", "l", "c", "v"])), bucket)
        self._backfill_minutes(need, bucket.to_pydatetime(), now)

    def _backfill_minutes(self, symbols, start, end):
        if not symbols or end <= start:
            return
        try:
            mins = self.rest.bars(symbols, "1Min", start, end)
        except Exception as e:
            log(f"[backfill] {e}")
            return
        for s, df in mins.items():
            for r in df.itertuples(index=False):
                self.cache.add_minute(s, r.t, r.o, r.h, r.l, r.c, r.v)

    def _refresh_stale(self, symbols: list[str]) -> None:
        """If the stream missed a symbol (dropped connection, not subscribed yet), fill from REST."""
        now = pd.Timestamp(self.now())
        stale = [s for s in symbols if now - self.cache.last_bar_at.get(s, pd.Timestamp(0, tz="UTC")) > pd.Timedelta(minutes=3)]
        if stale:
            self._backfill_minutes(stale, (now - pd.Timedelta(minutes=10)).floor("5min").to_pydatetime(), now.to_pydatetime())

    def bars(self, symbols, timeframe, start, end=None, max_pages=20):
        if timeframe == "1Day":
            today = self.now().astimezone(ET).date().isoformat()
            need = [s for s in symbols if self.cache.daily_day.get(s) != today]
            if need:
                got = self.rest.bars(need, "1Day", start, end)
                with self.cache.lock:
                    for s in need:
                        if s in got:
                            self.cache.daily[s] = got[s]
                        self.cache.daily_day[s] = today
            return {s: self.cache.daily[s] for s in symbols if s in self.cache.daily}
        if timeframe != "5Min":
            return self.rest.bars(symbols, timeframe, start, end, max_pages)
        self.universe = list(symbols)
        self._ensure_seeded(symbols)
        self._refresh_stale(symbols)
        st = pd.Timestamp(start)
        out = {}
        for s in symbols:
            f = self.cache.frame(s, st)
            if f is not None:
                out[s] = f
        return out

    def latest_prices(self, symbols):
        with self.cache.lock:
            return {s: self.cache.last_price[s] for s in symbols if s in self.cache.last_price}

    def news(self, symbols, start, end=None, max_pages=4):
        need = [s for s in symbols if s not in self.cache.news_seeded]
        if need:
            try:
                for a in self.rest.news(need, start, end, max_pages):
                    self.cache.add_news(a)
                self.cache.news_seeded.update(need)
            except Exception as e:
                log(f"[news seed] {e}")
        if not self.stream_ok["news"] and time.time() - self._last_news_poll > 120:
            # news stream down → poll recent headlines every ~2 min instead
            self._last_news_poll = time.time()
            try:
                for a in self.rest.news(symbols, self.now() - timedelta(minutes=30), None, 1):
                    self.cache.add_news(a)
            except Exception as e:
                log(f"[news poll] {e}")
        return self.cache.news_for(symbols, start, end or self.now())

    # options → straight to REST
    def option_chain(self, *a, **k):
        return self.rest.option_chain(*a, **k)

    def option_snapshots(self, syms):
        return self.rest.option_snapshots(syms)


# ─────────────────────────────────────────────────────────────
async def _ws_session(url, key, secret, on_ready, on_msgs, label, status: dict):
    """Connect, authenticate, then hand control to `on_ready(ws)` while dispatching messages."""
    from websockets.asyncio.client import connect

    backoff = 3
    while True:
        try:
            async with connect(url, ping_interval=20, ping_timeout=20, max_size=2 ** 23, open_timeout=15) as ws:
                await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
                authed = False
                while not authed:
                    for m in json.loads(await asyncio.wait_for(ws.recv(), 15)):
                        if m.get("T") == "success" and m.get("msg") == "authenticated":
                            authed = True
                        elif m.get("T") == "error":
                            raise RuntimeError(f"{label} auth error {m.get('code')}: {m.get('msg')}")
                log(f"[{label}] stream connected")
                status[label] = True
                backoff = 3
                pump = asyncio.create_task(on_ready(ws))
                try:
                    async for raw in ws:
                        msgs = json.loads(raw)
                        for m in msgs:
                            if m.get("T") == "error":
                                log(f"[{label}] error {m.get('code')}: {m.get('msg')}")
                        on_msgs(msgs)
                finally:
                    pump.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[{label}] stream down: {e!r} — retrying in {backoff}s (REST fallback active)")
        status[label] = False
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def stock_stream(secrets, live: LiveClient, wanted, on_bar):
    async def sync_subs(ws):
        current: set[str] = set()
        while True:
            want = set(list(wanted())[:MAX_STREAM_SYMBOLS])
            rem, add = current - want, want - current
            if rem:
                await ws.send(json.dumps({"action": "unsubscribe", "bars": sorted(rem), "updatedBars": sorted(rem)}))
            if add:
                await ws.send(json.dumps({"action": "subscribe", "bars": sorted(add), "updatedBars": sorted(add)}))
            if rem or add:
                log(f"[stocks] watching {len(want)} symbols")
            current = want
            await asyncio.sleep(5)

    def handle(msgs):
        got = False
        for m in msgs:
            if m.get("T") in ("b", "u"):
                live.cache.add_minute(m["S"], m["t"], m["o"], m["h"], m["l"], m["c"], m["v"])
                got = True
        if got:
            on_bar()

    await _ws_session(STOCK_WS, secrets.alpaca_key, secrets.alpaca_secret, sync_subs, handle, "stocks", live.stream_ok)


async def news_stream(secrets, live: LiveClient, on_news):
    async def subscribe(ws):
        await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
        await asyncio.Event().wait()

    def handle(msgs):
        for m in msgs:
            if m.get("T") == "n":
                live.cache.add_news(m)
                on_news(m)

    await _ws_session(NEWS_WS, secrets.alpaca_key, secrets.alpaca_secret, subscribe, handle, "news", live.stream_ok)


# ─────────────────────────────────────────────────────────────
class Runner:
    def __init__(self, rest=None, now_fn=None):
        self.cfg, self.secrets = load_config(), Secrets()
        self.rest = rest or AlpacaClient(self.secrets.alpaca_key, self.secrets.alpaca_secret)
        self.cache = MarketCache()
        self.live = LiveClient(self.rest, self.cache, now_fn)
        self.now = self.live.now
        self.st = load_state()
        self.wake = asyncio.Event()
        self.scan_lock = threading.Lock()
        self.failures = 0
        self.last_fail_alert = 0.0

    def live_cfg(self) -> dict:
        cfg = load_config()                       # re-read each scan so config edits apply without restart
        cfg["scoring"]["confirm_cycles"] = cfg["live"]["confirm_cycles"]
        return cfg

    def wanted(self) -> list[str]:
        base = [s["underlying"] for s in self.st["open"].values()] + ["SPY"] + \
               self.cfg["universe"]["always_watch"] + self.cfg["universe"]["watchlist"]
        return list(dict.fromkeys(base + self.live.universe))

    def on_news(self, a: dict) -> None:
        if self.cfg["live"]["rescan_on_news"] and set(a.get("symbols", [])) & set(self.wanted()):
            log(f"[news] {','.join(a.get('symbols', [])[:4])}: {a.get('headline', '')[:90]}")
            self._loop.call_soon_threadsafe(self.wake.set)

    def run_scan(self) -> None:
        if not self.scan_lock.acquire(blocking=False):
            return
        try:
            cfg = self.live_cfg()
            self.cfg = cfg
            now = self.now()
            rep = scan(self.live, cfg, self.secrets, self.st, now)
            save_state(self.st)
            top = sorted(rep["rows"], key=lambda r: -abs(r["score"]))[:3]
            log(f"scan ok: {len(rep['rows'])} symbols · top " +
                ", ".join(f"{r['symbol']} {r['score']:+.2f}" for r in top) +
                (f" · BUY {len(rep['buys'])}" if rep["buys"] else "") + (f" · SELL {len(rep['sells'])}" if rep["sells"] else ""))
            self.failures = 0
        except Exception as e:
            self.failures += 1
            log(f"scan failed: {e!r}")
            traceback.print_exc()
            if self.failures >= 3 and time.time() - self.last_fail_alert > 3600:
                self.last_fail_alert = time.time()
                push(self.secrets, "⚠️ Signal agent having trouble", f"{self.failures} scans in a row failed: {e!r}"[:300],
                     tags=["warning"], priority=4)
        finally:
            self.scan_lock.release()

    def housekeeping(self, clock: dict) -> None:
        now_et = self.now().astimezone(ET)
        today = now_et.date().isoformat()
        if (not clock.get("is_open") and self.cfg["notifications"]["daily_summary"] and 16 <= now_et.hour < 18
                and self.st.get("summary_sent") != today and self.live.is_trading_day(today)):
            send_summary(self.cfg, self.secrets, self.st, now_et.date())
            save_state(self.st)
        if (self.cfg["live"]["morning_check"] and now_et.weekday() < 5 and (9, 20) <= (now_et.hour, now_et.minute) < (9, 30)
                and self.st.get("morning_sent") != today and self.live.is_trading_day(today)):
            self.st["morning_sent"] = today
            save_state(self.st)
            ok = self.live.stream_ok
            push(self.secrets, "☀️ Ready for the open",
                 f"Live price stream: {'on' if ok['stocks'] else 'reconnecting (REST backup on)'} · "
                 f"news stream: {'on' if ok['news'] else 'reconnecting (polling backup on)'}\n"
                 f"Open signals: {len(self.st['open'])}. New BUY alerts from "
                 f"{self.cfg['trading_window']['start']} to {self.cfg['trading_window']['end']} ET.",
                 tags=["sunny"], priority=2)

    async def scheduler(self) -> None:
        while True:
            # wake ~4s after each minute so that minute's bars have arrived — or immediately on breaking news
            now = time.time()
            nxt = (now // 60 + 1) * 60 + 4
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=max(0.5, nxt - now))
                await asyncio.sleep(2)               # let related headlines/bars land
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                clock = await asyncio.to_thread(self.live.clock)
            except Exception as e:
                log(f"clock failed: {e!r}")
                continue
            await asyncio.to_thread(self.housekeeping, clock)
            if clock.get("is_open"):
                await asyncio.to_thread(self.run_scan)

    async def main(self) -> None:
        self._loop = asyncio.get_running_loop()
        log("signal agent (live) starting")
        try:
            await asyncio.to_thread(self.rest.clock)
        except Exception as e:
            push(self.secrets, "⚠️ Alpaca keys problem", f"{e}"[:300] + "\nCheck the keys in your setup script.",
                 tags=["warning"], priority=4)
            log(f"Alpaca check failed: {e!r}")
        today = self.now().astimezone(ET).date().isoformat()
        if self.st.get("started") != today:          # one hello per day, not on every auto-update restart
            self.st["started"] = today
            save_state(self.st)
            push(self.secrets, "✅ Signal agent is running",
                 "Live mode on your server: streaming prices + news, re-scoring every minute during market hours.",
                 tags=["white_check_mark"], priority=3)
        await asyncio.gather(
            stock_stream(self.secrets, self.live, self.wanted, lambda: None),
            news_stream(self.secrets, self.live, self.on_news),
            self.scheduler(),
            self.command_loop(),
            self.kalshi_loop(),
        )

    async def kalshi_loop(self) -> None:
        """Kalshi signals run 24/7 (index markets only while the stock market is open)."""
        from .kalshi import KalshiEngine
        eng = KalshiEngine(self.cfg, self.secrets, self.live, now_fn=self.now)
        await asyncio.sleep(20)                    # let the price stream warm up
        while True:
            try:
                eng.cfg, eng.k = self.cfg, self.cfg["kalshi"]      # pick up config.yaml edits
                try:
                    is_open = bool((await asyncio.to_thread(self.live.clock)).get("is_open"))
                except Exception:
                    is_open = False                # Alpaca hiccup shouldn't stop non-index Kalshi signals
                done = await asyncio.to_thread(eng.tick, is_open)
                if done:
                    log("[kalshi] " + "; ".join(done))
            except Exception as e:
                log(f"[kalshi] {e!r}")
            await asyncio.sleep(30)

    async def command_loop(self) -> None:
        """Every 30s: check the ntfy topic for setup commands (e.g. 'telegram <bot key>')."""
        while True:
            try:
                done = await asyncio.to_thread(poll_commands, self.secrets)
                if done:
                    log(f"[commands] {', '.join(done)}")
            except Exception as e:
                log(f"[commands] {e!r}")
            await asyncio.sleep(30)


def run_live() -> int:
    try:
        asyncio.run(Runner().main())
    except KeyboardInterrupt:
        pass
    return 0
