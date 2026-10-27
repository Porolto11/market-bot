#!/usr/bin/env bash
# The bot state on the `state` branch.
#   state.sh get            check out state/ from the branch; record its commit (or "none") in $RUNNER_TEMP/state-base
#   state.sh save "message" commit state/ on top of that commit; the push fails if the branch moved meanwhile
# A failure to reach the branch stops the job: an empty state must never replace the real one.
set -eu
BASE_FILE="$RUNNER_TEMP/state-base"

case "$1" in
get)
  mkdir -p state
  rc=0
  git ls-remote --exit-code --heads origin state > /dev/null || rc=$?
  if [ "$rc" -eq 2 ]; then
    echo none > "$BASE_FILE"
    echo "no state branch yet"
    exit 0
  fi
  [ "$rc" -eq 0 ] || { echo "cannot read the state branch (git rc $rc): stopping"; exit 1; }
  git fetch --depth=1 origin refs/heads/state
  git rev-parse FETCH_HEAD > "$BASE_FILE"
  git checkout FETCH_HEAD -- state 2>/dev/null || echo "state branch has no state/ folder yet"
  if [ -f state/state.enc ]; then echo "STATE_EXPECTED=1" >> "$GITHUB_ENV"; fi
  ;;
save)
  [ -f state/state.enc ] || { echo "no state to save"; exit 0; }
  base=$(cat "$BASE_FILE")
  git config user.name "github-actions[bot]"
  git config user.email "41898299+github-actions[bot]@users.noreply.github.com"
  WT="$RUNNER_TEMP/state-wt"
  if [ "$base" = none ]; then
    git worktree add --detach "$WT" HEAD
    git -C "$WT" checkout -q --orphan state-new
    git -C "$WT" rm -rfq --ignore-unmatch .
    lease="refs/heads/state:"  # empty: the branch must still not exist
  else
    git worktree add --detach "$WT" "$base"
    lease="refs/heads/state:$base"
  fi
  mkdir -p "$WT/state"
  for f in state/state.enc state/offset.txt state/outbox_pending state/last_scan.txt state/scan_warned.txt \
           state/state.enc.bad-*; do
    if [ -f "$f" ]; then cp "$f" "$WT/state/"; fi
  done
  cd "$WT"
  git rm -q --ignore-unmatch state/last_calls.json  # replaced by the encrypted state
  git add state
  if git diff --cached --quiet; then echo "unchanged"; exit 0; fi
  git commit -qm "$2"
  git push --force-with-lease="$lease" origin HEAD:refs/heads/state \
    || { echo "the state branch changed since this run read it: not overwriting"; exit 1; }
  ;;
*)
  echo "usage: state.sh get | save MESSAGE"; exit 2
  ;;
esac
