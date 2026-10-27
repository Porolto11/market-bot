"""The bot's state as one encrypted JSON file (state/state.enc on the `state` branch).

Keys: offset (Telegram getUpdates), log (trades and cash entries, oldest first), sent (calls of the last sent
scan messages), calls (the call log scored by track.py), shadow (the model test, track.shadow), scan (the latest scan's ranking and momentum lists),
outbox (replies not yet delivered), weekly (time and portfolio value of the last weekly summary), alerts
(keys of the alerts already sent: symbol|date for big moves,
stop|symbol|ISO week for stops), dips ({symbol: {d, dd}}: date and
depth of the last core ETF dip alert), reset (set when decryption failed), scan_slot (the ISO UTC time of the
last scan slot the listener ran), scan_warned (the UTC date of the last "no scan" warning). Holdings and cash (per currency: HUF, USD, EUR) are not stored separately: book() replays the
log, so /undo just drops the last entry (and the automatic conversion recorded with a buy). The Fernet key comes from TELEGRAM_TOKEN; rotating the token starts an empty state with reset=True
and keeps the old file as state.enc.bad-<UTC time>. save() also writes two plaintext, non-sensitive files for the
cheap pre-check (precheck.py): offset.txt, outbox_pending (1 when a reply or the reset notice is waiting),
last_scan.txt (the last scan's ISO UTC time) and scan_warned.txt (the UTC date of the last "no scan" warning).
"""
import base64
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

PATH = "state/state.enc"
MAX_SENT = 60


def empty() -> dict:
    return dict(offset=0, log=[], sent=[], calls=[], scan=None, outbox=[])


def _fernet(token) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"invest-bot-state:" + token.strip().encode()).digest()))


def load(path, token, expected=None) -> dict:
    """The decrypted state. Missing file: empty, unless expected (default: env STATE_EXPECTED=1, set by the workflow
    when the state branch had a state.enc), then RuntimeError so an empty state never replaces the real one.
    Undecryptable: the file is kept as <path>.bad-<UTC time> and an empty state with reset=True is returned."""
    if expected is None:
        expected = os.environ.get("STATE_EXPECTED") == "1"
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except FileNotFoundError:
        if expected:
            raise RuntimeError(f"{path} is missing but the state branch has one: not starting empty") from None
        return empty()
    try:
        data = json.loads(_fernet(token).decrypt(blob))
        if not isinstance(data, dict):
            raise ValueError("not a dict")
    except (InvalidToken, ValueError):
        shutil.copyfile(path, f"{path}.bad-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
        return dict(empty(), reset=True)
    return dict(empty(), **data)


def dump(st) -> str:
    return json.dumps(st, sort_keys=True, separators=(",", ":"))


def save(st, path, token):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_fernet(token).encrypt(dump(st).encode()))
    os.replace(tmp, path)
    folder = os.path.dirname(path) or "."
    with open(os.path.join(folder, "offset.txt"), "w") as f:
        f.write(str(st.get("offset", 0)))
    with open(os.path.join(folder, "outbox_pending"), "w") as f:
        f.write("1" if st.get("outbox") or st.get("reset") else "0")
    with open(os.path.join(folder, "last_scan.txt"), "w") as f:
        f.write((st.get("scan") or {}).get("at") or "")
    with open(os.path.join(folder, "scan_warned.txt"), "w") as f:
        f.write(st.get("scan_warned") or "")


def _fifo(lots, qty) -> tuple:
    """Take qty shares from the oldest lots ([qty, price, cost_huf, tax_cost_huf]); the HUF cost and the tax cost
    of the shares taken."""
    cost = tcost = 0.0
    while qty:
        q, px, c, t = lots[0]
        take = min(q, qty)
        part, tpart = c * take / q, t * take / q
        cost, tcost = cost + part, tcost + tpart
        if take == q:
            lots.pop(0)
        else:
            lots[0] = [q - take, px, c - part, t - tpart]
        qty -= take
    return cost, tcost


HELD = ("HUF", "USD", "EUR")  # the cash currencies a K&H Platinum account holds (/cash, /deposit, /withdraw)


def wallet_ccy(ccy) -> tuple:
    """(the currency a trade in `ccy` is paid from, units of it per price unit): GBp / GBX prices are pence, so 0.01
    GBP; any other currency is its own, 1."""
    return ("GBP", 0.01) if ccy in ("GBp", "GBX") else (ccy, 1.0)


def flow(e) -> tuple:
    """(currency, amount) of a cash, deposit, withdraw or fx entry. Entries without a currency are HUF (the 'huf'
    key: every entry before multi-currency cash, and HUF entries still)."""
    return (e["ccy"], e["amt"]) if "amt" in e else ("HUF", e["huf"])


def total_huf(wallet, rates) -> float:
    """The HUF value of the balances {currency: amount} at the rates ({currency: HUF}); one without a rate counts 0."""
    return sum(v * (1.0 if c == "HUF" else (rates or {}).get(c) or 0.0) for c, v in (wallet or {}).items())


def book(log) -> dict:
    """Replay the log: pos {symbol: {ccy, lots}}, cash ({currency: balance}; None until a /cash, /deposit or
    /withdraw) and realised (the FIFO P/L of each sale in HUF, in order). Costs and proceeds include the fee and a
    buy's transaction tax (both deductible), at the trade's FX; taxed: the same P/L at the official MNB rate of the
    buy and the sell day (fx_mnb), where recorded, else at the trade's FX (approximate).
    Cash per currency: /cash sets one currency's balance, /deposit and /withdraw change it, an fx entry moves HUF
    into a currency (the HUF paid includes the 1% spread). A trade recorded with wallet=True pays from (or is paid
    into) its own currency (GBp: GBP); older trades, and trades recorded while the cash was unknown, move HUF at the
    trade's FX, as the single HUF cash did."""
    pos, cash, known, realised, taxed = {}, {}, False, [], []
    for e in log:
        t = e["t"]
        if t in ("cash", "deposit", "withdraw", "fx"):
            known = True
            ccy, amt = flow(e)
            if t == "fx":
                cash["HUF"] = cash.get("HUF", 0.0) - e["huf"]
            cash[ccy] = amt if t == "cash" else cash.get(ccy, 0.0) + (-amt if t == "withdraw" else amt)
        else:
            value, fx = e["qty"] * e["price"], e["fx"]
            tfx = e.get("fx_mnb") or fx
            w, k = wallet_ccy(e["ccy"]) if e.get("wallet") else ("HUF", fx)
            p = pos.setdefault(e["sym"], dict(ccy=e["ccy"], lots=[]))
            if t == "buy":
                paid = value + e["fee"] + e.get("tax", 0.0)  # the buy-side tax (FR 0.4%, ...) is a cost too
                cash[w] = cash.get(w, 0.0) - paid * k
                p["lots"].append([e["qty"], e["price"], paid * fx, paid * tfx])
            else:
                cash[w] = cash.get(w, 0.0) + (value - e["fee"]) * k
                cost, tcost = _fifo(p["lots"], e["qty"])
                realised.append((value - e["fee"]) * fx - cost)
                taxed.append((value - e["fee"]) * tfx - tcost)
                if not p["lots"]:
                    del pos[e["sym"]]
    return dict(pos=pos, cash=cash if known else None, realised=realised, taxed=taxed)


def qty(p) -> int:
    return sum(lot[0] for lot in p["lots"])


def portfolio(st, rates=None):
    """Holdings and cash in the PORTFOLIO_JSON shape cloud.py uses, or None when the state has neither. cash: the
    balances {currency: amount} (None when unknown); cash_huf: their HUF value at the rates (HUF only without)."""
    b = book(st["log"])
    if not b["pos"] and b["cash"] is None:
        return None
    holdings = [dict(symbol=s, qty=qty(p), ccy=p["ccy"], avg_cost_huf=sum(lot[2] for lot in p["lots"]) / qty(p))
                for s, p in b["pos"].items()]
    return dict(as_of=st["log"][-1].get("at"), cash=b["cash"],
                cash_huf=None if b["cash"] is None else total_huf(b["cash"], rates), holdings=holdings)
