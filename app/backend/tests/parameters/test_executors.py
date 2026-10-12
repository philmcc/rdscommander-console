import re

import boto3
from fastapi.testclient import TestClient

from rdscommander.control_db.models import (
    ExecutionRecord,
    InventoryDbInstance,
    InventoryParameterValue,
    Operation,
    ParameterGroupProvenance,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.main import build_executors, build_services, create_application
from rdscommander.operations.enums import OperationState
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from rdscommander.parameters.executors import UpgradeGroupBuilderExecutor
from rdscommander.resources.identity import encode_resource_id
from tests.operations.approval_helpers import approve_as_reviewed
from tests.operations.test_change_strategy import _direct_store
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import _git, _prepare_repo

WORKSPACE_FILES = ("terraform", "sandbox", "000000000000", "eu-west-2")


def _direct_runner(session_factory, operator_settings, config_store):
    _prepare_repo(operator_settings)
    store = _direct_store(config_store)
    services = build_services(operator_settings, session_factory)
    services.sleep = lambda seconds: None  # no test may really wait for AWS
    return OperationRunner(
        session_factory, build_executors(services, store), "r", operator_settings, store, services
    ), OperationService(session_factory)


def _run_to_end(runner, service, operation_id):
    runner.run_once()
    if service.get(operation_id).state == OperationState.AWAITING_APPROVAL:
        approve_as_reviewed(service, operation_id, "sam")
        runner.run_once()
    return service.get(operation_id)


def _aws_calls(session_factory, operation_id, api_name: str) -> list[ExecutionRecord]:
    """The recorded AWS calls named `api_name` (for example `rds:RebootDBInstance`) of one operation."""
    with session_factory() as session:
        return list(
            session.query(ExecutionRecord)
            .filter_by(operation_id=operation_id, record_kind="aws_api", api_name=api_name)
            .order_by(ExecutionRecord.id)
        )


def _diagnostic_kinds(operation) -> list[str]:
    return [d.diagnostic_kind for d in operation.diagnostic_reports]


def _workspace_file(operator_settings, name: str) -> str:
    """The text of a rendered workspace file; empty when the renderer removed the file."""
    path = operator_settings.repo_root.joinpath(*WORKSPACE_FILES) / name
    return path.read_text() if path.exists() else ""


def _adoption_status(session_factory, terraform_address: str) -> tuple[str, object] | None:
    """`(adoption_status, operation_id)` of the address's `resource_adoptions` row, or None."""
    from rdscommander.control_db.models import ResourceAdoption

    with session_factory() as session:
        row = session.query(ResourceAdoption).filter_by(terraform_address=terraform_address).one_or_none()
        return (row.adoption_status, row.operation_id) if row else None


def _create_options(name: str, **overrides) -> dict:
    options = {
        "target_key": TARGET_KEY,
        "name": name,
        "scope": "instance",
        "family": "postgres16",
        "description": f"{name} description",
        "source": "defaults",
        "clone_from_arn": None,
        "parameters": {},
    }
    return {"options": {**options, **overrides}}


def test_create_from_defaults_direct(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
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
                "description": "orders v2",
                "source": "defaults",
                "clone_from_arn": None,
                "parameters": {"work_mem": "32768"},
            }
        },
        "pg-create-1",
        "phil",
        title="Create parameter group: orders-pg16-v2",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    rds = boto3.client("rds", region_name="eu-west-2")
    assert (
        next(
            p["ParameterValue"]
            for p in rds.describe_db_parameters(DBParameterGroupName="orders-pg16-v2")["Parameters"]
            if p["ParameterName"] == "work_mem"
        )
        == "32768"
    )
    assert "orders-pg16-v2" in _workspace_file(operator_settings, "parameter_groups.tf")
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == "Create parameter group: orders-pg16-v2 (phil, approved by sam)"
    )
    assert final.resource_arn and final.resource_arn.endswith(":orders-pg16-v2")


