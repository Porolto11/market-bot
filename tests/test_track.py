from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from invest_bot import commands, track
from test_cloud import TOKEN, go, saved

D0 = date(2026, 1, 5)  # a Monday


def series(daily, n=200, start=D0, base=100.0):
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame({"Close": [base * daily ** i for i in range(n)]}, index=idx)


def test_log_dedupes_per_day_and_caps():
    h = {"NVDA": series(1.0), "CSPX.L": series(1.0, base=500)}
    st = {}
    track.log(st, [["NVDA", "BUY"], ["NVDA", "BUY"], ["X", "BUY"], ["NVDA", "HOLD"]], D0, h)
    track.log(st, [["NVDA", "BUY"]], D0, h)
    assert st["calls"] == [dict(d="2026-01-05", sym="NVDA", call="BUY", px=100.0, spx=500.0, x={})]
    track.log(st, [["NVDA", "BUY"]], date(2026, 1, 6), h)  # a new day: logged again
    track.log(st, [["NVDA", "SELL"]], date(2026, 1, 6), h)
    assert len(st["calls"]) == 3
    st["calls"] = [dict(d="2026-01-05", sym=str(i), call="BUY", px=1, spx=1, x={}) for i in range(600)]
    track.log(st, [["NVDA", "BUY"]], date(2026, 1, 7), h)
    assert len(st["calls"]) == track.MAX_CALLS and st["calls"][-1]["sym"] == "NVDA"


def test_score_horizons_vs_cspx():
    h = {"UP": series(1.01), "DOWN": series(0.99), "CSPX.L": series(1.0, base=500)}
    st = {"calls": [dict(d=D0.isoformat(), sym=s, call=c, px=100.0, spx=500.0, x={})
                    for s, c in (("UP", "BUY"), ("DOWN", "BUY"), ("DOWN", "SELL"), ("UP", "WAIT_FOR_DIP"),
                                 ("GONE", "BUY"))]}
    today = np.busday_offset(D0, 65).astype(date)  # 1m and 3m are due, 6m is not
    track.score(st, today, h)
    up, down, sell, wait, gone = st["calls"]
    assert set(up["x"]) == {"20", "60"} and abs(up["x"]["20"] - (1.01 ** 20 - 1)) < 1e-4
    assert down["x"]["60"] < 0 and gone["x"] == {}  # no history for GONE: left unscored
    assert track.edge("BUY", up["x"]["20"]) > 0 and track.edge("SELL", sell["x"]["20"]) > 0
    assert track.edge("WAIT_FOR_DIP", wait["x"]["20"]) < 0  # waiting on a riser lost
    before = dict(up["x"])
    track.score(st, today, h)
    assert up["x"] == before
    short = {k: v.iloc[:30] for k, v in h.items()}  # history ends before the 3m date: not scored yet
    st2 = {"calls": [dict(d=D0.isoformat(), sym="UP", call="BUY", px=100.0, spx=500.0, x={})]}
    track.score(st2, today, short)
    assert set(st2["calls"][0]["x"]) == {"20"}


def test_stats_too_early_and_hit_rate():
    assert "No calls tracked yet" in track.stats({})
    calls = [dict(d="2026-01-05", sym="A", call="BUY", px=1, spx=1, x={"20": 0.05 if i < 9 else -0.02})
             for i in range(12)]
    calls += [dict(d="2026-01-05", sym="B", call="SELL", px=1, spx=1, x={"20": -0.03}) for _ in range(3)]
    text = track.stats({"calls": calls})
    assert "• BUY: 75% won, avg +3.2% vs CSPX (n=12)" in text and text.startswith("📊 Track record of 15 ")
    assert "• SELL: too early (n=3)" in text and "all 80% won" in text
    assert "3m (60 trading days): all too early (n=0)" in text and "WAIT:" not in text
    assert track.one_liner({"calls": calls}).startswith("Calls: 1m 80% won")
    st = {"calls": calls, "log": []}
    assert commands.handle(st, "/stats", datetime(2026, 3, 1, tzinfo=timezone.utc)) == text


def test_scan_logs_sent_calls_and_keeps_them_encrypted(tmp_path):
    go(tmp_path)
    st = saved(tmp_path)
    logged = {(c["sym"], c["call"]) for c in st["calls"]}
    assert ("NVDA", "BUY") in logged and ("AMD", "WAIT_FOR_DIP") in logged
    assert all(c["spx"] > 0 and c["px"] > 0 and c["d"] == "2026-10-27" for c in st["calls"])
    go(tmp_path, force=True)  # same day, same calls: no duplicates
    assert len(saved(tmp_path)["calls"]) == len(st["calls"])
    assert not (tmp_path / "state" / "state.enc").read_bytes().startswith(b"{")


def test_shadow_logs_top_picks_once_per_episode_and_scores():
    h = {"UP": series(1.01), "DOWN": series(0.99), "OLD": series(1.0), "CSPX.L": series(1.0, base=500)}
    items = [dict(symbol="UP", score=90, mom=10), dict(symbol="DOWN", score=50, mom=95),
             dict(symbol="OLD", score=99, mom=99, stale=True), dict(symbol="NONE", score=0, mom=0)]
    st = {}
    track.shadow(st, items, D0, h)
    assert sorted((c["m"], c["sym"]) for c in st["shadow"]) == [
        ("mom", "DOWN"), ("mom", "UP"), ("score", "DOWN"), ("score", "UP")]  # stale and zero left out
    track.shadow(st, items, np.busday_offset(D0, 5).astype(date), h)
    assert len(st["shadow"]) == 4  # same episode
    track.shadow(st, items, np.busday_offset(D0, 20).astype(date), h)
    assert len(st["shadow"]) == 8
    st["shadow"] = st["shadow"][:4]
    track.score(st, np.busday_offset(D0, 25).astype(date), h)
    assert all("20" in c["x"] for c in st["shadow"])
    assert "🧪 Model test" in track.stats(st)


def test_mom_only_trend_filter():
    from invest_bot import scan
    moms = dict(long=[0.1, 0.2, 0.3])
    assert scan.mom_only(dict(last=10, sma200=9, mom_12_1=0.3), moms) > 50
    assert scan.mom_only(dict(last=8, sma200=9, mom_12_1=0.3), moms) == 0  # below the 200-day average
    assert scan.mom_only(dict(last=10), moms) == 0  # no momentum
