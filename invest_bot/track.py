"""Call tracking: log every sent BUY / SELL / WAIT call and score it against CSPX.L after about 1, 3 and 6 months.

The state's `calls` list holds dict(d=scan date, sym, call, px=price at the call, spx=CSPX.L price at the call,
x={horizon: excess}) for the newest MAX_CALLS calls. excess = symbol return - CSPX.L return from the call to the
close HORIZON weekdays later, from the scan's own price histories. A BUY won when excess > 0; SELL and the WAIT
calls won when excess < 0 (avoiding or waiting helped). A SWITCH is logged as sym "X>Y" (sell X, buy Y) and scored
as Y against X instead of CSPX.L (px = Y's price, spx = X's): it won when Y beat X. edge() turns excess into
"positive = the call helped".
"""
from datetime import date

import numpy as np

BENCH = "CSPX.L"
TRACKED = ("BUY", "SELL", "WAIT_FOR_DIP", "WAIT", "SWITCH")
HORIZONS = {20: "1m", 60: "3m", 120: "6m"}  # weekdays after the call
MAX_CALLS = 500
MIN_N = 10


def _close(hist):
    try:
        return hist["Close"].dropna().astype(float)
    except Exception:
        return None


def last_price(hist):
    c = _close(hist)
    return float(c.iloc[-1]) if c is not None and len(c) else None


def _pair(sym):
    """(symbol, benchmark): a SWITCH "X>Y" is Y against X, anything else is against CSPX.L."""
    sell, _, buy = sym.partition(">")
    return (buy, sell) if buy else (sym, BENCH)


def log(st, items, today, histories):
    """Add the sent calls ([symbol, call] pairs) of `today` to st['calls']; one per (date, symbol, call)."""
    old = st.setdefault("calls", [])
    seen = {(c["d"], c["sym"], c["call"]) for c in old}
    for sym, call in items:
        s, b = _pair(sym)
        px, spx = last_price(histories.get(s)), last_price(histories.get(b))
        key = (today.isoformat(), sym, call)
        if call in TRACKED and px and spx and key not in seen:
            old.append(dict(d=key[0], sym=sym, call=call, px=px, spx=spx, x={}))
            seen.add(key)
    st["calls"] = old[-MAX_CALLS:]


def _at(close, day):
    """The last close on or before `day`, or None."""
    part = close[close.index.date <= day] if close is not None else None
    return float(part.iloc[-1]) if part is not None and len(part) else None


def score(st, today, histories):
    """Fill x[horizon] for each logged call that is at least `horizon` weekdays old and whose histories reach the
    horizon date. Calls whose symbol is missing from this scan stay unscored until it is back."""
    for c in st.get("calls") or []:
        d = date.fromisoformat(c["d"])
        s, ref = _pair(c["sym"])
        for h in HORIZONS:
            if str(h) in c["x"] or np.busday_count(d, today) < h:
                continue
            target = np.busday_offset(d, h, roll="forward").astype(date)
            close, bench = _close(histories.get(s)), _close(histories.get(ref))
            if (close is None or not len(close) or bench is None or not len(bench)
                    or close.index[-1].date() < target or bench.index[-1].date() < target):
                continue
            p, b = _at(close, target), _at(bench, target)
            if p and b:
                c["x"][str(h)] = round((p / c["px"] - 1) - (b / c["spx"] - 1), 5)


def edge(call, excess) -> float:
    return excess if call in ("BUY", "SWITCH") else -excess


def _summary(rows):
    """(n, hit rate, average edge) of [(call, excess)]."""
    edges = [edge(c, x) for c, x in rows]
    return len(edges), sum(e > 0 for e in edges) / len(edges) if edges else 0.0, \
        sum(edges) / len(edges) if edges else 0.0


def _fmt(rows) -> str:
    n, hit, avg = _summary(rows)
    if n < MIN_N:
        return f"too early (n={n})"
    return f"{hit * 100:.0f}% won, avg {avg * 100:+.1f}% vs CSPX (n={n})"


def stats(st) -> str:
    """The /stats reply: hit rate and average edge vs CSPX.L per horizon and call type."""
    calls = st.get("calls") or []
    if not calls:
        return "📭 No calls tracked yet: they are logged when a scan message is sent."
    lines = [f"📊 Track record of {len(calls)} sent calls since {calls[0]['d']}. Won = BUY beat CSPX.L; SELL / WAIT "
             "won when the stock lagged CSPX.L; SWITCH won when the bought stock beat the sold one. Avg = the call's edge over CSPX.L (positive = the call helped)."]
    for h, label in HORIZONS.items():
        rows = [(c["call"], c["x"][str(h)]) for c in calls if str(h) in c["x"]]
        lines.append(f"\n🗓️ {label} ({h} trading days): all {_fmt(rows)}")
        for call in TRACKED:
            part = [r for r in rows if r[0] == call]
            if part or call not in ("WAIT", "SWITCH"):
                lines.append(f"• {call}: {_fmt(part)}")
    lines.append(f"\nℹ️ Too early = fewer than {MIN_N} scored calls.")
    return "\n".join(lines)


def one_liner(st) -> str:
    calls = st.get("calls") or []
    parts = [f"{label} {_fmt([(c['call'], c['x'][str(h)]) for c in calls if str(h) in c['x']])}"
             for h, label in HORIZONS.items()]
    return "Calls: " + "; ".join(parts)
