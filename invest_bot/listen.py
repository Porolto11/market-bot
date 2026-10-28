"""The always-on Telegram listener (GitHub Actions, .github/workflows/listen.yml): the only process that writes the
bot state while it runs.

    python -m invest_bot.listen --max-minutes 345     (env TELEGRAM_TOKEN, TELEGRAM_CHAT_ID; in Actions also
                                                      GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_REF_NAME)

Until the time is up it long-polls getUpdates (50 s) from the state's offset and handles the commands with
commands.step (the same code as a one-off commands run: outbox, reset notice, no-scan warning), so replies arrive
within seconds. On weekdays at 06:50 and 21:15 UTC (cloud.SCANS) it runs the market scan (cloud.run) in-process;
a slot missed by up to 3 hours (the listener was not running) is caught up. The slot done is kept in the state
(scan_slot), so a restart never repeats it.

The state file is rewritten only when the state changed, and pushed to the `state` branch (Git below: a commit on
the last known base, force-with-lease) right away, except offset-only changes: those at most once a minute, and
always before exit (an unpushed offset would answer a command twice after a restart). Network and Telegram errors
back off from 5 s to 60 s; nothing stops the loop. A minute before the end it flushes the state and requests the
next run (workflow_dispatch of listen.yml), then exits 0. A run started by the fallback schedule exits at once when
another listener run is active or queued.

Logs are public in a public repo: they print counts, times and exception type names only, never tickers, amounts,
chat ids, message text or tokens.
"""
import argparse
import glob
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from invest_bot import cloud, commands, prices, state

POLL = 50  # seconds getUpdates waits for a message
BACKOFF = (5, 60)  # seconds after a failed poll: first, max (doubling)
OFFSET_PUSH = timedelta(seconds=60)  # offset-only changes are pushed at most this often
CATCH_UP = timedelta(hours=3)  # a slot missed by up to this much still runs
SCAN_ROOM = timedelta(minutes=15)  # no scan closer than this to the end: the next run catches it up
STOP = timedelta(seconds=60)  # the loop ends this long before the time is up
API = "https://api.github.com/repos/{repo}/actions/workflows/listen.yml/{what}"
ACTIVE = {"queued", "in_progress", "waiting", "pending", "requested"}


def log(text, out=None):
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {text}", file=out or sys.stdout, flush=True)


def due_slot(st, now):
    """The latest weekday scan slot (cloud.SCANS, UTC) at or before now, when it is at most CATCH_UP old and not
    done yet: neither marked in st['scan_slot'] nor covered by a scan at or after it (st['scan']['at']); else None."""
    for d in range(4):
        day = (now - timedelta(days=d)).date()
        for h, m in reversed(cloud.SCANS):
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=timezone.utc)
            if t > now or day.weekday() >= 5:
                continue
            if now - t > CATCH_UP or (st.get("scan_slot") or "") >= t.isoformat():
                return None
            try:
                if datetime.fromisoformat((st.get("scan") or {}).get("at") or "") >= t:
                    return None
            except ValueError:
                pass
            return t
    return None


class Conflict(RuntimeError):
    """The state branch moved since this run read it: pushing would overwrite a newer state."""