def test_create_by_cloning_copies_overrides(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    source_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    operation = service.create(
        "parameter_group_create",
        "sandbox",
        TARGET_KEY,
        {
            "options": {
                "target_key": TARGET_KEY,
                "name": "tuned-copy",
                "scope": "instance",
                "family": "postgres16",
                "description": "copy",
                "source": "clone",
                "clone_from_arn": source_arn,
                "parameters": {},
            }
        },
        "pg-create-2",
        "phil",
        title="Create parameter group: tuned-copy",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    rds = boto3.client("rds", region_name="eu-west-2")
    values = {
        p["ParameterName"]: p["ParameterValue"]
        for p in rds.describe_db_parameters(DBParameterGroupName="tuned-copy")["Parameters"]
    }
    assert values["work_mem"] == "16384" and values["log_min_duration_statement"] == "500"


def test_create_with_an_invalid_value_fails_before_review_and_creates_nothing(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    operation = service.create(
        "parameter_group_create",
        "sandbox",
        TARGET_KEY,
        _create_options("bad-values", parameters={"work_mem": "abc"}),
        "pg-create-bad",
        "phil",
        title="Create parameter group: bad-values",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    assert "parameter_validation_failed" in _diagnostic_kinds(failed)
    rds = boto3.client("rds", region_name="eu-west-2")
    assert not any(
        g["DBParameterGroupName"] == "bad-values"
        for g in rds.describe_db_parameter_groups()["DBParameterGroups"]
    )


def test_create_with_a_name_in_the_inventory_fails_before_review_with_a_parameter_group_exists_diagnostic(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    operation = service.create(
        "parameter_group_create",
        "sandbox",
        TARGET_KEY,
        _create_options("demo-postgres16-tuned"),
        "pg-create-exists",
        "phil",
        title="Create parameter group: demo-postgres16-tuned",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    assert "parameter_group_exists" in _diagnostic_kinds(failed)
    assert "change_set" not in failed.plan  # no change set was shown for approval


def test_edit_validates_before_review_and_applies(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    bad = service.create(
        "parameter_group_edit",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameters": {"work_mem": "abc"}, "reset": []}, "resource_arn": group_arn},
        "pg-edit-bad",
        "phil",
        resource_arn=group_arn,
        title="Edit parameter group: demo-postgres16-tuned",
    )
    runner.run_once()
    failed = service.get(bad.id)
    assert failed.state == OperationState.FAILED
    assert any(
        d.diagnostic_kind == "parameter_validation_failed" and "work_mem must be an integer" in d.detail
        for d in failed.diagnostic_reports
    )
    good = service.create(
        "parameter_group_edit",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameters": {"work_mem": "65536"}, "reset": []}, "resource_arn": group_arn},
        "pg-edit-good",
        "phil",
        resource_arn=group_arn,
        title="Edit parameter group: demo-postgres16-tuned",
    )
    final = _run_to_end(runner, service, good.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    with session_factory() as session:
        latest = (
            session.query(InventoryParameterValue)
            .filter_by(parameter_name="work_mem")
            .order_by(InventoryParameterValue.id.desc())
            .first()
        )
        assert latest.parameter_value == "65536"


def test_edit_of_a_static_parameter_is_flagged_in_the_plan_and_applies_at_the_next_reboot(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    operation = service.create(
        "parameter_group_edit",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"parameters": {"max_connections": "200"}, "reset": []},
            "resource_arn": group_arn,
        },
        "pg-edit-static",
        "phil",
        resource_arn=group_arn,
        title="Edit parameter group: demo-postgres16-tuned",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL
    assert awaiting.plan["requires_reboot"] is True
    assert "reboot" in awaiting.plan["downtime"]
    [entry] = awaiting.plan["change_set"]
    assert entry["attribute"] == "parameters.max_connections" and entry["apply_type"] == "pending-reboot"
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    stored = {
        p["ParameterName"]: p
        for p in boto3.client("rds", region_name="eu-west-2").describe_db_parameters(
            DBParameterGroupName="demo-postgres16-tuned"
        )["Parameters"]
    }
    assert stored["max_connections"]["ApplyMethod"] == "pending-reboot"


def test_edit_reset_in_the_simulator_records_a_warning_and_sets_the_family_default(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    operation = service.create(
        "parameter_group_edit",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"parameters": {}, "reset": ["log_min_duration_statement"]},
            "resource_arn": group_arn,
        },
        "pg-edit-reset",
        "phil",
        resource_arn=group_arn,
        title="Edit parameter group: demo-postgres16-tuned",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    warning = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "simulator_unsupported")
    assert warning.severity == "warning" and "log_min_duration_statement" in warning.detail
    live = {
        p["ParameterName"]: p["ParameterValue"]
        for p in boto3.client("rds", region_name="eu-west-2").describe_db_parameters(
            DBParameterGroupName="demo-postgres16-tuned"
        )["Parameters"]
    }
    assert live["log_min_duration_statement"] == "-1"  # the family default


def test_edit_rejects_a_parameter_that_is_both_set_and_reset(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    operation = service.create(
        "parameter_group_edit",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"parameters": {"work_mem": "65536"}, "reset": ["work_mem"]},
            "resource_arn": group_arn,
        },
        "pg-edit-conflict",
        "phil",
        resource_arn=group_arn,
        title="Edit parameter group: demo-postgres16-tuned",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    assert any(
        d.diagnostic_kind == "parameter_validation_failed"
        and "work_mem cannot be both set and reset" in d.detail
        for d in failed.diagnostic_reports
    )


def test_attach_with_reboot_now_reboots_and_waits(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "attach-refresh", "phil")
    runner.run_once()
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "now"}, "resource_arn": orders_arn},
        "pg-attach-1",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [a.step_name for a in final.step_attempts][-3:] == [
        "reboot_instances",
        "await_in_sync",
        "record_to_checkout",
    ]
    assert (
        rds.describe_db_instances(DBInstanceIdentifier="demo-orders")["DBInstances"][0]["DBParameterGroups"][
            0
        ]["DBParameterGroupName"]
        == "orders-pg16-v2"
    )
    assert _aws_calls(session_factory, final.id, "rds:RebootDBInstance")


def test_attach_without_a_reboot_does_not_reboot(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "attach-refresh", "phil")
    runner.run_once()
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "none"}, "resource_arn": orders_arn},
        "pg-attach-2",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "reboot_instances" not in [a.step_name for a in final.step_attempts]
    assert not _aws_calls(session_factory, final.id, "rds:RebootDBInstance")


def test_attach_of_a_cluster_group_to_a_cluster_uses_the_cluster_api(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_cluster_parameter_group(
        DBClusterParameterGroupName="aurora-v2",
        DBParameterGroupFamily="aurora-postgresql16",
        Description="v2",
    )
    group_arn = rds.describe_db_cluster_parameter_groups(DBClusterParameterGroupName="aurora-v2")[
        "DBClusterParameterGroups"
    ][0]["DBClusterParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "attach-cluster-refresh", "phil")
    runner.run_once()
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "none"}, "resource_arn": cluster_arn},
        "pg-attach-cluster",
        "phil",
        resource_arn=cluster_arn,
        title="Attach parameter group: demo-aurora",
        approval_required_text="demo-aurora",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    cluster = rds.describe_db_clusters(DBClusterIdentifier="demo-aurora")["DBClusters"][0]
    assert cluster["DBClusterParameterGroup"] == "aurora-v2"


def test_delete_unused_group_direct(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="unused-pg16", DBParameterGroupFamily="postgres16", Description="unused"
    )
    service.create("inventory_refresh", "sandbox", None, {}, "delete-refresh", "phil")
    runner.run_once()
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="unused-pg16")["DBParameterGroups"][0][
        "DBParameterGroupArn"
    ]
    operation = service.create(
        "parameter_group_delete",
        "sandbox",
        TARGET_KEY,
        {"options": {}, "resource_arn": group_arn},
        "pg-delete-1",
        "phil",
        resource_arn=group_arn,
        title="Delete parameter group: unused-pg16",
        approval_required_text="unused-pg16",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert rds.describe_db_parameter_groups()["DBParameterGroups"] and not any(
        g["DBParameterGroupName"] == "unused-pg16"
        for g in rds.describe_db_parameter_groups()["DBParameterGroups"]
    )


def test_delete_of_a_group_in_use_fails_before_review_and_deletes_nothing(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    operation = service.create(
        "parameter_group_delete",
        "sandbox",
        TARGET_KEY,
        {"options": {}, "resource_arn": group_arn},
        "pg-delete-in-use",
        "phil",
        resource_arn=group_arn,
        title="Delete parameter group: demo-postgres16-tuned",
        approval_required_text="demo-postgres16-tuned",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    assert "parameter_group_in_use" in _diagnostic_kinds(failed)
    rds = boto3.client("rds", region_name="eu-west-2")
    assert any(
        g["DBParameterGroupName"] == "demo-postgres16-tuned"
        for g in rds.describe_db_parameter_groups()["DBParameterGroups"]
    )


def test_reboot_instance_requires_approval_and_records_the_call(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "reboot_instance",
        "sandbox",
        TARGET_KEY,
        {"options": {"force_failover": False}, "resource_arn": orders_arn},
        "reboot-1",
        "phil",
        resource_arn=orders_arn,
        title="Reboot instance: demo-orders",
        approval_required_text="demo-orders",
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED
    assert _aws_calls(session_factory, final.id, "rds:RebootDBInstance")


def test_reboot_of_a_parameter_group_reboots_only_the_instances_pending_a_reboot(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        session.query(InventoryDbInstance).filter_by(
            snapshot_id=snapshot.id, db_instance_identifier="demo-orders"
        ).update({"parameter_apply_status": "pending-reboot"})
        session.commit()
    operation = service.create(
        "reboot_instance",
        "sandbox",
        TARGET_KEY,
        {"options": {"force_failover": False}, "resource_arn": group_arn},
        "reboot-group",
        "phil",
        resource_arn=group_arn,
        title="Reboot instance: demo-postgres16-tuned",
        approval_required_text="demo-postgres16-tuned",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    rebooted = [
        call.request_parameters["DBInstanceIdentifier"]
        for call in _aws_calls(session_factory, final.id, "rds:RebootDBInstance")
    ]
    assert rebooted == ["demo-orders"]


def test_reboot_that_does_not_come_back_available_fails_with_a_diagnostic(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.parameters import direct_api

    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    runner.settings = runner.settings.model_copy(update={"direct_verify_timeout_seconds": 0})
    monkeypatch.setattr(direct_api, "instance_status", lambda context, identifier, step_name="": "rebooting")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "reboot_instance",
        "sandbox",
        TARGET_KEY,
        {"options": {"force_failover": False}, "resource_arn": orders_arn},
        "reboot-stuck",
        "phil",
        resource_arn=orders_arn,
        title="Reboot instance: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.FAILED
    assert "instance_not_available" in _diagnostic_kinds(final)


def test_edit_in_terraform_mode_goes_through_plan_and_apply(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group
    from tests.terraform.test_adoption_executor import FakeTerraformRunner

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    update = {
        "resource_changes": [
            {
                "address": "aws_db_parameter_group.demo_postgres16_tuned",
                "type": "aws_db_parameter_group",
                "change": {
                    "actions": ["update"],
                    "before": {"parameter": [{"name": "work_mem", "value": "16384"}]},
                    "after": {
                        "parameter": [
                            {"name": "work_mem", "value": "65536"},
                            {"name": "max_connections", "value": "200"},
                        ]
                    },
                },
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    operation = service.create(
        "parameter_group_edit",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"parameters": {"work_mem": "65536", "max_connections": "200"}, "reset": []},
            "resource_arn": group_arn,
        },
        "pg-edit-tf",
        "phil",
        resource_arn=group_arn,
        title="Edit parameter group: demo-postgres16-tuned",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL
    # Terraform mode computes no change set, so the change itself says a static parameter needs a reboot.
    assert awaiting.plan["requires_reboot"] is True
    assert "reboot" in awaiting.plan["downtime"] and "demo-orders" in awaiting.plan["downtime"]
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    rendered = _workspace_file(operator_settings, "parameter_groups.tf")
    assert "65536" in rendered and "200" in rendered
    assert re.search(
        r'name\s*=\s*"max_connections"\s*value\s*=\s*"200"\s*apply_method\s*=\s*"pending-reboot"', rendered
    )


RENDERED_PARAMETER = re.compile(
    r'parameter\s*\{\s*name\s*=\s*"([^"]+)"\s*value\s*=\s*"([^"]*)"\s*apply_method\s*=\s*"([^"]+)"'
)


def _terraform_runner_that_applies_the_rendered_group(recorder, plan_scripts):
    """A scripted Terraform runner whose apply writes the rendered parameters to the simulator, like tofu."""
    from tests.terraform.test_adoption_executor import FakeTerraformRunner

    fake = FakeTerraformRunner(recorder, plan_scripts=plan_scripts)
    scripted_apply = fake.apply

    def apply(workspace_dir, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        rendered = (workspace_dir / "parameter_groups.tf").read_text()
        boto3.client("rds", region_name="eu-west-2").modify_db_parameter_group(
            DBParameterGroupName="demo-postgres16-tuned",
            Parameters=[
                {"ParameterName": name, "ParameterValue": value, "ApplyMethod": method, "Source": "user"}
                for name, value, method in RENDERED_PARAMETER.findall(rendered)
            ],
        )
        return scripted_apply(workspace_dir, *args, **kwargs)

    fake.apply = apply
    return fake


def _rendered_value(operator_settings, name: str) -> str | None:
    rendered = _workspace_file(operator_settings, "parameter_groups.tf")
    return next((value for found, value, _ in RENDERED_PARAMETER.findall(rendered) if found == name), None)


def test_a_second_terraform_mode_edit_of_the_same_group_keeps_the_first_edit(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """The second edit renders the group from the inventory, which must already show the first edit.

    Nothing refreshes the inventory between the two edits except the edits themselves; a render from the
    snapshot taken before the first edit would put work_mem back to 16384 and the plan would revert it.
    """
    from tests.drift.test_detect_executor import _adopt_group

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    update = {
        "resource_changes": [
            {
                "address": "aws_db_parameter_group.demo_postgres16_tuned",
                "type": "aws_db_parameter_group",
                "change": {"actions": ["update"], "before": {}, "after": {}},
            }
        ]
    }
    clean = {"resource_changes": []}
    runner.services.terraform_runner = _terraform_runner_that_applies_the_rendered_group(
        runner.services.execution_recorder, [update, update, clean, update, update, clean]
    )

    def edit(parameters: dict[str, str], key: str):  # noqa: ANN202
        return service.create(
            "parameter_group_edit",
            "sandbox",
            TARGET_KEY,
            {"options": {"parameters": parameters, "reset": []}, "resource_arn": group_arn},
            key,
            "phil",
            resource_arn=group_arn,
            title="Edit parameters: demo-postgres16-tuned",
        )

    first = edit({"work_mem": "65536"}, "tf-edit-first")
    assert _run_to_end(runner, service, first.id).state == OperationState.SUCCEEDED
    second = edit({"log_min_duration_statement": "250"}, "tf-edit-second")
    runner.run_once()
    assert service.get(second.id).state == OperationState.AWAITING_APPROVAL
    assert _rendered_value(operator_settings, "log_min_duration_statement") == "250"
    assert _rendered_value(operator_settings, "work_mem") == "65536", (
        "the second edit must not revert the first"
    )
    approve_as_reviewed(service, second.id, "sam")
    runner.run_once()
    final = service.get(second.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    from rdscommander.terraform.record import read_record

    recorded = read_record(operator_settings.repo_root.joinpath(*WORKSPACE_FILES)).render_input
    [group] = [g for g in recorded.instance_parameter_groups if g.name == "demo-postgres16-tuned"]
    assert {p.name: p.value for p in group.parameters}["work_mem"] == "65536"


def test_after_a_change_that_could_not_refresh_the_next_edit_waits_for_a_refresh(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    """A refresh that failed after a commit leaves the inventory behind the change.

    Rendering from it would undo the change, so the next change on the target asks for Refresh inventory.
    """
    from rdscommander.operations import change_strategy as change_strategy_module
    from tests.drift.test_detect_executor import _adopt_group

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    update = {
        "resource_changes": [
            {
                "address": "aws_db_parameter_group.demo_postgres16_tuned",
                "type": "aws_db_parameter_group",
                "change": {"actions": ["update"], "before": {}, "after": {}},
            }
        ]
    }
    clean = {"resource_changes": []}
    runner.services.terraform_runner = _terraform_runner_that_applies_the_rendered_group(
        runner.services.execution_recorder, [update, update, clean, update, update, clean]
    )

    def edit(parameters: dict[str, str], key: str):  # noqa: ANN202
        return service.create(
            "parameter_group_edit",
            "sandbox",
            TARGET_KEY,
            {"options": {"parameters": parameters, "reset": []}, "resource_arn": group_arn},
            key,
            "phil",
            resource_arn=group_arn,
            title="Edit parameters: demo-postgres16-tuned",
        )

    def unreachable(context, target, step_name=None):  # noqa: ANN001, ANN202
        raise RuntimeError("the RDS endpoint could not be reached")

    with monkeypatch.context() as patched:
        patched.setattr(change_strategy_module, "refresh_target_now", unreachable)
        first = edit({"work_mem": "65536"}, "tf-unrefreshed-first")
        assert _run_to_end(runner, service, first.id).state == OperationState.SUCCEEDED
    blocked = edit({"log_min_duration_statement": "250"}, "tf-unrefreshed-second")
    runner.run_once()
    refused = service.get(blocked.id)
    assert refused.state == OperationState.FAILED
    [diagnostic] = [d for d in refused.diagnostic_reports if d.diagnostic_kind == "inventory_behind_change"]
    assert str(first.id) in diagnostic.detail and "Refresh inventory" in diagnostic.fix_suggestion
    assert _rendered_value(operator_settings, "work_mem") == "65536", "nothing was rendered over the change"
    service.create("inventory_refresh", "sandbox", None, {}, "tf-unrefreshed-refresh", "phil")
    runner.run_once()
    retried = edit({"log_min_duration_statement": "250"}, "tf-unrefreshed-third")
    runner.run_once()
    assert service.get(retried.id).state == OperationState.AWAITING_APPROVAL
    assert _rendered_value(operator_settings, "work_mem") == "65536"
    assert _rendered_value(operator_settings, "log_min_duration_statement") == "250"


def test_edit_of_dynamic_parameters_in_terraform_mode_does_not_flag_a_reboot(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group
    from tests.terraform.test_adoption_executor import FakeTerraformRunner

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    update = {
        "resource_changes": [
            {
                "address": "aws_db_parameter_group.demo_postgres16_tuned",
                "type": "aws_db_parameter_group",
                "change": {"actions": ["update"], "before": {}, "after": {}},
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    operation = service.create(
        "parameter_group_edit",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameters": {"work_mem": "65536"}, "reset": []}, "resource_arn": group_arn},
        "pg-edit-tf-dynamic",
        "phil",
        resource_arn=group_arn,
        title="Edit parameter group: demo-postgres16-tuned",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL
    assert awaiting.plan["requires_reboot"] is False and awaiting.plan["downtime"] == "none"


def test_create_in_terraform_mode_allows_the_plain_create_of_the_new_group(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group
    from tests.terraform.test_adoption_executor import FakeTerraformRunner

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    created = {
        "resource_changes": [
            {
                "address": "aws_db_parameter_group.orders_pg16_v2",
                "type": "aws_db_parameter_group",
                "change": {"actions": ["create"], "before": None, "after": {"name": "orders-pg16-v2"}},
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[created, created, {"resource_changes": []}]
    )
    operation = service.create(
        "parameter_group_create",
        "sandbox",
        TARGET_KEY,
        _create_options("orders-pg16-v2", parameters={"work_mem": "32768"}),
        "pg-create-tf",
        "phil",
        title="Create parameter group: orders-pg16-v2",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    rendered = _workspace_file(operator_settings, "parameter_groups.tf")
    assert "orders-pg16-v2" in rendered and "32768" in rendered
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == "Create parameter group: orders-pg16-v2 (phil, approved by sam)"
    )
    # Created through Terraform, the group is in state: it counts as adopted, whatever its tags say.
    assert _adoption_status(session_factory, "aws_db_parameter_group.orders_pg16_v2") == (
        "adopted",
        operation.id,
    )


def test_delete_in_terraform_mode_allows_the_plain_delete_of_the_unused_group(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt
    from tests.terraform.test_adoption_executor import FakeTerraformRunner

    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="unused-pg16", DBParameterGroupFamily="postgres16", Description="unused"
    )
    services = build_services(operator_settings, session_factory)
    refresh_runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        operator_settings,
        config_store,
        services,
    )
    OperationService(session_factory).create(
        "inventory_refresh", "sandbox", None, {}, "delete-tf-refresh", "phil"
    )
    refresh_runner.run_once()
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        session.expunge_all()
    runner, service = _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        snapshot,
        ["unused-pg16"],
        ["aws_db_parameter_group.unused_pg16"],
        "delete-tf-adopt",
    )
    group_arn = arn_for(session_factory, snapshot, "unused-pg16")
    deleted = {
        "resource_changes": [
            {
                "address": "aws_db_parameter_group.unused_pg16",
                "type": "aws_db_parameter_group",
                "change": {"actions": ["delete"], "before": {"name": "unused-pg16"}, "after": None},
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[deleted, deleted, {"resource_changes": []}]
    )
    operation = service.create(
        "parameter_group_delete",
        "sandbox",
        TARGET_KEY,
        {"options": {}, "resource_arn": group_arn},
        "pg-delete-tf",
        "phil",
        resource_arn=group_arn,
        title="Delete parameter group: unused-pg16",
        approval_required_text="unused-pg16",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "unused-pg16" not in _workspace_file(operator_settings, "parameter_groups.tf")
    # Destroyed through Terraform, the group has left state: it no longer counts as adopted.
    assert _adoption_status(session_factory, "aws_db_parameter_group.unused_pg16") == (
        "removed",
        operation.id,
    )


def test_delete_of_a_group_in_use_in_terraform_mode_fails_before_any_file_is_written(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    operation = service.create(
        "parameter_group_delete",
        "sandbox",
        TARGET_KEY,
        {"options": {}, "resource_arn": group_arn},
        "pg-delete-tf-in-use",
        "phil",
        resource_arn=group_arn,
        title="Delete parameter group: demo-postgres16-tuned",
        approval_required_text="demo-postgres16-tuned",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    assert "parameter_group_in_use" in _diagnostic_kinds(failed)
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def _start_terraform_attach(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    *,
    family: str = "postgres16",
    reboot: str = "now",
):
    """An adopted estate, a new group in the inventory and a created attach of `demo-orders` to it.

    Returns the runner, the service and the operation, which has not run yet.
    """
    from tests.drift.test_detect_executor import _adopt_estate
    from tests.terraform.test_adoption_executor import FakeTerraformRunner

    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    runner.services.sleep = lambda seconds: None
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily=family, Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "attach-tf-refresh", "phil")
    runner.run_once()
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    update = {
        "resource_changes": [
            {
                "address": "aws_db_instance.demo_orders",
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"parameter_group_name": "demo-postgres16-tuned"},
                    "after": {"parameter_group_name": "orders-pg16-v2"},
                },
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": reboot}, "resource_arn": orders_arn},
        "pg-attach-tf",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    return runner, service, operation


def test_attach_with_reboot_now_in_terraform_mode_commits_before_the_reboot_and_points_at_the_group(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, operation = _start_terraform_attach(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL
    assert awaiting.plan["requires_reboot"] is True and awaiting.plan["downtime"] == "a reboot now"
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [a.step_name for a in final.step_attempts][-5:] == [
        "verify_clean_plan",
        "commit_to_checkout",
        "reboot_instances",
        "await_in_sync",
        "refresh_resource",
    ], "the refresh comes last, so the inventory sees the instance after its reboot"
    assert re.search(
        r'parameter_group_name\s*=\s*"orders-pg16-v2"', _workspace_file(operator_settings, "instances.tf")
    )
    assert _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root) == (
        "Attach parameter group: demo-orders (phil, approved by sam)"
    )
    assert _aws_calls(session_factory, final.id, "rds:RebootDBInstance")


def test_attach_without_a_reboot_in_terraform_mode_says_static_parameters_stay_pending(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, operation = _start_terraform_attach(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot, reboot="none"
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.plan["requires_reboot"] is False
    assert "until the next reboot" in awaiting.plan["downtime"]


def test_attach_of_a_group_of_another_family_in_terraform_mode_fails_before_any_file_is_written(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, operation = _start_terraform_attach(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        family="postgres15",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    diagnostic = next(
        d for d in failed.diagnostic_reports if d.diagnostic_kind == "parameter_group_not_attachable"
    )
    assert "family postgres15" in diagnostic.detail and "demo-postgres16-tuned" in diagnostic.detail
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""
    assert not _aws_calls(session_factory, failed.id, "rds:RebootDBInstance")


def test_a_reboot_that_does_not_reach_in_sync_advises_by_mode_in_terraform_mode(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service, operation = _start_terraform_attach(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    runner.settings = runner.settings.model_copy(update={"direct_verify_timeout_seconds": 0})
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "reboot_not_in_sync")
    assert "files are committed" in diagnostic.fix_suggestion
    assert "reboot from the instance page" in diagnostic.fix_suggestion
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == "Attach parameter group: demo-orders (phil, approved by sam)"
    )  # the commit is made before the reboot, so the failure cannot lose it


def test_create_by_cloning_across_families_drops_missing_parameters_and_warns(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="legacy-pg15", DBParameterGroupFamily="postgres15", Description="legacy"
    )
    rds.modify_db_parameter_group(
        DBParameterGroupName="legacy-pg15",
        Parameters=[
            {"ParameterName": "work_mem", "ParameterValue": "8192", "ApplyMethod": "immediate"},
            {"ParameterName": "force_parallel_mode", "ParameterValue": "off", "ApplyMethod": "immediate"},
        ],
    )
    service.create("inventory_refresh", "sandbox", None, {}, "clone-refresh", "phil")
    runner.run_once()
    legacy_arn = rds.describe_db_parameter_groups(DBParameterGroupName="legacy-pg15")["DBParameterGroups"][0][
        "DBParameterGroupArn"
    ]
    operation = service.create(
        "parameter_group_create",
        "sandbox",
        TARGET_KEY,
        _create_options("modern-pg16", source="clone", clone_from_arn=legacy_arn),
        "pg-create-cross-family",
        "phil",
        title="Create parameter group: modern-pg16",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    warning = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "clone_parameters_dropped")
    assert warning.severity == "warning"
    assert "force_parallel_mode" in warning.detail and "postgres16" in warning.detail
    values = {
        p["ParameterName"]: p["ParameterValue"]
        for p in rds.describe_db_parameters(DBParameterGroupName="modern-pg16")["Parameters"]
    }
    assert values == {"work_mem": "8192"}


def test_create_in_terraform_mode_refuses_a_name_the_inventory_has_before_any_file_is_written(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    operation = service.create(
        "parameter_group_create",
        "sandbox",
        TARGET_KEY,
        _create_options("demo-postgres16-tuned"),
        "pg-create-tf-exists",
        "phil",
        title="Create parameter group: demo-postgres16-tuned",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    diagnostic = next(d for d in failed.diagnostic_reports if d.diagnostic_kind == "parameter_group_exists")
    assert "inventory" in diagnostic.detail
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def test_create_in_terraform_mode_refuses_a_name_the_record_has_even_when_the_inventory_lost_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt

    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="unused-pg16", DBParameterGroupFamily="postgres16", Description="unused"
    )
    services = build_services(operator_settings, session_factory)
    refresh_runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        operator_settings,
        config_store,
        services,
    )
    OperationService(session_factory).create(
        "inventory_refresh", "sandbox", None, {}, "record-refresh", "phil"
    )
    refresh_runner.run_once()
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        session.expunge_all()
    runner, service = _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        snapshot,
        ["unused-pg16"],
        ["aws_db_parameter_group.unused_pg16"],
        "record-adopt",
    )
    rds.delete_db_parameter_group(DBParameterGroupName="unused-pg16")  # gone from AWS, still in the record
    service.create("inventory_refresh", "sandbox", None, {}, "record-refresh-2", "phil")
    runner.run_once()
    operation = service.create(
        "parameter_group_create",
        "sandbox",
        TARGET_KEY,
        _create_options("unused-pg16"),
        "pg-create-tf-record",
        "phil",
        title="Create parameter group: unused-pg16",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    diagnostic = next(d for d in failed.diagnostic_reports if d.diagnostic_kind == "parameter_group_exists")
    assert "record" in diagnostic.detail
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def test_attach_of_a_group_of_another_family_in_direct_mode_fails_before_review(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="legacy-pg15", DBParameterGroupFamily="postgres15", Description="legacy"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="legacy-pg15")["DBParameterGroups"][0][
        "DBParameterGroupArn"
    ]
    service.create("inventory_refresh", "sandbox", None, {}, "family-refresh", "phil")
    runner.run_once()
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "none"}, "resource_arn": orders_arn},
        "pg-attach-family",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    assert "parameter_group_not_attachable" in _diagnostic_kinds(failed)
    assert "change_set" not in failed.plan


def test_attach_reboot_waits_for_a_modifying_instance_before_rebooting(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.parameters import direct_api

    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    sleeps: list[float] = []
    runner.services.sleep = sleeps.append
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "wait-refresh", "phil")
    runner.run_once()
    real_status = direct_api.instance_status
    reads: list[str] = []

    def status_that_settles(context, identifier, step_name="x"):  # noqa: ANN001, ANN202
        reads.append(step_name)
        if step_name == "reboot_instances" and reads.count("reboot_instances") == 1:
            return "modifying"
        return real_status(context, identifier, step_name)

    monkeypatch.setattr(direct_api, "instance_status", status_that_settles)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "now"}, "resource_arn": orders_arn},
        "pg-attach-wait",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert reads.count("reboot_instances") == 2 and sleeps  # read, saw modifying, slept, read again
    assert _aws_calls(session_factory, final.id, "rds:RebootDBInstance")


def test_attach_reboot_fails_when_the_instance_never_settles_and_reboots_nothing(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.parameters import direct_api

    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    runner.settings = runner.settings.model_copy(update={"direct_verify_timeout_seconds": 0})
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "stuck-refresh", "phil")
    runner.run_once()
    monkeypatch.setattr(direct_api, "instance_status", lambda context, identifier, step_name="": "modifying")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "now"}, "resource_arn": orders_arn},
        "pg-attach-stuck",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "instance_not_ready_for_reboot"
    )
    assert "demo-orders is modifying" in diagnostic.detail
    assert "refresh inventory, then reboot from the instance page" in diagnostic.fix_suggestion
    assert not _aws_calls(session_factory, final.id, "rds:RebootDBInstance")
    assert final.plan.get("applied_without_record") is True


def test_a_reboot_that_does_not_reach_in_sync_advises_by_mode_in_direct_mode(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    runner.settings = runner.settings.model_copy(update={"direct_verify_timeout_seconds": 0})
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "sync-refresh", "phil")
    runner.run_once()
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "now"}, "resource_arn": orders_arn},
        "pg-attach-sync",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "reboot_not_in_sync")
    assert "refresh inventory, then reboot from the instance page" in diagnostic.fix_suggestion
    assert "files are committed" not in diagnostic.fix_suggestion
    assert _aws_calls(
        session_factory, final.id, "rds:RebootDBInstance"
    )  # it was rebooted, just not seen in sync


def test_direct_attach_of_a_group_created_after_the_record_commits_the_group_with_the_instance(
    direct_mode_runner, session_factory, operator_settings, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.terraform.record import read_record

    runner, service, _ = direct_mode_runner
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "dangling-refresh", "phil")
    runner.run_once()
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    workspace = operator_settings.repo_root.joinpath(*WORKSPACE_FILES)
    assert "orders-pg16-v2" not in [
        g.name for g in read_record(workspace).render_input.instance_parameter_groups
    ]
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "none"}, "resource_arn": orders_arn},
        "pg-attach-dangling",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "aws_db_parameter_group.orders_pg16_v2" in final.plan["touched_addresses"]
    recorded = read_record(workspace).render_input
    assert "orders-pg16-v2" in [g.name for g in recorded.instance_parameter_groups]
    orders = next(i for i in recorded.instances if i.identifier == "demo-orders")
    assert orders.parameter_group_reference == "aws_db_parameter_group.orders_pg16_v2.name"
    assert 'resource "aws_db_parameter_group" "orders_pg16_v2"' in _workspace_file(
        operator_settings, "parameter_groups.tf"
    )
    assert "aws_db_parameter_group.orders_pg16_v2.name" in _workspace_file(operator_settings, "instances.tf")


UPGRADE_BUILD_KIND = "parameter_group_build_for_upgrade"


def _create_upgrade_build(
    service, resource_arn: str, resource_kind: str, key: str, title: str, **options_overrides
):
    """A builder operation as `POST /operations` would make it: the plan carries `resource_kind`."""
    options = {"target_engine_version": "17.2", "name_suffix": "", "overrides": {}, **options_overrides}
    return service.create(
        UPGRADE_BUILD_KIND,
        "sandbox",
        TARGET_KEY,
        {"options": options, "resource_arn": resource_arn, "resource_kind": resource_kind},
        key,
        "phil",
        resource_arn=resource_arn,
        title=title,
    )


def _instance_group_names() -> set[str]:
    rds = boto3.client("rds", region_name="eu-west-2")
    return {
        group["DBParameterGroupName"] for group in rds.describe_db_parameter_groups()["DBParameterGroups"]
    }


def _application_client(operator_settings, config, session_factory) -> tuple[TestClient, str]:
    client = TestClient(
        create_application(settings=operator_settings, console_config=config, session_factory=session_factory)
    )
    return client, client.get("/api/v1/bootstrap").json()["csrf_token"]


def test_upgrade_builder_creates_instance_and_cluster_groups_with_provenance(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    client, token = _application_client(operator_settings, runner.config_store.current, session_factory)
    preview = client.post(
        "/api/v1/parameters/upgrade-preview",
        headers={"X-CSRF-Token": token},
        json={"resource_id": encode_resource_id(aurora_arn), "target_engine_version": "17.2"},
    ).json()
    assert {p["scope"] for p in preview["plans"]} == {"instance", "cluster"}
    cluster_plan = next(p for p in preview["plans"] if p["scope"] == "cluster")
    assert (
        cluster_plan["target_family"] == "aurora-postgresql17"
        and cluster_plan["new_group_name"] == "demo-aurora-pg16-tuned-pg17"
    )
    assert {c["name"]: c["outcome"] for c in cluster_plan["classifications"]} == {
        "log_min_duration_statement": "carried",
        "rds.force_ssl": "carried",
        "rds.logical_replication": "carried",
    }
    # The members use the AWS default instance group: the new instance group is named after the cluster.
    instance_plan = next(p for p in preview["plans"] if p["scope"] == "instance")
    assert instance_plan["source_group_name"] == "default.aurora-postgresql16"
    assert instance_plan["new_group_name"] == "demo-aurora-pg17"
    assert instance_plan["classifications"] == [] and instance_plan["parameters_to_set"] == {}
    operation = _create_upgrade_build(
        service,
        aurora_arn,
        "cluster",
        "upgrade-build-1",
        "Build upgrade parameter groups: demo-aurora",
        overrides={"cluster": {"log_min_duration_statement": "2000"}},
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL
    # One review covers every group: the change set at the pause holds the entries of both.
    reviewed = awaiting.plan["change_set"]
    assert {entry["address"] for entry in reviewed} == {
        "aws_db_parameter_group.demo_aurora_pg17",
        "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned_pg17",
    }
    assert {
        entry["desired"]
        for entry in reviewed
        if entry["address"].startswith("aws_rds_cluster_parameter_group")
        and entry["attribute"] == "parameters.log_min_duration_statement"
    } == {"2000"}  # the review shows the operator's edit
    assert not [key for key in awaiting.plan if ":" in key]  # the plan keys are not split per group
    assert "demo-aurora-pg17" not in _instance_group_names()  # nothing is created before approval
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    rds = boto3.client("rds", region_name="eu-west-2")
    cluster_values = {
        p["ParameterName"]: p["ParameterValue"]
        for p in rds.describe_db_cluster_parameters(
            DBClusterParameterGroupName="demo-aurora-pg16-tuned-pg17"
        )["Parameters"]
    }
    assert cluster_values["log_min_duration_statement"] == "2000" and cluster_values["rds.force_ssl"] == "1"
    instance_group = rds.describe_db_parameter_groups(DBParameterGroupName="demo-aurora-pg17")[
        "DBParameterGroups"
    ][0]
    assert instance_group["DBParameterGroupFamily"] == "aurora-postgresql17"
    with session_factory() as session:
        rows = {
            row.group_scope: row
            for row in session.query(ParameterGroupProvenance).filter_by(operation_id=operation.id)
        }
    assert {scope: row.target_family for scope, row in rows.items()} == {
        "instance": "aurora-postgresql17",
        "cluster": "aurora-postgresql17",
    }
    assert rows["instance"].source_group_names == ["default.aurora-postgresql16"]
    assert rows["cluster"].source_group_names == ["demo-aurora-pg16-tuned"]
    assert {c["name"]: c["final_value"] for c in rows["cluster"].classification} == {
        "log_min_duration_statement": "2000",  # the operator's edit on the review step wins
        "rds.force_ssl": "1",
        "rds.logical_replication": "1",
    }
    assert not [key for key in final.plan if ":" in key]
    detail = client.get(f"/api/v1/operations/{operation.id}").json()
    assert {entry["address"] for entry in detail["change_set"]} == {
        "aws_db_parameter_group.demo_aurora_pg17",
        "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned_pg17",
    }
    assert set(detail["touched_addresses"]) == {
        "aws_db_parameter_group.demo_aurora_pg17",
        "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned_pg17",
    }
    new_cluster_group_arn = rds.describe_db_cluster_parameter_groups(
        DBClusterParameterGroupName="demo-aurora-pg16-tuned-pg17"
    )["DBClusterParameterGroups"][0]["DBClusterParameterGroupArn"]
    page = client.get(f"/api/v1/resources/{encode_resource_id(new_cluster_group_arn)}").json()
    provenance = page["record"]["facts"]["provenance"]
    assert provenance["source_group_names"] == ["demo-aurora-pg16-tuned"]
    assert provenance["target_family"] == "aurora-postgresql17"
    assert provenance["operation_id"] == str(operation.id)
    assert {c["name"] for c in provenance["classification"]} == {
        "log_min_duration_statement",
        "rds.force_ssl",
        "rds.logical_replication",
    }
    source_group_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora-pg16-tuned")
    other_page = client.get(f"/api/v1/resources/{encode_resource_id(source_group_arn)}").json()
    assert "provenance" not in other_page["record"]["facts"]
    subjects = _git("log", "--format=%s", cwd=operator_settings.repo_root).splitlines()
    assert subjects.count("Build upgrade parameter groups: demo-aurora (phil, approved by sam)") == 1
    # The operation stays linked to the cluster it was built for, not to the group created last.
    assert final.resource_arn == aurora_arn
    source_page = client.get(f"/api/v1/resources/{encode_resource_id(aurora_arn)}").json()
    assert str(operation.id) in {summary["id"] for summary in source_page["recent_operations"]}


def test_upgrade_builder_for_an_instance_builds_one_group_from_its_tuned_group(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_upgrade_build(
        service, orders_arn, "instance", "upgrade-build-orders", "Build upgrade parameter groups: demo-orders"
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    rds = boto3.client("rds", region_name="eu-west-2")
    group = rds.describe_db_parameter_groups(DBParameterGroupName="demo-postgres16-tuned-pg17")[
        "DBParameterGroups"
    ][0]
    assert group["DBParameterGroupFamily"] == "postgres17"
    values = {
        p["ParameterName"]: p["ParameterValue"]
        for p in rds.describe_db_parameters(DBParameterGroupName="demo-postgres16-tuned-pg17", Source="user")[
            "Parameters"
        ]
    }
    assert values == {
        "log_min_duration_statement": "500",
        "rds.logical_replication": "1",
        "shared_buffers": "{DBInstanceClassMemory/32768}",
        "work_mem": "16384",
    }
    assert {entry["address"] for entry in final.plan["change_set"]} == {
        "aws_db_parameter_group.demo_postgres16_tuned_pg17"
    }
    assert final.resource_arn == orders_arn
    with session_factory() as session:
        [row] = session.query(ParameterGroupProvenance).filter_by(operation_id=operation.id).all()
    assert row.group_scope == "instance" and row.source_group_names == ["demo-postgres16-tuned"]


def test_upgrade_builder_fails_before_creating_anything_when_a_new_group_name_is_taken(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_cluster_parameter_group(
        DBClusterParameterGroupName="demo-aurora-pg16-tuned-pg17",
        DBParameterGroupFamily="aurora-postgresql17",
        Description="taken",
    )
    service.create("inventory_refresh", "sandbox", None, {}, "upgrade-taken-refresh", "phil")
    runner.run_once()
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = _create_upgrade_build(
        service, aurora_arn, "cluster", "upgrade-build-taken", "Build upgrade parameter groups: demo-aurora"
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    diagnostic = next(d for d in failed.diagnostic_reports if d.diagnostic_kind == "parameter_group_exists")
    assert "demo-aurora-pg16-tuned-pg17" in diagnostic.detail
    # The instance group came first in the plan, but the whole plan was refused before it was created.
    assert "demo-aurora-pg17" not in _instance_group_names()
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def test_upgrade_builder_refuses_an_edited_value_the_target_family_rejects_before_review(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = _create_upgrade_build(
        service,
        aurora_arn,
        "cluster",
        "upgrade-build-invalid",
        "Build upgrade parameter groups: demo-aurora",
        overrides={"cluster": {"log_min_duration_statement": "abc"}},
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert (
        failed.state == OperationState.FAILED
    )  # not AWAITING_APPROVAL: the instance group is not built first
    assert "parameter_validation_failed" in _diagnostic_kinds(failed)
    assert "demo-aurora-pg17" not in _instance_group_names()


def test_upgrade_builder_refuses_edits_for_a_group_the_operation_does_not_build(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_upgrade_build(
        service,
        orders_arn,
        "instance",
        "upgrade-build-stray-edit",
        "Build upgrade parameter groups: demo-orders",
        overrides={"cluster": {"work_mem": "8192"}},
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    diagnostic = next(d for d in failed.diagnostic_reports if d.diagnostic_kind == "change_not_possible")
    assert "cluster" in diagnostic.detail


def test_upgrade_builder_runs_the_strategy_once_for_all_groups_in_both_modes(
    session_factory, operator_settings, config_store, console_config
) -> None:
    operation = Operation(operation_kind=UPGRADE_BUILD_KIND, target_key=TARGET_KEY, plan={})
    assert UpgradeGroupBuilderExecutor(_direct_store(config_store)).step_names(operation) == [
        "check_checkout",
        "build_plan",
        "compute_change_set",
        "policy_check",
        "await_approval",
        "apply_direct",
        "verify_direct",
        "refresh_resource",
        "record_to_checkout",
        "record_provenance",
    ]
    assert UpgradeGroupBuilderExecutor(config_store).step_names(operation) == [
        "check_checkout",
        "build_plan",
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
        "record_provenance",
    ]


def test_upgrade_builder_in_terraform_mode_plans_reviews_and_commits_all_groups_together(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group
    from tests.terraform.test_adoption_executor import FakeTerraformRunner

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    creation = {
        "resource_changes": [
            {
                "address": address,
                "type": address.split(".")[0],
                "change": {"actions": ["create"], "before": None, "after": {"name": name}},
            }
            for address, name in (
                ("aws_db_parameter_group.demo_aurora_pg17", "demo-aurora-pg17"),
                (
                    "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned_pg17",
                    "demo-aurora-pg16-tuned-pg17",
                ),
            )
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder,
        plan_scripts=[creation, creation, {"resource_changes": []}],
    )
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = _create_upgrade_build(
        service,
        aurora_arn,
        "cluster",
        "upgrade-build-tf",
        "Build upgrade parameter groups: demo-aurora",
        overrides={"instance": {"max_connections": "200"}},  # static: only the instance group needs a reboot
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL
    # The one plan that is approved creates both groups, and the reboot one of them needs is flagged.
    planned = {
        change["address"]: change["actions"] for change in awaiting.plan["plan_summary"]["resource_changes"]
    }
    assert planned == {
        "aws_db_parameter_group.demo_aurora_pg17": ["create"],
        "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned_pg17": ["create"],
    }
    assert awaiting.plan["requires_reboot"] is True and "reboot" in awaiting.plan["downtime"]
    assert set(awaiting.plan["touched_addresses"]) == set(planned)
    assert not [key for key in awaiting.plan if ":" in key]
    assert [g["new_group_name"] for g in awaiting.plan["upgrade_plans"]] == [
        "demo-aurora-pg17",
        "demo-aurora-pg16-tuned-pg17",
    ]
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert runner.services.terraform_runner.calls.count("apply") == 1
    assert 'name        = "demo-aurora-pg17"' in _workspace_file(operator_settings, "parameter_groups.tf")
    cluster_groups = _workspace_file(operator_settings, "cluster_parameter_groups.tf")
    assert "demo-aurora-pg16-tuned-pg17" in cluster_groups and "rds.force_ssl" in cluster_groups
    subjects = _git("log", "--format=%s", cwd=operator_settings.repo_root).splitlines()
    assert subjects.count("Build upgrade parameter groups: demo-aurora (phil, approved by sam)") == 1
    with session_factory() as session:
        recorded = {
            row.group_scope: row.group_name
            for row in session.query(ParameterGroupProvenance).filter_by(operation_id=operation.id)
        }
    assert recorded == {"instance": "demo-aurora-pg17", "cluster": "demo-aurora-pg16-tuned-pg17"}
    assert final.resource_arn == aurora_arn
    client, _ = _application_client(operator_settings, console_config, session_factory)
    source_page = client.get(f"/api/v1/resources/{encode_resource_id(aurora_arn)}").json()
    assert str(operation.id) in {summary["id"] for summary in source_page["recent_operations"]}


def test_upgrade_builder_in_terraform_mode_refuses_a_taken_name_before_any_file_is_written(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_group

    runner, service = _adopt_group(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned-pg17",
        DBParameterGroupFamily="postgres17",
        Description="taken",
    )
    service.create("inventory_refresh", "sandbox", None, {}, "upgrade-tf-taken-refresh", "phil")
    runner.run_once()
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = _create_upgrade_build(
        service,
        orders_arn,
        "instance",
        "upgrade-build-tf-taken",
        "Build upgrade parameter groups: demo-orders",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    assert "parameter_group_exists" in _diagnostic_kinds(failed)
    assert _git("status", "--porcelain", cwd=operator_settings.repo_root) == ""


def test_a_build_that_failed_after_the_first_group_is_completed_by_building_again(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.parameters import direct_api
    from rdscommander.terraform.record import read_record

    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    create_group = direct_api.create_group

    def cluster_creation_fails(context, group_name, scope, *arguments, **keywords):
        if scope == "cluster":
            raise RuntimeError("AWS is unavailable")
        return create_group(context, group_name, scope, *arguments, **keywords)

    monkeypatch.setattr(direct_api, "create_group", cluster_creation_fails)
    first = _create_upgrade_build(
        service, aurora_arn, "cluster", "upgrade-build-first", "Build upgrade parameter groups: demo-aurora"
    )
    failed = _run_to_end(runner, service, first.id)
    assert failed.state == OperationState.FAILED
    diagnostic = next(
        d for d in failed.diagnostic_reports if d.diagnostic_kind == "upgrade_groups_partially_created"
    )
    assert "demo-aurora-pg17" in diagnostic.detail and "demo-aurora-pg16-tuned-pg17" in diagnostic.detail
    assert "build the groups again" in diagnostic.fix_suggestion
    assert failed.plan["applied_without_record"] is True
    assert "demo-aurora-pg17" in _instance_group_names()
    with session_factory() as session:
        [row] = session.query(ParameterGroupProvenance).filter_by(operation_id=first.id).all()
    assert (row.group_scope, row.group_name) == ("instance", "demo-aurora-pg17")

    monkeypatch.setattr(direct_api, "create_group", create_group)  # AWS is back
    second = _create_upgrade_build(
        service, aurora_arn, "cluster", "upgrade-build-second", "Build upgrade parameter groups: demo-aurora"
    )
    runner.run_once()
    awaiting = service.get(second.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert {g["new_group_name"]: g["already_built"] for g in awaiting.plan["upgrade_plans"]} == {
        "demo-aurora-pg17": True,
        "demo-aurora-pg16-tuned-pg17": False,
    }
    assert {entry["address"] for entry in awaiting.plan["change_set"]} == {
        "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned_pg17"
    }
    approve_as_reviewed(service, second.id, "sam")
    runner.run_once()
    final = service.get(second.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert len(_aws_calls(session_factory, second.id, "rds:CreateDBParameterGroup")) == 0
    assert len(_aws_calls(session_factory, second.id, "rds:CreateDBClusterParameterGroup")) == 1
    with session_factory() as session:
        rows = {
            (row.group_scope, row.group_name): row.operation_id
            for row in session.query(ParameterGroupProvenance).all()
        }
    assert rows == {
        ("instance", "demo-aurora-pg17"): first.id,
        ("cluster", "demo-aurora-pg16-tuned-pg17"): second.id,
    }
    # The second build also put the group the first one created into the record.
    workspace = operator_settings.repo_root.joinpath(*WORKSPACE_FILES)
    recorded = read_record(workspace).render_input
    assert "demo-aurora-pg17" in [g.name for g in recorded.instance_parameter_groups]
    assert "demo-aurora-pg16-tuned-pg17" in [g.name for g in recorded.cluster_parameter_groups]


def test_upgrade_builder_refuses_a_second_build_when_every_group_exists_already(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    first = _create_upgrade_build(
        service, orders_arn, "instance", "upgrade-build-once", "Build upgrade parameter groups: demo-orders"
    )
    assert _run_to_end(runner, service, first.id).state == OperationState.SUCCEEDED
    again = _create_upgrade_build(
        service, orders_arn, "instance", "upgrade-build-twice", "Build upgrade parameter groups: demo-orders"
    )
    runner.run_once()
    failed = service.get(again.id)
    assert failed.state == OperationState.FAILED
    diagnostic = next(
        d for d in failed.diagnostic_reports if d.diagnostic_kind == "upgrade_plan_not_possible"
    )
    assert "demo-postgres16-tuned-pg17" in diagnostic.detail


def test_direct_attach_keeps_reading_while_aws_is_still_applying_the_group(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.parameters import direct_api

    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    sleeps: list[float] = []
    runner.services.sleep = sleeps.append
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "applying-refresh", "phil")
    runner.run_once()
    real_attached_group = direct_api.attached_group
    verify_statuses = iter(["applying", "pending-reboot"])
    verify_reads: list[str] = []

    def describe_that_is_still_applying(context, identifier, is_cluster, step_name="verify_direct"):  # noqa: ANN001, ANN202
        group_name, status = real_attached_group(context, identifier, is_cluster, step_name)
        if step_name != "verify_direct":
            return group_name, status
        verify_reads.append(next(verify_statuses, "pending-reboot"))
        return group_name, verify_reads[-1]

    monkeypatch.setattr(direct_api, "attached_group", describe_that_is_still_applying)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "none"}, "resource_arn": orders_arn},
        "pg-attach-applying",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert verify_reads == ["applying", "pending-reboot"] and len(sleeps) == 1
    verify = next(a for a in final.step_attempts if a.step_name == "verify_direct" and a.outcome_status)
    assert verify.output["live"]["apply_status"] == "pending-reboot"


def test_direct_attach_stops_reading_when_aws_failed_to_apply_the_group_and_names_the_status(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    """`failed-to-apply` never turns into `in-sync` by waiting, so verification fails at once."""
    from rdscommander.parameters import direct_api

    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    sleeps: list[float] = []
    runner.services.sleep = _sleep_that_refuses_to_wait_long(sleeps)
    runner.settings = runner.settings.model_copy(update={"direct_verify_timeout_seconds": 1800})
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "failed-to-apply-refresh", "phil")
    runner.run_once()
    real_attached_group = direct_api.attached_group
    verify_reads: list[str] = []

    def describe_that_failed_to_apply(context, identifier, is_cluster, step_name="verify_direct"):  # noqa: ANN001, ANN202
        group_name, status = real_attached_group(context, identifier, is_cluster, step_name)
        if step_name != "verify_direct":
            return group_name, status
        verify_reads.append("failed-to-apply")
        return group_name, "failed-to-apply"

    monkeypatch.setattr(direct_api, "attached_group", describe_that_failed_to_apply)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "none"}, "resource_arn": orders_arn},
        "pg-attach-failed-to-apply",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.FAILED
    assert verify_reads == ["failed-to-apply"] and sleeps == []
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "direct_change_not_verified"]
    assert "orders-pg16-v2" in diagnostic.detail and "failed-to-apply" in diagnostic.detail
    assert "check the resource's status and events in AWS" in diagnostic.fix_suggestion


def _sleep_that_refuses_to_wait_long(sleeps: list[float]):  # noqa: ANN202
    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise AssertionError("the wait went on although the instance reached a terminal status")

    return sleep


def test_a_reboot_stops_waiting_when_the_instance_reaches_a_terminal_status(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.parameters import direct_api

    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    sleeps: list[float] = []
    runner.services.sleep = _sleep_that_refuses_to_wait_long(sleeps)
    runner.settings = runner.settings.model_copy(update={"direct_verify_timeout_seconds": 1800})
    monkeypatch.setattr(
        direct_api,
        "instance_status",
        lambda context, identifier, step_name="": (
            "incompatible-parameters" if step_name == "await_settled" else "available"
        ),
    )
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "reboot_instance",
        "sandbox",
        TARGET_KEY,
        {"options": {"force_failover": False}, "resource_arn": orders_arn},
        "reboot-terminal",
        "phil",
        resource_arn=orders_arn,
        title="Reboot instance: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "instance_not_available")
    assert "demo-orders is incompatible-parameters" in diagnostic.detail
    assert "will not recover from by waiting" in diagnostic.detail
    assert sleeps == []


def test_an_attach_reboot_stops_waiting_for_in_sync_when_the_instance_reaches_a_terminal_status(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.parameters import direct_api

    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    sleeps: list[float] = []
    runner.services.sleep = _sleep_that_refuses_to_wait_long(sleeps)
    runner.settings = runner.settings.model_copy(update={"direct_verify_timeout_seconds": 1800})
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    group_arn = rds.describe_db_parameter_groups(DBParameterGroupName="orders-pg16-v2")["DBParameterGroups"][
        0
    ]["DBParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, "terminal-refresh", "phil")
    runner.run_once()
    real_status = direct_api.instance_status

    def status_after_the_reboot(context, identifier, step_name="x"):  # noqa: ANN001, ANN202
        if step_name == "await_in_sync":
            return "storage-full"
        return real_status(context, identifier, step_name)

    monkeypatch.setattr(direct_api, "instance_status", status_after_the_reboot)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": "now"}, "resource_arn": orders_arn},
        "pg-attach-terminal",
        "phil",
        resource_arn=orders_arn,
        title="Attach parameter group: demo-orders",
        approval_required_text="demo-orders",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "reboot_not_in_sync")
    assert "demo-orders is storage-full" in diagnostic.detail
    assert sleeps == []


def test_a_reboot_of_a_foreign_owned_instance_is_refused_before_approval(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        session.query(InventoryDbInstance).filter_by(
            snapshot_id=snapshot.id, db_instance_identifier="demo-orders"
        ).update({"ownership": "foreign_cloudformation", "parameter_apply_status": "pending-reboot"})
        session.commit()
    finals = []
    for key, resource_arn in (("reboot-foreign", orders_arn), ("reboot-foreign-group", group_arn)):
        operation = service.create(
            "reboot_instance",
            "sandbox",
            TARGET_KEY,
            {"options": {"force_failover": False}, "resource_arn": resource_arn},
            key,
            "phil",
            resource_arn=resource_arn,
            title="Reboot",
        )
        runner.run_once()
        finals.append(service.get(operation.id))
    for final in finals:
        assert final.state == OperationState.FAILED
        diagnostic = next(
            d for d in final.diagnostic_reports if d.diagnostic_kind == "foreign_owned_resource"
        )
        assert "demo-orders is foreign cloudformation" in diagnostic.detail
        assert "allow direct changes to foreign-owned resources" in diagnostic.fix_suggestion
        assert not _aws_calls(session_factory, final.id, "rds:RebootDBInstance")


def test_a_reboot_of_a_foreign_owned_instance_goes_ahead_where_the_environment_allows_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    runner.config_store.current.environment("sandbox").allow_direct_changes_to_foreign_owned = True
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        session.query(InventoryDbInstance).filter_by(
            snapshot_id=snapshot.id, db_instance_identifier="demo-orders"
        ).update({"ownership": "foreign_cloudformation"})
        session.commit()
    operation = service.create(
        "reboot_instance",
        "sandbox",
        TARGET_KEY,
        {"options": {"force_failover": False}, "resource_arn": orders_arn},
        "reboot-foreign-allowed",
        "phil",
        resource_arn=orders_arn,
        title="Reboot",
    )
    final = _run_to_end(runner, service, operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert _aws_calls(session_factory, final.id, "rds:RebootDBInstance")


def _attach_a_new_cluster_group_to_demo_aurora(
    runner, service, session_factory, sandbox_snapshot, key, reboot
):
    """Create a cluster group, refresh, then ask to attach it to `demo-aurora` with the given reboot choice.

    Before the operation is created, the cluster's reader instance is marked as owned by CloudFormation in
    the latest inventory, as tooling other than the console would tag it.
    """
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_cluster_parameter_group(
        DBClusterParameterGroupName=f"aurora-{key}",
        DBParameterGroupFamily="aurora-postgresql16",
        Description="v2",
    )
    group_arn = rds.describe_db_cluster_parameter_groups(DBClusterParameterGroupName=f"aurora-{key}")[
        "DBClusterParameterGroups"
    ][0]["DBClusterParameterGroupArn"]
    service.create("inventory_refresh", "sandbox", None, {}, f"{key}-refresh", "phil")
    runner.run_once()
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        session.query(InventoryDbInstance).filter_by(
            snapshot_id=snapshot.id, db_instance_identifier="demo-aurora-reader"
        ).update({"ownership": "foreign_cloudformation"})
        session.commit()
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    return service.create(
        "parameter_group_attach",
        "sandbox",
        TARGET_KEY,
        {"options": {"parameter_group_arn": group_arn, "reboot": reboot}, "resource_arn": cluster_arn},
        key,
        "phil",
        resource_arn=cluster_arn,
        title="Attach parameter group: demo-aurora",
        approval_required_text="demo-aurora",
    )


def test_a_cluster_attach_that_reboots_is_refused_before_approval_when_a_member_is_foreign_owned(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """The reboot touches every member, so each member gets the reboot's foreign-ownership refusal."""
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    operation = _attach_a_new_cluster_group_to_demo_aurora(
        runner, service, session_factory, sandbox_snapshot, "attach-cluster-reboot-foreign", "now"
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED and final.approved_at_utc is None
    [diagnostic] = [d for d in final.diagnostic_reports if d.diagnostic_kind == "foreign_owned_resource"]
    assert "demo-aurora-reader is foreign cloudformation" in diagnostic.detail
    assert "demo-aurora-writer" not in diagnostic.detail
    assert "allow direct changes to foreign-owned resources" in diagnostic.fix_suggestion
    assert not _aws_calls(session_factory, final.id, "rds:ModifyDBCluster")
    assert not _aws_calls(session_factory, final.id, "rds:RebootDBInstance")


def test_a_cluster_attach_with_a_foreign_owned_member_goes_ahead_without_a_reboot_or_where_allowed(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """No reboot touches no member; with a reboot, the environment's setting decides, as for reboots."""
    runner, service = _direct_runner(session_factory, operator_settings, config_store)
    without_reboot = _attach_a_new_cluster_group_to_demo_aurora(
        runner, service, session_factory, sandbox_snapshot, "attach-cluster-no-reboot-foreign", "none"
    )
    runner.run_once()
    assert service.get(without_reboot.id).state == OperationState.AWAITING_APPROVAL
    service.reject(without_reboot.id, "sam")
    runner.config_store.current.environment("sandbox").allow_direct_changes_to_foreign_owned = True
    allowed = _attach_a_new_cluster_group_to_demo_aurora(
        runner, service, session_factory, sandbox_snapshot, "attach-cluster-reboot-allowed", "now"
    )
    runner.run_once()
    assert service.get(allowed.id).state == OperationState.AWAITING_APPROVAL
