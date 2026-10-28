import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest

from invest_bot import listen, state
from test_commands import download, info

TOKEN = "123456:SECRET-token-value"
CHAT = "987654321"
TUE = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)  # no scan slot due


def at(*a):
    return datetime(*a, tzinfo=timezone.utc)


def msg(uid, text, chat=CHAT):
    return {"update_id": uid, "message": {"chat": {"id": int(chat), "type": "private"},
                                          "from": {"id": int(chat)}, "text": text}}


class Fake:
    """A clock plus Telegram: a long poll returns the updates at or after the offset that have arrived, else waits
    out its timeout. fail: how many polls raise first."""

    def __init__(self, start, updates=(), fail=0):
        self.t, self.updates, self.fail = start, list(updates), fail
        self.sent, self.offsets, self.sleeps, self.scans, self.pushes = [], [], [], [], []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += timedelta(seconds=s)

    def get(self, token, offset, timeout=0):
        assert token == TOKEN
        self.offsets.append(offset)
        if self.fail:
            self.fail -= 1
            self.t += timedelta(seconds=1)
            raise RuntimeError(f"getUpdates failed: {TOKEN} {CHAT} /cash 5000")
        waiting = [t for t, u in self.updates if u["update_id"] >= offset]
        first = min(waiting, default=None)
        if first is None or first > self.t + timedelta(seconds=timeout):
            self.t += timedelta(seconds=timeout)
            return []
        self.t = max(self.t, first)  # the long poll returns as soon as a message arrives
        return [u for t, u in self.updates if t <= self.t and u["update_id"] >= offset]

    def send(self, token, chat, text):
        self.sent.append(text)

    def scan(self, path, force=False):
        self.scans.append(("forced", self.t) if force else self.t)
        self.t += timedelta(minutes=3)
        return 0

    def push(self):
        self.pushes.append(self.t)


def run(tmp_path, fake, minutes=10, **kw):
    out = StringIO()
    code = listen.listen(str(tmp_path / "state.enc"), TOKEN, CHAT, minutes, push_fn=fake.push, now_fn=fake.now,
                         sleep_fn=fake.sleep, updates_fn=fake.get, send_fn=fake.send, scan_fn=fake.scan,
                         download_fn=download, info_fn=info, out=out, **kw)
    return code, out.getvalue()


def load(tmp_path):
    return state.load(str(tmp_path / "state.enc"), TOKEN)


def test_commands_are_handled_once_and_the_log_shows_counts_only(tmp_path):
    fake = Fake(TUE, [(TUE, msg(1, "/cash 5000")), (TUE + timedelta(minutes=2), msg(2, "/deposit 1234")),
                      (TUE + timedelta(minutes=3), msg(3, "/cash 1", chat="666"))])
    code, log = run(tmp_path, fake)
    assert code == 0 and len(fake.sent) == 2 and "Cash 5 000 HUF recorded" in fake.sent[0]
    st = load(tmp_path)
    assert st["offset"] == 4 and [e["huf"] for e in st["log"]] == [5000, 1234]
    assert set(fake.offsets) == {0, 2, 3, 4} and fake.offsets.count(0) == 1  # never fetched from an old offset again
    assert "command(s) handled" in log
    for secret in (TOKEN, CHAT, "5000", "5 000", "1234", "/cash"):
        assert secret not in log
    fake2 = Fake(TUE + timedelta(minutes=10), fake.updates)  # a restart: the offset comes from the state
    run(tmp_path, fake2)
    assert fake2.sent == [] and min(fake2.offsets) == 4


def test_state_is_saved_and_pushed_only_on_change(tmp_path, monkeypatch):
    saves = []
    real = state.save
    monkeypatch.setattr(state, "save", lambda *a: (saves.append(1), real(*a)))
    fake = Fake(TUE)
    assert run(tmp_path, fake, minutes=30)[0] == 0
    assert saves == [] and fake.pushes == []  # nothing happened: no write, no push
    fake = Fake(TUE, [(TUE, msg(1, "hi", chat="666")), (TUE + timedelta(minutes=5), msg(2, "/deposit 10"))])
    run(tmp_path, fake, minutes=30)
    # the stranger moves only the offset: pushed after a minute, not at once; the deposit is pushed right away
    assert fake.pushes[0] - TUE >= listen.OFFSET_PUSH and fake.pushes[1] - TUE == timedelta(minutes=5)
    assert len(fake.pushes) == 2 and len(saves) == 2


