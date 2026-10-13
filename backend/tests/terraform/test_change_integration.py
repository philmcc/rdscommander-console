"""Terraform-mode changes planned and applied by real OpenTofu against a moto server.

Every change-strategy unit test scripts the plan; this one lets `tofu` plan and apply the HCL the renderer
emits for an attach (a reference to an adopted group), a delete, a restore of drift and a create, and creates
an instance from the template (`test_terraform_mode_create_instance_with_real_opentofu`). Run with
`RDSC_INTEGRATION=1`, `RDSC_SIMULATED_AWS_ENDPOINT_URL` pointing at a moto server (the compose
`simulated-aws` service with a published port) and `tofu` on PATH.
"""

import re
import subprocess
from pathlib import Path

import pytest

from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.control_db.models import DriftFinding
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.main import build_executors, build_services
from rdscommander.operations.enums import OperationState
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from rdscommander.simulated.estate import load_simulated_estate
from rdscommander.simulated.seeder import seed_simulated_estate
from rdscommander.terraform.adoption_plan import build_adoption_plan
from rdscommander.terraform.record import read_record
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import _git, _prepare_repo
from tests.terraform.test_adoption_integration import ENDPOINT, integration_settings  # noqa: F401

pytestmark = pytest.mark.integration
WORKSPACE = Path("terraform") / "sandbox" / "000000000000" / "eu-west-2"


def _diagnostics(operation) -> list[tuple[str, str]]:
    return [(d.diagnostic_kind, d.detail) for d in operation.diagnostic_reports]


def _work_mem(rds, group_name: str) -> str | None:
    pages = rds.get_paginator("describe_db_parameters").paginate(DBParameterGroupName=group_name)
    return next(
        (
            p.get("ParameterValue")
            for page in pages
            for p in page["Parameters"]
            if p["ParameterName"] == "work_mem"
        ),
        None,
    )


