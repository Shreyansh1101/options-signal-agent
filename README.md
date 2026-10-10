# Options Signal Agent

A free agent that runs on GitHub every 5 minutes during market hours. Each run it:

1. **Scans the market**: SPY/QQQ/IWM, your watchlist, plus Alpaca's live *most-active* and *top gainers/losers* lists.
2. **Reads the chart** on 5-minute bars: EMA 9/21 trend, VWAP, MACD, RSI, volume compared with normal for that time of day, and the daily trend.
3. **Reads the news** from the last 24 hours (Alpaca/Benzinga headlines). Newer headlines count more.
4. **Scores each stock from -1 to +1.** A strong score on two scans in a row, with nothing disagreeing, gives a **BUY CALL** (bullish) or **BUY PUT** (bearish).
5. **Picks a contract**: 3–30 days to expiry (target ~10), delta 0.35–0.65, tight bid/ask spread. The alert includes the entry, stop, target and position size.
6. **Watches every open signal** and sends a **SELL** alert when it hits the target (+60%), the stop (-35%), a breakeven stop, a chart stop, a signal flip, or a time limit.
7. Sends a **daily recap** at the close with a running track record (win rate and average result).

Alerts arrive as push notifications on your iPhone through the free **ntfy** app.

> ⚠️ This is a signal tool, not financial advice, and nothing here guarantees profits. Options can lose 100% quickly.
> The free Alpaca option feed is *indicative*, meaning delayed and approximate. Always check the live quote in your
> broker app before placing an order. Paper-trade the signals for a few weeks first.

---

## Setup from an iPhone (about 15 minutes, everything free)

### 1. Alpaca (market data)
1. In Safari go to **alpaca.markets** → *Sign up* (email + password; no money needed).
2. Open the **Paper Trading** dashboard → right side **API Keys** → *Generate New Keys*.
3. Copy the **Key** and the **Secret** into Notes. The Secret is shown only once.

### 2. ntfy (push notifications)
1. App Store → install **ntfy** (by Philipp Heckel).
2. Tap **+** → enter your private topic name (Claude gave you one, e.g. `signals-xxxxxxxxxxxx`) → *Subscribe*.
3. Allow notifications when asked.

Keep the topic name secret. Anyone who knows it can read your alerts.

### 3. GitHub secrets
In your repo on github.com (Safari, *Desktop site* helps): **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Name | Value |
|---|---|
| `APCA_API_KEY_ID` | Alpaca Key |
| `APCA_API_SECRET_KEY` | Alpaca Secret |
| `NTFY_TOPIC` | your ntfy topic name |

### 4. Test it
**Actions** tab → *Options signals* → **Run workflow** → mode `test-notification` → Run.
Within a minute you should get two notifications: "Signal agent connected" and "Alpaca connected".

After that it runs by itself every weekday. There's nothing else to do.

---

## Using it

| You want to… | Do this |
|---|---|
| See what's open + track record | Actions → Run workflow → `status` |
| Test the strategy on past data | Actions → Run workflow → `backtest` (pick days/symbols). Results go to your phone, and the full table is on the run page |
| See every score from a scan | Actions → click any run → the summary table |
| Change settings | Open `config.yaml` → pencil icon → edit → *Commit changes*. Applies from the next run |
| Pause it | Actions → *Options signals* → ••• → *Disable workflow* |

### Reading an alert
```
BUY CALL · NVDA  (score +0.71)
NVDA 2026-10-23 $190 Call  (14d, Δ 0.52)
Buy: limit ≤ $4.20  (bid 4.10 / ask 4.30)
Size: 1 contract ≈ $420  (risk ≈ $147)
Stop: option $2.73 or stock < $183.10
Target: option $6.72 (+60%)
Stock $187.42 · score +0.71 (chart +0.78, news +0.55)
Why: EMA9 > EMA21, above VWAP, MACD rising, RSI 63, daily uptrend, volume 2.1x normal
News: “Nvidia rises after analyst upgrade…” (benzinga)
```
Tap the notification to open the chart. The buttons open the chart, the news story, or Alpaca.

### Settings worth knowing (`config.yaml`)
- `watchlist`: add tickers you always want checked.
- `entry_threshold`: raise it (e.g. 0.65) for fewer, stronger signals; lower it for more.
- `risk_per_trade_usd`: how much you're willing to lose per trade; sets the suggested contract count.
- `min_days_to_expiry`: set to 0 to allow same-day (0DTE) options. Not recommended.
- `claude_news.enabled`: lets Claude read the headlines instead of the built-in keyword model. Needs an `ANTHROPIC_API_KEY` secret and costs a few cents a day, so it's off to keep things free.

---

## Honest limits
- **Every 5 minutes, not tick-by-tick.** GitHub's scheduler sometimes starts runs 5–15 minutes late at busy times. It's built for trades held minutes to days, not scalping.
- **Free data**: stock prices come from the IEX exchange only, and option quotes are Alpaca's delayed *indicative* feed.
- **Backtest = direction, not option profits.** It shows what the stock did after each historical signal. Option results depend on time decay and implied volatility, which free data can't replay.
- **Cost:** $0 as long as this repository stays **public**, because GitHub Actions is free for public repos. Your keys and topic are stored as encrypted secrets and are never visible. If you make the repo private, change the schedule to `*/15` to stay inside the free 2,000 minutes a month.

## Files
```
config.yaml                    ← your settings
signal_agent/agent.py          ← scan cycle: universe → scores → exits → entries → recap
signal_agent/strategy.py       ← chart score, news/chart blend, entry rules
signal_agent/indicators.py     ← EMA, RSI, MACD, VWAP, ATR, relative volume, daily trend
signal_agent/sentiment.py      ← headline scoring (keyword model, optional Claude)
signal_agent/options.py        ← contract selection and sizing
signal_agent/backtest.py       ← historical test
signal_agent/alpaca.py         ← Alpaca REST client
signal_agent/notify.py         ← ntfy push
.github/workflows/signals.yml  ← schedule
tests/                         ← offline tests with simulated market data
```
