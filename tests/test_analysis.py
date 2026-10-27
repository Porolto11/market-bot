import numpy as np
import pandas as pd

from invest_bot import analysis


def _hist(closes):
    idx = pd.bdate_range("2024-01-01", periods=len(closes))
    return pd.DataFrame({"Close": list(closes)}, index=idx)


def _up(n=300):
    return _hist(100 * 1.001 ** np.arange(n))


def _down(n=300):
    return _hist(100 * 0.999 ** np.arange(n))


def test_uptrend_is_bullish():
    ind = analysis.indicators(_up())
    r = analysis.rate(ind)
    assert r["rating"] == "bullish" and r["chip"] == "Bullish"
    assert r["score"] >= 3
    assert ind["mom_12m"] > 0 and ind["dd_52w"] == 0


def test_downtrend_is_bearish():
    r = analysis.rate(analysis.indicators(_down()))
    assert r["rating"] == "bearish" and r["chip"] == "Bearish"
    assert r["score"] <= 1


def test_short_history_is_na():
    ind = analysis.indicators(_up(100))
    assert ind["sma200"] is None and ind["mom_6m"] is None and ind["mom_12m"] is None
    assert ind["sma50"] is not None
    r = analysis.rate(ind)
    assert r["rating"] == "n/a" and r["chip"] == "n/a"
    assert "not enough history" in r["reasons"]


def test_empty_history_does_not_crash():
    r = analysis.rate(analysis.indicators(pd.DataFrame({"Close": []})))
    assert r["rating"] == "n/a"
    assert analysis.rate(analysis.indicators(None))["rating"] == "n/a"


def test_rsi_known_value():
    # 7 changes of +2 and 7 of -1: avg gain 1, avg loss 0.5, RS 2 -> RSI 66.667
    closes = [100]
    for i in range(14):
        closes.append(closes[-1] + (2 if i % 2 == 0 else -1))
    assert abs(analysis.indicators(_hist(closes))["rsi14"] - 200 / 3) < 1e-6


def test_rsi_all_gains_is_100():
    assert analysis.indicators(_hist(range(100, 130)))["rsi14"] == 100.0


def test_indicator_values():
    closes = list(range(1, 301))
    ind = analysis.indicators(_hist(closes))
    assert ind["last"] == 300
    assert ind["sma50"] == np.mean(closes[-50:])
    assert ind["sma200"] == np.mean(closes[-200:])
    assert abs(ind["mom_3m"] - (300 / closes[-64] - 1)) < 1e-12
    assert abs(ind["mom_12m"] - (300 / closes[-253] - 1)) < 1e-12
    assert ind["dd_52w"] == 0
    exp = np.log(pd.Series(closes)).diff().iloc[-20:].std() * np.sqrt(252)
    assert abs(ind["vol_20d"] - exp) < 1e-12


def test_drawdown():
    closes = [100] * 100 + [200] * 100 + [150] * 100
    assert analysis.indicators(_hist(closes))["dd_52w"] == -0.25


def test_reasons_mention_numbers():
    r = analysis.rate({"last": 105.1, "sma50": 104, "sma200": 100, "rsi14": 50,
                       "mom_6m": 0.082, "mom_12m": 0.2})
    text = " | ".join(r["reasons"])
    assert "price 5.1% above 200-day average" in text
    assert "6-month momentum +8.2%" in text
    assert "12-month momentum +20.0%" in text


def test_bearish_reasons_and_rsi_adjustments():
    r = analysis.rate({"last": 95, "sma50": 99, "sma200": 100, "rsi14": 20,
                       "mom_6m": -0.05, "mom_12m": -0.1})
    assert "price 5.0% below 200-day average" in " | ".join(r["reasons"])
    assert r["score"] == 0
    hot = analysis.rate({"last": 110, "sma50": 105, "sma200": 100, "rsi14": 80,
                         "mom_6m": 0.1, "mom_12m": 0.2})
    assert hot["score"] == 3 and any("80" in s for s in hot["reasons"])
    dip = analysis.rate({"last": 101, "sma50": 99, "sma200": 100, "rsi14": 25,
                         "mom_6m": -0.1, "mom_12m": -0.1})
    assert dip["score"] == 2 and dip["rating"] == "neutral"


def test_market_regime():
    out = analysis.market_regime({"^GSPC": _up(), "^STOXX50E": _down()})
    assert out["US"]["rating"] == "bullish"
    assert out["EU"]["rating"] == "bearish"
    out = analysis.market_regime({"^GSPC": _up()})
    assert out["EU"]["rating"] == "n/a" and out["EU"]["reasons"]
