"""One scan cycle: find candidates → score chart + news → manage open signals → send BUY/SELL alerts."""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import indicators as ind
from .notify import chart_actions, push
from .options import chain_window, contracts_for_risk, limit_price, pick_contract, quote_of
from .sentiment import claude_score_articles, symbol_news_scores
from .state import track_record
from .strategy import chart_score, combine, entry_direction

ET = ZoneInfo("America/New_York")
TICKER = re.compile(r"^[A-Z]{1,5}$")
BAR = timedelta(minutes=5)


def _hm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def in_window(now_et: datetime, cfg: dict) -> bool:
    (sh, sm), (eh, em) = _hm(cfg["trading_window"]["start"]), _hm(cfg["trading_window"]["end"])
    t = (now_et.hour, now_et.minute)
    return (sh, sm) <= t < (eh, em)


def fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:+.0f}%"


# ── universe ─────────────────────────────────────────────────
def build_universe(client, cfg: dict, st: dict) -> tuple[list[str], set[str]]:
    u = cfg["universe"]
    must = [s["underlying"] for s in st["open"].values()]
    core = list(dict.fromkeys(["SPY"] + u["always_watch"] + u["watchlist"]))
    scanned: list[str] = []
    try:
        mv = client.movers(u["scanner_movers"])
        movers = [m for m in mv["gainers"] + mv["losers"]
                  if u["min_price"] <= float(m.get("price") or 0) <= u["max_price"]]
        movers.sort(key=lambda m: -abs(float(m.get("percent_change") or 0)))
        scanned += [m["symbol"] for m in movers]
    except Exception as e:
        print(f"[universe] movers failed: {e}")
    try:
        scanned += [a["symbol"] for a in client.most_actives(u["scanner_most_active"])]
    except Exception as e:
        print(f"[universe] most-actives failed: {e}")
    ok = lambda s: TICKER.match(s or "") and s not in u["exclude"]
    protected = {s for s in must + core if ok(s)}
    out = list(dict.fromkeys([s for s in must + core if ok(s)]))
    for s in scanned:
        if len(out) >= max(u["max_symbols"], len(protected)):
            break
        if ok(s) and s not in out:
            out.append(s)
    return out, protected


# ── per-symbol analysis ──────────────────────────────────────
def analyse(bars5: pd.DataFrame | None, daily: pd.DataFrame | None, now: datetime, price: float | None):
    if bars5 is None or bars5.empty:
        return None
    d = ind.regular_session(bars5)
    d = d[d["t"] + BAR <= now]                      # only completed bars
    if len(d) < 30:
        return None
    d = ind.add_intraday(d)
    d = ind.attach_daily(d, ind.daily_levels(daily) if daily is not None else None)
    row = d.iloc[-1].to_dict()
    prev_hist = d.iloc[-2]["macd_hist"]
    px = price or row["c"]
    score, reasons = chart_score(row, prev_hist, px)
    return {"row": row, "price": float(px), "chart": score, "reasons": reasons}


def _history(st: dict, sym: str, now: datetime, max_gap_min: int = 25) -> list[float]:
    """Recent consecutive scores for confirmation (drops anything older than the gap)."""
    out, last_t = [], now
    for ts, sc in reversed(st["scores"].get(sym, [])):
        t = datetime.fromisoformat(ts)
        if (last_t - t) > timedelta(minutes=max_gap_min):
            break
        out.append(sc)
        last_t = t
    return list(reversed(out))


def _record_score(st: dict, sym: str, now: datetime, score: float) -> None:
    h = st["scores"].setdefault(sym, [])
    h.append([now.isoformat(), round(score, 3)])
    del h[:-6]


