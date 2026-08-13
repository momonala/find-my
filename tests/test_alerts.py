"""Tests for src/alerts.py's check_alerts().

Exercised directly against a seeded connection, mirroring tests/test_db.py's
style -- check_alerts is called from the poller with whatever moved_device_ids
record_fetch reports, not from a request.
"""

from src.alerts import check_alerts
from src.config import HOME_LATITUDE
from src.config import HOME_LONGITUDE
from src.db import alerts_for_device
from src.db import create_alert
from src.db import record_fetch
from tests.conftest import make_item
from tests.conftest import make_location
from tests.conftest import minutes_later


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


def test_proximity_alert_activates_on_entering_the_radius(conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])  # far away
    alert_id = create_alert(conn, "tag-1", "proximity", 100)

    result = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, result.moved_device_ids)

    alert = alerts_for_device(conn, "tag-1")[0]
    assert alert["id"] == alert_id
    assert alert["is_active"] == 1
    assert alert["triggered_at"] is not None


def test_proximity_alert_does_not_activate_outside_the_radius(conn):
    record_fetch(conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))])
    create_alert(conn, "tag-1", "proximity", 100)

    result = record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 0.9, HOME_LONGITUDE, minutes_later(1)))]
    )
    check_alerts(conn, result.moved_device_ids)

    alert = alerts_for_device(conn, "tag-1")[0]
    assert alert["is_active"] == 0
    assert alert["triggered_at"] is None


def test_proximity_alert_deactivates_on_leaving_but_keeps_last_triggered_at(conn):
    record_fetch(
        conn, [make_item("tag-1", make_location(HOME_LATITUDE + 1, HOME_LONGITUDE))]
    )  # start far away
    create_alert(conn, "tag-1", "proximity", 100)

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
    assert alert["triggered_at"] == triggered_at
