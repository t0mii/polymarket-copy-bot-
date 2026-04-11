"""
Auto-Backup — pushed alles auf GitHub (piff remote) alle 6h.
Committed nur wenn es Aenderungen gibt.
"""
import logging
import subprocess
import os

logger = logging.getLogger(__name__)

REPO_DIR = '/root/polymarket-copy-bot'
REMOTE = 'piff'
BRANCH = 'piff-custom'


def run_backup():
    """Git add, commit, push auf piff remote."""
    # Check ob es Aenderungen gibt
    result = subprocess.run(['git', 'status', '--porcelain'],
                          capture_output=True, text=True, cwd=REPO_DIR)
    changes = result.stdout.strip()

    if not changes:
        logger.info("[BACKUP] No changes to backup")
        return

    # Add all changes (except secrets)
    subprocess.run(['git', 'add', '-A'], cwd=REPO_DIR)

    # Dont add secrets
    for secret in ['secrets.env', '.env']:
        subprocess.run(['git', 'reset', 'HEAD', secret],
                      capture_output=True, cwd=REPO_DIR)

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
