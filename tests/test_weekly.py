from datetime import timedelta

from invest_bot import cloud, fmt, prices, state
from test_cloud import MON, SECRETS, TOKEN, TUE, go, market, saved


def holdings_state(tmp_path, extra=()):
    path = str(tmp_path / "state" / "state.enc")
    st = state.empty()
    st["log"] = [dict(t="cash", huf=400000, at="2026-10-01T10:00:00+00:00"),
                 dict(t="buy", sym="NVDA", qty=2, price=100, ccy="USD", fx=350.0, fee=3.49, at="2026-10-01T10:00:00+00:00"),
                 dict(t="buy", sym="MOL.BD", qty=10, price=3000, ccy="HUF", fx=1.0, fee=349, at="2026-10-01T10:00:00+00:00"),
                 *extra]
    state.save(st, path, TOKEN)
    return st


def test_monday_sends_a_separate_summary_and_stores_the_value(tmp_path):
    holdings_state(tmp_path)
    _, sent, log = go(tmp_path, now=MON, env=SECRETS)
    assert len(sent) == 2
    text = sent[1][2]
    assert text.startswith("🗓️ Weekly summary · Mon 26 Oct, 07:45\n💼 Portfolio ≈ ")
    assert "(holdings + cash)" in text and "🆕 First summary" in text
    h = prices.fresh(market(), MON)[0]  # the unfinished Monday bar (and BÉT's 23 Oct holiday) are dropped
    week = {s: h[s]["Close"].iloc[-1] / h[s]["Close"].iloc[-6] - 1 for s in ("NVDA", "MOL.BD")}
    best, worst = sorted(week, key=week.get, reverse=True)
    assert (f"🏆 Best this week: {fmt.tick(best)} {fmt.signed(week[best] * 100)} · "
            f"🐢 worst: {fmt.tick(worst)} {fmt.signed(week[worst] * 100)}") in text and ".BD" not in text
    assert "🎯 Calls: 1m too early (n=0)" in text and "🇺🇸 US 🟢 uptrend" in text
    w = saved(tmp_path)["weekly"]
    assert w["at"].startswith("2026-10-26") and w["value"] > 400000 * 0.5
    assert str(w["value"]) not in log and "Portfolio" not in log


def test_change_since_last_week_leaves_out_deposits(tmp_path):
    st = holdings_state(tmp_path, [dict(t="deposit", huf=50000, at="2026-10-20T10:00:00+00:00")])
    _, sent, _ = go(tmp_path, now=MON, env=SECRETS)
    value = saved(tmp_path)["weekly"]["value"]
    st = saved(tmp_path)
    st["weekly"] = dict(at="2026-10-19T06:50:00+00:00", value=value - 60000)
    state.save(st, str(tmp_path / "state" / "state.enc"), TOKEN)
    _, sent, _ = go(tmp_path, now=MON + timedelta(minutes=5), env=SECRETS)
    assert "\n📈 +10 000 HUF (" in sent[-1][2] and "since Mon 19 Oct (excl. deposits/withdrawals)" in sent[-1][2]


def test_no_holdings_skips_portfolio_part_and_not_on_other_days(tmp_path):
    _, sent, _ = go(tmp_path, now=MON)
    text = sent[1][2]
    assert "Portfolio" not in text and "this week" not in text and "Calls: " in text and "🇺🇸 US " in text
    assert "weekly" not in saved(tmp_path)
    _, sent, _ = go(tmp_path, now=TUE, force=True)
    assert len(sent) == 1


def test_summary_send_failure_keeps_the_last_value(tmp_path):
    holdings_state(tmp_path)
    calls = []

    def flaky(t, c, text):
        calls.append(text)
        if "Weekly summary" in text:
            raise RuntimeError("down")
    from io import StringIO
    from test_cloud import args, info
    out = StringIO()
    code = cloud.run(args(tmp_path), env=SECRETS, now=MON, download_fn=lambda s, period: market(), info_fn=info,
                     send_fn=flaky, out=out)
    st = saved(tmp_path)
    assert code == 1 and len(calls) == 2 and st["sent"] and "weekly" not in st


def test_dry_run_prints_summary_without_holdings(tmp_path):
    _, sent, log = go(tmp_path, now=MON, env={})
    assert not sent and "--- weekly summary ---" in log and "🗓️ Weekly summary" in log
