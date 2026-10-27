"""Multi-currency cash: balances per currency, buys paid from their own currency first, conversions from HUF at 1%."""
from datetime import datetime, timezone

import pytest

from invest_bot import cloud, commands, scan, state
from test_commands import download, info
from test_scan import FX, buy_item

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


def say(st, text):
    return commands.handle(st, text, NOW, download, info)


def test_book_keeps_cash_per_currency_and_old_entries_are_huf():
    log = [dict(t="cash", huf=100_000, at="a"), dict(t="cash", ccy="USD", amt=500, at="a"),
           dict(t="deposit", ccy="EUR", amt=300, at="a"), dict(t="withdraw", ccy="USD", amt=100, at="a"),
           dict(t="fx", ccy="USD", amt=50, huf=50 * 350 * 1.01, auto=True, at="a")]
    assert state.book(log)["cash"] == pytest.approx({"HUF": 100_000 - 17_675, "USD": 450, "EUR": 300})
    legacy = dict(t="buy", sym="NVDA", qty=1, price=100, ccy="USD", fx=350.0, fee=3.49, at="a")  # before wallets
    assert state.book([*log, legacy])["cash"]["HUF"] == pytest.approx(100_000 - 17_675 - 103.49 * 350)
    new = dict(legacy, wallet=True)
    assert state.book([*log, new])["cash"]["USD"] == pytest.approx(450 - 103.49)
    pence = dict(t="sell", sym="X.L", qty=10, price=500, ccy="GBp", fx=4.6, fee=0, wallet=True, at="a")
    log2 = [*log, dict(pence, t="buy"), pence]
    assert state.book(log2)["cash"]["GBP"] == pytest.approx(0)  # 50 GBP out, 50 GBP back
    assert state.total_huf({"HUF": 1000, "USD": 2, "DKK": 5}, {"USD": 350}) == 1700  # no DKK rate: counts 0


def test_bought_pays_from_the_usd_balance_first_and_converts_only_the_shortfall():
    st = state.empty()
    say(st, "/cash 1000000")
    assert "Cash ≈ 1 000 000 HUF · $300.00." in say(st, "/cash 300 USD")
    r = say(st, "/bought NVDA 2 100")  # 203.49 USD: all from the USD balance
    assert "Converted" not in r and st["log"][-1]["wallet"] is True
    assert state.book(st["log"])["cash"] == pytest.approx({"HUF": 1_000_000, "USD": 96.51})
    r = say(st, "/bought NVDA 2 100")  # 203.49 USD, 96.51 on hand: 106.98 USD converted from HUF at 350 x 1.01
    short = 203.49 - 96.51
    assert st["log"][-2] == dict(t="fx", ccy="USD", amt=pytest.approx(short), huf=pytest.approx(short * 353.5),
                                 auto=True, at=st["log"][-1]["at"])
    assert "Converted ≈ 37 817 HUF → $106.98 (1% ≈ 374 HUF)" in r
    assert state.book(st["log"])["cash"] == pytest.approx({"HUF": 1_000_000 - short * 353.5, "USD": 0})
    r = say(st, "/sold NVDA 1 120")  # credits USD, no conversion back
    assert state.book(st["log"])["cash"]["USD"] == pytest.approx(120 - 3.49) and "$116.51" in r
    say(st, "/undo")
    r = say(st, "/undo")  # the second buy and its conversion go together
    assert "and its conversion 37 817 HUF → $106.98" in r and st["log"][-1]["t"] == "buy"
    assert state.book(st["log"])["cash"] == pytest.approx({"HUF": 1_000_000, "USD": 96.51})


def test_eur_and_huf_trades_and_unknown_cash():
    st = state.empty()
    say(st, "/cash 0")
    say(st, "/deposit 100 EUR")
    r = say(st, "/bought MC.PA 1 500")  # 500 + 3.49 fee + 2 FR tax; 100 EUR on hand
    assert "Converted ≈ 163 818 HUF → €405.49" in r
    say(st, "/bought MOL.BD 1 3000")  # HUF: straight from HUF, never a conversion
    assert state.book(st["log"])["cash"]["HUF"] == pytest.approx(-405.49 * 404 - 3349)
    st2 = state.empty()
    say(st2, "/bought NVDA 1 100")  # cash unknown: no balances to pay from, nothing converted
    assert "wallet" not in st2["log"][-1] and len(st2["log"]) == 1 and "Cash not set yet" in say(st2, "/holdings")


def test_holdings_show_each_balance_and_the_huf_total():
    st = state.empty()
    say(st, "/cash 400000")
    say(st, "/cash 600 USD")
    say(st, "/cash 300 EUR")
    say(st, "/bought MOL.BD 10 3000")
    r = say(st, "/holdings")  # 600 USD x 350 + 300 EUR x 400 = 330 000 HUF on top of the HUF
    assert "💰 Cash ≈ 369 651 HUF · $600.00 · €300.00 (≈ 700 000 HUF in total)." in r
    assert "🧮 Total ≈ 732 651 HUF" in r  # + 10 MOL at 3300


