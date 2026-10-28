"""Cloud runner (GitHub Actions): market scan with live yfinance data, calls sent to Telegram.

    python -m invest_bot.cloud --state state/state.enc [--force] [--portfolio local.json] [--state-file plain.json]

No database. Holdings and cash: the encrypted bot state (entered with Telegram commands, see commands.py) when it
has any; else env PORTFOLIO_JSON (Actions secret, or a local --portfolio file for testing) and env CASH_HUF, else
--budget-huf (env CLOUD_BUDGET_HUF, default 200 000 HUF). The cash buys a BUY PLAN (scan.plan_spread), spread out
among the picks themselves: at most 2 per Yahoo sector (ETFs are one sector) and none whose 6-month daily returns
correlate more than 0.75 with a higher pick; the skipped ones are listed with the reason. The plan is the top 3
picks it can actually buy, in whole shares, value + fee + buy tax + FX conversion within the cash less 1%, no fee
over 1%; the other BUYs are a watchlist. The cash is per currency (HUF, USD, EUR): a pick is paid from its own
currency's balance first, the rest converted from HUF at the 1% spread (the line says which). Without holdings it gives
market calls only (no SELL calls). Holdings and cash are never printed. The scan ranking (for /top and /scan) and
the sent calls go into the state (with the call log that /stats scores, see track.py), which is saved only when
TELEGRAM_TOKEN (its key) is set.
FX is yfinance's (approximate), not MNB. It sends only when the set of (symbol, call) changed since the last
sent message, on the Monday morning run, or with --force. The Monday morning run also sends a short weekly summary
(portfolio value and its change since the last summary, the week's best and worst holding, the /stats line, the
market regime, EUR/HUF and USD/HUF vs the last 3 months; from Dec 1 to Dec 20 also the tax-loss check of
tax.december). Before the message it fetches headlines for the BUY / dip candidates, the holdings and big movers
(news.py): a fresh negative headline turns a BUY into WAIT, and a new big daily move of a holding or top-10 pick
is an alert at the top (and a reason to send). When free cash cannot buy one share of a top BUY pick, a SWITCH
call may suggest selling a weak holding for it, with the tax and fees (tax.py). A holding 15% below its peak since
the first buy or below its average cost is an alert too, once a week per symbol, and so is a core ETF 8% or more
below its 52-week high, again only 5 points deeper or 20 trading days later (watch.py). The token is never printed.
--state-file reads a plaintext state JSON (fake holdings for a dry run): only without TELEGRAM_TOKEN, never saved,
and the message is printed.
The message is plain text with emojis (fmt.py, no parse_mode): a header, a TODAY'S MOVE headline picked from the
calls (a SELL or stop first, then a SWITCH, the top BUY that fits the cash, else why nothing fits or no trade),
then ALERTS, BUY, WAIT, WAIT FOR A DIP, SWITCH, SELL and the holdings. Only completed daily closes are used
(prices.fresh): today's unfinished bar is dropped, and a symbol whose last close is older than its exchange's last
completed session gets no BUY or SELL call and is named in the header. Over 4000 characters it goes out in several
sends, cut at section boundaries.
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from invest_bot import analysis, fees, fmt, news, prices, scan, state, tax, track, watch
from invest_bot.rules import INSTRUMENTS

INDEXES = ("^GSPC", "^STOXX50E")
FX_SYMBOLS = {"EUR": "EURHUF=X", "USD": "USDHUF=X", "GBP": "GBPHUF=X", "DKK": "DKKHUF=X"}
DEFAULT_BUDGET = 200_000
MAX_BUYS = 5  # BUY calls shown: the plan, then the watchlist
MAX_STALE = 8  # stale symbols named in the header
MAX_ALERTS = 200  # (symbol, date) keys of sent big-move alerts
MAX_LEN = fmt.MAX_LEN  # longer messages are split at section boundaries
WORKERS = 8
FOOTER = ("Calls from price trend + fundamentals, not certainty. HUF amounts use approximate Yahoo FX; whole "
          "shares only. You place and confirm every order.")
NO_HOLDINGS = "Market calls only (no holdings data)"
def stop_why() -> str:
    hard = f"{watch.HARD_STOP:.0%} below your cost"
    return f"it fell {watch.STOP:.0%} below its peak with a weakening trend, or {hard}" if watch.STOP else f"it fell {hard}"
# docs/EXPERIMENTS.md: top-3 momentum portfolios had worst drawdowns of -42..-48% (top 5: about -30%)
CONCENTRATED = "⚖️ Risk: a few momentum stocks fell 40-50% at worst in the backtest; a core ETF softens that."
DIST = "💸 pays dividends (taxable)"  # a distributing ETF; the accumulating ones pay nothing out
TELEGRAM = "https://api.telegram.org/bot{token}/sendMessage"
SCANS = ((6, 50), (21, 15))  # the weekday scan slots the listener (listen.py) runs, UTC


class Permanent(RuntimeError):
    """A Telegram 400 or 403 (e.g. message too long, bot blocked): sending it again cannot help."""


def load_portfolio(path=None, env=None):
    """The exported portfolio dict from env PORTFOLIO_JSON, else the file at `path`; None when there is none
    or it is unreadable (the content is never echoed)."""
    raw = (env or {}).get("PORTFOLIO_JSON") or ""
    try:
        if raw.strip():
            data = json.loads(raw)
        elif path and os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            return None
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def fx_rates(histories) -> dict:
    out = {}
    for ccy, sym in FX_SYMBOLS.items():
        h = histories.get(sym)
        try:
            v = float(h["Close"].dropna().iloc[-1])
        except Exception:
            continue
        if v > 0:
            out[ccy] = v
    return out


def _budget(arg, env):
    raw = arg if arg is not None else (env.get("CLOUD_BUDGET_HUF") or "").strip()
    try:
        v = float(raw) if raw not in (None, "") else DEFAULT_BUDGET
    except ValueError:
        v = DEFAULT_BUDGET
    return v if v > 0 else DEFAULT_BUDGET


def cash_env(env):
    """CASH_HUF from the environment (the Actions secret), or None when unset or not a number."""
    raw = (env.get("CASH_HUF") or "").strip()
    try:
        v = float(raw)
    except ValueError:
        return None
    return v if v >= 0 and v == v else None


def cash_and_holdings(portfolio, budget, rates, cash=None):
    """(cash_huf, holdings {symbol: qty}). cash (CASH_HUF) wins over the portfolio's Normál cash; either is
    reduced by the portfolio's pending buys (approximate FX). Neither: the budget. No portfolio: no holdings."""
    if portfolio is None:
        return (budget if cash is None else cash), {}
    holdings = {}
    for h in portfolio.get("holdings") or []:
        sym = h.get("symbol") or prices.TICKERS.get(h.get("ticker"), h.get("ticker"))
        if sym and (h.get("qty") or 0) > 0:
            holdings[sym] = holdings.get(sym, 0) + h["qty"]
    cash = portfolio.get("cash_huf") if cash is None else cash
    if cash is None:
        return budget, holdings
    for o in portfolio.get("pending_orders") or []:
        if o.get("side") != "buy":
            continue
        ccy = INSTRUMENTS.get(o.get("ticker"), (None, "HUF"))[1]
        rate = scan.huf_rate(ccy, rates) or 0
        cash -= (o.get("qty") or 0) * (o.get("limit") or 0) * rate
    return max(0.0, cash), holdings


