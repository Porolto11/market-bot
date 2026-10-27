"""Market scan: rank ~90 stocks and ETFs by a 0-100 potential score and give clear BUY / WAIT / SELL calls.

Deterministic, no LLM. Pure functions of injected price histories, fundamentals, cash, holdings and FX rates,
so the same code runs in the daily review (review.run_review) and in the cloud runner (invest_bot.cloud).
Only the momentum part of the score is backtested; growth and analyst targets are today's yfinance figures.
"""
import json
import math
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from invest_bot import analysis, fees, hours, prices, rules

# yfinance symbol: (name, kind, region[, ETF payout: "acc" accumulating, "dist" distributing (taxable payouts)])
UNIVERSE = {
    # US mega caps and growth
    "AAPL": ("Apple", "stock", "US"), "MSFT": ("Microsoft", "stock", "US"), "NVDA": ("Nvidia", "stock", "US"),
    "AMZN": ("Amazon", "stock", "US"), "GOOGL": ("Alphabet", "stock", "US"), "META": ("Meta Platforms", "stock", "US"),
    "AVGO": ("Broadcom", "stock", "US"), "TSLA": ("Tesla", "stock", "US"), "BRK-B": ("Berkshire Hathaway", "stock", "US"),
    "JPM": ("JPMorgan Chase", "stock", "US"), "LLY": ("Eli Lilly", "stock", "US"), "V": ("Visa", "stock", "US"),
    "MA": ("Mastercard", "stock", "US"), "COST": ("Costco", "stock", "US"), "NFLX": ("Netflix", "stock", "US"),
    "AMD": ("AMD", "stock", "US"), "ORCL": ("Oracle", "stock", "US"), "PLTR": ("Palantir", "stock", "US"),
    "CRWD": ("CrowdStrike", "stock", "US"), "NET": ("Cloudflare", "stock", "US"), "SHOP": ("Shopify", "stock", "US"),
    "SNOW": ("Snowflake", "stock", "US"), "MELI": ("MercadoLibre", "stock", "US"), "ARM": ("Arm Holdings", "stock", "US"),
    "TSM": ("TSMC", "stock", "US"), "ANET": ("Arista Networks", "stock", "US"), "APP": ("AppLovin", "stock", "US"),
    "UBER": ("Uber", "stock", "US"), "NOW": ("ServiceNow", "stock", "US"), "PANW": ("Palo Alto Networks", "stock", "US"),
    "INTU": ("Intuit", "stock", "US"), "ISRG": ("Intuitive Surgical", "stock", "US"),
    "UNH": ("UnitedHealth", "stock", "US"), "XOM": ("Exxon Mobil", "stock", "US"), "WMT": ("Walmart", "stock", "US"),
    "KO": ("Coca-Cola", "stock", "US"), "ADBE": ("Adobe", "stock", "US"), "CRM": ("Salesforce", "stock", "US"),
    "QCOM": ("Qualcomm", "stock", "US"), "MU": ("Micron", "stock", "US"), "AMAT": ("Applied Materials", "stock", "US"),
    "LRCX": ("Lam Research", "stock", "US"), "KLAC": ("KLA", "stock", "US"), "VRT": ("Vertiv", "stock", "US"),
    "HOOD": ("Robinhood", "stock", "US"), "RKLB": ("Rocket Lab", "stock", "US"),
    "LIN": ("Linde", "stock", "US"), "CAT": ("Caterpillar", "stock", "US"), "ABBV": ("AbbVie", "stock", "US"),
    "GS": ("Goldman Sachs", "stock", "US"), "SNPS": ("Synopsys", "stock", "US"), "CDNS": ("Cadence", "stock", "US"),
    "IBM": ("IBM", "stock", "US"), "GE": ("GE Aerospace", "stock", "US"), "SPOT": ("Spotify", "stock", "US"), "DDOG": ("Datadog", "stock", "US"),
    # EU
    "ASML.AS": ("ASML", "stock", "EU"), "SAP.DE": ("SAP", "stock", "EU"), "SIE.DE": ("Siemens", "stock", "EU"),
    "ALV.DE": ("Allianz", "stock", "EU"), "RHM.DE": ("Rheinmetall", "stock", "EU"), "MC.PA": ("LVMH", "stock", "EU"),
    "AIR.PA": ("Airbus", "stock", "EU"), "NOVO-B.CO": ("Novo Nordisk", "stock", "EU"),
    "ADYEN.AS": ("Adyen", "stock", "EU"), "IFX.DE": ("Infineon", "stock", "EU"),
    "SU.PA": ("Schneider Electric", "stock", "EU"), "DTE.DE": ("Deutsche Telekom", "stock", "EU"),
    # HU
    "OTP.BD": ("OTP Bank", "stock", "HU"), "MOL.BD": ("MOL", "stock", "HU"), "RICHT.BD": ("Richter Gedeon", "stock", "HU"),
    "MTELEKOM.BD": ("Magyar Telekom", "stock", "HU"),
    # ETFs
    "CSPX.L": ("iShares Core S&P 500", "etf", "US", "acc"), "CNDX.AS": ("iShares Nasdaq 100", "etf", "US", "acc"),
    "VVSM.DE": ("VanEck Semiconductor", "etf", "World", "acc"),
    "VWCE.DE": ("Vanguard FTSE All-World", "etf", "World", "acc"),
    "IWDA.AS": ("iShares Core MSCI World", "etf", "World", "acc"),
    "EIMI.L": ("iShares Core MSCI EM IMI", "etf", "EM", "acc"),
    "IGLN.L": ("iShares Physical Gold", "etf", "World", "acc"),  # a physical gold ETC: no payouts
    "VHYL.AS": ("Vanguard All-World High Dividend", "etf", "World", "dist"),
}
CALLS = ("BUY", "WAIT_FOR_DIP", "WAIT", "HOLD", "SELL", "AVOID")  # WAIT: a BUY held back by earnings or news
ACTIONABLE = ("BUY", "WAIT_FOR_DIP", "SELL")
BUY_SCORE = 70
RSI_HOT = 70
MIN_ANALYSTS = 5
UPSIDE_CAP = 0.30  # +30% analyst upside or more earns the full 20 points
EARNINGS_DAYS = 7  # flag
EARNINGS_WAIT = 3  # a BUY this close to earnings becomes WAIT
FEE_MAX = 0.01  # a buy whose K&H fee is more than 1% of its value is skipped
SECTOR_MAX = 2  # BUY picks per sector
CORR_MAX = 0.75  # a BUY pick this correlated with a higher pick is skipped (6-month daily returns)
CORR_DAYS = 126
PLAN_SIZE = 3  # BUY picks in the buy plan
BUFFER = 0.01  # of the cash kept back for FX drift
SUFFIX_CCY = {".AS": "EUR", ".DE": "EUR", ".PA": "EUR", ".CO": "DKK", ".BD": "HUF", ".L": "USD"}  # .L: our USD ETFs
FUND_KEYS = ("revenue_growth", "earnings_growth", "profit_margin", "target_mean", "analysts", "currency",
             "earnings_date", "quote_type", "sector")
