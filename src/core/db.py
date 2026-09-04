"""The one SQLite database: where it lives, how to open it, how it migrates.

Connections and schema only -- no queries. Each feature's queries live with
the feature that owns the tables (src/findmy/db.py for devices, fixes and
alerts), so another page can reuse this plumbing without importing find-my's.

Schema is owned by Alembic (see migrations/) -- init_db() runs `alembic
upgrade head` rather than issuing DDL. Everything else talks to sqlite
directly through get_connection/connection; Alembic never reads or writes
application data.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config

from src.core.paths import DATA_DIR
from src.core.paths import REPO_ROOT

DB_PATH = DATA_DIR / "findmy.db"
_ALEMBIC_INI = REPO_ROOT / "alembic.ini"
_MIGRATIONS_DIR = REPO_ROOT / "migrations"

# A write from the API (PUT /icon) can land while the poller is mid-write. Wait
# for the lock instead of failing the request with "database is locked".
_BUSY_TIMEOUT_MS = 5000


def get_connection(path: Path | None = None) -> sqlite3.Connection:
    """Open a fresh connection, safe to call from any thread.

    `path` defaults to the module-level `DB_PATH`, read at call time rather
    than bind time so that patching it points every caller that doesn't pass a
    path -- the web layer, the poller -- at the same database.
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

    Runs on every `findmy serve`/poller boot (see src/web/app.py, src/cli.py), so
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
