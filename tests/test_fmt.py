from datetime import date, datetime, timezone

from invest_bot import cloud, commands, fmt, state

TUE = datetime(2026, 10, 27, 6, 45, tzinfo=timezone.utc)
REGIME = {"US": {"rating": "bullish", "score": 4}, "EU": {"rating": "neutral", "score": 2}}


def item(sym, call, score=90, qty=1, one=60000.0, name=None, **kw):
    i = dict(symbol=sym, name=name or sym, kind="stock", ccy="USD", last=100.0, score=score, call=call,
             parts=dict(upside_pct=0.71), mom_long=4.24, mom_6m=1.0, limit=179.4, qty=qty,
             approx_huf=(qty or 0) * one or None, one_share_huf=one, valid_until="2026-10-28",
             reason=f"Score {score}/100.", why=f"Score {score}/100.", flags=[], rating="bullish")
    i.update(kw)
    return i


def move(items, switches=(), stops=()):
    return cloud.message(items, REGIME, TUE, None, 250000, has_cash=True, switches=switches,
                         stops=stops).split("🎯 TODAY'S MOVE\n")[1].split("\n\n")[0]


def test_short_tickers_never_show_the_exchange_suffix():
    assert fmt.tick("ASML.AS") == "ASML" and fmt.tick("CSPX.L") == "CSPX" and fmt.tick("OTP.BD") == "OTP"
    assert fmt.tick("NVDA") == "NVDA" and fmt.tick("NOVO-B.CO") == "NOVO-B"
    assert fmt.label("ASML", "ASML.AS") == "ASML" and fmt.label("Micron", "MU") == "Micron (MU)"
    text = cloud.message([item("ASML.AS", "BUY", name="ASML", ccy="EUR", plan=True, value_huf=59000.0,
                               fee_huf=1000.0, fee_pct=1000 / 59000)], REGIME, TUE, None, 250000)
    assert "1️⃣ Buy 1× ASML at ≤ €179.4 ≈ 59 000 HUF + fee 1 000 HUF (1.69%)" in text and ".AS" not in text
    assert "👀 Watchlist: ASML 90" in cloud.message([item("ASML.AS", "BUY", name="ASML")], REGIME, TUE, None, 0)


def test_prices_money_scores_and_times():
    assert fmt.price(915.33, "USD") == "$915.33" and fmt.price(12.5, "EUR") == "€12.5"
    assert fmt.price(3.2, "GBP") == "£3.2" and fmt.price(120, "GBp") == "120 GBp" and fmt.price(3000, "HUF") == "3000 HUF"
    assert fmt.huf(302493) == "302 000 HUF" and fmt.huf(59_912) == "59 900 HUF" and fmt.huf(2443) == "2 440 HUF"
    assert fmt.signed(-14.0) == "−14.0%" and fmt.signed(11.84) == "+11.8%" and fmt.signed(-0.01) == "+0.0%"
    assert [fmt.badge(s) for s in (99, 90, 89, 75, 74, 70, 69)] == ["🔥", "🔥", "✅", "✅", "👍", "👍", ""]
    assert fmt.stamp(TUE) == "Tue 27 Oct, 07:45"  # Budapest: UTC+1 after the October clock change
    assert fmt.stamp(datetime(2026, 7, 1, 6, 50, tzinfo=timezone.utc)) == "Wed 1 Jul, 08:50"  # summer: UTC+2
    assert fmt.day("2026-10-28", date(2026, 10, 27)) == "Wed" and fmt.day("2026-11-09", date(2026, 10, 27)) == "Mon 9 Nov"