def test_terraform_mode_attach_delete_restore_and_create_with_real_opentofu(
    session_factory,
    integration_settings,  # noqa: F811
    console_config,
    config_store,
) -> None:
    _prepare_repo(integration_settings)
    sync_configuration_to_control_db(session_factory, console_config)
    services = build_services(integration_settings, session_factory)
    services.sleep = lambda seconds: None
    target = console_config.target(TARGET_KEY)
    subprocess.run(["curl", "-s", "-X", "POST", f"{ENDPOINT}/moto-api/reset"], check=False)
    rds = services.aws_sessions.client(target, "rds")
    seed_simulated_estate(rds, services.aws_sessions.client(target, "ec2"), load_simulated_estate(Path("/x")))
    # The simulator gives every DB instance the same DbiResourceId, and the AWS provider looks an instance
    # up by that id while it waits for a modification to finish; with demo-orders the only instance left,
    # the lookup is unambiguous (see "Known limits" in docs/verification-results.md).
    for identifier in ("demo-orders-replica", "demo-aurora-reader", "demo-aurora-writer"):
        rds.delete_db_instance(DBInstanceIdentifier=identifier, SkipFinalSnapshot=True)
    for name in ("orders-pg16-alt", "unused-pg16"):
        rds.create_db_parameter_group(
            DBParameterGroupName=name, DBParameterGroupFamily="postgres16", Description=f"{name} group"
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

    def run_to_end(operation_id):  # noqa: ANN001, ANN202
        runner.run_once()
        waiting_operation = service.get(operation_id)
        if waiting_operation.state == OperationState.AWAITING_APPROVAL:
            # The approver types the identifier the review asks for, as a person would.
            service.approve(operation_id, "sam", confirmation_text=waiting_operation.approval_required_text)
            runner.run_once()
        return service.get(operation_id)

    def refresh(key: str):  # noqa: ANN202
        service.create("inventory_refresh", "sandbox", None, {}, key, "phil")
        runner.run_once()
        with session_factory() as session:
            snapshot = latest_snapshot_for_target(session, TARGET_KEY)
            session.expunge_all()
            return snapshot

    snapshot = refresh("tofu-refresh")
    names = ("demo-orders", "demo-postgres16-tuned", "demo-private-subnets", "orders-pg16-alt", "unused-pg16")
    with session_factory() as session:
        adoption_plan = build_adoption_plan(
            session, console_config, TARGET_KEY, [arn_for(session_factory, snapshot, n) for n in names]
        )
    adoption = run_to_end(
        service.create(
            "terraform_adoption", "sandbox", TARGET_KEY, adoption_plan.model_dump(), "tofu-adopt", "phil"
        ).id
    )
    assert adoption.state == OperationState.SUCCEEDED, _diagnostics(adoption)
    workspace_dir = integration_settings.repo_root / WORKSPACE

    # Attach: the instance is rendered with a reference to the adopted group, and tofu applies it.
    orders_arn = arn_for(session_factory, snapshot, "demo-orders")
    attach = run_to_end(
        service.create(
            "parameter_group_attach",
            "sandbox",
            TARGET_KEY,
            {
                "options": {
                    "parameter_group_arn": arn_for(session_factory, snapshot, "orders-pg16-alt"),
                    "reboot": "none",
                },
                "resource_arn": orders_arn,
            },
            "tofu-attach",
            "phil",
            resource_arn=orders_arn,
            title="Attach parameter group: demo-orders",
            approval_required_text="demo-orders",
        ).id
    )
    assert attach.state == OperationState.SUCCEEDED, _diagnostics(attach)
    [orders] = rds.describe_db_instances(DBInstanceIdentifier="demo-orders")["DBInstances"]
    assert [g["DBParameterGroupName"] for g in orders["DBParameterGroups"]] == ["orders-pg16-alt"]
    recorded_orders = next(
        i for i in read_record(workspace_dir).render_input.instances if i.identifier == "demo-orders"
    )
    assert recorded_orders.parameter_group_reference == "aws_db_parameter_group.orders_pg16_alt.name"

    # Delete: tofu destroys the unused group, and nothing else.
    unused_arn = arn_for(session_factory, snapshot, "unused-pg16")
    delete = run_to_end(
        service.create(
            "parameter_group_delete",
            "sandbox",
            TARGET_KEY,
            {"options": {}, "resource_arn": unused_arn},
            "tofu-delete",
            "phil",
            resource_arn=unused_arn,
            title="Delete parameter group: unused-pg16",
            approval_required_text="unused-pg16",
        ).id
    )
    assert delete.state == OperationState.SUCCEEDED, _diagnostics(delete)
    remaining = {g["DBParameterGroupName"] for g in rds.describe_db_parameter_groups()["DBParameterGroups"]}
    assert "unused-pg16" not in remaining and "orders-pg16-alt" in remaining

    # Restore: an outside change to an adopted group is detected, then tofu puts the recorded value back.
    tuned_arn = arn_for(session_factory, snapshot, "demo-postgres16-tuned")
    rds.modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[{"ParameterName": "work_mem", "ParameterValue": "65536", "ApplyMethod": "immediate"}],
    )
    refresh("tofu-refresh-drift")
    detect = run_to_end(
        service.create("drift_detect", "sandbox", None, {"options": {}}, "tofu-detect", "phil").id
    )
    assert detect.state == OperationState.SUCCEEDED, _diagnostics(detect)
    with session_factory() as session:
        assert (
            session.query(DriftFinding).filter_by(resource_arn=tuned_arn, resolved_at_utc=None).count() >= 1
        )
    restore = run_to_end(
        service.create(
            "drift_resolve",
            "sandbox",
            TARGET_KEY,
            {"options": {"resource_arn": tuned_arn, "choice": "restore"}, "resource_arn": tuned_arn},
            "tofu-restore",
            "phil",
            resource_arn=tuned_arn,
            title="Resolve drift: demo-postgres16-tuned",
        ).id
    )
    assert restore.state == OperationState.SUCCEEDED, _diagnostics(restore)
    assert _work_mem(rds, "demo-postgres16-tuned") == "16384"
    assert _git("status", "--porcelain", cwd=integration_settings.repo_root) == ""

    # Create: tofu creates the group. Depending on the AWS provider version the run resolves, the simulator
    # not marking the parameters Terraform set as `Source: user` makes the provider read them back as missing,
    # and then the clean-plan check fails and names them; with a provider that tolerates it, the create
    # succeeds.
    create = run_to_end(
        service.create(
            "parameter_group_create",
            "sandbox",
            TARGET_KEY,
            {
                "options": {
                    "target_key": TARGET_KEY,
                    "name": "orders-pg16-new",
                    "scope": "instance",
                    "family": "postgres16",
                    "description": "created by tofu",
                    "source": "defaults",
                    "clone_from_arn": None,
                    "parameters": {"work_mem": "32768"},
                }
            },
            "tofu-create",
            "phil",
            title="Create parameter group: orders-pg16-new",
        ).id
    )
    if create.state == OperationState.FAILED:
        [residual] = [d for d in create.diagnostic_reports if d.diagnostic_kind == "residual_plan_changes"]
        assert "aws_db_parameter_group.orders_pg16_new: update on parameter" in residual.detail
    else:
        assert create.state == OperationState.SUCCEEDED, _diagnostics(create)
    assert "orders-pg16-new" in {
        g["DBParameterGroupName"] for g in rds.describe_db_parameter_groups()["DBParameterGroups"]
    }


