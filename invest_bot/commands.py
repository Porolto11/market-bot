"""Telegram commands: read new messages with getUpdates, record trades and cash in the encrypted state, reply.

    python -m invest_bot.commands      (env TELEGRAM_TOKEN, TELEGRAM_CHAT_ID; one pass, for manual and debug runs:
                                        the always-on listener, listen.py, calls step() in its loop)

Only private-chat messages from TELEGRAM_CHAT_ID (chat and sender) are handled; anything else is skipped silently.
The getUpdates offset lives in the state, so a command is handled once. A reply that cannot be sent waits in the
state's outbox and goes out first on the next run, unless Telegram refused it for good (400 / 403: logged and
dropped). Cash is per currency (HUF, USD, EUR; state.book): a buy pays from its own currency first and the
shortfall is converted from HUF at 1% (an fx entry, undone with the buy); a sale credits its own currency. On a
weekday with no market scan for over 26 weekday hours it warns once a day (precheck.overdue). Prices are in the instrument's currency; HUF values use
approximate Yahoo FX. Holdings and amounts are never printed to the log.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from invest_bot import cloud, fees, fmt, mnb, precheck, prices, rules, scan, state, tax, track

UPDATES = "https://api.telegram.org/bot{token}/getUpdates?{query}"
TOP = 10
SCAN_EVERY = timedelta(minutes=10)  # /refresh at most this often
HELP = """🤖 Invest Bot commands

📝 Record
🟢 /bought SYM QTY PRICE — a buy, whole shares (cash goes down by value + K&H fee + buy tax)
🔴 /sold SYM QTY PRICE — a sale (FIFO P/L kept for tax)
💰 /deposit AMOUNT [HUF|USD|EUR] · /withdraw AMOUNT [CCY] — change cash (write 120000, not 120.000)
🏦 /cash AMOUNT [HUF|USD|EUR] — set that currency's cash to your real K&H balance (no currency: HUF)
↩️ /undo — remove the last trade or cash entry

🔎 Look
💼 /holdings — positions, P/L, total in HUF
🏆 /top — top 10 of the last scan
🔄 /refresh — run a fresh full scan now (the message with your buy plan, about 2 minutes)
🔍 /scan SYM — live analysis of one symbol
📊 /stats — how past calls did vs CSPX
🧾 /tax — realised P/L, estimated 15% tax (MNB rates)

