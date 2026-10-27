from datetime import datetime, timezone

import pytest

from invest_bot import cloud, fmt, state, tax
from test_cloud import TOKEN, TUE, go

RATES = {"USD": 350.0, "EUR": 400.0}


def item(sym, score, call=None, last=100.0, kind="stock", ccy="USD", one=None):
    return dict(symbol=sym, score=score, call=call, last=last, kind=kind, ccy=ccy, limit=last,
                one_share_huf=one)


def pf(*holdings):
    return dict(cash_huf=20000, holdings=[dict(symbol=s, qty=q, avg_cost_huf=a) for s, q, a in holdings])


PICK = item("NVDA", 90, "BUY", last=200.0, one=72000)


def test_tax_on_offsets_this_years_losses():
    assert round(tax.tax_on(100000, 0)) == 15000 and tax.RATE == 0.15  # 15% SZJA, no szocho
    assert round(tax.tax_on(100000, -60000)) == 6000  # 60 000 of losses already taken offset part of it
    assert tax.tax_on(100000, -200000) == 0 and tax.tax_on(-5000, 0) == 0
    assert round(tax.tax_on(-5000, 50000)) == -750  # a loss with gains taken: saves tax


def test_ytd_is_fifo_per_year():
    log = [dict(t="buy", sym="A", qty=2, price=100, ccy="HUF", fx=1.0, fee=0, at="2025-03-01T10:00:00+00:00"),
           dict(t="sell", sym="A", qty=1, price=150, ccy="HUF", fx=1.0, fee=0, at="2025-06-01T10:00:00+00:00"),
           dict(t="sell", sym="A", qty=1, price=80, ccy="HUF", fx=1.0, fee=0, at="2026-02-01T10:00:00+00:00")]
    assert tax.ytd(log, 2025) == 50 and tax.ytd(log, 2026) == -20


def test_switch_sells_a_loser_first_and_shows_tax_and_fees():
    items = [PICK, item("AMD", 50, "HOLD", last=150.0), item("OTP.BD", 40, "HOLD", last=40000.0, ccy="HUF")]
    out = tax.switches(items, pf(("AMD", 5, 60000), ("OTP.BD", 10, 20000)), 20000, RATES, 0)
    assert len(out) == 1 and out[0][1:] == ("AMD", "NVDA")  # the AMD loss beats OTP's lower score
    text = out[0][0]
    assert text.startswith("Sell 5× AMD (50) → buy 3× NVDA (90)\n   💸 Tax now ≈ 0 HUF · fees ≈ 2 440 HUF · the ")
    assert text.endswith("HUF loss offsets this year's gains")  # no gains this year: no tax saved
    loss = 300000 - (5 * 150 * 350 - 3.49 * 350)
    with_gains = tax.switches(items, pf(("AMD", 5, 60000), ("OTP.BD", 10, 20000)), 20000, RATES, 100000)[0][0]
    assert with_gains.endswith(f"loss offsets this year's gains, saving ≈ {fmt.approx(0.15 * loss)} HUF tax")


def test_switch_with_a_gain_pays_tax_after_this_years_losses():
    items = [PICK, item("OTP.BD", 40, "HOLD", last=40000.0, ccy="HUF")]
    out = tax.switches(items, pf(("OTP.BD", 10, 30000)), 20000, RATES, -50000)
    # proceeds 400 000 - 960 fee, gain 99 040, minus 50 000 of losses taken: tax 15% of 49 040
    assert "Sell 10× OTP (40) → buy 5× NVDA" in out[0][0] and "Tax now ≈ 7 360 HUF" in out[0][0]  # 7 356


def test_no_switch_for_etfs_winners_small_gaps_enough_cash_or_no_edge():
    def one(items, holdings, cash=20000):
        return tax.switches([PICK, *items], pf(*holdings), cash, RATES, 0)
    assert one([item("CSPX.L", 20, "HOLD", kind="etf", last=600.0)], [("CSPX.L", 2, 200000)]) == []
    assert one([item("AMD", 64, "HOLD", last=150.0)], [("AMD", 5, 60000)]) == []  # gap 26 but a winner
    assert one([item("AMD", 64, "SELL", last=150.0)], [("AMD", 5, 60000)])  # a winner with a SELL call may go
    assert one([item("AMD", 70, "SELL", last=150.0)], [("AMD", 5, 60000)]) == []  # gap 20
    assert one([item("AMD", 50, "HOLD", last=150.0)], [("AMD", 5, 60000)], cash=80000) == []  # cash buys 1 share
    assert one([item("AMD", 50, "HOLD", last=150.0)], [("AMD", 5, 10000)]) == []  # big gain: tax beats the edge
    assert tax.switches([PICK], None, 20000, RATES, 0) == [] and tax.switches([PICK], pf(), None, RATES, 0) == []


