"""Evaluate configured alerts against freshly-written location fixes.

Called from src/poller.py right after src.db.record_fetch, inside the same
transaction. Only devices that got a new `location_history` row this cycle are
checked -- for every alert type, nothing about the movement delta or the
distance to the anchor changes without a new fix.

Three alert types: `movement` fires when consecutive fixes are more than
`threshold_m` apart; `enter` and `exit` fire when the device crosses into or
out of `threshold_m` of its anchor point -- `(anchor_lat, anchor_lon)` if set,
else the configured home coordinates.

`enter`/`exit` are edge-triggered off `alerts.is_active`, which always means
"the device is currently inside this alert's radius" regardless of type. Both
types track it so each can detect its own transition; the opposite transition
updates `is_active` silently so the next real crossing is still detected.

All three types are gated by ALERT_COOLDOWN_S. A suppressed `enter`/`exit`
transition leaves `is_active` untouched and so is retried on the next cycle,
landing late rather than being lost.

Delivery is in-app (the dashboard reads `is_active`/`triggered_at` off
GET /alerts) plus an optional Telegram push from src/telegram.py.
"""

import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC
from datetime import datetime

import requests

import src.db as db
from src.config import HOME_LATITUDE
from src.config import HOME_LONGITUDE
from src.telegram import send_enter_alert
from src.telegram import send_exit_alert
from src.telegram import send_movement_alert
from src.telemetry import metrics
from src.tracking import haversine_m

logger = logging.getLogger(__name__)

# Minimum time between two notifications for the *same* alert, regardless of
# type -- keeps a device flapping across a radius boundary, or drifting back
# and forth past a movement threshold, from notifying every poll cycle.
ALERT_COOLDOWN_S = 300


def check_alerts(conn: sqlite3.Connection, moved_device_ids: set[str]) -> None:
    """Re-evaluate every alert belonging to a device that just moved."""
    now = datetime.now(UTC).isoformat()

    for device_id in moved_device_ids:
        device_alerts = db.alerts_for_device(conn, device_id)
        if not device_alerts:
            continue

        fixes = db.history_for(conn, device_id, limit=2)  # newest first
        if not fixes:
            continue
        current = fixes[0]
        previous = fixes[1] if len(fixes) > 1 else None

        for alert in device_alerts:
            if alert["alert_type"] == "movement":
                _check_movement(conn, alert, previous, current, now)
            else:
                _check_radius_crossing(conn, alert, current, now)


def _check_movement(
    conn: sqlite3.Connection, alert: sqlite3.Row, previous: sqlite3.Row | None, current: sqlite3.Row, now: str
) -> None:
    if previous is None:
        return  # first-ever fix for this device: nothing to compare against
    moved_m = haversine_m(
        previous["latitude"], previous["longitude"], current["latitude"], current["longitude"]
    )
    if moved_m > alert["threshold_m"] and _cooldown_elapsed(alert, now):
        db.log_alert_event(conn, alert["id"], now)
        metrics.increment("movement_triggered")
        _notify(alert["id"], lambda: send_movement_alert(alert, moved_m))


def _check_radius_crossing(
    conn: sqlite3.Connection, alert: sqlite3.Row, current: sqlite3.Row, now: str
) -> None:
    is_enter = alert["alert_type"] == "enter"
    anchor_lat = alert["anchor_lat"] if alert["anchor_lat"] is not None else HOME_LATITUDE
    anchor_lon = alert["anchor_lon"] if alert["anchor_lon"] is not None else HOME_LONGITUDE
    distance_m = haversine_m(anchor_lat, anchor_lon, current["latitude"], current["longitude"])
    inside = distance_m <= alert["threshold_m"]
    was_inside = bool(alert["is_active"])

    if inside == was_inside:
        return

    notifies = inside if is_enter else not inside
    if not notifies:
        # The opposite transition for this alert's type: bookkeeping only, so
        # it's silent, not cooldown-gated, and writes no alert_events row.
        db.set_alert_active(conn, alert["id"], is_active=inside)
        return

    if _cooldown_elapsed(alert, now):
        db.set_alert_active(conn, alert["id"], is_active=inside)
        db.log_alert_event(conn, alert["id"], now)
        metrics.increment(f"{alert['alert_type']}_triggered")
        send = send_enter_alert if is_enter else send_exit_alert
        _notify(alert["id"], lambda: send(alert))


def _cooldown_elapsed(alert: sqlite3.Row, now: str) -> bool:
    if alert["triggered_at"] is None:
        return True
    elapsed = datetime.fromisoformat(now) - datetime.fromisoformat(alert["triggered_at"])
    return elapsed.total_seconds() >= ALERT_COOLDOWN_S


def _notify(alert_id: int, send: Callable[[], None]) -> None:
    """Best-effort Telegram push -- a down or misconfigured bot must not stop the
    rest of this cycle's alerts from being evaluated and recorded in-app."""
    try:
        send()
    except requests.RequestException:
        metrics.increment("telegram_failed")
        logger.warning("Telegram notification failed for alert %s", alert_id, exc_info=True)
