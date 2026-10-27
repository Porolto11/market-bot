"""Pure price-history indicators and a rule-based trend rating (no network, no LLM)."""
import numpy as np
import pandas as pd

CHIPS = {"bullish": "Bullish", "neutral": "Neutral", "bearish": "Bearish", "n/a": "n/a"}
_KEYS = ("sma50", "sma200", "rsi14", "mom_3m", "mom_6m", "mom_12m", "vol_20d", "dd_52w", "last")


def _rsi14(close: pd.Series):
    """Wilder RSI: seed with the simple mean of the first 14 changes, then smooth."""
    if len(close) < 15:
        return None
    delta = close.diff().dropna().to_numpy()
    gain, loss = np.maximum(delta, 0.0), np.maximum(-delta, 0.0)
    ag, al = gain[:14].mean(), loss[:14].mean()
    for g, l in zip(gain[14:], loss[14:]):
        ag = (ag * 13 + g) / 14
        al = (al * 13 + l) / 14
    if al == 0:
        return 100.0 if ag > 0 else 50.0
    return float(100 - 100 / (1 + ag / al))


def indicators(hist: pd.DataFrame) -> dict:
    out = dict.fromkeys(_KEYS)
    if hist is None or "Close" not in hist:
        return out
    c = hist["Close"].dropna().astype(float).reset_index(drop=True)
    n = len(c)
    if n == 0:
        return out
    last = float(c.iloc[-1])
    out["last"] = last
    if n >= 50:
        out["sma50"] = float(c.iloc[-50:].mean())
    if n >= 200:
        out["sma200"] = float(c.iloc[-200:].mean())
    out["rsi14"] = _rsi14(c)
    for key, lag in (("mom_3m", 63), ("mom_6m", 126), ("mom_12m", 252)):
        if n > lag:
            out[key] = last / float(c.iloc[-1 - lag]) - 1
    if n >= 21:
        out["vol_20d"] = float(np.log(c).diff().iloc[-20:].std() * np.sqrt(252))
    if n >= 252:
        out["dd_52w"] = last / float(c.iloc[-252:].max()) - 1
    return out


def rate(ind: dict) -> dict:
    last, s50, s200 = ind.get("last"), ind.get("sma50"), ind.get("sma200")
    if s200 is None or last is None:
        return {"rating": "n/a", "score": 0, "reasons": ["not enough history"], "chip": "n/a"}
    score, reasons = 0, []
    pct = (last / s200 - 1) * 100
    if last > s200:
        score += 1
        reasons.append(f"price {pct:.1f}% above 200-day average")
    else:
        reasons.append(f"price {-pct:.1f}% below 200-day average")
    if s50 is not None:
        if s50 > s200:
            score += 1
            reasons.append("50-day average above 200-day average")
        else:
            reasons.append("50-day average below 200-day average")
    for key, label in (("mom_6m", "6-month"), ("mom_12m", "12-month")):
        m = ind.get(key)
        if m is None:
            continue
        if m > 0:
            score += 1
        reasons.append(f"{label} momentum {m * 100:+.1f}%")
    rsi = ind.get("rsi14")
    if rsi is not None:
        if rsi > 75:
            score -= 1
            reasons.append(f"RSI {rsi:.0f}, overheated")
        elif rsi < 30 and last > s200:
            score += 1
            reasons.append(f"RSI {rsi:.0f}, dip in an uptrend")
    rating = "bullish" if score >= 3 else "bearish" if score <= 1 else "neutral"
    return {"rating": rating, "score": score, "reasons": reasons, "chip": CHIPS[rating]}


def market_regime(histories: dict) -> dict:
    out = {}
    for name, ticker in (("US", "^GSPC"), ("EU", "^STOXX50E")):
        out[name] = rate(indicators(histories.get(ticker)))
    return out
