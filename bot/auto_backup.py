"""
Auto-Backup — pushed alles auf GitHub (piff remote) alle 6h.
Committed nur wenn es Aenderungen gibt.
"""
import logging
import subprocess
import os

logger = logging.getLogger(__name__)

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REMOTE = 'piff'
BRANCH = 'piff-custom'


def _remote_exists() -> bool:
    """Return True iff the configured remote exists on this machine."""
    r = subprocess.run(['git', 'remote'], capture_output=True, text=True, cwd=REPO_DIR)
    return REMOTE in r.stdout.split()


def _local_branch_exists() -> bool:
    """Return True iff the configured branch exists locally."""
    r = subprocess.run(
        ['git', 'rev-parse', '--verify', '--quiet', f'refs/heads/{BRANCH}'],
        capture_output=True, cwd=REPO_DIR,
    )
    return r.returncode == 0


def run_backup():
    """Git add, commit, push auf piff remote."""
    if not _remote_exists() or not _local_branch_exists():
        logger.debug("[BACKUP] remote=%s or branch=%s not configured on this host — skipping",
                     REMOTE, BRANCH)
        return

    # Check ob es Aenderungen gibt
    result = subprocess.run(['git', 'status', '--porcelain'],
                          capture_output=True, text=True, cwd=REPO_DIR)
    changes = result.stdout.strip()

    if not changes:
        logger.info("[BACKUP] No changes to backup")
        return

    # Add all changes (except secrets)
    subprocess.run(['git', 'add', '-A'], cwd=REPO_DIR)

    # Dont add secrets — verify they are unstaged
    for secret in ['secrets.env', '.env']:
        r = subprocess.run(['git', 'reset', 'HEAD', secret],
                          capture_output=True, cwd=REPO_DIR)
        if r.returncode != 0:
            logger.warning('[BACKUP] Failed to unstage %s — aborting commit!', secret)
            subprocess.run(['git', 'reset', 'HEAD'], cwd=REPO_DIR)
            return

    # Commit
    from datetime import datetime
    msg = "auto-backup %s" % datetime.now().strftime("%Y-%m-%d %H:%M")
    result = subprocess.run(
        ['git', 'commit', '-m', msg],
        capture_output=True, text=True, cwd=REPO_DIR
    )

    if result.returncode != 0:
        if 'nothing to commit' in result.stdout:
            logger.info("[BACKUP] Nothing to commit")
            return
        logger.warning("[BACKUP] Commit failed: %s", result.stderr)
        return

    # Push to piff remote
    result = subprocess.run(
        ['git', 'push', REMOTE, BRANCH],
        capture_output=True, text=True, cwd=REPO_DIR
    )

    if result.returncode == 0:
        logger.info("[BACKUP] Pushed to %s/%s", REMOTE, BRANCH)
    else:
        logger.warning("[BACKUP] Push failed: %s", result.stderr)
