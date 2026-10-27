from datetime import datetime, timezone

import pandas as pd

from invest_bot import state, watch
from test_cloud import TOKEN, go, market, saved

NOW = datetime(2026, 10, 27, 6, 45, tzinfo=timezone.utc)
RATES = {"USD": 350.0}


def closes(values, end="2026-10-23"):
    return pd.DataFrame({"Close": values}, index=pd.bdate_range(end=end, periods=len(values)))


def buy(sym, qty, at, t="buy"):
    return dict(t=t, sym=sym, qty=qty, price=100, ccy="USD", fx=350.0, fee=3.49, at=at)


def pf(avg=30000):
    return dict(holdings=[dict(symbol="AMD", qty=5, avg_cost_huf=avg)])


def test_first_buy_of_the_position_held_now():
    log = [buy("AMD", 5, "2026-01-05T10:00:00"), buy("AMD", 5, "2026-02-02T10:00:00", "sell"),
           buy("AMD", 2, "2026-03-02T10:00:00"), buy("AMD", 1, "2026-04-01T10:00:00"), buy("X", 1, "2026-01-05T10:00:00"),
           buy("X", 1, "2026-01-06T10:00:00", "sell"), dict(t="cash", huf=1, at="2026-01-01")]
    assert watch.first_buys(log) == {"AMD": datetime(2026, 3, 2).date()}


def test_trailing_stop_uses_the_peak_since_the_first_buy():
    h = {"AMD": closes([300, 200, 220, 190, 186.9])}  # 300 was before the buy
    log = [buy("AMD", 5, "2026-10-20T10:00:00")]
    out = watch.stops(pf(), log, h, [], RATES, NOW, [])
    assert out == [("stop|AMD|2026-W44", "🛑 AMD is 15.0% below its peak since you bought (peak $220, now $186.9) "
                    "— consider selling.")]
    assert watch.stops(pf(), log, {"AMD": closes([300, 200, 220, 190, 187.1])}, [], RATES, NOW, []) == []
    assert watch.stops(pf(), log, h, [], RATES, NOW, ["stop|AMD|2026-W44"]) == []  # once a week


def test_hard_stop_vs_average_cost_without_a_trade_log():
    h = {"AMD": closes([100, 100, 100, 100, 85])}  # 85 x 350 = 29 750 HUF vs avg 35 000: -15%
    out = watch.stops(pf(35000), [], h, [dict(symbol="AMD", ccy="USD")], RATES, NOW, [])
    assert out[0][1] == ("🛑 AMD is 15.0% below your average cost (avg 35 000 HUF, now 29 750 HUF a share) — hard stop, "
                         "consider selling.")
    assert watch.stops(pf(34000), [], h, [], RATES, NOW, []) == []


