from datetime import datetime, timezone

import numpy as np
import pandas as pd

from invest_bot import cloud, hours, prices, scan
from test_scan import FX, NOW, hist, item, strong_fund, universe

TUE_MORNING = datetime(2026, 10, 27, 6, 50, tzinfo=timezone.utc)


def frame(end, n=5, last=None):
    idx = pd.bdate_range(end=end, periods=n)
    close = [100.0 + i for i in range(n)]
    if last is not None:
        close[-1] = last
    return pd.DataFrame({"Close": close, "Open": close}, index=idx)


def test_mic_for():
    assert hours.mic_for("SAP.DE") == "XETR" and hours.mic_for("MOL.BD") == "XBUD" and hours.mic_for("CSPX.L") == "XLON"
    assert hours.mic_for("AAPL") == "XNYS" and hours.mic_for("BRK-B") == "XNYS" and hours.mic_for("^GSPC") == "XNYS"
    assert hours.mic_for("EURHUF=X") is None and hours.mic_for("0700.HK") is None


def test_nan_last_close_is_dropped_and_the_symbol_is_stale():
    h, stale = prices.fresh({"SAP.DE": frame("2026-10-26", last=np.nan)}, TUE_MORNING)
    assert h["SAP.DE"].index[-1].date().isoformat() == "2026-10-23" and h["SAP.DE"]["Close"].iloc[-1] == 103.0
    assert stale == {"SAP.DE": "2026-10-23"}


def test_partial_today_bar_is_dropped_while_the_session_is_open():
    at_1550 = datetime(2026, 10, 27, 15, 50, tzinfo=timezone.utc)  # NYSE open, Xetra open till 16:30 UTC
    h, stale = prices.fresh({"AAPL": frame("2026-10-27"), "SAP.DE": frame("2026-10-27")}, at_1550)
    assert h["AAPL"].index[-1].date().isoformat() == "2026-10-26" and len(h["AAPL"]) == 4
    assert h["SAP.DE"].index[-1].date().isoformat() == "2026-10-26" and stale == {}
    after = datetime(2026, 10, 27, 21, 15, tzinfo=timezone.utc)  # the evening scan: every market closed
    h, stale = prices.fresh({"AAPL": frame("2026-10-27"), "SAP.DE": frame("2026-10-27")}, after)
    assert len(h["AAPL"]) == 5 and len(h["SAP.DE"]) == 5 and stale == {}


def test_missing_session_is_stale_but_a_holiday_is_not():
    # 23 Oct is a BÉT holiday: 22 Oct is the session before 26 Oct
    _, stale = prices.fresh({"MTELEKOM.BD": frame("2026-10-22"), "OTP.BD": frame("2026-10-26")}, TUE_MORNING)
    assert stale == {"MTELEKOM.BD": "2026-10-22"}
    _, stale = prices.fresh({"OTP.BD": frame("2026-10-22")}, datetime(2026, 10, 26, 6, 50, tzinfo=timezone.utc))
    assert stale == {}


def test_dst_changes_the_close_in_utc():
    # US: EDT till 1 Nov (close 20:00 UTC), EST after (21:00 UTC)
    fri = datetime(2026, 10, 30, 20, 30, tzinfo=timezone.utc)
    mon = datetime(2026, 11, 2, 20, 30, tzinfo=timezone.utc)
    assert len(prices.fresh({"AAPL": frame("2026-10-30")}, fri)[0]["AAPL"]) == 5
    assert prices.fresh({"AAPL": frame("2026-11-02")}, mon)[0]["AAPL"].index[-1].date().isoformat() == "2026-10-30"
    # Xetra: CEST till 25 Oct (close 15:30 UTC), CET after (16:30 UTC)
    at = datetime(2026, 10, 23, 16, 0, tzinfo=timezone.utc)
    assert len(prices.fresh({"SAP.DE": frame("2026-10-23")}, at)[0]["SAP.DE"]) == 5
    at = datetime(2026, 10, 26, 16, 0, tzinfo=timezone.utc)
    assert prices.fresh({"SAP.DE": frame("2026-10-26")}, at)[0]["SAP.DE"].index[-1].date().isoformat() == "2026-10-23"


def test_fx_keeps_its_live_bar_and_loses_nan_rows():
    h, stale = prices.fresh({"EURHUF=X": frame("2026-10-27", last=np.nan)}, TUE_MORNING)
    assert h["EURHUF=X"]["Close"].iloc[-1] == 103.0 and stale == {}


def test_stale_symbol_abstains_from_buy_and_sell():
    hs = universe({"GOOD": hist(), "BEAR": hist(up=0.985, down=1.01)})
    funds = {"GOOD": strong_fund(scan.features(hs["GOOD"])["last"])}
    assert item(scan.run_scan(hs, funds, NOW, 1e6, {"BEAR": 3}, FX), "GOOD")["call"] == "BUY"
    items = scan.run_scan(hs, funds, NOW, 1e6, {"BEAR": 3}, FX, stale={"GOOD": "2026-10-22", "BEAR": "2026-10-22"})
    g, b = item(items, "GOOD"), item(items, "BEAR")
    assert g["call"] is None and g["qty"] is None and g["stale"] and "stale data (last close 2026-10-22)" in g["flags"]
    assert b["call"] is None and "No fresh close" in b["reason"]
    text = cloud.message(items, {}, NOW, {"holdings": [dict(symbol="BEAR", qty=3, avg_cost_huf=1.0)]}, 1e6, True, FX)
    assert "⚠️ 2 without a fresh close (no calls): GOOD, BEAR" in text and "· ⚠️ stale data" in text
    assert "🟢 BUY" not in text and "🔴 SELL" not in text
