import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from invest_bot import scan

NOW = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)
TODAY = NOW.date()
FX = {"USD": 350.0, "EUR": 400.0, "GBP": 460.0, "DKK": 53.0}
GOOD = dict(revenue_growth=0.30, earnings_growth=0.40, profit_margin=0.25, target_mean=None, analysts=10,
            currency="USD")


def hist(up=1.015, down=0.99, n=320, start=100.0, hot_tail=0):
    """Alternating up/down days (RSI ~60 for the default uptrend); hot_tail: that many +1% days at the end."""
    px, out = start, []
    for i in range(n):
        px *= up if i % 2 == 0 else down
        if i >= n - hot_tail:
            px *= 1.01 / (up if i % 2 == 0 else down)
        out.append(px)
    idx = pd.bdate_range(end="2026-10-23", periods=n)
    return pd.DataFrame({"Close": out}, index=idx)


def weak():
    return hist(up=1.004, down=0.998)


def universe(extra):
    h = {f"W{i}": weak() for i in range(5)}
    h.update(extra)
    return h


def strong_fund(last):
    return dict(GOOD, target_mean=last * 1.3)


def item(items, s):
    return next(i for i in items if i["symbol"] == s)


def test_score_monotonic_in_momentum():
    moms = dict(long=[0.0, 0.1, 0.2, 0.3], mom_6m=[0.0, 0.05, 0.1, 0.15])
    base = scan.features(hist())
    scores = []
    for m in (0.0, 0.1, 0.2, 0.3):
        ind = dict(base, mom_12_1=m, mom_6m=m / 2)
        scores.append(scan.potential(ind, GOOD, moms)[0])
    assert scores == sorted(scores) and scores[0] < scores[-1]


def test_etf_gets_neutral_growth_and_missing_fundamentals_are_neutral():
    ind = scan.features(hist())
    _, parts, _ = scan.potential(ind, dict(GOOD, kind="etf"), {})
    assert parts["growth"] == 12.5
    _, parts, _ = scan.potential(ind, dict(GOOD, revenue_growth=0.0, earnings_growth=0.0, profit_margin=0.0), {})
    assert parts["growth"] == 0
    _, parts, _ = scan.potential(ind, {}, {})
    assert parts["growth"] == 12.5 and parts["upside"] == 10


def test_upside_needs_five_analysts():
    ind = scan.features(hist())
    last = ind["last"]
    _, parts, _ = scan.potential(ind, dict(GOOD, target_mean=last * 1.3, analysts=4), {})
    assert parts["upside"] == 10 and parts["upside_pct"] is None
    _, parts, _ = scan.potential(ind, dict(GOOD, target_mean=last * 1.3, analysts=5), {})
    assert parts["upside"] == 20
    _, parts, _ = scan.potential(ind, dict(GOOD, target_mean=last * 0.9, analysts=5), {})
    assert parts["upside"] == 0


def test_flags_fire():
    ind = dict(scan.features(hist()), vol_20d=0.7, dd_52w=-0.35, rsi14=80)
    fund = dict(GOOD, earnings_date=(TODAY + timedelta(days=3)).isoformat())  # Thu 29 Oct: 3 trading days
    _, _, flags = scan.potential(ind, fund, {}, TODAY)
    assert flags == ["earnings in 3 days", "very volatile", "far below 52w high", "overheated"]
    far = dict(GOOD, earnings_date=(TODAY + timedelta(days=30)).isoformat())
    assert scan.potential(scan.features(hist()), far, {}, TODAY)[2] == []


def test_parse_info_missing_fields():
    assert scan.parse_info({}) == dict.fromkeys(scan.FUND_KEYS)
    assert scan.parse_info(None)["analysts"] is None
    f = scan.parse_info({"revenueGrowth": 0.2, "numberOfAnalystOpinions": 12, "currency": "EUR",
                         "earningsTimestampStart": 1793404800, "targetMeanPrice": "nan"})
    assert f["revenue_growth"] == 0.2 and f["analysts"] == 12 and f["currency"] == "EUR"
    assert f["earnings_date"] == "2026-10-31" and f["target_mean"] is None


