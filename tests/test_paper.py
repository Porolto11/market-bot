import copy

from invest_bot import commands, paper, scan
from test_scan import FX, NOW, TODAY, hist, strong_fund, universe


def scanned():
    hs = universe({"NVDA": hist(), "ANET": hist(up=1.014), "XOM": hist(up=1.013), "CSPX.L": hist(up=1.006),
                   "VWCE.DE": hist(up=1.005)})
    funds = {s: dict(strong_fund(scan.features(h)["last"]), sector=sec)
             for s, h, sec in (("NVDA", hs["NVDA"], "Technology"), ("ANET", hs["ANET"], "Technology"),
                               ("XOM", hs["XOM"], "Energy"))}
    return hs, scan.run_scan(hs, funds, NOW, None, {}, FX)


def test_first_scan_buys_the_core_and_the_picks_with_virtual_money_only():
    hs, items = scanned()
    before = copy.deepcopy(items)
    st = {}
    done = paper.step(st, items, hs, FX, TODAY)
    p = st["paper"]
    assert items == before  # the real message's items are untouched
    bought = [t[1] for t in done if t[0] == "B"]
    assert bought[0] == "VWCE.DE" and set(bought[1:]) <= {"NVDA", "ANET", "XOM"} and len(bought) >= 2
    assert p["cash"] >= 0 and p["start"] == TODAY.isoformat() and p["start_value"] == paper.START
    assert 0.97 * paper.START <= p["value"] <= paper.START  # fees and the FX spread cost a little
    assert p["pos"]["VWCE.DE"][1] >= 0.45 * paper.START  # the core is about half
    again = paper.step(st, items, hs, FX, TODAY)  # the same scan again: nothing held is bought twice
    assert not [t for t in again if t[0] == "B" and t[1] in bought and t[1] != "VWCE.DE"]


def test_sells_follow_the_live_rules():
    hs, items = scanned()
    st = {}
    paper.step(st, items, hs, FX, TODAY)
    p = st["paper"]
    pick = next(s for s in p["pos"] if s != "VWCE.DE")
    by = {i["symbol"]: i for i in items}
    by[pick]["rating"] = by["VWCE.DE"]["rating"] = "bearish"  # the trend turned: the pick goes, the core stays
    done = paper.step(st, items, hs, FX, TODAY)
    assert ["S", pick] in [t[:2] for t in done] and "VWCE.DE" in p["pos"]
    st2 = {}
    _, items2 = scanned()
    paper.step(st2, items2, hs, FX, TODAY)
    by2 = {i["symbol"]: i for i in items2}
    held = next(s for s in st2["paper"]["pos"] if s != "VWCE.DE")
    by2[held]["last"] *= 0.7  # 30% below the cost: the hard stop sells it whatever the trend
    assert ["S", held] in [t[:2] for t in paper.step(st2, items2, hs, FX, TODAY)]


def test_line_and_report():
    hs, items = scanned()
    st = {}
    assert paper.report(st).startswith("🧪 The paper bot starts at the next scan")
    done = paper.step(st, items, hs, FX, TODAY)
    assert paper.line(st, done).startswith("🧪 Paper bot: bought ") and " vs CSPX " in paper.line(st, done)
    assert paper.line(st, []) == ""
    r = commands.handle(st, "/paper", NOW, lambda s, period: {}, lambda s: {})
    assert r.startswith(f"🧪 Paper bot since {TODAY.isoformat()}: ") and "📦 Positions\n• VWCE " in r
    assert "🔁 Last trades\n• " in r