# ── exits ────────────────────────────────────────────────────
def manage_open(client, cfg, secrets, st, analyses, scores, now, today) -> list[dict]:
    rk = cfg["risk"]
    closed = []
    if not st["open"]:
        return closed
    try:
        snaps = client.option_snapshots([s["contract"]["symbol"] for s in st["open"].values()])
    except Exception as e:
        print(f"[exits] option snapshots failed: {e}")
        snaps = {}
    for sid, sig in list(st["open"].items()):
        c = sig["contract"]
        sign = 1 if c["type"] == "call" else -1
        a = analyses.get(sig["underlying"])
        price = a["price"] if a else None
        bid, ask, mid = quote_of(snaps.get(c["symbol"], {})) if c["symbol"] in snaps else (0, 0, 0)
        pnl = (mid / sig["entry"] - 1) if mid > 0 else None
        if pnl is not None:
            sig["peak_pnl"] = max(sig.get("peak_pnl", 0.0), pnl)
            sig["last_mid"] = mid
        be_armed = sig.get("peak_pnl", 0.0) >= rk["breakeven_after_pct"]
        held = int(np.busday_count(datetime.fromisoformat(sig["opened"]).astimezone(ET).date(), today))
        dte = (datetime.fromisoformat(c["expiry"]).date() - today).days
        score = scores.get(sig["underlying"])

        why = None
        if pnl is not None and pnl >= rk["take_profit_pct"]:
            why = ("🎯 Target hit", "take profit")
        elif pnl is not None and pnl <= -rk["stop_loss_pct"]:
            why = ("🛑 Stop loss", "option stop")
        elif be_armed and pnl is not None and pnl <= 0.02:
            why = ("🛡️ Breakeven stop", "gave back gains")
        elif price is not None and (price - sig["u_stop"]) * sign <= 0:
            why = ("🛑 Chart stop", f"stock crossed ${sig['u_stop']:.2f}")
        elif score is not None and score * sign <= -rk["flip_exit_score"]:
            why = ("🔄 Signal flipped", f"score now {score:+.2f}")
        elif held >= rk["max_hold_days"]:
            why = ("⏰ Time stop", f"held {held} trading days")
        elif dte <= 1:
            why = ("⏰ Expiry close", f"{dte} day(s) to expiry")
        if not why:
            continue

        title_tag, reason = why
        val = f"${mid:.2f} (bid {bid:.2f} / ask {ask:.2f})" if mid > 0 else "no live quote — check your app"
        msg = (f"{c['root']} {c['expiry']} ${c['strike']:g} {c['type'].title()}\n"
               f"Exit: {reason}\n"
               f"Now: {val}  ·  entry ${sig['entry']:.2f}  ·  P&L {fmt_pct(pnl)}\n"
               + (f"Stock ${price:.2f}" if price else "")
               + f"\nOpened {datetime.fromisoformat(sig['opened']).astimezone(ET):%a %b %d %I:%M %p} ET")
        push(secrets, f"{title_tag} SELL {c['type'].upper()} · {sig['underlying']}", msg,
             tags=["red_circle" if (pnl or 0) < 0 else "large_green_circle"], priority=5,
             click=f"https://www.tradingview.com/chart/?symbol={sig['underlying']}",
             actions=chart_actions(sig["underlying"]))
        sig.update({"closed": now.isoformat(), "exit": mid if mid > 0 else None, "pnl_pct": pnl, "exit_reason": reason})
        st["closed"].append(sig)
        del st["open"][sid]
        closed.append(sig)
    return closed


# ── entries ──────────────────────────────────────────────────
def build_signal(sym, side, a, news, score, contract, cfg, now) -> dict:
    rk = cfg["risk"]
    row = a["row"]
    atr5 = row.get("atr") or a["price"] * 0.003
    d_atr = row.get("d_atr")
    dist = max(1.5 * atr5, 0.5 * d_atr if d_atr and d_atr == d_atr else 0)
    sign = 1 if side == "call" else -1
    entry = limit_price(contract["mid"])
    qty = contracts_for_risk(entry, rk["stop_loss_pct"], rk["risk_per_trade_usd"])
    return {
        "id": f"{sym}-{now:%Y%m%d%H%M}",
        "underlying": sym, "side": side, "opened": now.isoformat(),
        "contract": contract, "entry": entry, "qty": qty,
        "stock_entry": round(a["price"], 2),
        "u_stop": round(a["price"] - sign * dist, 2),
        "opt_stop": round(entry * (1 - rk["stop_loss_pct"]), 2),
        "opt_target": round(entry * (1 + rk["take_profit_pct"]), 2),
        "score": round(score, 2), "chart": round(a["chart"], 2),
        "news": round(news["score"], 2) if news else None,
        "reasons": a["reasons"],
        "headline": (news or {}).get("top", {}).get("headline") if news else None,
    }


