import pytest

from invest_bot import state

TOKEN = "123456:SECRET-token-value"


def test_round_trip_is_encrypted(tmp_path):
    path = str(tmp_path / "state" / "state.enc")
    st = state.empty()
    st["log"].append(dict(t="buy", sym="RICHT.BD", qty=3, price=10000, ccy="HUF", fx=1.0, fee=349, at="x"))
    state.save(st, path, TOKEN)
    blob = (tmp_path / "state" / "state.enc").read_bytes()
    assert b"RICHT" not in blob and b"10000" not in blob
    assert state.load(path, TOKEN) == st


def test_missing_file_is_empty(tmp_path):
    assert state.load(str(tmp_path / "nope.enc"), TOKEN) == state.empty()


def test_wrong_key_resets_with_flag(tmp_path):
    path = str(tmp_path / "s.enc")
    st = state.empty()
    st["offset"] = 7
    state.save(st, path, TOKEN)
    out = state.load(path, "999:other-token")
    assert out["reset"] is True and out["offset"] == 0 and out["log"] == []
    (tmp_path / "s.enc").write_bytes(b"garbage")
    assert state.load(path, TOKEN)["reset"] is True


def buy(sym, q, px, fee=0.0, fx=1.0, ccy="HUF"):
    return dict(t="buy", sym=sym, qty=q, price=px, ccy=ccy, fx=fx, fee=fee, at="2026-10-26")


def sell(sym, q, px, fee=0.0, fx=1.0, ccy="HUF"):
    return dict(buy(sym, q, px, fee, fx, ccy), t="sell")


def test_book_fifo_and_cash():
    log = [dict(t="cash", huf=100_000), buy("A", 10, 100, fee=10), buy("A", 10, 200, fee=10),
           sell("A", 15, 300, fee=20), dict(t="deposit", huf=1000), dict(t="withdraw", huf=500)]
    b = state.book(log)
    # sale proceeds 4480; FIFO cost: the 10 @ 100 lot (1010) + half the 10 @ 200 lot (1005)
    assert b["realised"] == [pytest.approx(4480 - 2015)]
    assert state.qty(b["pos"]["A"]) == 5 and b["pos"]["A"]["lots"] == [[5, 200, pytest.approx(1005), pytest.approx(1005)]]
    assert b["cash"] == pytest.approx({"HUF": 100_000 - 1010 - 2010 + 4480 + 1000 - 500})


def test_book_without_cash_entries_has_unknown_cash_and_sold_out_positions_go():
    b = state.book([buy("A", 1, 10, fx=400, ccy="EUR"), sell("A", 1, 12, fx=410, ccy="EUR")])
    assert b["cash"] is None and b["pos"] == {} and b["realised"] == [pytest.approx(12 * 410 - 10 * 400)]


def test_portfolio_shape():
    assert state.portfolio(state.empty()) is None
    st = dict(state.empty(), log=[buy("MOL.BD", 4, 3000, fee=349), buy("MOL.BD", 6, 3500)])
    p = state.portfolio(st)
    assert p["cash_huf"] is None and p["cash"] is None and p["as_of"] == "2026-10-26"
    assert p["holdings"] == [dict(symbol="MOL.BD", qty=10, ccy="HUF", avg_cost_huf=pytest.approx(3334.9))]


def test_expected_but_missing_file_raises(tmp_path, monkeypatch):
    with pytest.raises(RuntimeError, match="missing"):
        state.load(str(tmp_path / "s.enc"), TOKEN, expected=True)
    monkeypatch.setenv("STATE_EXPECTED", "1")  # what the workflow sets when the branch has a state.enc
    with pytest.raises(RuntimeError):
        state.load(str(tmp_path / "s.enc"), TOKEN)


def test_undecryptable_file_is_kept_and_token_whitespace_is_ignored(tmp_path):
    path = str(tmp_path / "s.enc")
    state.save(dict(state.empty(), offset=5), path, TOKEN + "\n")
    assert state.load(path, "  " + TOKEN)["offset"] == 5  # same key with stray whitespace
    blob = (tmp_path / "s.enc").read_bytes()
    assert state.load(path, "999:other")["reset"] is True
    bad = list(tmp_path.glob("s.enc.bad-*"))
    assert len(bad) == 1 and bad[0].read_bytes() == blob


def test_save_writes_the_plaintext_precheck_files(tmp_path):
    path = str(tmp_path / "s.enc")
    state.save(dict(state.empty(), offset=12), path, TOKEN)
    assert (tmp_path / "offset.txt").read_text() == "12" and (tmp_path / "outbox_pending").read_text() == "0"
    state.save(dict(state.empty(), outbox=["hi"]), path, TOKEN)
    assert (tmp_path / "outbox_pending").read_text() == "1"
