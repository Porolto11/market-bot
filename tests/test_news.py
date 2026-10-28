import json
import time
from datetime import date, datetime, timedelta, timezone
from io import StringIO

import pandas as pd

from invest_bot import cloud, news, scan
from test_cloud import SECRETS, TOKEN, TUE, args, hist, info, market, saved
from test_scan import FX, NOW, TODAY, strong_fund, universe

EPOCH = int((TUE - timedelta(hours=5)).timestamp())


def old(title, ts=EPOCH, publisher="Reuters"):
    return {"uuid": "x", "title": title, "publisher": publisher, "providerPublishTime": ts}


def new(title, when="2026-10-26T20:00:00Z", provider="Bloomberg"):
    return {"id": "y", "content": {"title": title, "pubDate": when, "provider": {"displayName": provider}}}


def test_parse_both_shapes_and_bad_items():
    raw = [old("Old shape"), new("New shape"), {"content": None}, {"title": "no time"}, "junk", None,
           new("Bad date", when="yesterday"), old(None)]
    out = news.parse(raw)
    assert [(t, s) for _, t, s in out] == [("Old shape", "Reuters"), ("New shape", "Bloomberg")]  # newest first
    assert news.parse(None) == [] and news.parse({"a": 1}) == []


def test_clean_plain_one_line_and_truncated():
    t = news.clean("*AMD* [click](http://x)\nfalls\t _hard_ " + "x" * 200)
    assert "\n" not in t and "\t" not in t and len(t) == news.MAX_TITLE and t.endswith("…")
    assert news.clean("  short  ") == "short"


def test_negative_keywords_last_3_days_only():
    def neg(title, ts=EPOCH):
        return news.negative(news.parse([old(title, ts)]), TUE)
    for t in ("Analyst downgrades AMD", "Company cuts guidance", "SEC opens probe", "Nvidia faces lawsuit",
              "Q3 revenue misses estimates", "CEO resigns", "Trading halted", "Firm files for bankruptcy",
              "Regulators widen investigation", "Carmaker announces RECALL", "Lowers guidance for 2027"):
        assert neg(t) == t, t
    for t in ("Second-quarter sector rally", "Upgrade to buy", "Secure growth ahead", "Guidance raised",
              "Insider sold shares, according to a recent SEC filing", "New SEC Filings show stake"):
        assert neg(t) is None, t
    assert neg("Analyst downgrades AMD", int((TUE - timedelta(days=4)).timestamp())) is None


def test_fetch_parallel_with_timeout_and_failures():
    def fn(s):
        if s == "SLOW":
            time.sleep(2)
        if s == "BAD":
            raise RuntimeError("yahoo down")
        return [old(f"{s} news")]
    t0 = time.monotonic()
    got = news.fetch(["A", "SLOW", "BAD", "A"], fn, timeout=0.5)
    assert time.monotonic() - t0 < 1.5
    assert got["A"][0][1] == "A news" and got["SLOW"] == [] and got["BAD"] == []
    assert news.fetch([], fn) == {}


def test_movers_and_alert_lines():
    idx = pd.bdate_range(end="2026-10-27", periods=3)
    h = {"AMD": pd.DataFrame({"Close": [100, 100, 91.8]}, index=idx),
         "UP": pd.DataFrame({"Close": [100, 100, 107]}, index=idx),
         "MEH": pd.DataFrame({"Close": [100, 100, 96]}, index=idx), "SHORT": pd.DataFrame({"Close": [1.0]})}
    moves = news.movers(h, ["AMD", "UP", "MEH", "SHORT", "MISSING"])
    assert [m[0] for m in moves] == ["AMD", "UP"]
    lines = news.alert_lines(moves, {"AMD": news.parse([old("AMD slides"), old("b"), old("c")])}, TUE)
    assert lines[0] == "📉 AMD −8.2% today" and lines[1] == "   📰 AMD slides (Reuters)" and len(lines) == 4
    assert lines[3] == "📈 UP +7.0% today"
    assert news.alert_lines([("X", -0.06, date(2026, 10, 23))], {}, TUE) == ["📉 X −6.0% on Fri 23 Oct"]
    idx = pd.bdate_range(end="2026-10-27", periods=62)
    wild = pd.DataFrame({"Close": [100 * (1.04 if i % 2 else 1 / 1.04) ** (i % 2) for i in range(61)] + [92.0]},
                        index=idx)  # swings 4% a day: -8% is under 2.5 sigma
    assert news.movers({"W": wild}, ["W"]) == []
    assert news.alert_lines([("ASML.AS", -0.06, date(2026, 10, 23))], {}, TUE) == ["📉 ASML −6.0% on Fri 23 Oct"]


