"""Hourly timer that commits a database snapshot to git.

The long-running counterpart to src/git_tool.py, run as its own service (`uv run
scheduler`, see install/projects_test-find-my-backup.service). Separate from the
poller on purpose: backups shouldn't stop if a fetch cycle starts failing, and
vice versa.
"""

import logging
import time

import schedule

from src.git_tool import commit_db_if_changed

logger = logging.getLogger(__name__)

TICK_SECONDS = 1


def get_scheduled_jobs() -> list[str]:
    """The registered jobs, for logging what this process is actually doing."""
    return [repr(job) for job in schedule.get_jobs()]


def schedule_loop() -> None:
    """Register the hourly backup and run the timer until interrupted."""
    schedule.every().hour.at(":00").do(commit_db_if_changed)
    logger.info("Scheduled jobs: %s", ", ".join(get_scheduled_jobs()))
    while True:
        schedule.run_pending()
        time.sleep(TICK_SECONDS)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        schedule_loop()
    except KeyboardInterrupt:
        logger.info("Stopping backup scheduler.")


if __name__ == "__main__":
    main()
