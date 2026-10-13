from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from structlog.testing import capture_logs

from rdscommander.config.store import StaticConfigStore
from rdscommander.control_db.models import (
    ChangeSet,
    InventorySnapshot,
    Operation,
    OperationStepAttempt,
    ResourceChangeEvent,
    TerraformWorkspace,
)
from rdscommander.main import build_executors, build_services, create_application
from rdscommander.operations import change_strategy as change_strategy_module
from rdscommander.operations.attribute_change import AttributeChangeExecutor
from rdscommander.operations.change_strategy import (
    DIRECT_STEPS,
    TERRAFORM_STEPS,
    ChangeSetEntry,
    ResourceChangeExecutor,
    VerificationResult,
)
from rdscommander.operations.enums import OperationState
from rdscommander.operations.executor import StepContext
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from rdscommander.terraform.record import read_record
from rdscommander.terraform.render_input import RenderWorkspaceInput
from rdscommander.terraform.runner import PlanRunResult
from tests.drift.test_detect_executor import _adopt_estate, _adopt_group
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _git, _prepare_repo

GROUP = "demo-postgres16-tuned"
GROUP_ADDRESS = "aws_db_parameter_group.demo_postgres16_tuned"
WORKSPACE_RELATIVE_DIR = "terraform/sandbox/000000000000/eu-west-2"


class SetWorkMem:
    """Test-only desired change: set work_mem on the demo group."""

    summary = "set work_mem"

    def __init__(self, resource_arn: str, value: str) -> None:
        self.resource_arn = resource_arn
        self.value = value

    def touched_addresses(self, context: StepContext) -> set[str]:
        return {GROUP_ADDRESS}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        updated = render_input.model_copy(deep=True)
        group = next(g for g in updated.instance_parameter_groups if g.name == GROUP)
        next(p for p in group.parameters if p.name == "work_mem").value = self.value
        return updated

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        rds = context.services.aws_sessions.client(
            context.target, "rds", operation_id=context.operation.id, step_name="compute_change_set"
        )
        current = next(
            p["ParameterValue"]
            for p in rds.describe_db_parameters(DBParameterGroupName=GROUP)["Parameters"]
            if p["ParameterName"] == "work_mem"
        )
        return [
            ChangeSetEntry(
                address=GROUP_ADDRESS,
                attribute="parameters.work_mem",
                current=current,
                desired=self.value,
                apply_type="immediate",
                requires_reboot=False,
                downtime="none",
            )
        ]

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        rds = context.services.aws_sessions.client(
            context.target, "rds", operation_id=context.operation.id, step_name="apply_direct"
        )
        rds.modify_db_parameter_group(
            DBParameterGroupName=GROUP,
            Parameters=[
                {"ParameterName": "work_mem", "ParameterValue": self.value, "ApplyMethod": "immediate"}
            ],
        )
        return {"work_mem": self.value}

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        rds = context.services.aws_sessions.client(
            context.target, "rds", operation_id=context.operation.id, step_name="verify_direct"
        )
        live = next(
            p["ParameterValue"]
            for p in rds.describe_db_parameters(DBParameterGroupName=GROUP)["Parameters"]
            if p["ParameterName"] == "work_mem"
        )
        return VerificationResult(ok=live == self.value, live={"work_mem": live}, pending=[], detail="")


class SetWorkMemExecutor(ResourceChangeExecutor):
    operation_kind = "test_set_work_mem"

    def desired_change(self, context: StepContext):  # noqa: ANN201
        return SetWorkMem(context.operation.resource_arn, context.operation.plan["options"]["value"])


def _runner(session_factory, operator_settings, config_store, terraform_runner=None):
    services = build_services(operator_settings, session_factory)
    services.sleep = lambda seconds: None  # no test may really wait for AWS
    if terraform_runner is not None:
        services.terraform_runner = terraform_runner
    executors = build_executors(services, config_store)
    executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    return OperationRunner(
        session_factory, executors, "r", operator_settings, config_store, services
    ), services


def _direct_store(config_store):
    config = config_store.current.model_copy(deep=True)
    config.environment("sandbox").change_management = "direct"
    return StaticConfigStore(config, config_store.session_factory)


def _create_set_work_mem(service, key: str, group_arn: str, value: str = "40000", title: str = "x"):
    return service.create(
        "test_set_work_mem",
        "sandbox",
        TARGET_KEY,
        {"options": {"value": value}, "resource_arn": group_arn},
        key,
        "phil",
        resource_arn=group_arn,
        title=title,
    )


def _diagnostic_kinds(operation) -> list[str]:
    return [d.diagnostic_kind for d in operation.diagnostic_reports]


def _group_update_plan(before: str = "16384", after: str = "40000") -> dict:
    return {
        "resource_changes": [
            {
                "address": GROUP_ADDRESS,
                "type": "aws_db_parameter_group",
                "change": {
                    "actions": ["update"],
                    "before": {"parameter": [{"name": "work_mem", "value": before}]},
                    "after": {"parameter": [{"name": "work_mem", "value": after}]},
                },
            }
        ]
    }


def _workspace_dir(operator_settings):
    return operator_settings.repo_root / WORKSPACE_RELATIVE_DIR


def test_direct_strategy_steps_apply_verify_and_record(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "direct-1", group_arn, title=f"Edit parameter group: {GROUP}")
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL
    assert [a.step_name for a in paused.step_attempts] == DIRECT_STEPS[:4]
    with session_factory() as session:
        [change_set] = session.query(ChangeSet).filter_by(operation_id=operation.id).all()
        assert change_set.entries == [
            {
                "address": GROUP_ADDRESS,
                "attribute": "parameters.work_mem",
                "current": "16384",
                "desired": "40000",
                "apply_type": "immediate",
                "requires_reboot": False,
                "downtime": "none",
            }
        ]
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [a.step_name for a in final.step_attempts if a.outcome_status == "completed"][-4:] == DIRECT_STEPS[
        -4:
    ]
    live = boto3.client("rds", region_name="eu-west-2").describe_db_parameters(DBParameterGroupName=GROUP)[
        "Parameters"
    ]
    assert next(p["ParameterValue"] for p in live if p["ParameterName"] == "work_mem") == "40000"
    with session_factory() as session:
        [change_set] = session.query(ChangeSet).filter_by(operation_id=operation.id).all()
        assert change_set.applied_at_utc is not None and change_set.verification == {"work_mem": "40000"}
    recorded = (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()
    assert "40000" in recorded
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == f"Edit parameter group: {GROUP} (phil, approved by sam)"
    )


