"""Scoring and entry/exit rules. Shared by the live scanner and the backtest."""
from __future__ import annotations

import math


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 0.0
    return max(lo, min(hi, x))


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def _rsi_component(r: float) -> float:
    """Momentum is good, but chasing extremes is not."""
    if not _ok(r):
        return 0.0
    if r >= 80:
        return -0.3
    if r >= 72:
        return 1 - (r - 72) / 8          # 1 → 0
    if r >= 65:
        return 1.0
    if r >= 50:
        return (r - 50) / 15             # 0 → 1
    if r > 35:
        return -(50 - r) / 15            # 0 → -1
    if r > 28:
        return -1.0
    if r > 20:
        return -1 + (28 - r) / 8         # -1 → 0
    return 0.3


def chart_score(row: dict, prev_hist: float | None = None, price: float | None = None) -> tuple[float, list[str]]:
    """Technical score in [-1, 1] from one row of indicators, plus human-readable reasons."""
    p = float(price if price else row["c"])
    atr = row.get("atr") or 0
    atr = atr if _ok(atr) and atr > 0 else max(p * 0.002, 0.01)
    e9, e21 = row.get("ema9"), row.get("ema21")
    reasons: list[str] = []

    # 1) Intraday trend
    if p > e9 > e21:
        trend = 1.0; reasons.append("EMA9 > EMA21, price above both")
    elif p < e9 < e21:
        trend = -1.0; reasons.append("EMA9 < EMA21, price below both")
    else:
        trend = 0.3 if e9 > e21 else -0.3

    # 2) VWAP
    vw = row.get("vwap")
    vwap = _clip((p - vw) / atr) if _ok(vw) else 0.0
    if vwap > 0.3:
        reasons.append("above VWAP")
    elif vwap < -0.3:
        reasons.append("below VWAP")

    # 3) MACD histogram (+ is it strengthening?)
    h = row.get("macd_hist", 0.0)
    macd = _clip(h / (0.15 * atr)) if _ok(h) else 0.0
    if _ok(prev_hist) and _ok(h):
        rising = h > prev_hist
        if (macd > 0 and rising) or (macd < 0 and not rising):
            reasons.append("MACD " + ("rising" if rising else "falling"))
        else:
            macd *= 0.5

    # 4) RSI
    r = row.get("rsi")
    rsi_c = _rsi_component(r)
    if _ok(r):
        reasons.append(f"RSI {r:.0f}")

    # 5) Daily trend (yesterday's EMAs vs. price now)
    d20, d50 = row.get("d_ema20"), row.get("d_ema50")
    daily = 0.0
    if _ok(d20) and _ok(d50):
        daily = 0.5 * (1 if p > d20 else -1) + 0.5 * (1 if d20 > d50 else -1)
        if daily >= 1:
            reasons.append("daily uptrend")
        elif daily <= -1:
            reasons.append("daily downtrend")

    raw = 0.25 * trend + 0.20 * vwap + 0.20 * macd + 0.10 * rsi_c + 0.25 * daily

    # Volume conviction
    rv = row.get("relvol", 1.0)
    rv = rv if _ok(rv) else 1.0
    mult = max(0.6, min(1.15, 0.6 + 0.25 * rv))
    if rv >= 1.5:
        reasons.append(f"volume {rv:.1f}x normal")
    return _clip(raw * mult), reasons


def combine(chart: float, news: dict | None, cfg: dict, market: float | None = None) -> float:
    sc = cfg["scoring"]
    if news and news.get("count", 0) > 0:
        s = sc["chart_weight"] * chart + sc["news_weight"] * news["score"]
    else:
        s = chart * sc["no_news_factor"]
    if sc.get("market_filter") and market is not None and abs(market) > 0.5 and s * market < 0:
        s -= 0.15 * math.copysign(1, s)  # fighting a strong market trend
    return _clip(s)


def entry_direction(score: float, chart: float, news: dict | None, relvol: float, cfg: dict,
                    history: list[float] | None = None) -> tuple[str | None, str]:
    """Return ('call'|'put'|None, reason). `history` = previous scores for this symbol (oldest→newest)."""
    sc = cfg["scoring"]
    th = sc["entry_threshold"]
    if abs(score) < th:
        return None, "score below threshold"
    side = "call" if score > 0 else "put"
    sign = 1 if score > 0 else -1
    if chart * sign <= 0:
        return None, "chart disagrees"
    if news and news.get("count", 0) > 0 and news["score"] * sign < -sc["news_veto"]:
        return None, "news disagrees"
    if (relvol if relvol is not None and not math.isnan(relvol) else 1.0) < sc["min_relative_volume"]:
        return None, "volume too light"
    need = int(sc.get("confirm_cycles", 1)) - 1
    if need > 0:
        prev = (history or [])[-need:]
        if len(prev) < need or any(p * sign < th * 0.8 for p in prev):
            return None, "waiting for confirmation"
    return side, "ok"
