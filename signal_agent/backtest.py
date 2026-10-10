"""Backtest the signal rules on past data (run it from the GitHub Actions tab → 'Run workflow' → backtest).

What it measures: every BUY CALL / BUY PUT the rules would have fired, and what the *stock* did afterwards,
using the same chart scoring, news scoring (keyword model), confirmation, stops, flips and time stops.
What it can't measure for free: historical option prices. Option P&L is roughly the stock move × leverage
minus time decay, so treat this as a test of whether the signals point the right way — not of option profits.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import indicators as ind
from .agent import ET, in_window
from .notify import push
from .sentiment import symbol_news_scores
from .strategy import chart_score, combine, entry_direction

DEFAULT_SYMBOLS = ["SPY", "QQQ", "AAPL", "NVDA", "TSLA", "AMD", "META", "AMZN", "MSFT", "GOOGL"]


def prepare(bars5: pd.DataFrame, daily: pd.DataFrame | None) -> pd.DataFrame:
    d = ind.regular_session(bars5)
    d = ind.add_intraday(d)
    d = ind.attach_daily(d, ind.daily_levels(daily) if daily is not None else None)
    return d.reset_index(drop=True)


def score_series(d: pd.DataFrame, articles: list[dict], sym: str, cfg: dict,
                 market: dict | None = None) -> pd.DataFrame:
    sc = cfg["scoring"]
    rows = d.to_dict("records")
    arts = sorted(articles, key=lambda a: a.get("created_at", ""))
    times = [datetime.fromisoformat(a["created_at"].replace("Z", "+00:00")) for a in arts]
    chart, news_s, news_n, total = [], [], [], []
    lo = 0
    look = timedelta(hours=sc["news_lookback_hours"])
    hi = 0
    for i, r in enumerate(rows):
        prev = rows[i - 1]["macd_hist"] if i else None
        c, _ = chart_score(r, prev)
        t_end = r["t"].to_pydatetime() + timedelta(minutes=5)
        while hi < len(arts) and times[hi] <= t_end:
            hi += 1
        while lo < hi and times[lo] < t_end - look:
            lo += 1
        nw = symbol_news_scores(arts[lo:hi], [sym], t_end, sc["news_half_life_hours"]).get(sym) if hi > lo else None
        m = None if (market is None or sym == "SPY") else market.get(r["t"])
        chart.append(c)
        news_s.append(nw["score"] if nw else np.nan)
        news_n.append(nw["count"] if nw else 0)
        total.append(combine(c, nw, cfg, m))
    out = d.copy()
    out["chart"], out["news"], out["n_news"], out["score"] = chart, news_s, news_n, total
    return out


def simulate(s: pd.DataFrame, cfg: dict) -> list[dict]:
    """Walk forward through scored bars; one position at a time per symbol."""
    rk, sc = cfg["risk"], cfg["scoring"]
    trades, pos, hist, cooldown_until = [], None, [], None
    rows = s.to_dict("records")
    for i, r in enumerate(rows[:-1]):
        et = r["et"].to_pydatetime() + timedelta(minutes=5)   # decision time = bar close
        nxt = rows[i + 1]
        if pos:
            sign = pos["sign"]
            held = int(np.busday_count(pos["et"].date(), et.date()))
            exit_px, why = None, None
            # stop checked against the bar's extreme
            if (r["l"] if sign > 0 else r["h"]) * sign <= pos["stop"] * sign:
                exit_px, why = pos["stop"], "chart stop"
            elif r["score"] * sign <= -rk["flip_exit_score"]:
                exit_px, why = r["c"], "flip"
            elif held >= rk["max_hold_days"]:
                exit_px, why = r["c"], "time"
            if exit_px is not None:
                ret = sign * (exit_px / pos["entry"] - 1)
                trades.append({**{k: pos[k] for k in ("side", "entry", "stop", "score")},
                               "opened": pos["et"].strftime("%Y-%m-%d %H:%M"), "exit": exit_px, "why": why,
                               "ret": ret, "r": ret / pos["risk_pct"],
                               "max_fav": pos["mfe"], "bars": i - pos["i"]})
                pos = None
                cooldown_until = et + timedelta(minutes=rk["cooldown_minutes"])
            else:
                fav = sign * ((r["h"] if sign > 0 else r["l"]) / pos["entry"] - 1)
                pos["mfe"] = max(pos["mfe"], fav)
        if not pos and in_window(et, cfg) and (cooldown_until is None or et >= cooldown_until) \
                and nxt["session"] == r["session"]:
            nw = {"score": r["news"], "count": r["n_news"]} if r["n_news"] else None
            side, _ = entry_direction(r["score"], r["chart"], nw, r["relvol"], cfg, hist[-3:])
            if side:
                sign = 1 if side == "call" else -1
                entry = nxt["o"]
                d_atr = r.get("d_atr")
                dist = max(1.5 * r["atr"], 0.5 * d_atr if d_atr == d_atr and d_atr else 0)
                pos = {"side": side, "sign": sign, "entry": entry, "stop": entry - sign * dist,
                       "risk_pct": dist / entry, "score": round(r["score"], 2), "et": et, "i": i + 1, "mfe": 0.0}
        hist.append(r["score"])
    return trades


def summarize(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    rets = np.array([t["ret"] for t in trades])
    rs = np.array([t["r"] for t in trades])
    gains, losses = rets[rets > 0].sum(), -rets[rets <= 0].sum()
    return {"n": len(trades), "win": float((rets > 0).mean()), "avg_ret": float(rets.mean()),
            "avg_r": float(rs.mean()), "pf": float(gains / losses) if losses > 0 else float("inf"),
            "calls": sum(t["side"] == "call" for t in trades), "puts": sum(t["side"] == "put" for t in trades)}


def run_backtest(cfg: dict, secrets) -> int:
    from .alpaca import AlpacaClient
    days = int(os.environ.get("BACKTEST_DAYS", "60"))
    syms = [s.strip().upper() for s in os.environ.get("BACKTEST_SYMBOLS", "").split(",") if s.strip()] \
        or list(dict.fromkeys(cfg["universe"]["always_watch"] + cfg["universe"]["watchlist"] + DEFAULT_SYMBOLS))[:15]
    if "SPY" not in syms:
        syms.insert(0, "SPY")
    client = AlpacaClient(secrets.alpaca_key, secrets.alpaca_secret)
    end = datetime.now(timezone.utc) - timedelta(minutes=16)
    start = end - timedelta(days=days)
    print(f"Backtesting {len(syms)} symbols over {days} days…")
    bars5 = client.bars(syms, "5Min", start - timedelta(days=7), end, max_pages=200)
    daily = client.bars(syms, "1Day", start - timedelta(days=140), end, max_pages=20)
    try:
        news = client.news(syms, start - timedelta(days=1), end, max_pages=60)
    except Exception as e:
        print(f"[news] {e}"); news = []

    prepared = {s: prepare(bars5[s], daily.get(s)) for s in syms if s in bars5}
    market = None
    if "SPY" in prepared:
        spy = score_series(prepared["SPY"], [a for a in news if "SPY" in a.get("symbols", [])], "SPY", cfg)
        market = dict(zip(spy["t"], spy["chart"]))
    all_trades, lines = [], []
    for s, d in prepared.items():
        d = d[d["t"] >= pd.Timestamp(start)]
        sc = score_series(d, [a for a in news if s in a.get("symbols", [])], s, cfg, market)
        tr = simulate(sc, cfg)
        for t in tr:
            t["symbol"] = s
        all_trades += tr
        sm = summarize(tr)
        if sm["n"]:
            lines.append(f"| {s} | {sm['n']} | {sm['calls']}/{sm['puts']} | {sm['win'] * 100:.0f}% | "
                         f"{sm['avg_ret'] * 100:+.2f}% | {sm['avg_r']:+.2f} | {sm['pf']:.2f} |")
        else:
            lines.append(f"| {s} | 0 | – | – | – | – | – |")
    tot = summarize(all_trades)
    md = [f"## Backtest — last {days} days, {len(prepared)} symbols", "",
          "Stock-move results of every signal (options amplify these moves but also lose value to time decay).", "",
          "| Symbol | Signals | Calls/Puts | Win rate | Avg stock move | Avg R | Profit factor |",
          "|---|---:|---:|---:|---:|---:|---:|", *lines]
    if tot["n"]:
        md += ["", f"**All: {tot['n']} signals · win rate {tot['win'] * 100:.0f}% · avg move {tot['avg_ret'] * 100:+.2f}% · "
                   f"avg R {tot['avg_r']:+.2f} · profit factor {tot['pf']:.2f}**", "",
               "R = result divided by the distance to the stop. Profit factor > 1.3 with 30+ signals is a reasonable bar; "
               "below 1.0 means the rules lost money on the stock moves.",
               "", "### Last 15 signals", "| Symbol | Opened (ET) | Side | Score | Exit | Stock move |", "|---|---|---|---:|---|---:|"]
        for t in sorted(all_trades, key=lambda t: t["opened"])[-15:]:
            md.append(f"| {t['symbol']} | {t['opened']} | {t['side']} | {t['score']:+.2f} | {t['why']} | {t['ret'] * 100:+.2f}% |")
    text = "\n".join(md)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    msg = (f"{tot['n']} signals · win rate {tot['win'] * 100:.0f}% · avg stock move {tot['avg_ret'] * 100:+.2f}% · "
           f"profit factor {tot['pf']:.2f}\nFull table on the GitHub run page.") if tot["n"] else "No signals fired in this period — try lowering entry_threshold."
    push(secrets, f"🧪 Backtest done ({days} days)", msg, tags=["test_tube"])
    return 0
