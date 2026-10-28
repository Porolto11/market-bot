import pytest

from invest_bot.fees import commission


def test_min_fee_eur(): assert commission(220, "EUR") == 3.49
def test_pct_fee_eur(): assert commission(5000, "EUR") == pytest.approx(12.0)
def test_min_fee_huf(): assert commission(14460, "HUF") == 349
def test_usd(): assert commission(695.45, "USD") == 3.49
def test_unknown_ccy(): pytest.raises(ValueError, commission, 100, "GBP")


def test_market_by_exchange_suffix():
    from invest_bot import fees
    assert [fees.market(s) for s in ("NVDA", "BRK-B", "NOVO-B.CO", "ASML.AS", "SAP.DE", "MC.PA", "CSPX.L",
                                     "MOL.BD", "X.MC", "X.MI", "X.SW", "X.IR", "X.TO", "^GSPC")] == [
        "US", "US", "DK", "NL", "DE", "FR", "UK", "BÉT", "ES", "IT", "CH", "IE", None, None]


def test_every_universe_symbol_has_a_market_with_a_minimum():
    from invest_bot import fees, scan
    for s in scan.UNIVERSE:
        assert fees.market(s) in fees.MARKET_MIN, s


def test_minimum_fee_per_market_in_the_trade_currency():
    from invest_bot import scan
    fx = {"USD": 350.0, "EUR": 400.0, "GBP": 460.0, "DKK": 53.0}
    assert scan._fee(100, "USD", fx, "NVDA") == 3.49
    assert scan._fee(100, "HUF", fx, "MOL.BD") == 349
    assert scan._fee(100, "EUR", fx, "MC.PA") == 3.49
    assert scan._fee(100, "DKK", fx, "NOVO-B.CO") == pytest.approx(11.99 * 400 / 53)  # 11.99 EUR in DKK
    assert scan._fee(100, "EUR", fx, "X.MC") == 11.99  # Madrid
    assert scan._fee(100, "USD", fx, "CSPX.L") == pytest.approx(3.49 * 400 / 350)  # LSE: 3.49 EUR, in USD
    assert scan._fee(100, "GBp", fx, "X.L") == pytest.approx(3.49 * 400 / 4.6)  # in pence
    assert scan._fee(10_000, "DKK", fx, "NOVO-B.CO") == pytest.approx(11.99 * 400 / 53)  # 0.24% is less
    assert scan._fee(1_000_000, "DKK", fx, "NOVO-B.CO") == pytest.approx(2400)  # 0.24% is more
    assert scan._fee(100, "CHF", {}, "X.SW") == 0.24  # no FX for the minimum: the rate alone


def test_novo_needs_more_shares_for_a_fee_under_1_percent():
    from invest_bot import scan
    fx = {"EUR": 400.0, "DKK": 53.0}
    novo = dict(symbol="NOVO-B.CO", ccy="DKK", limit=400.0)
    assert scan.min_qty(novo, fx) == 23  # 11.99 EUR ≈ 90.5 DKK; / 1% = 9 050 DKK; / 400 DKK a share


def test_buy_side_taxes_by_market_and_etfs_are_exempt():
    from invest_bot import fees
    assert [fees.buy_tax(s) for s in ("MC.PA", "X.MC", "X.MI", "X.L", "X.IR", "NVDA", "SAP.DE", "MOL.BD")] == [
        0.004, 0.002, 0.002, 0.005, 0.01, 0.0, 0.0, 0.0]
    assert fees.buy_tax("CSPX.L", "etf") == 0.0 and fees.buy_tax("MC.PA", "etf") == 0.0
    assert fees.tax_label("AIR.PA") == "FR tax 0.4%" and fees.tax_label("X.L") == "UK stamp duty 0.5%"


def test_buy_tax_in_the_sizing_and_the_plan():
    from datetime import datetime, timezone
    from invest_bot import cloud, scan
    fx = {"USD": 350.0, "EUR": 400.0}
    q, approx, one = scan.size(1_000_000, 100.0, "EUR", fx, "MC.PA", "stock")
    assert one == pytest.approx((100 * 1.004 + 3.49) * 400 * 1.01)
    assert approx == pytest.approx((q * 100 * 1.004 + max(q * 100 * 0.0024, 3.49)) * 400)
    lvmh = dict(symbol="MC.PA", name="LVMH", kind="stock", call="BUY", ccy="EUR", limit=100.0, score=90,
                valid_until="2026-10-28", flags=[])
    picks = scan.plan([lvmh], 1_000_000, fx)
    value, fee, tax = scan.cost(lvmh, lvmh["qty"], fx, spread=False)  # the plan shows market-rate amounts
    assert picks and tax == pytest.approx(value * 0.004) and lvmh["tax_huf"] == pytest.approx(tax)
    assert lvmh["approx_huf"] == pytest.approx(value + fee + tax + lvmh["fx_cost_huf"]) and lvmh["approx_huf"] <= 990_000
    total = cloud.plan_total([lvmh], lvmh["approx_huf"], 1_000_000)  # the tax is part of the fees line
    assert f"fees ≈ {cloud.fmt.k(fee + tax + lvmh['fx_cost_huf'])}" in total
    etf = dict(lvmh, symbol="CNDX.AS", kind="etf")
    scan.plan([etf], 1_000_000, fx)
    assert etf["tax_huf"] == 0 and "tax" not in cloud.plan_lines(etf, datetime(2026, 10, 27, tzinfo=timezone.utc), 1)[0]
