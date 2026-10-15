"""Kalshi engine tests with a fake Kalshi API, fake prices and a fake Claude."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DRY_RUN"] = "1"

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from signal_agent import kalshi as K  # noqa: E402
from signal_agent import notify  # noqa: E402
from signal_agent.config import Secrets, load_config  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 13, 14, 0, tzinfo=ET).astimezone(timezone.utc)      # Tue 2pm ET
CLOSE = "2026-10-13T20:00:00Z"


def mk(ticker, event, sub, st, lo, hi, yb, ya, vol=5000, close=CLOSE, status="active", result=""):
    return {"ticker": ticker, "event_ticker": event, "yes_sub_title": sub, "strike_type": st,
            "floor_strike": lo, "cap_strike": hi, "yes_bid_dollars": f"{yb:.4f}", "yes_ask_dollars": f"{ya:.4f}",
            "no_bid_dollars": f"{1 - ya:.4f}", "no_ask_dollars": f"{1 - yb:.4f}",
            "yes_bid_size_fp": "500", "yes_ask_size_fp": "500", "volume_24h_fp": str(vol),
            "close_time": close, "status": status, "result": result, "rules_primary": f"Resolves on {sub}"}


class FakePrices:
    def __init__(self, spy=780.0):
        self.spy = spy
        rng = np.random.default_rng(1)
        t = pd.date_range("2026-10-07 13:30", periods=78 * 5, freq="5min", tz="UTC")
        t = t[(t.tz_convert(ET).time >= pd.Timestamp("09:30").time()) & (t.tz_convert(ET).time < pd.Timestamp("16:00").time())]
        c = 780 * np.exp(np.cumsum(rng.normal(0, 0.0007, len(t))))
        self.five = pd.DataFrame({"t": t, "o": c, "h": c, "l": c, "c": c, "v": 1000})
        days = pd.to_datetime(["2026-10-08 04:00", "2026-10-09 04:00", "2026-10-12 04:00"], utc=True)
        self.daily = pd.DataFrame({"t": days, "o": 780.0, "h": 785.0, "l": 775.0, "c": [779.0, 781.154, 779.5], "v": 1})

    def latest_prices(self, syms):
        return {"SPY": self.spy}

    def bars(self, syms, tf, start, end=None):
        return {"SPY": self.daily if tf == "1Day" else self.five}

    def option_chain(self, *a, **k):
        return {}


class FakeKalshi:
    def __init__(self, ladder, extra_markets=(), extra_events=()):
        self.ladder = {m["ticker"]: m for m in ladder}
        self.extra = {m["ticker"]: m for m in extra_markets}
        self.extra_events = list(extra_events)

    def events(self, series, status="open", limit=20):
        if series == "KXINX" and status == "settled":
            return [{"event_ticker": "KXINX-26OCT09H1600", "strike_date": "2026-10-09T20:00:00Z",
                     "markets": [{"expiration_value": "7811.54"}]}]
        if series == "KXINX" and status == "open":
            return [{"event_ticker": "KXINX-26OCT13H1600", "series_ticker": "KXINX", "mutually_exclusive": True,
                     "title": "S&P price range on Oct 13, 2026 at 4pm EDT?", "markets": list(self.ladder.values())}]
        return []

    def open_events(self, max_pages=40):
        return [{"event_ticker": "KXINX-26OCT13H1600", "series_ticker": "KXINX", "mutually_exclusive": True,
                 "title": "S&P range"}] + self.extra_events

    def open_markets(self, max_pages=40):
        return list(self.ladder.values()) + list(self.extra.values())

    def markets(self, tickers):
        allm = {**self.ladder, **self.extra}
        return [allm[t] for t in tickers if t in allm]

    def series(self):
        return [{"ticker": "KXINX", "category": "Financials"}, {"ticker": "KXFEDDECISION", "category": "Economics"},
                {"ticker": "KXWINNER", "category": "Sports"}]


def fair_ladder(S=7800.0, sigma=0.15 * 1.15, mins=120, mispriced=None):
    """Ladder priced at the model's fair value (±1¢ spread), with one bucket optionally mispriced."""
    out, edges = [], list(range(7700, 7925, 25))
    specs = [("T7700", "less", None, 7700)] + \
        [(f"B{a + 12}", "between", a, a + 24.9999) for a in edges[:-1]] + [("T7900", "greater", 7900, None)]
    for code, st, lo, hi in specs:
        m = {"strike_type": st, "floor": lo, "cap": hi}
        p = K.prob_yes(m, S, sigma, mins)
        yb, ya = max(0.0, round(p - 0.01, 2)), min(1.0, round(p + 0.01, 2))
        if mispriced and code == mispriced:
            yb, ya = 0.55, 0.57                          # bucket priced far above its neighbours → NO is cheap
        out.append(mk(f"KXINX-26OCT13H1600-{code}", "KXINX-26OCT13H1600", code, st, lo, hi, yb, ya))
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["NOTIFY_SETTINGS_PATH"] = str(Path(self.tmp.name) / "notify.json")
        self.cfg = load_config()
        self.sec = Secrets()
        self.sec.dry_run = True
        notify.SENT.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def engine(self, client, prices=None, claude=None, now=NOW):
        e = K.KalshiEngine(self.cfg, self.sec, prices or FakePrices(), client,
                           state_path=Path(self.tmp.name) / "k.json", claude_fn=claude or (lambda *a: {}),
                           now_fn=lambda: self.now)
        self.now = now
        return e


