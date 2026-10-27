"""Cheap pre-check for the commands workflow: stdlib only, so it runs before setup-python and pip install.

    python3 -m invest_bot.precheck >> "$GITHUB_OUTPUT"      (prints work=1 or work=0)

work=1 when a reply or the reset notice is waiting (state/outbox_pending), when the market scan is overdue
(state/last_scan.txt more than 26 weekday hours old on a weekday, not yet warned about today: state/scan_warned.txt),
when Telegram has any update at or after state/offset.txt, or when the check itself fails (the full run then
decides). The token is never printed.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta, timezone

URL = "https://api.telegram.org/bot{token}/getUpdates?{query}"
SCAN_HOURS = 26  # weekday hours without a scan before the warning


def _read(folder, name, default):
    try:
        with open(os.path.join(folder, name)) as f:
            return f.read().strip() or default
    except OSError:
        return default


def weekday_hours(start, end) -> float:
    """Hours from start to end, leaving out Saturdays and Sundays (UTC)."""
    total, t = 0.0, start.astimezone(timezone.utc)
    while t < end:
        nxt = min(end, datetime.combine(t.date() + timedelta(days=1), time(), timezone.utc))
        if t.weekday() < 5:
            total += (nxt - t).total_seconds() / 3600
        t = nxt
    return total


def overdue(folder, now):
    """The last scan time (datetime) when it is a weekday, the scan in last_scan.txt is more than SCAN_HOURS weekday
    hours old and today was not warned about yet (scan_warned.txt); else None (also without any scan yet)."""
    try:
        last = datetime.fromisoformat(_read(folder, "last_scan.txt", ""))
    except ValueError:
        return None
    if now.weekday() >= 5 or _read(folder, "scan_warned.txt", "") == now.date().isoformat():
        return None
    return last if weekday_hours(last, now) > SCAN_HOURS else None


def needed(folder, token, opener=None, now=None) -> bool:
    if not token or _read(folder, "outbox_pending", "0") == "1":
        return True
    if overdue(folder, now or datetime.now(timezone.utc)):
        return True
    query = urllib.parse.urlencode(dict(offset=_read(folder, "offset.txt", "0"), limit=1, timeout=0,
                                        allowed_updates='["message"]'))
    try:
        with (opener or urllib.request.urlopen)(URL.format(token=token, query=query), timeout=20) as r:
            body = json.loads(r.read().decode("utf-8") or "{}")
    except Exception:
        return True
    return not body.get("ok") or bool(body.get("result"))


def main() -> int:
    work = needed("state", (os.environ.get("TELEGRAM_TOKEN") or "").strip())
    print("new messages or replies waiting" if work else "nothing to do", file=sys.stderr)
    print(f"work={int(work)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
