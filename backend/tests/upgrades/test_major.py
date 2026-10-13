"""Major upgrades through blue/green, steps 1 to 6 (and the second pause), in direct mode on the simulator."""

from __future__ import annotations

import uuid

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from pydantic import ValidationError
from sqlalchemy import func, select

from rdscommander.catalogue.catalogue import GROUP_UPGRADES, definition_for, version_changeable
from rdscommander.control_db.models import BlueGreenDeployment, ExecutionRecord, Operation
from rdscommander.operations.enums import OperationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext
from rdscommander.resources.identity import encode_resource_id
from rdscommander.upgrades.blue_green_api import BlueGreenStatus
from rdscommander.upgrades.major import MAJOR_UPGRADE_STEP_NAMES, MajorUpgradeExecutor
from rdscommander.upgrades.options import MajorUpgradeOptions
from tests.lifecycle.test_settings_changes import _set_live_and_refresh
from tests.operations.approval_helpers import approve_as_reviewed
from tests.parameters.test_executors import _application_client, _create_upgrade_build, _run_to_end
from tests.terraform.conftest import TARGET_KEY, arn_for

TARGET_VERSION = "17.2"
ORDERS_GROUP_FOR_17 = "demo-postgres16-tuned-pg17"
FIRST_PAUSE_DOWNTIME = "none until switchover"


def _quiet_runner(direct_mode_runner):
    """The direct-mode runner and service, polling without waiting."""
    runner, service, _ = direct_mode_runner
    runner.services.sleep = lambda seconds: None
    return runner, service


def _create_major(service, arn: str, resource_kind: str, key: str, identifier: str, **options):
    """A major upgrade as `POST /operations` makes it: disruptive, so the identifier is set at creation."""
    validated = MajorUpgradeOptions(target_engine_version=TARGET_VERSION, **options)
    return service.create(
        "major_upgrade",
        "sandbox",
        TARGET_KEY,
        {"options": validated.model_dump(mode="json"), "resource_arn": arn, "resource_kind": resource_kind},
        key,
        "phil",
        resource_arn=arn,
        title=f"Major version upgrade: {identifier}",
        approval_required_text=identifier,
    )


def _operations_of_kind(session_factory, kind: str) -> list[Operation]:
    with session_factory() as session:
        operations = list(session.scalars(select(Operation).where(Operation.operation_kind == kind)))
        for operation in operations:
            _ = operation.step_attempts, operation.diagnostic_reports
        session.expunge_all()
        return operations


def _aws_call_count(session_factory, operation_id, api_name: str) -> int:
    with session_factory() as session:
        return session.scalar(
            select(func.count(ExecutionRecord.id)).where(
                ExecutionRecord.operation_id == operation_id, ExecutionRecord.api_name == api_name
            )
        )


def _aws_calls_named(session_factory, api_name: str) -> int:
    """AWS calls of one API made by any operation."""
    with session_factory() as session:
        return session.scalar(
            select(func.count(ExecutionRecord.id)).where(ExecutionRecord.api_name == api_name)
        )


def _instance_group_names_in_aws() -> set[str]:
    rds = boto3.client("rds", region_name="eu-west-2")
    return {
        group["DBParameterGroupName"] for group in rds.describe_db_parameter_groups()["DBParameterGroups"]
    }


def _commit_count(operator_settings) -> int:
    from tests.terraform.test_adoption_executor import _git

    return int(_git("rev-list", "--count", "HEAD", cwd=operator_settings.repo_root))


def _deployment_rows(session_factory) -> list[BlueGreenDeployment]:
    with session_factory() as session:
        rows = list(session.scalars(select(BlueGreenDeployment)))
        session.expunge_all()
        return rows


def _diagnostic(operation, kind: str):
    return next(d for d in operation.diagnostic_reports if d.diagnostic_kind == kind)


# --- options and catalogue ---------------------------------------------------------------------------------


def test_major_upgrade_options_default_to_building_the_groups_and_ask_the_picker_for_major_targets() -> None:
    options = MajorUpgradeOptions(target_engine_version=TARGET_VERSION)
    assert (options.parameter_group_choice, options.group_name_suffix) == ("build", "")
    assert options.instance_parameter_group_name is None and options.cluster_parameter_group_name is None
    hints = MajorUpgradeOptions.model_json_schema()["properties"]["target_engine_version"]["ui"]
    assert (hints["widget"], hints["upgrade_kind"]) == ("engine_version", "major")
    with pytest.raises(ValidationError):
        MajorUpgradeOptions(target_engine_version="seventeen")
    with pytest.raises(ValidationError, match="instance parameter group"):
        MajorUpgradeOptions(target_engine_version=TARGET_VERSION, parameter_group_choice="existing")
    with pytest.raises(ValidationError, match="existing"):
        MajorUpgradeOptions(target_engine_version=TARGET_VERSION, instance_parameter_group_name="pg17")
    picked = MajorUpgradeOptions(
        target_engine_version=TARGET_VERSION,
        parameter_group_choice="existing",
        instance_parameter_group_name="arn:aws:rds:eu-west-2:000000000000:pg:my-pg17",
    )
    assert picked.instance_parameter_group_name == "my-pg17"  # the picker's ARN becomes the name


def test_major_upgrade_is_a_disruptive_upgrade_of_an_instance_or_cluster_with_eleven_steps() -> None:
    definition = definition_for("major_upgrade")
    assert definition.group == GROUP_UPGRADES and definition.title == "Major version upgrade"
    assert definition.disruptive is True and definition.destructive is False
    assert definition.options_model is MajorUpgradeOptions
    assert definition.applicability is version_changeable
    assert MAJOR_UPGRADE_STEP_NAMES == (
        "choose_target",
        "preflight",
        "await_approval",
        "prepare_parameter_groups",
        "create_deployment",
        "await_green_available",
        "await_switchover_approval",
        "switchover",
        "record_blue_side",
        "verify",
        "reconcile",
    )


# --- choose_target -----------------------------------------------------------------------------------------


def test_choose_target_refuses_a_minor_target_before_anything_is_built(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, service = _quiet_runner(direct_mode_runner)
    arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "major_upgrade",
        "sandbox",
        TARGET_KEY,
        {"options": {"target_engine_version": "16.6"}, "resource_arn": arn, "resource_kind": "instance"},
        "major-minor-target",
        "phil",
        resource_arn=arn,
        title="Major version upgrade: demo-orders",
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "upgrade_target_not_valid")
    assert "16.6" in diagnostic.detail and "17.2" in diagnostic.fix_suggestion
    assert {attempt.step_name for attempt in final.step_attempts} == {"choose_target"}
    assert _operations_of_kind(session_factory, "parameter_group_build_for_upgrade") == []


# --- steps 1 to 4: target, groups (built), preflight, first pause ------------------------------------------


