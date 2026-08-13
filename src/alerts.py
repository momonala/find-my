"""Evaluate configured alerts against freshly-written location fixes.

Called from src/poller.py right after src.db.record_fetch, inside the same
transaction, so evaluation never runs against a partially-written cycle.
Only devices that actually got a new `location_history` row this cycle are
checked -- for both alert types, nothing about the movement delta or the
distance to home changes without a new fix.

Delivery is in-app only for now (the dashboard reads `is_active`/`triggered_at`
off GET /alerts). A later Telegram integration would call out from the
`is_active` transitions below -- proximity alerts are edge-triggered
(entering/leaving the radius) rather than re-fired every cycle, precisely so
that hook only fires once per real event.
"""

import sqlite3
from datetime import UTC
from datetime import datetime

import src.db as db
from src.tracking import distance_from_home_m_at
from src.tracking import haversine_m


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
                if previous is None:
                    continue  # first-ever fix for this device: nothing to compare against
                moved_m = haversine_m(
                    previous["latitude"], previous["longitude"], current["latitude"], current["longitude"]
                )
                if moved_m > alert["threshold_m"]:
                    db.set_alert_state(
                        conn, alert["id"], is_active=bool(alert["is_active"]), triggered_at=now
                    )
            else:  # proximity
                distance_m = distance_from_home_m_at(current["latitude"], current["longitude"])
                inside = distance_m <= alert["threshold_m"]
                if inside and not alert["is_active"]:
                    db.set_alert_state(conn, alert["id"], is_active=True, triggered_at=now)
                elif not inside and alert["is_active"]:
                    db.set_alert_state(conn, alert["id"], is_active=False, triggered_at=alert["triggered_at"])
