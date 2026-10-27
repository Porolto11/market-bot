from datetime import date

from invest_bot import hours, rules


def test_huf_tick_rounding():
    assert rules._tick(3479.6, "HUF") == 3480.0 and rules._tick(512.34, "HUF") == 512.3
    assert rules._tick(1.23456, "EUR") == 1.23
    assert rules._huf(1234567) == "1 234 567"


def test_add_trading_days_skips_holidays():
    assert hours.add_trading_days("CSPX", date(2026, 12, 18), 10) == date(2027, 1, 6)  # Christmas, Boxing Day, New Year
    assert hours.add_trading_days("VVSM", date(2026, 10, 24), 1) == date(2026, 10, 26)


def test_dip_deadline_from_weekend_lands_on_session():
    d = rules.dip_deadline("CSPX", date(2026, 10, 24))
    assert d.isoformat() == "2026-11-06" and hours._calendar("XLON").is_session(d.isoformat())


def test_buy_qty_keeps_the_fx_spread_aside_for_non_huf():
    assert rules.buy_qty(1_000_000, 1_000_000, 50, 100, "USD") == 198
    assert rules.buy_qty(1_000_000, 1_000_000, 50, 1, "HUF") == 19_952


def test_dip_rule():
    calm = {"vol_20d": 0.1, "rsi14": 50}
    act, limit, kind = rules.dip_rule(calm, "bullish", 100.0, "USD")
    assert (act, limit, kind) == ("buy_now", 100.2, "buy")
    act, limit, kind = rules.dip_rule(dict(calm, rsi14=80), "bullish", 100.0, "USD")
    assert (act, kind) == ("wait_for_dip", "dip") and limit == 97.0
    assert rules.dip_rule(calm, "bullish", 100.0, "USD", {"valid_until": "2026-10-01", "limit": 95}, "2026-10-26")[2] == "expired"
    assert rules.dip_rule(calm, "bullish", 100.0, "USD", {"valid_until": "2026-11-01", "limit": 95}, "2026-10-26")[1:] == (95.0, "open")
    assert rules.INSTRUMENTS["MOL"][1] == "HUF"


def test_price_format_has_no_exponent_or_rounding():
    assert rules._px(12345.67) == "12345.67" and rules._px(100.5) == "100.5" and rules._px(3000.0) == "3000"
    assert rules._px(1234567.891) == "1234567.891" and rules._px(0.1234) == "0.1234"