def test_major_upgrade_of_demo_orders_names_the_groups_it_will_build_and_builds_them_only_after_approval(
    session_factory, operator_settings, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    client, token = _application_client(operator_settings, runner.config_store.current, session_factory)
    created = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "major_upgrade",
            "environment_slug": "sandbox",
            "resource_id": encode_resource_id(orders_arn),
            "options": {"target_engine_version": TARGET_VERSION},
            "idempotency_key": "major-orders",
        },
    )
    assert created.status_code == 202, created.json()
    operation_id = uuid.UUID(created.json()["operation_id"])
    assert service.get(operation_id).approval_required_text == "demo-orders"  # set by the route at creation

    commits_before = _commit_count(operator_settings)
    runner.run_once()
    paused = service.get(operation_id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    plan = paused.plan
    # choose_target: the family and the groups the upgrade will build, worked out without touching anything
    assert plan["target_family"] == "postgres17"
    assert plan["parameter_groups_plan"] == {
        "source": "build",
        "instance": ORDERS_GROUP_FOR_17,
        "cluster": None,
    }
    # nothing in AWS or the repository changes before the first approval
    assert _operations_of_kind(session_factory, "parameter_group_build_for_upgrade") == []
    assert _aws_calls_named(session_factory, "rds:CreateDBParameterGroup") == 0
    assert ORDERS_GROUP_FOR_17 not in _instance_group_names_in_aws()
    assert _commit_count(operator_settings) == commits_before
    # preflight: every check on the review's checklist, the replica warned about
    severities = {d.diagnostic_kind: d.severity for d in paused.diagnostic_reports}
    assert severities["preflight_backups"] == "info"
    assert severities["preflight_replicas"] == "warning"
    assert severities["preflight_extensions"] == "warning"
    assert [check["name"] for check in plan["preflight"]] == [
        "pending_maintenance",
        "backups",
        "in_progress_operations",
        "replicas",
        "class_and_storage",
        "extensions",
        "blue_side_name",
        "logical_replication",
    ]
    # the first pause: the action review names the groups it will build, no identifier to type
    review = plan["action_review"]
    assert review["what"] == [
        f"create a green copy of demo-orders on {TARGET_VERSION}: will build {ORDERS_GROUP_FOR_17} first; "
        "switchover needs a second approval"
    ]
    assert review["requires_reboot"] is False and review["downtime"] == FIRST_PAUSE_DOWNTIME
    assert plan["downtime"] == FIRST_PAUSE_DOWNTIME and plan["requires_reboot"] is False
    assert paused.approval_required_text is None
    detail = client.get(f"/api/v1/operations/{operation_id}").json()
    assert detail["approval_required_text"] is None and detail["downtime"] == FIRST_PAUSE_DOWNTIME
    # nothing is created in AWS before the approval
    assert _deployment_rows(session_factory) == []
    assert _aws_call_count(session_factory, operation_id, "rds:CreateBlueGreenDeployment") == 0

    # after the approval: the groups are built inline, by an operation of its own approved with this one,
    # then the green side is created and becomes available; the second pause asks for typing
    approve_as_reviewed(service, operation_id, "sam")
    runner.run_once()
    second = service.get(operation_id)
    [child] = _operations_of_kind(session_factory, "parameter_group_build_for_upgrade")
    assert second.plan["groups_operation_id"] == str(child.id)
    assert child.state == OperationState.SUCCEEDED, [d.detail for d in child.diagnostic_reports]
    assert child.approved_by_operator_name == "sam" and child.approved_at_utc == second.approved_at_utc
    assert child.plan["approved_with_operation_id"] == str(operation_id)
    assert child.lease_owner is None
    assert {"build_plan", "apply_direct", "record_provenance"} <= {a.step_name for a in child.step_attempts}
    assert second.plan["target_parameter_group_name"] == ORDERS_GROUP_FOR_17
    assert second.plan["target_cluster_parameter_group_name"] is None
    assert second.plan["parameter_groups_source"] == "built"
    group = boto3.client("rds", region_name="eu-west-2").describe_db_parameter_groups(
        DBParameterGroupName=ORDERS_GROUP_FOR_17
    )["DBParameterGroups"][0]
    assert group["DBParameterGroupFamily"] == "postgres17"
    assert _commit_count(operator_settings) > commits_before
    assert second.state == OperationState.AWAITING_APPROVAL, [d.detail for d in second.diagnostic_reports]
    assert second.plan["approval_stage"] == "switchover"
    assert second.approval_required_text == "demo-orders"
    [row] = _deployment_rows(session_factory)
    assert second.plan["deployment_id"] == row.deployment_id and row.operation_id == operation_id
    assert (row.status, row.target_engine_version, row.resource_arn) == (
        "AVAILABLE",
        TARGET_VERSION,
        orders_arn,
    )
    assert row.green_endpoint and second.plan["green_endpoint"] == row.green_endpoint
    assert _aws_call_count(session_factory, operation_id, "rds:CreateBlueGreenDeployment") == 1
    with session_factory() as session:
        request = session.scalars(
            select(ExecutionRecord).where(
                ExecutionRecord.operation_id == operation_id,
                ExecutionRecord.api_name == "rds:CreateBlueGreenDeployment",
            )
        ).one()
    assert request.request_parameters["TargetEngineVersion"] == TARGET_VERSION
    assert request.request_parameters["TargetDBParameterGroupName"] == ORDERS_GROUP_FOR_17
    assert "TargetDBClusterParameterGroupName" not in request.request_parameters
    # the first approval's audit trail is what the second pause shows; the switchover has its own
    assert second.approved_by_operator_name == "sam"
    assert "switchover_approved_at_utc" not in second.plan and "switchover_approved_by" not in second.plan
    assert len(_deployment_rows(session_factory)) == 1


def test_major_upgrade_reuses_groups_an_earlier_build_made_for_the_resource(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    build = _create_upgrade_build(service, orders_arn, "instance", "build-first", "Build upgrade groups")
    assert _run_to_end(runner, service, build.id).state == OperationState.SUCCEEDED
    operation = _create_major(service, orders_arn, "instance", "major-reuse", "demo-orders")
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["target_parameter_group_name"] == ORDERS_GROUP_FOR_17
    assert paused.plan["parameter_groups_plan"]["source"] == "reused"
    assert "groups_operation_id" not in paused.plan
    assert (
        f": reuses {ORDERS_GROUP_FOR_17} (built earlier for demo-orders);"
        in (paused.plan["action_review"]["what"][0])
    )
    assert [op.id for op in _operations_of_kind(session_factory, "parameter_group_build_for_upgrade")] == [
        build.id
    ]


def test_major_upgrade_of_the_aurora_cluster_builds_both_groups_and_passes_both_to_the_deployment(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, service = _quiet_runner(direct_mode_runner)
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = _create_major(service, aurora_arn, "cluster", "major-aurora", "demo-aurora")
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["target_family"] == "aurora-postgresql17"
    assert paused.plan["parameter_groups_plan"] == {
        "source": "build",
        "instance": "demo-aurora-pg17",
        "cluster": "demo-aurora-pg16-tuned-pg17",
    }
    assert (
        "will build demo-aurora-pg17, demo-aurora-pg16-tuned-pg17 first"
        in (paused.plan["action_review"]["what"][0])
    )
    assert _operations_of_kind(session_factory, "parameter_group_build_for_upgrade") == []
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    second = service.get(operation.id)
    assert second.state == OperationState.AWAITING_APPROVAL, [d.detail for d in second.diagnostic_reports]
    with session_factory() as session:
        request = session.scalars(
            select(ExecutionRecord).where(
                ExecutionRecord.operation_id == operation.id,
                ExecutionRecord.api_name == "rds:CreateBlueGreenDeployment",
            )
        ).one()
    assert request.request_parameters["TargetDBClusterParameterGroupName"] == "demo-aurora-pg16-tuned-pg17"
    assert request.request_parameters["TargetDBParameterGroupName"] == "demo-aurora-pg17"


def test_major_upgrade_fails_naming_the_build_operation_when_the_inline_build_fails(
    session_factory, operator_settings, config_store, sandbox_snapshot, direct_mode_runner
) -> None:
    # A group of the build's name exists that no build made: the builder refuses it, so the build fails.
    _set_live_and_refresh(
        session_factory,
        operator_settings,
        config_store,
        lambda rds: rds.create_db_parameter_group(
            DBParameterGroupName=ORDERS_GROUP_FOR_17,
            DBParameterGroupFamily="postgres17",
            Description="by hand",
        ),
    )
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(service, orders_arn, "instance", "major-build-fails", "demo-orders")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    [child] = _operations_of_kind(session_factory, "parameter_group_build_for_upgrade")
    assert child.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "upgrade_parameter_groups_not_built")
    assert str(child.id) in diagnostic.detail and ORDERS_GROUP_FOR_17 in diagnostic.detail
    assert _aws_call_count(session_factory, operation.id, "rds:CreateBlueGreenDeployment") == 0


def test_major_upgrade_refuses_existing_groups_of_another_family(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(
        service,
        orders_arn,
        "instance",
        "major-existing-wrong-family",
        "demo-orders",
        parameter_group_choice="existing",
        instance_parameter_group_name="demo-postgres16-tuned",
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "upgrade_parameter_group_not_usable")
    assert "demo-postgres16-tuned" in diagnostic.detail and "postgres17" in diagnostic.detail
    assert _operations_of_kind(session_factory, "parameter_group_build_for_upgrade") == []
    assert {attempt.step_name for attempt in final.step_attempts} == {"choose_target"}  # before any approval


def test_major_upgrade_uses_the_existing_groups_the_operator_names(
    session_factory, operator_settings, config_store, sandbox_snapshot, direct_mode_runner
) -> None:
    _set_live_and_refresh(
        session_factory,
        operator_settings,
        config_store,
        lambda rds: rds.create_db_parameter_group(
            DBParameterGroupName="orders-pg17", DBParameterGroupFamily="postgres17", Description="by hand"
        ),
    )
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(
        service,
        orders_arn,
        "instance",
        "major-existing",
        "demo-orders",
        parameter_group_choice="existing",
        instance_parameter_group_name="orders-pg17",
    )
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["target_parameter_group_name"] == "orders-pg17"
    assert ": uses existing orders-pg17;" in paused.plan["action_review"]["what"][0]
    assert _operations_of_kind(session_factory, "parameter_group_build_for_upgrade") == []


# --- steps 5 and 6: create_deployment (Review Focus 2) and await_green_available -------------------------


def _paused_at_switchover(session_factory, sandbox_snapshot, direct_mode_runner, key: str):
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(service, orders_arn, "instance", key, "demo-orders")
    runner.run_once()
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).plan.get("approval_stage") == "switchover"
    return runner, service, operation.id


def _context_for(runner, session, operation_id) -> StepContext:
    return StepContext(
        session=session,
        operation=session.get(Operation, operation_id),
        settings=runner.settings,
        console_config=runner.config_store.current,
        services=runner.services,
        runner_identity="r",
    )


def test_create_deployment_run_twice_creates_one_deployment_and_one_row(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, _, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-idempotent"
    )
    executor = runner.executors["major_upgrade"]
    assert isinstance(executor, MajorUpgradeExecutor)
    with session_factory() as session:
        context = _context_for(runner, session, operation_id)
        first = executor.execute("create_deployment", context)
        second = executor.execute("create_deployment", context)
    assert first.status == second.status == StepOutcomeStatus.COMPLETED
    assert len(_deployment_rows(session_factory)) == 1
    assert _aws_call_count(session_factory, operation_id, "rds:CreateBlueGreenDeployment") == 1


def test_create_deployment_after_a_crash_before_the_id_was_stored_finds_the_deployment_by_name(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, _, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-crash-window"
    )
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        deployment_id = operation.plan["deployment_id"]
        operation.plan = {key: value for key, value in operation.plan.items() if key != "deployment_id"}
        session.delete(session.get(BlueGreenDeployment, deployment_id))
        session.commit()
        outcome = runner.executors["major_upgrade"].execute(
            "create_deployment", _context_for(runner, session, operation_id)
        )
        assert outcome.status == StepOutcomeStatus.COMPLETED
        assert session.get(Operation, operation_id).plan["deployment_id"] == deployment_id
    [row] = _deployment_rows(session_factory)
    assert row.deployment_id == deployment_id
    deployments = boto3.client("rds", region_name="eu-west-2").describe_blue_green_deployments()
    assert len(deployments["BlueGreenDeployments"]) == 1
    assert _aws_call_count(session_factory, operation_id, "rds:CreateBlueGreenDeployment") == 1


def test_await_green_available_fails_on_a_terminal_status_and_leaves_the_deployment_in_place(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    monkeypatch.setattr(
        major_module,
        "describe_deployment",
        lambda context, deployment_id, step_name: BlueGreenStatus(
            "INVALID_CONFIGURATION", None, None, status_details="the target version is not supported"
        ),
    )
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(service, orders_arn, "instance", "major-invalid", "demo-orders")
    runner.run_once()
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_green_not_available")
    assert "INVALID_CONFIGURATION" in diagnostic.detail
    assert "the target version is not supported" in diagnostic.detail
    [row] = _deployment_rows(session_factory)
    assert row.status == "INVALID_CONFIGURATION" and row.deleted_at_utc is None
    assert _aws_call_count(session_factory, operation.id, "rds:DeleteBlueGreenDeployment") == 0


# --- fix round 1: the green wait's own timeout, and a create retried after a lost response ----------------


def test_the_green_wait_has_its_own_four_hour_timeout_setting() -> None:
    from rdscommander.config.settings import OperatorSettings

    assert OperatorSettings.model_fields["blue_green_create_timeout_seconds"].default == 14400


def test_await_green_available_past_its_timeout_says_the_deployment_is_still_being_created_and_was_left(
    session_factory, operator_settings, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    monkeypatch.setattr(
        major_module,
        "describe_deployment",
        lambda context, deployment_id, step_name: BlueGreenStatus("PROVISIONING", None, None),
    )
    operator_settings.blue_green_create_timeout_seconds = 30
    operator_settings.direct_verify_timeout_seconds = 100000  # the green wait must not use this one
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(service, orders_arn, "instance", "major-slow-green", "demo-orders")
    runner.run_once()
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_green_not_available")
    deployment_id = final.plan["deployment_id"]
    text = f"{diagnostic.detail} {diagnostic.fix_suggestion}"
    assert deployment_id in diagnostic.detail
    assert "still being created" in diagnostic.detail and "left in place" in text
    assert "nothing was switched" in text
    assert (
        "AVAILABLE" in diagnostic.fix_suggestion
        and "Clean up blue/green deployment" in diagnostic.fix_suggestion
    )
    assert "fix the cause" not in text
    [row] = _deployment_rows(session_factory)
    assert row.status == "PROVISIONING" and row.deleted_at_utc is None


def _failing_first(real, failures: list[Exception], pass_through_first: int = 0):
    """`real`, except that calls after the first `pass_through_first` raise `failures` one by one."""
    calls = {"count": 0}

    def describe(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls["count"] += 1
        if calls["count"] > pass_through_first and failures:
            raise failures.pop(0)
        return real(*args, **kwargs)

    return describe


def test_a_throttled_describe_during_the_green_wait_is_asked_again_after_a_backoff(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    runner, service = _quiet_runner(direct_mode_runner)
    slept: list[float] = []
    runner.services.sleep = slept.append
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(service, orders_arn, "instance", "major-throttled-green", "demo-orders")
    runner.run_once()
    monkeypatch.setattr(
        major_module,
        "describe_deployment",
        _failing_first(
            major_module.describe_deployment,
            [_client_error("Throttling"), EndpointConnectionError(endpoint_url="https://rds")],
        ),
    )
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["approval_stage"] == "switchover"
    assert len(slept) == 2 and slept[1] == 2 * slept[0] and max(slept) <= 60


def test_a_green_wait_whose_describe_keeps_failing_past_its_limit_names_the_deployment_left_in_place(
    session_factory, operator_settings, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    def unreachable(context, deployment_id, step_name):  # noqa: ANN001, ANN202
        raise EndpointConnectionError(endpoint_url="https://rds.eu-west-2.amazonaws.com")

    operator_settings.blue_green_create_timeout_seconds = 30
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(service, orders_arn, "instance", "major-unreachable-green", "demo-orders")
    runner.run_once()
    monkeypatch.setattr(major_module, "describe_deployment", unreachable)
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_step_aws_error")
    assert final.plan["deployment_id"] in diagnostic.detail and "await_green_available" in diagnostic.detail
    assert "EndpointConnectionError" in diagnostic.detail
    assert "left in place" in diagnostic.fix_suggestion
    [row] = _deployment_rows(session_factory)
    assert row.deleted_at_utc is None


def test_a_describe_that_fails_once_while_the_switchover_is_polled_does_not_end_the_upgrade(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-throttled-switchover"
    )
    # the first describe is the check before the switchover; the poll after it is throttled once
    monkeypatch.setattr(
        major_module,
        "describe_deployment",
        _failing_first(major_module.describe_deployment, [_client_error("ThrottlingException")], 1),
    )
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert _aws_call_count(session_factory, operation_id, "rds:SwitchoverBlueGreenDeployment") == 1


def test_a_cleanup_whose_settle_poll_is_throttled_once_still_settles(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.cleanup as cleanup_module

    runner, service, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-cleanup-throttled"
    )
    service.reject(operation_id, "sam")
    runner.run_once()
    [row] = _deployment_rows(session_factory)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    cleanup = service.create(
        "blue_green_cleanup",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"deployment_id": row.deployment_id, "delete_green": True},
            "deployment_id": row.deployment_id,
            "resource_arn": orders_arn,
            "resource_kind": "instance",
        },
        "cleanup-throttled",
        "phil",
        resource_arn=orders_arn,
        title="Clean up blue/green deployment: demo-orders",
        approval_required_text="demo-orders",
    )
    runner.run_once()
    assert service.get(cleanup.id).state == OperationState.AWAITING_APPROVAL
    # the describe before the delete passes; the first settle poll is throttled
    monkeypatch.setattr(
        cleanup_module,
        "describe_deployment",
        _failing_first(cleanup_module.describe_deployment, [_client_error("Throttling")], 1),
    )
    approve_as_reviewed(service, cleanup.id, "sam")
    runner.run_once()
    final = service.get(cleanup.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert (
        boto3.client("rds", region_name="eu-west-2").describe_blue_green_deployments()["BlueGreenDeployments"]
        == []
    )


def _client_error(code: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": f"{code} for the test"}}, "CreateBlueGreenDeployment"
    )


def test_a_create_answered_already_exists_after_a_lost_response_tracks_the_deployment_aws_made(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    real_create = major_module.create_deployment

    def create_then_answer_already_exists(*arguments, **keyword_arguments):  # noqa: ANN002, ANN003, ANN202
        real_create(*arguments, **keyword_arguments)  # AWS made it; the response was lost and the retry...
        raise _client_error("BlueGreenDeploymentAlreadyExistsFault")

    monkeypatch.setattr(major_module, "create_deployment", create_then_answer_already_exists)
    runner, service, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-lost-response"
    )
    paused = service.get(operation_id)
    [row] = _deployment_rows(session_factory)
    assert paused.plan["deployment_id"] == row.deployment_id
    assert (
        row.deployment_id
        == boto3.client("rds", region_name="eu-west-2").describe_blue_green_deployments()[
            "BlueGreenDeployments"
        ][0]["BlueGreenDeploymentIdentifier"]
    )
    assert "blue_green_create_refused" not in [d.diagnostic_kind for d in paused.diagnostic_reports]


def test_a_create_aws_refuses_without_making_a_deployment_fails_naming_the_deployment_name(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    def refuse(*arguments, **keyword_arguments):  # noqa: ANN002, ANN003, ANN202
        raise _client_error("InvalidParameterCombination")

    monkeypatch.setattr(major_module, "create_deployment", refuse)
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(service, orders_arn, "instance", "major-refused", "demo-orders")
    runner.run_once()
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_create_refused")
    deployment_name = final.plan["deployment_name"]
    assert deployment_name in diagnostic.detail and deployment_name in diagnostic.fix_suggestion
    assert "InvalidParameterCombination" in diagnostic.detail
    assert _deployment_rows(session_factory) == [] and "deployment_id" not in final.plan


# --- step 7: the second pause (Review Focus 1) -------------------------------------------------------------

SWITCHOVER_DOWNTIME = "connections drop while AWS switches over, usually for under a minute"


def _stub_major(monkeypatch, name: str, replacement) -> None:  # noqa: ANN001
    import rdscommander.upgrades.major as major_module

    monkeypatch.setattr(major_module, name, replacement)


def _switchover_approved(session_factory, sandbox_snapshot, direct_mode_runner, key: str):
    """Paused at the second stage, then approved for the switchover by a second approver."""
    runner, service, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, key
    )
    approve_as_reviewed(service, operation_id, "lee")
    return runner, service, operation_id


def _upgraded(session_factory, sandbox_snapshot, direct_mode_runner, key: str):
    """A major upgrade of demo-orders run to its end: switched over, verified, reconciled, blue side kept."""
    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, key
    )
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    return runner, service, final


def _described_instance(identifier: str) -> dict:
    return boto3.client("rds", region_name="eu-west-2").describe_db_instances(
        DBInstanceIdentifier=identifier
    )["DBInstances"][0]


def test_the_second_pause_asks_for_the_identifier_and_reviews_the_switchover_with_the_green_endpoint(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    _, service, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-second-pause"
    )
    paused = service.get(operation_id)
    plan = paused.plan
    assert paused.approval_required_text == "demo-orders"
    assert plan["approval_stage"] == "switchover"
    review = plan["action_review"]
    assert review["what"] == [
        f"switch production traffic from demo-orders to green ({plan['green_endpoint']}, {TARGET_VERSION}); "
        "demo-orders becomes demo-orders-old1 and is kept"
    ]
    assert review["identifiers"] == ["demo-orders"] and review["reconcile"] is True
    assert review["downtime"] == plan["downtime"] == SWITCHOVER_DOWNTIME
    # the preflight list stays on the plan for the review page to show again
    assert [check["name"] for check in plan["preflight"]][0] == "pending_maintenance"
    # nothing is switched before the second approval
    assert _aws_call_count(session_factory, operation_id, "rds:SwitchoverBlueGreenDeployment") == 0


def test_the_second_approval_is_recorded_on_its_own_and_keeps_the_first_approvals_audit_trail(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    _, service, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-second-approval"
    )
    first = service.get(operation_id)
    approved = approve_as_reviewed(service, operation_id, "lee")
    assert approved.state == OperationState.QUEUED
    after = service.get(operation_id)
    assert (after.approved_by_operator_name, after.approved_at_utc) == (
        first.approved_by_operator_name,
        first.approved_at_utc,
    )
    assert after.plan["switchover_approved_by"] == "lee" and after.plan["switchover_approved_at_utc"]


def test_a_switchover_approved_hours_later_on_a_deployment_no_longer_available_fails_and_leaves_it(
    session_factory, operator_settings, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-stale"
    )
    _stub_major(
        monkeypatch,
        "describe_deployment",
        lambda context, deployment_id, step_name: BlueGreenStatus(
            "INVALID_CONFIGURATION", None, None, status_details="the source was modified"
        ),
    )
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_not_ready_for_switchover")
    assert "INVALID_CONFIGURATION" in diagnostic.detail and "the source was modified" in diagnostic.detail
    assert "left in place" in diagnostic.fix_suggestion
    assert _aws_call_count(session_factory, operation_id, "rds:SwitchoverBlueGreenDeployment") == 0
    [row] = _deployment_rows(session_factory)
    assert row.deleted_at_utc is None and row.switched_over_at_utc is None
    assert (
        len(
            boto3.client("rds", region_name="eu-west-2").describe_blue_green_deployments()[
                "BlueGreenDeployments"
            ]
        )
        == 1
    )
    # the failed upgrade's inbox row offers the cleanup of the deployment it left
    client, _ = _application_client(operator_settings, runner.config_store.current, session_factory)
    inbox = client.get("/api/v1/attention", params={"environment_slug": "sandbox"}).json()
    [failed_row] = [row for row in inbox["failed_recent"] if row["operation_id"] == str(operation_id)]
    assert failed_row["action_kind"] == "blue_green_cleanup"
    assert failed_row["action_options"] == {"deployment_id": row.deployment_id, "delete_green": True}


def test_a_switchover_is_refused_while_the_blue_side_has_pending_modifications(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-pending-modifications"
    )
    _stub_major(
        monkeypatch,
        "blue_pending_modifications",
        lambda context, resource_arn, step_name: ["demo-orders: DBInstanceClass"],
    )
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_not_ready_for_switchover")
    assert "demo-orders: DBInstanceClass" in diagnostic.detail
    assert _aws_call_count(session_factory, operation_id, "rds:SwitchoverBlueGreenDeployment") == 0


def _add_other_operation(session_factory, resource_arn: str, state: str, key: str, title: str) -> uuid.UUID:
    """Another console operation on the resource, in `state`, finished now when it succeeded."""
    from rdscommander.control_db.models import utc_now

    with session_factory() as session:
        other = Operation(
            operation_kind="instance_settings_change",
            state=state,
            environment_slug="sandbox",
            target_key=TARGET_KEY,
            resource_arn=resource_arn,
            title=title,
            plan={"options": {}},
            idempotency_key=key,
            created_by_operator_name="kim",
            finished_at_utc=utc_now() if state == OperationState.SUCCEEDED.value else None,
        )
        session.add(other)
        session.commit()
        return other.id


def test_the_switchover_pause_fails_while_another_operation_on_the_resource_is_in_progress(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, service = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(service, orders_arn, "instance", "major-busy-at-pause", "demo-orders")
    runner.run_once()
    approve_as_reviewed(service, operation.id, "sam")
    other_id = _add_other_operation(
        session_factory, orders_arn, "AWAITING_APPROVAL", "busy-1", "Change settings: demo-orders"
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_not_ready_for_switchover")
    assert str(other_id) in diagnostic.detail and "Change settings: demo-orders" in diagnostic.detail
    assert "left in place" in diagnostic.fix_suggestion


def test_the_switchover_is_refused_while_another_operation_on_the_resource_is_in_progress(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-busy-at-switchover"
    )
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    other_id = _add_other_operation(session_factory, orders_arn, "RUNNING", "busy-2", "Reboot: demo-orders")
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_not_ready_for_switchover")
    assert str(other_id) in diagnostic.detail
    assert _aws_call_count(session_factory, operation_id, "rds:SwitchoverBlueGreenDeployment") == 0


def test_a_change_to_the_blue_side_since_the_green_copy_was_made_is_named_on_the_switchover_review(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, service, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-blue-changed"
    )
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    _add_other_operation(
        session_factory, orders_arn, "SUCCEEDED", "changed-1", "Edit parameters: demo-orders"
    )
    executor = runner.executors["major_upgrade"]
    with session_factory() as session:
        outcome = executor.execute("await_switchover_approval", _context_for(runner, session, operation_id))
    assert outcome.status == StepOutcomeStatus.AWAITING_APPROVAL
    paused = service.get(operation_id)
    what = paused.plan["action_review"]["what"]
    assert (
        "changed on the blue side since the green copy was made: Edit parameters: demo-orders; the green "
        "side does not carry them"
    ) in what
    warning = _diagnostic(paused, "blue_side_changed_since_green_copy")
    assert warning.severity == "warning" and "Edit parameters: demo-orders" in warning.detail
    # right before the switchover the change is named again, and the approved switchover goes ahead
    approve_as_reviewed(service, operation_id, "lee")
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert {
        d.step_name
        for d in final.diagnostic_reports
        if d.diagnostic_kind == "blue_side_changed_since_green_copy"
    } == {
        "await_switchover_approval",
        "switchover",
    }


# --- steps 8 and 9: switchover and verify ------------------------------------------------------------------


def test_switchover_promotes_green_to_the_production_name_and_verify_reads_the_target_version_and_groups(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    _, _, final = _upgraded(session_factory, sandbox_snapshot, direct_mode_runner, "major-switchover")
    assert [attempt.step_name for attempt in final.step_attempts if attempt.outcome_status == "completed"][
        -5:
    ] == ["await_switchover_approval", "switchover", "record_blue_side", "verify", "reconcile"]
    production = _described_instance("demo-orders")
    assert production["EngineVersion"] == TARGET_VERSION
    assert production["DBParameterGroups"][0]["DBParameterGroupName"] == ORDERS_GROUP_FOR_17
    assert _described_instance("demo-orders-old1")["EngineVersion"] == "16.4"
    assert _aws_call_count(session_factory, final.id, "rds:SwitchoverBlueGreenDeployment") == 1
    with session_factory() as session:
        request = session.scalars(
            select(ExecutionRecord).where(
                ExecutionRecord.operation_id == final.id,
                ExecutionRecord.api_name == "rds:SwitchoverBlueGreenDeployment",
            )
        ).one()
    assert request.request_parameters["SwitchoverTimeout"] == 300
    [row] = _deployment_rows(session_factory)
    assert row.status == "SWITCHOVER_COMPLETED" and row.switched_over_at_utc is not None
    assert row.deleted_at_utc is None
    # a major upgrade never deletes anything: the deployment and the blue side are kept
    assert _aws_call_count(session_factory, final.id, "rds:DeleteBlueGreenDeployment") == 0
    assert _aws_call_count(session_factory, final.id, "rds:DeleteDBInstance") == 0
    # the second approval kept the first one's audit trail
    assert final.approved_by_operator_name == "sam" and final.plan["switchover_approved_by"] == "lee"


def test_a_switchover_aws_reports_failed_fails_naming_the_aws_reason_and_leaves_the_deployment(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-switchover-failed"
    )
    statuses = iter(
        [
            BlueGreenStatus("AVAILABLE", "green.example", None),
            BlueGreenStatus("SWITCHOVER_IN_PROGRESS", "green.example", None),
            BlueGreenStatus(
                "SWITCHOVER_FAILED", "green.example", None, status_details="replica lag too high"
            ),
        ]
    )
    _stub_major(monkeypatch, "describe_deployment", lambda context, deployment_id, step_name: next(statuses))
    _stub_major(monkeypatch, "switchover", lambda context, deployment_id, timeout_seconds, step_name: None)
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_switchover_failed")
    assert "SWITCHOVER_FAILED" in diagnostic.detail and "replica lag too high" in diagnostic.detail
    assert "left in place" in diagnostic.fix_suggestion
    [row] = _deployment_rows(session_factory)
    assert row.status == "SWITCHOVER_FAILED" and row.deleted_at_utc is None
    assert "verify" not in {attempt.step_name for attempt in final.step_attempts}


def test_a_verify_mismatch_after_the_switchover_warns_and_the_upgrade_still_reconciles_and_succeeds(
    session_factory, operator_settings, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-not-verified"
    )
    real_describe = major_module.instance_describe

    def still_on_the_old_version(context, identifier, step_name):  # noqa: ANN001, ANN202
        described = real_describe(context, identifier, step_name)
        return {**described, "EngineVersion": "16.4"} if identifier == "demo-orders" else described

    monkeypatch.setattr(major_module, "instance_describe", still_on_the_old_version)
    runner.run_once()
    final = service.get(operation_id)
    # the switchover is done, so the record must catch up: the mismatch is a warning on the operation page
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    diagnostic = _diagnostic(final, "upgrade_not_verified")
    assert diagnostic.severity == "warning"
    assert "16.4" in diagnostic.detail and TARGET_VERSION in diagnostic.detail
    reconcile = next(attempt for attempt in final.step_attempts if attempt.step_name == "reconcile")
    assert reconcile.output["commit_sha"]
    # the blue side was tagged and listed right after the switchover, before verify (I4)
    old = _described_instance("demo-orders-old1")
    assert {t["Key"]: t["Value"] for t in old.get("TagList", [])}["rdscommander:blue-side"] == str(final.id)
    client, _ = _application_client(operator_settings, runner.config_store.current, session_factory)
    inbox = client.get("/api/v1/attention", params={"environment_slug": "sandbox"}).json()
    [row] = inbox["blue_side"]
    assert row["resource_identifier"] == "demo-orders-old1" and row["action_kind"] == "decommission"


def test_verify_fails_when_aws_does_not_list_the_production_identifier_after_the_switchover(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-production-gone"
    )
    real_describe = major_module.instance_describe

    def production_not_listed_at_verify(context, identifier, step_name):  # noqa: ANN001, ANN202
        if identifier == "demo-orders" and step_name == "verify":
            return None
        return real_describe(context, identifier, step_name)

    monkeypatch.setattr(major_module, "instance_describe", production_not_listed_at_verify)
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "upgrade_not_verified")
    assert diagnostic.severity == "error" and "AWS does not list demo-orders" in diagnostic.detail
    assert "reconcile" not in {attempt.step_name for attempt in final.step_attempts}
    assert final.plan.get("applied_without_record") is True
    # direct mode: there is no Terraform state to finish by hand
    assert "state mv" not in diagnostic.fix_suggestion


# --- step 10: reconcile ------------------------------------------------------------------------------------


def test_reconcile_in_direct_mode_records_the_new_version_and_group_from_the_fresh_snapshot(
    session_factory, operator_settings, sandbox_snapshot, direct_mode_runner
) -> None:
    from tests.lifecycle.test_settings_changes import _recorded
    from tests.terraform.test_adoption_executor import _git

    _, _, final = _upgraded(session_factory, sandbox_snapshot, direct_mode_runner, "major-reconcile")
    recorded = _recorded(operator_settings, "instances", "demo-orders")
    assert recorded.engine_version == TARGET_VERSION
    # the group the build made is recorded, so the instance refers to it by its Terraform address
    assert recorded.parameter_group_reference == "aws_db_parameter_group.demo_postgres16_tuned_pg17.name"
    reconcile = next(attempt for attempt in final.step_attempts if attempt.step_name == "reconcile")
    assert reconcile.output["commit_sha"]
    log = _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
    assert "Major version upgrade: demo-orders" in log


# --- step 11: record_blue_side (Review Focus 4) ------------------------------------------------------------


def test_record_blue_side_tags_the_old_instance_which_the_inbox_lists_and_history_credits_to_the_upgrade(
    session_factory, operator_settings, sandbox_snapshot, direct_mode_runner
) -> None:
    from rdscommander.control_db.models import ResourceChangeEvent
    from rdscommander.history.change_events import EXPLANATION_OUTSIDE_CONSOLE

    runner, _, final = _upgraded(session_factory, sandbox_snapshot, direct_mode_runner, "major-blue-side")
    assert final.plan["blue_side_identifiers"] == ["demo-orders-old1"]
    old = _described_instance("demo-orders-old1")
    tags = {tag["Key"]: tag["Value"] for tag in old.get("TagList", [])}
    assert tags["rdscommander:blue-side"] == str(final.id)
    # the inbox lists the blue side with Decommission
    client, _ = _application_client(operator_settings, runner.config_store.current, session_factory)
    inbox = client.get("/api/v1/attention", params={"environment_slug": "sandbox"}).json()
    [row] = inbox["blue_side"]
    assert row["kind"] == "blue_side" and row["resource_identifier"] == "demo-orders-old1"
    assert row["resource_id"] == encode_resource_id(old["DBInstanceArn"])
    assert row["operation_id"] == str(final.id) and row["action_kind"] == "decommission"
    assert row["environment_slug"] == "sandbox"
    # history explains the rename and the new resources by the upgrade, never as outside changes
    with session_factory() as session:
        events = list(session.scalars(select(ResourceChangeEvent)))
    assert [e for e in events if e.explanation == EXPLANATION_OUTSIDE_CONSOLE] == []
    old_events = [e for e in events if e.resource_arn == old["DBInstanceArn"]]
    assert old_events and {e.operation_id for e in old_events} == {final.id}
    assert inbox["outside_changes"] == []


def test_a_refresh_while_the_upgrade_waits_for_the_switchover_credits_the_green_side_to_the_upgrade(
    session_factory, sandbox_snapshot, direct_mode_runner
) -> None:
    from rdscommander.control_db.models import ResourceChangeEvent

    runner, service, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-refresh-in-pause"
    )
    green_arn = service.get(operation_id).plan["green_arn"]
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    # someone changes production by hand while the upgrade waits: that stays an outside change (I3)
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=3, ApplyImmediately=True
    )
    service.create("inventory_refresh", "sandbox", None, {}, "refresh-in-pause", "phil")
    runner.run_once()
    with session_factory() as session:
        green_events = list(
            session.scalars(select(ResourceChangeEvent).where(ResourceChangeEvent.resource_arn == green_arn))
        )
        production_events = list(
            session.scalars(select(ResourceChangeEvent).where(ResourceChangeEvent.resource_arn == orders_arn))
        )
    assert green_events and {(e.explanation, e.operation_id) for e in green_events} == {
        ("operation", operation_id)
    }
    assert production_events and {(e.explanation, e.operation_id) for e in production_events} == {
        ("outside_console", None)
    }


# --- rejection and blue_green_cleanup (Review Focus 5) -----------------------------------------------------


def test_rejecting_the_switchover_cancels_leaves_the_deployment_and_the_inbox_offers_cleanup_which_deletes_it(
    session_factory, operator_settings, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, service, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-rejected"
    )
    client, token = _application_client(operator_settings, runner.config_store.current, session_factory)
    rejected = client.post(f"/api/v1/operations/{operation_id}/reject", headers={"X-CSRF-Token": token})
    assert rejected.status_code == 200
    runner.run_once()  # the cleanup hook runs for the rejected operation
    cancelled = service.get(operation_id)
    assert cancelled.state == OperationState.CANCELLED and cancelled.plan.get("cleanup_done_at_utc")
    left = _diagnostic(cancelled, "blue_green_deployment_left_in_place")
    [row] = _deployment_rows(session_factory)
    assert row.deployment_id in left.detail and "Clean up blue/green deployment" in left.fix_suggestion
    rds = boto3.client("rds", region_name="eu-west-2")
    [deployment] = rds.describe_blue_green_deployments()["BlueGreenDeployments"]
    green_identifier = deployment["Target"].rsplit(":", 1)[-1]
    assert (
        row.deleted_at_utc is None
        and _aws_call_count(session_factory, operation_id, "rds:DeleteBlueGreenDeployment") == 0
    )
    # the inbox's cancelled row offers the cleanup, naming the deployment
    inbox = client.get("/api/v1/attention", params={"environment_slug": "sandbox"}).json()
    [cancelled_row] = [r for r in inbox["failed_recent"] if r["operation_id"] == str(operation_id)]
    assert cancelled_row["kind"] == "cancelled_recent"
    assert cancelled_row["action_kind"] == "blue_green_cleanup"
    assert cancelled_row["action_options"] == {"deployment_id": row.deployment_id, "delete_green": True}
    orders_id = cancelled_row["resource_id"]
    actions = {a["kind"]: a for a in client.get(f"/api/v1/resources/{orders_id}").json()["actions"]}
    assert actions["blue_green_cleanup"]["applicable"] is True

    created = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "blue_green_cleanup",
            "environment_slug": "sandbox",
            "resource_id": orders_id,
            "options": cancelled_row["action_options"],
            "idempotency_key": "cleanup-orders",
        },
    )
    assert created.status_code == 202, created.json()
    cleanup_id = uuid.UUID(created.json()["operation_id"])
    assert service.get(cleanup_id).approval_required_text == "demo-orders"
    runner.run_once()
    awaiting = service.get(cleanup_id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    [what] = awaiting.plan["action_review"]["what"]
    assert row.deployment_id in what and "AVAILABLE" in what and green_identifier in what
    approve_as_reviewed(service, cleanup_id, "sam")
    runner.run_once()
    finished = service.get(cleanup_id)
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert rds.describe_blue_green_deployments()["BlueGreenDeployments"] == []
    with pytest.raises(ClientError):
        rds.describe_db_instances(DBInstanceIdentifier=green_identifier)
    assert _described_instance("demo-orders")["EngineVersion"] == "16.4"  # production untouched
    [row] = _deployment_rows(session_factory)
    assert row.deleted_at_utc is not None
    inbox = client.get("/api/v1/attention", params={"environment_slug": "sandbox"}).json()
    assert [r for r in inbox["failed_recent"] if r["operation_id"] == str(operation_id)] == []
    actions = {a["kind"]: a for a in client.get(f"/api/v1/resources/{orders_id}").json()["actions"]}
    assert actions["blue_green_cleanup"]["applicable"] is False


def test_blue_green_cleanup_is_not_offered_without_a_deployment_left_in_place(
    session_factory, operator_settings, sandbox_snapshot, direct_mode_runner
) -> None:
    runner, _ = _quiet_runner(direct_mode_runner)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    client, token = _application_client(operator_settings, runner.config_store.current, session_factory)
    orders_id = encode_resource_id(orders_arn)
    actions = {a["kind"]: a for a in client.get(f"/api/v1/resources/{orders_id}").json()["actions"]}
    assert actions["blue_green_cleanup"]["applicable"] is False
    assert "no blue/green deployment" in actions["blue_green_cleanup"]["reason"]
    refused = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "blue_green_cleanup",
            "environment_slug": "sandbox",
            "resource_id": orders_id,
            "options": {},
            "idempotency_key": "cleanup-nothing",
        },
    )
    assert refused.status_code == 422


def test_a_cleanup_whose_deployment_started_switching_over_after_its_review_deletes_nothing(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    runner, service, operation_id = _paused_at_switchover(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-cleanup-raced"
    )
    service.reject(operation_id, "sam")
    runner.run_once()
    [row] = _deployment_rows(session_factory)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    cleanup = service.create(
        "blue_green_cleanup",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"deployment_id": row.deployment_id, "delete_green": True},
            "deployment_id": row.deployment_id,
            "resource_arn": orders_arn,
            "resource_kind": "instance",
        },
        "cleanup-raced",
        "phil",
        resource_arn=orders_arn,
        title="Clean up blue/green deployment: demo-orders",
        approval_required_text="demo-orders",
    )
    runner.run_once()
    assert service.get(cleanup.id).state == OperationState.AWAITING_APPROVAL
    # between the review and the approval, someone starts the switchover in the AWS console
    import rdscommander.upgrades.cleanup as cleanup_module

    monkeypatch.setattr(
        cleanup_module,
        "describe_deployment",
        lambda context, deployment_id, step_name: BlueGreenStatus("SWITCHOVER_IN_PROGRESS", None, None),
    )
    approve_as_reviewed(service, cleanup.id, "sam")
    runner.run_once()
    final = service.get(cleanup.id)
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_green_deployment_switching_over")
    assert "SWITCHOVER_IN_PROGRESS" in diagnostic.detail and row.deployment_id in diagnostic.detail
    assert _aws_call_count(session_factory, cleanup.id, "rds:DeleteBlueGreenDeployment") == 0
    assert not final.plan.get("applied_without_record")
    deployments = boto3.client("rds", region_name="eu-west-2").describe_blue_green_deployments()
    assert len(deployments["BlueGreenDeployments"]) == 1


ORDERS_OLD_ADDRESS = "aws_db_instance.demo_orders_old1"


def _orders_import_plan(actions: list[str]) -> dict:
    """A scripted plan that imports the green instance into the production address with `actions`."""
    return {
        "resource_changes": [
            {
                "address": "aws_db_instance.demo_orders",
                "type": "aws_db_instance",
                "change": {
                    "actions": actions,
                    "before": {"engine_version": TARGET_VERSION},
                    "after": {"engine_version": TARGET_VERSION},
                    "importing": {"id": "demo-orders"},
                },
            }
        ]
    }


def _terraform_major_upgrade(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    key: str,
    plan_scripts: list[dict | Exception] | None = None,
    **fake_fields,
):
    """A Terraform-mode major upgrade of demo-orders (existing groups), run to its end, scripted runner.

    A plan script that is an exception is raised by the scripted plan, as a step stopping half-way (the
    step walk turns it into a failed step and nothing reverts the files).
    """
    from tests.drift.test_detect_executor import _adopt_estate
    from tests.terraform.test_adoption_executor import FakeTerraformRunner

    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    _set_live_and_refresh(
        session_factory,
        operator_settings,
        config_store,
        lambda rds: rds.create_db_parameter_group(
            DBParameterGroupName="orders-pg17", DBParameterGroupFamily="postgres17", Description="by hand"
        ),
    )
    runner.services.sleep = lambda seconds: None
    fake = FakeTerraformRunner(
        runner.services.execution_recorder,
        plan_scripts=plan_scripts or [_orders_import_plan(["no-op"]), {"resource_changes": []}],
        **fake_fields,
    )
    runner.services.terraform_runner = fake
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_major(
        service,
        orders_arn,
        "instance",
        key,
        "demo-orders",
        parameter_group_choice="existing",
        instance_parameter_group_name="orders-pg17",
    )
    runner.run_once()
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).plan["approval_stage"] == "switchover"
    approve_as_reviewed(service, operation.id, "lee")
    runner.run_once()
    return runner, service, fake, service.get(operation.id)


