"""add device battery level

Stores each device's last-known battery level -- "Full", "Medium", "Low",
"Very Low", or NULL if unknown/unsupported. Only src/airtags.py populates
this (decoded from the status byte in Apple's crowdsourced network reports);
src/find_my.py's iCloud devices leave it NULL.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-18

"""

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE devices ADD COLUMN battery_level TEXT")


def downgrade() -> None:
    # SQLite can't drop a column without a full table rebuild; not needed here.
    raise NotImplementedError("downgrade not supported for 0004_device_battery")
