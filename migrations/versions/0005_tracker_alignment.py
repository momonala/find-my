"""tracker rolling-key alignment

Where each tracker is in its key rotation, so a locate resumes there instead of
rescanning up to a week of keys. Written once per poll cycle by src/airtags.py.

Keys are deliberately not stored here. This database is swept into the
Cloudflare R2 and git backup jobs, git history is append-only, and a tracker's
master key is fixed at pairing and so cannot be rotated if leaked -- they stay
in `.icloud_session/trackers.json`, owner-read-only and git-ignored.

`tracker_id` is src/airtags.py's `_stable_id`: Apple's `identifier`, or a hash
of the master key for third-party tags where Apple leaves it empty. No foreign
key to `devices` -- `load_trackers()` needs alignment before any fetch has
happened, so a row here can legitimately predate the device row.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-27

"""

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tracker_alignment (
            tracker_id      TEXT PRIMARY KEY,
            alignment_date  TEXT NOT NULL,
            alignment_index INTEGER NOT NULL
        )
        """)


def downgrade() -> None:
    # Safe to drop: alignment is a cache, not data. Losing it costs one slow
    # rolling-key sweep per tracker, after which it rebuilds itself.
    op.execute("DROP TABLE tracker_alignment")