def test_terraform_mode_major_upgrade_reimports_the_switched_instance_and_commits_after_a_clean_plan(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import ORDERS_ADDRESS
    from tests.lifecycle.test_settings_changes import _recorded, _workspace_dir
    from tests.terraform.test_adoption_executor import _git

    runner, _, fake, final = _terraform_major_upgrade(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, "major-terraform"
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    # state's entry (the blue instance's resource id) moves to the blue side's address first, then the
    # production address is removed and the green instance imported into it (I5)
    assert fake.calls == [
        "init",
        "state_list",
        f"state_mv:{ORDERS_ADDRESS}->{ORDERS_OLD_ADDRESS}",
        f"state_rm:{ORDERS_ADDRESS}",
        "plan:reconcile",
        "apply",
        "init",
        "apply_refresh_only",
        "plan:reconcile",
    ]
    recorded = _recorded(operator_settings, "instances", "demo-orders")
    assert recorded.engine_version == TARGET_VERSION and "orders-pg17" in str(
        recorded.parameter_group_reference
    )
    # the blue side stays console-managed: it is in the record at its own address
    blue_side = _recorded(operator_settings, "instances", "demo-orders-old1")
    assert blue_side.engine_version == "16.4" and blue_side.terraform_name == "demo_orders_old1"
    assert (
        'resource "aws_db_instance" "demo_orders_old1"'
        in (_workspace_dir(operator_settings) / "instances.tf").read_text()
    )
    workspace = _workspace_dir(operator_settings)
    assert not (workspace / "imports.tf").exists()
    committed = _git("show", "--name-only", "--format=%s", "HEAD", cwd=operator_settings.repo_root)
    assert "Major version upgrade: demo-orders" in committed and "imports.tf" not in committed
    assert final.plan.get("terraform_apply_invoked") is True
    # and the inbox offers Decommission on it in Terraform mode too
    client, _ = _application_client(operator_settings, runner.config_store.current, session_factory)
    inbox = client.get("/api/v1/attention", params={"environment_slug": "sandbox"}).json()
    [row] = inbox["blue_side"]
    assert row["resource_identifier"] == "demo-orders-old1" and row["action_kind"] == "decommission"


def test_terraform_mode_reconcile_refuses_with_the_commands_when_the_state_move_fails_and_touches_nothing(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.terraform.record import read_record
    from tests.drift.test_detect_executor import ORDERS_ADDRESS
    from tests.lifecycle.test_settings_changes import _workspace_dir

    _, _, fake, final = _terraform_major_upgrade(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        "major-terraform-mv-fails",
        state_move_error="Error acquiring the state lock",
    )
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "terraform_state_move_failed")
    assert "Error acquiring the state lock" in diagnostic.detail
    assert (
        f"`cd {_workspace_dir(operator_settings)}; tofu state mv {ORDERS_ADDRESS} {ORDERS_OLD_ADDRESS}; "
        f"tofu import {ORDERS_ADDRESS} demo-orders; tofu plan`"
    ) in diagnostic.fix_suggestion
    # state was not touched further, and the files are back as the last commit has them
    assert not any(call.startswith(("state_rm", "apply")) for call in fake.calls)
    workspace = _workspace_dir(operator_settings)
    assert not (workspace / "imports.tf").exists()
    identifiers = {item.identifier for item in read_record(workspace).render_input.instances}
    assert "demo-orders-old1" not in identifiers


# --- fix round 1, I1: the blue side is found by its resource id, and an old "-old1" stops the upgrade -------


def test_record_blue_side_refuses_an_old1_whose_resource_id_is_not_the_blue_sides(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module

    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-wrong-old1"
    )
    real_describe = major_module.instance_describe

    def another_instance_under_old1(context, identifier, step_name):  # noqa: ANN001, ANN202
        described = real_describe(context, identifier, step_name)
        if identifier == "demo-orders-old1" and described is not None:
            return {**described, "DbiResourceId": "db-SOMEONEELSE"}
        return described

    monkeypatch.setattr(major_module, "instance_describe", another_instance_under_old1)
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.FAILED
    captured = final.plan["blue_resource_ids"]["demo-orders"]
    diagnostic = _diagnostic(final, "blue_side_not_found")
    assert captured in diagnostic.detail and "db-SOMEONEELSE" in diagnostic.detail
    tags = {t["Key"] for t in _described_instance("demo-orders-old1").get("TagList", [])}
    assert "rdscommander:blue-side" not in tags
    # direct mode: there is no Terraform state to finish by hand
    assert "state mv" not in diagnostic.fix_suggestion and "import" not in diagnostic.fix_suggestion


# --- fix round 1, I2: an AWS error after the deployment exists names it and keeps history -------------------


def test_an_aws_error_after_the_switchover_names_the_deployment_left_in_place_and_history_keeps_the_credit(
    session_factory, sandbox_snapshot, direct_mode_runner, monkeypatch
) -> None:
    from rdscommander.control_db.models import utc_now
    from rdscommander.history.change_events import explain_event

    runner, service, operation_id = _switchover_approved(
        session_factory, sandbox_snapshot, direct_mode_runner, "major-tag-denied"
    )
    green_arn = service.get(operation_id).plan["green_arn"]

    def access_denied(context, resource_arn, step_name):  # noqa: ANN001, ANN202
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "not allowed to tag"}}, "AddTagsToResource"
        )

    _stub_major(monkeypatch, "tag_blue_side", access_denied)
    runner.run_once()
    final = service.get(operation_id)
    assert final.state == OperationState.FAILED
    [row] = _deployment_rows(session_factory)
    diagnostic = _diagnostic(final, "blue_green_step_aws_error")
    assert row.deployment_id in diagnostic.detail and "AccessDenied" in diagnostic.detail
    assert "left in place" in diagnostic.fix_suggestion
    assert final.plan.get("applied_without_record") is True
    with session_factory() as session:
        assert explain_event(session, green_arn, None, utc_now()) == ("operation", operation_id)