def fetch(symbols, download_fn=prices.download, info_fn=prices.info):
    """(histories, fundamentals) live from yfinance; a failed symbol is left out / gets empty fundamentals."""
    hists = download_fn(list(symbols), period="2y") or {}

    def one(s):
        try:
            return s, scan.parse_info(info_fn(s))
        except Exception:
            return s, {}
    wanted = [s for s in hists if s not in INDEXES and s not in FX_SYMBOLS.values()]
    with ThreadPoolExecutor(WORKERS) as pool:
        funds = dict(pool.map(one, wanted))
    return hists, funds


def _money(x):
    return fmt.huf(x)


def _label(i):
    return fmt.label(i.get("name"), i["symbol"])


def chips(i) -> list:
    """Plain-English reasons from the item's own parts and flags: momentum, analyst upside, risk flags."""
    out, m, parts = [], i.get("mom_long"), i.get("parts") or {}
    if m is not None:
        months = 6 if i.get("mom_6m") is not None and m == i["mom_6m"] else 12  # 6m: the 12-1 fallback
        out.append(f"📈 {fmt.signed(m * 100, 0)} in {months} months")
    if parts.get("upside_pct") is not None:  # only with >= 5 analysts
        out.append(f"🎯 analysts {fmt.signed(parts['upside_pct'] * 100, 0)} upside")
    return out


