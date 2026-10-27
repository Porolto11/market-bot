"""Hungarian tax on a normal account: exchange trades (ellenőrzött tőkepiaci ügylet, K&H qualifies) pay 15% SZJA and
no szocho on the year's net realised gain. Losses offset gains of the same calendar year, and a net loss declared in
a year's return (SZJA line 172, column 'a') offsets the gains of the next 2 years (oldest loss first, each used once).
There is no holding-period discount. Here: tax-aware switch calls, the December tax-loss helper (Monday summaries
from Dec 1 to Dec 20) and the /tax reply.

HUF amounts: realised P/L at the official MNB rate of the buy and the sell day (state.book 'taxed', FIFO, fees
included), or the trade's Yahoo FX where no MNB rate was recorded (approximate); today's values at approximate
Yahoo FX. A rough guide, not tax advice.
"""
from invest_bot import fmt, hours, prices, rules, scan, state

RATE = 0.15  # SZJA; exchange trades carry no szocho
CARRY_YEARS = 2
DECLARE = "📝 Declare this loss in your tax return (line 172 'a') to use it in the next 2 years."
US_DIVIDENDS = "dividends: 15% SZJA + 13% szocho (capped), US withholds 30%"
GAP = 25  # a switch needs the pick to score at least this much more than the holding
STRONG = 60  # a holding scoring this or more is a winner: never switched away unless its call is SELL
EDGE_PER_POINT = 0.002
MAX_SWITCHES = 2


def nets(log) -> dict:
    """{year: net realised P/L in HUF of the sales made that year} (FIFO, fees included, MNB rates where recorded)."""
    out = {}
    for e, pl in zip([e for e in log if e["t"] == "sell"], state.book(log)["taxed"]):
        out[int(e["at"][:4])] = out.get(int(e["at"][:4]), 0.0) + pl
    return out


def ytd(log, year) -> float:
    """Net realised P/L in HUF of the sales made in `year`."""
    return nets(log).get(year, 0.0)


def carry(log, year) -> float:
    """The net losses of the CARRY_YEARS years before `year` not used up yet (assumed declared): each later year's
    net gain uses them, oldest first, and a loss is used only once."""
    n, left = nets(log), {}
    for y in range(min(n, default=year), year):
        gain = n.get(y, 0.0)
        if gain < 0:
            left[y] = -gain
        for old in sorted(left):
            if gain > 0 and old >= y - CARRY_YEARS:
                use = min(gain, left[old])
                left[old], gain = left[old] - use, gain - use
    return sum(v for y, v in left.items() if y >= year - CARRY_YEARS)


def base(log, year) -> float:
    """This year's net realised P/L less the losses carried from the 2 years before: tax is RATE x max(0, base)."""
    pl = ytd(log, year)
    return pl - carry(log, year) if pl > 0 else pl


def approx(log) -> bool:
    """True when a foreign-currency trade has no MNB rate recorded (its tax figure uses Yahoo FX)."""
    return any(e["t"] in ("buy", "sell") and e["ccy"] != "HUF" and not e.get("fx_mnb") for e in log)


def us_stocks(symbols) -> list:
    """The direct US stocks among the symbols (non-EEA: their dividends carry szocho too)."""
    return [s for s in symbols if hours.mic_for(s) == "XNYS" and not s.startswith("^")
            and scan.UNIVERSE.get(s, ("", "stock"))[1] != "etf"]


def tax_on(gain, ytd_pl) -> float:
    """The extra tax from realising `gain` on top of this year's net realised P/L (base(): losses taken this year and
    carried from the 2 years before offset it); a loss adds no tax."""
    return RATE * (max(0.0, ytd_pl + gain) - max(0.0, ytd_pl))


def _sym(h):
    return h.get("symbol") or prices.TICKERS.get(h.get("ticker"), h.get("ticker"))


def switches(items, portfolio, cash, rates, ytd_pl) -> list:
    """[(text, sell symbol, buy symbol)], at most MAX_SWITCHES: for a top BUY pick that free cash cannot buy one
    share of, sell a whole weak holding to fund it. A holding qualifies when it is not an ETF (the core ETFs are
    kept), has a fresh close (a stale one abstains, like its SELL call), scores at least GAP below the pick and is not a winner (score < STRONG, or its call is SELL). Losers come
    first (no tax, and the loss offsets this year's gains), then the lowest score.
    The whole position is sold, so its FIFO cost is the sum of its lots = avg cost x qty. Tax: tax_on (offset by
    this year's realised losses). Fees: the K&H commission of both trades.
    Expected edge, a heuristic and nothing more: each score point of gap is worth about 0.2% of the switched value
    (min of the sale proceeds and the buy), so gap x 0.2% x value must exceed tax + fees."""
    if not portfolio or not portfolio.get("holdings") or cash is None:
        return []
    by = {i["symbol"]: i for i in items}
    cands = []
    for h in portfolio["holdings"]:
        i, q = by.get(_sym(h)), int(h.get("qty") or 0)
        rate = scan.huf_rate(i["ccy"], rates) if i else None
        if not i or not rate or q <= 0 or not h.get("avg_cost_huf") or i["kind"] == "etf" or i.get("stale"):
            continue
        if i["score"] >= STRONG and i["call"] != "SELL":
            continue
        fee = scan._fee(q * i["last"], i["ccy"], rates) * rate
        proceeds = q * i["last"] * rate - fee
        cands.append(dict(i=i, q=q, fee=fee, proceeds=proceeds, gain=proceeds - h["avg_cost_huf"] * q))
    cands.sort(key=lambda c: (c["gain"] >= 0, c["i"]["score"]))
    out = []
    for pick in [i for i in items if i["call"] == "BUY"][:5]:
        if len(out) >= MAX_SWITCHES or not pick.get("one_share_huf") or cash >= pick["one_share_huf"]:
            continue
        for c in cands:
            gap = pick["score"] - c["i"]["score"]
            if gap < GAP:
                continue
            m, cost, _ = scan.size(cash + c["proceeds"], pick["limit"], pick["ccy"], rates)
            if not m:
                continue
            fees = c["fee"] + scan._fee(m * pick["limit"], pick["ccy"], rates) * scan.huf_rate(pick["ccy"], rates)
            tax = max(0.0, tax_on(c["gain"], ytd_pl))  # a loss: no tax now (its saving shows in the note)
            if gap * EDGE_PER_POINT * min(c["proceeds"], cost) <= tax + fees:
                continue
            saved = -tax_on(c["gain"], ytd_pl)
            note = ((f" · the {fmt.approx(-c['gain'])} HUF loss offsets this year's gains"
                     + (f", saving ≈ {fmt.approx(saved)} HUF tax" if saved > 0 else "")) if c["gain"] < 0 else "")
            out.append((f"Sell {c['q']}× {fmt.tick(c['i']['symbol'])} ({c['i']['score']}) → buy {m}× "
                        f"{fmt.tick(pick['symbol'])} ({pick['score']})\n"
                        f"   💸 Tax now ≈ {fmt.approx(tax)} HUF · fees ≈ {fmt.approx(fees)} HUF{note}",
                        c["i"]["symbol"], pick["symbol"]))
            cands.remove(c)
            ytd_pl += c["gain"]
            cash += c["proceeds"] - cost
            break
    return out