# --- fix round 2, N1: a reconcile that failed after the state move is consistent and resumes -------------


def test_a_reconcile_failing_after_the_state_move_keeps_the_blue_side_recorded_and_resumes(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.terraform.record import read_record
    from tests.drift.test_detect_executor import ORDERS_ADDRESS
    from tests.lifecycle.test_settings_changes import _recorded, _workspace_dir
    from tests.terraform.test_adoption_executor import _git

    runner, service, fake, failed = _terraform_major_upgrade(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        "major-terraform-resume",
        plan_scripts=[_orders_import_plan(["update"])],  # the import would change more than state
        state_addresses=[ORDERS_ADDRESS],
    )
    assert failed.state == OperationState.FAILED
    diagnostic = _diagnostic(failed, "reconcile_import_not_clean")
    assert ORDERS_OLD_ADDRESS in diagnostic.fix_suggestion  # it says state already moved the blue side
    assert "committed record keeps" in diagnostic.fix_suggestion
    assert "running the operation again" not in diagnostic.fix_suggestion
    assert (
        f"`cd {_workspace_dir(operator_settings)}; tofu import {ORDERS_ADDRESS} demo-orders; tofu plan`"
    ) in diagnostic.fix_suggestion
    assert fake.state_addresses == [ORDERS_OLD_ADDRESS]
    # state and the committed record agree: the blue side is recorded, so no plan proposes destroying it
    workspace = _workspace_dir(operator_settings)
    recorded = {item.identifier: item for item in read_record(workspace).render_input.instances}
    assert recorded["demo-orders-old1"].engine_version == "16.4"
    assert 'resource "aws_db_instance" "demo_orders_old1"' in (workspace / "instances.tf").read_text()
    assert not (workspace / "imports.tf").exists()
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root).strip() == ""  # committed

    # the retry finds the blue side already moved and does not move it again
    fake.calls.clear()
    fake.plan_scripts = [_orders_import_plan(["no-op"]), {"resource_changes": []}]
    executor = runner.executors["major_upgrade"]
    with session_factory() as session:
        outcome = executor.execute("reconcile", _context_for(runner, session, failed.id))
    assert outcome.status == StepOutcomeStatus.COMPLETED, outcome.detail
    assert fake.calls == [
        "init",
        "state_list",
        f"state_rm:{ORDERS_ADDRESS}",
        "plan:reconcile",
        "apply",
        "init",
        "apply_refresh_only",
        "plan:reconcile",
    ]
    assert _recorded(operator_settings, "instances", "demo-orders").engine_version == TARGET_VERSION
    assert _recorded(operator_settings, "instances", "demo-orders-old1").engine_version == "16.4"