def test_stop_alert_is_sent_once_a_week(tmp_path):
    st = state.empty()
    st["log"] = [dict(t="cash", huf=100000, at="2026-09-01T10:00:00+00:00"),
                 buy("AAPL", 2, "2026-09-01T10:00:00+00:00")]
    state.save(st, str(tmp_path / "state" / "state.enc"), TOKEN)
    h = market()
    drop = h["AAPL"].copy()
    drop.iloc[-1, 0] = drop["Close"].iloc[-30:].max() * 0.8
    h["AAPL"] = drop
    from io import StringIO
    from invest_bot import cloud
    from test_cloud import args, info
    sent = []

    def run():
        cloud.run(args(tmp_path), env={"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}, now=NOW,
                  download_fn=lambda s, period: h, info_fn=info, send_fn=lambda t, c, x: sent.append(x),
                  out=StringIO(), news_fn=lambda s: [])
    run()
    assert "🚨 ALERTS\n📉 AAPL −20.0% on Mon 26 Oct\n🛑 AAPL is 20.0% below its peak since you bought" in sent[0]
    assert "🎯 TODAY'S MOVE\n🛑 Sell AAPL — it hit a stop\nWhy: it fell 15% or more, see 🚨 ALERTS." in sent[0]
    assert "stop|AAPL|2026-W44" in saved(tmp_path)["alerts"]
    run()
    assert len(sent) == 1  # same week, same calls: nothing new


def test_core_etf_dip_and_big_dip_then_only_deeper_or_20_days_later():
    flat = [100.0] * 260
    h = {"CSPX.L": closes(flat + [90.9]), "CNDX.AS": closes(flat + [84.0]), "VWCE.DE": closes(flat + [93.0]),
         "IWDA.AS": closes(flat + [80.0])}
    out, last = watch.dips(h, NOW, {})
    assert out == [("dip|CSPX.L|2026-10-27", "🛒 CSPX is 9.1% below its 52-week high — good moment to add if you have cash."),
                   ("dip|CNDX.AS|2026-10-27", "🛒 Big dip: CNDX is 16.0% below its 52-week high — good moment to add if "
                    "you have cash.")]
    assert last == {"CSPX.L": dict(d="2026-10-27", dd=-0.091), "CNDX.AS": dict(d="2026-10-27", dd=-0.16)}
    prev = {"CSPX.L": dict(d="2026-10-01", dd=-0.05), "CNDX.AS": dict(d="2026-10-13", dd=-0.12),
            "VVSM.DE": dict(d="2026-10-01", dd=-0.2)}
    out, last = watch.dips(dict(h, **{"VVSM.DE": closes(flat + [79.0])}), NOW, prev)
    # CSPX 4.1 points deeper, 18 trading days: no; CNDX 4 points deeper: no; VVSM 1 point deeper: no
    assert out == [] and last == {k: v for k, v in prev.items()}
    out, last = watch.dips(dict(h, **{"VVSM.DE": closes(flat + [75.0])}), NOW, prev)  # -20% -> -25%
    assert [k for k, _ in out] == ["dip|VVSM.DE|2026-10-27"] and last["VVSM.DE"]["dd"] == -0.25
    old = {"CSPX.L": dict(d="2026-09-29", dd=-0.091)}  # 20 trading days ago
    assert [k for k, _ in watch.dips(h, NOW, old)[0]][0] == "dip|CSPX.L|2026-10-27"
    out, last = watch.dips({"CSPX.L": closes(flat + [97.0])}, NOW, old)  # recovered: forgotten
    assert out == [] and last == {}
    assert watch.dips({"CSPX.L": closes([100.0] * 20 + [80.0])}, NOW, {}) == ([], {})  # under a year of history


def test_dip_alert_goes_into_alerts_and_its_depth_into_state(tmp_path):
    h = market()
    c = h["CSPX.L"].copy()
    c.iloc[-1, 0] = c["Close"].max() * 0.9
    h["CSPX.L"] = c
    from io import StringIO
    from invest_bot import cloud
    from test_cloud import args, info
    sent = []

    def run():
        cloud.run(args(tmp_path), env={"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}, now=NOW,
                  download_fn=lambda s, period: h, info_fn=info, send_fn=lambda t, c, x: sent.append(x),
                  out=StringIO(), news_fn=lambda s: [])
    run()
    assert "\n🛒 CSPX is 10.0% below its 52-week high" in sent[0]
    assert saved(tmp_path)["dips"] == {"CSPX.L": dict(d="2026-10-27", dd=-0.1)}
    run()
    assert len(sent) == 1  # same depth: no new alert, calls unchanged


def test_fx_line_comments_only_in_the_top_or_bottom_20_percent():
    window = [380.0 + i * 0.2 for i in range(62)]  # 380.0 .. 392.2
    cheap = watch.fx_lines({"EURHUF=X": closes(window + [382.0]), "USDHUF=X": closes([350.0] * 70)})
    assert cheap[0].startswith("EUR/HUF 382.0 (3-month avg 386.0) — cheaper than 82% of the last 3 months (good time")
    assert cheap[1] == "USD/HUF 350.0 (3-month avg 350.0)"  # in the middle: no comment
    dear = watch.fx_lines({"EURHUF=X": closes(window + [391.0])})
    assert dear == ["EUR/HUF 391.0 (3-month avg 386.2) — pricier than 89% of the last 3 months, consider waiting"]
    assert watch.fx_lines({"EURHUF=X": closes(window[:50] + [386.0])}) == ["EUR/HUF 386.0 (3-month avg 384.9)"]
    assert watch.fx_lines({"EURHUF=X": closes([400.0] * 5)}) == []


def test_monday_summary_has_the_fx_lines(tmp_path):
    from test_cloud import MON
    _, sent, _ = go(tmp_path, now=MON)
    assert "\nEUR/HUF " not in sent[1][2]  # the test market has 5 days of FX: too short
    h = market()
    h["EURHUF=X"] = closes([400.0] * 62 + [390.0])
    from io import StringIO
    from invest_bot import cloud
    from test_cloud import args, info
    out = []
    cloud.run(args(tmp_path, force=True), env={"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}, now=MON,
              download_fn=lambda s, period: h, info_fn=info, send_fn=lambda t, c, x: out.append(x), out=StringIO(),
              news_fn=lambda s: [])
    assert "EUR/HUF 390.0 (3-month avg 399.8) — cheaper than 100% of the last 3 months (good time to convert)" in out[1]


def test_trailing_stop_on_a_strong_holding_says_hold():
    h = {"AMD": closes([300, 200, 220, 190, 186.9])}
    log = [buy("AMD", 5, "2026-10-20T10:00:00")]
    strong = [dict(symbol="AMD", ccy="USD", score=89, rating="bullish")]
    line = watch.stops(pf(), log, h, strong, RATES, NOW, [])[0][1]
    assert line == ("⚠️ AMD is 15.0% below its peak since you bought, but the trend/score is still strong (89) — hold "
                    "unless it falls further (hard stop at −15% vs your cost).")
    for i in (dict(strong[0], score=69), dict(strong[0], rating="neutral")):
        assert watch.stops(pf(), log, h, [i], RATES, NOW, [])[0][1].endswith("— consider selling.")
    hard = watch.stops(pf(80000), log, h, strong, RATES, NOW, [])[0][1]  # the hard stop stays as it is
    assert "hold unless" in hard and hard.endswith("hard stop, consider selling.")
