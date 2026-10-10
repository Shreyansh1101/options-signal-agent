"""Chart indicators. All functions work on full DataFrames so live scans and the backtest share one code path."""
from __future__ import annotations

from datetime import time as dtime

import numpy as np
import pandas as pd

ET = "America/New_York"
OPEN, CLOSE = dtime(9, 30), dtime(16, 0)


def regular_session(df: pd.DataFrame) -> pd.DataFrame:
    """Keep 9:30–16:00 ET bars; add `et` (ET timestamp) and `session` (ET date)."""
    if df.empty:
        return df.assign(et=pd.Series(dtype="datetime64[ns, America/New_York]"), session=pd.Series(dtype=object))
    d = df.copy()
    d["et"] = d["t"].dt.tz_convert(ET)
    tod = d["et"].dt.time
    d = d[(tod >= OPEN) & (tod < CLOSE)].copy()
    d["session"] = d["et"].dt.date
    return d.reset_index(drop=True)


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / down.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    prev = df["c"].shift(1)
    tr = pd.concat([df["h"] - df["l"], (df["h"] - prev).abs(), (df["l"] - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def add_intraday(df: pd.DataFrame) -> pd.DataFrame:
    """Expects regular-session 5-minute bars (from `regular_session`)."""
    d = df.copy()
    if d.empty:
        return d
    c = d["c"]
    d["ema9"] = ema(c, 9)
    d["ema21"] = ema(c, 21)
    d["rsi"] = rsi(c, 14)
    macd = ema(c, 12) - ema(c, 26)
    d["macd_hist"] = macd - ema(macd, 9)
    d["atr"] = atr(d, 14)

    # Session VWAP
    tp = (d["h"] + d["l"] + d["c"]) / 3
    pv = (tp * d["v"]).groupby(d["session"]).cumsum()
    vv = d["v"].groupby(d["session"]).cumsum().replace(0, np.nan)
    d["vwap"] = (pv / vv).fillna(c)

    # Relative volume: last 3 bars vs the same time-of-day over the previous (up to) 5 sessions
    d["vol3"] = d["v"].rolling(3, min_periods=1).sum()
    d["slot"] = d["et"].dt.strftime("%H:%M")
    base = d.groupby("slot")["vol3"].transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean())
    fallback = d["vol3"].shift(1).rolling(60, min_periods=5).mean()
    base = base.fillna(fallback).replace(0, np.nan)
    d["relvol"] = (d["vol3"] / base).fillna(1.0).clip(upper=10)
    return d


def daily_levels(daily: pd.DataFrame) -> pd.DataFrame:
    """Daily EMA20/EMA50/ATR per ET session date (values as of that day's close)."""
    cols = ["session", "d_ema20", "d_ema50", "d_atr", "d_close"]
    if daily is None or daily.empty:
        return pd.DataFrame(columns=cols)
    d = daily.copy()
    d["session"] = d["t"].dt.tz_convert(ET).dt.date
    d = d.drop_duplicates("session", keep="last").reset_index(drop=True)
    d["d_ema20"] = ema(d["c"], 20)
    d["d_ema50"] = ema(d["c"], 50)
    d["d_atr"] = atr(d, 14)
    d["d_close"] = d["c"]
    return d[cols]


def attach_daily(intraday: pd.DataFrame, levels: pd.DataFrame) -> pd.DataFrame:
    """For each intraday bar on session D, attach daily levels from the last session *before* D (no look-ahead)."""
    if intraday.empty:
        return intraday
    if levels is None or levels.empty:
        return intraday.assign(d_ema20=np.nan, d_ema50=np.nan, d_atr=np.nan)
    left = intraday.copy()
    left["_sd"] = pd.to_datetime(left["session"])
    right = levels[["session", "d_ema20", "d_ema50", "d_atr"]].copy()
    right["_sd"] = pd.to_datetime(right["session"])
    right = right.drop(columns="session").sort_values("_sd")
    m = pd.merge_asof(left.sort_values("_sd", kind="stable"), right, on="_sd", allow_exact_matches=False)
    return m.drop(columns="_sd").sort_values("t").reset_index(drop=True)
