import base64
import json
import subprocess
from argparse import Namespace
from datetime import datetime, timezone
from io import StringIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from invest_bot import cloud, fmt, scan, state

BUD = ZoneInfo("Europe/Budapest")
TUE = datetime(2026, 10, 27, 6, 45, tzinfo=timezone.utc)
MON = datetime(2026, 10, 26, 6, 45, tzinfo=timezone.utc)
TOKEN = "123456:SECRET-token-value"



def hist(up=1.015, down=0.99, n=320, hot_tail=0):
    px, out = 100.0, []
    for i in range(n):
        px *= up if i % 2 == 0 else down
        if i >= n - hot_tail:
            px *= 1.01 / (up if i % 2 == 0 else down)
        out.append(px)
    return pd.DataFrame({"Close": out}, index=pd.bdate_range(end="2026-10-26", periods=n))


def flat(v):
    return pd.DataFrame({"Close": [v] * 5}, index=pd.bdate_range(end="2026-10-26", periods=5))


def market(extra=None):
    weak = hist(up=1.004, down=0.998)
    h = {s: weak for s in [*list(scan.UNIVERSE)[:45], "CSPX.L"]}  # enough symbols: under half means an outage
    h.update({"NVDA": hist(), "AMD": hist(hot_tail=20), "MOL.BD": hist(up=0.985, down=1.01),
              "^GSPC": hist(), "^STOXX50E": hist(up=1.004, down=0.998),
              "EURHUF=X": flat(400.0), "USDHUF=X": flat(350.0)})
    h.update(extra or {})
    return h


def info(symbol):
    if symbol not in ("NVDA", "AMD", "MOL.BD"):
        return {}
    return {"revenueGrowth": 0.3, "earningsGrowth": 0.4, "profitMargins": 0.25, "currency": "USD",
            "targetMeanPrice": 10_000, "numberOfAnalystOpinions": 30}


def args(tmp_path, **kw):
    a = dict(portfolio=str(tmp_path / "state" / "portfolio.json"), state=str(tmp_path / "state" / "state.enc"),
             budget_huf=None, force=False)
    a.update(kw)
    return Namespace(**a)


def saved(tmp_path):
    return state.load(str(tmp_path / "state" / "state.enc"), TOKEN)


