"""add alert anchor point

`enter`/`exit` alerts were always measured from the configured home
coordinates. This lets an alert anchor to an arbitrary point instead: NULL
(the default) still means "home"; a value means the device's location at
the moment the alert was created (see src/api.py's `anchor: "current"`).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-14

"""

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE alerts ADD COLUMN anchor_lat REAL")
    op.execute("ALTER TABLE alerts ADD COLUMN anchor_lon REAL")


def downgrade() -> None:
    # SQLite can't drop a column without a full table rebuild; not needed here.
    raise NotImplementedError("downgrade not supported for 0002_alert_anchor")