class TestModel(Base):
    def test_ladder_probabilities_sum_to_one(self):
        ms = [K.norm_market(m) for m in fair_ladder()]
        total = sum(K.prob_yes(m, 7800, 0.17, 120) for m in ms)
        self.assertAlmostEqual(total, 1.0, places=3)
        self.assertTrue(K.ladder_complete(ms))
        self.assertFalse(K.ladder_complete(ms[:3] + ms[4:]))   # a missing bucket is not exhaustive

    def test_fees(self):
        self.assertAlmostEqual(K.fee_per_contract(0.50, 0.07, 10), 0.018, places=3)   # 0.175 → rounds to $0.18 / 10
        self.assertEqual(K.fee_coef("KXINX"), 0.035)
        self.assertEqual(K.trading_minutes(NOW, datetime.fromisoformat("2026-10-13T20:00:00+00:00")), 120)


class TestIndex(Base):
    def ladder(self, **kw):
        """A ladder priced with the same volatility the engine will measure."""
        probe = self.engine(FakeKalshi([]))
        return fair_ladder(sigma=probe._sigma("SPY", 780.0), **kw)

    def test_fair_ladder_gives_no_signal(self):
        e = self.engine(FakeKalshi(self.ladder()))
        self.assertEqual(e.scan_index(), [])

    def test_mispriced_bucket_signals_then_exits_and_settles(self):
        fk = FakeKalshi(self.ladder(mispriced="B7812"))
        e = self.engine(fk)
        done = e.scan_index()
        self.assertEqual(len(done), 1, done)
        sig = next(iter(e.st["open"].values()))
        self.assertEqual((sig["ticker"], sig["side"], sig["entry"]), ("KXINX-26OCT13H1600-B7812", "no", 0.45))
        self.assertGreater(sig["fair"], 0.6)
        self.assertTrue(any(n["title"].startswith("📈 KALSHI BUY NO") for n in notify.SENT))
        # no duplicate on the next minute
        self.assertEqual(e.scan_index(), [])
        # price catches up to fair value → SELL alert
        fair = sig["fair"]                       # NO fair value → YES should trade near 1 - fair
        y = 1 - fair
        fk.ladder[sig["ticker"]].update({"yes_bid_dollars": f"{y - 0.01:.4f}", "yes_ask_dollars": f"{y:.4f}",
                                         "no_bid_dollars": f"{1 - y:.4f}", "no_ask_dollars": f"{1 - y + 0.01:.4f}"})
        e.scan_index()
        out = e.manage_open()
        self.assertEqual(out, [f"exit {sig['ticker']}"])
        self.assertTrue(any("KALSHI SELL NO" in n["title"] for n in notify.SENT))
        self.assertGreater(e.st["closed"][-1]["pnl"], 0.05)

    def test_settlement_alert(self):
        fk = FakeKalshi(self.ladder(mispriced="B7812"))
        e = self.engine(fk)
        e.scan_index()
        t = next(iter(e.st["open"].values()))["ticker"]
        fk.ladder[t].update({"status": "finalized", "result": "no"})
        e.manage_open()
        self.assertTrue(any(n["title"].startswith("🏁 KALSHI SETTLED") for n in notify.SENT))
        self.assertAlmostEqual(e.st["closed"][-1]["pnl"], 1 - 0.45 - e.st["closed"][-1]["fee"], places=4)

    def test_one_signal_per_event_and_level_bias_ignored(self):
        # our own index estimate is 40 points too low, but the ladder is fairly priced → no bet
        e = self.engine(FakeKalshi(self.ladder()), prices=FakePrices(spy=776.0))
        self.assertEqual(e.scan_index(), [])

    def test_bad_ratio_skips(self):
        e = self.engine(FakeKalshi(fair_ladder(mispriced="B7812")), prices=FakePrices(spy=78.0))
        e.cfg["kalshi"]["index"]["ratio_bounds"]["SPY"] = [9.0, 11.5]
        e._ratio.clear()
        # with SPY=78 the calibration day close (781) still gives ratio 10 → fine; break the daily bar instead
        e.prices.daily = e.prices.daily.iloc[:0]
        self.assertEqual(e.scan_index(), [])