def buy_message(sig: dict, news: dict | None, cfg: dict) -> str:
    c = sig["contract"]
    risk_usd = sig["entry"] * 100 * cfg["risk"]["stop_loss_pct"] * sig["qty"]
    lines = [
        f"{c['root']} {c['expiry']} ${c['strike']:g} {c['type'].title()}  ({c['dte']}d, Δ {c['delta'] if c['delta'] is not None else 'n/a'})",
        f"Buy: limit ≤ ${sig['entry']:.2f}  (bid {c['bid']:.2f} / ask {c['ask']:.2f})",
        f"Size: {sig['qty']} contract{'s' if sig['qty'] > 1 else ''} ≈ ${sig['entry'] * 100 * sig['qty']:,.0f}  (risk ≈ ${risk_usd:,.0f})",
        f"Stop: option ${sig['opt_stop']:.2f} or stock {'<' if c['type'] == 'call' else '>'} ${sig['u_stop']:.2f}",
        f"Target: option ${sig['opt_target']:.2f} (+{cfg['risk']['take_profit_pct'] * 100:.0f}%)",
        f"Stock ${sig['stock_entry']:.2f} · score {sig['score']:+.2f} (chart {sig['chart']:+.2f}, news "
        + (f"{sig['news']:+.2f}" if sig["news"] is not None else "none") + ")",
        "Why: " + ", ".join(sig["reasons"][:5]),
    ]
    if news and news.get("top"):
        t = news["top"]
        lines.append(f"News: “{t.get('headline', '')[:140]}” ({t.get('source', '')})")
    lines.append("Option price is Alpaca's free delayed feed — check the live quote before you buy.")
    return "\n".join(lines)