class Git:
    """Commits state/ on top of the last known state-branch commit (written by `.github/state.sh get` to
    $RUNNER_TEMP/state-base, "none" without a branch) and pushes it with force-with-lease on that commit, like
    `state.sh save`, but repeatable: a private index file, no worktree, fixed argv."""

    FILES = ("state.enc", "offset.txt", "outbox_pending", "last_scan.txt", "scan_warned.txt")
    WHO = dict(GIT_AUTHOR_NAME="github-actions[bot]",
               GIT_AUTHOR_EMAIL="41898299+github-actions[bot]@users.noreply.github.com")

    def __init__(self, folder="state", tmp=None, run=subprocess.run):
        self.folder, self.tmp, self.run = folder, tmp or os.environ["RUNNER_TEMP"], run
        with open(os.path.join(self.tmp, "state-base")) as f:
            self.base = f.read().strip() or "none"

    def git(self, *args, env=None) -> str:
        return self.run(["git", *args], check=True, capture_output=True, text=True, env=env).stdout.strip()

    def remote(self) -> str:
        line = self.git("ls-remote", "origin", "refs/heads/state")
        return line.split()[0] if line else "none"

    def save(self, message="state from listener") -> bool:
        """Commit and push the state files; False when nothing changed. Conflict when the branch moved."""
        index = os.path.join(self.tmp, "listen-index")
        if os.path.exists(index):
            os.remove(index)
        env = dict(os.environ, GIT_INDEX_FILE=index, GIT_COMMITTER_NAME=self.WHO["GIT_AUTHOR_NAME"],
                   GIT_COMMITTER_EMAIL=self.WHO["GIT_AUTHOR_EMAIL"], **self.WHO)
        new = self.base == "none"
        self.git("read-tree", *(["--empty"] if new else [self.base]), env=env)
        self.git("update-index", "--force-remove", f"{self.folder}/last_calls.json", env=env)
        names = [*self.FILES, *(os.path.basename(p) for p in
                               sorted(glob.glob(os.path.join(self.folder, "state.enc.bad-*"))))]
        for name in names:
            path = os.path.join(self.folder, name)
            if os.path.isfile(path):
                blob = self.git("hash-object", "-w", path)
                self.git("update-index", "--add", "--cacheinfo", f"100644,{blob},{self.folder}/{name}", env=env)
        tree = self.git("write-tree", env=env)
        if not new and tree == self.git("rev-parse", f"{self.base}^{{tree}}"):
            return False
        commit = self.git("commit-tree", tree, *([] if new else ["-p", self.base]), "-m", message, env=env)
        lease = "refs/heads/state:" + ("" if new else self.base)
        try:
            self.git("push", "-q", f"--force-with-lease={lease}", "origin", f"{commit}:refs/heads/state")
        except subprocess.CalledProcessError:
            try:
                moved = self.remote() != self.base
            except subprocess.CalledProcessError:
                moved = False
            if moved:
                raise Conflict("the state branch changed since this run read it") from None
            raise
        self.base = commit
        with open(os.path.join(self.tmp, "state-base"), "w") as f:
            f.write(commit)
        return True


def _api(env, what, data=None):
    req = urllib.request.Request(API.format(repo=env["GITHUB_REPOSITORY"], what=what), data=data,
                                 method="POST" if data is not None else "GET",
                                 headers={"Authorization": f"Bearer {env['GITHUB_TOKEN']}",
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          **({"Content-Type": "application/json"} if data is not None else {})})
    return req


def dispatch(env, opener=urllib.request.urlopen, sleep_fn=time.sleep, out=None) -> bool:
    """Request the next listener run: POST .../workflows/listen.yml/dispatches {"ref": GITHUB_REF_NAME}, 3 tries.
    Logs the outcome without the token."""
    if not all(env.get(k) for k in ("GITHUB_REPOSITORY", "GITHUB_REF_NAME", "GITHUB_TOKEN")):
        log("not in GitHub Actions: no next run requested", out)
        return False
    body = json.dumps({"ref": env["GITHUB_REF_NAME"]}).encode()
    for attempt in range(3):
        try:
            with opener(_api(env, "dispatches", body), timeout=30):
                pass
            log("next listener run requested", out)
            return True
        except Exception as e:
            log(f"next run request failed ({type(e).__name__} {getattr(e, 'code', '') or ''})".rstrip(), out)
            if attempt < 2:
                sleep_fn(5)
    return False


def another_run(env, opener=urllib.request.urlopen, out=None) -> bool:
    """True when a listen.yml run other than this one (GITHUB_RUN_ID) is queued or in progress. API errors: False
    (the concurrency group still keeps runs apart)."""
    try:
        with opener(_api(env, "runs?per_page=30"), timeout=30) as r:
            runs = json.loads(r.read().decode("utf-8") or "{}").get("workflow_runs") or []
    except Exception as e:
        log(f"run list failed ({type(e).__name__}): continuing", out)
        return False
    own = str(env.get("GITHUB_RUN_ID") or "")
    return any(str(r.get("id")) != own and r.get("status") in ACTIVE for r in runs)


def _scan(path, force=False) -> int:
    return cloud.run(cloud.parse_args(["--state", path, *(["--force"] if force else [])]),
                     buttons_fn=cloud.send_buttons)


def _no_offset(dump) -> str:
    if not dump:
        return dump
    d = json.loads(dump)
    d.pop("offset", None)
    return state.dump(d)