def test_scan_message_layout():
    items = [item("MU", "BUY", 99, qty=0, one=302493.0, name="Micron",
                  flags=["earnings in 5 days", "very volatile", "overheated"]),
             item("ANET", "BUY", 80, qty=2, one=59900.0, name="Arista Networks", plan=True, value_huf=119000.0,
                  fee_huf=800.0, fee_pct=800 / 119000),
             item("LLY", "WAIT", 88, name="Eli Lilly", why="Earnings in 2 days — wait for the report.",
                  reason="Earnings in 2 days — wait for the report. Score 88/100.")]
    text = cloud.message(items, REGIME, TUE, None, 250000, has_cash=True,
                         alerts=["📉 AMD −8.2% today", "   📰 AMD guidance cut (Reuters)"])
    assert text.startswith("📊 Market scan · Tue 27 Oct, 07:45\n🇺🇸 US 🟢 uptrend · 🇪🇺 EU 🟡 sideways\n"
                           "💰 Cash ≈ 250 000 HUF\n📭 Market calls only (no holdings data)\n\n"
                           "🎯 TODAY'S MOVE\n🟢 Buy the plan: 2× ANET ≈ 120 000 HUF\n"
                           "Why: the top-ranked picks your cash can buy, fees included.\n\n"
                           "🚨 ALERTS\n📉 AMD −8.2% today\n   📰 AMD guidance cut (Reuters)\n\n🟢 BUY PLAN\n")
    assert ("🟢 BUY PLAN\n1️⃣ Buy 2× Arista Networks (ANET) at ≤ $179.4 ≈ 119 000 HUF + fee 800 HUF (0.67%)\n"
            "   80/100 ✅ · 📈 +424% in 12 months · 🎯 analysts +71% upside · ⏰ valid till Wed\n"
            "   🛑 exit plan: hold while the trend is up · SELL call when it turns bearish, or below $143.52 (−20%)\n"
            "🧮 Total ≈ 120 000 HUF of your 250 000 HUF · left ≈ 130 000 HUF\n"
            "👀 Watchlist: Micron (MU) 99\n\n") in text
    assert "⏳ WAIT (would be BUY)\n• Eli Lilly (LLY) · 88/100 — Earnings in 2 days — wait for the report.\n\n" in text
    assert text.endswith("\n\nℹ️ " + cloud.FOOTER)
    # 6-month momentum when there is no 12-1 value; no analyst chip without >= 5 analysts (upside_pct None)
    six = cloud.chips(item("X", "BUY", mom_long=0.5, mom_6m=0.5, parts=dict(upside_pct=None)))
    assert six == ["📈 +50% in 6 months"]


def test_todays_move_priority():
    buy, fits = item("MU", "BUY", 99, qty=0, one=302493.0, name="Micron"), item("ANET", "BUY", 80, qty=1, plan=True,
                                                                              value_huf=59000.0, fee_huf=1000.0, fee_pct=0.017)
    sell = item("OTP.BD", "SELL", 30, qty=10, limit=40000.0, ccy="HUF", approx_huf=400000.0, name="OTP Bank")
    switch = ("Sell 10× OTP (49) → buy 1× MU (98)\n   💸 Tax now ≈ 0 HUF", "OTP.BD", "MU")
    # 1. a SELL on a holding beats everything
    assert move([buy, fits, sell], [switch], ["AMD"]) == ("🔴 Sell 10× OTP Bank (OTP) at ≥ 40000 HUF ≈ 400 000 HUF "
                                                         "(+1 more below)\nWhy: its price trend turned bearish.")
    # 1b. a stop alert when there is no SELL call
    assert move([buy, fits], [switch], ["AMD"]).startswith("🛑 Sell AMD — it hit a stop\n")
    # 2. a SWITCH
    assert move([buy, fits], [switch]) == ("🔁 Sell 10× OTP (49) → buy 1× MU (98)\nWhy: MU scores far higher and "
                                           "your cash alone cannot buy one share.")
    # 3. the BUY PLAN
    assert move([buy, fits]) == ("🟢 Buy the plan: 1× ANET ≈ 60 000 HUF\n"
                                 "Why: the top-ranked picks your cash can buy, fees included.")
    # 4. no BUY fits
    assert move([buy]) == "🤷 Nothing fits your cash — top pick Micron (MU) needs ≈ 302 000 HUF. Deposit or wait."
    # 5. no calls: no trade is a valid answer
    assert move([]) == move([item("LLY", "WAIT")]) == "No good setups today — holding is fine. ✋"


def test_split_at_4000_on_section_boundaries():
    sections = ["a" * 1500, "b" * 1500, "c" * 1500, "d" * 100]
    chunks = fmt.split("\n\n".join(sections))
    assert chunks == ["a" * 1500 + "\n\n" + "b" * 1500, "c" * 1500 + "\n\n" + "d" * 100]
    assert fmt.split("short") == ["short"] and fmt.split("") == [""]
    big = "\n".join(["x" * 99] * 100)  # one 9999-character section: cut at line ends
    parts = fmt.split(big)
    assert len(parts) == 3 and all(len(p) <= 4000 for p in parts) and "\n".join(parts) == big
    assert [len(p) for p in fmt.split("y" * 9000)] == [4000, 4000, 1000]  # one huge line: hard-cut


def test_plain_text_without_parse_mode_one_post_per_chunk():
    posts = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def opener(req, timeout):
        posts.append(req.data)
        return Resp()
    for chunk in fmt.split("🎯 *bold* <b>x</b>\n\n" + "z" * 3999):
        cloud.send_telegram("1:T", "42", chunk, opener=opener)
    assert len(posts) == 2 and not any(b"parse_mode" in p for p in posts)


def test_help_is_a_short_emoji_list():
    text = commands.handle(state.empty(), "/help", TUE)
    assert text.startswith("🤖 Invest Bot commands") and len(text.splitlines()) <= 20
    for c in ("/bought", "/sold", "/deposit", "/withdraw", "/cash", "/undo", "/holdings", "/top", "/scan", "/stats", "/tax"):
        assert c in text
