"""News sentiment.

Default: a free finance-tuned phrase model (no API costs).
Claude scores headlines when claude_news.enabled and an Anthropic key is connected (keyword model is the fallback).
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone

import time

import requests

_CLAUDE_PAUSED_UNTIL = 0.0

# (pattern, weight). Phrases are matched before single words; weights in [-1, 1].
PHRASES: list[tuple[str, float]] = [
    # earnings / guidance
    (r"\b(beats?|tops?|surpass(es)?|exceeds?)\b.{0,40}\b(estimates?|expectations|forecasts?|consensus|views?)", 0.8),
    (r"\b(miss(es|ed|ing)?|falls? short of|below)\b.{0,40}\b(estimates?|expectations|forecasts?|consensus|views?)", -0.8),
    (r"\b(raises?|lifts?|boosts?|hikes?|ups)\b.{0,25}\b(guidance|outlook|forecast|full[- ]year)", 0.9),
    (r"\b(cuts?|lowers?|slashes?|reduces?|withdraws?|suspends?)\b.{0,25}\b(guidance|outlook|forecast|full[- ]year)", -0.9),
    (r"\b(weak|soft|disappointing|downbeat)\b.{0,20}\b(guidance|outlook|forecast|results|quarter|demand)", -0.7),
    (r"\b(strong|robust|upbeat|solid|blowout)\b.{0,20}\b(guidance|outlook|forecast|results|quarter|demand)", 0.7),
    (r"\brecord (revenue|sales|profit|quarter|earnings)", 0.6),
    # analysts
    (r"\bupgrade[sd]?\b", 0.7),
    (r"\bdowngrade[sd]?\b", -0.7),
    (r"\b(price target|pt)\b.{0,20}\b(raised|increased|lifted|boosted|to \$?\d)", 0.4),
    (r"\b(raises?|lifts?|boosts?)\b.{0,15}\b(price target|pt)\b", 0.45),
    (r"\b(cuts?|lowers?|slashes?|reduces?)\b.{0,15}\b(price target|pt)\b", -0.45),
    (r"\b(initiated?|starts?)\b.{0,20}\b(buy|outperform|overweight)\b", 0.4),
    (r"\b(initiated?|starts?)\b.{0,20}\b(sell|underperform|underweight)\b", -0.4),
    # corporate events
    (r"\b(fda|regulatory)\b.{0,25}\b(approv(al|es|ed))", 0.8),
    (r"\b(fda)\b.{0,30}\b(reject|complete response letter|crl|declin)", -0.8),
    (r"\b(to be acquired|buyout|takeover offer|agrees to be bought|tender offer)\b", 0.8),
    (r"\b(buyback|repurchase)\b", 0.4),
    (r"\b(dividend)\b.{0,15}\b(raise|increase|hike|boost)", 0.4),
    (r"\b(wins?|awarded|secures?|lands?)\b.{0,25}\b(contract|deal|order)", 0.5),
    (r"\b(partnership|collaboration|strategic alliance)\b", 0.3),
    (r"\b(stock|share) (offering|sale)\b|\bpriced? .{0,15}offering\b|\bdilution\b", -0.6),
    (r"\b(bankruptcy|chapter 11|insolvency|going concern)\b", -1.0),
    (r"\b(delist(ing|ed)?)\b", -0.8),
    (r"\b(recall(s|ed)?)\b", -0.5),
    (r"\b(lawsuit|sued|class action|antitrust)\b", -0.4),
    (r"\b(probe|investigation|subpoena|sec charges|fraud|doj)\b", -0.6),
    (r"\b(short[- ]seller|short report)\b", -0.6),
    (r"\b(layoffs?|job cuts|restructuring)\b", -0.25),
    (r"\b(ceo|cfo)\b.{0,20}\b(resigns?|steps down|departs|ousted|fired)", -0.5),
    (r"\b(data breach|hack(ed)?|outage|cyberattack)\b", -0.5),
    (r"\b(halt(ed)?|suspend(ed)?) trading\b", -0.4),
    (r"\b(tariffs?|export (ban|curbs|controls|restrictions))\b", -0.35),
    # price action words
    (r"\b(soar(s|ing|ed)?|skyrocket(s|ing|ed)?|surg(e|es|ing|ed)|rall(y|ies|ying|ied)|jump(s|ing|ed)?|spik(e|es|ing|ed))\b", 0.5),
    (r"\b(climb(s|ing|ed)?|ris(e|es|ing)|rose|gain(s|ing|ed)?|advanc(e|es|ing|ed)|higher)\b", 0.25),
    (r"\b(plung(e|es|ing|ed)|plummet(s|ing|ed)?|crash(es|ing|ed)?|tank(s|ing|ed)?|tumbl(e|es|ing|ed)|sink(s|ing)?|sank|slump(s|ing|ed)?)\b", -0.5),
    (r"\b(fall(s|ing)?|fell|drop(s|ping|ped)?|declin(e|es|ing|ed)|slid(es|ing)?|lower|los(e|es|ing)|lost)\b", -0.25),
    (r"\b(all[- ]time high|record high|52[- ]week high|breakout)\b", 0.35),
    (r"\b(52[- ]week low|multi[- ]year low)\b", -0.35),
    (r"\b(bullish|optimis(m|tic)|outperform(s|ed)?)\b", 0.35),
    (r"\b(bearish|pessimis(m|tic)|underperform(s|ed)?|warns?|warning|concerns?|fears?)\b", -0.35),
]
_COMPILED = [(re.compile(p, re.I), w) for p, w in PHRASES]
_NEGATION = re.compile(r"\b(not|no|isn't|wasn't|won't|didn't|doesn't|fails? to)\b", re.I)


def score_text(text: str) -> float:
    """Score one headline/summary in [-1, 1]."""
    if not text:
        return 0.0
    total, hits = 0.0, 0
    consumed: list[tuple[int, int]] = []
    for rx, w in _COMPILED:
        for m in rx.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in consumed):
                continue  # this span was already scored by a stronger phrase
            consumed.append((m.start(), m.end()))
            window = text[max(0, m.start() - 25): m.start()]
            total += -0.6 * w if _NEGATION.search(window) else w
            hits += 1
    if hits == 0:
        return 0.0
    return max(-1.0, min(1.0, math.tanh(total)))


def _age_hours(created_at: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except Exception:
        return 24.0
    return max(0.0, (now - t).total_seconds() / 3600)


def article_score(article: dict, cache: dict | None = None) -> float:
    aid = str(article.get("id", ""))
    if cache is not None and aid in cache:
        return float(cache[aid])
    s = 0.7 * score_text(article.get("headline", "")) + 0.3 * score_text(article.get("summary", ""))
    return max(-1.0, min(1.0, s))


def symbol_news_scores(articles: list[dict], symbols: list[str], now: datetime | None = None,
                       half_life_h: float = 6.0, cache: dict | None = None) -> dict[str, dict]:
    """Aggregate per-symbol: {'score', 'count', 'top': article dict, 'top_score'}."""
    now = now or datetime.now(timezone.utc)
    want = set(symbols)
    acc: dict[str, dict] = {}
    for a in articles:
        syms = [s for s in a.get("symbols", []) if s in want]
        if not syms:
            continue
        s = article_score(a, cache)
        age = _age_hours(a.get("created_at", ""), now)
        recency = 0.5 ** (age / max(half_life_h, 0.1))
        breadth = 1.0 if len(a.get("symbols", [])) <= 3 else 0.4  # roundups mentioning many tickers count less
        w = recency * breadth
        for sym in syms:
            r = acc.setdefault(sym, {"num": 0.0, "den": 0.0, "count": 0, "top": None, "top_w": -1})
            r["num"] += w * s
            r["den"] += w
            r["count"] += 1
            impact = abs(s) * w
            if impact > r["top_w"]:
                r["top_w"], r["top"], r["top_score"] = impact, a, s
    out = {}
    for sym, r in acc.items():
        confidence = min(1.0, r["den"] / 1.5)       # ~1.5 fresh, focused articles → full confidence
        raw = r["num"] / r["den"] if r["den"] else 0.0
        out[sym] = {"score": max(-1.0, min(1.0, raw * (0.5 + 0.5 * confidence))),
                    "count": r["count"], "top": r["top"], "top_score": r.get("top_score", 0.0)}
    return out


def claude_score_articles(articles: list[dict], api_key: str, model: str, cache: dict) -> int:
    """Score uncached articles with Claude; writes into `cache` (id -> score). Returns #scored."""
    global _CLAUDE_PAUSED_UNTIL
    if time.time() < _CLAUDE_PAUSED_UNTIL:
        return 0
    todo = [a for a in articles if str(a.get("id")) not in cache][:60]
    if not todo or not api_key:
        return 0
    lines = [f'{a["id"]} | {",".join(a.get("symbols", [])[:5])} | {a.get("headline","")[:200]}' for a in todo]
    prompt = (
        "You rate financial news headlines for their likely SHORT-TERM (1-5 day) effect on the named stocks.\n"
        "For each line (id | symbols | headline) return a score from -1 (very bearish) to 1 (very bullish); "
        "0 for neutral, stale, or generic market commentary.\n"
        'Reply with ONLY a JSON object mapping id to score, e.g. {"123": 0.6, "124": -0.2}.\n\n' + "\n".join(lines)
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]},
            timeout=60,
        )
        r.raise_for_status()
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except Exception as e:  # fall back to the keyword model; pause so a persistent error doesn't retry every minute
        print(f"[news] Claude scoring failed, using keyword model for 10 min: {e}")
        _CLAUDE_PAUSED_UNTIL = time.time() + 600
        return 0
    n = 0
    for k, v in data.items():
        try:
            cache[str(k)] = max(-1.0, min(1.0, float(v)))
            n += 1
        except (TypeError, ValueError):
            pass
    return n