def test_a_reconcile_refuses_when_state_holds_both_addresses_under_other_identities_and_keeps_the_blue_side(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.terraform.record import read_record
    from tests.drift.test_detect_executor import ORDERS_ADDRESS
    from tests.lifecycle.test_settings_changes import _workspace_dir
    from tests.terraform.test_adoption_executor import _git

    _, _, fake, final = _terraform_major_upgrade(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        "major-terraform-both",
        state_addresses=[ORDERS_ADDRESS, ORDERS_OLD_ADDRESS],
        # both entries still answer to the production name: not a move and import an earlier attempt did
        state_identifiers={ORDERS_ADDRESS: "demo-orders", ORDERS_OLD_ADDRESS: "demo-orders"},
    )
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "terraform_state_move_conflict")
    assert ORDERS_ADDRESS in diagnostic.detail and ORDERS_OLD_ADDRESS in diagnostic.detail
    assert "demo-orders" in diagnostic.detail
    assert f"state_show:{ORDERS_ADDRESS}" in fake.calls and f"state_show:{ORDERS_OLD_ADDRESS}" in fake.calls
    assert not any(call.startswith(("state_mv", "state_rm", "apply")) for call in fake.calls)
    # state tracks the -old1 address, so the committed record keeps it described and no plan destroys it
    workspace = _workspace_dir(operator_settings)
    identifiers = {item.identifier for item in read_record(workspace).render_input.instances}
    assert "demo-orders-old1" in identifiers
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root).strip() == ""


