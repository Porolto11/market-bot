"""Alerts from the scan's own price histories: on the holdings a trailing stop (15% below the peak close since the
first buy) and a hard stop (HARD_STOP = 12% below the average cost in HUF,
Han-Zhou-Zhu in docs/RESEARCH.md); on the core ETFs a dip of 8% or more below the
52-week high. Each is a (dedupe key, line) pair; stop keys go into the state's `alerts` list once the message is
sent, the dip depths into its `dips` dict ({symbol: {d, dd}} of the last dip alert).
fx_lines: the Monday summary's EUR/HUF and USD/HUF line against the last 3 months.
"""
from datetime import date

import numpy as np

from invest_bot import analysis, fmt, prices, rules, scan

STOP = 0.15  # trailing, from the peak
HARD_STOP = 0.12  # vs cost: the stop each BUY shows at entry
CORE = ("CSPX.L", "CNDX.AS", "VVSM.DE", "VWCE.DE")
DIP, BIG_DIP = -0.08, -0.15  # drawdown from the 52-week high
DIP_EVERY = 20  # trading days before the same dip is alerted again
DIP_DEEPER = 0.05  # or sooner, once it is 5 more points deeper than at the last alert
FX = (("EUR/HUF", "EURHUF=X"), ("USD/HUF", "USDHUF=X"))
FX_DAYS = 63  # about 3 months of trading days
FX_EDGE = 0.8  # comment only when the rate is cheaper or pricier than 80% of the window


def _close(hist):
    try:
        return hist["Close"].dropna().astype(float)
    except Exception:
        return None


def first_buys(log) -> dict:
    """{symbol: date of the first buy of the position held now} from the trade log."""
    held, first = {}, {}
    for e in log:
        if e["t"] not in ("buy", "sell"):
            continue
        q = held.get(e["sym"], 0)
        if e["t"] == "buy" and q == 0:
            first[e["sym"]] = date.fromisoformat(e["at"][:10])
        held[e["sym"]] = q + (e["qty"] if e["t"] == "buy" else -e["qty"])
    return {s: d for s, d in first.items() if held.get(s, 0) > 0}


def stops(portfolio, log, hists, items, rates, now, seen) -> list:
    """[(key, line)]: per holding, at most once per ISO week (key stop|SYM|week)."""
    first, by, out = first_buys(log), {i["symbol"]: i for i in items}, []
    week = "{}-W{:02d}".format(*now.isocalendar()[:2])
    for h in (portfolio or {}).get("holdings") or []:
        s = h.get("symbol") or prices.TICKERS.get(h.get("ticker"), h.get("ticker"))
        key, c = f"stop|{s}|{week}", _close(hists.get(s))
        if key in seen or c is None or not len(c):
            continue
        last, i, lines = float(c.iloc[-1]), by.get(s), []
        ccy = i["ccy"] if i else scan.default_ccy(s)
        since = c[c.index.date >= first[s]] if s in first else None
        if since is not None and len(since) and last <= since.max() * (1 - STOP):
            peak = float(since.max())
            pct = f"{fmt.tick(s)} is {(1 - last / peak) * 100:.1f}% below its peak since you bought"
            if i and i["score"] >= scan.BUY_SCORE and i["rating"] == "bullish":  # the scan would call it a BUY
                lines.append(f"⚠️ {pct}, but the trend/score is still strong ({i['score']}) — hold unless it falls "
                             f"further (hard stop at −{HARD_STOP:.0%} vs your cost).")
            else:
                lines.append(f"🛑 {pct} (peak {fmt.price(round(peak, 2), ccy)}, now {fmt.price(round(last, 2), ccy)}) "
                             "— consider selling.")
        rate, avg = scan.huf_rate(ccy, rates), h.get("avg_cost_huf")
        if rate and avg and last * rate <= avg * (1 - HARD_STOP):
            lines.append(f"🛑 {fmt.tick(s)} is {(1 - last * rate / avg) * 100:.1f}% below your average cost (avg "
                         f"{rules._huf(avg)} HUF, now {rules._huf(last * rate)} HUF a share) — hard stop, "
                         "consider selling.")
        if lines:
            out.append((key, "\n".join(lines)))
    return out


def dips(hists, now, last) -> tuple:
    """(alerts [(key dip|SYM|date, line)], new `dips` state) for the core ETFs at least 8% below their 52-week high
    (15%: a big dip). last: {symbol: {d, dd}} of the last alert. The same dip is alerted again only when it is
    DIP_DEEPER deeper (-20% -> -25%) or DIP_EVERY trading days later; an ETF back above -8% is forgotten."""
    today, out, new = now.date(), [], {}
    for s in CORE:
        dd, prev = analysis.indicators(hists.get(s)).get("dd_52w"), last.get(s)
        if dd is None:
            if prev:
                new[s] = prev
            continue
        if dd > DIP:
            continue
        if prev and dd > prev["dd"] - DIP_DEEPER and np.busday_count(date.fromisoformat(prev["d"]), today) < DIP_EVERY:
            new[s] = prev
            continue
        new[s] = dict(d=today.isoformat(), dd=round(dd, 4))
        name = fmt.tick(s)
        out.append((f"dip|{s}|{today}", "🛒 " + ("Big dip: " if dd <= BIG_DIP else "") + f"{name} is {-dd * 100:.1f}% "
                    "below its 52-week high — good moment to add if you have cash."))
    return out, new


def fx_lines(hists) -> list:
    """'EUR/HUF 386.2 (3-month avg 390.1)' per pair, with a comment when the rate is cheaper than 80% of the last
    3 months (good time to convert) or pricier than 80% (consider waiting). Yahoo FX, not the bank's rate."""
    out = []
    for name, sym in FX:
        c = _close(hists.get(sym))
        if c is None or len(c) < 20:
            continue
        c = c.iloc[-FX_DAYS:]
        last, before = float(c.iloc[-1]), c.iloc[:-1]
        line = f"{name} {last:.1f} (3-month avg {float(c.mean()):.1f})"
        above, below = float((before > last).mean()), float((before < last).mean())
        if above >= FX_EDGE:
            line += f" — cheaper than {above * 100:.0f}% of the last 3 months (good time to convert)"
        elif below >= FX_EDGE:
            line += f" — pricier than {below * 100:.0f}% of the last 3 months, consider waiting"
        out.append(line)
    return out
