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