def test_call_rules_each_branch():
    good, bear, hot = hist(), hist(up=0.985, down=1.01), hist(hot_tail=20)
    hs = universe({"GOOD": good, "BEAR": bear, "HOT": hot, "HELD": good, "HELDBEAR": bear, "MEH": weak()})
    funds = {s: strong_fund(scan.features(h)["last"]) for s, h in hs.items() if s[0] != "W" and s != "MEH"}
    items = scan.run_scan(hs, funds, NOW, 1_000_000, {"HELD": 3, "HELDBEAR": 7}, FX)
    assert scan.features(good)["rsi14"] <= 70 and scan.features(hot)["rsi14"] > 70
    g = item(items, "GOOD")
    assert g["score"] >= 70 and g["call"] == "BUY" and g["limit"] == round(g["last"] * 1.002, 2)
    assert "Buy at up to" in g["reason"] and "Score" in g["reason"]
    h = item(items, "HOT")
    assert h["call"] == "WAIT_FOR_DIP" and h["limit"] < h["last"] and h["valid_until"] > TODAY.isoformat()
    assert "overheated" in h["reason"]
    assert item(items, "BEAR")["call"] == "AVOID"
    assert item(items, "HELD")["call"] == "HOLD"
    s = item(items, "HELDBEAR")
    assert s["call"] == "SELL" and s["qty"] == 7 and s["limit"] < s["last"]
    assert item(items, "MEH")["call"] is None and item(items, "W0")["call"] is None
    assert [i["score"] for i in items] == sorted((i["score"] for i in items), reverse=True)


def test_whole_share_sizing_and_share_over_budget():
    hs = universe({"GOOD": hist()})
    last = scan.features(hs["GOOD"])["last"]
    funds = {"GOOD": strong_fund(last)}
    g = item(scan.run_scan(hs, funds, NOW, 1_000_000, {}, FX), "GOOD")
    assert g["call"] == "BUY" and isinstance(g["qty"], int) and g["qty"] >= 1
    assert g["approx_huf"] <= 500_000 and (g["qty"] + 1) * g["limit"] * FX["USD"] > 500_000 * 0.99
    assert f"{g['qty']} shares" in g["reason"]
    small = item(scan.run_scan(hs, funds, NOW, 2 * last * 350 * 0.5, {}, FX), "GOOD")
    assert small["call"] == "BUY" and small["qty"] == 0 and "1 share ≈" in small["reason"]
    assert "more than your budget" in small["reason"]
    unknown = item(scan.run_scan(hs, funds, NOW, None, {}, FX), "GOOD")
    assert unknown["qty"] is None and "Cash unknown" in unknown["reason"]


def test_sizing_huf_and_pence():
    assert scan.size(100_000, 3000.0, "HUF", FX)[0] == 33  # 33 x 3000 + 349 fee <= 100 000
    qty, approx, one = scan.size(100_000, 500.0, "GBp", FX)  # 5 GBP a share; 43 would go over with fee and FX spread
    assert qty == 42 and approx <= 100_000 and one == pytest.approx(5 * 460 * 1.003 + 3.49 * 400 * 1.003, rel=0.01)
    assert scan.size(100_000, 10.0, "CHF", FX) == (None, None, None)


def test_missing_fundamentals_and_bad_history_do_not_crash():
    hs = universe({"X": hist(), "SHORT": hist(n=30), "EMPTY": pd.DataFrame({"Close": []}), "NONE": None})
    items = scan.run_scan(hs, {"X": None}, NOW, 100_000, {}, {})
    assert {i["symbol"] for i in items} >= {"X", "SHORT"} and "EMPTY" not in {i["symbol"] for i in items}
    x = item(items, "X")
    assert x["parts"]["growth"] == 12.5 and x["parts"]["upside"] == 10


