# Options Signal Agent

A free, always-on agent for a small cloud server. It sends **BUY CALL / BUY PUT** and **SELL** alerts to your phone, usually within about a minute of a setup forming.

- **Streams live prices** (1-minute bars, IEX feed) for up to 30 stocks and **all breaking news** over Alpaca websockets.
- **Re-scores every minute**, and within seconds when a headline hits a stock it's watching.
- **Watches the whole market**: SPY/QQQ/IWM, your watchlist, and Alpaca's most-active stocks and top gainers/losers, refreshed every 5 minutes.
- **Scores the chart**: EMA 9/21 trend, VWAP, MACD, RSI, volume compared with normal, and the daily trend.
- **Scores the news**: recent headlines, with newer ones counting more.
- A signal fires when the combined score holds for 3 straight minutes and nothing disagrees. The alert names a liquid contract (3–30 days to expiry, delta ~0.5, tight spread) and gives the entry, stop, target and size.
- It then **tracks every open signal** and sends SELL alerts on the target (+60%), a stop (-35%), a breakeven stop, a chart stop, a signal flip, or a time limit.
- Sends a **9:20 AM "ready"** check and a **daily recap** with a running win rate.
- If a stream drops, it switches to polling automatically and reconnects.
- It pulls updates (code or `config.yaml` edits) from GitHub every 15 minutes.

> ⚠️ This is a signal tool, not financial advice, and nothing here guarantees profits. Options can lose 100% quickly.
> The free Alpaca option feed is *indicative*, meaning delayed and approximate. Always check the live quote in your
> broker app before placing an order. Paper-trade the signals for a few weeks first.

---

## Setup (iPhone only, no terminal)
Follow the setup guide Claude made for you. In short:

1. **Alpaca**: free paper account → API Key + Secret.
2. **ntfy** app: subscribe to your private topic name.
3. **Oracle Cloud Always Free**: create an *Ubuntu 24.04* instance, shape **VM.Standard.E2.1.Micro**. Under
   *Advanced options → Management → Cloud-init script*, paste the setup script (it has your keys and this repo's URL).
4. Wait about 5–10 minutes. You'll get **"✅ Signal agent is running"**.

The setup script just runs `deploy/install.sh`, which adds swap, installs Python, clones this repo,
stores your keys in `/etc/signal-agent.env` (root-only), and starts the `signal-agent` service.

---

## Using it

| You want to… | Do this |
|---|---|
| Change settings | Edit `config.yaml` on GitHub (pencil icon → *Commit changes*). The server picks it up within 15 minutes |
| Test the strategy on past data | Add `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `NTFY_TOPIC` as repo secrets (Settings → Secrets → Actions), then Actions → *Backtest & checks* → Run workflow |
| Pause alerts | Oracle console → Compute → Instances → your instance → **Stop**. **Start** turns it back on |

### Telegram alerts (recommended on iPhone)
ntfy's iPhone banners can be unreliable, so the agent can also send every alert to Telegram:
1. In Telegram, open **@BotFather** → `/newbot` → pick a name → copy the bot key it gives you.
2. Open your new bot and tap **Start**.
3. On **ntfy.sh/<your topic>** (browser), publish the message: `telegram <bot key>`
4. Within a minute Telegram shows **"✅ Telegram connected"**. No server rebuild needed.

Other commands you can publish to your topic: `test` (sends a test alert) and `telegram off`.

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
- **About a minute, not instant.** Alerts come about 1–4 minutes after a setup forms: the minute bar has to close, then the score has to hold for 3 minutes. Breaking news is re-scored within seconds. Set `live.confirm_cycles: 1` for faster but noisier signals.
- **Free data**: stock prices come from the IEX exchange only, at most 30 streamed symbols at a time. Option quotes are Alpaca's delayed *indicative* feed.
- **Backtest = direction, not option profits.** It shows what the stock did after each historical signal.
- **Oracle's idle rule**: Oracle says it may reclaim Always Free servers that stay very idle for 7 days. If yours is ever stopped, start it again from the console. If it's deleted, create a new one with the same setup script (about 10 minutes).

## Files
```
config.yaml                    ← your settings
signal_agent/live.py           ← always-on server mode: websockets, 1-minute scheduler, REST fallback
signal_agent/agent.py          ← scan cycle: universe → scores → exits → entries → recap
signal_agent/strategy.py       ← chart score, news/chart blend, entry rules
signal_agent/indicators.py     ← EMA, RSI, MACD, VWAP, ATR, relative volume, daily trend
signal_agent/sentiment.py      ← headline scoring (keyword model, optional Claude)
signal_agent/options.py        ← contract selection and sizing
signal_agent/backtest.py       ← historical test
signal_agent/alpaca.py         ← Alpaca REST client
signal_agent/notify.py         ← ntfy push
deploy/                        ← install.sh, systemd service, auto-update
.github/workflows/signals.yml  ← on-demand backtest + tests
tests/                         ← offline tests with simulated market data
```