def scan(client, cfg: dict, secrets, st: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    now_et = now.astimezone(ET)
    today = now_et.date()
    report = {"time": now_et.strftime("%Y-%m-%d %H:%M ET"), "rows": [], "buys": [], "sells": [], "skips": []}

    symbols, _ = build_universe(client, cfg, st)
    bars5 = client.bars(symbols, "5Min", now - timedelta(days=8), now)
    daily = client.bars(symbols, "1Day", now - timedelta(days=130), now)
    try:
        prices = client.latest_prices(symbols)
    except Exception as e:
        print(f"[prices] snapshot failed, using last bar: {e}")
        prices = {}

    u = cfg["universe"]
    analyses: dict[str, dict] = {}
    for sym in symbols:
        a = analyse(bars5.get(sym), daily.get(sym), now, prices.get(sym))
        if not a:
            continue
        in_open = any(s["underlying"] == sym for s in st["open"].values())
        if not in_open and not (u["min_price"] <= a["price"] <= u["max_price"]):
            continue
        analyses[sym] = a

    # News
    sc = cfg["scoring"]
    articles = []
    if analyses:
        try:
            articles = client.news(list(analyses), now - timedelta(hours=sc["news_lookback_hours"]), now)
        except Exception as e:
            print(f"[news] failed: {e}")
    if cfg["claude_news"]["enabled"] and secrets.anthropic_key and articles:
        n = claude_score_articles(articles, secrets.anthropic_key, cfg["claude_news"]["model"], st["news_cache"])
        print(f"[news] Claude scored {n} new headlines")
    news = symbol_news_scores(articles, list(analyses), now, sc["news_half_life_hours"],
                              st["news_cache"] if cfg["claude_news"]["enabled"] else None)

    market = analyses.get("SPY", {}).get("chart")
    scores = {}
    for sym, a in analyses.items():
        s = combine(a["chart"], news.get(sym), cfg, None if sym == "SPY" else market)
        scores[sym] = s
        report["rows"].append({"symbol": sym, "price": a["price"], "chart": a["chart"],
                               "news": news.get(sym, {}).get("score"), "n_news": news.get(sym, {}).get("count", 0),
                               "score": s, "relvol": a["row"].get("relvol")})

    # 1) exits first
    report["sells"] = manage_open(client, cfg, secrets, st, analyses, scores, now, today)

    # 2) entries
    rk = cfg["risk"]
    if in_window(now_et, cfg):
        open_syms = {s["underlying"] for s in st["open"].values()}
        for sym in sorted(scores, key=lambda k: -abs(scores[k])):
            if len(st["open"]) >= rk["max_open_signals"]:
                break
            if sym in open_syms:
                continue
            cd = st["cooldown"].get(sym)
            if cd and now < datetime.fromisoformat(cd):
                continue
            a, nw = analyses[sym], news.get(sym)
            side, why = entry_direction(scores[sym], a["chart"], nw, a["row"].get("relvol", 1.0), cfg,
                                        _history(st, sym, now))
            if not side:
                continue
            win = chain_window(a["price"], today, cfg)
            try:
                chain = client.option_chain(sym, side, win["exp_gte"], win["exp_lte"], win["strike_gte"], win["strike_lte"])
            except Exception as e:
                chain = {}
                print(f"[options] {sym} chain failed: {e}")
            contract, why = pick_contract(chain, side, a["price"], today, cfg)
            if not contract:
                report["skips"].append({"symbol": sym, "side": side, "score": scores[sym], "why": why})
                st["cooldown"][sym] = (now + timedelta(minutes=30)).isoformat()
                if not cfg["notifications"]["quiet_skips"]:
                    push(secrets, f"Skipped {side.upper()} · {sym}", f"Score {scores[sym]:+.2f} but {why}", tags=["grey_question"], priority=2)
                continue
            sig = build_signal(sym, side, a, nw, scores[sym], contract, cfg, now)
            st["open"][sig["id"]] = sig
            st["cooldown"][sym] = (now + timedelta(minutes=rk["cooldown_minutes"])).isoformat()
            report["buys"].append(sig)
            top_url = ((nw or {}).get("top") or {}).get("url")
            push(secrets, f"BUY {side.upper()} · {sym}  (score {scores[sym]:+.2f})", buy_message(sig, nw, cfg),
                 tags=["chart_with_upwards_trend" if side == "call" else "chart_with_downwards_trend"],
                 priority=5, click=f"https://www.tradingview.com/chart/?symbol={sym}",
                 actions=chart_actions(sym, top_url))

    for sym, s in scores.items():
        _record_score(st, sym, now, s)
    st["last_scan"] = now.isoformat()

    # 3) end-of-day recap
    if cfg["notifications"]["daily_summary"] and (now_et.hour, now_et.minute) >= (15, 50) \
            and st.get("summary_sent") != today.isoformat():
        send_summary(cfg, secrets, st, today)
    return report


def send_summary(cfg, secrets, st, today) -> None:
    day = today.isoformat()
    opened = [s for s in list(st["open"].values()) + st["closed"]
              if datetime.fromisoformat(s["opened"]).astimezone(ET).date() == today]
    closed_today = [s for s in st["closed"] if s.get("closed") and datetime.fromisoformat(s["closed"]).astimezone(ET).date() == today]
    lines = [f"New signals today: {len(opened)}  ·  closed: {len(closed_today)}"]
    for s in closed_today:
        lines.append(f"  {s['underlying']} {s['side']}: {fmt_pct(s.get('pnl_pct'))} ({s.get('exit_reason')})")
    if st["open"]:
        lines.append("Still open:")
        for s in st["open"].values():
            last = s.get("last_mid")
            lines.append(f"  {s['underlying']} {s['contract']['expiry']} ${s['contract']['strike']:g}{s['side'][0].upper()}"
                         f" entry ${s['entry']:.2f}" + (f" → ${last:.2f} ({fmt_pct(last / s['entry'] - 1)})" if last else ""))
    tr = track_record(st)
    if tr["trades"]:
        lines.append(f"Track record: {tr['trades']} trades · win rate {tr['win_rate'] * 100:.0f}% · "
                     f"avg {fmt_pct(tr['avg'])} (wins {fmt_pct(tr['avg_win'])}, losses {fmt_pct(tr['avg_loss'])})")
    else:
        lines.append("Track record: no closed signals yet.")
    push(secrets, f"Daily recap · {today:%a %b %d}", "\n".join(lines), tags=["bar_chart"], priority=3)
    st["summary_sent"] = day


def write_step_summary(report: dict) -> None:
    """Readable table on the GitHub Actions run page (handy from the phone)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    rows = sorted(report.get("rows", []), key=lambda r: -abs(r["score"]))
    md = [f"### Scan {report.get('time', '')}", "",
          f"BUY alerts: {len(report.get('buys', []))} · SELL alerts: {len(report.get('sells', []))}", "",
          "| Symbol | Price | Chart | News | Score | RelVol |", "|---|---:|---:|---:|---:|---:|"]
    for r in rows[:30]:
        nw = f"{r['news']:+.2f} ({r['n_news']})" if r["news"] is not None else "–"
        md.append(f"| {r['symbol']} | {r['price']:.2f} | {r['chart']:+.2f} | {nw} | **{r['score']:+.2f}** | {r['relvol'] or 0:.1f}x |")
    if report.get("skips"):
        md += ["", "Skipped (no good contract): " + ", ".join(f"{s['symbol']} ({s['why']})" for s in report["skips"])]
    text = "\n".join(md)
    print(text)
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")
