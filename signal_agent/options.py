"""Choose a liquid, sensibly priced option contract for a signal."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta

OCC = re.compile(r"^([A-Z.]+?)(\d{6})([CP])(\d{8})$")


def parse_occ(sym: str) -> dict | None:
    m = OCC.match(sym)
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    return {
        "root": root,
        "expiry": datetime.strptime(ymd, "%y%m%d").date(),
        "type": "call" if cp == "C" else "put",
        "strike": int(strike) / 1000,
    }


def quote_of(snap: dict) -> tuple[float, float, float]:
    q = snap.get("latestQuote") or {}
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else float((snap.get("latestTrade") or {}).get("p") or 0)
    return bid, ask, mid


def chain_window(price: float, today: date, cfg: dict) -> dict:
    c = cfg["contracts"]
    return {
        "exp_gte": (today + timedelta(days=c["min_days_to_expiry"])).isoformat(),
        "exp_lte": (today + timedelta(days=c["max_days_to_expiry"])).isoformat(),
        "strike_gte": price * 0.85,
        "strike_lte": price * 1.15,
    }


def pick_contract(snaps: dict[str, dict], side: str, price: float, today: date, cfg: dict) -> tuple[dict | None, str]:
    c = cfg["contracts"]
    best, best_cost, rejects = None, math.inf, {}

    def reject(why):
        rejects[why] = rejects.get(why, 0) + 1

    for sym, snap in snaps.items():
        info = parse_occ(sym)
        if not info or info["type"] != side:
            reject("wrong type"); continue
        dte = (info["expiry"] - today).days
        if dte < c["min_days_to_expiry"] or dte > c["max_days_to_expiry"]:
            reject("expiry out of range"); continue
        bid, ask, mid = quote_of(snap)
        if bid <= 0 or ask <= 0 or mid <= 0:
            reject("no quote"); continue
        spread = (ask - bid) / mid
        if spread > c["max_spread_pct"]:
            reject("spread too wide"); continue
        if mid < c["min_premium"] or mid > c["max_premium"]:
            reject("premium out of range"); continue
        delta = (snap.get("greeks") or {}).get("delta")
        if delta is not None:
            ad = abs(float(delta))
            if ad < c["min_delta"] or ad > c["max_delta"]:
                reject("delta out of range"); continue
            delta_gap = abs(ad - 0.5)
        else:
            moneyness = abs(info["strike"] - price) / price
            if moneyness > 0.03:
                reject("too far from the money"); continue
            delta_gap = moneyness * 5
        dte_gap = abs(dte - c["target_days_to_expiry"]) / max(c["target_days_to_expiry"], 1)
        cost = 1.0 * dte_gap + 1.5 * delta_gap + 2.0 * spread
        if cost < best_cost:
            best_cost = cost
            best = {
                "symbol": sym, **info, "expiry": info["expiry"].isoformat(), "dte": dte,
                "bid": bid, "ask": ask, "mid": round(mid, 2), "spread_pct": round(spread, 3),
                "delta": None if delta is None else round(float(delta), 2),
                "iv": snap.get("impliedVolatility"),
            }
    if best:
        return best, "ok"
    if not snaps:
        return None, "no option chain (stock may not have listed options)"
    top = sorted(rejects.items(), key=lambda kv: -kv[1])[:2]
    return None, "no suitable contract (" + ", ".join(f"{k}: {v}" for k, v in top) + ")"


def limit_price(mid: float) -> float:
    tick = 0.05 if mid >= 3 else 0.01
    return round(round(mid / tick) * tick, 2)


def contracts_for_risk(mid: float, stop_pct: float, risk_usd: float) -> int:
    per_contract_risk = mid * 100 * stop_pct
    if per_contract_risk <= 0:
        return 1
    return max(1, int(risk_usd // per_contract_risk))