INSTANCE_CREATE_OUTCOME_PREFIX = "instance_create_integration_outcome:"
"""Printed by the instance-create case (run with `-s`) so the verification record can say what happened."""
SIMULATOR_PERFORMANCE_INSIGHTS_RESIDUAL = (
    "aws_db_instance.demo_reports: update on performance_insights_enabled"
)
"""The documented limit: moto accepts `EnablePerformanceInsights` but never reports it, so the provider reads
the created instance back with Performance Insights off and the clean-plan check after the apply names it."""


def test_terraform_mode_create_instance_with_real_opentofu(
    session_factory,
    integration_settings,  # noqa: F811
    console_config,
    config_store,
) -> None:
    """tofu creates an instance from the template with a managed master password, nothing adopted first.

    Every other instance is deleted first: the simulator gives each instance the same `DbiResourceId`, and
    the AWS provider looks a created instance up by it, so the new instance must be the only one. The outcome
    is printed (`instance_create_integration_outcome: ...`) for the verification record: either the create
    succeeded and recorded as built, or tofu created the instance and the clean-plan check after the apply
    named only Performance Insights, which the simulator never reports (on moto 5.2.3 this is what happens).
    """
    _prepare_repo(integration_settings)
    sync_configuration_to_control_db(session_factory, console_config)
    services = build_services(integration_settings, session_factory)
    services.sleep = lambda seconds: None
    target = console_config.target(TARGET_KEY)
    subprocess.run(["curl", "-s", "-X", "POST", f"{ENDPOINT}/moto-api/reset"], check=False)
    rds = services.aws_sessions.client(target, "rds")
    ec2 = services.aws_sessions.client(target, "ec2")
    report = seed_simulated_estate(rds, ec2, load_simulated_estate(Path("/x")))
    for identifier in ("demo-orders-replica", "demo-aurora-reader", "demo-aurora-writer", "demo-orders"):
        rds.delete_db_instance(DBInstanceIdentifier=identifier, SkipFinalSnapshot=True)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        integration_settings,
        config_store,
        services,
    )
    service = OperationService(session_factory)
    service.create("inventory_refresh", "sandbox", None, {}, "tofu-create-refresh", "phil")
    runner.run_once()
    options = {
        "target_key": TARGET_KEY,
        "identifier": "demo-reports",
        "engine_version": "16.4",
        "instance_class": "db.m6g.large",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": [report.security_group_ids["demo-db-access"]],
        "parameter_group_name": "demo-postgres16-tuned",
    }
    operation = service.create(
        "instance_create",
        "sandbox",
        TARGET_KEY,
        {**options, "options": options, "resource_arn": None, "resource_kind": None},
        "tofu-create-instance",
        "phil",
        title="Create instance: demo-reports",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, _diagnostics(awaiting)
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    workspace_dir = integration_settings.repo_root / WORKSPACE
    instances_tf = (workspace_dir / "instances.tf").read_text()
    assert re.search(r"manage_master_user_password\s+=\s+true", instances_tf)
    if final.state == OperationState.SUCCEEDED:
        [created] = rds.describe_db_instances(DBInstanceIdentifier="demo-reports")["DBInstances"]
        assert created["MasterUserSecret"]["SecretArn"]
        assert created["StorageEncrypted"] is True and created["DeletionProtection"] is True
        recorded = next(
            i for i in read_record(workspace_dir).render_input.instances if i.identifier == "demo-reports"
        )
        assert recorded.manage_master_user_password is True
        as_built = next(a for a in final.step_attempts if a.step_name == "record_as_built")
        print(
            f"{INSTANCE_CREATE_OUTCOME_PREFIX} succeeded; tofu created demo-reports with a managed master "
            f"password; record_as_built: {as_built.output}"
        )
        assert _git("status", "--porcelain", cwd=integration_settings.repo_root) == ""
        return
    diagnostics = _diagnostics(final)
    print(f"{INSTANCE_CREATE_OUTCOME_PREFIX} failed after the apply; diagnostics: {diagnostics}")
    assert diagnostics == [("residual_plan_changes", SIMULATOR_PERFORMANCE_INSIGHTS_RESIDUAL)], diagnostics
    [created] = rds.describe_db_instances(DBInstanceIdentifier="demo-reports")["DBInstances"]
    assert created["MasterUserSecret"]["SecretArn"], "tofu asked AWS to keep the master password"
    assert created["MasterUsername"] == "postgres_admin" and created["StorageEncrypted"] is True
    assert not re.search(r"\bpassword\s*=|master_password", instances_tf)
