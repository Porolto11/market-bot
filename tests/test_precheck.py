import json

from invest_bot import precheck

TOKEN = "123456:SECRET-token-value"


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def test_idle_when_no_updates_and_nothing_waiting(tmp_path):
    (tmp_path / "offset.txt").write_text("41")
    (tmp_path / "outbox_pending").write_text("0")
    seen = []

    def opener(url, timeout):
        seen.append(url)
        return Resp({"ok": True, "result": []})
    assert precheck.needed(str(tmp_path), TOKEN, opener) is False
    assert "offset=41" in seen[0] and "limit=1" in seen[0]


def test_work_when_updates_outbox_or_errors(tmp_path):
    assert precheck.needed(str(tmp_path), TOKEN, lambda u, timeout: Resp({"ok": True, "result": [{}]})) is True
    assert precheck.needed(str(tmp_path), TOKEN, lambda u, timeout: Resp({"ok": False})) is True

    def down(url, timeout):
        raise OSError("no network")
    assert precheck.needed(str(tmp_path), TOKEN, down) is True
    (tmp_path / "outbox_pending").write_text("1")
    assert precheck.needed(str(tmp_path), TOKEN, down) is True  # no call needed
    assert precheck.needed(str(tmp_path), "", down) is True


def test_main_prints_only_the_output_line(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_TOKEN", TOKEN)
    monkeypatch.setattr(precheck.urllib.request, "urlopen", lambda u, timeout: Resp({"ok": True, "result": []}))
    assert precheck.main() == 0
    out = capsys.readouterr()
    assert out.out == "work=0\n" and TOKEN not in out.err


def test_overdue_scan_counts_weekday_hours_only_and_warns_once_a_day(tmp_path):
    from datetime import datetime, timezone
    at = lambda *a: datetime(*a, tzinfo=timezone.utc)
    folder = str(tmp_path)
    assert precheck.overdue(folder, at(2026, 10, 27, 9)) is None  # no scan yet
    (tmp_path / "last_scan.txt").write_text("2026-10-26T06:50:00+00:00")  # Monday morning
    assert precheck.overdue(folder, at(2026, 10, 27, 8)) is None  # 25.2 h
    assert precheck.overdue(folder, at(2026, 10, 27, 9)) == at(2026, 10, 26, 6, 50)  # 26.2 h
    assert precheck.needed(folder, TOKEN, lambda u, timeout: Resp({"ok": True, "result": []}),
                           now=at(2026, 10, 27, 9)) is True
    (tmp_path / "scan_warned.txt").write_text("2026-10-27")
    assert precheck.overdue(folder, at(2026, 10, 27, 9)) is None  # already warned today
    assert precheck.overdue(folder, at(2026, 10, 28, 9)) is not None  # the next day again
    (tmp_path / "last_scan.txt").write_text("2026-10-30T21:15:00+00:00")  # Friday evening
    assert precheck.overdue(folder, at(2026, 11, 1, 20)) is None  # Sunday: no warning
    assert precheck.overdue(folder, at(2026, 11, 2, 20)) is None  # Monday 20:00: 22.75 weekday hours
    assert precheck.weekday_hours(at(2026, 10, 30, 21, 15), at(2026, 11, 3, 0)) == 26.75  # Fri 2.75 h + Mon 24 h
