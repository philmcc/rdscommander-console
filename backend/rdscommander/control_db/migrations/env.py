"""Alembic environment wired to the declarative metadata, with comments enabled."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from rdscommander.control_db.models import Base

config = context.config
if os.environ.get("RDSC_CONTROL_DATABASE_URL"):
    config.set_main_option("sqlalchemy.url", os.environ["RDSC_CONTROL_DATABASE_URL"].replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
