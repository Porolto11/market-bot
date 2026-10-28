"""Offline backtest of the scan's price signals (ROADMAP 8, docs/AUDIT.md Deliverable 6). Run by hand on a PC:

    python tools/backtest.py            # downloads 2012..today once into tools/.bt_cache.pkl (gitignored)

Monthly: on the first trading day of each month, hold the top 5 of the universe by the signal, equal weight,
trading at that close on the previous close's signal. K&H costs: 0.24% (min 3.49 USD) per side, slippage, 15%
tax on each year's net realised gain (losses not carried), the rest taxed at the end. Benchmark: SPY.
Signals: 'mom' = plain 12-1 momentum; 'mom+trend' = the same, only above the 200-day average (the shadow model,
scan.mom_only); 'bot' = the price part of today's score (momentum + trend, RSI <= 70).
'stop' variants sell a position at the first close 12% below its cost (watch.HARD_STOP); the cash waits for
the next month.

BIASES: the universes are today's lists (survivorship), growth and analyst targets have no point-in-time data
and are left out, and a backtest Sharpe barely predicts live results (docs/RESEARCH.md). Compare the variants
with each other; trust the forward test in /stats more.
"""
import os
import pickle
import sys
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from invest_bot import scan, watch  # noqa: E402

warnings.filterwarnings("ignore")
TAX, RATE, MIN_FEE = 0.15, 0.0024, 3.49
SP100 = ("AAPL ABBV ABT ACN ADBE AIG AMD AMGN AMT AMZN AVGO AXP BA BAC BK BKNG BLK BMY BRK-B C CAT CHTR CL CMCSA COF "
         "COP COST CRM CSCO CVS CVX DE DHR DIS DUK EMR F FDX GD GE GILD GM GOOGL GS HD HON IBM INTC INTU ISRG JNJ JPM KO "
         "LIN LLY LMT LOW MA MCD MDLZ MDT MET META MMM MO MRK MS MSFT NEE NFLX NKE NOW NVDA ORCL PEP PFE PG PLTR PM "
         "PYPL QCOM RTX SBUX SCHW SO SPG T TGT TMO TMUS TSLA TXN UBER UNH UNP UPS USB V VZ WFC WMT XOM").split()
BOT = [s for s, (_, k, r, *_) in scan.UNIVERSE.items() if k == "stock" and r == "US"]
CACHE = os.path.join(os.path.dirname(__file__), ".bt_cache.pkl")


def prices():
    if os.path.exists(CACHE):
        with open(CACHE, "rb") as f:
            px = pickle.load(f)
    else:
        import yfinance as yf
        syms = sorted({*BOT, *SP100, "SPY"})
        d = yf.download(syms, start="2012-01-01", auto_adjust=True, progress=False, group_by="ticker")
        px = pd.DataFrame({s: d[s]["Close"] for s in syms if s in d.columns.get_level_values(0)})
        with open(CACHE, "wb") as f:
            pickle.dump(px, f)
    px = px.sort_index()
    return px[px.index < pd.Timestamp.today().normalize()]  # no unfinished bar