def _fit(i) -> str:
    """'✅ 2× ≈ 120 000 HUF' / '❌ 1 share ≈ 302 000 HUF, over budget' / why there is no share count."""
    qty, one = i.get("qty"), i.get("one_share_huf")
    if one is None:
        return "❔ no FX rate, so no share count"
    if qty is None:
        return f"❔ cash unknown · 1 share ≈ {_money(one)}"
    if qty == 0:
        return f"❌ 1 share ≈ {_money(one)}, over budget"
    return f"✅ {'Buy ' if i['call'] == 'BUY' else ''}{qty}× ≈ {_money(i['approx_huf'])}"


def plan_lines(i, now, n) -> list:
    """A BUY PLAN line: 'Buy N× X at ≤ limit ≈ value HUF + fee F HUF (f%)[ + FR tax 0.4% ≈ T HUF]', then score,
    chips, deadline, flags. 🆕 marks a pick the last sent message did not have (i['new'], set in run)."""
    out = [f"{fmt.NUMBERS[n - 1]} {'🆕 ' if i.get('new') else ''}Buy {i['qty']}× {_label(i)} at ≤ {fmt.price(i['limit'], i['ccy'])} ≈ "
           f"{_money(i['value_huf'])} + fee {_money(i['fee_huf'])} ({i['fee_pct'] * 100:.2f}%)"
           + (f" + {fees.tax_label(i['symbol'])} ≈ {_money(i['tax_huf'])}" if i.get("tax_huf") else ""),
           "   " + " · ".join([fmt.scored(i["score"], "/100"), *chips(i),
                               f"⏰ valid till {fmt.day(i['valid_until'], now.date())}"])]
    if pay := paid_line(i):
        out.insert(1, f"   {pay}")
    # the live exits (docs/EXPERIMENTS.md): hold while the trend is up, SELL when it turns bearish, the hard stop
    out.append(f"   🛑 exit plan: hold while the trend is up · SELL call when it turns bearish, or below "
               f"{fmt.price(round(i['limit'] * (1 - watch.HARD_STOP), 2), i['ccy'])} (−{watch.HARD_STOP:.0%})")
    out += [f"   {fmt.flag(f)}" for f in i.get("flags") or []]
    if i.get("dist"):
        out.append(f"   {DIST}")
    return out


def paid_line(i) -> str:
    """How a plan pick is paid (scan.pay): '💵 pay from your USD balance ($905.50)', '💱 convert ≈ X HUF → USD (1% ≈
    Y HUF)', or both; '' for a HUF pick."""
    w, own, conv = i.get("wallet_ccy"), i.get("own") or 0.0, i.get("convert_huf") or 0.0
    if not w or w == "HUF":
        return ""
    convert = f"convert ≈ {_money(conv)} → {w} (1% ≈ {_money(i.get('fx_cost_huf') or 0.0)})"
    if own and conv:
        return f"💱 {fmt.amount(own, w)} from your {w} balance + {convert}"
    return f"💵 pay from your {w} balance ({fmt.amount(own, w)})" if own else f"💱 {convert}"


def buy_lines(i, now, n=None) -> list:
    head = f"{fmt.NUMBERS[n - 1] if n else '•'} {_label(i)} · {fmt.scored(i['score'], '/100')}"
    out = [head]
    if c := chips(i):
        out.append("   " + " · ".join(c))
    if i.get("limit"):
        out.append(f"   💵 ≤ {fmt.price(i['limit'], i['ccy'])} · ⏰ valid till {fmt.day(i['valid_until'], now.date())}")
    out.append(f"   {_fit(i)}")
    out += [f"   {fmt.flag(f)}" for f in i.get("flags") or []]
    if i.get("dist"):
        out.append(f"   {DIST}")
    return out