def test_earnings_within_3_days_turns_buy_into_wait():
    h = universe({"NVDA": hist()})
    last = scan.features(h["NVDA"])["last"]
    def call(days):
        ed = str(pd.Timestamp(TODAY) + pd.offsets.BDay(days))[:10] if days is not None else None
        fund = {"NVDA": dict(strong_fund(last), earnings_date=ed)}
        return next(i for i in scan.run_scan(h, fund, NOW, 1e6, {}, FX) if i["symbol"] == "NVDA")
    assert call(None)["call"] == "BUY"
    two = call(2)
    assert two["call"] == "WAIT" and two["reason"].startswith("Earnings in 2 days — wait for the report.")
    assert call(0)["reason"].startswith("Earnings today")
    five = call(5)
    assert five["call"] == "BUY" and "earnings in 5 days" in five["flags"]


def run(tmp_path, h=None, news_fn=lambda s: [], env=SECRETS, sent=None, **kw):
    sent = [] if sent is None else sent
    out = StringIO()
    code = cloud.run(args(tmp_path, **kw), env=env, now=TUE, download_fn=lambda s, period: h or market(),
                     info_fn=info, send_fn=lambda t, c, text: sent.append(text), out=out, news_fn=news_fn)
    return code, sent, out.getvalue()


def test_negative_news_turns_buy_into_wait(tmp_path):
    asked = []

    def fn(s):
        asked.append(s)
        return [old("Nvidia *faces* SEC probe\nover sales")] if s == "NVDA" else [old("AMD hits record")]
    _, sent, log = run(tmp_path, news_fn=fn)
    text = sent[0]
    assert ("⏳ WAIT (would be BUY)\n• Nvidia (NVDA) · 99/100 — Fresh negative news: 'Nvidia *faces* SEC probe "
            "over sales'. Wait for clarity.\n\n") in text
    assert "Score" not in text.split("⏳ WAIT (would be BUY)")[1].split("\n\n")[0]
    assert "1️⃣ Nvidia" not in text and "🎯 TODAY'S MOVE\nNo good setups today" in text and set(asked) == {"NVDA", "AMD"}  # only BUY / dip candidates (no holdings)
    assert ["NVDA", "WAIT"] in saved(tmp_path)["sent"][-1]["calls"] and "news for 2 symbols" in log


def test_holding_with_negative_news_gets_a_flag(tmp_path):
    from test_weekly import holdings_state
    holdings_state(tmp_path)
    _, sent, log = run(tmp_path, news_fn=lambda s: [old("MOL lawsuit filed")] if s == "MOL.BD" else [])
    assert "\n🟢 MOL " in sent[0] and "\n   📰 negative news: 'MOL lawsuit filed'" in sent[0]
    assert "lawsuit" not in log


