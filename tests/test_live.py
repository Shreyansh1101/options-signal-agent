"""Live-mode tests: bar aggregation, scanning from the stream cache, and a fake Alpaca websocket server."""
import asyncio
import json
import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ["DRY_RUN"] = "1"

import pandas as pd  # noqa: E402

from fake_alpaca import FakeAlpaca  # noqa: E402
from signal_agent import live as L  # noqa: E402
from signal_agent import notify  # noqa: E402
from signal_agent.agent import scan  # noqa: E402
from signal_agent.config import Secrets, load_config  # noqa: E402
from signal_agent.state import empty_state  # noqa: E402

try:
    import websockets  # noqa: F401
    HAVE_WS = True
except ImportError:
    HAVE_WS = False

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 9)


class TestCache(unittest.TestCase):
    def test_minutes_build_five_minute_bar(self):
        c = L.MarketCache()
        base = pd.Timestamp("2026-10-09T13:30:00Z")
        for i, (o, h, l, cl, v) in enumerate([(10, 11, 9.5, 10.5, 100), (10.5, 12, 10, 11.5, 200),
                                               (11.5, 11.8, 11, 11.2, 50), (11.2, 11.3, 10.8, 11, 70),
                                               (11, 11.1, 10.2, 10.4, 30)]):
            c.add_minute("X", base + pd.Timedelta(minutes=i), o, h, l, cl, v)
        c.add_minute("X", base + pd.Timedelta(minutes=5), 10.4, 10.6, 10.3, 10.5, 10)   # next bucket
        f = c.frame("X", base - pd.Timedelta(days=1))
        self.assertEqual(len(f), 2)
        b = f.iloc[0]
        self.assertEqual((b.o, b.h, b.l, b.c, b.v), (10, 12, 9.5, 10.4, 450))
        self.assertEqual(c.last_price["X"], 10.5)
        # an updated bar for minute 2 replaces it rather than double counting
        c.add_minute("X", base + pd.Timedelta(minutes=1), 10.5, 12.5, 10, 11.5, 210)
        b = c.frame("X", base - pd.Timedelta(days=1)).iloc[0]
        self.assertEqual((b.h, b.v), (12.5, 460))


class TestScanFromCache(unittest.TestCase):
    def test_day_of_live_scans(self):
        news = [{"id": 1, "headline": "Tesla downgraded as deliveries miss expectations", "summary": "",
                 "symbols": ["TSLA"], "created_at": "2026-10-09T13:00:00Z", "source": "benzinga", "url": None}]
        f = FakeAlpaca(DAY, {"SPY": "up", "QQQ": "up", "IWM": "flat", "NVDA": "up", "TSLA": "down"}, news=news)
        t = datetime(2026, 10, 9, 9, 40, tzinfo=ET)
        f._now = t.astimezone(timezone.utc)
        cache = L.MarketCache()
        lc = L.LiveClient(f, cache, now_fn=lambda: f._now)
        cfg = load_config()
        cfg["scoring"]["confirm_cycles"] = cfg["live"]["confirm_cycles"]
        st, sec = empty_state(), Secrets()
        notify.SENT.clear()
        fed = set()
        while (t.hour, t.minute) < (15, 59):
            f._now = t.astimezone(timezone.utc)
            # simulate the stream: deliver each new bar from the fake market as it completes
            for sym, df in f.series.items():
                done = df[(df["t"] + timedelta(minutes=5) <= f._now) & (df["t"] >= f._now - timedelta(minutes=30))]
                for r in done.itertuples(index=False):
                    if (sym, r.t) not in fed:
                        fed.add((sym, r.t))
                        cache.add_minute(sym, r.t, r.o, r.h, r.l, r.c, r.v)
            scan(lc, cfg, sec, st, f._now)
            t += timedelta(minutes=1)
        titles = [n["title"] for n in notify.SENT]
        self.assertTrue(any(x.startswith("BUY PUT · TSLA") for x in titles), titles)
        self.assertTrue(any("SELL" in x for x in titles), titles)
        self.assertLessEqual(sum(1 for c in f.calls if c == ("bars", "5Min")), 3)   # history seeded once, not every minute
        self.assertTrue(lc.universe)


@unittest.skipUnless(HAVE_WS, "websockets not installed")
class TestWebsocket(unittest.TestCase):
    def test_stream_subscribe_bars_news_and_reconnect(self):
        from websockets.asyncio.server import serve

        seen = {"stock_conns": 0, "subs": [], "news_subs": 0}
        got_news = []

        async def stock_handler(ws):
            seen["stock_conns"] += 1
            await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
            auth = json.loads(await ws.recv())
            assert auth["action"] == "auth" and auth["key"] == "k"
            await ws.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
            sub = json.loads(await ws.recv())
            seen["subs"].append(sub)
            await ws.send(json.dumps([{"T": "subscription", "bars": sub["bars"]}]))
            bars = [{"T": "b", "S": s, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 1000,
                     "t": "2026-10-09T14:00:00Z"} for s in sub["bars"]]
            await ws.send(json.dumps(bars))
            if seen["stock_conns"] == 1:
                await asyncio.sleep(0.2)
                await ws.close()                     # force a reconnect
            else:
                await asyncio.sleep(5)

        async def news_handler(ws):
            await ws.send(json.dumps([{"T": "success", "msg": "connected"}]))
            json.loads(await ws.recv())
            await ws.send(json.dumps([{"T": "success", "msg": "authenticated"}]))
            sub = json.loads(await ws.recv())
            assert sub == {"action": "subscribe", "news": ["*"]}
            seen["news_subs"] += 1
            await ws.send(json.dumps([{"T": "n", "id": 9, "headline": "NVDA upgraded at Example", "summary": "",
                                       "symbols": ["NVDA"], "created_at": "2026-10-09T14:00:30Z", "source": "benzinga"}]))
            await asyncio.sleep(5)

        async def go():
            async with serve(stock_handler, "127.0.0.1", 0) as s1, serve(news_handler, "127.0.0.1", 0) as s2:
                L.STOCK_WS = f"ws://127.0.0.1:{s1.sockets[0].getsockname()[1]}"
                L.NEWS_WS = f"ws://127.0.0.1:{s2.sockets[0].getsockname()[1]}"
                sec = Secrets(); sec.alpaca_key, sec.alpaca_secret = "k", "s"
                cache = L.MarketCache()
                lc = L.LiveClient(None, cache)
                orig_sleep = asyncio.sleep

                async def fast_sleep(d, *a, **k):     # make reconnect backoff instant in tests
                    return await orig_sleep(min(d, 0.05), *a, **k)

                L.asyncio.sleep = fast_sleep
                try:
                    tasks = [asyncio.create_task(L.stock_stream(sec, lc, lambda: ["SPY", "NVDA"], lambda: None)),
                             asyncio.create_task(L.news_stream(sec, lc, got_news.append))]
                    for _ in range(100):
                        await orig_sleep(0.05)
                        if seen["stock_conns"] >= 2 and got_news:
                            break
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                finally:
                    L.asyncio.sleep = orig_sleep
                return cache, lc

        cache, lc = asyncio.run(go())
        self.assertGreaterEqual(seen["stock_conns"], 2, "client should reconnect after the server drops it")
        self.assertEqual(sorted(seen["subs"][0]["bars"]), ["NVDA", "SPY"])
        self.assertEqual(cache.last_price.get("SPY"), 100.5)
        self.assertEqual(got_news[0]["symbols"], ["NVDA"])
        self.assertIn("9", cache.news)


if __name__ == "__main__":
    unittest.main(verbosity=2)