def todays_move(buys, sells, stops, switches, watch=()) -> str:
    """One headline from the calls, by priority: a SELL or stop on a holding, a SWITCH, the BUY PLAN (buys: its
    picks), the top watchlist pick when nothing fits (deposit or wait), else no trade."""
    if sells or stops:
        more = len(sells) + len(stops) - 1
        tail = f" (+{more} more below)" if more > 0 else ""
        if sells:
            i = sells[0]
            amount = f" ≈ {_money(i['approx_huf'])}" if i.get("approx_huf") else ""
            return (f"🔴 Sell {i['qty']}× {_label(i)} at ≥ {fmt.price(i['limit'], i['ccy'])}{amount}{tail}\n"
                    "Why: its price trend turned bearish.")
        return f"🛑 Sell {fmt.tick(stops[0])} — it hit a stop{tail}\nWhy: {stop_why()} — see 🚨 ALERTS."
    if switches:
        text, _, y = switches[0]
        return (f"🔁 {text.splitlines()[0]}\nWhy: {fmt.tick(y)} scores far higher and your cash alone "
                "cannot buy one share.")
    if buys:
        return ("🟢 Buy the plan: " + ", ".join(f"{i['qty']}× {fmt.tick(i['symbol'])}" for i in buys)
                + f" ≈ {_money(sum(i['approx_huf'] for i in buys))}\n"
                "Why: the top-ranked picks your cash can buy, fees included.")
    if watch:
        top = watch[0]
        need = f" needs ≈ {_money(top['one_share_huf'])}" if top.get("one_share_huf") else " has no share count"
        return f"🤷 Nothing fits your cash — top pick {_label(top)}{need}. Deposit or wait."
    return "No good setups today — holding is fine. ✋"


def holding_lines(items, portfolio, rates) -> list:
    """One line per holding: P/L % since the average cost (HUF, approximate FX), score and call."""
    by = {i["symbol"]: i for i in items}
    out = []
    for h in (portfolio or {}).get("holdings") or []:
        sym = h.get("symbol") or prices.TICKERS.get(h.get("ticker"), h.get("ticker"))
        i = by.get(sym)
        if not i:
            out.append(f"⚪ {fmt.tick(sym)} · no price data")
            continue
        rate, avg = scan.huf_rate(i["ccy"], rates), h.get("avg_cost_huf")
        pl = (i["last"] * rate / avg - 1) * 100 if rate and avg else None
        dot = "⚪" if pl is None else "🟢" if pl >= 0 else "🔴"
        call = (i["call"] or "HOLD").lower()
        out.append(f"{dot} {fmt.tick(sym)} {'P/L n/a' if pl is None else fmt.signed(pl)} · score {i['score']} · "
                   + ("🔴 sell" if call == "sell" else call) + (" · ⚠️ stale data" if i.get("stale") else "")
                   + (f"\n   📰 negative news: '{i['news']}'" if i.get("news") else ""))
    return out