NOTE = "Score uses today's fundamentals and analyst targets; only the momentum part is backtested."
BOT_TICKER = {v: k for k, v in prices.TICKERS.items()}  # yfinance symbol -> the bot's ticker


def _num(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _earnings(info) -> str | None:
    for key in ("earningsTimestampStart", "earningsTimestamp"):
        ts = _num(info.get(key))
        if ts:
            return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
    return None


def parse_info(info: dict) -> dict:
    """The fields the score uses from yfinance Ticker.info; every one may be missing (None)."""
    info = info if isinstance(info, dict) else {}
    n = _num(info.get("numberOfAnalystOpinions"))
    return dict(revenue_growth=_num(info.get("revenueGrowth")), earnings_growth=_num(info.get("earningsGrowth")),
                profit_margin=_num(info.get("profitMargins")), target_mean=_num(info.get("targetMeanPrice")),
                analysts=int(n) if n is not None else None, currency=info.get("currency") or None,
                earnings_date=_earnings(info), quote_type=info.get("quoteType") or None,
                sector=info.get("sector") or None)


def fundamentals(symbol, info_fn=prices.info, conn=None, today: str | None = None) -> dict:
    """parse_info(info_fn(symbol)), cached per day in price_cache (key 'fund:<symbol>') when conn is given.
    A failed fetch raises and is not cached."""
    key = "fund:" + symbol
    if conn is not None and today:
        row = conn.execute("SELECT json FROM price_cache WHERE date=? AND ticker=?", (today, key)).fetchone()
        if row:
            return json.loads(row[0])
    out = parse_info(info_fn(symbol))
    if conn is not None and today:
        conn.execute("INSERT OR REPLACE INTO price_cache(date, ticker, json) VALUES (?,?,?)",
                     (today, key, json.dumps(out)))
        conn.commit()
    return out


def features(hist) -> dict:
    """analysis.indicators plus mom_12_1: 12-month momentum leaving out the last month (21 sessions)."""
    ind = analysis.indicators(hist)
    ind["mom_12_1"] = None
    if hist is not None and "Close" in hist:
        c = hist["Close"].dropna().astype(float)
        if len(c) > 252:
            ind["mom_12_1"] = float(c.iloc[-22] / c.iloc[-253] - 1)
    return ind


def _long_mom(ind):
    return ind.get("mom_12_1") if ind.get("mom_12_1") is not None else ind.get("mom_6m")


def _pct(v, values):
    """Percentile rank 0..1 of v among values (ties count half); None when unknown."""
    values = [x for x in values if x is not None]
    if v is None or not values:
        return None
    return (sum(x < v for x in values) + 0.5 * sum(x == v for x in values)) / len(values)


def _clamp(x):
    return max(0.0, min(1.0, x))


def _trading_days(start: date, end: date) -> int:
    return int(np.busday_count(start, end))


def earnings_in(fund, today):
    """Trading days until the next earnings date (0: today), or None when unknown or past."""
    try:
        d = date.fromisoformat(str((fund or {}).get("earnings_date"))[:10])
    except ValueError:
        return None
    return _trading_days(today, d) if d >= today else None


def potential(ind, fund, universe_moms, today: date | None = None):
    """(score 0..100, parts, flags). parts: momentum (40, percentile ranks of the long and 6-month momentum in the
    universe), growth (25; ETFs and unknown values neutral), upside (20, analyst target vs last price, only with
    >= 5 analysts, else neutral 10), trend (15, from analysis.rate). fund['kind']='etf' marks an ETF.
    universe_moms: {'long': [...], 'mom_6m': [...]}; today: for the earnings flag."""
    fund = fund or {}
    moms = universe_moms or {}
    long_p, six_p = _pct(_long_mom(ind), moms.get("long", [])), _pct(ind.get("mom_6m"), moms.get("mom_6m", []))
    momentum = 20 * (long_p or 0) + 20 * (six_p or 0)
    if fund.get("kind") == "etf":
        growth = 12.5
    else:
        rg, eg, pm = fund.get("revenue_growth"), fund.get("earnings_growth"), fund.get("profit_margin")
        growth = ((5 if rg is None else 10 * _clamp(rg / 0.30)) + (5 if eg is None else 10 * _clamp(eg / 0.40))
                  + (2.5 if pm is None else 5 * _clamp(pm / 0.25)))
    last, target, n = ind.get("last"), fund.get("target_mean"), fund.get("analysts") or 0
    upside_pct = target / last - 1 if target and last and n >= MIN_ANALYSTS else None
    upside = 10.0 if upside_pct is None else 20 * _clamp(upside_pct / UPSIDE_CAP)
    r = analysis.rate(ind)
    trend = 0.0 if r["rating"] == "n/a" else 15 * _clamp(r["score"] / 4)
    parts = dict(momentum=round(momentum, 1), growth=round(growth, 1), upside=round(upside, 1),
                 trend=round(trend, 1))
    score = int(round(momentum + growth + upside + trend))
    flags = []
    ed = fund.get("earnings_date")
    if ed and today:
        try:
            d = date.fromisoformat(str(ed)[:10])
        except ValueError:
            d = None
        if d and d >= today and _trading_days(today, d) <= EARNINGS_DAYS:
            n_days = _trading_days(today, d)
            flags.append(f"earnings in {n_days} day{'' if n_days == 1 else 's'}")
    if (ind.get("vol_20d") or 0) > 0.60:
        flags.append("very volatile")
    if ind.get("dd_52w") is not None and ind["dd_52w"] < -0.30:
        flags.append("far below 52w high")
    if ind.get("rsi14") is not None and ind["rsi14"] > 75:
        flags.append("overheated")
    return score, dict(parts, upside_pct=upside_pct), flags


def default_ccy(symbol) -> str:
    bt = BOT_TICKER.get(symbol)
    if bt:
        if bt in rules.INSTRUMENTS:
            return rules.INSTRUMENTS[bt][1]
    for suffix, ccy in SUFFIX_CCY.items():
        if symbol.endswith(suffix):
            return ccy
    return "USD"


def huf_rate(ccy, fx_rates) -> float | None:
    """HUF per 1 unit of ccy from fx_rates ({ccy: HUF}); GBp/GBX (pence) = GBP / 100. None when unknown."""
    if ccy == "HUF":
        return 1.0
    if ccy in ("GBp", "GBX"):
        g = (fx_rates or {}).get("GBP")
        return g / 100 if g else None
    return (fx_rates or {}).get(ccy) or None


def _fee(value, ccy, fx_rates, symbol=None):
    """K&H commission in the instrument currency: 0.24%, at least the minimum of the symbol's market (fees.minimum,
    e.g. 11.99 EUR on Copenhagen), converted to the instrument currency at Yahoo FX when it differs (0 when unknown)."""
    amount, mccy = fees.minimum(symbol, ccy)
    if mccy == ccy:
        floor = amount
    else:
        m, own = huf_rate(mccy, fx_rates), huf_rate(ccy, fx_rates)
        floor = amount * m / own if m and own else 0.0
    return max(value * fees.RATE, floor)


def size(budget_huf, limit, ccy, fx_rates, symbol=None, kind=None):
    """(qty, approx_huf, one_share_huf): whole shares for the budget incl. fee, buy-side tax (fees.buy_tax) and FX
    spread."""
    rate = huf_rate(ccy, fx_rates)
    if rate is None or not limit:
        return None, None, None
    spread, tax = 1 + (rules.FX_SPREAD if ccy != "HUF" else 0), fees.buy_tax(symbol, kind)

    def total(q):  # in the instrument currency
        return q * limit * (1 + tax) + _fee(q * limit, ccy, fx_rates, symbol)
    one = total(1) * rate * spread
    if budget_huf is None:
        return None, None, one
    cap = budget_huf / spread
    qty = max(0, math.floor(cap / (limit * (1 + tax) * rate)))
    while qty > 0 and total(qty) * rate > cap:
        qty -= 1
    return qty, (total(qty) * rate if qty else None), one


def _until(symbol, today, n):
    bt = BOT_TICKER.get(symbol)
    if bt in hours.MARKET:
        return (rules.dip_deadline(bt, today) if n == rules.DIP_DAYS
                else hours.add_trading_days(bt, today, n)).isoformat()
    return str(np.busday_offset(today, n, roll="backward"))


def _huf(x):
    return rules._huf(x)


def _why(score, ind, parts):
    bits = []
    m = _long_mom(ind)
    if m is not None:
        label = "12-month momentum (ex last month)" if ind.get("mom_12_1") is not None else "6-month momentum"
        bits.append(f"{label} {m * 100:+.0f}%")
    if parts.get("upside_pct") is not None:
        bits.append(f"analysts see {parts['upside_pct'] * 100:+.0f}% to their target")
    return f"Score {score}/100" + (": " + ", ".join(bits) if bits else "") + "."


def _call(held, rating, score, rsi):
    if held:
        return "SELL" if rating == "bearish" else "HOLD"
    if rating == "bearish":
        return "AVOID"
    if score >= BUY_SCORE and rsi is not None and rsi > RSI_HOT:
        return "WAIT_FOR_DIP"
    if score >= BUY_SCORE and rating == "bullish":
        return "BUY"
    return None


def run_scan(histories, funds, now, cash_huf, holdings, fx_rates, moms=None, stale=None) -> list[dict]:
    """Ranked scan items (highest score first). histories/funds: {symbol: ...}; holdings: {symbol: qty held};
    cash_huf: free cash (None: unknown, no share counts); each BUY / WAIT_FOR_DIP gets cash_huf / 2.
    moms: the universe's momentum lists ({'long': [...], 'mom_6m': [...]}) to rank against, e.g. from the last
    full scan when scanning one symbol; default: from these histories. stale: {symbol: last bar date} of symbols
    without a fresh close (prices.fresh): they get no BUY, dip or SELL call, and a 'stale data' flag."""
    today = now.date() if isinstance(now, datetime) else now
    funds, holdings, stale = funds or {}, holdings or {}, stale or {}
    inds = {}
    for s, h in histories.items():
        try:
            ind = features(h)
        except Exception:  # malformed history: left out
            continue
        if ind.get("last"):
            inds[s] = ind
    moms = moms or dict(long=[_long_mom(i) for i in inds.values()], mom_6m=[i.get("mom_6m") for i in inds.values()])
    budget = cash_huf / 2 if cash_huf is not None else None
    out = []
    for s, ind in inds.items():
        name, kind, *rest = UNIVERSE.get(s, (s, None, None))
        fund = dict(funds.get(s) or {})
        kind = kind or ("etf" if fund.get("quote_type") == "ETF" else "stock")
        score, parts, flags = potential(ind, dict(fund, kind=kind), moms, today)
        r = analysis.rate(ind)
        rating, last, rsi = r["rating"], ind["last"], ind.get("rsi14")
        ccy = fund.get("currency") or default_ccy(s)
        held = float(holdings.get(s) or 0)
        call = _call(held > 0, rating, score, rsi)
        why = _why(score, ind, parts)
        if s in stale:
            flags.append(f"stale data (last close {stale[s] or '?'})")
            if call in ACTIONABLE:
                call = None
        limit = qty = approx = until = one = None
        days = earnings_in(fund, today)
        if call == "BUY" and days is not None and days <= EARNINGS_WAIT:
            call = "WAIT"
            when = "today" if days == 0 else f"in {days} day{'' if days == 1 else 's'}"
            why, act = f"Earnings {when} — wait for the report.", why
        elif call in ("BUY", "WAIT_FOR_DIP"):
            if call == "BUY":
                limit, until = rules._tick(last * (1 + rules.SLIP), ccy), _until(s, today, 1)
            else:
                _, limit, _ = rules.dip_rule(ind, rating, last, ccy)
                until = _until(s, today, rules.DIP_DAYS)
            qty, approx, one = size(budget, limit, ccy, fx_rates, s, kind)
            if one is None:
                tail = "Exchange rate unavailable, so no share count."
            elif qty is None:
                tail = "Cash unknown, so no share count."
            elif qty == 0:
                tail = f"1 share ≈ {_huf(one)} HUF, more than your budget ({_huf(budget)} HUF)."
            else:
                tail = f"{'Buy ' if call == 'BUY' else ''}{qty} share{'' if qty == 1 else 's'} ≈ {_huf(approx)} HUF."
            if call == "BUY":
                act = f"Buy at up to {rules._px(limit)} {ccy}, valid until {until}. {tail}"
            else:
                act = (f"RSI {rsi:.0f} is overheated: wait for a dip to {rules._px(limit)} {ccy} until {until}; "
                       f"if it doesn't come, buy anyway. {tail}")
        elif call == "SELL":
            qty = int(held)
            limit, until = rules._tick(last * (1 - rules.SLIP), ccy), _until(s, today, 1)
            rate = huf_rate(ccy, fx_rates)
            approx = qty * limit * rate if rate else None
            act = (f"Trend turned bearish (score {r['score']}): sell {qty} at {rules._px(limit)} {ccy}"
                   + (f" (≈ {_huf(approx)} HUF)." if approx else "."))
        elif call == "HOLD":
            act = f"Trend is {rating}: hold."
        elif call == "AVOID":
            act = f"Trend is bearish (score {r['score']}): avoid for now."
        else:
            act = "No fresh close, so no call." if s in stale else ""
        out.append(dict(symbol=s, name=name, kind=kind, ccy=ccy, last=last, score=score, parts=parts, rating=rating,
                        call=call, limit=limit, qty=qty, approx_huf=approx, one_share_huf=one, valid_until=until,
                        reason=f"{why} {act}".strip(), why=why, flags=flags, mom_long=_long_mom(ind),
                        mom_6m=ind.get("mom_6m"), stale=s in stale,
                        sector="ETF" if kind == "etf" else fund.get("sector"), dist=rest[-1] == "dist"))
    out.sort(key=lambda i: (-i["score"], i["symbol"]))
    return out


def cost(i, qty, fx_rates) -> tuple:
    """(value, fee, buy tax) in HUF of qty shares at the item's limit, all with the bank's 1% FX spread
    (rules.FX_SPREAD). The tax: fees.buy_tax (FR 0.4%, UK 0.5%, ...; ETFs none)."""
    rate = huf_rate(i["ccy"], fx_rates) * (1 + (rules.FX_SPREAD if i["ccy"] != "HUF" else 0))
    value = qty * i["limit"] * rate
    return (value, _fee(qty * i["limit"], i["ccy"], fx_rates, i["symbol"]) * rate,
            value * fees.buy_tax(i["symbol"], i.get("kind")))


def min_qty(i, fx_rates) -> int:
    """The fewest whole shares (at least 1) whose K&H fee is at most FEE_MAX of their value."""
    return max(1, math.ceil(_fee(0.0, i["ccy"], fx_rates, i["symbol"]) / FEE_MAX / i["limit"] - 1e-9))


def plan(items, cash_huf, fx_rates, picks_max=PLAN_SIZE) -> list:
    """The BUY PLAN: at most picks_max BUY items to buy now, so that value + fee + buy tax + FX spread of all of them stays
    within cash_huf less a BUFFER for FX drift. Candidates: the BUY items in rank order (after diversify). A pick
    needs its smallest ticket with a fee of at most FEE_MAX (min_qty, at least 1 share) to fit into the cash / picks_max;
    the higher-ranked ones that do not are passed over. When fewer fit, a pick may use the cash the others leave
    (one big pick can take more). Sizing: the cash split equally, floored to whole shares (never below min_qty), then
    the leftover goes 1 share at a time to the highest-ranked pick that still fits. Sets plan=True, qty, value_huf,
    fee_huf, fee_pct, tax_huf and approx_huf (value + fee + tax) on the picks and returns them (rank order); WAIT_FOR_DIP items are
    sized from the cash the plan leaves."""
    budget = max(0.0, cash_huf or 0) * (1 - BUFFER)
    cands = [i for i in items if i["call"] == "BUY" and i.get("limit") and huf_rate(i["ccy"], fx_rates)]
    need = {id(i): sum(cost(i, min_qty(i, fx_rates), fx_rates)) for i in cands}
    picks = [i for i in cands if need[id(i)] <= budget / picks_max][:picks_max]
    left = budget * (1 - len(picks) / picks_max)
    for i in cands:  # fewer than picks_max fit: one may use the cash the others leave
        if len(picks) < picks_max and i not in picks and need[id(i)] <= left:
            picks.append(i)
            left -= need[id(i)]
    picks.sort(key=cands.index)
    qty = {id(i): max(min_qty(i, fx_rates), size(budget / len(picks), i["limit"], i["ccy"], fx_rates, i["symbol"], i.get("kind"))[0] or 0)
           for i in picks}

    def total():
        return sum(sum(cost(i, qty[id(i)], fx_rates)) for i in picks)
    while total() > budget:  # a big pick took more than an equal share: trim the others, lowest rank first
        i = next(i for i in reversed(picks) if qty[id(i)] > min_qty(i, fx_rates))
        qty[id(i)] -= 1
    spent = total()
    while True:  # the leftover: 1 share at a time to the highest-ranked pick that still fits
        for i in picks:
            extra = sum(cost(i, qty[id(i)] + 1, fx_rates)) - sum(cost(i, qty[id(i)], fx_rates))
            if spent + extra <= budget:
                qty[id(i)] += 1
                spent += extra
                break
        else:
            break
    for i in picks:
        value, fee, tax = cost(i, qty[id(i)], fx_rates)
        i.update(plan=True, qty=qty[id(i)], value_huf=value, fee_huf=fee, fee_pct=fee / value, tax_huf=tax,
                 approx_huf=value + fee + tax)
    for i in items:
        if i["call"] == "WAIT_FOR_DIP" and i.get("limit"):
            i["qty"], i["approx_huf"], _ = size(budget - spent, i["limit"], i["ccy"], fx_rates, i["symbol"],
                                                i.get("kind"))
    return picks


def _returns(hist):
    try:
        return hist["Close"].astype(float).pct_change().iloc[-CORR_DAYS:].dropna()
    except Exception:
        return None


def diversify(items, histories, n=5) -> None:
    """Walk the BUY items by rank and keep at most n: a pick is skipped (call None, 'skipped' says why) when SECTOR_MAX
    kept picks share its sector (unknown sectors never count) or its 6-month daily returns correlate more than
    CORR_MAX with a kept pick's. The BUYs below the n-th kept one are left as they are."""
    kept = []
    for i in [i for i in items if i["call"] == "BUY"]:
        if len(kept) >= n:
            break
        sector, mine = i.get("sector"), _returns(histories.get(i["symbol"]))
        why = None
        if sector and sum(k.get("sector") == sector for k in kept) >= SECTOR_MAX:
            why = f"{SECTOR_MAX} {sector} picks already"
        for k in kept:
            other = _returns(histories.get(k["symbol"]))
            if why or mine is None or other is None:
                break
            both = pd.concat([mine, other], axis=1, join="inner")
            if len(both) >= CORR_DAYS // 2 and both.corr().iloc[0, 1] > CORR_MAX:
                why = f"too similar to {k['symbol'].split('.')[0]}"
        if why:
            i.update(call=None, skipped=why)
        else:
            kept.append(i)
