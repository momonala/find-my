"""Background fetch loop for the Flask API.

Runs `fetch_devices()` and `fetch_airtags()` once a minute on a daemon thread
and records the results via `src.db.record_fetch`, so HTTP requests never wait
on a live Apple round trip -- they just read whatever's already in SQLite.

This reuses whatever session and tracker keys are already cached in
`.icloud_session/`. If that cache doesn't exist yet, run `uv run findmy
airtags` (or `devices`) once at the console first -- first-time 2FA and the
Keychain prompt can't be satisfied from a background thread.
"""

import logging
import threading

from src.airtags import fetch_airtags
from src.alerts import check_alerts
from src.db import connection
from src.db import record_fetch
from src.find_my import fetch_devices

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60
MAX_BACKOFF_SECONDS = 15 * 60
# Past this many consecutive failures the cause is no longer a passing network
# blip (an expired session, revoked credentials), so it's logged as an error.
_PERSISTENT_FAILURE_THRESHOLD = 3


def _poll_once() -> None:
    items = fetch_devices() + fetch_airtags()
    with connection() as conn:
        result = record_fetch(conn, items)
        # Same connection/transaction as record_fetch, so a crash between the
        # two can't leave history written but that cycle's alerts unevaluated.
        check_alerts(conn, result.moved_device_ids)
    items_written, items_fetched = result.counts.get("item", (0, 0))
    devices_written, devices_fetched = result.counts.get("device", (0, 0))
    logger.info(
        "[%d/%d] items  [%d/%d] devices",
        items_written,
        items_fetched,
        devices_written,
        devices_fetched,
    )


def _backoff_seconds(consecutive_failures: int) -> int:
    """Delay before the next cycle: the normal interval, doubling while failing."""
    if consecutive_failures == 0:
        return POLL_INTERVAL_SECONDS
    doubled: int = POLL_INTERVAL_SECONDS * 2**consecutive_failures
    return min(doubled, MAX_BACKOFF_SECONDS)


def run_forever(stop_event: threading.Event) -> None:
    """Poll immediately, then on an interval until `stop_event` is set.

    Failures back off exponentially up to `MAX_BACKOFF_SECONDS` rather than
    retrying at full rate: an expired iCloud session fails identically every
    time, and hammering Apple once a minute forever helps nobody.
    """
    consecutive_failures = 0
    while not stop_event.is_set():
        try:
            _poll_once()
            consecutive_failures = 0
        except Exception:
            # A network blip or expired session shouldn't kill the poller or the
            # API -- just keep serving the last-known data and try again later.
            consecutive_failures += 1
            if consecutive_failures >= _PERSISTENT_FAILURE_THRESHOLD:
                logger.exception(
                    "Poll cycle failed %d times in a row; the session may need re-authenticating "
                    "at the console",
                    consecutive_failures,
                )
            else:
                logger.warning("Poll cycle failed; keeping last known data", exc_info=True)
        stop_event.wait(_backoff_seconds(consecutive_failures))


def start_background_poller() -> threading.Event:
    """Start the poller on a daemon thread and return its stop event."""
    stop_event = threading.Event()
    threading.Thread(target=run_forever, args=(stop_event,), daemon=True).start()
    return stop_event