def message(items, regime, now, portfolio, cash_huf, has_cash=False, rates=None, alerts=(), switches=(),
            stops=(), wallet=None) -> str:
    """The scan message (split it with fmt.split before sending). stops: symbols with a sell-type stop alert.
    wallet: the cash per currency; with a foreign balance the header shows each and the HUF total."""
    buys, watch = shown(items)
    waits = [i for i in items if i["call"] == "WAIT"][:MAX_BUYS]
    sells = [i for i in items if i["call"] == "SELL"]
    dips = [i for i in items if i["call"] == "WAIT_FOR_DIP"]
    head = [f"📊 Market scan · {fmt.stamp(now)}", fmt.regime(regime)]
    known = has_cash or (portfolio and portfolio.get("cash_huf") is not None)
    if known and wallet and any(c != "HUF" and abs(v) >= 0.005 for c, v in wallet.items()):
        head.append(f"💰 Cash ≈ {fmt.cash(wallet)} ≈ {_money(cash_huf)} in total")
    else:
        head.append(f"💰 Cash ≈ {_money(cash_huf)}" if known else f"💰 Budget {_money(cash_huf)} (no cash data)")
    old = [fmt.tick(i["symbol"]) for i in items if i.get("stale")]
    if old:
        head.append(f"⚠️ {len(old)} without a fresh close (no calls): " + ", ".join(old[:MAX_STALE])
                    + (", …" if len(old) > MAX_STALE else ""))
    if portfolio is None:
        head.append(f"📭 {NO_HOLDINGS}")
    else:
        head.append(f"🗂️ Holdings as of {str(portfolio.get('as_of') or '?')[:16].replace('T', ' ')}")
    parts = ["\n".join(head), "🎯 TODAY'S MOVE\n" + todays_move(buys, sells, stops, switches, watch)]
    if alerts:
        parts.append("🚨 ALERTS\n" + "\n".join(alerts))
    if buys or watch:
        total = sum(i["approx_huf"] for i in buys)
        parts.append("\n".join(
            [*(["🟢 BUY PLAN"] + [line for n, i in enumerate(buys, 1) for line in plan_lines(i, now, n)]
               + [f"🧮 Total ≈ {_money(total)} of your {_money(cash_huf)} · left ≈ {_money(cash_huf - total)}"]
               + ([CONCENTRATED] if sum(i.get("kind") != "etf" for i in buys) >= 2 else [])
               if buys else [f"🟢 BUY: nothing fits {'your cash' if known else 'the budget'} "
                             f"({_money(cash_huf)}), fees included"]),
             *([f"👀 Watchlist: " + " · ".join(f"{_label(i)} {i['score']}" for i in watch)] if watch else [])]))
    if skipped := [i for i in items if i.get("skipped")]:
        parts.append("↪️ SKIPPED (keeps the picks spread out)\n" + "\n".join(
            f"• {_label(i)} · {i['score']}/100 — {i['skipped']}" for i in skipped))
    if waits:
        parts.append("⏳ WAIT (would be BUY)\n" + "\n".join(
            f"• {_label(i)} · {i['score']}/100 — {_wait_why(i)}" for i in waits))
    if dips:
        parts.append("🪤 WAIT FOR A DIP (overheated: buy lower)\n" + "\n".join(
            "\n".join(buy_lines(i, now)) for i in dips) + "\nNo dip by the deadline? Buy anyway.")
    if switches:
        parts.append("🔁 SWITCH (tax-aware, rough estimate)\n" + "\n".join(t for t, _, _ in switches))
    if sells:
        parts.append("🔴 SELL\n" + "\n".join("\n".join(sell_lines(i, now)) for i in sells))
    if portfolio is not None and portfolio.get("holdings"):
        parts.append("💼 YOUR HOLDINGS\n" + "\n".join(holding_lines(items, portfolio, rates or {})))
    parts.append(f"ℹ️ {FOOTER}")
    return "\n\n".join(parts)


def sell_lines(i, now) -> list:
    amount = f" ≈ {_money(i['approx_huf'])}" if i.get("approx_huf") else ""
    return [f"• {_label(i)} · {i['score']}/100",
            f"   📉 trend turned bearish: sell {i['qty']}× at ≥ {fmt.price(i['limit'], i['ccy'])}{amount}",
            f"   ⏰ valid till {fmt.day(i['valid_until'], now.date())}",
            *(f"   {fmt.flag(f)}" for f in i.get("flags") or [])]


def _wait_why(i) -> str:
    """Why a BUY is held back: the earnings or news part of its reason, without the score text."""
    reason, why = i.get("reason") or "", i.get("why") or ""
    if why and reason.endswith(why) and reason != why:
        return reason[:-len(why)].strip()  # news: "Fresh negative news: '...'. Wait for clarity."
    return why or reason  # earnings: "Earnings in 2 days — wait for the report."


def shown(items) -> tuple:
    """(the BUY PLAN picks, the watchlist: the other BUY calls), together at most MAX_BUYS."""
    picks = [i for i in items if i.get("plan") and i["call"] == "BUY"]
    return picks, [i for i in items if i["call"] == "BUY" and not i.get("plan")][:MAX_BUYS - len(picks)]


def calls(items, switches=()) -> list:
    """The (symbol, call) pairs the message shows: dedupe key. A switch is ["X>Y", "SWITCH"] (see track.py)."""
    buys = sum(shown(items), [])
    waits = [i for i in items if i["call"] == "WAIT"][:MAX_BUYS]
    rest = [i for i in items if i["call"] in ("SELL", "WAIT_FOR_DIP")]
    return sorted([[i["symbol"], i["call"]] for i in buys + waits + rest]
                  + [[f"{x}>{y}", "SWITCH"] for _, x, y in switches])