def item(sym, limit, ccy, score=90, kind="stock"):
    return dict(buy_item(sym, limit, ccy, score), kind=kind, name=sym, valid_until="2026-10-28", flags=[])


def spend(picks):
    return sum(i["approx_huf"] for i in picks)


def test_plan_pays_from_balances_first_and_converts_the_rest_within_the_cash():
    wallet = {"HUF": 400_000, "USD": 600, "EUR": 300}  # ≈ 730 000 HUF in total
    total = 400_000 + 600 * 350 + 300 * 400
    us, eu, hu = item("US1", 150.0, "USD"), item("EU1.DE", 120.0, "EUR", 85), item("HU.BD", 5000.0, "HUF", 80)
    picks = scan.plan([us, eu, hu], total, FX, wallet=wallet)
    assert [i["symbol"] for i in picks] == ["US1", "EU1.DE", "HU.BD"]
    assert spend(picks) <= total * 0.99 + 1e-6
    usd_used = us["qty"] * 150 + max(us["qty"] * 150 * 0.0024, 3.49)
    assert us["own"] == pytest.approx(min(usd_used, 600 * 0.99))  # the USD balance pays first
    assert us["convert_huf"] == pytest.approx(max(0, usd_used - 594) * 350 * 1.01)
    assert hu["convert_huf"] == 0 and hu["fx_cost_huf"] == 0
    huf_used = (hu["approx_huf"] + us["convert_huf"] + eu["convert_huf"])
    assert huf_used <= 400_000 * 0.99 + 1e-6  # the HUF balance covers the HUF pick and every conversion
    lines = "\n".join(cloud.plan_lines(us, NOW, 1) + cloud.plan_lines(eu, NOW, 2) + cloud.plan_lines(hu, NOW, 3))
    assert "from your USD balance" in lines and "convert ≈ " in lines and "→ EUR (1% ≈ " in lines


def test_a_pick_paid_from_its_own_balance_has_no_fx_cost():
    a = item("US1", 100.0, "USD")
    scan.plan([a], 0, FX, wallet={"HUF": 0, "USD": 1000})
    assert a["plan"] and a["convert_huf"] == 0 and a["fx_cost_huf"] == 0 and a["own"] <= 990
    assert cloud.paid_line(a) == f"💵 pay from your USD balance (${a['own']:,.2f})".replace(",", " ")
    e = item("EU1.DE", 100.0, "EUR")  # no EUR and no HUF to convert from: it cannot be bought
    assert scan.plan([e], 0, FX, wallet={"HUF": 0, "USD": 1000}) == []
    usd_only = [item("US1", 100.0, "USD"), item("EU1.DE", 100.0, "EUR", 85)]
    assert [i["symbol"] for i in scan.plan(usd_only, 350_000, FX, wallet={"USD": 1000})] == ["US1"]


def test_header_shows_each_balance_and_the_total():
    wallet = {"HUF": 400_000, "USD": 600, "EUR": 300}
    text = cloud.message([], {}, NOW, None, 730_000, True, FX, wallet=wallet)
    assert "💰 Cash ≈ 400 000 HUF · $600.00 · €300.00 ≈ 730 000 HUF in total" in text
    assert "💰 Cash ≈ 400 000 HUF\n" in cloud.message([], {}, NOW, None, 400_000, True, FX, wallet={"HUF": 400_000})


def test_cash_huf_fallback_is_all_huf():
    a = item("US1", 100.0, "USD")
    scan.plan([a], 200_000, FX)  # no wallet: CASH_HUF / the budget, all of it HUF
    assert a["own"] == 0 and a["convert_huf"] > 0 and a["fx_cost_huf"] == pytest.approx(a["convert_huf"] / 101)


def test_dry_run_with_a_multi_currency_state_file(tmp_path):
    import json
    from test_cloud import go, args
    at = "2026-10-20T10:00:00+00:00"
    plain = tmp_path / "plain.json"
    plain.write_text(json.dumps({"log": [dict(t="cash", huf=400000, at=at), dict(t="cash", ccy="USD", amt=600, at=at),
                                         dict(t="cash", ccy="EUR", amt=300, at=at)]}), encoding="utf-8")
    code, sent, out = go(tmp_path, env={}, a=args(tmp_path, state_file=str(plain), force=True))
    assert code == 0 and not sent
    assert "💰 Cash ≈ 400 000 HUF · $600.00 · €300.00 ≈ " in out and " HUF in total" in out
    assert "from your USD balance" in out
