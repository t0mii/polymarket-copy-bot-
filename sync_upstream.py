"""
Sync local bot with upstream developer repo.
Pulls latest code from GitHub, preserves local config (.env, database, logs)
and re-applies local patches (auto-redeem, relayer headers).

Runs automatically every 10 minutes via main.py scheduler.
Can also be run manually: python sync_upstream.py
"""
import logging
import os
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("sync")

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
PATCHES_DIR = os.path.join(BOT_DIR, ".local-patches")
REMOTE = "origin"
BRANCH = "main"


def run_git(*args):
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", "-C", BOT_DIR] + list(args),
        capture_output=True, text=True, timeout=30,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def get_local_head():
    rc, out, _ = run_git("rev-parse", "HEAD")
    return out if rc == 0 else None


def get_remote_head():
    rc, out, _ = run_git("rev-parse", f"{REMOTE}/{BRANCH}")
    return out if rc == 0 else None


def sync():
    """Pull upstream changes and re-apply local patches."""
    # Fetch latest from upstream
    rc, _, err = run_git("fetch", REMOTE, BRANCH)
    if rc != 0:
        logger.error("git fetch failed: %s", err)
        return False

    local_head = get_local_head()
    remote_head = get_remote_head()

    if local_head == remote_head:
        logger.info("[SYNC] Already up to date.")
        return False

    # Check what changed upstream
    rc, diff_files, _ = run_git("diff", "--name-only", f"HEAD..{REMOTE}/{BRANCH}")
    if not diff_files:
        logger.info("[SYNC] No file changes detected.")
        return False

    changed = diff_files.split("\n")
    logger.info("[SYNC] Upstream has %d changed files: %s", len(changed), ", ".join(changed))

    # Stash local changes (our patches)
    rc, _, err = run_git("stash", "push", "-m", "pre-sync-backup")
    if rc != 0:
        logger.error("[SYNC] Stash failed — aborting: %s", err)
        return False
    if rc != 0:
        logger.error("[SYNC] Stash failed — aborting sync: %s", err)
        return False

    # Reset to upstream (keeps .env, database, logs because they're untracked/gitignored)
    rc, _, err = run_git("reset", "--hard", f"{REMOTE}/{BRANCH}")
    if rc != 0:
        logger.error("git reset failed: %s — restoring stash", err)
        run_git("stash", "pop")
        return False

    logger.info("[SYNC] Updated to upstream %s", remote_head[:12])

    # Re-apply local patches
    patches_applied = 0
    if os.path.isdir(PATCHES_DIR):
        for patch_file in sorted(os.listdir(PATCHES_DIR)):
            if not patch_file.endswith(".patch"):
                continue
            patch_path = os.path.join(PATCHES_DIR, patch_file)
            rc, out, err = run_git("apply", "--check", patch_path)
            if rc == 0:
                rc2, _, err2 = run_git("apply", patch_path)
                if rc2 == 0:
                    patches_applied += 1
                    logger.info("[SYNC] Patch applied: %s", patch_file)
                else:
                    logger.warning("[SYNC] Patch failed to apply: %s — %s", patch_file, err2)
            else:
                logger.warning("[SYNC] Patch conflict: %s — %s (may already be in upstream)", patch_file, err)

    if patches_applied:
        logger.info("[SYNC] %d local patches re-applied.", patches_applied)

    return True


if __name__ == "__main__":
    changed = sync()
    if changed:
        logger.info("Sync complete — bot code updated!")
    else:
        logger.info("No changes needed.")