def is_weekly(now) -> bool:
    return now.weekday() == 0 and now.hour < 12  # Monday's morning run


def should_send(current, state, now, force=False) -> bool:
    return force or is_weekly(now) or current != state.get("calls")


def _week(hist):
    """The close-to-close return over the last 5 sessions, or None."""
    try:
        c = hist["Close"].dropna()
        return float(c.iloc[-1] / c.iloc[-6] - 1) if len(c) > 5 else None
    except Exception:
        return None


def regime_line(regime) -> str:
    return fmt.regime(regime)


def weekly_summary(st, items, regime, holdings, cash_huf, rates, hists, now):
    """(text, new st['weekly'] or None) of the Monday summary. holdings {symbol: qty}; cash_huf None when unknown.
    st['weekly'] = {at, value}, value = holdings + known cash in HUF, stored once the summary is sent; the change
    leaves out the deposits and withdrawals entered since the last summary. No holdings: no portfolio part."""
    new = None
    lines = [f"🗓️ Weekly summary · {fmt.stamp(now)}"]
    by = {i["symbol"]: i for i in items}
    if holdings:
        value, missing = 0.0, 0
        for s, q in holdings.items():
            i = by.get(s)
            rate = scan.huf_rate(i["ccy"], rates) if i else None
            if rate:
                value += q * i["last"] * rate
            else:
                missing += 1
        value += cash_huf or 0
        what = "holdings + cash" if cash_huf is not None else "holdings, cash unknown"
        line = f"💼 Portfolio ≈ {_money(value)} ({what}{f', {missing} without price/FX' if missing else ''})"
        last = st.get("weekly")
        if last and last.get("value"):
            flows = sum((1 if e["t"] == "deposit" else -1) * state.total_huf(dict([state.flow(e)]), rates)
                        for e in st.get("log") or []
                        if e["t"] in ("deposit", "withdraw") and e.get("at", "") > last["at"])
            ch = value - last["value"] - flows
            line += (f"\n{'📈' if ch >= 0 else '📉'} {fmt.signed_huf(ch)} ({fmt.signed(ch / last['value'] * 100)}) "
                     f"since {fmt.day(last['at'])}" + (" (excl. deposits/withdrawals)" if flows else ""))
        else:
            line += "\n🆕 First summary: no change to compare yet"
        lines.append(line)
        new = dict(at=now.isoformat(timespec="seconds"), value=round(value))
        moves = sorted((m, s) for s in holdings if (m := _week(hists.get(s))) is not None)
        if len(moves) == 1:
            lines.append(f"📅 This week: {fmt.tick(moves[0][1])} {fmt.signed(moves[0][0] * 100)}")
        elif moves:
            lines.append(f"🏆 Best this week: {fmt.tick(moves[-1][1])} {fmt.signed(moves[-1][0] * 100)} · "
                         f"🐢 worst: {fmt.tick(moves[0][1])} {fmt.signed(moves[0][0] * 100)}")
    lines.append("🎯 " + track.one_liner(st))
    lines.append(regime_line(regime))
    lines += [f"💱 {line}" for line in watch.fx_lines(hists)]
    dec = tax.december(st.get("log") or [], now, {i["symbol"]: i["last"] for i in items}, rates)
    if dec:
        lines.append("\n" + dec)
    return "\n".join(lines), new


