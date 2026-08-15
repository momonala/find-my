"""Tests for src/alerts.py's check_alerts().

Exercised directly against a seeded connection, mirroring tests/test_db.py's
style -- check_alerts is called from the poller with whatever moved_device_ids
record_fetch reports, not from a request.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import requests

from src.alerts import ALERT_COOLDOWN_S
from src.alerts import check_alerts
from src.config import HOME_LATITUDE
from src.config import HOME_LONGITUDE
from src.db import alerts_for_device
from src.db import create_alert
from src.db import log_alert_event
from src.db import record_fetch
from tests.conftest import make_item
from tests.conftest import make_location
from tests.conftest import minutes_later

COOLDOWN_LATER = timedelta(seconds=ALERT_COOLDOWN_S + 1)


def test_check_alerts_is_a_no_op_for_a_device_with_no_alerts(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    result = record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])

    check_alerts(conn, result.moved_device_ids)  # must not raise


def test_movement_alert_fires_when_the_jump_exceeds_the_threshold(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    alert_id = create_alert(conn, "tag-1", "movement", 100)

    # ~11km jump between fixes, well over the 100m threshold.
    result = record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])
    check_alerts(conn, result.moved_device_ids)

    alert = alerts_for_device(conn, "tag-1")[0]
    assert alert["id"] == alert_id
    assert alert["triggered_at"] is not None


def test_movement_alert_does_not_fire_for_a_move_under_the_threshold(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    create_alert(conn, "tag-1", "movement", 100)

    # ~1.1m move -- under the 100m threshold.
    result = record_fetch(conn, [make_item("tag-1", make_location(52.50001, 13.4, minutes_later(1)))])
    check_alerts(conn, result.moved_device_ids)

    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] is None


def test_movement_alert_skipped_on_a_devices_first_ever_fix(conn):
    record_fetch(conn, [make_item("tag-1")])  # device known, no fix yet
    create_alert(conn, "tag-1", "movement", 100)

    # First-ever fix: nothing to compare against, so this must not crash or fire.
    result = record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    check_alerts(conn, result.moved_device_ids)

    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] is None


def test_movement_alert_does_not_refire_within_the_cooldown(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    create_alert(conn, "tag-1", "movement", 100)

    first = record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])
    check_alerts(conn, first.moved_device_ids)
    first_triggered_at = alerts_for_device(conn, "tag-1")[0]["triggered_at"]

    # Another big jump a second later -- well inside the cooldown window.
    second = record_fetch(
        conn, [make_item("tag-1", make_location(52.5, 13.4, minutes_later(1) + timedelta(seconds=1)))]
    )
    check_alerts(conn, second.moved_device_ids)

    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] == first_triggered_at


def test_movement_alert_refires_once_the_cooldown_elapses(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    alert_id = create_alert(conn, "tag-1", "movement", 100)

    first = record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])
    check_alerts(conn, first.moved_device_ids)
    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] is not None

    # check_alerts gates the cooldown on wall-clock time, not the fix's own
    # timestamp, so simulate an elapsed cooldown by backdating directly.
    backdated = (datetime.now(UTC) - COOLDOWN_LATER).isoformat()
    log_alert_event(conn, alert_id, backdated)

    second = record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4, minutes_later(2)))])
    check_alerts(conn, second.moved_device_ids)

    second_triggered_at = alerts_for_device(conn, "tag-1")[0]["triggered_at"]
    assert second_triggered_at is not None
    assert second_triggered_at != backdated


def test_enter_alert_activates_on_entering_the_radius(conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])  # far away
    alert_id = create_alert(conn, "tag-1", "enter", 100)

    result = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, result.moved_device_ids)

    alert = alerts_for_device(conn, "tag-1")[0]
    assert alert["id"] == alert_id
    assert alert["is_active"] == 1
    assert alert["triggered_at"] is not None


def test_enter_alert_measures_from_a_custom_anchor_not_home(conn):
    anchor_lat, anchor_lon = HOME_LATITUDE + 2, HOME_LONGITUDE
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE))])
    create_alert(conn, "tag-1", "enter", 100, anchor_lat=anchor_lat, anchor_lon=anchor_lon)

    # Moves closer to home -- irrelevant, since this alert isn't anchored there.
    near_home = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 0.001, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, near_home.moved_device_ids)
    assert alerts_for_device(conn, "tag-1")[0]["is_active"] == 0

    # Moves to within the anchor's own radius.
    at_anchor = record_fetch(
        conn, [make_item("tag-1", make_location(anchor_lat, anchor_lon, minutes_later(2)))]
    )
    check_alerts(conn, at_anchor.moved_device_ids)
    assert alerts_for_device(conn, "tag-1")[0]["is_active"] == 1


def test_enter_alert_does_not_activate_outside_the_radius(conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])
    create_alert(conn, "tag-1", "enter", 100)

    result = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 0.9, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, result.moved_device_ids)

    alert = alerts_for_device(conn, "tag-1")[0]
    assert alert["is_active"] == 0
    assert alert["triggered_at"] is None


def test_enter_alert_does_not_notify_on_leaving_but_resets_state_silently(conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])  # far away
    create_alert(conn, "tag-1", "enter", 100)

    enter = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, enter.moved_device_ids)
    triggered_at = alerts_for_device(conn, "tag-1")[0]["triggered_at"]
    assert triggered_at is not None

    leave = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE, minutes_later(2)))]
    )
    check_alerts(conn, leave.moved_device_ids)

    alert = alerts_for_device(conn, "tag-1")[0]
    assert alert["is_active"] == 0
    assert alert["triggered_at"] == triggered_at  # unchanged: leaving isn't a notified event for "enter"


def test_exit_alert_fires_on_leaving_after_silently_tracking_the_entry(conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])  # far away
    create_alert(conn, "tag-1", "exit", 100)

    # Entering isn't a notified event for "exit", but is_active must still
    # get set to "currently inside" so the later exit is correctly detected.
    enter = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, enter.moved_device_ids)
    assert alerts_for_device(conn, "tag-1")[0]["is_active"] == 1

    leave = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE, minutes_later(2)))]
    )
    check_alerts(conn, leave.moved_device_ids)

    alert = alerts_for_device(conn, "tag-1")[0]
    assert alert["is_active"] == 0
    assert alert["triggered_at"] is not None


def test_exit_alert_does_not_notify_on_entering(conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])  # far away
    create_alert(conn, "tag-1", "exit", 100)

    result = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, result.moved_device_ids)

    alert = alerts_for_device(conn, "tag-1")[0]
    assert alert["is_active"] == 1  # now known to be inside
    assert alert["triggered_at"] is None  # but never notified


def test_enter_alert_does_not_refire_within_the_cooldown_across_a_bounce(conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])
    create_alert(conn, "tag-1", "enter", 100)

    enter = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, enter.moved_device_ids)
    first_triggered_at = alerts_for_device(conn, "tag-1")[0]["triggered_at"]
    assert first_triggered_at is not None

    leave = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE, minutes_later(2)))]
    )
    check_alerts(conn, leave.moved_device_ids)

    # Re-enters well within the cooldown window of the first notification.
    reenter = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE, minutes_later(3)))]
    )
    check_alerts(conn, reenter.moved_device_ids)

    alert = alerts_for_device(conn, "tag-1")[0]
    assert alert["triggered_at"] == first_triggered_at
    # Not marked active yet either -- the transition is retried, not lost,
    # so it will still fire (and correctly mark active) once the cooldown clears.
    assert alert["is_active"] == 0


@patch("src.alerts.send_movement_alert")
def test_movement_alert_notifies_telegram_when_it_fires(mock_send, conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    create_alert(conn, "tag-1", "movement", 100)

    result = record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])
    check_alerts(conn, result.moved_device_ids)

    mock_send.assert_called_once()
    assert mock_send.call_args.args[0]["device_id"] == "tag-1"


@patch("src.alerts.send_movement_alert")
def test_movement_alert_does_not_notify_when_it_does_not_fire(mock_send, conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    create_alert(conn, "tag-1", "movement", 100)

    result = record_fetch(conn, [make_item("tag-1", make_location(52.50001, 13.4, minutes_later(1)))])
    check_alerts(conn, result.moved_device_ids)

    mock_send.assert_not_called()


@patch("src.alerts.send_movement_alert", side_effect=requests.RequestException("down"))
def test_a_failed_telegram_send_does_not_stop_the_rest_of_the_cycle(mock_send, conn):
    """One alert's notification failing must not prevent other alerts (or
    other devices) from being evaluated and recorded in-app this cycle."""
    record_fetch(
        conn, [make_item("tag-1", make_location(52.5, 13.4)), make_item("tag-2", make_location(52.5, 13.4))]
    )
    create_alert(conn, "tag-1", "movement", 100)
    create_alert(conn, "tag-2", "movement", 100)

    result = record_fetch(
        conn,
        [
            make_item("tag-1", make_location(52.6, 13.4, minutes_later(1))),
            make_item("tag-2", make_location(52.6, 13.4, minutes_later(1))),
        ],
    )
    check_alerts(conn, result.moved_device_ids)  # must not raise

    assert alerts_for_device(conn, "tag-1")[0]["triggered_at"] is not None
    assert alerts_for_device(conn, "tag-2")[0]["triggered_at"] is not None


@patch("src.alerts.send_enter_alert")
def test_enter_alert_notifies_telegram_on_entering_only(mock_send, conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])
    create_alert(conn, "tag-1", "enter", 100)

    enter = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, enter.moved_device_ids)
    assert mock_send.call_count == 1

    leave = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE, minutes_later(2)))]
    )
    check_alerts(conn, leave.moved_device_ids)
    assert mock_send.call_count == 1  # still just the one call, from entering


@patch("src.alerts.send_exit_alert")
def test_exit_alert_notifies_telegram_on_leaving_only(mock_send, conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])  # far away
    create_alert(conn, "tag-1", "exit", 100)

    enter = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, enter.moved_device_ids)
    assert mock_send.call_count == 0  # entering isn't notified for "exit"

    leave = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE, minutes_later(2)))]
    )
    check_alerts(conn, leave.moved_device_ids)
    assert mock_send.call_count == 1