def test_direct_strategy_fails_when_verification_disagrees_and_keeps_the_change_set(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "direct-2", group_arn)
    runner.run_once()
    service.approve(operation.id, "sam")
    monkeypatch.setattr(
        SetWorkMem,
        "verify_direct",
        lambda self, context, entries: VerificationResult(
            ok=False, live={"work_mem": "16384"}, pending=[], detail="value did not change"
        ),
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert any(d.diagnostic_kind == "direct_change_not_verified" for d in final.diagnostic_reports)
    with session_factory() as session:
        [change_set] = session.query(ChangeSet).filter_by(operation_id=operation.id).all()
        assert change_set.applied_at_utc is not None and change_set.verification == {"work_mem": "16384"}


def test_direct_strategy_waits_for_an_asynchronous_apply_before_trusting_the_value(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, services = _runner(session_factory, operator_settings, store)
    sleeps: list[float] = []
    services.sleep = sleeps.append
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "direct-async", group_arn)
    reads: list[VerificationResult] = [
        VerificationResult(ok=False, live={"work_mem": "16384"}, pending=[], detail="still applying"),
        VerificationResult(ok=False, live={"work_mem": "16384"}, pending=[], detail="still applying"),
        VerificationResult(ok=True, live={"work_mem": "40000"}, pending=[], detail=""),
    ]
    monkeypatch.setattr(SetWorkMem, "verify_direct", lambda self, context, entries: reads.pop(0))
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert len(sleeps) == 2 and reads == []
    assert sleeps == [operator_settings.direct_verify_poll_seconds] * 2
    with session_factory() as session:
        [change_set] = session.query(ChangeSet).filter_by(operation_id=operation.id).all()
        assert change_set.verification == {"work_mem": "40000"}


def test_direct_strategy_gives_up_waiting_after_the_verification_timeout(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    operator_settings.direct_verify_timeout_seconds = 30
    operator_settings.direct_verify_poll_seconds = 10.0
    store = _direct_store(config_store)
    runner, services = _runner(session_factory, operator_settings, store)
    sleeps: list[float] = []
    services.sleep = sleeps.append
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "direct-timeout", group_arn)
    monkeypatch.setattr(
        SetWorkMem,
        "verify_direct",
        lambda self, context, entries: VerificationResult(
            ok=False, live={"work_mem": "16384"}, pending=[], detail="still applying"
        ),
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert sleeps == [10.0, 10.0, 10.0]
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "direct_change_not_verified"]
    assert "still applying" in diagnostic.detail
    assert "submitted to AWS and may still be applying" in diagnostic.detail
    assert diagnostic.fix_suggestion == (
        "refresh inventory; when the values show, run Detect drift and accept the drift into the record"
    )
    assert final.plan["applied_without_record"] is True


def test_direct_strategy_treats_pending_reboot_attributes_as_applied(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "direct-3", group_arn)
    monkeypatch.setattr(
        SetWorkMem,
        "change_set",
        lambda self, context: [
            ChangeSetEntry(
                address=GROUP_ADDRESS,
                attribute="parameters.work_mem",
                current="16384",
                desired="40000",
                apply_type="pending-reboot",
                requires_reboot=True,
                downtime="a reboot of every instance using the group",
            )
        ],
    )
    monkeypatch.setattr(
        SetWorkMem,
        "verify_direct",
        lambda self, context, entries: VerificationResult(
            ok=False, live={"work_mem": "16384"}, pending=["parameters.work_mem"], detail="pending reboot"
        ),
    )
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.plan["requires_reboot"] is True
    assert paused.plan["downtime"] == "a reboot of every instance using the group"
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert final.plan["pending_reboot_attributes"] == ["parameters.work_mem"]


def test_direct_strategy_reports_nothing_to_change_for_an_empty_change_set(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "direct-empty", group_arn)
    monkeypatch.setattr(SetWorkMem, "change_set", lambda self, context: [])
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert "nothing_to_change" in _diagnostic_kinds(final)


@pytest.mark.parametrize("error_type", [KeyError, ValueError])
def test_direct_strategy_turns_an_impossible_change_into_a_diagnostic(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
    monkeypatch,
    error_type,
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, f"direct-impossible-{error_type.__name__}", group_arn)

    def impossible(self, context):  # noqa: ANN001, ANN202
        raise error_type("work_mem is not a parameter of this family")

    monkeypatch.setattr(SetWorkMem, "change_set", impossible)
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "change_not_possible"]
    assert "work_mem is not a parameter of this family" in diagnostic.detail
    assert "work_mem is not a parameter of this family" in diagnostic.fix_suggestion


def test_direct_strategy_refuses_a_change_set_outside_the_touched_addresses(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "direct-outside", group_arn)
    monkeypatch.setattr(
        SetWorkMem,
        "change_set",
        lambda self, context: [
            ChangeSetEntry(
                address="aws_db_instance.demo_orders",
                attribute="instance_class",
                current="db.t3.micro",
                desired="db.t3.large",
                apply_type="immediate",
                requires_reboot=False,
                downtime="none",
            )
        ],
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "policy_refused"]
    assert "aws_db_instance.demo_orders" in diagnostic.detail and diagnostic.fix_suggestion


def test_direct_strategy_replaces_only_the_touched_resource_in_the_record(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
    direct_mode_runner,
) -> None:
    runner, service, _ = direct_mode_runner
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(runner.config_store)
    runner.services.sleep = lambda seconds: None
    workspace_dir = _workspace_dir(operator_settings)
    recorded_before = read_record(workspace_dir).render_input
    orders_before = next(i for i in recorded_before.instances if i.identifier == "demo-orders")
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders",
        BackupRetentionPeriod=orders_before.backup_retention_period + 3,
        ApplyImmediately=True,
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    operation = _create_set_work_mem(service, "direct-record", group_arn)
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    assert read_record(workspace_dir).render_input == recorded_before, "nothing is recorded before the apply"
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    recorded_after = read_record(workspace_dir).render_input
    group_after = next(g for g in recorded_after.instance_parameter_groups if g.name == GROUP)
    assert next(p.value for p in group_after.parameters if p.name == "work_mem") == "40000"
    orders_after = next(i for i in recorded_after.instances if i.identifier == "demo-orders")
    assert orders_after == orders_before, "the drift of an untouched resource stays visible"
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def test_terraform_strategy_renders_plans_applies_and_commits(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    update_plan = _group_update_plan()
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update_plan, update_plan, {"resource_changes": []}]
    )
    runner.services.terraform_runner = fake
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, "tf-1", group_arn, title=f"Edit parameter group: {GROUP}")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    assert [a.step_name for a in service.get(operation.id).step_attempts] == TERRAFORM_STEPS[
        : TERRAFORM_STEPS.index("await_approval") + 1
    ]
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert fake.applied_plan_files == ["console.tfplan"]
    assert "40000" in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == f"Edit parameter group: {GROUP} (phil, approved by sam)"
    )