def send_telegram(token, chat_id, text, opener=urllib.request.urlopen):
    """POST to the Bot API. Errors are re-raised without the token (it is part of the URL)."""
    data = urllib.parse.urlencode(dict(chat_id=chat_id, text=text, disable_web_page_preview="true")).encode()
    req = urllib.request.Request(TELEGRAM.format(token=token), data=data, method="POST")
    try:
        with opener(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:
        kind = Permanent if getattr(e, "code", None) in (400, 403) else RuntimeError
        raise kind(f"Telegram send failed: {type(e).__name__}: {str(e).replace(token, '***')}") from None
    if not body.get("ok"):
        raise RuntimeError(f"Telegram send failed: {str(body.get('description'))[:200].replace(token, '***')}")


def next_scan(now) -> datetime:
    """The next scheduled scan after `now` (weekdays, SCANS)."""
    for d in range(8):
        day = (now + timedelta(days=d)).date()
        for h, m in SCANS if day.weekday() < 5 else ():
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
            if t > now:
                return t


def fail(reason, token, chat, now, send_fn, out) -> int:
    """An aborted scan: logged, and a short Telegram note when the secrets are set (best effort). Returns 1."""
    print(reason, file=out)
    if token and chat:
        try:
            for chunk in fmt.split(f"⚠️ Scan failed: {reason}. Next try {fmt.stamp(next_scan(now))}."):
                send_fn(token, chat, chunk)
        except Exception as e:
            print(str(e).replace(token, "***"), file=out)
    return 1


def run(args, env=None, now=None, download_fn=prices.download, info_fn=prices.info, send_fn=send_telegram,
        out=sys.stdout, news_fn=prices.news) -> int:
    env = os.environ if env is None else env
    now = now or datetime.now(timezone.utc)
    token, chat = (env.get("TELEGRAM_TOKEN") or "").strip(), (env.get("TELEGRAM_CHAT_ID") or "").strip()
    plain = getattr(args, "state_file", None)
    if plain and token:
        print("--state-file is a plaintext test input: only allowed without TELEGRAM_TOKEN", file=out)
        return 2
    try:
        if plain:
            with open(plain, encoding="utf-8") as f:
                st = dict(state.empty(), **json.load(f))
        else:
            st = state.load(args.state, token) if token else state.empty()
    except RuntimeError as e:  # the state branch has a state but this checkout has none: never start empty
        return fail(str(e), token, chat, now, send_fn, out)
    mine = state.portfolio(st)
    portfolio = mine or load_portfolio(args.portfolio, env)
    budget = _budget(args.budget_huf, env)
    held = []
    if portfolio:
        held = [h.get("symbol") or prices.TICKERS.get(h.get("ticker"), h.get("ticker"))
                for h in portfolio.get("holdings") or []]
    symbols = list(dict.fromkeys([*scan.UNIVERSE, *(s for s in held if s), *INDEXES, *FX_SYMBOLS.values()]))
    try:
        hists, funds = fetch(symbols, download_fn, info_fn)
    except Exception as e:
        return fail(f"price download failed ({type(e).__name__})", token, chat, now, send_fn, out)
    hists, stale = prices.fresh(hists, now)
    rates = fx_rates(hists)
    if mine:  # the cash per currency, valued in HUF at today's rates
        portfolio = mine = state.portfolio(st, rates)
    secret_cash = cash_env(env) if not mine or mine["cash_huf"] is None else None
    cash, holdings = cash_and_holdings(portfolio, budget, rates, secret_cash)
    # the cash per currency for the plan: the state's balances, else CASH_HUF / the portfolio / the budget as HUF
    wallet = mine["cash"] if mine and mine.get("cash") is not None and secret_cash is None else {"HUF": cash}
    scanned = {s: h for s, h in hists.items() if s not in INDEXES and s not in FX_SYMBOLS.values()}
    if len(scanned) < len(scan.UNIVERSE) // 2:  # a yfinance outage must not look like "no calls today"
        return fail(f"only {len(scanned)} of {len(scan.UNIVERSE)} symbols had prices (data outage?)", token, chat,
                    now, send_fn, out)
    items = scan.run_scan(scanned, funds, now, cash, holdings, rates, stale=stale)
    regime = analysis.market_regime(hists)
    seen = st.get("alerts") or []
    fresh = [m for m in news.movers(scanned, holdings)
             if f"{m[0]}|{m[2]}" not in seen]
    started = time.monotonic()
    headlines = news.fetch([*(i["symbol"] for i in items if i["call"] in ("BUY", "WAIT_FOR_DIP")), *holdings,
                            *(m[0] for m in fresh)], news_fn)
    print(f"news for {len(headlines)} symbols in {time.monotonic() - started:.1f}s; {len(fresh)} new alert(s)",
          file=out)
    news.apply(items, headlines, now)
    scan.plan_spread(items, scanned, cash, rates, wallet=wallet)
    if st["sent"]:  # 🆕 on the picks the last sent message did not have
        for i in items:
            i["new"] = [i["symbol"], i["call"]] not in st["sent"][-1]["calls"]
    known = cash if secret_cash is not None or (portfolio or {}).get("cash_huf") is not None else None
    switches = tax.switches(items, portfolio, known, rates, tax.base(st["log"], now.year))
    dipped, dips = watch.dips(scanned, now, st.get("dips") or {})
    watched = watch.stops(portfolio, st["log"], scanned, items, rates, now, seen) + dipped
    stops = [k.split("|")[1] for k, line in watched if k.startswith("stop|") and "consider selling" in line]
    text = message(items, regime, now, portfolio, cash, has_cash=secret_cash is not None, rates=rates,
                   alerts=news.alert_lines(fresh, headlines, now) + [line for _, line in watched], switches=switches,
                   stops=stops, wallet=wallet)
    current = calls(items, switches)
    track.score(st, now.date(), hists)
    track.shadow(st, items, now.date(), hists)
    summary = weekly = None
    if is_weekly(now):
        summary, weekly = weekly_summary(st, items, regime, holdings, known, rates, hists, now)
    st["scan"] = dict(at=now.isoformat(timespec="seconds"),
                      items=[[i["symbol"], i["name"], i["score"], i["call"]] for i in items],
                      moms=dict(long=[i["mom_long"] for i in items], mom_6m=[i["mom_6m"] for i in items]))
    print(f"scanned {len(scanned)} of {len(symbols) - len(INDEXES) - len(FX_SYMBOLS)} symbols; "
          "calls: " + ", ".join(f"{sum(c[1] == k for c in current)} {k}" for k in ("BUY", "WAIT", "WAIT_FOR_DIP", "SELL"))
          + f", {len(switches)} switch", file=out)  # counts only: symbols could reveal holdings
    if not should_send(current, dict(calls=st["sent"][-1]["calls"] if st["sent"] else None), now,
                       args.force or bool(fresh) or bool(watched)):  # a new alert is a change too
        st["dips"] = dips  # no dip alert here (it would force a send): only ETFs back above -8% are dropped
        if token:
            state.save(st, args.state, token)
        print("calls unchanged: no message", file=out)
        return 0
    if not token or not chat:
        if (mine or secret_cash is not None or env.get("PORTFOLIO_JSON")) and not plain:  # never show your account
            print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set: message not sent (not printed: it shows your account)",
                  file=out)
        else:
            print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set: message printed only\n\n" + text
                  + (f"\n\n--- weekly summary ---\n\n{summary}" if summary else ""), file=out)
        return 0
    code = 0
    try:
        for chunk in fmt.split(text):
            try:
                send_fn(token, chat, chunk)
            except Permanent as e:  # retrying next run cannot help: dropped, the calls count as sent
                print(str(e).replace(token, "***") + " (permanent: dropped)", file=out)
                code = 1
    except Exception as e:
        print(str(e).replace(token, "***"), file=out)
        return 1
    st["sent"] = [*st["sent"], dict(calls=current, at=now.isoformat(timespec="seconds"))][-state.MAX_SENT:]
    track.log(st, current, now.date(), hists, by={i["symbol"]: i for i in items})
    st["alerts"] = [*seen, *(f"{s}|{d}" for s, _, d in fresh), *(k for k, _ in watched if k.startswith("stop|"))
                    ][-MAX_ALERTS:]
    st["dips"] = dips
    if summary:
        try:
            for chunk in fmt.split(summary):
                send_fn(token, chat, chunk)
            if weekly:
                st["weekly"] = weekly
        except Exception as e:
            print("weekly summary: " + str(e).replace(token, "***"), file=out)
            code = 1
    state.save(st, args.state, token)
    print("message sent" + (" with the weekly summary" if summary and not code else ""), file=out)
    return code


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="python -m invest_bot.cloud", description=__doc__.splitlines()[0])
    p.add_argument("--portfolio", default=None, help="local holdings export for testing (env PORTFOLIO_JSON wins)")
    p.add_argument("--state", default=state.PATH, help="the encrypted bot state (holdings, last sent calls)")
    p.add_argument("--budget-huf", default=None, help="cash to size BUY calls without a portfolio "
                   f"(env CLOUD_BUDGET_HUF, default {DEFAULT_BUDGET})")
    p.add_argument("--force", action="store_true", help="send even if the calls did not change")
    p.add_argument("--state-file", default=None, help="plaintext state JSON with test holdings for a dry run "
                   "(only without TELEGRAM_TOKEN; printed, never saved)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
