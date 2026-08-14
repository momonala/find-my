"""Alembic migration environment.

No ORM models here -- migrations/versions/*.py write raw SQL via `op.execute`,
matching src/db.py's own raw-sqlite3 style rather than introducing SQLAlchemy
Core table definitions just for this. That also means `--autogenerate` has
nothing to diff against; new migrations are written by hand.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config
from sqlalchemy import pool

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: init_db() runs this on every app/test
    # boot, and fileConfig's default (True) would silence every logger the
    # app itself set up (src.telegram, src.alerts, ...) the first time it ran.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