def test_an_unpushed_offset_is_flushed_before_exit(tmp_path):
    fake = Fake(TUE, [(TUE + timedelta(minutes=8, seconds=20), msg(1, "/deposit 10")),
                      (TUE + timedelta(minutes=8, seconds=30), msg(2, "/help"))])
    run(tmp_path, fake)
    # the deposit is pushed at once; the /help offset 10 s later waits for the minute, then the final flush
    assert fake.pushes[0] == TUE + timedelta(minutes=8, seconds=20) and len(fake.pushes) == 2
    assert fake.pushes[1] >= TUE + timedelta(minutes=9)


def test_failed_push_is_retried_and_reported(tmp_path):
    fake = Fake(TUE, [(TUE, msg(1, "/deposit 10"))])
    calls = []

    def push():
        calls.append(fake.t)
        if len(calls) < 3:
            raise OSError("network")
    out = StringIO()
    code = listen.listen(str(tmp_path / "state.enc"), TOKEN, CHAT, 10, push_fn=push, now_fn=fake.now,
                         sleep_fn=fake.sleep, updates_fn=fake.get, send_fn=fake.send, scan_fn=fake.scan, out=out)
    assert code == 0 and len(calls) == 3 and "state push failed (OSError)" in out.getvalue()


def test_conflict_stops_the_loop(tmp_path):
    fake = Fake(TUE, [(TUE, msg(1, "/deposit 10"))])

    def push():
        raise listen.Conflict("moved")
    out = StringIO()
    code = listen.listen(str(tmp_path / "state.enc"), TOKEN, CHAT, 60, push_fn=push, now_fn=fake.now,
                         sleep_fn=fake.sleep, updates_fn=fake.get, send_fn=fake.send, scan_fn=fake.scan, out=out)
    assert code == 1 and fake.t - TUE < timedelta(minutes=2) and "NOT pushed" in out.getvalue()


def test_network_errors_back_off_and_the_loop_survives(tmp_path):
    fake = Fake(TUE, [(TUE, msg(1, "/cash 5000"))], fail=6)
    code, log = run(tmp_path, fake)
    assert code == 0 and fake.sleeps == [5, 10, 20, 40, 60, 60] and len(fake.sent) == 1
    assert log.count("poll failed (RuntimeError)") == 6
    assert TOKEN not in log and CHAT not in log and "5000" not in log


def test_unexpected_errors_are_logged_by_type_only(tmp_path, monkeypatch):
    from invest_bot import commands
    boom = [2]
    real = commands.step

    def step(*a, **k):
        if boom[0]:
            boom[0] -= 1
            raise KeyError(f"holding NVDA 12 shares {CHAT}")
        return real(*a, **k)
    monkeypatch.setattr(commands, "step", step)
    code, log = run(tmp_path, Fake(TUE, [(TUE, msg(1, "/help"))]))
    assert code == 0 and log.count("poll failed (KeyError)") == 2 and "NVDA" not in log and CHAT not in log


@pytest.mark.parametrize("now, slot", [
    (at(2026, 10, 27, 6, 49), None),                       # before the slot
    (at(2026, 10, 27, 6, 50), at(2026, 10, 27, 6, 50)),
    (at(2026, 10, 27, 9, 50), at(2026, 10, 27, 6, 50)),    # catch-up: 3 h late
    (at(2026, 10, 27, 9, 51), None),                       # too late
    (at(2026, 10, 28, 0, 10), at(2026, 10, 27, 21, 15)),   # past midnight
    (at(2026, 10, 31, 9, 0), None),                        # Saturday
    (at(2026, 10, 31, 0, 5), at(2026, 10, 30, 21, 15)),    # Friday evening, caught up on Saturday
])
def test_due_slot(now, slot):
    assert listen.due_slot({}, now) == slot


def test_due_slot_is_done_when_marked_or_a_scan_ran_after_it():
    t = at(2026, 10, 27, 6, 50)
    assert listen.due_slot(dict(scan_slot=t.isoformat()), t + timedelta(hours=1)) is None
    assert listen.due_slot(dict(scan=dict(at="2026-10-27T06:52:10+00:00")), t + timedelta(hours=1)) is None
    assert listen.due_slot(dict(scan=dict(at="2026-10-26T21:15:10+00:00")), t + timedelta(hours=1)) == t