def test_at_most_two_switches():
    picks = [dict(PICK, symbol=s) for s in ("A", "B", "C")]
    held = [item(s, 30, "HOLD", last=150.0) for s in ("X", "Y", "Z")]
    out = tax.switches([*picks, *held], pf(*((s, 5, 60000) for s in "XYZ")), 20000, RATES, 0)
    assert [(x, y) for _, x, y in out] == [("X", "A"), ("Y", "B")]


def test_scan_message_tracks_the_switch(tmp_path):
    st = state.empty()
    st["log"] = [dict(t="cash", huf=200000, at="2026-09-01T10:00:00+00:00"),
                 dict(t="buy", sym="AAPL", qty=3, price=150, ccy="USD", fx=350.0, fee=3.49, at="2026-09-01T10:00:00+00:00"),
                 dict(t="cash", huf=20000, at="2026-10-01T10:00:00+00:00")]
    state.save(st, str(tmp_path / "state" / "state.enc"), TOKEN)
    _, sent, log = go(tmp_path)
    text = sent[0][2]
    assert "🔁 SWITCH (tax-aware, rough estimate)\nSell 3× AAPL" in text and "→ buy 2× NVDA" in text
    assert "🎯 TODAY'S MOVE\n🔁 Sell 3× AAPL (" in text and "\nWhy: NVDA scores far higher" in text
    calls = state.load(str(tmp_path / "state" / "state.enc"), TOKEN)["calls"]
    assert any(c["sym"] == "AAPL>NVDA" and c["call"] == "SWITCH" for c in calls) and "AAPL" not in log
    assert "calls: 1 BUY, 0 WAIT, 1 WAIT_FOR_DIP, 0 SELL, 1 switch" in log  # counts only, no symbols
    assert not any(sym in log for sym in ("NVDA", "AMD", "AAPL"))


def test_state_file_only_without_token(tmp_path):
    import json
    p = tmp_path / "plain.json"
    p.write_text(json.dumps(dict(log=[dict(t="cash", huf=20000, at="2026-10-01T10:00:00+00:00")])))
    from test_cloud import args
    code, sent, log = go(tmp_path, env={}, a=args(tmp_path, state_file=str(p)))
    assert code == 0 and "💰 Cash ≈ 20 000 HUF" in log and not (tmp_path / "state" / "state.enc").exists()
    code, _, log = go(tmp_path, a=args(tmp_path, state_file=str(p)))
    assert code == 2 and "only allowed without TELEGRAM_TOKEN" in log


def traded():
    from test_commands import say
    st = state.empty()
    for c in ("/cash 200000", "/bought MOL.BD 10 3000", "/bought NVDA 2 150", "/sold MOL.BD 5 4000"):
        say(st, c)
    return st


def test_tax_command_realised_tax_and_unrealised():
    from test_commands import say
    r = say(traded(), "/tax")
    assert r.startswith("🧾 Tax 2026\n💵 Realised P/L ≈ +4 476 HUF (FIFO, fees included)\n")
    assert "🏛️ Estimated tax ≈ 671 HUF (15% SZJA, no szocho on exchange trades)" in r
    assert "💵 US stocks (NVDA): dividends: 15% SZJA + 13% szocho (capped), US withholds 30%." in r
    assert "except trades recorded without one (Yahoo FX, approximate)" in r  # MNB unreachable in tests
    assert "📊 Unrealised P/L ≈ −20 896 HUF" in r and "🔴 NVDA −22 222 HUF (−20.9%)" in r and "🟢 MOL +1 326" in r
    assert "No holdings with a price" in say(state.empty(), "/tax") and "/tax" in say(state.empty(), "/help")


def test_december_helper_dates_and_offset():
    from datetime import datetime, timezone
    log, last = traded()["log"], {"NVDA": 120.0, "MOL.BD": 3300.0}
    dec = tax.december(log, datetime(2026, 12, 7, 6, 45, tzinfo=timezone.utc), last, RATES)
    assert dec.startswith("🧾 December tax check: realised gains 2026 ≈ +4 476 HUF, tax due ≈ 671 HUF (15%).")
    assert "🔴 At a loss now: NVDA −22 222 HUF." in dec and "offset 4 476 HUF of gains and save ≈ 671 HUF" in dec
    assert "act by about Dec 20" in dec and "no wash-sale rule" in dec and "check with your tax advisor" in dec
    for d in ((2026, 11, 30), (2026, 12, 21)):
        assert tax.december(log, datetime(*d, tzinfo=timezone.utc), last, RATES) is None
    assert tax.december([], datetime(2026, 12, 7, tzinfo=timezone.utc), last, RATES) is None
    no_gain = [e for e in log if e["t"] != "sell"]
    assert "saves no tax now" in tax.december(no_gain, datetime(2026, 12, 7, tzinfo=timezone.utc), last, RATES)


