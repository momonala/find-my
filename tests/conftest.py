"""Shared fixtures and builders for the test suite.

`make_item`/`make_location` were duplicated verbatim in test_api.py and
test_db.py; they live here so the domain shape is described once. `make_item`
takes a whole `Location` rather than loose coordinates, so a half-specified
position (a latitude with no longitude) can't be expressed at all.
"""

from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

import src.db as db
import src.telegram as telegram
from src.api import create_app
from src.tracking import Location
from src.tracking import TrackedItem

# Fixed so that anything deriving an age or ordering from it is deterministic.
NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def make_location(latitude: float, longitude: float, seen_at: datetime = NOW) -> Location:
    return Location(latitude=latitude, longitude=longitude, seen_at=seen_at)


def make_item(
    item_id: str,
    location: Location | None = None,
    *,
    kind: str = "AirTag",
    source: str = "item",
) -> TrackedItem:
    return TrackedItem(
        id=item_id,
        name=f"Item {item_id}",
        kind=kind,
        source=source,
        location=location,
    )


def minutes_later(minutes: int) -> datetime:
    """A timestamp `minutes` after NOW, for building movement sequences."""
    return NOW + timedelta(minutes=minutes)


@pytest.fixture(autouse=True)
def _no_real_telegram_credentials(monkeypatch):
    """Blank out Telegram credentials for every test, even if `.env` has real
    ones loaded -- alert tests that don't explicitly mock the send_*_alert
    functions must never be able to reach the real Telegram API.
    test_telegram.py's own tests still work: their @patch decorators target
    the same attributes and take precedence for the duration of the test."""
    monkeypatch.setattr(telegram, "TELEGRAM_API_TOKEN", "")
    monkeypatch.setattr(telegram, "TELEGRAM_CHAT_ID", "")


@pytest.fixture
def conn(tmp_path):
    """An open connection to a freshly initialised temp database."""
    db_path = tmp_path / "findmy.db"
    db.init_db(db_path)
    connection = db.get_connection(db_path)
    yield connection
    connection.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A Flask test client backed by a temp DB, with no background poller."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "findmy.db")
    app = create_app(start_poller=False)
    return app.test_client()


@pytest.fixture
def seed(client):
    """Write items straight to the client's database, bypassing Apple."""

    def _seed(*items: TrackedItem) -> None:
        with db.connection() as connection:
            db.record_fetch(connection, list(items))

    return _seed