ℹ️ Prices in the instrument's currency (comma or dot); HUF in whole forints.
SYM: a bot ticker (MOL, CSPX, SAP, OTP) or a Yahoo symbol (MOL.BD, NVDA, SAP.DE)."""
RESET = "State was reset (it could not be decrypted, e.g. the bot token changed): please re-enter holdings and cash."
DELAYED = "(delayed reply) "
USAGE = {"bought": "/bought SYM QTY PRICE", "sold": "/sold SYM QTY PRICE", "deposit": "/deposit AMOUNT [HUF|USD|EUR]",
         "withdraw": "/withdraw AMOUNT [HUF|USD|EUR]", "cash": "/cash AMOUNT [HUF|USD|EUR]", "scan": "/scan SYM"}
MONEY = ("deposit", "withdraw", "cash")  # AMOUNT and an optional currency (default HUF)
ARGS = {"bought": 3, "sold": 3, "deposit": 1, "withdraw": 1, "cash": 1, "scan": 1,
        "holdings": 0, "top": 0, "refresh": 0, "stats": 0, "tax": 0, "undo": 0, "help": 0, "start": 0}
CALL = {"BUY": "🟢 BUY", "WAIT": "⏳ WAIT", "WAIT_FOR_DIP": "🪤 WAIT FOR DIP", "HOLD": "✋ HOLD", "SELL": "🔴 SELL",
        "AVOID": "⛔ AVOID"}
THOUSANDS = re.compile(r"[.,]\d{3}(?!\d)")  # 120.000 or 1,500: a thousands separator, or a decimal?


def num(s) -> float:
    v = float(s.replace(",", "."))
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError(s)
    return v


def parse(text):
    """(command, args). Raises ValueError with a hint for an unknown command or bad arguments."""
    words = text.split()
    cmd = words[0].lower().lstrip("/").split("@")[0] if words and words[0].startswith("/") else ""
    if cmd not in ARGS:
        raise ValueError("Unknown command. Send /help for the list.")
    args = words[1:]
    if len(args) != ARGS[cmd] and not (cmd in MONEY and len(args) == 2):
        raise ValueError(f"Usage: {USAGE.get(cmd, '/' + cmd)}")
    if cmd in MONEY and THOUSANDS.search(args[0]):
        raise ValueError(f"Ambiguous amount {args[0]}: write it without thousands separators, e.g. 120000.")
    if cmd in MONEY and len(args) == 2 and args[1].upper() not in state.HELD:
        raise ValueError(f"Usage: {USAGE[cmd]} (cash currencies: {', '.join(state.HELD)})")
    try:
        if cmd in ("bought", "sold"):
            q, px = num(args[1]), num(args[2])
            if q <= 0 or q != int(q) or px <= 0:
                raise ValueError
            return cmd, (args[0].upper(), int(q), px)
        if cmd in MONEY:
            v = num(args[0])
            if v < 0 or (v == 0 and cmd != "cash"):
                raise ValueError
            return cmd, (v, args[1].upper() if len(args) == 2 else "HUF")
    except ValueError:
        raise ValueError(f"Usage: {USAGE[cmd]} (QTY a whole number, amounts positive)") from None
    return cmd, tuple(a.upper() for a in args)


def _huf(x) -> str:
    return f"{rules._huf(x)} HUF"


def _cash(cash, rates=None) -> str:
    """'Cash ≈ 120 000 HUF · $500.00 (≈ 295 000 HUF in total).' cash: {currency: balance} or None; the total only
    with rates and a foreign balance."""
    if cash is None:
        return "Cash not set yet: send /cash AMOUNT [HUF|USD|EUR]."
    foreign = any(c != "HUF" and abs(v) >= 0.005 for c, v in cash.items())
    total = f" (≈ {fmt.huf(state.total_huf(cash, rates))} in total)" if rates and foreign else ""
    return (f"Cash ≈ {fmt.cash(cash)}{total}."
            + (" Warning: cash is below 0, check it with /cash." if any(v < -0.005 for v in cash.values()) else ""))


def resolve(sym, pos, download_fn):
    """(yfinance symbol, from_yahoo). Held, in the scan universe, a bot ticker (MOL -> MOL.BD), a bare ticker of
    one held or universe symbol (SAP -> SAP.DE), else whatever yfinance knows (from_yahoo True); (None, False)."""
    if sym in pos or sym in scan.UNIVERSE:
        return sym, False
    if sym in rules.INSTRUMENTS:
        return prices.TICKERS[sym], False
    hits = [s for s in dict.fromkeys([*pos, *scan.UNIVERSE]) if s.split(".")[0] == sym]
    if len(hits) == 1:
        return hits[0], False
    try:
        found = (download_fn([sym], period="5d") or {}).get(sym) is not None
    except Exception:
        found = False
    return (sym, True) if found else (None, False)


def currency(sym, pos, info_fn):
    if sym in pos:
        return pos[sym]["ccy"]
    if sym in scan.UNIVERSE or sym in scan.BOT_TICKER:
        return scan.default_ccy(sym)
    try:
        return info_fn(sym).get("currency") or scan.default_ccy(sym)
    except Exception:
        return scan.default_ccy(sym)


def kind(sym, info_fn) -> str:
    """'etf' for an ETF / ETC (no buy-side tax), else 'stock': the scan universe's kind, else Yahoo's quote type."""
    if sym in scan.UNIVERSE:
        return scan.UNIVERSE[sym][1]
    try:
        return "etf" if (info_fn(sym) or {}).get("quoteType") in ("ETF", "MUTUALFUND") else "stock"
    except Exception:
        return "stock"


def _rates(download_fn, extra=(), period="5d"):
    """(histories, FX rates) for the extra symbols plus the FX pairs, in one download."""
    try:
        hists = download_fn([*extra, *cloud.FX_SYMBOLS.values()], period=period) or {}
    except Exception:
        hists = {}
    return hists, cloud.fx_rates(hists)


def _scores(st):
    return {s: (score, call) for s, _, score, call in (st.get("scan") or {}).get("items", [])}


def trade(st, side, sym, q, px, now, download_fn, info_fn, mnb_fn=mnb.fetch) -> str:
    b = state.book(st["log"])
    s, from_yahoo = resolve(sym, b["pos"], download_fn)
    if s is None:
        return f"Unknown symbol {sym}. Use a bot ticker (MOL, CSPX) or the Yahoo symbol (MOL.BD, NVDA, SAP.DE)."
    held = state.qty(b["pos"][s]) if s in b["pos"] else 0
    if side == "sell" and q > held:
        return f"You hold {held} {s}, cannot sell {q}."
    ccy = currency(s, b["pos"], info_fn)
    rates = _rates(download_fn)[1] if ccy != "HUF" else {}
    fx = scan.huf_rate(ccy, rates)
    if fx is None:
        return f"No {ccy}/HUF rate from Yahoo right now: nothing recorded, try again later."
    fee = scan._fee(q * px, ccy, rates, s)
    e = dict(t=side, sym=s, qty=q, price=px, ccy=ccy, fx=fx, fee=round(fee, 4), at=now.isoformat(timespec="seconds"))
    levy = fees.buy_tax(s, kind(s, info_fn)) if side == "buy" else 0.0
    if levy:
        e["tax"] = round(q * px * levy, 4)
    note = ""
    if ccy != "HUF":  # the tax figures use the official MNB rate of the trade day
        day = fmt.local(now).date()
        e["fx_mnb"] = mnb.rate(ccy, day, st.setdefault("mnb", {}), day, mnb_fn)
        note = (f" MNB rate {rules._px(e['fx_mnb'])} HUF/{ccy} for tax." if e["fx_mnb"] else
                " No MNB rate yet: tax uses Yahoo FX (approx) until /tax finds it.")
    huf = (q * px + (fee + e.get("tax", 0.0) if side == "buy" else -fee)) * fx
    conv = None
    if b["cash"] is not None:  # cash known: the trade pays from / into its own currency's balance
        e["wallet"] = True
        w, k = state.wallet_ccy(ccy)
        short = (q * px + fee + e.get("tax", 0.0)) * k - max(0.0, b["cash"].get(w, 0.0))
        if side == "buy" and w != "HUF" and short > 1e-9:  # the shortfall is converted from HUF at FX x 1.01
            wfx = scan.huf_rate(w, rates)
            if wfx is None:
                return f"No {w}/HUF rate from Yahoo right now: nothing recorded, try again later."
            conv = dict(t="fx", ccy=w, amt=round(short, 4), huf=round(short * wfx * (1 + rules.FX_SPREAD), 2),
                        auto=True, at=e["at"])
            st["log"].append(conv)
    st["log"].append(e)
    after = state.book(st["log"])
    word = "Bought" if side == "buy" else "Sold"
    text = (f"Recorded in {ccy} — /undo if wrong. " if from_yahoo else "") + (
        f"{word} {q} {s} at {rules._px(px)} {ccy} (fee {fee:.2f} {ccy}"
        + (f" + {fees.tax_label(s)} {e['tax']:.2f} {ccy}" if levy else "") + "), "
        f"{'cost' if side == 'buy' else 'proceeds'} ≈ {_huf(huf)}.")
    if side == "sell":
        e["pl_huf"] = round(after["realised"][-1])
        text += f" Realised P/L ≈ {e['pl_huf']:+,.0f} HUF (FIFO).".replace(",", " ")
    if conv:
        text += (f" Converted ≈ {_huf(conv['huf'])} → {fmt.amount(conv['amt'], conv['ccy'])} "
                 f"(1% ≈ {_huf(conv['huf'] - conv['huf'] / (1 + rules.FX_SPREAD))}).")
    return text + " " + _cash(after["cash"], rates) + note


def holdings(st, now, download_fn) -> str:
    b = state.book(st["log"])
    if not b["pos"]:
        return "📭 No holdings recorded. Use /bought SYM QTY PRICE. " + _cash(b["cash"])
    hists, rates = _rates(download_fn, list(b["pos"]))
    scores, lines, total, missing = _scores(st), [], 0.0, 0
    for s, p in b["pos"].items():
        q = state.qty(p)
        cost = sum(lot[2] for lot in p["lots"])
        avg = sum(lot[0] * lot[1] for lot in p["lots"]) / q
        line = f"{fmt.tick(s)} · {q} @ {fmt.price(avg, p['ccy'])}"
        try:
            last = float(hists[s]["Close"].dropna().iloc[-1])
        except Exception:
            last = None
        rate = scan.huf_rate(p["ccy"], rates)
        dot = "⚪"
        if last is None:
            line += " · no price"
        else:
            line += f" · now {fmt.price(last, p['ccy'])}"
            if rate:
                pl = (q * last * rate / cost - 1) * 100
                dot = "🟢" if pl >= 0 else "🔴"
                line += f" · {fmt.signed(pl)}"
            else:
                line += " · no FX rate"
        if last is None or not rate:
            missing += 1
        else:
            total += q * last * rate
        if s in scores:
            line += f"\n   score {fmt.scored(scores[s][0])} · {(scores[s][1] or 'HOLD').lower()}"
        lines.append(f"{dot} {line}")
    year = str(now.year)
    pl = sum(e.get("pl_huf", 0) for e in st["log"] if e["t"] == "sell" and e["at"].startswith(year))
    excl = f" (excl. {missing} without price/FX)" if missing else ""
    lines.append(f"\n📦 Holdings ≈ {_huf(total)}{excl}. 💰 {_cash(b['cash'], rates)}")
    if b["cash"] is not None:
        lines.append(f"🧮 Total ≈ {_huf(total + state.total_huf(b['cash'], rates))}{excl}")
    lines.append(f"🧾 Realised P/L {year} ≈ {fmt.signed_huf(pl)}")
    if us := tax.us_stocks(b["pos"]):
        lines.append(f"💵 US stocks ({', '.join(fmt.tick(s) for s in us)}): {tax.US_DIVIDENDS}.")
    return "💼 YOUR HOLDINGS\n" + "\n".join(lines) + "\n\nℹ️ P/L % in HUF incl. fees; approximate Yahoo FX."


def top(st) -> str:
    sc = st.get("scan")
    if not sc:
        return "📭 No scan yet: the first scheduled scan fills this."
    try:
        when = fmt.stamp(datetime.fromisoformat(sc["at"]))
    except ValueError:
        when = sc["at"][:16]
    lines = [f"🏆 Top {TOP} · scan of {when}"]
    lines += [f"{fmt.NUMBERS[n - 1]} {fmt.label(name, s)} · {fmt.scored(score)}"
              + (f" · {CALL.get(call, call)}" if call else "")
              for n, (s, name, score, call) in enumerate(sc["items"][:TOP], 1)]
    return "\n".join(lines)


def scan_one(st, sym, now, download_fn, info_fn) -> str:
    b = state.book(st["log"])
    s = resolve(sym, b["pos"], download_fn)[0]
    if s is None:
        return f"Unknown symbol {sym}."
    hists, rates = _rates(download_fn, [s], period="2y")
    hists, stale = prices.fresh(hists, now)
    if s not in hists:
        return f"No price data for {s} right now."
    try:
        fund = scan.parse_info(info_fn(s))
    except Exception:
        fund = {}
    moms = (st.get("scan") or {}).get("moms")
    held = {k: state.qty(p) for k, p in b["pos"].items()}
    cash = None if b["cash"] is None else state.total_huf(b["cash"], rates)
    items = scan.run_scan({s: hists[s]}, {s: fund}, now, cash, held, rates, moms, stale)
    if not items:
        return f"Not enough price data for {s}."
    i = items[0]
    lines = [f"🔍 {fmt.label(i['name'], s)} · {fmt.scored(i['score'], '/100')}",
             f"Call: {CALL.get(i['call'], i['call']) if i['call'] else 'none'}"]
    if c := cloud.chips(i):
        lines.append(" · ".join(c))
    lines.append(f"📝 {i['reason']}")
    lines += [fmt.flag(f) for f in i["flags"]]
    if i.get("dist"):
        lines.append(cloud.DIST)
    if not moms:
        lines.append("ℹ️ No scan yet to rank momentum against: the score is rough.")
    return "\n".join(lines)


def fill_mnb(st, now, mnb_fn=mnb.fetch):
    """Add the MNB rate to trades recorded without one (MNB was down, or that day's rate was not out yet); stops at
    the first one MNB cannot give, to try again at the next /tax."""
    today = fmt.local(now).date()
    for e in st["log"]:
        if "sym" in e and e["ccy"] != "HUF" and not e.get("fx_mnb"):
            day = fmt.local(datetime.fromisoformat(e["at"])).date()
            e["fx_mnb"] = mnb.rate(e["ccy"], day, st.setdefault("mnb", {}), today, mnb_fn)
            if not e["fx_mnb"]:
                return


def handle(st, text, now, download_fn=prices.download, info_fn=prices.info, mnb_fn=mnb.fetch) -> str:
    try:
        cmd, args = parse(text)
    except ValueError as e:
        return str(e)
    if cmd in ("help", "start"):
        return HELP
    if cmd in ("bought", "sold"):
        return trade(st, "buy" if cmd == "bought" else "sell", *args, now, download_fn, info_fn, mnb_fn)
    if cmd in MONEY:
        v, ccy = args
        at = now.isoformat(timespec="seconds")  # HUF entries keep the old shape (huf=...)
        st["log"].append(dict(t=cmd, huf=v, at=at) if ccy == "HUF" else dict(t=cmd, ccy=ccy, amt=v, at=at))
        return f"{cmd.capitalize()} {fmt.amount(v, ccy)} recorded. {_cash(state.book(st['log'])['cash'])}"
    if cmd == "undo":
        if not st["log"]:
            return "Nothing to undo."
        e = st["log"].pop()
        what = (f"{e['t']} {e['qty']} {e['sym']} at {rules._px(e['price'])} {e['ccy']}" if "sym" in e
                else f"{e['t']} {fmt.amount(state.flow(e)[1], state.flow(e)[0])}")
        if "sym" in e and st["log"] and st["log"][-1]["t"] == "fx" and st["log"][-1].get("auto"):
            c = st["log"].pop()  # the conversion recorded with this buy goes too
            what += f" and its conversion {_huf(c['huf'])} → {fmt.amount(c['amt'], c['ccy'])}"
        return f"Removed: {what}. {_cash(state.book(st['log'])['cash'])}"
    if cmd == "holdings":
        return holdings(st, now, download_fn)
    if cmd == "top":
        return top(st)
    if cmd == "refresh":
        try:
            ago = now - datetime.fromisoformat((st.get("scan") or {}).get("at") or "")
        except ValueError:
            ago = None
        if ago is not None and ago < SCAN_EVERY:
            return f"⏳ A scan ran {int(ago.total_seconds() // 60)} min ago: /top shows it. Try again in a few minutes."
        st["scan_now"] = True  # listen.py runs it after this reply
        return "🔄 Scanning now: the full message with your buy plan arrives in about 2 minutes."
    if cmd == "stats":
        return track.stats(st)
    if cmd == "tax":
        fill_mnb(st, now, mnb_fn)
        pos = state.book(st["log"])["pos"]
        hists, rates = _rates(download_fn, list(pos))
        return tax.report(st["log"], now, {s: track.last_price(hists.get(s)) for s in pos}, rates)
    return scan_one(st, args[0], now, download_fn, info_fn)


def ours(msg, chat) -> bool:
    """A text message in the private chat TELEGRAM_CHAT_ID, sent by that user (groups are ignored)."""
    c = msg.get("chat") or {}
    return (c.get("type") == "private" and str(c.get("id")) == chat
            and str((msg.get("from") or {}).get("id")) == chat and bool(msg.get("text")))


def get_updates(token, offset, opener=urllib.request.urlopen, limit=100, timeout=0) -> list:
    """New updates at or after offset; timeout > 0 long-polls (Telegram holds the request up to that many seconds)."""
    query = urllib.parse.urlencode(dict(offset=offset, limit=limit, timeout=timeout, allowed_updates='["message"]'))
    try:
        with opener(UPDATES.format(token=token, query=query), timeout=timeout + 30) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
    except Exception as e:
        raise RuntimeError(f"getUpdates failed: {type(e).__name__}: {str(e).replace(token, '***')}") from None
    if not body.get("ok"):
        raise RuntimeError(f"getUpdates failed: {str(body.get('description'))[:200].replace(token, '***')}")
    return body.get("result") or []


def step(st, token, chat, now, folder, fetch, send_fn=cloud.send_telegram, download_fn=prices.download,
         info_fn=prices.info, out=sys.stdout) -> tuple:
    """One pass over the in-memory state: the reset notice, the daily no-scan warning (precheck.overdue on the
    plaintext files in folder), the outbox, then the updates fetch(offset) returns, each handled once (the offset
    moves past it first). Returns (commands handled, replies dropped). Errors of fetch propagate; the token is
    never printed."""
    handled, up, dropped = 0, [True], [0]

    def send(text) -> bool:
        """Send unless Telegram already failed this pass; a failure is logged without the token. A permanent
        refusal (400 / 403) counts as done: it is dropped, never kept in the outbox."""
        if up[0]:
            try:
                for chunk in fmt.split(text):
                    send_fn(token, chat, chunk)
                return True
            except cloud.Permanent as e:
                print(str(e).replace(token, "***") + " (permanent: dropped)", file=out)
                dropped[0] += 1
                return True
            except Exception as e:
                print(str(e).replace(token, "***"), file=out)
                up[0] = False
        return False

    if st.get("reset") and send(RESET):
        st.pop("reset")
    last = precheck.overdue(folder, now)
    if last and st.get("scan_warned") != now.date().isoformat():
        st["scan_warned"] = now.date().isoformat()
        note = (f"⚠️ No market scan since {fmt.stamp(last)}. Check the listener workflow (listen.yml) in GitHub "
                "Actions (a failed run, or the chain of runs broke).")
        if not send(note):
            st["outbox"].append(note)
    if st["outbox"] and up[0]:
        waiting, st["outbox"] = st["outbox"], []
        for text in waiting:
            if not send(DELAYED + text):
                st["outbox"].append(text)
    for u in fetch(st["offset"]):
        st["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if not ours(msg, chat):
            continue
        try:
            reply = handle(st, msg["text"], now, download_fn, info_fn)
        except Exception as e:  # one bad command must not stop the rest
            reply = f"Error: {type(e).__name__}. Check with /holdings and try again."
        if not send(reply):
            st["outbox"].append(reply)
        handled += 1
    return handled, dropped[0]


def run(path=state.PATH, env=None, now=None, updates_fn=get_updates, send_fn=cloud.send_telegram,
        download_fn=prices.download, info_fn=prices.info, out=sys.stdout) -> int:
    env = os.environ if env is None else env
    token, chat = (env.get("TELEGRAM_TOKEN") or "").strip(), (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        print("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set", file=out)
        return 1
    now = now or datetime.now(timezone.utc)
    try:
        st = state.load(path, token)
    except RuntimeError as e:
        print(e, file=out)
        return 1
    before = "" if st.get("reset") else state.dump(st)  # after a reset, re-save under the current key
    try:
        handled, dropped = step(st, token, chat, now, os.path.dirname(path) or ".",
                                lambda offset: updates_fn(token, offset), send_fn, download_fn, info_fn, out)
    except Exception as e:  # our own Telegram errors carry no user data; anything else: the type only (public logs)
        print(str(e).replace(token, "***") if type(e) is RuntimeError else f"failed ({type(e).__name__})", file=out)
        return 1
    finally:
        if state.dump(st) != before:
            state.save(st, path, token)
    print(f"{handled} command(s) handled", file=out)
    if st["outbox"] or st.get("reset"):
        print(f"{len(st['outbox'])} repl{'y' if len(st['outbox']) == 1 else 'ies'} waiting for Telegram", file=out)
        return 1
    return 1 if dropped else 0


if __name__ == "__main__":
    sys.exit(run())
