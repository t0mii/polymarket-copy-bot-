#!/bin/bash
# Auto-update copybot from GitHub. Runs every 15 min via cron.
# - Force-fetches origin/main (defends against any stale-ref glitches).
# - Only updates if origin/main is strictly ahead of local HEAD.
# - Stashes local working-tree edits, fast-forwards, restores them.
# - Restarts copybot only on a successful pull.
set -u
cd /root/polymarket-copy-bot || exit 1

LOG=/var/log/copybot-autoupdate.log

# Force fetch — ignores any stale local refs.
git fetch --force --prune --quiet origin main || {
  echo "[$(date -Is)] fetch failed" >> "$LOG"
  exit 1
}

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)

# No change → silent exit.
[ "$LOCAL" = "$REMOTE" ] && exit 0

# Only act if origin/main is strictly ahead (LOCAL is ancestor of REMOTE,
# REMOTE is not ancestor of LOCAL). Anything else means local has its own
# commits — do not touch, log it so a human can resolve.
if git merge-base --is-ancestor "$LOCAL" "$REMOTE" && ! git merge-base --is-ancestor "$REMOTE" "$LOCAL"; then
  echo "[$(date -Is)] Update: $LOCAL -> $REMOTE" >> "$LOG"

  STASH_MSG="auto-update-$(date +%s)"
  STASHED=0
  if ! git diff --quiet || ! git diff --cached --quiet; then
    if git stash push --quiet -m "$STASH_MSG" 2>>"$LOG"; then
      STASHED=1
    fi
  fi

  if git pull --ff-only --quiet >>"$LOG" 2>&1; then
    if [ "$STASHED" = "1" ]; then
      git stash pop --quiet 2>>"$LOG" || echo "[$(date -Is)] stash pop had conflicts — manual fix needed" >> "$LOG"
    fi
    systemctl restart copybot
    echo "[$(date -Is)] Restarted to $REMOTE" >> "$LOG"
  else
    echo "[$(date -Is)] pull --ff-only failed, restoring stash" >> "$LOG"
    [ "$STASHED" = "1" ] && git stash pop --quiet 2>>"$LOG"
  fi
else
  echo "[$(date -Is)] Local has diverged from origin/main (LOCAL=$LOCAL REMOTE=$REMOTE) — manual rebase needed" >> "$LOG"
fi
