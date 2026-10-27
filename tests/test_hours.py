from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from invest_bot import hours

BUD = ZoneInfo("Europe/Budapest")


def dt(day, h, m=0):
    return datetime(2026, 10, day, h, m, tzinfo=BUD)


def test_xetra_open_midday():
    assert hours.is_tradeable("VVSM", dt(27, 12)) is True


def test_first_15_min_blocked():
    assert hours.is_tradeable("VVSM", dt(27, 9, 5)) is False


def test_last_15_min_blocked():
    # Xetra closes 17:30 Budapest time
    assert hours.is_tradeable("VVSM", dt(27, 17, 20)) is False


def test_us_open_evening():
    assert hours.is_tradeable("AAPL", dt(27, 16)) is True


def test_us_dst_gap():
    assert hours.is_tradeable("AAPL", dt(27, 14, 45)) is False


def test_weekend():
    assert hours.any_open(dt(25, 12)) is False


def test_any_open_weekday():
    assert hours.any_open(dt(27, 12)) is True


def test_naive_rejected():
    with pytest.raises(ValueError):
        hours.is_tradeable("VVSM", datetime(2026, 10, 27, 12))
    with pytest.raises(ValueError):
        hours.any_open(datetime(2026, 10, 27, 12))


def test_unknown_ticker():
    with pytest.raises(ValueError):
        hours.is_tradeable("NOPE", dt(27, 12))


def test_exactly_15_before_close_blocked():
    assert hours.is_tradeable("VVSM", dt(27, 17, 15)) is False
    assert hours.is_tradeable("VVSM", dt(27, 17, 14)) is True


def test_vhyl_lists_in_amsterdam():
    from invest_bot import prices
    assert hours.MARKET["VHYL"] == "XAMS" and prices.TICKERS["VHYL"] == "VHYL.AS"
