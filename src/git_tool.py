"""Commit a snapshot of the location database to git, for off-box backup.

Driven by src/data_backup_scheduler.py on an hourly timer. The live sqlite file
is never committed directly -- it's copied to a `.bk` sibling first, so git
never races the poller mid-write.

Consecutive backup commits are amended into one rather than piling up: the
commit subject carries the time range it covers (`[DB-AUTO-BACKUP] <start>-<end>`),
so a run that follows another backup commit extends that range in place.
"""

import logging
import re
import shutil
import subprocess
from datetime import UTC
from datetime import datetime
from pathlib import Path

from src.db import DB_PATH

logger = logging.getLogger(__name__)

BRANCH = "main"
COMMIT_PREFIX = "[DB-AUTO-BACKUP]"
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%S"
RANGE_RE = re.compile(
    r"(?P<start>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})-(?P<end>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
)

# The committed copy, alongside the live DB that src/db.py owns.
BACKUP_PATH = DB_PATH.with_suffix(DB_PATH.suffix + ".bk")


def run_command(cmd: list[str]) -> str:
    """Run `cmd` and return its stdout, raising CalledProcessError on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("Command %s failed: %s", cmd, result.stderr.strip())
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result.stdout.strip()


def format_datetime(value: datetime) -> str:
    return value.strftime(DATETIME_FORMAT)


def parse_start_from_commit(message: str) -> str | None:
    """Return the start timestamp if the commit matches the auto-backup pattern."""
    match = RANGE_RE.search(message)
    if match:
        return match.group("start")
    return None


def commit_db_if_changed(
    database_path: Path | None = None,
    backup_path: Path | None = None,
) -> None:
    """Snapshot the database and commit it, if its contents changed since last time.

    Paths default to the live DB and its `.bk` sibling; they're parameters so
    tests don't have to touch the real database.
    """
    source = database_path or DB_PATH
    destination = backup_path or BACKUP_PATH

    if not source.exists():
        logger.warning("No database at %s; nothing to back up.", source)
        return

    shutil.copy2(source, destination)
    logger.info("Copied %s to %s", source, destination)

    diff = run_command(["git", "diff", str(destination)])
    if not diff:
        logger.info("No changes. Skipping commit.")
        return

    run_command(["git", "add", str(destination)])
    # UTC, not local time: these timestamps are parsed back out of commit
    # subjects that may have been written on a differently-configured host.
    now_str = format_datetime(datetime.now(UTC))
    start_time = now_str
    should_amend = False
    last_commit_msg = ""

    try:
        last_commit_msg = run_command(["git", "log", "-1", "--pretty=%s"])
    except subprocess.CalledProcessError:
        logger.info("Unable to read last commit; creating a new backup commit.")

    if last_commit_msg.startswith(COMMIT_PREFIX):
        possible_start = parse_start_from_commit(last_commit_msg)
        if possible_start:
            start_time = possible_start
            should_amend = True

    commit_message = f"{COMMIT_PREFIX} {start_time}-{now_str}"
    if should_amend:
        run_command(["git", "commit", "--amend", "-m", commit_message])
        push_args = ["git", "push", "--force", "origin", BRANCH]
        log_action = "Changes amended to auto-backup commit with bounds %s-%s."
    else:
        run_command(["git", "commit", "-m", commit_message])
        push_args = ["git", "push", "origin", BRANCH]
        log_action = "New auto-backup commit created with bounds %s-%s."

    run_command(push_args)
    logger.info(log_action, start_time, now_str)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    commit_db_if_changed()
