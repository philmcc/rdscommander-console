"""Drift test fixtures: the seeded simulated estate, and a runner for an environment in direct mode."""

import pytest

from rdscommander.config.store import StaticConfigStore
from rdscommander.main import build_executors, build_services
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from rdscommander.terraform.render_input import build_render_input
from rdscommander.terraform.renderer import render_workspace_files, write_workspace_files
from tests.terraform.conftest import TARGET_KEY, sandbox_snapshot  # noqa: F401
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _git, _prepare_repo


@pytest.fixture
def direct_mode_runner(session_factory, operator_settings, console_config, sandbox_snapshot):  # noqa: F811
    """`(runner, service, fake_terraform_runner)` for a sandbox that changes AWS directly.

    The repository is initialised and holds a committed record of the whole estate, as a direct-mode
    environment has once its first import has run.
    """
    _prepare_repo(operator_settings)
    direct_config = console_config.model_copy(deep=True)
    direct_config.environments[0].change_management = "direct"
    direct_store = StaticConfigStore(direct_config, session_factory)
    direct_store.reload()
    target = direct_config.target(TARGET_KEY)
    with session_factory() as session:
        estate = build_render_input(
            session,
            target,
            direct_config,
            sandbox_snapshot,
            set(),
            set(),
            include_imports=False,
            simulated_endpoint_url=None,
            include_all=True,
        )
    write_workspace_files(
        operator_settings.repo_root / target.workspace_relative_dir,
        render_workspace_files(estate, format_binary=operator_settings.terraform_binary),
        record=estate,
        snapshot_id=sandbox_snapshot.id,
    )
    _git("add", "-A", cwd=operator_settings.repo_root)
    _git("commit", "-q", "-m", "Record the estate", cwd=operator_settings.repo_root)
    services = build_services(operator_settings, session_factory)
    fake = FakeTerraformRunner(services.execution_recorder)
    services.terraform_runner = fake
    runner = OperationRunner(
        session_factory,
        build_executors(services, direct_store),
        "r",
        operator_settings,
        direct_store,
        services,
    )
    return runner, OperationService(session_factory), fake
