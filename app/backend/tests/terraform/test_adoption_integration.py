import os
import shutil
import subprocess
from pathlib import Path

import pytest

from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.main import build_executors, build_services
from rdscommander.operations.enums import OperationState
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from rdscommander.simulated.estate import load_simulated_estate
from rdscommander.simulated.seeder import seed_simulated_estate
from rdscommander.terraform.adoption_plan import build_adoption_plan
from tests.terraform.conftest import arn_for
from tests.terraform.test_adoption_executor import _git, _prepare_repo

pytestmark = pytest.mark.integration
ENDPOINT = os.environ.get("RDSC_SIMULATED_AWS_ENDPOINT_URL", "http://127.0.0.1:5000")


@pytest.fixture
def integration_settings(operator_settings):
    if os.environ.get("RDSC_INTEGRATION") != "1" or shutil.which("tofu") is None:
        pytest.skip("integration run needs RDSC_INTEGRATION=1, a moto server, and tofu")
    return operator_settings.model_copy(
        update={"simulated_aws_endpoint_url": ENDPOINT, "terraform_binary": "tofu"}
    )


def test_full_adoption_against_moto_server_reaches_a_clean_plan(
    session_factory, integration_settings, console_config, config_store
) -> None:
    _prepare_repo(integration_settings)
    sync_configuration_to_control_db(session_factory, console_config)
    services = build_services(integration_settings, session_factory)
    target = console_config.target("sandbox/000000000000/eu-west-2")
    subprocess.run(["curl", "-s", "-X", "POST", f"{ENDPOINT}/moto-api/reset"], check=False)
    seed_simulated_estate(
        services.aws_sessions.client(target, "rds"),
        services.aws_sessions.client(target, "ec2"),
        load_simulated_estate(Path("/nonexistent")),
    )
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        integration_settings,
        config_store,
        services,
    )
    service = OperationService(session_factory)
    service.create("inventory_refresh", "sandbox", None, {}, "int-refresh", "phil")
    runner.run_once()
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, target.target_key)
        session.expunge_all()
    arns = [
        arn_for(session_factory, snapshot, n)
        for n in ("demo-postgres16-tuned", "demo-aurora-pg16-tuned", "demo-private-subnets")
    ]
    with session_factory() as session:
        plan = build_adoption_plan(session, console_config, target.target_key, arns)
    operation = service.create(
        "terraform_adoption", "sandbox", target.target_key, plan.model_dump(), "int-adopt", "phil"
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [
        (d.diagnostic_kind, d.detail) for d in final.diagnostic_reports
    ]
    assert _git("status", "--porcelain", cwd=integration_settings.repo_root) == ""
