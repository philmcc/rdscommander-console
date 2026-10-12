"""Engine, session factory, and programmatic migrations."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

ControlDbSessionFactory = sessionmaker[Session]


def create_control_db_engine(url: str) -> Engine:
    """Create a pooled engine with pre-ping so dropped connections are retried."""
    return create_engine(url, pool_pre_ping=True, future=True)


def make_session_factory(engine: Engine) -> ControlDbSessionFactory:
    """Session factory bound to `engine`."""
    return sessionmaker(bind=engine, expire_on_commit=False)


def run_migrations(url: str) -> None:
    """Apply all Alembic migrations to the database at `url`."""
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parent / "migrations"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")
