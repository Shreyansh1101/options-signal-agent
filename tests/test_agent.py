"""Offline tests:  python -m unittest discover -s tests -v"""
import os
import sys
import tempfile
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
from signal_agent import indicators as ind  # noqa: E402
from signal_agent import notify  # noqa: E402
from signal_agent.agent import scan  # noqa: E402
from signal_agent.backtest import prepare, score_series, simulate, summarize  # noqa: E402
from signal_agent.config import Secrets, load_config  # noqa: E402
from signal_agent.options import parse_occ, pick_contract  # noqa: E402
from signal_agent.sentiment import score_text, symbol_news_scores  # noqa: E402
from signal_agent.state import empty_state, load_state, save_state  # noqa: E402
from signal_agent.strategy import entry_direction  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 9)
NEWS = [
    {"id": 1, "headline": "Nvidia beats estimates and raises full-year guidance", "summary": "",
     "symbols": ["NVDA"], "created_at": "2026-10-09T12:00:00Z", "source": "benzinga", "url": "https://example.com"},
    {"id": 2, "headline": "Tesla downgraded as deliveries miss expectations", "summary": "",
     "symbols": ["TSLA"], "created_at": "2026-10-09T13:00:00Z", "source": "benzinga", "url": None},
]
PROFILES = {"SPY": "up", "QQQ": "up", "IWM": "flat", "NVDA": "up", "TSLA": "down", "KO": "flat"}


def run_day(cfg=None, start=(9, 35), end=(16, 0)):
    cfg = cfg or load_config()
    f = FakeAlpaca(DAY, PROFILES, news=NEWS)
    st, sec = empty_state(), Secrets()
    notify.SENT.clear()
    t = datetime(DAY.year, DAY.month, DAY.day, *start, tzinfo=ET)
    reports = []
    while (t.hour, t.minute) < end:
        now = t.astimezone(timezone.utc)
        f._now = now
        reports.append((t, scan(f, cfg, sec, st, now)))
        t += timedelta(minutes=5)
    return f, st, reports, list(notify.SENT)


class TestSentiment(unittest.TestCase):
    def test_directions(self):
        self.assertGreater(score_text("Nvidia beats estimates and raises full-year guidance"), 0.6)
        self.assertLess(score_text("Tesla shares plunge after missing delivery expectations"), -0.4)
        self.assertLess(score_text("Apple downgraded to neutral"), 0)
        self.assertLess(score_text("Shares rise despite weak guidance"), 0)
        self.assertEqual(score_text("Microsoft to hold annual meeting"), 0)

    def test_recency_and_breadth(self):
        now = datetime(2026, 10, 9, 18, tzinfo=timezone.utc)
        fresh = {"id": 1, "headline": "XYZ upgraded at Goldman", "symbols": ["XYZ"], "created_at": "2026-10-09T17:30:00Z"}
        old = {"id": 2, "headline": "XYZ downgraded at MS", "symbols": ["XYZ"], "created_at": "2026-10-08T18:00:00Z"}
        s = symbol_news_scores([fresh, old], ["XYZ"], now, 6)["XYZ"]
        self.assertGreater(s["score"], 0.3)       # the fresh upgrade dominates the day-old downgrade
        self.assertEqual(s["count"], 2)


class TestIndicators(unittest.TestCase):
    def test_daily_levels_no_lookahead(self):
        f = FakeAlpaca(DAY, {"SPY": "up"})
        f._now = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
        five = f.bars(["SPY"], "5Min", datetime(2026, 9, 1, tzinfo=timezone.utc), f._now)["SPY"]
        daily = f.bars(["SPY"], "1Day", datetime(2026, 8, 1, tzinfo=timezone.utc), f._now)["SPY"]
        lv = ind.daily_levels(daily)
        d = ind.attach_daily(ind.add_intraday(ind.regular_session(five)), lv)
        last_session = d["session"].iloc[-1]
        prev = lv[lv["session"] < last_session].iloc[-1]
        self.assertAlmostEqual(d["d_ema20"].iloc[-1], prev["d_ema20"])
        same_day = lv[lv["session"] == last_session].iloc[-1]
        self.assertNotAlmostEqual(d["d_ema20"].iloc[-1], same_day["d_ema20"])

    def test_regular_session_only(self):
        t = pd.to_datetime(["2026-10-09T12:00:00Z", "2026-10-09T13:30:00Z", "2026-10-09T20:00:00Z"], utc=True)
        df = pd.DataFrame({"t": t, "o": 1.0, "h": 1.0, "l": 1.0, "c": 1.0, "v": 1})
        self.assertEqual(len(ind.regular_session(df)), 1)  # 8:00 ET and 16:00 ET dropped