def test_terraform_strategy_creates_the_state_bucket_before_init_on_a_fresh_workspace(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """A Terraform-mode change in an environment where nothing was adopted yet has no state bucket.

    `init` configures the S3 backend, so the strategy must create the bucket first, as the adoption does.
    """
    _prepare_repo(operator_settings)
    runner, services = _runner(session_factory, operator_settings, config_store)
    created = {
        "resource_changes": [
            {
                "address": "aws_db_parameter_group.orders_pg16_v2",
                "type": "aws_db_parameter_group",
                "change": {"actions": ["create"], "before": None, "after": {"name": "orders-pg16-v2"}},
            }
        ]
    }
    services.terraform_runner = FakeTerraformRunner(services.execution_recorder, plan_scripts=[created])
    service = OperationService(session_factory)
    operation = service.create(
        "parameter_group_create",
        "sandbox",
        TARGET_KEY,
        {
            "options": {
                "target_key": TARGET_KEY,
                "name": "orders-pg16-v2",
                "scope": "instance",
                "family": "postgres16",
                "description": "orders-pg16-v2 description",
                "source": "defaults",
                "clone_from_arn": None,
                "parameters": {},
            }
        },
        "tf-fresh-bucket",
        "phil",
        title="Create parameter group: orders-pg16-v2",
    )
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    steps = [attempt.step_name for attempt in paused.step_attempts]
    assert steps.index("ensure_state_bucket") < steps.index("init")
    with session_factory() as session:
        bucket_name = session.query(TerraformWorkspace).one().state_bucket_name
    boto3.client("s3", region_name="eu-west-2").head_bucket(Bucket=bucket_name)


def _run_terraform_set_work_mem(
    session_factory, operator_settings, config_store, console_config, snapshot, key
):
    """Adopt the demo group, then run a Terraform-mode work_mem change to its end; return it."""
    runner, service = _adopt_group(session_factory, operator_settings, config_store, console_config, snapshot)
    update_plan = _group_update_plan()
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update_plan, update_plan, {"resource_changes": []}]
    )
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, key, arn_for(session_factory, snapshot, GROUP))
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    return service.get(operation.id)


