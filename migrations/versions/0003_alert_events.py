"""split alert trigger history into its own table

`alerts` conflated three concerns on one row: definition (device_id,
alert_type, threshold_m, anchor), current state (is_active), and trigger
history (triggered_at -- only ever the *last* firing, nothing before it).

This splits out trigger history into `alert_events`, one row per firing.
`alerts.is_active` stays where it is -- it's current enter/exit state, not
history. `alerts.triggered_at` is dropped; src/db.py's projection now computes
it as MAX(alert_events.triggered_at) so the API/dashboard shape is unchanged.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-15

"""

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alert_id INTEGER NOT NULL REFERENCES alerts(id),
            triggered_at TEXT NOT NULL
        )
        """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_alert_events_alert ON alert_events (alert_id, triggered_at DESC)"
    )

    # Carry forward any already-recorded last-fired time so cooldown/history
    # isn't lost for alerts that had already triggered before this migration.
    op.execute("""
        INSERT INTO alert_events (alert_id, triggered_at)
        SELECT id, triggered_at FROM alerts WHERE triggered_at IS NOT NULL
        """)

    with op.batch_alter_table("alerts") as batch_op:
        batch_op.drop_column("triggered_at")


def downgrade() -> None:
    # SQLite can't drop a table referenced by data loss reversal cleanly here;
    # not needed for this project (see 0002's downgrade note).
    raise NotImplementedError("downgrade not supported for 0003_alert_events")