def tap(uid, data, chat=CHAT, message_id=77):
    return {"update_id": uid, "callback_query": {
        "id": str(uid), "from": {"id": int(chat)}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": int(chat), "type": "private"}}}}


def test_bought_button_records_once_and_only_from_the_owner(tmp_path):
    fake = Fake(TUE, [(TUE, tap(1, "/bought MOL 10 3000")), (TUE, tap(2, "/bought MOL 10 3000")),
                      (TUE, tap(3, "/bought MOL 10 3000", chat="666"))])
    run(tmp_path, fake)
    st = load(tmp_path)
    assert [(e["t"], e["sym"], e["qty"]) for e in st["log"] if "sym" in e] == [("buy", "MOL.BD", 10)]  # once
    assert len(fake.sent) == 2 and fake.sent[1].startswith("Already recorded")  # the stranger got nothing
    assert st["taps"] == ["77|/bought MOL 10 3000"]


def test_refresh_runs_one_forced_scan_right_after_the_reply(tmp_path):
    noon = at(2026, 10, 27, 12, 0)
    fake = Fake(noon, [(noon, msg(1, "/refresh"))])
    run(tmp_path, fake, minutes=30)
    assert fake.sent == ["🔄 Scanning now: the full message with your buy plan arrives in about 2 minutes."]
    assert fake.scans == [("forced", noon)]  # once, not a slot scan, and no slot is marked
    st = load(tmp_path)
    assert "scan_now" not in st and "scan_slot" not in st
    st["scan"] = dict(at=(noon + timedelta(minutes=5)).isoformat(), items=[], moms={})  # a scan 3 min before the next
    state.save(st, str(tmp_path / "state.enc"), TOKEN)
    later = noon + timedelta(minutes=8)
    again = Fake(later, [(later, msg(2, "/refresh"))])
    run(tmp_path, again, minutes=30)
    assert again.scans == [] and again.sent[0].startswith("⏳ A scan ran 3 min ago")


def test_scan_runs_once_per_slot_and_a_restart_does_not_repeat_it(tmp_path):
    fake = Fake(at(2026, 10, 27, 6, 40))
    run(tmp_path, fake, minutes=40)
    assert len(fake.scans) == 1 and at(2026, 10, 27, 6, 50) <= fake.scans[0] < at(2026, 10, 27, 6, 52)
    assert load(tmp_path)["scan_slot"] == "2026-10-27T06:50:00+00:00" and len(fake.pushes) == 1
    again = Fake(at(2026, 10, 27, 7, 30))
    run(tmp_path, again, minutes=30)
    assert again.scans == []


def test_missed_slot_is_caught_up_at_start_but_not_near_the_end(tmp_path):
    fake = Fake(at(2026, 10, 27, 8, 30))
    run(tmp_path, fake, minutes=20)
    assert fake.scans == [at(2026, 10, 27, 8, 30)]
    late = Fake(at(2026, 10, 27, 21, 10))  # the 21:15 slot comes 10 min before the end: left to the next run
    run(tmp_path / "b", late, minutes=15)
    assert late.scans == []


def test_failed_scan_still_marks_the_slot(tmp_path):
    fake = Fake(at(2026, 10, 27, 6, 50))

    def scan(path):
        raise ValueError("NVDA 3 shares")
    out = StringIO()
    listen.listen(str(tmp_path / "state.enc"), TOKEN, CHAT, 20, now_fn=fake.now, sleep_fn=fake.sleep,
                  updates_fn=fake.get, send_fn=fake.send, scan_fn=scan, out=out)
    assert "slot done (error ValueError)" in out.getvalue() and "NVDA" not in out.getvalue()
    assert load(tmp_path)["scan_slot"] == "2026-10-27T06:50:00+00:00"


def test_heartbeat_warning_still_goes_out(tmp_path):
    (tmp_path / "last_scan.txt").write_text("2026-10-26T06:50:00+00:00")
    fake = Fake(at(2026, 10, 27, 9, 55))  # past the 06:50 catch-up window, 27 weekday hours since the last scan
    run(tmp_path, fake)
    assert len(fake.sent) == 1 and fake.sent[0].startswith("⚠️ No market scan since")


class Opener:
    def __init__(self, body=b"", fail=0):
        self.reqs, self.body, self.fail = [], body, fail

    def __call__(self, req, timeout):
        self.reqs.append(req)
        if self.fail:
            self.fail -= 1
            raise OSError(f"down {req.headers}")
        outer = self

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return outer.body
        return R()


GH = {"GITHUB_REPOSITORY": "someone/bot", "GITHUB_REF_NAME": "main", "GITHUB_TOKEN": "ghs_SECRET", "GITHUB_RUN_ID": "7"}


def test_dispatch_posts_the_next_run_without_logging_the_token():
    op, out = Opener(fail=1), StringIO()
    assert listen.dispatch(GH, op, lambda s: None, out) is True
    req = op.reqs[-1]
    assert req.full_url == "https://api.github.com/repos/someone/bot/actions/workflows/listen.yml/dispatches"
    assert req.get_method() == "POST" and json.loads(req.data) == {"ref": "main"}
    assert req.get_header("Authorization") == "Bearer ghs_SECRET"
    assert "ghs_SECRET" not in out.getvalue() and "next listener run requested" in out.getvalue()
    assert listen.dispatch({}, op, out=out) is False


def test_main_runs_the_listener_then_dispatches(monkeypatch):
    calls = []
    monkeypatch.setattr(listen, "listen", lambda *a: calls.append(("listen", a[3])) or 0)
    monkeypatch.setattr(listen, "dispatch", lambda env: calls.append(("dispatch", env["GITHUB_REF_NAME"])))
    monkeypatch.setattr(listen.signal, "signal", lambda *a: None)
    env = dict(GH, TELEGRAM_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT, GITHUB_EVENT_NAME="workflow_dispatch")
    assert listen.main(["--max-minutes", "345"], env) == 0
    assert calls == [("listen", 345), ("dispatch", "main")]


def runs(*items):
    return json.dumps({"workflow_runs": [dict(id=i, status=s) for i, s in items]}).encode()


def test_fallback_run_exits_when_another_listener_is_active(monkeypatch):
    assert listen.another_run(GH, Opener(runs((7, "in_progress"), (5, "completed")))) is False  # only itself
    assert listen.another_run(GH, Opener(runs((7, "in_progress"), (6, "in_progress")))) is True
    assert listen.another_run(GH, Opener(runs((7, "in_progress"), (8, "queued")))) is True
    assert listen.another_run(GH, Opener(fail=1), out=StringIO()) is False  # API down: carry on
    req = Opener(runs())
    listen.another_run(GH, req)
    assert req.reqs[0].full_url.startswith("https://api.github.com/repos/someone/bot/actions/workflows/listen.yml/runs")
    called = []
    monkeypatch.setattr(listen, "another_run", lambda env: True)
    monkeypatch.setattr(listen, "listen", lambda *a: called.append(1) or 0)
    monkeypatch.setattr(listen, "dispatch", lambda env: called.append(2))
    env = dict(GH, TELEGRAM_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT, GITHUB_EVENT_NAME="schedule")
    assert listen.main([], env) == 0 and called == []  # no listening and no dispatch: the other run carries on


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    remote, work, tmp = tmp_path / "remote.git", tmp_path / "work", tmp_path / "runner"
    tmp.mkdir()
    git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git("init", "-q", str(work), cwd=tmp_path)
    git("remote", "add", "origin", str(remote), cwd=work)
    (work / "state").mkdir()
    (tmp / "state-base").write_text("none\n")
    monkeypatch.chdir(work)
    return remote, work, tmp


def test_git_helper_pushes_with_a_lease_and_detects_a_moved_branch(repo):
    remote, work, tmp = repo
    (work / "state" / "state.enc").write_bytes(b"one")
    (work / "state" / "offset.txt").write_text("1")
    g = listen.Git("state", str(tmp))
    assert g.save() is True
    first = git("rev-parse", "refs/heads/state", cwd=remote)
    assert first == g.base == (tmp / "state-base").read_text()
    assert git("show", "state:state/offset.txt", cwd=remote) == "1"
    assert g.save() is False  # unchanged: no commit
    (work / "state" / "offset.txt").write_text("2")
    stale = listen.Git("state", str(tmp))  # reads the same base as g
    assert g.save() is True and git("rev-parse", f"refs/heads/state~1", cwd=remote) == first
    (work / "state" / "offset.txt").write_text("3")
    stale.base = first
    with pytest.raises(listen.Conflict):
        stale.save()
    assert git("show", "state:state/offset.txt", cwd=remote) == "2"