def buy_item(sym, limit, ccy="USD", score=90):
    return dict(symbol=sym, call="BUY", ccy=ccy, limit=limit, score=score,
                one_share_huf=scan.size(None, limit, ccy, FX)[2])


def spent(picks):
    return sum(sum(scan.cost(i, i["qty"], FX)) for i in picks)


def test_plan_of_1_3m_with_10_candidates_has_at_most_3_picks_within_cash():
    items = [buy_item(f"S{k}", 50.0 + 40 * k, score=99 - k) for k in range(10)]
    picks = scan.plan(items, 1_300_000, FX)
    assert [i["symbol"] for i in picks] == ["S0", "S1", "S2"] and sum(i.get("plan", False) for i in items) == 3
    assert spent(picks) <= 1_300_000 * 0.99 and all(i["fee_pct"] <= scan.FEE_MAX for i in picks)
    assert all(i["approx_huf"] == pytest.approx(i["value_huf"] + i["fee_huf"]) for i in picks)
    # equal split, then the leftover went to the top pick: little cash is left over
    assert 1_300_000 * 0.99 - spent(picks) < min(sum(scan.cost(i, 1, FX)) for i in picks)
    hs = universe({f"S{k}": hist(up=1.015 + k / 1000) for k in range(10)})  # through run_scan too
    funds = {s: strong_fund(scan.features(h)["last"]) for s, h in hs.items() if s[0] == "S"}
    items = scan.run_scan(hs, funds, NOW, 1_300_000, {}, FX)
    picks = scan.plan(items, 1_300_000, FX)
    assert len(picks) <= 3 and spent(picks) <= 1_300_000


def test_expensive_top_pick_is_passed_over_unless_it_is_the_only_fit():
    big, a, b, c = (buy_item("BIG", 1500.0, score=99), buy_item("A", 100.0, score=90),
                    buy_item("B", 110.0, score=85), buy_item("C", 120.0, score=80))
    picks = scan.plan([big, a, b, c], 1_300_000, FX)  # 1 BIG ≈ 528 000 HUF > cash / 3
    assert [i["symbol"] for i in picks] == ["A", "B", "C"] and not big.get("plan")
    picks = scan.plan([dict(big), dict(a, call=None), dict(b, call=None)], 1_300_000, FX)
    assert [i["symbol"] for i in picks] == ["BIG"] and picks[0]["qty"] == 2  # the only fit takes the cash
    picks = scan.plan([dict(big), dict(a)], 1_300_000, FX)  # A fits a third, BIG may use the other two
    assert [i["symbol"] for i in picks] == ["BIG", "A"] and picks[0]["qty"] == 1 and spent(picks) <= 1_287_000


def test_leftover_goes_one_share_at_a_time_to_the_highest_ranked_pick():
    a, b = buy_item("A", 1000.0, score=95), buy_item("B", 300.0, score=90)
    picks = scan.plan([a, b], 1_300_000, FX)
    even = [scan.size(1_300_000 * 0.99 / 2, px, "USD", FX)[0] for px in (1000.0, 300.0)]  # 1 and 6 shares
    left = 1_300_000 * 0.99 - sum(sum(scan.cost(i, q, FX)) for i, q in zip(picks, even))  # ≈ 300 000
    assert left < sum(scan.cost(a, 1, FX))  # one more A (≈ 352 000) does not fit, so B, the next rank, gets it
    assert a["qty"] == even[0] and b["qty"] > even[1] and spent(picks) <= 1_300_000 * 0.99
    assert 1_300_000 * 0.99 - spent(picks) < sum(scan.cost(b, 1, FX))  # nothing more fits
    a2, b2 = buy_item("A", 100.0, score=95), buy_item("B", 400.0, score=90)
    scan.plan([a2, b2], 1_300_000, FX)
    assert a2["qty"] > scan.size(1_300_000 * 0.99 / 2, 100.0, "USD", FX)[0]  # the top pick takes the leftover first
    assert b2["qty"] == scan.size(1_300_000 * 0.99 / 2, 400.0, "USD", FX)[0]  # 4 shares leave ≈ 80 000


