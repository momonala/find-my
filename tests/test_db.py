"""Tests for src/db.py.

These tests verify that:
1. A device's metadata is recorded even without a location
2. A history row is written only when coordinates actually change
3. Latest-location and history lookups distinguish "no fix yet" from "unknown device"
4. Marker emoji can be set and cleared
5. Alerts can be created, listed, deleted, and have their triggered state updated
"""

from src.db import alerts_for_device
from src.db import all_latest_locations
from src.db import create_alert
from src.db import get_alert
from src.db import history_for
from src.db import latest_location_for
from src.db import list_alerts
from src.db import log_alert_event
from src.db import record_fetch
from src.db import remove_alert
from src.db import set_alert_active
from src.db import set_device_icon
from src.db import update_alert
from tests.conftest import make_item
from tests.conftest import make_location
from tests.conftest import minutes_later


def test_device_without_location_is_recorded_without_history(conn):
    record_fetch(conn, [make_item("no-fix")])

    row = latest_location_for(conn, "no-fix")
    assert row["id"] == "no-fix"
    assert row["latitude"] is None


def test_unknown_device_returns_none(conn):
    assert latest_location_for(conn, "does-not-exist") is None
    assert history_for(conn, "does-not-exist") is None


def test_repeated_identical_fix_does_not_grow_history(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])

    assert len(history_for(conn, "tag-1")) == 1


def test_changed_fix_adds_a_history_row(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])

    history = history_for(conn, "tag-1")
    assert len(history) == 2
    assert history[0]["latitude"] == 52.6  # newest first


def test_all_latest_locations_includes_devices_without_a_fix(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4)), make_item("no-fix")])

    rows = {row["id"]: row for row in all_latest_locations(conn)}
    assert rows["tag-1"]["latitude"] == 52.5
    assert rows["no-fix"]["latitude"] is None


def test_history_respects_limit(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(1)))])
    record_fetch(conn, [make_item("tag-1", make_location(52.7, 13.4, minutes_later(2)))])

    assert len(history_for(conn, "tag-1", limit=2)) == 2


def test_history_respects_since(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    record_fetch(conn, [make_item("tag-1", make_location(52.6, 13.4, minutes_later(10)))])

    recent = history_for(conn, "tag-1", since=minutes_later(5).isoformat())
    assert [row["latitude"] for row in recent] == [52.6]


def test_new_device_has_no_icon(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    assert latest_location_for(conn, "tag-1")["icon"] is None


def test_set_device_icon_is_reflected_in_lookups(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])

    assert set_device_icon(conn, "tag-1", "🚲") is True
    assert latest_location_for(conn, "tag-1")["icon"] == "🚲"
    assert {row["id"]: row["icon"] for row in all_latest_locations(conn)}["tag-1"] == "🚲"


def test_clearing_device_icon_removes_it(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    set_device_icon(conn, "tag-1", "🚲")

    set_device_icon(conn, "tag-1", None)

    assert latest_location_for(conn, "tag-1")["icon"] is None


def test_set_device_icon_for_unknown_device_returns_false(conn):
    assert set_device_icon(conn, "does-not-exist", "🚲") is False


# --- Alerts ------------------------------------------------------------------


def test_create_alert_for_unknown_device_returns_none(conn):
    assert create_alert(conn, "does-not-exist", "movement", 100) is None


def test_create_alert_is_reflected_in_list_and_device_lookups(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])

    alert_id = create_alert(conn, "tag-1", "movement", 150)

    assert alert_id is not None
    row = get_alert(conn, alert_id)
    assert row["device_id"] == "tag-1"
    assert row["alert_type"] == "movement"
    assert row["threshold_m"] == 150
    assert row["is_active"] == 0
    assert row["triggered_at"] is None
    assert row["anchor_lat"] is None
    assert row["anchor_lon"] is None
    assert [row["id"] for row in list_alerts(conn)] == [alert_id]
    assert [row["id"] for row in alerts_for_device(conn, "tag-1")] == [alert_id]


def test_create_alert_stores_a_custom_anchor(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])

    alert_id = create_alert(conn, "tag-1", "enter", 100, anchor_lat=52.5, anchor_lon=13.4)

    row = get_alert(conn, alert_id)
    assert row["anchor_lat"] == 52.5
    assert row["anchor_lon"] == 13.4


def test_update_alert_changes_type_threshold_and_anchor(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    alert_id = create_alert(conn, "tag-1", "movement", 150)

    assert update_alert(conn, alert_id, "exit", 250, anchor_lat=52.5, anchor_lon=13.4) is True

    row = get_alert(conn, alert_id)
    assert row["device_id"] == "tag-1"
    assert row["alert_type"] == "exit"
    assert row["threshold_m"] == 250
    assert row["anchor_lat"] == 52.5
    assert row["anchor_lon"] == 13.4


def test_update_unknown_alert_returns_false(conn):
    assert update_alert(conn, 999, "movement", 100) is False


def test_alerts_for_device_is_empty_for_a_device_with_no_alerts(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])

    assert alerts_for_device(conn, "tag-1") == []


def test_remove_alert_deletes_it(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    alert_id = create_alert(conn, "tag-1", "enter", 100)

    assert remove_alert(conn, alert_id) is True
    assert get_alert(conn, alert_id) is None


def test_remove_unknown_alert_returns_false(conn):
    assert remove_alert(conn, 999) is False


def test_set_alert_active_updates_is_active(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    alert_id = create_alert(conn, "tag-1", "exit", 100)

    set_alert_active(conn, alert_id, is_active=True)

    assert get_alert(conn, alert_id)["is_active"] == 1


def test_set_alert_active_for_unknown_alert_is_a_no_op(conn):
    set_alert_active(conn, 999, is_active=True)  # must not raise


def test_log_alert_event_is_reflected_as_triggered_at(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    alert_id = create_alert(conn, "tag-1", "exit", 100)

    log_alert_event(conn, alert_id, "2026-01-01T00:00:00+00:00")

    assert get_alert(conn, alert_id)["triggered_at"] == "2026-01-01T00:00:00+00:00"


def test_log_alert_event_triggered_at_is_the_latest_event(conn):
    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    alert_id = create_alert(conn, "tag-1", "exit", 100)

    log_alert_event(conn, alert_id, "2026-01-01T00:00:00+00:00")
    log_alert_event(conn, alert_id, "2026-01-02T00:00:00+00:00")

    assert get_alert(conn, alert_id)["triggered_at"] == "2026-01-02T00:00:00+00:00"


def test_init_db_is_idempotent(conn, tmp_path):
    """Re-initialising an existing database preserves its rows.

    `init_db` runs on every `findmy serve` boot, so it has to be safe to
    re-apply to a database that already holds history.
    """
    from src.db import init_db

    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    init_db(tmp_path / "findmy.db")

    assert latest_location_for(conn, "tag-1")["latitude"] == 52.5


def test_init_db_runs_alembic_migrations_to_head(tmp_path):
    """init_db() drives schema via `alembic upgrade head` (see migrations/),
    not raw DDL -- a fresh database should end up fully migrated, including
    the alerts.anchor_lat/anchor_lon columns added after the baseline."""
    from src.db import get_connection
    from src.db import init_db

    db_path = tmp_path / "findmy.db"
    init_db(db_path)

    conn = get_connection(db_path)
    assert conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"] == "0004"
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(alerts)")}
    assert {"anchor_lat", "anchor_lon"} <= columns
    device_columns = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    assert "battery_level" in device_columns
