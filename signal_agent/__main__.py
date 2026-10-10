"""Entry point:  python -m signal_agent [scan|test-notification|status|backtest]"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone

from .agent import ET, scan, send_summary, write_step_summary, fmt_pct
from .alpaca import AlpacaClient, AlpacaError
from .config import Secrets, load_config
from .notify import push
from .state import load_state, save_state, track_record


def main(argv: list[str]) -> int:
    mode = (argv[1] if len(argv) > 1 else "scan").strip().lower()
    cfg, secrets = load_config(), Secrets()

    if mode == "test-notification":
        ok = push(secrets, "✅ Signal agent connected",
                  "Notifications are working. You'll get BUY and SELL alerts here during market hours "
                  "(Mon–Fri, 9:45 AM–3:30 PM ET for new trades) plus a daily recap.",
                  tags=["white_check_mark"], priority=4)
        try:
            AlpacaClient(secrets.alpaca_key, secrets.alpaca_secret).clock()
            push(secrets, "✅ Alpaca connected", "Market data keys work.", tags=["white_check_mark"])
        except Exception as e:
            push(secrets, "⚠️ Alpaca keys problem", f"{e}\nRe-check the two Alpaca secrets in GitHub.", tags=["warning"], priority=4)
            return 1
        return 0 if ok else 1

    if mode == "backtest":
        from .backtest import run_backtest
        return run_backtest(cfg, secrets)

    st = load_state()

    if mode == "status":
        lines = []
        for s in st["open"].values():
            c = s["contract"]
            last = s.get("last_mid")
            lines.append(f"{s['underlying']} {c['expiry']} ${c['strike']:g} {c['type']} · entry ${s['entry']:.2f}"
                         + (f" · last ${last:.2f} ({fmt_pct(last / s['entry'] - 1)})" if last else ""))
        tr = track_record(st)
        lines.append(f"Closed signals: {tr['trades']}" + (f" · win rate {tr['win_rate'] * 100:.0f}% · avg {fmt_pct(tr['avg'])}" if tr["trades"] else ""))
        push(secrets, "📋 Open signals" if st["open"] else "📋 No open signals", "\n".join(lines) or "Nothing open.", tags=["clipboard"])
        return 0

    # ── scan ──
    try:
        client = AlpacaClient(secrets.alpaca_key, secrets.alpaca_secret)
        clock = client.clock()
        now = datetime.now(timezone.utc)
        now_et = now.astimezone(ET)
        if not clock.get("is_open"):
            # just after the close: make sure the daily recap goes out even if the last in-hours run was skipped
            today = now_et.date()
            if cfg["notifications"]["daily_summary"] and 16 <= now_et.hour < 18 \
                    and st.get("summary_sent") != today.isoformat() and client.is_trading_day(today.isoformat()):
                send_summary(cfg, secrets, st, today)
                save_state(st)
            print(f"Market closed ({now_et:%a %H:%M ET}). Next open: {clock.get('next_open')}")
            return 0
        report = scan(client, cfg, secrets, st, now)
        save_state(st)
        write_step_summary(report)
        return 0
    except AlpacaError as e:
        print(f"Alpaca error: {e}")
        today = datetime.now(timezone.utc).astimezone(ET).date().isoformat()
        if "rejected the keys" in str(e) and st.get("key_alert") != today:   # at most once a day
            st["key_alert"] = today
            push(secrets, "⚠️ Signal agent: Alpaca keys rejected", str(e)[:300], tags=["warning"], priority=4)
        save_state(st)
        return 1
    except Exception:
        traceback.print_exc()
        save_state(st)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