def _pl(x) -> str:
    return fmt.signed_huf(x)


def unrealised(log, last, rates) -> list:
    """[(symbol, value HUF, cost HUF)] of the positions held now that have a price (last {symbol: price}) and FX."""
    out = []
    for s, p in state.book(log)["pos"].items():
        rate = scan.huf_rate(p["ccy"], rates)
        if last.get(s) and rate:
            out.append((s, state.qty(p) * last[s] * rate, sum(lot[2] for lot in p["lots"])))
    return out


def _carry_lines(log, year) -> list:
    """The carried-loss line, and the declare reminder when the year (so far) closes with a net loss."""
    pl, c = ytd(log, year), carry(log, year)
    out = [f"📉 Losses from {year - CARRY_YEARS}–{year - 1} offset ≈ {rules._huf(min(c, pl))} HUF (if declared)"
           ] if c and pl > 0 else []
    return out + ([DECLARE] if pl < 0 else [])


def report(log, now, last, rates) -> str:
    """The /tax reply: realised P/L this year, the losses carried in, the estimated tax and the unrealised P/L per
    holding."""
    pl, rows = ytd(log, now.year), unrealised(log, last, rates)
    lines = [f"🧾 Tax {now.year}",
             f"💵 Realised P/L ≈ {_pl(pl)} (FIFO, fees included)", *_carry_lines(log, now.year),
             f"🏛️ Estimated tax ≈ {rules._huf(RATE * max(0.0, base(log, now.year)))} HUF (15% SZJA, no szocho on "
             "exchange trades)"]
    if rows:
        lines.append(f"\n📊 Unrealised P/L ≈ {_pl(sum(v - c for _, v, c in rows))}")
        lines += [f"{'🟢' if v >= c else '🔴'} {fmt.tick(s)} {_pl(v - c)} ({fmt.signed((v / c - 1) * 100)})"
                  for s, v, c in rows]
    else:
        lines.append("📭 No holdings with a price right now.")
    us = us_stocks(state.book(log)["pos"])
    if us:
        lines.append(f"💵 US stocks ({', '.join(fmt.tick(s) for s in us)}): {US_DIVIDENDS}.")
    fx = ("Realised P/L at MNB rates, except trades recorded without one (Yahoo FX, approximate)" if approx(log)
          else "Realised P/L at the official MNB rates of the buy and sell days")
    return "\n".join(lines) + f"\n\nℹ️ {fx}; unrealised at Yahoo FX. A rough guide: check with your tax advisor."


def december(log, now, last, rates):
    """The tax-loss section of the Monday summaries from Dec 1 to Dec 20, or None (other days, no trades)."""
    if now.month != 12 or now.day > 20 or not any(e["t"] in ("buy", "sell") for e in log):
        return None
    pl, taxable = ytd(log, now.year), base(log, now.year)
    lines = [f"🧾 December tax check: realised gains {now.year} ≈ {_pl(pl)}, tax due ≈ "
             f"{rules._huf(RATE * max(0.0, taxable))} HUF (15%).", *_carry_lines(log, now.year)]
    losers = [(s, v - c) for s, v, c in unrealised(log, last, rates) if v < c]
    if losers:
        off = min(-sum(d for _, d in losers), max(0.0, taxable))
        lines.append("🔴 At a loss now: " + ", ".join(f"{fmt.tick(s)} {_pl(d)}" for s, d in losers) + ".")
        lines.append(f"💡 Selling them would offset {rules._huf(off)} HUF of gains and save ≈ {rules._huf(RATE * off)} "
                     "HUF of tax." if off else "No realised gains to offset this year: selling them saves no tax now.")
    else:
        lines.append("🟢 No holding is at a loss.")
    lines.append("⏰ A sale counts for this year only if it settles in the calendar year: act by about Dec 20.")
    lines.append("Hungary has no wash-sale rule: buying the same thing straight back is allowed (check with your tax "
                 "advisor).")
    return "\n".join(lines)
