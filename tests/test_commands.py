from datetime import datetime, timezone
from io import StringIO

import pandas as pd
import pytest

from invest_bot import commands, state
from test_scan import hist

TOKEN = "123456:SECRET-token-value"
ENV = {"TELEGRAM_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "42"}
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


def flat(v):
    return pd.DataFrame({"Close": [v] * 5}, index=pd.bdate_range(end="2026-10-23", periods=5))


PRICES = {"EURHUF=X": flat(400.0), "USDHUF=X": flat(350.0), "NVDA": flat(120.0), "MOL.BD": flat(3300.0),
          "ZZZZ": flat(5.0)}


def download(symbols, period):
    out = {s: PRICES[s] for s in symbols if s in PRICES}
    if period == "2y" and "NVDA" in symbols:
        out["NVDA"] = hist()
    return out


def info(symbol):
    return {"currency": "USD"} if symbol == "ZZZZ" else {}


def say(st, text):
    return commands.handle(st, text, NOW, download, info)


def test_parse_commands():
    assert commands.parse("/bought nvda 3 120,5") == ("bought", ("NVDA", 3, 120.5))
    assert commands.parse("/SOLD MOL.BD 2 3300.25") == ("sold", ("MOL.BD", 2, 3300.25))
    assert commands.parse("/Bought@InvestBot csPX 1 600") == ("bought", ("CSPX", 1, 600.0))
    assert commands.parse("/deposit 150000,5") == ("deposit", (150000.5, "HUF"))
    assert commands.parse("/withdraw 1000") == ("withdraw", (1000.0, "HUF"))
    assert commands.parse("/cash 0") == ("cash", (0.0, "HUF"))
    assert commands.parse("/cash 500 usd") == ("cash", (500.0, "USD"))
    assert commands.parse("/deposit 300,5 EUR") == ("deposit", (300.5, "EUR"))
    assert commands.parse("/scan otp.bd") == ("scan", ("OTP.BD",))
    for c in ("holdings", "top", "undo", "help"):
        assert commands.parse(f"/{c.upper()}") == (c, ())


@pytest.mark.parametrize("text", ["/bought NVDA 1,5 100", "/bought NVDA 0 100", "/bought NVDA 2 -1",
                                  "/bought NVDA 2", "/sold NVDA x 1", "/deposit abc", "/deposit 0",
                                  "/withdraw -5", "/cash", "/holdings now", "/scan", "/bought NVDA 2 nan"])
def test_parse_rejects_bad_input(text):
    with pytest.raises(ValueError, match="Usage"):
        commands.parse(text)


def test_unknown_command_and_plain_text():
    for text in ("/sell NVDA 1 1", "hello", ""):
        with pytest.raises(ValueError, match="/help"):
            commands.parse(text)
    assert "/help" in say(state.empty(), "hello") and "/bought SYM QTY PRICE" in say(state.empty(), "/help")


def test_buy_and_sell_cash_math_with_fees_and_fifo():
    st = state.empty()
    assert "Cash ≈ 1 000 000 HUF" in say(st, "/cash 1000000")
    r = say(st, "/bought nvda 2 100,5")  # 201 USD, minimum fee 3.49 USD, at 350 HUF/USD
    assert "Bought 2 NVDA at 100.5 USD" in r and "fee 3.49 USD" in r
    assert "Converted ≈ 72 287 HUF → $204.49 (1% ≈ 716 HUF)" in r  # no USD balance: from HUF at FX x 1.01
    assert st["log"][-2] == dict(t="fx", ccy="USD", amt=204.49, huf=round(204.49 * 350 * 1.01, 2), auto=True,
                                 at=st["log"][-1]["at"])
    assert state.book(st["log"])["cash"] == pytest.approx({"HUF": 1_000_000 - 204.49 * 350 * 1.01, "USD": 0})
    say(st, "/bought NVDA 3 110")
    r = say(st, "/sold NVDA 4 120")  # proceeds 480 - 3.49; FIFO cost 204.49 + 2/3 of 333.49
    pl = (480 - 3.49) * 350 - (204.49 + 333.49 * 2 / 3) * 350
    assert st["log"][-1]["pl_huf"] == round(pl) and "Realised P/L ≈ +" in r
    b = state.book(st["log"])
    assert state.qty(b["pos"]["NVDA"]) == 1
    # the sale credits USD (no auto-convert back); the two buys were converted from HUF
    assert b["cash"] == pytest.approx({"HUF": 1_000_000 - (204.49 + 333.49) * 350 * 1.01, "USD": 476.51})
    assert "You hold 1 NVDA, cannot sell 2" in say(st, "/sold NVDA 2 120") and len(st["log"]) == 6  # 2 conversions


def test_huf_trade_uses_bot_ticker_and_needs_no_fx():
    st = state.empty()
    r = commands.handle(st, "/bought mol 10 3000", NOW, lambda s, period: {}, info)  # FX download fails: fine
    assert "Bought 10 MOL.BD at 3000 HUF (fee 349.00 HUF)" in r and "Cash not set yet" in r
    assert st["log"][-1]["fx"] == 1.0


def test_unknown_symbols():
    st = state.empty()
    assert "Unknown symbol NOPE" in say(st, "/bought NOPE 1 1") and not st["log"]
    r = say(st, "/bought zzzz 1 5")  # yfinance knows it: accepted, and the reply names the currency first
    assert r.startswith("Recorded in USD — /undo if wrong. Bought 1 ZZZZ at 5 USD")
    st2 = state.empty()
    assert "No USD/HUF rate" in commands.handle(st2, "/bought NVDA 1 5", NOW, lambda s, period: {}, info)
    assert not st2["log"]


def test_deposit_withdraw_and_undo():
    st = state.empty()
    assert "Nothing to undo" in say(st, "/undo")
    say(st, "/deposit 100000")
    say(st, "/withdraw 30000,5")
    assert state.book(st["log"])["cash"] == pytest.approx({"HUF": 69_999.5})
    say(st, "/bought MOL.BD 1 3000")
    r = say(st, "/undo")
    assert "Removed: buy 1 MOL.BD at 3000 HUF" in r and "Cash ≈ 70 000 HUF" in r
    assert "Removed: withdraw 30 000 HUF" in say(st, "/undo") and len(st["log"]) == 1


def test_holdings_top_and_scan():
    st = state.empty()
    assert "No holdings recorded" in say(st, "/holdings")
    assert "No scan yet" in say(st, "/top")
    say(st, "/cash 50000")
    say(st, "/bought MOL.BD 10 3000")
    st["scan"] = dict(at="2026-10-27T06:45:00+00:00", items=[[f"S{i}", f"Name {i}", 90 - i, "BUY"] for i in range(12)]
                      + [["MOL.BD", "MOL", 40, None]], moms=None)
    r = say(st, "/holdings")
    assert r.startswith("💼 YOUR HOLDINGS\n🟢 MOL · 10 @ 3000 HUF · now 3300 HUF · +8.7%\n   score 40 · hold\n")
    assert "📦 Holdings ≈ 33 000 HUF. 💰 Cash ≈ 19 651 HUF." in r and "🧮 Total ≈ 52 651 HUF" in r  # 33 000 / 30 349
    assert "excl." not in r and "🧾 Realised P/L 2026 ≈ +0 HUF" in r
    t = say(st, "/top")
    assert t.startswith("🏆 Top 10 · scan of Tue 27 Oct, 07:45\n1️⃣ Name 0 (S0) · 90 🔥 · 🟢 BUY\n")
    assert "🔟 Name 9 (S9) · 81 ✅ · 🟢 BUY" in t and "S10" not in t
    s = say(st, "/scan nvda")
    assert s.startswith("🔍 Nvidia (NVDA) · ") and "/100" in s and "\nCall: " in s and "📝 Score " in s
    assert "No scan yet to rank momentum" in s
    assert "Unknown symbol" in say(st, "/scan NOPE")


def test_holdings_total_names_positions_left_out():
    st = state.empty()
    say(st, "/cash 100000")
    say(st, "/bought MOL.BD 1 3000")
    say(st, "/bought ZZZZ 1 5")
    r = commands.handle(st, "/holdings", NOW, lambda s, period: {"MOL.BD": flat(3300.0)}, info)  # no FX, no ZZZZ
    assert "⚪ ZZZZ · 1 @ $5 · no price" in r and "Holdings ≈ 3 300 HUF (excl. 1 without price/FX)." in r
    assert "(excl. 1 without price/FX)" in r.split("Total ≈")[1]


def test_bare_tickers_resolve_to_the_universe_and_bot_tickers():
    for sym, want in (("SAP", "SAP.DE"), ("OTP", "OTP.BD"), ("ASML", "ASML.AS"), ("MOL", "MOL.BD"),
                      ("CSPX", "CSPX.L"), ("NVDA", "NVDA")):
        assert commands.resolve(sym, {}, download) == (want, False)
    assert commands.resolve("ZZZZ", {}, download) == ("ZZZZ", True)
    assert commands.resolve("NOPE", {}, download) == (None, False)


def test_thousands_separators_are_rejected_for_huf_amounts():
    for text in ("/deposit 120.000", "/cash 1,500", "/withdraw 1.200.000", "/cash 120,000.50"):
        with pytest.raises(ValueError, match="without thousands separators"):
            commands.parse(text)
    assert commands.parse("/deposit 120000,5") == ("deposit", (120000.5, "HUF"))
    with pytest.raises(ValueError, match="cash currencies: HUF, USD, EUR"):
        commands.parse("/cash 100 GBP")
    assert commands.parse("/bought SAP 2 212,345") == ("bought", ("SAP", 2, 212.345))  # prices: decimals are fine


def test_negative_cash_is_accepted_with_a_warning():
    st = state.empty()
    say(st, "/cash 1000")
    r = say(st, "/bought MOL 1 3000")
    assert "Cash ≈ -2 349 HUF. Warning: cash is below 0" in r and len(st["log"]) == 2
    assert "Warning" in say(st, "/withdraw 1") and "Warning" not in say(st, "/deposit 10000")


class Telegram:
    """getUpdates that, like the real one, returns only updates at or after the offset; send fails while down."""

    def __init__(self, updates, down=False):
        self.updates, self.offsets, self.sent, self.down = updates, [], [], down

    def get(self, token, offset):
        self.offsets.append(offset)
        return [u for u in self.updates if u["update_id"] >= offset]

    def send(self, token, chat, text):
        if self.down:
            raise RuntimeError("Telegram send failed: URLError: bot***")
        self.sent.append((chat, text))


def msg(uid, text, chat=42, sender=None, kind="private"):
    return {"update_id": uid, "message": {"chat": {"id": chat, "type": kind},
                                          "from": {"id": chat if sender is None else sender}, "text": text}}


def go(tmp_path, tg):
    out = StringIO()
    code = commands.run(str(tmp_path / "state.enc"), ENV, NOW, tg.get, tg.send, download, info, out)
    return code, out.getvalue()


def test_chat_filter_and_offset_advance(tmp_path):
    tg = Telegram([msg(5, "/deposit 1000"), msg(6, "/deposit 999", chat=666), msg(7, "/cash 5000"),
                   msg(8, "/cash 1", sender=777), msg(9, "/cash 2", kind="group"),
                   {"update_id": 10, "edited_message": {"chat": {"id": 42, "type": "private"}, "text": "/cash 3"}}])
    code, log = go(tmp_path, tg)
    assert code == 0 and "2 command(s) handled" in log and "5000" not in log
    assert [c for c, _ in tg.sent] == ["42", "42"] and "999" not in str(tg.sent)
    st = state.load(str(tmp_path / "state.enc"), TOKEN)
    assert st["offset"] == 11 and [e["huf"] for e in st["log"]] == [1000, 5000]
    assert (tmp_path / "offset.txt").read_text() == "11" and (tmp_path / "outbox_pending").read_text() == "0"
    blob = (tmp_path / "state.enc").read_bytes()
    go(tmp_path, tg)  # same updates again: nothing is handled twice, the file is not rewritten
    assert tg.offsets == [0, 11] and len(tg.sent) == 2 and (tmp_path / "state.enc").read_bytes() == blob


def test_reset_notice_once_and_bad_command_does_not_stop_the_rest(tmp_path):
    state.save(dict(state.empty(), offset=3), str(tmp_path / "state.enc"), "999:old-token")
    tg = Telegram([msg(1, "/bought"), msg(2, "/deposit 10")])
    go(tmp_path, tg)
    assert tg.sent[0][1] == commands.RESET and "Usage" in tg.sent[1][1] and "recorded" in tg.sent[2][1]
    assert len(list(tmp_path.glob("state.enc.bad-*"))) == 1  # the undecryptable file is kept
    go(tmp_path, Telegram([]))
    st = state.load(str(tmp_path / "state.enc"), TOKEN)
    assert "reset" not in st and st["offset"] == 3


def test_reset_flag_stays_until_the_notice_is_sent(tmp_path):
    state.save(state.empty(), str(tmp_path / "state.enc"), "999:old-token")
    code, _ = go(tmp_path, Telegram([], down=True))
    assert code == 1 and state.load(str(tmp_path / "state.enc"), TOKEN)["reset"] is True
    assert (tmp_path / "outbox_pending").read_text() == "1"
    tg = Telegram([])
    assert go(tmp_path, tg)[0] == 0 and tg.sent == [("42", commands.RESET)]
    assert "reset" not in state.load(str(tmp_path / "state.enc"), TOKEN)


def test_failed_replies_wait_in_the_outbox_and_go_first_next_run(tmp_path):
    tg = Telegram([msg(1, "/cash 5000"), msg(2, "/deposit 10")], down=True)
    code, log = go(tmp_path, tg)
    st = state.load(str(tmp_path / "state.enc"), TOKEN)
    assert code == 1 and "2 replies waiting" in log and TOKEN not in log
    assert len(st["log"]) == 2 and st["offset"] == 3 and len(st["outbox"]) == 2  # recorded once, replies kept
    assert (tmp_path / "outbox_pending").read_text() == "1"
    tg2 = Telegram([msg(3, "/undo")])
    assert go(tmp_path, tg2)[0] == 0
    texts = [t for _, t in tg2.sent]
    assert texts[0].startswith("(delayed reply) Cash 5 000 HUF recorded") and texts[1].startswith("(delayed reply) ")
    assert texts[2].startswith("Removed: deposit") and not state.load(str(tmp_path / "state.enc"), TOKEN)["outbox"]


def test_expected_state_missing_stops_without_saving(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_EXPECTED", "1")
    tg = Telegram([msg(1, "/cash 5")])
    code, log = go(tmp_path, tg)
    assert code == 1 and "missing" in log and not tg.offsets and not (tmp_path / "state.enc").exists()


def test_missing_secrets_and_telegram_errors_hide_the_token(tmp_path):
    assert commands.run(str(tmp_path / "s.enc"), {}, NOW, out=StringIO()) == 1

    def opener(url, timeout):
        raise OSError(f"cannot reach {url}")
    with pytest.raises(RuntimeError) as e:
        commands.get_updates(TOKEN, 0, opener=opener)
    assert TOKEN not in str(e.value) and "***" in str(e.value)


def test_no_scan_warning_once_a_day(tmp_path):
    from datetime import datetime, timezone
    (tmp_path / "last_scan.txt").write_text("2026-10-26T06:50:00+00:00")
    tg = Telegram([])
    out = StringIO()
    tue = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)
    assert commands.run(str(tmp_path / "state.enc"), ENV, tue, tg.get, tg.send, download, info, out) == 0
    assert [t for _, t in tg.sent] == ["⚠️ No market scan since Mon 26 Oct, 07:50. Check the listener workflow (listen.yml) in GitHub "
                       "Actions (a failed run, or the chain of runs broke)."]
    assert (tmp_path / "scan_warned.txt").read_text() == "2026-10-27"
    commands.run(str(tmp_path / "state.enc"), ENV, tue, tg.get, tg.send, download, info, out)
    assert len(tg.sent) == 1


def test_permanent_send_errors_are_dropped_not_kept_in_the_outbox(tmp_path):
    from invest_bot import cloud

    def refused(token, chat, text):
        raise cloud.Permanent("Telegram send failed: HTTPError: HTTP Error 403: Forbidden")
    tg = Telegram([msg(1, "/deposit 1000")])
    out = StringIO()
    code = commands.run(str(tmp_path / "state.enc"), ENV, NOW, tg.get, refused, download, info, out)
    st = state.load(str(tmp_path / "state.enc"), TOKEN)
    assert code == 1 and st["outbox"] == [] and len(st["log"]) == 1 and "(permanent: dropped)" in out.getvalue()
    assert (tmp_path / "outbox_pending").read_text() == "0"  # the precheck will not rerun for it


def test_bought_adds_the_buy_side_tax_to_cash_and_the_fifo_cost():
    st = state.empty()
    say(st, "/cash 1000000")
    r = say(st, "/bought MC.PA 10 500")  # LVMH, Paris: 5 000 EUR, fee 12 EUR, FR tax 0.4% = 20 EUR
    assert "fee 12.00 EUR + FR tax 0.4% 20.00 EUR" in r and st["log"][-1]["tax"] == 20.0
    b = state.book(st["log"])
    assert b["pos"]["MC.PA"]["lots"][0][2] == pytest.approx(5032 * 400)  # the tax is part of the cost basis
    r = say(st, "/sold MC.PA 10 500")  # selling at the same price realises the fees and the tax as a loss
    assert st["log"][-1]["pl_huf"] == round((5000 - 12) * 400 - 5032 * 400)
    say(st, "/bought CSPX 1 600")  # an ETF: no tax
    assert "tax" not in st["log"][-1]


def test_core_command_sets_the_share():
    st = state.empty()
    assert say(st, "/core 70").startswith("🧱 Core target 70%") and st["core_share"] == 0.7
    assert say(st, "/core 0") == "🧱 Core off: the buy plan is momentum picks only." and st["core_share"] == 0
    assert say(st, "/core 150").startswith("Usage: /core") and st["core_share"] == 0
