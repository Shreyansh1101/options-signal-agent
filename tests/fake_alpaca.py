"""A fake Alpaca client with synthetic but realistic-looking data, for offline tests and demos."""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")


def _sessions(end_day: date, n: int) -> list[date]:
    days, d = [], end_day
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


class FakeAlpaca:
    """Profiles: 'up' trends up (strong on the last day), 'down' trends down, 'flat' chops."""

    def __init__(self, last_day: date, profiles: dict[str, str], seed: int = 7, news: list[dict] | None = None):
        self.rng = np.random.default_rng(seed)
        self.profiles = profiles
        self.days = _sessions(last_day, 40)
        self.series = {s: self._make(s, p) for s, p in profiles.items()}
        self._news = news or []
        self.calls = []

    def _make(self, sym, profile):
        rows = []
        px = 100.0 + 50 * (hash(sym) % 5)
        drift = {"up": 0.0006, "down": -0.0006, "flat": 0.0}[profile]
        for di, d in enumerate(self.days):
            last = di == len(self.days) - 1
            for k in range(78):
                t = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET) + timedelta(minutes=5 * k)
                dr = drift * (2.5 if last else 1.0)
                ret = dr + self.rng.normal(0, 0.0012)
                o = px
                c = px * (1 + ret)
                h = max(o, c) * (1 + abs(self.rng.normal(0, 0.0006)))
                l = min(o, c) * (1 - abs(self.rng.normal(0, 0.0006)))
                v = int(20000 * (2.5 if k < 6 or k > 70 else 1) * (2.2 if last and profile != "flat" else 1)
                        * (1 + abs(self.rng.normal(0, 0.3))))
                rows.append({"t": t.astimezone(timezone.utc), "o": o, "h": h, "l": l, "c": c, "v": v})
                px = c
        return pd.DataFrame(rows)

    def price_at(self, sym, now):
        df = self.series[sym]
        df = df[df["t"] <= now]
        return float(df.iloc[-1]["c"])

    # ── API surface used by the agent ──
    def clock(self):
        return {"is_open": True}

    def is_trading_day(self, day):
        return True

    def movers(self, top):
        return {"gainers": [{"symbol": s, "price": 150.0, "percent_change": 3.0} for s, p in self.profiles.items() if p == "up"],
                "losers": [{"symbol": s, "price": 150.0, "percent_change": -3.0} for s, p in self.profiles.items() if p == "down"]}

    def most_actives(self, top):
        return [{"symbol": s} for s in self.profiles] + [{"symbol": "BRK.B"}, {"symbol": "TQQQ"}]

    def bars(self, symbols, timeframe, start, end=None, max_pages=20):
        self.calls.append(("bars", timeframe))
        out = {}
        for s in symbols:
            if s not in self.series:
                continue
            df = self.series[s]
            df = df[(df["t"] >= start) & ((df["t"] + timedelta(minutes=5) <= end) if end else True)]
            if timeframe == "1Day":
                df = df.assign(d=df["t"].dt.tz_convert(ET).dt.date).groupby("d").agg(
                    t=("t", "first"), o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last"), v=("v", "sum")
                ).reset_index(drop=True)
            out[s] = df.reset_index(drop=True)
        return out

    def latest_prices(self, symbols):
        now = self._now
        return {s: self.price_at(s, now) for s in symbols if s in self.series}

    def news(self, symbols, start, end=None, max_pages=4):
        return [n for n in self._news if any(s in symbols for s in n["symbols"])
                and start <= datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")) <= (end or self._now)]

    def _option_mid(self, sym, occ_type, strike, expiry):
        S = self.price_at(sym, self._now)
        T = max((expiry - self._now.astimezone(ET).date()).days, 1) / 365
        sigma = 0.35
        d1 = (math.log(S / strike) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
        if occ_type == "C":
            return S * N(d1) - strike * N(d2), N(d1)
        return strike * N(-d2) - S * N(-d1), N(d1) - 1

    def _snap(self, sym, cp, strike, exp):
        mid, delta = self._option_mid(sym, cp, strike, exp)
        mid = max(mid, 0.05)
        half = max(0.02, mid * 0.03)
        return {"latestQuote": {"bp": round(mid - half, 2), "ap": round(mid + half, 2)},
                "greeks": {"delta": delta}, "impliedVolatility": 0.35}

    def option_chain(self, underlying, opt_type, exp_gte, exp_lte, strike_gte, strike_lte, max_pages=3):
        self.calls.append(("chain", underlying, opt_type))
        cp = "C" if opt_type == "call" else "P"
        today = self._now.astimezone(ET).date()
        exps = [today + timedelta(days=d) for d in range(1, 40) if (today + timedelta(days=d)).weekday() == 4]
        exps = [e for e in exps if exp_gte <= e.isoformat() <= exp_lte]
        S = self.price_at(underlying, self._now)
        out = {}
        for e in exps:
            for k in np.arange(round(S * 0.9), round(S * 1.1), 1.0):
                if strike_gte <= k <= strike_lte:
                    occ = f"{underlying}{e:%y%m%d}{cp}{int(round(k * 1000)):08d}"
                    out[occ] = self._snap(underlying, cp, float(k), e)
        return out

    def option_snapshots(self, contract_symbols):
        from signal_agent.options import parse_occ
        out = {}
        for occ in contract_symbols:
            i = parse_occ(occ)
            out[occ] = self._snap(i["root"], "C" if i["type"] == "call" else "P", i["strike"], i["expiry"])
        return out
