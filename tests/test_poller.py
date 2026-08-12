"""Tests for src/poller.py.

The poller is the piece that runs unattended, so the behaviour worth pinning is
what it does when Apple fetches fail: keep the last known data, back off, and
escalate the log level once the failure looks persistent rather than transient.
"""

import threading

import pytest

import src.poller as poller
from src.errors import MissingCredentialsError
from tests.conftest import make_item
from tests.conftest import make_location


def test_backoff_is_the_normal_interval_when_healthy():
    assert poller._backoff_seconds(0) == poller.POLL_INTERVAL_SECONDS


def test_backoff_grows_with_consecutive_failures():
    first = poller._backoff_seconds(1)
    second = poller._backoff_seconds(2)

    assert first == poller.POLL_INTERVAL_SECONDS * 2
    assert second > first


def test_backoff_is_capped():
    assert poller._backoff_seconds(50) == poller.MAX_BACKOFF_SECONDS


def test_poll_once_records_fetched_items(tmp_path, monkeypatch):
    import src.db as db

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "findmy.db")
    db.init_db()
    monkeypatch.setattr(poller, "fetch_devices", lambda: [make_item("tag-1", make_location(52.5, 13.4))])
    monkeypatch.setattr(poller, "fetch_airtags", lambda: [])

    poller._poll_once()

    with db.connection() as conn:
        assert db.latest_location_for(conn, "tag-1")["latitude"] == 52.5


def test_run_forever_survives_a_failing_cycle(monkeypatch):
    """A raising fetch must not kill the loop -- the API keeps serving stale data."""
    stop_event = threading.Event()
    calls = []

    def failing_poll():
        calls.append(1)
        if len(calls) >= 2:
            stop_event.set()
        raise MissingCredentialsError("no credentials")

    monkeypatch.setattr(poller, "_poll_once", failing_poll)
    # Don't actually sleep through the backoff.
    monkeypatch.setattr(stop_event, "wait", lambda _seconds: None)

    poller.run_forever(stop_event)

    assert len(calls) == 2


def test_run_forever_stops_when_the_event_is_set(monkeypatch):
    stop_event = threading.Event()
    stop_event.set()
    monkeypatch.setattr(poller, "_poll_once", lambda: pytest.fail("should not poll when already stopped"))

    poller.run_forever(stop_event)