# --- final fix wave, B1: a resumed reconcile recognises a move and import an earlier attempt finished -------


def test_a_reconcile_resumed_after_the_import_applied_finds_both_addresses_by_identity_and_only_verifies(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import ResourceAdoption
    from tests.drift.test_detect_executor import ORDERS_ADDRESS
    from tests.lifecycle.test_settings_changes import _recorded
    from tests.terraform.test_adoption_executor import _git

    # the console stops during the verify plan, after the state move and the import were applied
    runner, service, fake, crashed = _terraform_major_upgrade(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        "major-terraform-crash-in-verify",
        plan_scripts=[_orders_import_plan(["no-op"]), RuntimeError("console stopped during the verify plan")],
        state_addresses=[ORDERS_ADDRESS],
    )
    assert crashed.state == OperationState.FAILED
    assert "apply" in fake.calls and fake.calls[-1] == "plan:reconcile"
    # what state holds then: the blue side moved, and the green instance imported at the production address
    fake.state_addresses = [ORDERS_OLD_ADDRESS, ORDERS_ADDRESS]
    fake.state_identifiers = {ORDERS_ADDRESS: "demo-orders", ORDERS_OLD_ADDRESS: "demo-orders-old1"}
    fake.calls.clear()
    fake.plan_scripts = [{"resource_changes": []}]
    executor = runner.executors["major_upgrade"]
    with session_factory() as session:
        outcome = executor.execute("reconcile", _context_for(runner, session, crashed.id))
    assert outcome.status == StepOutcomeStatus.COMPLETED, (outcome.detail, outcome.fix_suggestion)
    # nothing is moved, removed or imported again: only the clean-plan verification runs
    assert fake.calls == [
        "init",
        "state_list",
        f"state_show:{ORDERS_ADDRESS}",
        f"state_show:{ORDERS_OLD_ADDRESS}",
        "init",
        "apply_refresh_only",
        "plan:reconcile",
    ]
    assert _recorded(operator_settings, "instances", "demo-orders").engine_version == TARGET_VERSION
    assert _recorded(operator_settings, "instances", "demo-orders-old1").engine_version == "16.4"
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root).strip() == ""
    with session_factory() as session:
        adoption = session.scalars(
            select(ResourceAdoption).where(ResourceAdoption.terraform_address == ORDERS_OLD_ADDRESS)
        ).one()
        assert adoption.adoption_status == "adopted" and adoption.resource_identifier == "demo-orders-old1"


