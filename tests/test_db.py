"""Tests for src/db.py.

These tests verify that:
1. A device's metadata is recorded even without a location
2. A history row is written only when coordinates actually change
3. Latest-location and history lookups distinguish "no fix yet" from "unknown device"
4. Marker emoji can be set and cleared
"""

from src.db import all_latest_locations
from src.db import history_for
from src.db import latest_location_for
from src.db import record_fetch
from src.db import set_device_icon
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


def test_init_db_is_idempotent(conn, tmp_path):
    """Re-initialising an existing database preserves its rows.

    `init_db` runs on every `findmy serve` boot, so it has to be safe to
    re-apply to a database that already holds history.
    """
    from src.db import init_db

    record_fetch(conn, [make_item("tag-1", make_location(52.5, 13.4))])
    init_db(tmp_path / "findmy.db")

    assert latest_location_for(conn, "tag-1")["latitude"] == 52.5