def test_terraform_strategy_takes_a_fresh_inventory_snapshot_after_it_commits(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """Resource pages, ownership and history show the change as soon as the operation finishes."""
    final = _run_terraform_set_work_mem(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "tf-refresh"
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    completed = [a.step_name for a in final.step_attempts if a.outcome_status == "completed"]
    assert completed[-2:] == ["commit_to_checkout", "refresh_resource"]
    with session_factory() as session:
        snapshots = session.query(InventorySnapshot).filter_by(operation_id=final.id).all()
    [refresh] = [a for a in final.step_attempts if a.step_name == "refresh_resource"]
    assert [snapshot.id for snapshot in snapshots] == [refresh.output["snapshot_id"]]


def test_a_failed_refresh_after_a_terraform_commit_does_not_fail_the_committed_change(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    def unreachable(context, target, step_name=None):  # noqa: ANN001, ANN202
        raise RuntimeError("the RDS endpoint could not be reached")

    monkeypatch.setattr(change_strategy_module, "refresh_target_now", unreachable)
    with capture_logs() as logs:
        final = _run_terraform_set_work_mem(
            session_factory,
            operator_settings,
            config_store,
            console_config,
            sandbox_snapshot,
            "tf-no-refresh",
        )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root) == "x (phil, approved by sam)"
    [refresh] = [a for a in final.step_attempts if a.step_name == "refresh_resource"]
    assert refresh.outcome_status == "completed"
    assert refresh.output["refreshed"] is False
    assert "the RDS endpoint could not be reached" in refresh.output["error"]
    assert "Refresh inventory" in refresh.detail
    [warning] = [entry for entry in logs if entry["event"] == "terraform_change_refresh_failed"]
    assert warning["log_level"] == "warning"
    assert "Refresh inventory" in warning["fix_suggestion"]
    assert warning["operation_id"] == str(final.id)


def test_a_failed_refresh_in_direct_mode_still_fails_the_change(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    """Direct mode records after the refresh, so a refresh it cannot take stops the change as before."""
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))

    def unreachable(context, target, step_name=None):  # noqa: ANN001, ANN202
        raise RuntimeError("the RDS endpoint could not be reached")

    monkeypatch.setattr(change_strategy_module, "refresh_target_now", unreachable)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(
        service, "direct-no-refresh", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert any(
        attempt.step_name == "refresh_resource" and attempt.outcome_status == "failed"
        for attempt in final.step_attempts
    )
    assert [d.step_name for d in final.diagnostic_reports] == ["refresh_resource"]


def test_a_failed_refresh_after_a_verified_direct_change_says_the_change_is_in_aws_and_how_to_record_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))

    def unreachable(context, target, step_name=None):  # noqa: ANN001, ANN202
        raise RuntimeError("the RDS endpoint could not be reached")

    monkeypatch.setattr(change_strategy_module, "refresh_target_now", unreachable)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(
        service, "direct-no-refresh-advice", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert final.plan.get("applied_without_record") is True
    [diagnostic] = [d for d in final.diagnostic_reports if d.step_name == "refresh_resource"]
    assert diagnostic.diagnostic_kind == "refresh_after_change_failed"
    assert "the RDS endpoint could not be reached" in diagnostic.detail
    assert "Refresh inventory" in diagnostic.fix_suggestion and "Detect drift" in diagnostic.fix_suggestion
    assert "create a new operation" not in diagnostic.fix_suggestion


def test_terraform_strategy_writes_the_record_only_when_it_commits(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    update_plan = _group_update_plan()
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update_plan, update_plan, {"resource_changes": []}]
    )
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    workspace_dir = _workspace_dir(operator_settings)
    recorded_before = read_record(workspace_dir)
    operation = _create_set_work_mem(service, "tf-record", group_arn)
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    modified_while_waiting = _git("diff", "--name-only", "--", "terraform", cwd=operator_settings.repo_root)
    assert modified_while_waiting.splitlines() == [f"{WORKSPACE_RELATIVE_DIR}/parameter_groups.tf"]
    untracked_while_waiting = _git(
        "ls-files", "--others", "--exclude-standard", "--", "terraform", cwd=operator_settings.repo_root
    )
    assert untracked_while_waiting == ""
    assert read_record(workspace_dir).render_input == recorded_before.render_input
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.SUCCEEDED
    recorded_after = read_record(workspace_dir)
    group = recorded_after.render_input.instance_parameter_groups[0]
    assert next(p.value for p in group.parameters if p.name == "work_mem") == "40000"
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def test_terraform_strategy_replaces_only_the_touched_resource_in_files_and_record(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    workspace_dir = _workspace_dir(operator_settings)
    recorded_before = read_record(workspace_dir).render_input
    orders_before = next(i for i in recorded_before.instances if i.identifier == "demo-orders")
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders",
        BackupRetentionPeriod=orders_before.backup_retention_period + 3,
        ApplyImmediately=True,
    )
    service.create("inventory_refresh", "sandbox", None, {}, "tf-touched-refresh", "phil")
    runner.run_once()
    update_plan = _group_update_plan()
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update_plan, update_plan, {"resource_changes": []}]
    )
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    instances_file_before = (workspace_dir / "instances.tf").read_text()
    operation = _create_set_work_mem(service, "tf-touched", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.SUCCEEDED
    recorded_after = read_record(workspace_dir).render_input
    assert next(i for i in recorded_after.instances if i.identifier == "demo-orders") == orders_before
    group = next(g for g in recorded_after.instance_parameter_groups if g.name == GROUP)
    assert next(p.value for p in group.parameters if p.name == "work_mem") == "40000"
    assert (workspace_dir / "instances.tf").read_text() == instances_file_before


def test_terraform_strategy_refuses_a_plan_that_touches_other_resources(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    bad_plan = {
        "resource_changes": [
            {
                "address": "aws_db_instance.demo_orders",
                "type": "aws_db_instance",
                "change": {"actions": ["delete"], "before": {}, "after": None},
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[bad_plan]
    )
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, "tf-2", group_arn)
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [refusal] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "policy_refused"]
    assert "The workspace files were reverted to the last commit." in refusal.detail
    assert _git("status", "--porcelain", "--", "terraform", cwd=operator_settings.repo_root) == ""


def test_terraform_strategy_names_untouched_addresses_brought_up_to_date_on_the_policy_check(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """A workspace rendered by 0.2.0 gains `apply_immediately` everywhere; that update passes and is named."""
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    upgrade_plan = _group_update_plan()
    upgrade_plan["resource_changes"].append(
        {
            "address": "aws_db_instance.demo_orders",
            "type": "aws_db_instance",
            "change": {
                "actions": ["update"],
                "before": {"apply_immediately": None},
                "after": {"apply_immediately": True},
            },
        }
    )
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[upgrade_plan]
    )
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, "tf-upgrade", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    [policy_check] = [a for a in paused.step_attempts if a.step_name == "policy_check"]
    assert policy_check.detail == "Also brought up to date: aws_db_instance.demo_orders"


def test_terraform_strategy_refuses_to_apply_a_plan_that_changed_since_approval(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    fake = FakeTerraformRunner(
        runner.services.execution_recorder,
        plan_scripts=[_group_update_plan(), _group_update_plan(after="99999")],
    )
    runner.services.terraform_runner = fake
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, "tf-changed", group_arn)
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert "plan_changed_since_approval" in _diagnostic_kinds(final)
    assert fake.applied_plan_files == []
    assert _git("status", "--porcelain", "--", "terraform", cwd=operator_settings.repo_root) == ""


def test_terraform_strategy_turns_an_impossible_change_into_a_diagnostic(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)

    def impossible(self, render_input):  # noqa: ANN001, ANN202
        raise KeyError("work_mem")

    monkeypatch.setattr(SetWorkMem, "apply_to_render_input", impossible)
    operation = _create_set_work_mem(service, "tf-impossible", group_arn)
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert "change_not_possible" in _diagnostic_kinds(final)


def test_terraform_mode_requires_adoption_first(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, config_store)
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "tf-3", group_arn)
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert any(
        d.diagnostic_kind == "not_adopted" and "adopt" in d.fix_suggestion for d in final.diagnostic_reports
    )


def test_foreign_owned_resources_are_refused_unless_the_environment_allows_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    boto3.client("rds", region_name="eu-west-2").add_tags_to_resource(
        ResourceName=arn_for(session_factory, sandbox_snapshot, GROUP),
        Tags=[{"Key": "ManagedBy", "Value": "terraform"}],
    )
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    service = OperationService(session_factory)
    service.create("inventory_refresh", "sandbox", None, {}, "foreign-refresh", "phil")
    runner.run_once()
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    operation = _create_set_work_mem(service, "foreign-1", group_arn)
    runner.run_once()
    assert any(
        d.diagnostic_kind == "foreign_owned_resource" for d in service.get(operation.id).diagnostic_reports
    )


def test_foreign_owned_resources_proceed_with_a_banner_when_the_environment_allows_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    boto3.client("rds", region_name="eu-west-2").add_tags_to_resource(
        ResourceName=arn_for(session_factory, sandbox_snapshot, GROUP),
        Tags=[{"Key": "ManagedBy", "Value": "terraform"}],
    )
    store = _direct_store(config_store)
    store.current.environment("sandbox").allow_direct_changes_to_foreign_owned = True
    runner, _ = _runner(session_factory, operator_settings, store)
    service = OperationService(session_factory)
    service.create("inventory_refresh", "sandbox", None, {}, "banner-refresh", "phil")
    runner.run_once()
    operation = _create_set_work_mem(service, "banner-1", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["foreign_owner_banner"] == "terraform"
    assert paused.plan["touched_addresses"] == [GROUP_ADDRESS]


def test_a_resource_missing_from_the_inventory_is_refused(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create_set_work_mem(
        service, "missing-1", "arn:aws:rds:eu-west-2:000000000000:pg:no-such-group"
    )
    runner.run_once()
    assert "resource_not_in_inventory" in _diagnostic_kinds(service.get(operation.id))


def test_the_step_lists_are_the_strategy_contract_and_both_pause_for_approval() -> None:
    assert TERRAFORM_STEPS == [
        "check_checkout",
        "ensure_state_bucket",
        "render_desired",
        "init",
        "plan",
        "policy_check",
        "await_approval",
        "apply",
        "verify_clean_plan",
        "commit_to_checkout",
        "refresh_resource",
    ]
    assert DIRECT_STEPS == [
        "check_checkout",
        "compute_change_set",
        "policy_check",
        "await_approval",
        "apply_direct",
        "verify_direct",
        "refresh_resource",
        "record_to_checkout",
    ]


def _workspace_changes(operator_settings) -> str:
    return _git("status", "--porcelain", "--", "terraform", cwd=operator_settings.repo_root)


def _failing_result(fake: FakeTerraformRunner, exit_code: int = 1, standard_error: str = "boom"):
    return replace(fake._ok(), exit_code=exit_code, standard_error=standard_error)


def _break_init(fake: FakeTerraformRunner) -> str:
    fake.init = lambda *args, **kwargs: _failing_result(fake, standard_error="Error: no network")
    return "terraform_init_failed"


def _break_plan(fake: FakeTerraformRunner) -> str:
    fake.plan = lambda *args, **kwargs: PlanRunResult(
        result=_failing_result(fake, standard_error="Error: expired credentials"),
        has_changes=False,
        plan_json={},
    )
    return "terraform_plan_failed"


@pytest.mark.parametrize("break_step", [_break_init, _break_plan])
def test_a_terraform_failure_before_the_apply_leaves_the_workspace_as_the_last_commit_has_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, break_step
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    fake = FakeTerraformRunner(runner.services.execution_recorder, plan_scripts=[_group_update_plan()])
    expected_kind = break_step(fake)
    runner.services.terraform_runner = fake
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    head_before = _git("rev-parse", "HEAD", cwd=operator_settings.repo_root)
    operation = _create_set_work_mem(
        service, f"tf-revert-{expected_kind}", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == expected_kind]
    assert "The workspace files were reverted to the last commit." in diagnostic.detail
    assert _workspace_changes(operator_settings) == ""
    assert _git("rev-parse", "HEAD", cwd=operator_settings.repo_root) == head_before


def test_an_unexpected_exception_before_the_apply_also_reverts_the_workspace(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    fake = FakeTerraformRunner(runner.services.execution_recorder)

    def explode(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("the terraform binary vanished")

    fake.plan = explode
    runner.services.terraform_runner = fake
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, "tf-explode", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    assert service.get(operation.id).state == OperationState.FAILED
    assert _workspace_changes(operator_settings) == ""


def test_a_failed_apply_keeps_the_rendered_files_because_aws_may_already_have_changed(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    update_plan = _group_update_plan()
    fake = FakeTerraformRunner(runner.services.execution_recorder, plan_scripts=[update_plan, update_plan])
    fake.apply = lambda *args, **kwargs: _failing_result(fake, standard_error="Error: AWS rejected it")
    runner.services.terraform_runner = fake
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(
        service, "tf-apply-fails", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert "terraform_apply_failed" in _diagnostic_kinds(final)
    assert "40000" in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "terraform_apply_failed"]
    _assert_says_how_to_unblock_the_workspace(diagnostic.fix_suggestion)


def _assert_says_how_to_unblock_the_workspace(fix_suggestion: str) -> None:
    """The files stay after an apply; the advice must get the operator past the checkout guard first."""
    assert f"git add {WORKSPACE_RELATIVE_DIR}" in fix_suggestion
    assert "git commit" in fix_suggestion
    assert "refuses" in fix_suggestion
    assert fix_suggestion.index("git commit") < fix_suggestion.index("Detect drift")


def _set_work_mem_in_aws(value: str) -> None:
    """Change the demo group's work_mem in the simulator, as an outside change or an apply does."""
    boto3.client("rds", region_name="eu-west-2").modify_db_parameter_group(
        DBParameterGroupName=GROUP,
        Parameters=[{"ParameterName": "work_mem", "ParameterValue": value, "ApplyMethod": "immediate"}],
    )


def _refresh_inventory(runner, service, key: str) -> None:
    """Run one inventory refresh to completion."""
    service.create("inventory_refresh", "sandbox", None, {}, key, "phil")
    runner.run_once()


def _work_mem_change_event_after_a_refresh(runner, service, session_factory, group_arn: str, key: str):
    """Refresh the inventory, then return the one change event it recorded for the group's work_mem."""
    _refresh_inventory(runner, service, key)
    with session_factory() as session:
        [event] = (
            session.query(ResourceChangeEvent)
            .filter_by(resource_arn=group_arn, attribute_path="parameter.work_mem")
            .all()
        )
        session.expunge(event)
        return event


def _plan_that_changed_since_approval(fake: FakeTerraformRunner) -> str:
    fake.plan_scripts.append(_group_update_plan(after="99999"))
    return "plan_changed_since_approval"


@pytest.mark.parametrize("refuse_at_the_pre_apply_plan", [_plan_that_changed_since_approval, _break_plan])
def test_a_terraform_change_refused_at_its_pre_apply_plan_does_not_explain_the_outside_change_behind_it(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
    refuse_at_the_pre_apply_plan,
) -> None:
    """The refusal comes before Terraform's apply runs, so the change behind it stays an outside change."""
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _refresh_inventory(runner, service, "refresh-after-the-adoption")  # the window starts after the adoption
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    fake = FakeTerraformRunner(runner.services.execution_recorder, plan_scripts=[_group_update_plan()])
    runner.services.terraform_runner = fake
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, "tf-refused-at-the-pre-apply-plan", group_arn)
    runner.run_once()
    _set_work_mem_in_aws("65536")  # someone changes the group while the change waits for approval
    expected_kind = refuse_at_the_pre_apply_plan(fake)
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED and expected_kind in _diagnostic_kinds(final)
    assert fake.applied_plan_files == []
    event = _work_mem_change_event_after_a_refresh(
        runner, service, session_factory, group_arn, "refresh-after-the-refusal"
    )
    assert (event.value_after, event.explanation, event.operation_id) == ("65536", "outside_console", None)


@pytest.mark.parametrize("apply_ends", ["with_a_failure", "with_an_exception"])
def test_a_terraform_apply_that_failed_part_way_still_explains_the_change_it_made(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, apply_ends
) -> None:
    """Once the binary's apply runs, AWS may have changed, even when the console never hears how it ended."""
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _refresh_inventory(runner, service, "refresh-after-the-adoption")  # the window starts after the adoption
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    update_plan = _group_update_plan()
    fake = FakeTerraformRunner(runner.services.execution_recorder, plan_scripts=[update_plan, update_plan])

    def apply_that_changes_aws_then_fails(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        _set_work_mem_in_aws("40000")
        if apply_ends == "with_an_exception":
            raise RuntimeError("the console lost the terraform process half-way through apply")
        return _failing_result(fake, standard_error="Error: AWS rejected part of the change")

    fake.apply = apply_that_changes_aws_then_fails
    runner.services.terraform_runner = fake
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, f"tf-apply-part-way-{apply_ends}", group_arn)
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.FAILED
    event = _work_mem_change_event_after_a_refresh(
        runner, service, session_factory, group_arn, f"refresh-after-the-apply-{apply_ends}"
    )
    assert (event.value_after, event.explanation, event.operation_id) == ("40000", "operation", operation.id)


def _run_terraform_work_mem_edit(
    runner, service, config_store, group_arn: str, key: str, value_before: str, value_after: str
):
    """Run a Terraform-mode work_mem edit of the demo group to its end; its apply changes AWS."""
    update_plan = _group_update_plan(before=value_before, after=value_after)
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update_plan, update_plan, {"resource_changes": []}]
    )
    scripted_apply = fake.apply

    def apply_that_changes_aws(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        _set_work_mem_in_aws(value_after)
        return scripted_apply(*args, **kwargs)

    fake.apply = apply_that_changes_aws
    runner.services.terraform_runner = fake
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, key, group_arn, value=value_after)
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    return final


def _adoption_id(session_factory):
    """Id of the one adoption the test ran."""
    with session_factory() as session:
        return session.query(Operation).filter_by(operation_kind="terraform_adoption").one().id


def _assert_the_closing_snapshot_was_captured_before_the_operation_finished(
    session_factory, operation_id
) -> None:
    """The smoke's timing: the closing refresh runs inside the operation, so its capture precedes the finish.

    The operation's lifetime therefore overlaps the window that starts at its closing snapshot, which is
    what let it take the credit for the next change.
    """
    with session_factory() as session:
        [closing_snapshot_id] = [
            attempt.output["snapshot_id"]
            for attempt in session.query(OperationStepAttempt).filter_by(
                operation_id=operation_id, step_name="refresh_resource"
            )
            if attempt.output.get("snapshot_id") is not None
        ]
        closing_snapshot = session.get(InventorySnapshot, closing_snapshot_id)
        finished_at_utc = session.get(Operation, operation_id).finished_at_utc
        assert finished_at_utc is not None
        assert closing_snapshot.captured_at_utc < finished_at_utc, "the smoke's timing is not reproduced"


def _work_mem_change_events(session_factory, group_arn: str) -> list[tuple]:
    """`(before, after, explanation, operation_id)` of each work_mem event of the group, oldest first."""
    with session_factory() as session:
        return [
            (event.value_before, event.value_after, event.explanation, event.operation_id)
            for event in session.query(ResourceChangeEvent)
            .filter_by(resource_arn=group_arn, attribute_path="parameter.work_mem")
            .order_by(ResourceChangeEvent.id)
        ]


def test_two_consecutive_terraform_edits_of_a_group_each_explain_their_own_change(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """The operation before an edit never takes the credit for it (smoke item 3).

    Each closing refresh captures its snapshot before its operation finishes, so the finished operation's
    lifetime overlaps the next window, where it used to win over the running edit that made the change.
    """
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _assert_the_closing_snapshot_was_captured_before_the_operation_finished(
        session_factory, _adoption_id(session_factory)
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    first_edit = _run_terraform_work_mem_edit(
        runner, service, config_store, group_arn, "tf-first-edit", "16384", "65536"
    )
    _assert_the_closing_snapshot_was_captured_before_the_operation_finished(session_factory, first_edit.id)
    second_edit = _run_terraform_work_mem_edit(
        runner, service, config_store, group_arn, "tf-second-edit", "65536", "32768"
    )
    assert _work_mem_change_events(session_factory, group_arn) == [
        ("16384", "65536", "operation", first_edit.id),
        ("65536", "32768", "operation", second_edit.id),
    ]


@pytest.mark.parametrize("closing_operation", ["terraform_edit", "direct_edit", "adoption"])
def test_an_outside_change_right_after_an_operations_closing_refresh_is_outside_the_console_and_in_the_inbox(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
    closing_operation,
) -> None:
    """A change first seen after an operation's closing snapshot is never that operation's (smoke item 6)."""
    group_arn = arn_for(session_factory, sandbox_snapshot, GROUP)
    inbox_config = console_config
    if closing_operation == "direct_edit":
        _prepare_repo(operator_settings)
        direct_store = _direct_store(config_store)
        inbox_config = direct_store.current
        runner, _ = _runner(session_factory, operator_settings, direct_store)
        service = OperationService(session_factory)
        direct_edit = _create_set_work_mem(service, "direct-before-the-outside-change", group_arn)
        runner.run_once()
        service.approve(direct_edit.id, "sam")
        runner.run_once()
        assert service.get(direct_edit.id).state == OperationState.SUCCEEDED
        closing_operation_id, value_before = direct_edit.id, "40000"
    else:
        runner, service = _adopt_group(
            session_factory, operator_settings, config_store, console_config, sandbox_snapshot
        )
        closing_operation_id, value_before = _adoption_id(session_factory), "16384"
        if closing_operation == "terraform_edit":
            terraform_edit = _run_terraform_work_mem_edit(
                runner, service, config_store, group_arn, "tf-before-the-outside-change", "16384", "40000"
            )
            closing_operation_id, value_before = terraform_edit.id, "40000"
    _assert_the_closing_snapshot_was_captured_before_the_operation_finished(
        session_factory, closing_operation_id
    )
    _set_work_mem_in_aws("65536")  # someone changes the group in AWS once the operation has refreshed
    _refresh_inventory(runner, service, "refresh-after-the-outside-change")
    assert _work_mem_change_events(session_factory, group_arn)[-1] == (
        value_before,
        "65536",
        "outside_console",
        None,
    )
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=inbox_config, session_factory=session_factory
        )
    )
    inbox = client.get("/api/v1/attention", params={"environment_slug": "sandbox"}).json()
    [outside_change] = [
        item for item in inbox["outside_changes"] if item["title"] == f"{GROUP}: parameter.work_mem"
    ]
    assert outside_change["detail"] == f"'{value_before}' became '65536'"


def test_a_plan_that_is_not_clean_after_apply_says_how_to_unblock_the_workspace(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    update_plan = _group_update_plan()
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update_plan, update_plan, update_plan]
    )
    scripted_plan = fake.plan

    def plan_that_still_has_changes_after_apply(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        run = scripted_plan(*args, **kwargs)
        if kwargs.get("step_name") != "verify_clean_plan":
            return run
        return replace(run, result=replace(run.result, exit_code=2))  # -detailed-exitcode: changes

    fake.plan = plan_that_still_has_changes_after_apply
    runner.services.terraform_runner = fake
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(
        service, "tf-not-clean", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "residual_plan_changes"]
    assert "40000" in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()
    _assert_says_how_to_unblock_the_workspace(diagnostic.fix_suggestion)


def test_rejecting_a_waiting_terraform_change_cleans_the_workspace_once_the_runner_has_run(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    update_plan = _group_update_plan()
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update_plan]
    )
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, "tf-reject", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    assert _workspace_changes(operator_settings) != "", "the rendered files wait in the workspace"
    service.reject(operation.id, "sam")
    assert _workspace_changes(operator_settings) != "", "rejecting only records the intent to clean up"
    runner.run_once()
    assert _workspace_changes(operator_settings) == ""
    rejected = service.get(operation.id)
    assert rejected.state == OperationState.CANCELLED
    assert "cleanup_pending" not in rejected.plan and rejected.plan["cleanup_done_at_utc"]


def test_a_rejected_change_is_cleaned_up_by_the_console_that_holds_its_files(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[_group_update_plan()]
    )
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(
        service, "tf-reject-elsewhere", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    runner.run_once()
    service.reject(operation.id, "sam")
    other_checkout = operator_settings.repo_root.parent / "other-console-checkout"
    _git("clone", "-q", str(operator_settings.repo_root), str(other_checkout), cwd=other_checkout.parent)
    other_console = OperationRunner(
        session_factory,
        runner.executors,
        "another-machine",
        operator_settings.model_copy(update={"repo_root": other_checkout}),
        config_store,
        runner.services,
    )
    other_console.run_once()
    assert _workspace_changes(operator_settings) != "", "another console's checkout is not this one"
    assert service.get(operation.id).plan["cleanup_pending"] is True
    runner.run_once()
    assert _workspace_changes(operator_settings) == ""
    assert "cleanup_pending" not in service.get(operation.id).plan


def _wait_for_approval_of_a_terraform_change(
    session_factory, operator_settings, config_store, console_config, snapshot, key: str
):
    """Adopt the demo group and run a Terraform-mode work_mem change up to its approval pause."""
    runner, service = _adopt_group(session_factory, operator_settings, config_store, console_config, snapshot)
    update_plan = _group_update_plan()
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update_plan, update_plan, {"resource_changes": []}]
    )
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(
        service, key, arn_for(session_factory, snapshot, GROUP), title=f"Edit parameter group: {GROUP}"
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    assert _workspace_changes(operator_settings) != "", "the rendered files wait in the workspace"
    return runner, service, operation


def _recreated_console(runner: OperationRunner, session_factory, operator_settings, config_store):
    """The same checkout under a new runner identity, as after `console down` and `console up`."""
    return OperationRunner(
        session_factory,
        runner.executors,
        "0f3c9a2b1d4e:phil",
        operator_settings,
        config_store,
        runner.services,
    )


def test_a_waiting_terraform_change_is_approved_and_applied_after_the_console_identity_changed(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """A re-created container finds the operation's own rendered files and goes on with the apply."""
    runner, service, operation = _wait_for_approval_of_a_terraform_change(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "tf-recreated"
    )
    service.approve(operation.id, "sam")
    _recreated_console(runner, session_factory, operator_settings, config_store).run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [a.step_name for a in final.step_attempts].count("check_checkout") == 2, "the guard ran again"
    assert "40000" in (_workspace_dir(operator_settings) / "parameter_groups.tf").read_text()
    assert _workspace_changes(operator_settings) == ""


def test_a_waiting_terraform_change_rejected_after_the_console_identity_changed_is_cleaned_up(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, operation = _wait_for_approval_of_a_terraform_change(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "tf-recreated-no"
    )
    service.reject(operation.id, "sam")
    _recreated_console(runner, session_factory, operator_settings, config_store).run_once()
    assert _workspace_changes(operator_settings) == ""
    rejected = service.get(operation.id)
    assert "cleanup_pending" not in rejected.plan and rejected.plan["cleanup_done_at_utc"]


def test_a_waiting_terraform_change_whose_files_were_edited_by_hand_is_not_applied_by_a_new_identity(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """Only the files the operation wrote count as its own; a hand edit since then stops the guard."""
    runner, service, operation = _wait_for_approval_of_a_terraform_change(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "tf-hand-edit"
    )
    rendered = _workspace_dir(operator_settings) / "parameter_groups.tf"
    rendered.write_text(rendered.read_text() + "# edited by hand\n")
    service.approve(operation.id, "sam")
    _recreated_console(runner, session_factory, operator_settings, config_store).run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert "checkout_guard" in _diagnostic_kinds(final)


def test_a_change_waiting_in_one_workspace_does_not_block_an_operation_in_another(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    other_workspace = operator_settings.repo_root / "terraform" / "production" / "123456789012" / "eu-west-2"
    other_workspace.mkdir(parents=True)
    (other_workspace / "parameter_groups.tf").write_text("# rendered, waiting for approval elsewhere\n")
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "sibling-1", arn_for(session_factory, sandbox_snapshot, GROUP))
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]


def _age_every_snapshot(session_factory) -> None:
    with session_factory() as session:
        for snapshot in session.query(InventorySnapshot):
            snapshot.captured_at_utc = datetime.now(UTC) - timedelta(days=3)
        session.commit()


def test_a_direct_change_is_refused_from_a_stale_inventory(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create_set_work_mem(
        service, "stale-direct", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    _age_every_snapshot(session_factory)
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "stale_inventory"]
    assert "limit is 1440 minutes" in diagnostic.detail
    assert (
        diagnostic.fix_suggestion == "refresh inventory for this environment, then create the operation again"
    )
    with session_factory() as session:
        assert session.query(ChangeSet).count() == 0


def test_a_terraform_change_is_refused_from_a_stale_inventory_and_writes_nothing(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(service, "stale-tf", arn_for(session_factory, sandbox_snapshot, GROUP))
    _age_every_snapshot(session_factory)
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert "stale_inventory" in _diagnostic_kinds(final)
    assert _workspace_changes(operator_settings) == ""


class FlakyDesiredChangeExecutor(SetWorkMemExecutor):
    """Fails to build its desired change on the Nth call, as if the plan stopped making sense."""

    fail_on_call = 0

    def __init__(self, config_store, fail_on_call: int) -> None:
        super().__init__(config_store)
        self.fail_on_call = fail_on_call
        self.calls = 0

    def desired_change(self, context: StepContext):  # noqa: ANN201
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise ValueError("the parameter group was removed from the plan")
        return super().desired_change(context)


@pytest.mark.parametrize(("fail_on_call", "failing_step"), [(2, "apply_direct"), (3, "verify_direct")])
def test_an_impossible_desired_change_after_approval_is_a_diagnostic_too(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
    fail_on_call,
    failing_step,
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    runner.executors["test_set_work_mem"] = FlakyDesiredChangeExecutor(store, fail_on_call)
    service = OperationService(session_factory)
    operation = _create_set_work_mem(
        service, f"flaky-{failing_step}", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "change_not_possible"]
    assert diagnostic.step_name == failing_step
    assert "removed from the plan" in diagnostic.detail
    assert final.plan.get("applied_without_record", False) is (failing_step == "verify_direct")


def test_verification_stops_at_a_terminal_status_without_waiting(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    runner, services = _runner(session_factory, operator_settings, _direct_store(config_store))
    sleeps: list[float] = []
    services.sleep = sleeps.append
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "terminal-1", arn_for(session_factory, sandbox_snapshot, GROUP))
    reads = []

    def terminal(self, context, entries):  # noqa: ANN001, ANN202
        reads.append(1)
        return VerificationResult(
            ok=False,
            live={"work_mem": "16384"},
            pending=[],
            detail="AWS reports the resource as storage-full",
            terminal=True,
        )

    monkeypatch.setattr(SetWorkMem, "verify_direct", terminal)
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert sleeps == [] and len(reads) == 1
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "direct_change_not_verified"]
    assert "storage-full" in diagnostic.detail and "submitted to AWS" in diagnostic.detail
    assert diagnostic.fix_suggestion.startswith("check the resource's status and events in AWS")
    assert "accept the drift into the record" in diagnostic.fix_suggestion
    assert final.plan["applied_without_record"] is True


def test_a_terminal_result_does_not_fail_a_change_that_already_holds(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create_set_work_mem(service, "terminal-2", arn_for(session_factory, sandbox_snapshot, GROUP))
    monkeypatch.setattr(
        SetWorkMem,
        "verify_direct",
        lambda self, context, entries: VerificationResult(ok=True, live={"work_mem": "40000"}, terminal=True),
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.SUCCEEDED


def test_an_unreadable_record_is_a_diagnostic_with_the_way_to_restore_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    (_workspace_dir(operator_settings) / "console-record.json").write_text("{not json")
    _git("add", "-A", cwd=operator_settings.repo_root)
    _git("commit", "-q", "-m", "Corrupt the record", cwd=operator_settings.repo_root)
    runner.executors["test_set_work_mem"] = SetWorkMemExecutor(config_store)
    operation = _create_set_work_mem(
        service, "record-unreadable", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "record_unreadable"]
    assert "console-record.json" in diagnostic.detail and "git checkout" in diagnostic.fix_suggestion
    assert _workspace_changes(operator_settings) == ""


def test_a_file_staged_in_another_workspace_blocks_the_operation_and_is_never_committed(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _prepare_repo(operator_settings)
    other_workspace = operator_settings.repo_root / "terraform" / "production" / "123456789012" / "eu-west-2"
    other_workspace.mkdir(parents=True)
    (other_workspace / "parameter_groups.tf").write_text("# left staged by a commit that failed\n")
    _git("add", "-A", "--", "terraform/production", cwd=operator_settings.repo_root)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create_set_work_mem(
        service, "foreign-staged", arn_for(session_factory, sandbox_snapshot, GROUP)
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "checkout_guard"]
    assert "staged_changes_outside_workspace" in diagnostic.detail
    assert "git restore --staged" in diagnostic.fix_suggestion
    assert _git("diff", "--cached", "--name-only", cwd=operator_settings.repo_root) == (
        "terraform/production/123456789012/eu-west-2/parameter_groups.tf"
    )


class SetRetentionOptions(BaseModel):
    retention_days: int
    apply_at: str = "now"


class SetRetentionExecutor(AttributeChangeExecutor):
    operation_kind = "test_set_retention"

    def options_model(self):  # noqa: ANN201
        return SetRetentionOptions

    def attribute_values(self, options, record):  # noqa: ANN001, ANN201
        return {"backup_retention_period": options.retention_days}

    def summary(self, options):  # noqa: ANN001, ANN201
        return f"set retention to {options.retention_days}"


def test_direct_mode_queues_a_change_for_the_maintenance_window_and_records_the_desired_value(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    runner, _ = _runner(session_factory, operator_settings, store)
    runner.executors["test_set_retention"] = SetRetentionExecutor(store)
    # the simulator applies at once, so make it look like AWS holding the change: nothing is written, the
    # read-back shows the old value, and the pending modifications list the new one under AWS's field name.
    # Only turning backups on (0 -> 14) is held for the window; 7 -> 14 would apply at once.
    submitted: list[dict] = []
    monkeypatch.setattr(
        "rdscommander.operations.attribute_change.apply_attribute_values",
        lambda context, address, values, **keywords: (
            submitted.append({"values": values, **keywords}) or values
        ),
    )
    monkeypatch.setattr(
        "rdscommander.operations.attribute_change.read_pending_modifications",
        lambda context, address, step_name="verify_direct": {"BackupRetentionPeriod": 14},
    )
    monkeypatch.setattr(
        "rdscommander.operations.attribute_change.read_attribute_values",
        lambda context, address, attributes, step_name="verify_direct": {"backup_retention_period": 0},
    )
    service = OperationService(session_factory)
    arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "test_set_retention",
        "sandbox",
        TARGET_KEY,
        {"options": {"retention_days": 14, "apply_at": "maintenance_window"}, "resource_arn": arn},
        "k1",
        "phil",
        resource_arn=arn,
        title="t",
    )
    runner.run_once()
    service.approve(operation.id, "sam")
    runner.run_once()
    finished = service.get(operation.id)
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert submitted == [{"values": {"backup_retention_period": 14}, "apply_immediately": False}]
    entry = finished.plan["change_set"][0]
    assert entry["address"] == "aws_db_instance.demo_orders", (
        "direct mode derives the address of an unadopted record"
    )
    assert entry["apply_type"] == "pending-window"
    assert "maintenance window" in entry["downtime"]
    record = read_record(_workspace_dir(operator_settings))
    recorded_instance = next(i for i in record.render_input.instances if i.identifier == "demo-orders")
    assert recorded_instance.backup_retention_period == 14


def test_terraform_mode_refuses_to_queue_a_change_for_the_maintenance_window(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    runner.executors["test_set_retention"] = SetRetentionExecutor(runner.config_store)
    arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "test_set_retention",
        "sandbox",
        TARGET_KEY,
        {"options": {"retention_days": 14, "apply_at": "maintenance_window"}, "resource_arn": arn},
        "tf-window",
        "phil",
        resource_arn=arn,
        title="t",
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [diagnostic] = [
        d for d in final.diagnostic_reports if d.diagnostic_kind == "apply_at_window_not_available"
    ]
    assert "Apply: Now" in diagnostic.fix_suggestion
    assert [a.step_name for a in final.step_attempts][-1] == "render_desired"