def test_buy_with_a_fee_over_1_percent_is_skipped():
    usd, huf = buy_item("US", 100.0, score=95), buy_item("HU", 3000.0, "HUF", score=90)
    assert scan.min_qty(usd, FX) == 4 and scan.min_qty(huf, FX) == 12  # 3.49 USD / 1% = 349 USD; 349 HUF / 1%
    picks = scan.plan([usd, huf], 120_000, FX)  # 4 USD shares ≈ 141 000 HUF do not fit: US is skipped
    assert [i["symbol"] for i in picks] == ["HU"] and huf["fee_pct"] <= scan.FEE_MAX and not usd.get("plan")
    assert huf["qty"] == 39  # 39 x 3000 + 349 fee <= 118 800


def test_zero_cash_means_no_plan_and_the_deposit_line():
    from invest_bot import cloud
    items = [buy_item("A", 100.0)]
    assert scan.plan(items, 0, FX) == [] and scan.plan(items, None, FX) == []
    text = cloud.message(items, {}, NOW, None, 0, True, FX)
    assert "🎯 TODAY'S MOVE\n🤷 Nothing fits your cash — top pick A needs ≈ " in text and "Deposit or wait." in text
    assert "🟢 BUY: nothing fits your cash (0 HUF), fees included\n👀 Watchlist: A 90" in text


def walk(seed, n=320):
    rng = np.random.default_rng(seed)
    px = 100 * np.cumprod(1.003 + rng.normal(0, 0.01, n))
    return pd.DataFrame({"Close": px}, index=pd.bdate_range(end="2026-10-23", periods=n))


def scanned(hs, sector=None):
    funds = {s: dict(strong_fund(scan.features(h)["last"]), sector=sector) for s, h in hs.items() if s[0] != "W"}
    return scan.run_scan(hs, funds, NOW, 1_000_000, {}, FX)


def test_perfectly_correlated_picks_are_skipped():
    hs = universe({f"S{k}": hist() * (1 + k) for k in range(5)})
    items = scanned(hs)
    assert sum(i["call"] == "BUY" for i in items) == 5
    scan.diversify(items, hs, 5)
    assert [i["symbol"] for i in items if i["call"] == "BUY"] == ["S0"]
    assert {i["skipped"] for i in items if i.get("skipped")} == {"too similar to S0"}


def test_at_most_two_picks_per_sector_and_etfs_count_as_one_sector():
    hs = universe({f"S{k}": walk(k) for k in range(4)})
    items = scanned(hs, sector="Technology")
    buys = [i["symbol"] for i in items if i["call"] == "BUY"]
    assert len(buys) == 4 and item(items, "S0")["sector"] == "Technology"
    scan.diversify(items, hs, 5)
    assert [i["symbol"] for i in items if i["call"] == "BUY"] == buys[:2]
    assert [i["skipped"] for i in items if i.get("skipped")] == ["2 Technology picks already"] * 2
    from invest_bot import cloud
    text = cloud.message(items, {}, NOW, None, 1_000_000, True, FX)
    assert f"↪️ SKIPPED (keeps the picks spread out)\n• {buys[2]} · " in text and "— 2 Technology picks already" in text
    assert cloud.calls(items) == sorted([b, "BUY"] for b in buys[:2])
    items = scanned(hs)  # no sector known, uncorrelated: all kept, n caps the count
    scan.diversify(items, hs, 3)
    assert sum(i["call"] == "BUY" for i in items) == 4 and not any(i.get("skipped") for i in items)
    ind = scan.run_scan({"CSPX.L": hist()}, {}, NOW, None, {}, FX)[0]
    assert ind["sector"] == "ETF" and scan.parse_info({"sector": "Technology"})["sector"] == "Technology"
