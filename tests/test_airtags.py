"""Tests for src/airtags.py's tracker key cache and alignment handling.

The master keys in `.icloud_session/trackers.json` cannot be regenerated on a
non-macOS host, so the properties worth pinning are that nothing on the poll
path writes that file, that the one path which does write it cannot leave a
partial file behind, and that the alignment cache it no longer carries survives
a round trip through the database.
"""

import os
import sqlite3
import stat
from datetime import UTC
from datetime import datetime

import pytest

import src.airtags as airtags
import src.db as db

PAIRED_AT = datetime(2026, 8, 1, tzinfo=UTC)


def make_accessory(identifier: str = "ID-1", master_key: bytes = b"\x01" * 28):
    """A real FindMyAccessory -- the alignment logic reads upstream's own fields."""
    return airtags.FindMyAccessory(
        master_key=master_key,
        skn=b"\x02" * 32,
        sks=b"\x03" * 32,
        paired_at=PAIRED_AT,
        name=f"Tag {identifier}",
        model="AirTag",
        identifier=identifier,
    )


@pytest.fixture
def session_dir(tmp_path, monkeypatch):
    """Point the tracker cache and the database at temp locations."""
    monkeypatch.setattr(airtags, "_TRACKERS_FILE", tmp_path / "trackers.json")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "findmy.db")
    db.init_db()
    return tmp_path


# --------------------------------------------------------------- key cache ----


def test_saved_key_cache_is_read_only(session_dir):
    airtags._save_trackers([make_accessory()])

    mode = airtags._TRACKERS_FILE.stat().st_mode
    assert stat.S_IMODE(mode) == 0o400


def test_saving_leaves_no_temp_file_behind(session_dir):
    airtags._save_trackers([make_accessory()])

    assert sorted(p.name for p in session_dir.iterdir() if "tmp" in p.name) == []


def test_a_failed_save_leaves_the_previous_keys_intact(session_dir, monkeypatch):
    """The whole point of staging: a crash mid-write must not destroy the keys."""
    airtags._save_trackers([make_accessory("ID-original")])
    original = airtags._TRACKERS_FILE.read_text()

    def _explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(airtags.json, "dumps", _explode)
    with pytest.raises(OSError, match="disk full"):
        airtags._save_trackers([make_accessory("ID-replacement")])

    assert airtags._TRACKERS_FILE.read_text() == original
    assert stat.S_IMODE(airtags._TRACKERS_FILE.stat().st_mode) == 0o400
    assert [p.name for p in session_dir.iterdir() if "tmp" in p.name] == []


def test_a_read_only_cache_can_still_be_refreshed(session_dir):
    """0o400 must not wedge --refresh-keys: os.replace needs the directory, not the file."""
    airtags._save_trackers([make_accessory("ID-old")])

    airtags._save_trackers([make_accessory("ID-new")])

    assert "ID-new" in airtags._TRACKERS_FILE.read_text()
    assert stat.S_IMODE(airtags._TRACKERS_FILE.stat().st_mode) == 0o400


def test_loading_the_cache_never_writes_it(session_dir):
    """The poll path reads keys and must not touch the file, mode or mtime."""
    airtags._save_trackers([make_accessory()])
    before = airtags._TRACKERS_FILE.stat()
    os.utime(airtags._TRACKERS_FILE, (0, 0))  # an old mtime, so any rewrite shows

    trackers = airtags.load_trackers()
    airtags._persist_alignment(trackers)

    after = airtags._TRACKERS_FILE.stat()
    assert after.st_mtime == 0
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)


# --------------------------------------------------------------- alignment ----


def test_alignment_round_trips_through_the_database(session_dir):
    tracker = make_accessory()
    observed_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    tracker.update_alignment(observed_at, 4210)
    airtags._persist_alignment([tracker])

    restored = make_accessory()
    airtags._apply_alignment([restored])

    assert restored._alignment_index == 4210
    assert restored._alignment_date == observed_at


def test_alignment_is_keyed_per_tracker(session_dir):
    first, second = make_accessory("ID-1"), make_accessory("ID-2", b"\x09" * 28)
    first.update_alignment(datetime(2026, 8, 20, tzinfo=UTC), 100)
    second.update_alignment(datetime(2026, 8, 21, tzinfo=UTC), 200)
    airtags._persist_alignment([first, second])

    restored = [make_accessory("ID-1"), make_accessory("ID-2", b"\x09" * 28)]
    airtags._apply_alignment(restored)

    assert [t._alignment_index for t in restored] == [100, 200]


def test_a_tracker_with_no_stored_alignment_is_left_alone(session_dir):
    """A newly paired tracker has no row; that must not raise or reset it."""
    tracker = make_accessory("ID-unseen")

    airtags._apply_alignment([tracker])

    assert tracker._alignment_index == 0
    assert tracker._alignment_date == PAIRED_AT


def test_a_stale_stored_alignment_does_not_move_a_tracker_backwards(session_dir):
    tracker = make_accessory()
    tracker.update_alignment(datetime(2026, 8, 20, tzinfo=UTC), 5000)
    airtags._persist_alignment([tracker])

    ahead = make_accessory()
    ahead.update_alignment(datetime(2026, 8, 25, tzinfo=UTC), 9000)
    airtags._apply_alignment([ahead])

    assert ahead._alignment_index == 9000


def test_real_accessory_still_exposes_the_alignment_fields():
    """Guard the private reads in `_persist_alignment` against an upstream rename."""
    tracker = make_accessory()

    assert isinstance(tracker._alignment_date, datetime)
    assert isinstance(tracker._alignment_index, int)


def test_a_lookup_survives_a_database_with_no_alignment_table(tmp_path, monkeypatch):
    """`findmy airtags` and `findmy all` never call init_db(), so the table can
    be absent -- the cache must degrade, not raise."""
    monkeypatch.setattr(airtags, "_TRACKERS_FILE", tmp_path / "trackers.json")
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "unmigrated.db")
    airtags._save_trackers([make_accessory()])

    trackers = airtags.load_trackers()
    airtags._persist_alignment(trackers)

    assert [t.identifier for t in trackers] == ["ID-1"]
    assert trackers[0]._alignment_index == 0


def test_alignment_failures_are_logged_not_silent(session_dir, monkeypatch, caplog):
    def _broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(airtags, "load_tracker_alignment", _broken)
    with caplog.at_level("WARNING"):
        airtags._apply_alignment([make_accessory()])

    assert "Could not read tracker alignment" in caplog.text
