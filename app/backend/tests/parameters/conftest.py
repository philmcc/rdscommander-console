"""Fixtures for the direct AWS API helpers: a step context over the seeded direct-mode estate."""

import uuid

import pytest

from rdscommander.control_db.models import Operation
from rdscommander.operations.executor import StepContext
from tests.drift.conftest import direct_mode_runner  # noqa: F401
from tests.terraform.conftest import TARGET_KEY, sandbox_snapshot  # noqa: F401


@pytest.fixture
def make_step_context(session_factory, operator_settings, direct_mode_runner):  # noqa: F811
    """`make_step_context(resource_arn)` builds a context whose operation acts on `resource_arn`."""
    runner, _, _ = direct_mode_runner

    def build(session, resource_arn: str) -> StepContext:
        operation = Operation(
            operation_kind="test_direct_api",
            state="RUNNING",
            environment_slug="sandbox",
            target_key=TARGET_KEY,
            resource_arn=resource_arn,
            title="direct api",
            plan={},
            idempotency_key=f"direct-api-{uuid.uuid4()}",
            created_by_operator_name="phil",
        )
        session.add(operation)
        session.commit()
        return StepContext(
            session=session,
            operation=operation,
            settings=operator_settings,
            console_config=runner.config_store.current,
            services=runner.services,
            runner_identity="r",
        )

    return build
