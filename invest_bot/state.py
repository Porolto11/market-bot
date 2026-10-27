"""The bot's state as one encrypted JSON file (state/state.enc on the `state` branch).

Keys: offset (Telegram getUpdates), log (trades and cash entries, oldest first), sent (calls of the last sent
scan messages), calls (the call log scored by track.py), scan (the latest scan's ranking and momentum lists),
outbox (replies not yet delivered), weekly (time and portfolio value of the last weekly summary), alerts
(keys of the alerts already sent: symbol|date for big moves,
stop|symbol|ISO week for stops), dips ({symbol: {d, dd}}: date and
depth of the last core ETF dip alert), reset (set when decryption failed), scan_slot (the ISO UTC time of the
last scan slot the listener ran), scan_warned (the UTC date of the last "no scan" warning). Holdings and cash are not stored separately: book() replays the log, so /undo just drops
the last entry. The Fernet key comes from TELEGRAM_TOKEN; rotating the token starts an empty state with reset=True
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


def book(log) -> dict:
    """Replay the log: pos {symbol: {ccy, lots}}, cash (HUF; None until a /cash, /deposit or /withdraw) and
    realised (the FIFO P/L of each sale in HUF, in order). Costs and proceeds include the fee (deductible), at the
    trade's FX; taxed: the same P/L at the official MNB rate of the buy and the sell day (fx_mnb), where recorded,
    else at the trade's FX (approximate)."""
    pos, cash, known, realised, taxed = {}, 0.0, False, [], []
    for e in log:
        t = e["t"]
        if t == "cash":
            cash, known = e["huf"], True
        elif t in ("deposit", "withdraw"):
            cash += e["huf"] if t == "deposit" else -e["huf"]
            known = True
        else:
            value, fx = e["qty"] * e["price"], e["fx"]
            tfx = e.get("fx_mnb") or fx
            p = pos.setdefault(e["sym"], dict(ccy=e["ccy"], lots=[]))
            if t == "buy":
                cash -= (value + e["fee"]) * fx
                p["lots"].append([e["qty"], e["price"], (value + e["fee"]) * fx, (value + e["fee"]) * tfx])
            else:
                cash += (value - e["fee"]) * fx
                cost, tcost = _fifo(p["lots"], e["qty"])
                realised.append((value - e["fee"]) * fx - cost)
                taxed.append((value - e["fee"]) * tfx - tcost)
                if not p["lots"]:
                    del pos[e["sym"]]
    return dict(pos=pos, cash=cash if known else None, realised=realised, taxed=taxed)


def qty(p) -> int:
    return sum(lot[0] for lot in p["lots"])


def portfolio(st):
    """Holdings and cash in the PORTFOLIO_JSON shape cloud.py uses, or None when the state has neither."""
    b = book(st["log"])
    if not b["pos"] and b["cash"] is None:
        return None
    holdings = [dict(symbol=s, qty=qty(p), ccy=p["ccy"], avg_cost_huf=sum(lot[2] for lot in p["lots"]) / qty(p))
                for s, p in b["pos"].items()]
    return dict(as_of=st["log"][-1].get("at"), cash_huf=b["cash"], holdings=holdings)
