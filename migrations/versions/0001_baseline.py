"""baseline schema

Mirrors the schema src/db.py's `_CREATE_TABLES` used to create directly
(before schema ownership moved to Alembic): devices, location_history,
device_icons, and alerts (already on the movement/enter/exit split -- the
'proximity' type was retired and any existing rows fixed up by hand before
this migration existed, not by a migration).

Every statement is IF NOT EXISTS: this is the first migration a database
sees, but most databases running it aren't actually empty -- they already
have this exact schema from init_db()'s old executescript. Only a genuinely
fresh database creates anything here.

Revision ID: 0001
Revises:
Create Date: 2026-08-14

"""

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS devices (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'item',
            updated_at TEXT NOT NULL
        )
        """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS location_history (
            device_id TEXT NOT NULL REFERENCES devices(id),
            latitude REAL NOT NULL,
            longitude REAL NOT NULL,
            seen_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            PRIMARY KEY (device_id, seen_at)
        )
        """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_location_history_device ON location_history (device_id, seen_at DESC)"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS device_icons (
            device_id TEXT PRIMARY KEY REFERENCES devices(id),
            emoji TEXT NOT NULL
        )
        """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL REFERENCES devices(id),
            alert_type TEXT NOT NULL CHECK(alert_type IN ('movement', 'enter', 'exit')),
            threshold_m REAL NOT NULL,
            created_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            triggered_at TEXT
        )
        """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_alerts_device ON alerts (device_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS alerts")
    op.execute("DROP TABLE IF EXISTS device_icons")
    op.execute("DROP TABLE IF EXISTS location_history")
    op.execute("DROP TABLE IF EXISTS devices")
