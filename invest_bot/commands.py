"""Telegram commands: read new messages with getUpdates, record trades and cash in the encrypted state, reply.

    python -m invest_bot.commands      (env TELEGRAM_TOKEN, TELEGRAM_CHAT_ID; one pass, for manual and debug runs:
                                        the always-on listener, listen.py, calls step() in its loop)

Only private-chat messages from TELEGRAM_CHAT_ID (chat and sender) are handled; anything else is skipped silently.
The getUpdates offset lives in the state, so a command is handled once. A reply that cannot be sent waits in the
state's outbox and goes out first on the next run, unless Telegram refused it for good (400 / 403: logged and
dropped). On a weekday with no market scan for over 26 weekday hours it warns once a day (precheck.overdue). Prices are in the instrument's currency; HUF values use
approximate Yahoo FX. Holdings and amounts are never printed to the log.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from invest_bot import cloud, fmt, mnb, precheck, prices, rules, scan, state, tax, track

UPDATES = "https://api.telegram.org/bot{token}/getUpdates?{query}"
TOP = 10
HELP = """🤖 Invest Bot commands

📝 Record
🟢 /bought SYM QTY PRICE — a buy, whole shares (cash goes down by value + K&H fee)
🔴 /sold SYM QTY PRICE — a sale (FIFO P/L kept for tax)
💰 /deposit HUF · /withdraw HUF — change cash (write 120000, not 120.000)
🏦 /cash HUF — set cash to your real K&H balance
↩️ /undo — remove the last trade or cash entry

🔎 Look
💼 /holdings — positions, P/L, total in HUF
🏆 /top — top 10 of the last scan
🔍 /scan SYM — live analysis of one symbol
📊 /stats — how past calls did vs CSPX
🧾 /tax — realised P/L, estimated 15% tax (MNB rates)

ℹ️ Prices in the instrument's currency (comma or dot); HUF in whole forints.
SYM: a bot ticker (MOL, CSPX, SAP, OTP) or a Yahoo symbol (MOL.BD, NVDA, SAP.DE)."""
RESET = "State was reset (it could not be decrypted, e.g. the bot token changed): please re-enter holdings and cash."
DELAYED = "(delayed reply) "
USAGE = {"bought": "/bought SYM QTY PRICE", "sold": "/sold SYM QTY PRICE", "deposit": "/deposit HUF",
         "withdraw": "/withdraw HUF", "cash": "/cash HUF", "scan": "/scan SYM"}
ARGS = {"bought": 3, "sold": 3, "deposit": 1, "withdraw": 1, "cash": 1, "scan": 1,
        "holdings": 0, "top": 0, "stats": 0, "tax": 0, "undo": 0, "help": 0, "start": 0}
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
    if len(args) != ARGS[cmd]:
        raise ValueError(f"Usage: {USAGE.get(cmd, '/' + cmd)}")
    if cmd in ("deposit", "withdraw", "cash") and THOUSANDS.search(args[0]):
        raise ValueError(f"Ambiguous amount {args[0]}: write whole forints without separators, e.g. 120000.")
    try:
        if cmd in ("bought", "sold"):
            q, px = num(args[1]), num(args[2])
            if q <= 0 or q != int(q) or px <= 0:
                raise ValueError
            return cmd, (args[0].upper(), int(q), px)
        if cmd in ("deposit", "withdraw", "cash"):
            v = num(args[0])
            if v < 0 or (v == 0 and cmd != "cash"):
                raise ValueError
            return cmd, (v,)
    except ValueError:
        raise ValueError(f"Usage: {USAGE[cmd]} (QTY a whole number, amounts positive)") from None
    return cmd, tuple(a.upper() for a in args)


def _huf(x) -> str:
    return f"{rules._huf(x)} HUF"


def _cash(cash) -> str:
    if cash is None:
        return "Cash not set yet: send /cash HUF."
    return f"Cash ≈ {_huf(cash)}." + (" Warning: cash is below 0, check it with /cash." if cash < 0 else "")


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
    note = ""
    if ccy != "HUF":  # the tax figures use the official MNB rate of the trade day
        day = fmt.local(now).date()
        e["fx_mnb"] = mnb.rate(ccy, day, st.setdefault("mnb", {}), day, mnb_fn)
        note = (f" MNB rate {rules._px(e['fx_mnb'])} HUF/{ccy} for tax." if e["fx_mnb"] else
                " No MNB rate yet: tax uses Yahoo FX (approx) until /tax finds it.")
    huf = (q * px + (fee if side == "buy" else -fee)) * fx
    st["log"].append(e)
    after = state.book(st["log"])
    word = "Bought" if side == "buy" else "Sold"
    text = (f"Recorded in {ccy} — /undo if wrong. " if from_yahoo else "") + (
        f"{word} {q} {s} at {rules._px(px)} {ccy} (fee {fee:.2f} {ccy}), "
        f"{'cost' if side == 'buy' else 'proceeds'} ≈ {_huf(huf)}.")
    if side == "sell":
        e["pl_huf"] = round(after["realised"][-1])
        text += f" Realised P/L ≈ {e['pl_huf']:+,.0f} HUF (FIFO).".replace(",", " ")
    return text + " " + _cash(after["cash"]) + note


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
    lines.append(f"\n📦 Holdings ≈ {_huf(total)}{excl}. 💰 {_cash(b['cash'])}")
    if b["cash"] is not None:
        lines.append(f"🧮 Total ≈ {_huf(total + b['cash'])}{excl}")
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
    items = scan.run_scan({s: hists[s]}, {s: fund}, now, b["cash"], held, rates, moms, stale)
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
    if cmd in ("deposit", "withdraw", "cash"):
        st["log"].append(dict(t=cmd, huf=args[0], at=now.isoformat(timespec="seconds")))
        return f"{cmd.capitalize()} {_huf(args[0])} recorded. {_cash(state.book(st['log'])['cash'])}"
    if cmd == "undo":
        if not st["log"]:
            return "Nothing to undo."
        e = st["log"].pop()
        what = (f"{e['t']} {e['qty']} {e['sym']} at {rules._px(e['price'])} {e['ccy']}" if "sym" in e
                else f"{e['t']} {_huf(e['huf'])}")
        return f"Removed: {what}. {_cash(state.book(st['log'])['cash'])}"
    if cmd == "holdings":
        return holdings(st, now, download_fn)
    if cmd == "top":
        return top(st)
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
