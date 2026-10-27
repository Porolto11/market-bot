import pytest

from invest_bot.fees import commission


def test_min_fee_eur(): assert commission(220, "EUR") == 3.49
def test_pct_fee_eur(): assert commission(5000, "EUR") == pytest.approx(12.0)
def test_min_fee_huf(): assert commission(14460, "HUF") == 349
def test_usd(): assert commission(695.45, "USD") == 3.49
def test_unknown_ccy(): pytest.raises(ValueError, commission, 100, "GBP")