def test_december_monday_summary_has_the_tax_section(tmp_path):
    from datetime import datetime, timezone
    from test_weekly import holdings_state
    holdings_state(tmp_path)
    _, sent, _ = go(tmp_path, now=datetime(2026, 12, 7, 6, 45, tzinfo=timezone.utc))
    assert "December tax check: realised gains 2026" in sent[1][2]
    _, sent, _ = go(tmp_path, now=datetime(2026, 12, 21, 6, 45, tzinfo=timezone.utc))
    assert "December tax check" not in sent[-1][2]


def trade(t, qty, price, at, fx=1.0, fee=0.0, ccy="HUF", **kw):
    return dict(t=t, sym="A", qty=qty, price=price, ccy=ccy, fx=fx, fee=fee, at=f"{at}T10:00:00+00:00", **kw)


def test_losses_carry_two_years_oldest_first_and_once():
    log = [trade("buy", 10, 100, "2023-01-02"),
           trade("sell", 1, 50, "2023-06-01"),     # 2023: -50
           trade("sell", 1, 70, "2024-06-01"),     # 2024: -30
           trade("sell", 1, 160, "2025-06-01"),    # 2025: +60, uses 2023's 50, then 10 of 2024's 30
           trade("sell", 1, 150, "2026-06-01"),    # 2026: +50, 2024's 20 left (2023 has expired anyway)
           trade("sell", 1, 300, "2027-06-01")]    # 2027: +200, 2024's loss is too old now
    assert tax.nets(log) == {2023: -50, 2024: -30, 2025: 60, 2026: 50, 2027: 200}
    assert tax.carry(log, 2025) == 80 and tax.base(log, 2025) == -20  # more loss than gain: nothing taxed
    assert tax.carry(log, 2026) == 20 and tax.base(log, 2026) == 30
    assert tax.carry(log, 2027) == 0 and tax.base(log, 2027) == 200
    assert tax.base(log, 2024) == -30  # a loss year stays a loss
    r = tax.report(log[:3], datetime(2024, 12, 1, tzinfo=timezone.utc), {}, {})
    assert tax.DECLARE in r and "Estimated tax ≈ 0 HUF" in r
    r = tax.report(log[:5], datetime(2026, 12, 1, tzinfo=timezone.utc), {}, {})
    assert "📉 Losses from 2024–2025 offset ≈ 20 HUF (if declared)" in r and "Estimated tax ≈ 4 HUF" in r
    assert "at the official MNB rates of the buy and sell days" in r and tax.DECLARE not in r
    dec = tax.december(log[:3], datetime(2024, 12, 2, tzinfo=timezone.utc), {}, {})
    assert tax.DECLARE in dec


def test_tax_pl_uses_mnb_rates_and_fees_are_deductible():
    log = [trade("buy", 2, 100, "2026-03-02", fx=360.0, fee=3.49, ccy="USD", fx_mnb=350.0),
           trade("sell", 2, 120, "2026-09-01", fx=330.0, fee=3.49, ccy="USD", fx_mnb=340.0)]
    b = state.book(log)
    assert b["taxed"][0] == pytest.approx((240 - 3.49) * 340 - (200 + 3.49) * 350)  # cost incl. buy fee, net proceeds
    assert b["realised"][0] == pytest.approx((240 - 3.49) * 330 - (200 + 3.49) * 360)  # cash view: Yahoo FX
    assert tax.ytd(log, 2026) == pytest.approx(b["taxed"][0]) and not tax.approx(log)
    assert tax.approx([dict(log[0], fx_mnb=None)])


def test_no_switch_away_from_a_holding_without_a_fresh_close():
    items = [PICK, item("AMD", 50, "HOLD", last=150.0)]
    assert tax.switches(items, pf(("AMD", 5, 60000)), 20000, RATES, 0)
    items[1]["stale"] = True
    assert tax.switches(items, pf(("AMD", 5, 60000)), 20000, RATES, 0) == []