class TestOptions(unittest.TestCase):
    def test_parse_occ(self):
        i = parse_occ("AAPL240426C00162500")
        self.assertEqual((i["root"], i["type"], i["strike"], i["expiry"].isoformat()), ("AAPL", "call", 162.5, "2024-04-26"))
        self.assertEqual(parse_occ("SPY261016P00650000")["type"], "put")

    def test_pick_respects_filters(self):
        cfg = load_config()
        today = date(2026, 10, 9)
        snaps = {
            "XYZ261012C00100000": {"latestQuote": {"bp": 2.0, "ap": 2.1}, "greeks": {"delta": 0.5}},   # 3 DTE ok-ish
            "XYZ261023C00100000": {"latestQuote": {"bp": 3.0, "ap": 3.1}, "greeks": {"delta": 0.52}},  # 14 DTE best
            "XYZ261023C00090000": {"latestQuote": {"bp": 10.5, "ap": 10.7}, "greeks": {"delta": 0.85}},  # too deep
            "XYZ261016C00100000": {"latestQuote": {"bp": 1.0, "ap": 2.0}, "greeks": {"delta": 0.5}},   # spread too wide
            "XYZ261010C00100000": {"latestQuote": {"bp": 1.0, "ap": 1.05}, "greeks": {"delta": 0.5}},  # 1 DTE
            "XYZ261023P00100000": {"latestQuote": {"bp": 3.0, "ap": 3.1}, "greeks": {"delta": -0.5}},  # wrong side
        }
        c, why = pick_contract(snaps, "call", 100.0, today, cfg)
        self.assertEqual(why, "ok")
        self.assertEqual(c["symbol"], "XYZ261023C00100000")
        none, why = pick_contract({}, "call", 100.0, today, cfg)
        self.assertIsNone(none)


class TestStrategy(unittest.TestCase):
    def test_confirmation_and_vetoes(self):
        cfg = load_config()
        news_bad = {"score": -0.6, "count": 2}
        self.assertEqual(entry_direction(0.7, 0.8, None, 1.5, cfg, [])[0], None)            # needs a 2nd scan
        self.assertEqual(entry_direction(0.7, 0.8, None, 1.5, cfg, [0.6])[0], "call")
        self.assertEqual(entry_direction(-0.7, -0.8, None, 1.5, cfg, [-0.6])[0], "put")
        self.assertEqual(entry_direction(0.7, 0.8, news_bad, 1.5, cfg, [0.6])[1], "news disagrees")
        self.assertEqual(entry_direction(0.7, 0.8, None, 0.5, cfg, [0.6])[1], "volume too light")
        self.assertEqual(entry_direction(0.4, 0.8, None, 1.5, cfg, [0.6])[0], None)


class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.f, cls.st, cls.reports, cls.sent = run_day()

    def test_buys_and_sells_fire(self):
        buys = [n for n in self.sent if n["title"].startswith("BUY")]
        sells = [n for n in self.sent if "SELL" in n["title"]]
        self.assertGreater(len(buys), 0)
        self.assertGreater(len(sells), 0)
        for b in buys:
            self.assertIn("Stop:", b["message"]); self.assertIn("Target:", b["message"])

    def test_entries_only_inside_window(self):
        for t, rep in self.reports:
            if rep["buys"]:
                self.assertTrue((9, 45) <= (t.hour, t.minute) < (15, 30), t)

    def test_never_two_open_on_same_symbol_and_cap(self):
        seen = {}
        for _, rep in self.reports:
            for b in rep["buys"]:
                seen.setdefault(b["underlying"], []).append(b)
        self.assertLessEqual(len(self.st["open"]), load_config()["risk"]["max_open_signals"])
        ids = [s["underlying"] for s in self.st["open"].values()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_excluded_and_odd_tickers_skipped(self):
        scanned = {r["symbol"] for _, rep in self.reports for r in rep["rows"]}
        self.assertNotIn("TQQQ", scanned)
        self.assertNotIn("BRK.B", scanned)

    def test_direction_matches_trend(self):
        for _, rep in self.reports:
            for b in rep["buys"]:
                if b["underlying"] == "TSLA":
                    self.assertEqual(b["side"], "put")
                if b["underlying"] == "NVDA":
                    self.assertEqual(b["side"], "call")

    def test_daily_recap_sent_once(self):
        recaps = [n for n in self.sent if n["title"].startswith("Daily recap")]
        self.assertEqual(len(recaps), 1)

    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            save_state(self.st, p)
            back = load_state(p)
            self.assertEqual(set(back["open"]), set(self.st["open"]))
            self.assertEqual(len(back["closed"]), len(self.st["closed"]))


class TestBacktest(unittest.TestCase):
    def test_simulate_uses_next_bar_open(self):
        cfg = load_config()
        f = FakeAlpaca(DAY, {"SPY": "up", "TSLA": "down"}, news=NEWS)
        f._now = datetime(2026, 10, 9, 20, 30, tzinfo=timezone.utc)
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        d = prepare(f.bars(["TSLA"], "5Min", start, f._now)["TSLA"], f.bars(["TSLA"], "1Day", start, f._now)["TSLA"])
        s = score_series(d, NEWS, "TSLA", cfg)
        trades = simulate(s, cfg)
        self.assertGreater(len(trades), 0)
        opens = set(s["o"].round(6))
        for t in trades:
            self.assertIn(round(t["entry"], 6), opens)
        self.assertGreater(summarize(trades)["n"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