# --- final fix wave, B2: post-switchover failures in Terraform mode name the commands to finish by hand -----


def test_a_terraform_mode_record_blue_side_failure_names_the_commands_that_finish_the_reconcile_by_hand(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    import rdscommander.upgrades.major as major_module
    from tests.drift.test_detect_executor import ORDERS_ADDRESS
    from tests.lifecycle.test_settings_changes import _workspace_dir

    real_describe = major_module.instance_describe

    def another_instance_under_old1(context, identifier, step_name):  # noqa: ANN001, ANN202
        described = real_describe(context, identifier, step_name)
        if identifier == "demo-orders-old1" and described is not None:
            return {**described, "DbiResourceId": "db-SOMEONEELSE"}
        return described

    monkeypatch.setattr(major_module, "instance_describe", another_instance_under_old1)
    _, _, fake, final = _terraform_major_upgrade(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        "major-terraform-blue-side-refused",
    )
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "blue_side_not_found")
    assert (
        f"`cd {_workspace_dir(operator_settings)}; tofu state mv {ORDERS_ADDRESS} {ORDERS_OLD_ADDRESS}; "
        f"tofu import {ORDERS_ADDRESS} demo-orders; tofu plan`"
    ) in diagnostic.fix_suggestion
    assert not any(call.startswith(("state_mv", "state_rm", "apply")) for call in fake.calls)


def test_a_terraform_mode_verify_plan_that_is_not_clean_after_the_import_names_only_the_plan_left_to_run(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import ORDERS_ADDRESS
    from tests.lifecycle.test_settings_changes import _workspace_dir

    not_clean = {
        "resource_changes": [
            {
                "address": ORDERS_ADDRESS,
                "type": "aws_db_instance",
                "change": {"actions": ["update"], "before": {}, "after": {}},
            }
        ]
    }
    _, _, _, final = _terraform_major_upgrade(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        "major-terraform-verify-not-clean",
        plan_scripts=[_orders_import_plan(["no-op"]), not_clean],
        state_addresses=[ORDERS_ADDRESS],
    )
    assert final.state == OperationState.FAILED
    diagnostic = _diagnostic(final, "reconcile_not_clean")
    # the move and the import are done: what is left by hand is the plan, then Detect drift
    assert f"`cd {_workspace_dir(operator_settings)}; tofu plan`" in diagnostic.fix_suggestion
    assert "tofu import" not in diagnostic.fix_suggestion and "running the operation again" not in (
        diagnostic.fix_suggestion
    )