def go(tmp_path, now=TUE, env=None, sent=None, a=None, **kw):
    sent = [] if sent is None else sent
    out = StringIO()
    env = {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"} if env is None else env
    code = cloud.run(a or args(tmp_path, **kw), env=env, now=now, download_fn=lambda s, period: market(),
                     info_fn=info, send_fn=lambda t, c, text: sent.append((t, c, text)), out=out)
    return code, sent, out.getvalue()


def test_no_portfolio_market_calls_only(tmp_path):
    code, sent, log = go(tmp_path)
    assert code == 0 and len(sent) == 1
    text = sent[0][2]
    assert text.startswith("📊 Market scan · Tue 27 Oct, 07:45\n🇺🇸 US 🟢 uptrend · 🇪🇺 EU 🟢 uptrend\n")
    assert "📭 Market calls only (no holdings data)" in text and "💰 Budget 200 000 HUF (no cash data)\n" in text
    assert "\n\n🟢 BUY PLAN\n1️⃣ Buy 2× Nvidia (NVDA) at ≤ $" in text  # 2 shares: the fee stays under 1%
    assert "\n   99/100 🔥 · 📈 +77% in 12 months · 🎯 analysts +4511% upside · ⏰ valid till Wed\n🧮 Total ≈ " in text
    assert " of your 200 000 HUF · left ≈ " in text
    assert "MOL" not in text  # bearish, not held: AVOID is not messaged
    assert "🔴 SELL" not in text and "🪤 WAIT FOR A DIP" in text and "• AMD · " in text and "🌡️ overheated" in text
    assert text.endswith(cloud.FOOTER) and "\n\n🎯 TODAY'S MOVE\n🟢 Buy " in text
    st = saved(tmp_path)
    assert ["NVDA", "BUY"] in st["sent"][-1]["calls"] and ["AMD", "WAIT_FOR_DIP"] in st["sent"][-1]["calls"]
    assert st["scan"]["items"][0][2] >= st["scan"]["items"][-1][2] and len(st["scan"]["moms"]["long"]) > 40


def test_budget_from_env_and_whole_shares(tmp_path):
    _, sent, _ = go(tmp_path, env={"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42", "CLOUD_BUDGET_HUF": "1000000"})
    text = sent[0][2]
    assert "💰 Budget 1 000 000 HUF (no cash data)\n" in text
    nv = next(l for l in text.splitlines() if l.startswith("1️⃣ Buy "))
    qty = int(nv.split("Buy ")[1].split("×")[0])
    assert qty >= 1 and " + fee " in nv and nv.endswith("%)")
    assert f"🎯 TODAY'S MOVE\n🟢 Buy the plan: {qty}× NVDA ≈ " in text


PORTFOLIO = dict(as_of="2026-10-26T18:00:00+01:00", cash_huf=300000, realised_gain_huf_this_year=0,
                 holdings=[dict(ticker="MOL", symbol="MOL.BD", qty=10, avg_cost_huf=3000, ccy="HUF"),
                           dict(ticker="VHYL", symbol="VHYL.AS", qty=1, avg_cost_huf=1, ccy="EUR")],
                 pending_orders=[dict(ticker="CSPX", side="buy", qty=1, limit=100)])
SECRETS = {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}


def test_portfolio_secret_gives_sell_calls_and_holding_lines(tmp_path):
    _, sent, log = go(tmp_path, env=dict(SECRETS, PORTFOLIO_JSON=json.dumps(PORTFOLIO)))
    text = sent[0][2]
    assert "Market calls only" not in text and "🗂️ Holdings as of 2026-10-26 18:00" in text
    assert "\n🔴 SELL\n• MOL · " in text and "📉 trend turned bearish: sell 10× at ≥ $" in text
    assert "🎯 TODAY'S MOVE\n🔴 Sell 10× MOL at ≥ $" in text  # a SELL comes first
    assert "💰 Cash ≈ 265 000 HUF" in text  # 300 000 less the pending CSPX buy (1 x 100 USD x 350)
    mol = scan.features(market()["MOL.BD"])["last"]
    # the test fundamentals say USD for every symbol: 350 HUF per unit
    pl = f"{(mol * 350 / 3000 - 1) * 100:+.1f}%".replace("-", "−")
    assert f"💼 YOUR HOLDINGS\n🟢 MOL {pl} · score " in text and " · 🔴 sell" in text
    assert "⚪ VHYL · no price data" in text and ".AS" not in text and ".BD" not in text
    assert b"MOL.BD" not in (tmp_path / "state" / "state.enc").read_bytes()  # encrypted (base64 has no ".")
    assert not saved(tmp_path)["log"]  # PORTFOLIO_JSON is not copied into the state
    assert "300000" not in log and "MOL" not in log.split("calls:")[0]


def test_portfolio_local_file_for_testing(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps(PORTFOLIO))
    _, sent, _ = go(tmp_path, portfolio=str(f))
    assert "🗂️ Holdings as of" in sent[0][2]
    _, sent, _ = go(tmp_path, env=dict(SECRETS, PORTFOLIO_JSON="{broken"), force=True)
    assert "Market calls only (no holdings data)" in sent[0][2]


def test_cash_secret_sizes_buys(tmp_path):
    _, sent, log = go(tmp_path, env=dict(SECRETS, CASH_HUF="1000000"))
    text = sent[0][2]
    assert "💰 Cash ≈ 1 000 000 HUF\n📭 Market calls only (no holdings data)" in text
    assert "1️⃣ Buy " in text and " of your 1 000 000 HUF · left ≈ " in text and "Nothing fits" not in text
    assert "1000000" not in log and "1 000 000" not in log
    _, sent, _ = go(tmp_path, env=dict(SECRETS, CASH_HUF="500000", PORTFOLIO_JSON=json.dumps(PORTFOLIO)), force=True)
    assert "💰 Cash ≈ 465 000 HUF" in sent[0][2]  # CASH_HUF wins over the holdings' cash, less pending buys


def test_not_enough_cash_message(tmp_path):
    _, sent, _ = go(tmp_path, env=dict(SECRETS, CASH_HUF="5000"))
    text = sent[0][2]
    assert "🎯 TODAY'S MOVE\n🤷 Nothing fits your cash — top pick Nvidia (NVDA) needs ≈ " in text
    assert "HUF. Deposit or wait." in text and "❌ 1 share ≈ " in text and ", over budget" in text


def test_secrets_never_printed_when_telegram_missing(tmp_path):
    _, _, log = go(tmp_path, env=dict(CASH_HUF="777777", PORTFOLIO_JSON=json.dumps(PORTFOLIO)))
    assert "not printed" in log and "777" not in log and "avg cost" not in log


def test_dedupe_same_changed_forced_and_monday(tmp_path):
    sent = []
    go(tmp_path, sent=sent)
    go(tmp_path, sent=sent)
    assert len(sent) == 1  # same calls: no message
    st = saved(tmp_path)
    st["sent"][-1]["calls"] = [["X", "BUY"]]
    state.save(st, str(tmp_path / "state" / "state.enc"), TOKEN)
    go(tmp_path, sent=sent)
    assert len(sent) == 2  # changed: send
    go(tmp_path, sent=sent, force=True)
    assert len(sent) == 3
    go(tmp_path, sent=sent, now=MON)
    assert len(sent) == 5  # Monday morning: the scan message and the weekly summary
    go(tmp_path, sent=sent, now=MON.replace(hour=15))
    assert len(sent) == 5


def test_yfinance_outage_sends_a_short_failure_note(tmp_path):
    sent, out = [], StringIO()
    code = cloud.run(args(tmp_path), env=SECRETS, now=TUE, download_fn=lambda s, period: {"NVDA": hist()},
                     info_fn=info, send_fn=lambda t, c, text: sent.append(text), out=out)
    n = len(scan.UNIVERSE)
    assert code == 1 and sent == [f"⚠️ Scan failed: only 1 of {n} symbols had prices (data outage?). "
                                  "Next try Tue 27 Oct, 07:50."]
    assert not (tmp_path / "state" / "last_scan.txt").exists()  # no scan: the heartbeat stays old

    def broken(s, period):
        raise ConnectionError("down")
    later = datetime(2026, 10, 30, 21, 30, tzinfo=timezone.utc)  # Friday evening: next try Monday morning
    code = cloud.run(args(tmp_path), env=SECRETS, now=later, download_fn=broken, info_fn=info,
                     send_fn=lambda t, c, text: sent.append(text), out=out)
    assert code == 1 and sent[-1] == "⚠️ Scan failed: price download failed (ConnectionError). Next try Mon 2 Nov, 07:50."
    code = cloud.run(args(tmp_path), env={}, now=TUE, download_fn=broken, info_fn=info,
                     send_fn=lambda *a: sent.append(a), out=out)
    assert code == 1 and len(sent) == 2  # no secrets: logged only


def test_scan_writes_the_plaintext_heartbeat(tmp_path):
    go(tmp_path)
    assert (tmp_path / "state" / "last_scan.txt").read_text() == "2026-10-27T06:45:00+00:00"
    assert (tmp_path / "state" / "scan_warned.txt").read_text() == ""


def test_permanent_telegram_errors_are_dropped(tmp_path):
    import urllib.error

    def opener(code):
        def fn(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, "Forbidden", {}, None)
        return fn
    with pytest.raises(cloud.Permanent):
        cloud.send_telegram(TOKEN, "42", "hi", opener=opener(403))
    with pytest.raises(RuntimeError) as e:
        cloud.send_telegram(TOKEN, "42", "hi", opener=opener(502))
    assert not isinstance(e.value, cloud.Permanent) and TOKEN not in str(e.value)

    def blocked(t, c, text):
        cloud.send_telegram(t, c, text, opener=opener(403))
    out = StringIO()
    code = cloud.run(args(tmp_path), env=SECRETS, now=TUE, download_fn=lambda s, period: market(), info_fn=info,
                     send_fn=blocked, out=out)
    assert code == 1 and "(permanent: dropped)" in out.getvalue() and saved(tmp_path)["sent"]  # not retried


def test_missing_secrets_prints_only_and_keeps_state(tmp_path):
    code, sent, log = go(tmp_path, env={})
    assert code == 0 and not sent and "message printed only" in log and cloud.FOOTER in log
    assert not (tmp_path / "state" / "state.enc").exists()


def test_token_never_in_output(tmp_path, capsys):
    def opener(req, timeout):
        raise OSError(f"cannot reach {req.full_url}")
    with pytest.raises(RuntimeError) as e:
        cloud.send_telegram(TOKEN, "42", "hi", opener=opener)
    assert TOKEN not in str(e.value) and "***" in str(e.value)

    def failing(t, c, text):
        cloud.send_telegram(t, c, text, opener=opener)
    out = StringIO()
    code = cloud.run(args(tmp_path), env={"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}, now=TUE,
                     download_fn=lambda s, period: market(), info_fn=info, send_fn=failing, out=out)
    assert code == 1 and TOKEN not in out.getvalue() and TOKEN not in capsys.readouterr().out
    assert not (tmp_path / "state" / "state.enc").exists()  # not sent: retried next run


def test_send_telegram_posts_form(tmp_path):
    seen = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def opener(req, timeout):
        seen.update(url=req.full_url, data=req.data, method=req.get_method())
        return Resp()
    cloud.send_telegram(TOKEN, "42", "a & b", opener=opener)
    assert seen["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage" and seen["method"] == "POST"
    assert b"chat_id=42" in seen["data"] and b"text=a+%26+b" in seen["data"]


def test_message_without_buys_is_split_at_4000_at_section_boundaries():
    regime = {"US": {"rating": "neutral", "score": 2}, "EU": {"rating": "bearish", "score": 0}}
    long = [dict(symbol=f"S{i}.AS", name="N" * 50, score=80, call="WAIT_FOR_DIP", limit=1.0, ccy="EUR",
                 valid_until="2026-11-09", qty=1, approx_huf=420.0, one_share_huf=420.0, reason="r", flags=[])
            for i in range(200)]
    text = cloud.message(long, regime, TUE, None, 200000)
    assert "🇺🇸 US 🟡 sideways · 🇪🇺 EU 🔴 downtrend" in text and "🟢 BUY" not in text
    assert "🎯 TODAY'S MOVE\nNo good setups today — holding is fine. ✋" in text
    assert "• N" in text and "(S7) · 80/100 ✅\n   💵 ≤ €1 · ⏰ valid till Mon 9 Nov\n   ✅ 1× ≈ 420 HUF" in text
    assert len(text) > 4000 and ".AS" not in text
    chunks = fmt.split(text)
    assert len(chunks) > 1 and all(len(c) <= 4000 for c in chunks)
    assert [x for c in chunks for x in c.splitlines() if x] == [x for x in text.splitlines() if x]  # nothing lost
    assert chunks[0].endswith("🎯 TODAY'S MOVE\nNo good setups today — holding is fine. ✋")  # cut between sections
    assert chunks[-1].endswith(cloud.FOOTER)


def test_long_message_goes_out_in_several_sends(tmp_path, monkeypatch):
    real = cloud.message
    monkeypatch.setattr(cloud, "message", lambda *a, **k: real(*a, **k) + "\n\n" + "x" * 3990)
    _, sent, _ = go(tmp_path)
    assert len(sent) == 2 and sent[0][2].startswith("📊 Market scan") and sent[1][2].startswith("x" * 3990)
    assert all(len(t) <= 4000 for _, _, t in sent) and saved(tmp_path)["sent"]  # sent once, logged once


def test_state_holdings_win_over_secrets_and_give_sell_calls(tmp_path):
    path = str(tmp_path / "state" / "state.enc")
    st = state.empty()
    st["log"] = [dict(t="cash", huf=400000, at="2026-10-26T10:00:00"),
                 dict(t="buy", sym="MOL.BD", qty=10, price=3000, ccy="HUF", fx=1.0, fee=349, at="2026-10-26T10:00:00")]
    state.save(st, path, TOKEN)
    _, sent, log = go(tmp_path, env=dict(SECRETS, CASH_HUF="5000", PORTFOLIO_JSON=json.dumps(PORTFOLIO)))
    text = sent[0][2]
    assert "\n🔴 SELL\n• MOL · " in text and "sell 10× at ≥" in text and "💼 YOUR HOLDINGS\n🟢 MOL " in text
    assert "💰 Cash ≈ 370 000 HUF" in text and "VHYL" not in text  # 400 000 - 10 x 3000 - 349, not the secrets
    assert "369" not in log and saved(tmp_path)["log"] == st["log"]


def test_expected_state_missing_stops_before_sending(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_EXPECTED", "1")
    code, sent, log = go(tmp_path)
    assert code == 1 and "missing" in log and not (tmp_path / "state" / "state.enc").exists()
    assert len(sent) == 1 and sent[0][2].startswith("⚠️ Scan failed: ") and "Next try Tue 27 Oct, 07:50." in sent[0][2]