class TestNewsAndArb(Base):
    def fed_event(self):
        ev = {"event_ticker": "KXFEDDECISION-26OCT", "series_ticker": "KXFEDDECISION", "mutually_exclusive": True,
              "title": "Fed decision in October?"}
        close = (NOW + timedelta(days=10)).isoformat().replace("+00:00", "Z")
        ms = [mk("KXFEDDECISION-26OCT-H0", ev["event_ticker"], "Hold", "custom", None, None, 0.60, 0.62, close=close),
              mk("KXFEDDECISION-26OCT-C25", ev["event_ticker"], "Cut 25bp", "custom", None, None, 0.36, 0.38, close=close)]
        return ev, ms

    def test_claude_forecast_shrunk_and_budgeted(self):
        ev, ms = self.fed_event()
        fk = FakeKalshi(fair_ladder(), ms, [ev])
        calls = []

        def fake_claude(key, model, title, markets, now, heads, ws):
            calls.append(title)
            return {"probs": {"KXFEDDECISION-26OCT-H0": (0.85, "high", "Officials signalled a pause"),
                              "KXFEDDECISION-26OCT-C25": (0.15, "high", "Futures price a hold")},
                    "news": ["Fed minutes lean hawkish"], "searches": 2}

        notify.save_settings({"anthropic_key": "sk-ant-test"})
        e = self.engine(fk, claude=fake_claude)
        e.refresh_universe()
        out = e.scan_news()
        self.assertEqual(calls, ["Fed decision in October?"])
        # market 61%, Claude 85% high → fair 0.61 + 0.6*0.24 = 0.754 → YES at 0.62 has ~13¢ edge
        sig = next(iter(e.st["open"].values()))
        self.assertEqual((sig["ticker"], sig["side"]), ("KXFEDDECISION-26OCT-H0", "yes"))
        self.assertAlmostEqual(sig["fair"], 0.754, places=2)
        self.assertIn("news yes KXFEDDECISION-26OCT-H0", out)
        # not re-analysed right away (no new move) and budget counted
        self.assertEqual(e.scan_news(), [])
        self.assertEqual(sum(e.st["budget"].values()), 1)

    def test_no_key_no_calls(self):
        ev, ms = self.fed_event()
        e = self.engine(FakeKalshi(fair_ladder(), ms, [ev]), claude=lambda *a: self.fail("called"))
        e.refresh_universe()
        self.assertEqual(e.scan_news(), [])

    def test_no_basket_arbitrage(self):
        ev = {"event_ticker": "KXWINNER-X", "series_ticker": "KXWINNER", "mutually_exclusive": True, "title": "Who wins?"}
        close = (NOW + timedelta(days=1)).isoformat().replace("+00:00", "Z")
        # YES bids sum to 1.20 → NOs too cheap: NO asks 0.55+0.55+0.70 = 1.80 < 2 payout
        ms = [mk("KXWINNER-X-A", "KXWINNER-X", "A", "custom", None, None, 0.45, 0.47, close=close),
              mk("KXWINNER-X-B", "KXWINNER-X", "B", "custom", None, None, 0.45, 0.47, close=close),
              mk("KXWINNER-X-C", "KXWINNER-X", "C", "custom", None, None, 0.30, 0.32, close=close)]
        e = self.engine(FakeKalshi(fair_ladder(), ms, [ev]))
        e.refresh_universe()
        out = e.scan_mispricing()
        self.assertIn("arb NO KXWINNER-X", out)
        self.assertNotIn("arb YES KXINX-26OCT13H1600", out)      # fairly priced ladder is not flagged
        self.assertTrue(any("MISPRICING" in n["title"] for n in notify.SENT))
        self.assertEqual(e.scan_mispricing(), [])               # cooldown


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestClaudeParsing(unittest.TestCase):
    def test_parses_final_text_after_search(self):
        from unittest import mock
        body = {"content": [
            {"type": "text", "text": "Let me search."},
            {"type": "server_tool_use", "name": "web_search", "input": {"query": "fed"}},
            {"type": "web_search_tool_result", "content": []},
            {"type": "text", "text": 'Here you go: {"markets": [{"ticker": "T1", "probability": 0.7, '
                                     '"confidence": "medium", "reason": "x"}], "key_news": ["a", "b"]}'}],
            "usage": {"server_tool_use": {"web_search_requests": 2}}}

        class R:
            status_code, text = 200, ""
            def json(self): return body
            def raise_for_status(self): pass

        m = K.norm_market(mk("T1", "E", "Hold", "custom", None, None, 0.5, 0.52))
        with mock.patch.object(K.requests, "post", return_value=R()) as post:
            out = K.claude_forecast("sk-ant-x", "claude-haiku-5-5", "Fed?", [m], NOW)
        self.assertEqual(out["probs"]["T1"], (0.7, "medium", "x"))
        self.assertEqual(out["searches"], 2)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["tools"][0]["name"], "web_search")
        self.assertEqual(sent["model"], "claude-haiku-5-5")


