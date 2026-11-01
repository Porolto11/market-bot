"""Paper portfolio (ROADMAP 10): the bot trades its own calls with virtual money, so its record is measured the way
you would have lived it. A simulation inside the encrypted state; no orders anywhere.

st['paper'] = dict(start=ISO date, start_value=HUF, bench=CSPX.L price at the start, cash=HUF,
pos={symbol: [qty, cost in HUF]}, marks={symbol: [last price, ccy, HUF value]} from the last scan, value=HUF,
trades=[[ISO date, 'B' or 'S', symbol, qty, HUF]] (newest last, at most MAX_TRADES)).

Each scan (step), with the live rules:
1. sell a position whose trend is bearish (not a core ETF: scan.CORE), or that is HARD_STOP below its cost;
2. buy the plan with the paper cash: the 🧱 core (scan.core_plan, the /core share) then the picks
   (scan.plan_spread), on copies of the scan items (the real message is untouched).
Fills: the scan's last price ± rules.SLIP, the K&H fee (scan._fee / scan.cost), the 1% FX spread both ways.
The cash is HUF only. Taxes are not deducted (the record is pre-tax)."""
import copy

from invest_bot import fmt, rules, scan, watch

START = 1_000_000  # HUF
MAX_TRADES = 300
BENCH = "CSPX.L"


def _rate(i, fx):
    return scan.huf_rate(i["ccy"], fx)


def step(st, items, histories, fx, today, share=None) -> list:
    """Run one paper scan (see the module doc). Returns the trades made ([['B'|'S', symbol, qty, HUF]])."""
    by = {i["symbol"]: i for i in items}
    p = st.get("paper")
    if not p:
        if not by.get(BENCH):
            return []
        p = st["paper"] = dict(start=today.isoformat(), start_value=START, bench=by[BENCH]["last"], cash=START,
                               pos={}, marks={}, value=START, trades=[])
    done = []
    for s, (q, cost) in list(p["pos"].items()):  # 1. sells
        i = by.get(s)
        if not i or i.get("stale") or not _rate(i, fx):
            continue
        px = i["last"] * (1 - rules.SLIP)
        r = _rate(i, fx) * (1 - (rules.FX_SPREAD if i["ccy"] != "HUF" else 0))
        huf = (q * px - scan._fee(q * px, i["ccy"], fx, s)) * r
        if (i.get("rating") == "bearish" and s not in scan.CORE) or q * i["last"] * _rate(i, fx) <= cost * (
                1 - watch.HARD_STOP):
            p["cash"] += huf
            del p["pos"][s]
            done.append(["S", s, q, round(huf)])
    copies = copy.deepcopy(items)  # 2. buys, never on the real items
    for i in copies:
        i.pop("plan", None)
        i.pop("core", None)
        if i["symbol"] in p["pos"] and i["symbol"] != scan.CORE_ETF:
            i["call"] = None  # held already: no second lot
    held = {s: q for s, (q, _) in p["pos"].items()}
    core, left = scan.core_plan(copies, held, p["cash"], fx, today, {"HUF": p["cash"]},
                                scan.CORE_SHARE if share is None else share)
    cash = p["cash"] - (core["approx_huf"] if core else 0)
    picks = scan.plan_spread(copies, histories, cash, fx, wallet=left)
    for i in ([core] if core else []) + picks:
        q0, c0 = p["pos"].get(i["symbol"], [0, 0.0])
        p["pos"][i["symbol"]] = [q0 + i["qty"], c0 + i["approx_huf"]]
        p["cash"] -= i["approx_huf"]
        done.append(["B", i["symbol"], i["qty"], round(i["approx_huf"])])
    p["marks"] = {s: [by[s]["last"], by[s]["ccy"], round(q * by[s]["last"] * (_rate(by[s], fx) or 0))]
                  for s, (q, _) in p["pos"].items() if s in by}
    p["value"] = round(p["cash"] + sum(m[2] for m in p["marks"].values()))
    if by.get(BENCH):
        p["bench_now"] = by[BENCH]["last"]
    p["trades"] = [*p["trades"], *([today.isoformat(), *t] for t in done)][-MAX_TRADES:]
    return done


def _pl(p) -> tuple:
    """(own return, CSPX return) since the start."""
    own = p["value"] / p["start_value"] - 1
    bench = p.get("bench_now", p["bench"]) / p["bench"] - 1
    return own, bench


def line(st, trades) -> str:
    """The scan message line when the paper bot traded: '🧪 Paper bot: bought 7× VWCE, sold 1× ANET · 1.00M HUF
    (+0.4%) vs CSPX +0.2%'; '' when it did not trade."""
    p = st.get("paper")
    if not p or not trades:
        return ""
    own, bench = _pl(p)
    acts = [f"{'bought' if t[0] == 'B' else 'sold'} {t[2]}× {fmt.tick(t[1])}" for t in trades]
    return f"🧪 Paper bot: {', '.join(acts)} · {fmt.k(p['value'])} HUF ({fmt.signed(own * 100)}) vs CSPX {fmt.signed(bench * 100)}"


def report(st) -> str:
    """The /paper reply: value and return vs CSPX since the start, the positions, the last trades."""
    p = st.get("paper")
    if not p:
        return "🧪 The paper bot starts at the next scan with 1 000 000 virtual HUF and trades every call by itself."
    own, bench = _pl(p)
    out = [f"🧪 Paper bot since {p['start']}: {fmt.huf(p['value'])} ({fmt.signed(own * 100)}) vs CSPX "
           f"{fmt.signed(bench * 100)} · cash {fmt.huf(p['cash'])}",
           "Follows every call by itself: real prices, K&H fees, 1% FX; before tax."]
    if p["pos"]:
        out.append("\n📦 Positions")
        for s, (q, cost) in sorted(p["pos"].items(), key=lambda kv: -kv[1][1]):
            m = p["marks"].get(s)
            pl = f" {fmt.signed((m[2] / cost - 1) * 100)}" if m and cost else ""
            out.append(f"• {fmt.tick(s)} {q}× · {fmt.k(m[2] if m else cost)} HUF{pl}")
    if p["trades"]:
        out.append("\n🔁 Last trades")
        out += [f"• {d} {'bought' if a == 'B' else 'sold'} {q}× {fmt.tick(s)} ≈ {fmt.k(h)} HUF"
                for d, a, s, q, h in p["trades"][-8:]]
    return "\n".join(out)