def listen(path, token, chat, minutes, push_fn=None, now_fn=lambda: datetime.now(timezone.utc),
           sleep_fn=time.sleep, updates_fn=commands.get_updates, send_fn=cloud.send_telegram, scan_fn=_scan,
           download_fn=prices.download, info_fn=prices.info, out=None) -> int:
    """The loop (see the module doc). push_fn() pushes the saved state files (None: local only). Returns 0, or 1
    when the state could not be read or its last push failed."""
    start = now_fn()
    end = start + timedelta(minutes=minutes)
    try:
        st = state.load(path, token)
    except RuntimeError as e:
        log(f"state not loaded ({type(e).__name__}): stopping", out)
        return 1
    folder = os.path.dirname(path) or "."
    saved = pushed = "" if st.get("reset") else state.dump(st)  # after a reset, re-save under the current key
    last_push, stop, handled_total, scans = start, [False], 0, 0
    ok = False

    def save(force=False) -> bool:
        """Write the state when it changed; push per the rules above. False when a needed push failed."""
        nonlocal saved, pushed, last_push
        d = state.dump(st)
        if d != saved:
            state.save(st, path, token)
            saved = d
        if saved == pushed or push_fn is None:
            return True
        now = now_fn()
        if not force and _no_offset(saved) == _no_offset(pushed) and now - last_push < OFFSET_PUSH:
            return True
        try:
            push_fn()
        except Conflict:
            log("state push refused: the state branch moved; stopping", out)
            stop[0] = True
            return False
        except Exception as e:
            log(f"state push failed ({type(e).__name__}): retrying later", out)
            return False
        pushed, last_push = saved, now
        return True

    def scan(slot):
        """A slot scan, or slot None: an on-demand /scan (forced, so it is sent even with unchanged calls)."""
        nonlocal st, scans
        st.pop("scan_now", None)
        save()  # the scan reads the file
        try:
            code = scan_fn(path) if slot else scan_fn(path, force=True)
        except Exception as e:
            code = f"error {type(e).__name__}"
        try:
            st = state.load(path, token)
        except Exception as e:
            log(f"state reload after the scan failed ({type(e).__name__})", out)
        if slot:
            st["scan_slot"] = slot.isoformat()
        scans += 1
        log(f"scan {'for the ' + format(slot, '%H:%M') + ' UTC slot' if slot else 'on request (/scan)'} done ({code})",
            out)

    backoff = BACKOFF[0]
    log(f"listening for {minutes:g} min", out)
    try:
        while not stop[0] and (now := now_fn()) < end - STOP:
            try:
                slot = due_slot(st, now)
                if slot and end - now > SCAN_ROOM:
                    scan(slot)
                elif st.get("scan_now") and end - now > SCAN_ROOM:
                    scan(None)
                else:
                    timeout = max(0, min(POLL, int((end - STOP - now).total_seconds())))
                    handled, _ = commands.step(st, token, chat, now, folder,
                                               lambda offset: updates_fn(token, offset, timeout=timeout), send_fn,
                                               download_fn, info_fn, out or sys.stdout)
                    if handled:
                        handled_total += handled
                        log(f"{handled} command(s) handled", out)
                save()
                backoff = BACKOFF[0]
            except Exception as e:  # network, Telegram or a bug: only the type, never text that may hold user data
                log(f"poll failed ({type(e).__name__}): retrying in {backoff} s", out)
                try:
                    save()
                except Exception as e2:
                    log(f"state save failed ({type(e2).__name__})", out)
                sleep_fn(backoff)
                backoff = min(backoff * 2, BACKOFF[1])
    finally:
        try:  # one retry: an unpushed offset means duplicate replies after the restart
            ok = save(force=True) or save(force=True)
        except Exception as e:
            log(f"state save failed ({type(e).__name__})", out)
        log(f"stopping: {handled_total} command(s), {scans} scan(s); state {'flushed' if ok else 'NOT pushed'}", out)
    return 0 if ok else 1


def _term(*_):
    raise SystemExit(143)  # cancelled or timed out: the finally above still flushes the state


def main(argv=None, env=None) -> int:
    p = argparse.ArgumentParser(prog="python -m invest_bot.listen", description=__doc__.splitlines()[0])
    p.add_argument("--max-minutes", type=float, default=345, help="run time before handing over to the next run")
    p.add_argument("--state", default=state.PATH, help="the encrypted bot state")
    args = p.parse_args(argv)
    env = os.environ if env is None else env
    token, chat = (env.get("TELEGRAM_TOKEN") or "").strip(), (env.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        log("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set")
        return 1
    if env.get("GITHUB_EVENT_NAME") == "schedule" and env.get("GITHUB_TOKEN") and another_run(env):
        log("another listener run is active or queued: this fallback run exits")
        return 0
    push = None
    if env.get("RUNNER_TEMP") and os.path.exists(os.path.join(env["RUNNER_TEMP"], "state-base")):
        push = Git(os.path.dirname(args.state) or ".", env["RUNNER_TEMP"]).save
    signal.signal(signal.SIGTERM, _term)
    code = listen(args.state, token, chat, args.max_minutes, push)
    dispatch(env)
    return code


if __name__ == "__main__":
    sys.exit(main())
