"""SQLite persistence for the Flask API: current devices, location history,
and user-assigned marker emoji.

Three tables: `devices` holds the latest known metadata per device (upserted
on every poll), `location_history` holds one row per fix but only when a
fix's coordinates differ from the previously stored one for that device --
repeated identical reports from Apple's network don't grow the table -- and
`device_icons` holds an optional emoji per device, set via the API rather
than fetched from Apple (see src/api.py's PUT /locations/<id>/icon -- Apple
doesn't expose the per-item emoji you pick in the Find My app to either
`pyicloud` or `findmy`). See src/poller.py for what writes here and
src/api.py for what reads it.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import cast

from src.tracking import TrackedItem

_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = _REPO_ROOT / "data"
DB_PATH = DATA_DIR / "findmy.db"

# A write from the API (PUT /icon) can land while the poller is mid-write. Wait
# for the lock instead of failing the request with "database is locked".
_BUSY_TIMEOUT_MS = 5000

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'item',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS location_history (
    device_id TEXT NOT NULL REFERENCES devices(id),
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    seen_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (device_id, seen_at)
);
CREATE INDEX IF NOT EXISTS idx_location_history_device
    ON location_history (device_id, seen_at DESC);
CREATE TABLE IF NOT EXISTS device_icons (
    device_id TEXT PRIMARY KEY REFERENCES devices(id),
    emoji TEXT NOT NULL
);
"""

# One row per device: its most recent location_history fix, if it has any.
_LATEST_PER_DEVICE = """
SELECT device_id, latitude, longitude, seen_at FROM (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY device_id ORDER BY seen_at DESC) AS rn
    FROM location_history
) WHERE rn = 1
"""

# The projection both location reads share, so a new response column only has to
# be added in one place. Callers append their own WHERE/ORDER BY.
_DEVICE_WITH_LATEST_FIX = f"""
SELECT d.id, d.name, d.kind, d.source, di.emoji AS icon, lh.latitude, lh.longitude, lh.seen_at
FROM devices d
LEFT JOIN device_icons di ON di.device_id = d.id
LEFT JOIN ({_LATEST_PER_DEVICE}) lh ON lh.device_id = d.id
"""


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    """Open a fresh connection, safe to call from any thread.

    `path` defaults to the module-level `DB_PATH` read at call time (not bind
    time), so tests can `monkeypatch.setattr(db, "DB_PATH", tmp_path)` and have
    it take effect for callers -- like src.api and src.poller -- that don't
    pass a path explicitly.
    """
    effective_path = path or DB_PATH
    effective_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(effective_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    return conn


@contextmanager
def connection(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """A `get_connection` that always closes, for request and poll-cycle scopes."""
    conn = get_connection(path)
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    """Create the schema if it doesn't exist yet."""
    with connection(path) as conn:
        conn.executescript(_CREATE_TABLES)
        conn.commit()


def record_fetch(conn: sqlite3.Connection, items: list[TrackedItem]) -> dict[str, tuple[int, int]]:
    """Upsert device metadata, and append a history row only on a coordinate change.

    The whole cycle is one transaction, so a failure partway through a batch
    rolls back rather than leaving some devices updated and others not.

    Returns, per `item.source` ("device" or "item"), how many of the fetched
    items actually got a new history row written versus how many were fetched
    -- src.poller logs this so a poll cycle's console line shows write volume,
    not just fetch volume.
    """
    now = datetime.now(UTC).isoformat()
    counts: dict[str, list[int]] = {}

    with conn:
        for item in items:
            counts.setdefault(item.source, [0, 0])
            counts[item.source][0] += 1

            conn.execute(
                """
                INSERT INTO devices (id, name, kind, source, updated_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET name = excluded.name, kind = excluded.kind,
                    source = excluded.source, updated_at = excluded.updated_at
                """,
                (item.id, item.name, item.kind, item.source, now),
            )

            if item.location is None:
                continue

            last = conn.execute(
                "SELECT latitude, longitude FROM location_history WHERE device_id = ? "
                "ORDER BY seen_at DESC LIMIT 1",
                (item.id,),
            ).fetchone()
            moved = last is None or (last["latitude"], last["longitude"]) != (
                item.location.latitude,
                item.location.longitude,
            )
            if moved:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO location_history
                        (device_id, latitude, longitude, seen_at, recorded_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item.id,
                        item.location.latitude,
                        item.location.longitude,
                        item.location.seen_at.isoformat(),
                        now,
                    ),
                )
                counts[item.source][1] += 1

    return {source: (written, fetched) for source, (fetched, written) in counts.items()}


def all_latest_locations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """One row per known device, with its latest fix if it has one."""
    return conn.execute(f"{_DEVICE_WITH_LATEST_FIX} ORDER BY d.name").fetchall()


def latest_location_for(conn: sqlite3.Connection, device_id: str) -> sqlite3.Row | None:
    """The device's latest fix, or None if `device_id` is unknown."""
    # sqlite3.Cursor.fetchone() is typed to return Any (its type depends on the
    # connection's row_factory, which mypy can't see); get_connection() always
    # sets sqlite3.Row, so this is a type-annotation cast, not a runtime check.
    return cast(
        "sqlite3.Row | None",
        conn.execute(f"{_DEVICE_WITH_LATEST_FIX} WHERE d.id = ?", (device_id,)).fetchone(),
    )


def set_device_icon(conn: sqlite3.Connection, device_id: str, emoji: str | None) -> bool:
    """Set (or clear, if `emoji` is falsy) a device's marker emoji.

    Returns False if `device_id` is unknown, so the caller can 404.
    """
    if not device_exists(conn, device_id):
        return False

    with conn:
        if emoji:
            conn.execute(
                """
                INSERT INTO device_icons (device_id, emoji) VALUES (?, ?)
                ON CONFLICT(device_id) DO UPDATE SET emoji = excluded.emoji
                """,
                (device_id, emoji),
            )
        else:
            conn.execute("DELETE FROM device_icons WHERE device_id = ?", (device_id,))
    return True


def last_updated(conn: sqlite3.Connection) -> str | None:
    """When the most recent full poll cycle wrote to `devices`, or None if it never has."""
    row = conn.execute("SELECT MAX(updated_at) AS last_updated FROM devices").fetchone()
    return cast("str | None", row["last_updated"])


def device_exists(conn: sqlite3.Connection, device_id: str) -> bool:
    return conn.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,)).fetchone() is not None


def history_for(
    conn: sqlite3.Connection,
    device_id: str,
    since: str | None = None,
    limit: int | None = None,
) -> list[sqlite3.Row] | None:
    """Fixes for `device_id`, newest first, or None if the device is unknown."""
    if not device_exists(conn, device_id):
        return None

    query = "SELECT latitude, longitude, seen_at FROM location_history WHERE device_id = ?"
    params: list[object] = [device_id]
    if since is not None:
        query += " AND seen_at >= ?"
        params.append(since)
    query += " ORDER BY seen_at DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    return conn.execute(query, params).fetchall()