def signal(c, i, how, n=5):
    w = c.iloc[:i + 1]
    w = w.loc[:, w.notna().sum() >= 253]
    if w.shape[1] == 0:
        return []
    last, s200 = w.iloc[-1], w.iloc[-200:].mean()
    m121 = w.iloc[-22] / w.iloc[-253] - 1
    if how == "mom":
        sc = m121
    elif how == "mom+trend":
        sc = m121[last > s200]
    else:
        m6, m12, s50 = last / w.iloc[-127] - 1, last / w.iloc[-253] - 1, w.iloc[-50:].mean()
        tr = (last > s200).astype(int) + (s50 > s200).astype(int) + (m6 > 0).astype(int) + (m12 > 0).astype(int)
        dl = w.iloc[-300:].diff()
        g = dl.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        lo = (-dl.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
        rsi = 100 - 100 / (1 + g / lo)
        tr = tr - (rsi > 75).astype(int)
        sc = 20 * m121.rank(pct=True) + 20 * m6.rank(pct=True) + 15 * (tr.clip(0, 4) / 4)
        sc = sc[(tr >= 3) & (rsi <= 70)]
    return list(sc.dropna().sort_values(ascending=False).index[:n])


def run(px, univ, how, cap=3000.0, start="2014-01-01", end=None, slip=0.001, stop=None, exit=None):
    """exit: optional daily rule exit(closes up to today, symbol) -> True to sell at today's close."""
    c, idx = px[univ], px.index
    rebal = {i for i in range(1, len(idx)) if idx[i].month != idx[i - 1].month and idx[i] >= pd.Timestamp(start)
             and (end is None or idx[i] <= pd.Timestamp(end))}
    first, last_i = min(rebal), max(rebal)
    cash, pos, gains, vals, fees, trades = cap, {}, {}, [], 0.0, 0

    def sell(s, i):
        nonlocal cash, fees, trades
        sh, cost = pos.pop(s)
        p = c[s].iloc[:i + 1].dropna().iloc[-1]
        v = sh * p * (1 - slip)
        f = max(RATE * v, MIN_FEE)
        cash += v - f
        fees, trades = fees + f, trades + 1
        gains[idx[i].year] = gains.get(idx[i].year, 0) + v - f - cost

    for i in range(first, last_i + 1):
        if idx[i].year != idx[i - 1].year and i > first:
            cash -= TAX * max(0, gains.get(idx[i - 1].year, 0))
        price = c.iloc[i]
        if stop:
            for s in [s for s, (sh, cost) in pos.items() if price[s] * sh <= cost * (1 - stop)]:
                sell(s, i)
        if exit:
            for s in [s for s in pos if exit(c[s].iloc[:i + 1].dropna(), s)]:
                sell(s, i)
        if i not in rebal:
            continue
        target = signal(c, i - 1, how)
        for s in [s for s in pos if s not in target or np.isnan(price[s])]:
            sell(s, i)
        new = [s for s in target if s not in pos and not np.isnan(price[s])]
        per = cash / len(new) if new else 0
        for s in new:
            f = max(RATE * per, MIN_FEE)
            if per - f > 0:
                pos[s] = [(per - f) / (price[s] * (1 + slip)), per]
                cash, fees, trades = cash - per, fees + f, trades + 1
        vals.append((idx[i], cash + sum(sh * price[s] for s, (sh, _) in pos.items() if not np.isnan(price[s]))))
    v = pd.Series(dict(vals))
    open_gain = sum(sh * c[s].iloc[last_i] - cost for s, (sh, cost) in pos.items())
    after = v.iloc[-1] - TAX * max(0, gains.get(v.index[-1].year, 0) + open_gain)
    yrs = (v.index[-1] - v.index[0]).days / 365.25
    return dict(cagr=(v.iloc[-1] / cap) ** (1 / yrs) - 1, cagr_at=(after / cap) ** (1 / yrs) - 1,
                mdd=(v / v.cummax() - 1).min(), trades=trades, fee_pct=fees / cap)


def bench(px, start="2014-01-01", end=None):
    s = px["SPY"]
    v = s[(s.index >= pd.Timestamp(start)) & ((s.index <= pd.Timestamp(end)) if end else True)].resample("MS").first()
    yrs = (v.index[-1] - v.index[0]).days / 365.25
    after = v.iloc[-1] - TAX * (v.iloc[-1] - v.iloc[0])
    return dict(cagr=(v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1, cagr_at=(after / v.iloc[0]) ** (1 / yrs) - 1,
                mdd=(v / v.cummax() - 1).min())


def line(name, r):
    extra = f"  trades {r['trades']:4d}  fees {r['fee_pct']:.0%} of start" if "trades" in r else ""
    return f"{name:34s} CAGR {r['cagr']:+6.1%}  after tax {r['cagr_at']:+6.1%}  max drawdown {r['mdd']:4.0%}{extra}"


def main():
    px = prices()
    sp, bu = [s for s in SP100 if s in px], [s for s in BOT if s in px]
    print(f"data {px.index[0].date()} .. {px.index[-1].date()}, {px.shape[1]} symbols; $3000 start")
    for period, (a, e) in {"2014-today": ("2014-01-01", None), "2014-2019": ("2014-01-01", "2019-12-31"),
                           "2020-today": ("2020-01-01", None)}.items():
        print(f"\n=== {period}\n" + line("SPY buy & hold", bench(px, a, e)))
        for name, u in (("S&P100", sp), ("bot US", bu)):
            for how in ("mom", "mom+trend", "bot"):
                print(line(f"{name} {how}", run(px, u, how, start=a, end=e)))
                print(line(f"{name} {how} + {watch.HARD_STOP:.0%} stop",
                           run(px, u, how, start=a, end=e, stop=watch.HARD_STOP)))


if __name__ == "__main__":
    main()