def test_big_move_alert_on_a_holding_deduped_and_forces_a_send(tmp_path):
    base = market()
    drop = base["NVDA"].copy()
    drop.iloc[-1, 0] = drop.iloc[-2, 0] * 0.92
    h = dict(base, NVDA=drop)
    sent, env = [], dict(SECRETS, PORTFOLIO_JSON=json.dumps(dict(holdings=[
        dict(ticker="NVDA", symbol="NVDA", qty=1, avg_cost_huf=1, ccy="USD")])))
    run(tmp_path, h=h, sent=sent, env=env, news_fn=lambda s: [old("Chip stocks slide (wire)", publisher="AP")])
    assert "\n\n🚨 ALERTS\n📉 NVDA −8.0% on Mon 26 Oct\n   📰 Chip stocks slide (wire) (AP)\n" in sent[0]
    assert sent[0].startswith("📊 Market scan")
    assert saved(tmp_path)["alerts"] == ["NVDA|2026-10-26"]
    run(tmp_path, h=h, sent=sent, env=env)
    assert len(sent) == 1  # same calls, alert already sent: nothing new
    st = saved(tmp_path)
    st["alerts"] = []
    from invest_bot import state
    state.save(st, str(tmp_path / "state" / "state.enc"), TOKEN)
    run(tmp_path, h=h, sent=sent, env=env)
    assert len(sent) == 2 and "🚨 ALERTS\n📉 NVDA −8.0%" in sent[1]  # calls unchanged, but a new alert


def test_telegram_is_plain_text():
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def opener(req, timeout):
        seen["data"] = req.data
        return Resp()
    cloud.send_telegram(TOKEN, "42", "*x* [y](z)", opener=opener)
    assert b"parse_mode" not in seen["data"]


def test_only_headlines_about_the_company_count():
    def neg(sym, title):
        return news.negative(news.parse([old(title)]), TUE, sym)
    assert neg("NVDA", "Bitcoin miners face SEC probe as crypto slides") is None
    assert neg("NVDA", "NVDA faces probe") and neg("NVDA", "Nvidia faces probe")
    assert neg("SAP.DE", "SAP misses estimates") and neg("MU", "micron cuts guidance") and neg("LLY", "Lilly recall")
    assert neg("MOL.BD", "MOL lawsuit filed") and neg("BRK-B", "BRK probe widens")
    assert neg("SAP.DE", "Sapient lawsuit") is None and neg("LLY", "Eli Manning lawsuit") is None
    assert news.mentions("META", "Meta Platforms shares fall") and not news.mentions("V", "v-shaped recovery")



def test_short_tickers_only_as_cashtag_or_in_brackets():
    assert not news.mentions("V", "V probe widens") and not news.mentions("MA", "MA lawsuit filed")
    assert news.mentions("GE", "GE Aerospace probe")  # its company word is GE itself ("GE Aerospace")
    assert news.mentions("V", "$V falls on probe") and news.mentions("MA", "Mastercard (MA) lawsuit")
    assert news.mentions("V", "Visa probe") and news.mentions("MA", "Mastercard lawsuit")
    assert not news.mentions("V", "$VZ probe") and news.mentions("NVDA", "$NVDA slides")
    assert news.mentions("KLAC", "KLAC misses") and news.mentions("MU", "(MU) misses")


def test_eli_lilly_company_word_is_lilly():
    assert news.name_word("LLY") == "Lilly" and news.name_word("MU") == "Micron"
    assert news.mentions("LLY", "Lilly faces lawsuit") and not news.mentions("LLY", "Eli Manning lawsuit")


def test_alert_headlines_prefer_the_company():
    h = news.parse([old("Bitcoin slides"), old("Crypto selloff deepens"), old("Nvidia drops after report", EPOCH - 60)])
    lines = news.alert_lines([("NVDA", -0.06, TODAY)], {"NVDA": h}, TUE)
    assert lines[1] == "   📰 Nvidia drops after report (Reuters)" and len(lines) == 3


def test_unrelated_negative_headline_keeps_the_buy(tmp_path):
    _, sent, _ = run(tmp_path, news_fn=lambda s: [old("Bitcoin miners face SEC probe")] if s == "NVDA" else [])
    assert "\n1️⃣ NVDA · " in sent[0] and "WAIT (would be BUY)" not in sent[0]