class FakeCrypto:
    def price(self, coin): return 82000.0 if coin == "BTC" else None
    def vol(self, coin): return 0.45 if coin == "BTC" else None


class CryptoKalshi(FakeKalshi):
    def __init__(self, yb, ya):
        super().__init__([])
        close = (NOW + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        self.btc = mk("KXBTC15M-26OCT131415-15", "KXBTC15M-26OCT131415", "Target Price: $82,000.00",
                      "greater_or_equal", 82000.0, None, yb, ya, close=close)

    def events(self, series, status="open", limit=20):
        if series == "KXBTC15M" and status == "open":
            return [{"event_ticker": "KXBTC15M-26OCT131415", "series_ticker": "KXBTC15M",
                     "title": "BTC Up or Down - 15 minutes", "markets": [self.btc]}]
        return []

    def markets(self, tickers):
        return [self.btc] if self.btc["ticker"] in tickers else []


class TestCrypto(Base):
    def test_cheap_up_contract_signals_and_fair_one_does_not(self):
        e = self.engine(CryptoKalshi(0.49, 0.51))
        e.crypto = FakeCrypto()
        self.assertEqual(e.scan_crypto(), [])                         # at-the-money ≈ 50% → fair
        fk = CryptoKalshi(0.30, 0.32)
        e2 = self.engine(fk)
        e2.crypto = FakeCrypto()
        out = e2.scan_crypto()
        self.assertEqual(len(out), 1, out)
        sig = next(iter(e2.st["open"].values()))
        self.assertEqual((sig["kind"], sig["side"], sig["entry"]), ("crypto", "yes", 0.32))
        self.assertAlmostEqual(sig["fair"], 0.5, delta=0.02)
        self.assertTrue(any(n["title"].startswith("₿ KALSHI BUY YES") for n in notify.SENT))
        # market reprices to fair → exit
        fk.btc.update({"yes_bid_dollars": "0.5000", "yes_ask_dollars": "0.5100"})
        e2.scan_crypto()
        self.assertEqual(e2.manage_open(), [f"exit {sig['ticker']}"])

    def test_status_text(self):
        e = self.engine(CryptoKalshi(0.49, 0.51))
        e.crypto = FakeCrypto()
        e.scan_crypto()
        txt = e.status_text()
        self.assertIn("Crypto contracts priced: 1", txt)
        self.assertIn("key MISSING", txt)
