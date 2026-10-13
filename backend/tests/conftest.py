"""Shared fixtures: throwaway Postgres, migrated schema, settings, config, moto."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from moto import mock_aws
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer

from rdscommander.config.models import ConsoleConfig
from rdscommander.config.settings import OperatorSettings
from rdscommander.config.store import StaticConfigStore
from rdscommander.control_db.models import Base
from rdscommander.control_db.session import (
    ControlDbSessionFactory,
    create_control_db_engine,
    make_session_factory,
    run_migrations,
)
from tests.recorded_aws_calls import collect_recorded_aws_api_names

RUNS_AFTER_EVERY_OTHER_TEST_MARKER = "runs_after_every_other_test"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Move tests marked `runs_after_every_other_test` to the end, keeping everything else in order."""
    last = [item for item in items if item.get_closest_marker(RUNS_AFTER_EVERY_OTHER_TEST_MARKER)]
    if last:
        first = [item for item in items if not item.get_closest_marker(RUNS_AFTER_EVERY_OTHER_TEST_MARKER)]
        items[:] = first + last


@pytest.fixture(scope="session", autouse=True)
def recorded_product_aws_api_names() -> Iterator[None]:
    """Remember every AWS API name the console's own code calls during the session (see the IAM test)."""
    with collect_recorded_aws_api_names():
        yield


@pytest.fixture(scope="session")
def control_db_url() -> Iterator[str]:
    with PostgresContainer("postgres:17", driver="psycopg") as container:
        url = container.get_connection_url()
        run_migrations(url)
        yield url


@pytest.fixture
def session_factory(control_db_url: str) -> Iterator[ControlDbSessionFactory]:
    engine = create_control_db_engine(control_db_url)
    factory = make_session_factory(engine)
    yield factory
    with engine.begin() as connection:
        tables = ", ".join(f'"{table.name}"' for table in reversed(Base.metadata.sorted_tables))
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    engine.dispose()


@pytest.fixture
def operator_settings(tmp_path: Path, control_db_url: str) -> OperatorSettings:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    return OperatorSettings(
        operator_name="test-operator",
        git_author_name="Test Operator",
        git_author_email="test@example.invalid",
        control_database_url=control_db_url,
        config_dir=repo_root / "config",
        repo_root=repo_root,
        simulated_aws_endpoint_url=None,
        runner_enabled=False,
        scheduler_enabled=False,
        allow_checkout_without_upstream=True,
    )


@pytest.fixture
def console_config() -> ConsoleConfig:
    return ConsoleConfig.model_validate(
        {
            "terraform": {},
            "environments": [
                {
                    "slug": "sandbox",
                    "display_name": "Sandbox",
                    "targets": [
                        {"aws_account_id": "000000000000", "aws_region": "eu-west-2", "simulated": True}
                    ],
                },
                {
                    "slug": "production",
                    "display_name": "Production",
                    "targets": [
                        {
                            "aws_account_id": "123456789012",
                            "aws_region": "eu-west-2",
                            "aws_profile_name": "example-prod",
                        }
                    ],
                },
            ],
        }
    )


@pytest.fixture
def config_store(
    console_config: ConsoleConfig, session_factory: ControlDbSessionFactory
) -> StaticConfigStore:
    """A configuration store around `console_config`; nothing is synced until a test asks for it."""
    return StaticConfigStore(console_config, session_factory)


@pytest.fixture
def moto_aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-2")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield
