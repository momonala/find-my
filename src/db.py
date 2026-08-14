"""SQLite persistence for the Flask API: current devices, location history,
user-assigned marker emoji, and configured alerts.

Four tables: `devices` holds the latest known metadata per device (upserted
on every poll), `location_history` holds one row per fix but only when a
fix's coordinates differ from the previously stored one for that device --
repeated identical reports from Apple's network don't grow the table --
`device_icons` holds an optional emoji per device, set via the API rather
than fetched from Apple (see src/api.py's PUT /locations/<id>/icon -- Apple
doesn't expose the per-item emoji you pick in the Find My app to either
`pyicloud` or `findmy`), and `alerts` holds user-configured movement/enter/exit
alerts, evaluated by src/alerts.py from the background poller. See
src/poller.py for what writes here and src/api.py for what reads it.

Schema itself is owned by Alembic (see migrations/) -- init_db() below runs
`alembic upgrade head` rather than issuing DDL directly. Everything else in
this module still talks to sqlite the plain way, through get_connection/
connection; Alembic is only ever invoked for schema changes, never for reads
or writes of actual data.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from typing import cast

from alembic import command
from alembic.config import Config

from src.tracking import TrackedItem

_REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = _REPO_ROOT / "data"
DB_PATH = DATA_DIR / "findmy.db"
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_MIGRATIONS_DIR = _REPO_ROOT / "migrations"

# A write from the API (PUT /icon) can land while the poller is mid-write. Wait
# for the lock instead of failing the request with "database is locked".
_BUSY_TIMEOUT_MS = 5000

# No ON DELETE CASCADE on alerts.device_id, and this connection never sets
# PRAGMA foreign_keys=ON -- both fine today since no route deletes a device
# row, but worth knowing if one ever gets added.

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
    """Bring the schema up to date, creating the database file if needed.

    Runs on every `findmy serve`/poller boot (see src/api.py, src/cli.py), so
    it has to be safe to re-run against a database that's already at head --
    which `alembic upgrade head` already guarantees (a no-op once nothing's
    pending). Uses its own SQLAlchemy-driven connection, entirely separate
    from get_connection/connection's raw sqlite3 one -- Alembic never touches
    application data, only schema.
    """
    effective_path = path or DB_PATH
    effective_path.parent.mkdir(parents=True, exist_ok=True)

    config = Config(str(_ALEMBIC_INI))
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{effective_path}")
    command.upgrade(config, "head")


class FetchResult(NamedTuple):
    """What a poll cycle did: write counts per source, and which devices moved.

    `moved_device_ids` is exactly the set of devices that got a new
    `location_history` row this cycle -- src.alerts.check_alerts uses it to
    only re-evaluate alerts where something could actually have changed.
    """

    counts: dict[str, tuple[int, int]]
    moved_device_ids: set[str]


def record_fetch(conn: sqlite3.Connection, items: list[TrackedItem]) -> FetchResult:
    """Upsert device metadata, and append a history row only on a coordinate change.

    The whole cycle is one transaction, so a failure partway through a batch
    rolls back rather than leaving some devices updated and others not.

    `counts` holds, per `item.source` ("device" or "item"), how many of the
    fetched items actually got a new history row written versus how many were
    fetched -- src.poller logs this so a poll cycle's console line shows write
    volume, not just fetch volume.
    """
    now = datetime.now(UTC).isoformat()
    counts: dict[str, list[int]] = {}
    moved_device_ids: set[str] = set()

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
                moved_device_ids.add(item.id)

    return FetchResult(
        counts={source: (written, fetched) for source, (fetched, written) in counts.items()},
        moved_device_ids=moved_device_ids,
    )


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


# The projection alert reads share, joined with devices for a name/icon to
# display without a second query. Callers append their own WHERE/ORDER BY.
_ALERT_WITH_DEVICE = """
SELECT a.id, a.device_id, d.name AS device_name, di.emoji AS device_icon,
       a.alert_type, a.threshold_m, a.created_at, a.is_active, a.triggered_at,
       a.anchor_lat, a.anchor_lon
FROM alerts a
JOIN devices d ON d.id = a.device_id
LEFT JOIN device_icons di ON di.device_id = a.device_id
"""


def create_alert(
    conn: sqlite3.Connection,
    device_id: str,
    alert_type: str,
    threshold_m: float,
    *,
    anchor_lat: float | None = None,
    anchor_lon: float | None = None,
) -> int | None:
    """Create an alert for `device_id`, returning its id, or None if unknown.

    `anchor_lat`/`anchor_lon` only matter for `enter`/`exit` alerts: NULL (the
    default) means "measured from home", a value means "measured from this
    fixed point" (see src/api.py's `anchor: 'current'`, which snapshots the
    device's location at creation time rather than tracking it live).
    """
    if not device_exists(conn, device_id):
        return None

    now = datetime.now(UTC).isoformat()
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO alerts (device_id, alert_type, threshold_m, created_at, anchor_lat, anchor_lon)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (device_id, alert_type, threshold_m, now, anchor_lat, anchor_lon),
        )
    return cast("int", cursor.lastrowid)


def list_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every configured alert, across all devices, newest first."""
    return conn.execute(f"{_ALERT_WITH_DEVICE} ORDER BY a.created_at DESC").fetchall()


def alerts_for_device(conn: sqlite3.Connection, device_id: str) -> list[sqlite3.Row]:
    """A single device's configured alerts."""
    return conn.execute(
        f"{_ALERT_WITH_DEVICE} WHERE a.device_id = ? ORDER BY a.created_at", (device_id,)
    ).fetchall()


def get_alert(conn: sqlite3.Connection, alert_id: int) -> sqlite3.Row | None:
    """A single alert by id, or None if unknown."""
    return cast(
        "sqlite3.Row | None", conn.execute(f"{_ALERT_WITH_DEVICE} WHERE a.id = ?", (alert_id,)).fetchone()
    )


def remove_alert(conn: sqlite3.Connection, alert_id: int) -> bool:
    """Remove an alert. Returns False if `alert_id` is unknown.

    Named `remove_alert` rather than `delete_alert` so the DELETE route in
    src/api.py can be named `delete_alert` without shadowing this import.
    """
    with conn:
        cursor = conn.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    return cursor.rowcount > 0


def set_alert_state(
    conn: sqlite3.Connection, alert_id: int, *, is_active: bool, triggered_at: str | None
) -> None:
    """Update an alert's triggered state.

    A no-op (not an error) if `alert_id` no longer exists -- it can be deleted
    via the API in the moment between src.alerts reading it and writing this.
    """
    with conn:
        conn.execute(
            "UPDATE alerts SET is_active = ?, triggered_at = ? WHERE id = ?",
            (int(is_active), triggered_at, alert_id),
        )
