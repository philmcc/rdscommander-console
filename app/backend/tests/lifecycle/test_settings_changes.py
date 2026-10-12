"""Instance class, storage and Multi-AZ as attribute changes."""

from __future__ import annotations

import re

import boto3
import pytest
from pydantic import ValidationError

from rdscommander.control_db.models import ExecutionRecord
from rdscommander.lifecycle.options import (
    BackupSettingsOptions,
    DeletionProtectionOptions,
    InstanceClassChangeOptions,
    MaintenanceWindowOptions,
    MonitoringOptions,
    MultiAzChangeOptions,
    ServerlessCapacityOptions,
    StorageChangeOptions,
    TagsOptions,
)
from rdscommander.operations.enums import OperationState
from rdscommander.operations.service import OperationService
from rdscommander.terraform.record import read_record
from tests.drift.test_detect_executor import _adopt_estate
from tests.operations.approval_helpers import approve_as_reviewed
from tests.operations.test_change_strategy import (
    _direct_store,
    _git,
    _runner,
    _workspace_dir,
)
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _prepare_repo


def test_instance_class_must_look_like_an_rds_class():
    assert InstanceClassChangeOptions(instance_class="db.m6g.xlarge").apply_at == "now"
    with pytest.raises(ValidationError, match="db\\."):
        InstanceClassChangeOptions(instance_class="m6g.xlarge")


def test_an_aurora_member_can_move_to_serverless_v2_as_the_class_picker_offers():
    # DescribeOrderableDBInstanceOptions offers db.serverless for aurora-postgresql; the pattern refused it.
    from rdscommander.lifecycle.options import ClusterReaderCreateOptions

    assert InstanceClassChangeOptions(instance_class="db.serverless").instance_class == "db.serverless"
    reader = ClusterReaderCreateOptions.model_validate(
        {"reader_identifier": "demo-aurora-reader-2", "instance_class": "db.serverless"}
    )
    assert reader.instance_class == "db.serverless"
    with pytest.raises(ValidationError, match="db.serverless"):
        InstanceClassChangeOptions(instance_class="serverless")
    with pytest.raises(ValidationError, match="db.serverless"):
        InstanceClassChangeOptions(instance_class="db.serverlessv2")


def test_storage_needs_at_least_one_setting_and_iops_only_with_provisioned_types():
    with pytest.raises(ValidationError, match="at least one"):
        StorageChangeOptions()
    with pytest.raises(ValidationError, match="io1 or io2"):
        StorageChangeOptions(storage_type="gp2", iops=3000)
    assert StorageChangeOptions(allocated_storage_gb=200).allocated_storage_gb == 200


def test_multi_az_options_carry_the_flag():
    assert MultiAzChangeOptions(multi_az=True).multi_az is True


def _run(
    session_factory,
    operator_settings,
    config_store,
    sandbox_snapshot,
    kind,
    identifier,
    options,
    resource_arn=None,
):
    """Create the operation in direct mode and run it; stops after the first run when it fails there.

    `resource_arn` names the resource directly (a snapshot, which `arn_for` does not look up).
    """
    if not (operator_settings.repo_root / ".git").exists():
        _prepare_repo(operator_settings)
    runner, services = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    arn = resource_arn or arn_for(session_factory, sandbox_snapshot, identifier)
    operation = service.create(
        kind,
        "sandbox",
        TARGET_KEY,
        {"options": options, "resource_arn": arn},
        f"{kind}-1",
        "phil",
        resource_arn=arn,
        title=kind,
    )
    runner.run_once()
    if service.get(operation.id).state == OperationState.FAILED:
        return service.get(operation.id), services
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    return service.get(operation.id), services


def test_instance_class_change_direct_writes_the_class_and_records_it(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "instance_class_change",
        "demo-orders",
        {"instance_class": "db.m6g.xlarge"},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    entry = finished.plan["change_set"][0]
    assert (entry["attribute"], entry["current"], entry["desired"]) == (
        "instance_class",
        "db.m6g.large",
        "db.m6g.xlarge",
    )
    assert "restart" in entry["downtime"]
    record = read_record(_workspace_dir(operator_settings))
    assert (
        next(i for i in record.render_input.instances if i.identifier == "demo-orders").instance_class
        == "db.m6g.xlarge"
    )


def test_storage_change_refuses_to_shrink_naming_the_current_size(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "storage_change",
        "demo-orders",
        {"allocated_storage_gb": 50},
    )
    assert finished.state == OperationState.FAILED
    [diagnostic] = [d for d in finished.diagnostic_reports if d.diagnostic_kind == "storage_cannot_shrink"]
    assert "100 GiB" in diagnostic.detail
    assert "100 GiB" in finished.failure_summary


def test_storage_change_direct_grows_the_allocated_storage(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "storage_change",
        "demo-orders",
        {"allocated_storage_gb": 200},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    entry = finished.plan["change_set"][0]
    assert (entry["attribute"], entry["current"], entry["desired"]) == ("allocated_storage", 100, 200)


def test_multi_az_change_direct_turns_the_standby_off(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "multi_az_change",
        "demo-orders",
        {"multi_az": False},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert finished.plan["change_set"][0]["desired"] is False


def test_instance_class_change_in_terraform_mode_renders_plans_and_commits(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
):
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    update = {
        "resource_changes": [
            {
                "address": "aws_db_instance.demo_orders",
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"instance_class": "db.m6g.large"},
                    "after": {"instance_class": "db.m6g.xlarge"},
                },
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "instance_class_change",
        "sandbox",
        TARGET_KEY,
        {"options": {"instance_class": "db.m6g.xlarge"}, "resource_arn": arn},
        "class-tf",
        "phil",
        resource_arn=arn,
        title="Change instance class: demo-orders",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL
    assert "restart" in awaiting.plan["downtime"]
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    rendered = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    assert re.search(r'instance_class\s*=\s*"db\.m6g\.xlarge"', rendered)
    assert (
        _git("log", "-1", "--format=%s", cwd=operator_settings.repo_root)
        == "Change instance class: demo-orders (phil, approved by sam)"
    )


def _make_moto_accept_a_zero_ceiling_like_aws(monkeypatch):
    """AWS turns autoscaling off for MaxAllocatedStorage=0; moto 5.x rejects it as below the storage size."""
    from moto.rds.models import DBInstance

    original = DBInstance.max_allocated_storage

    def set_ceiling(self, value):
        original.fset(self, self.allocated_storage if value == 0 else value)

    monkeypatch.setattr(DBInstance, "max_allocated_storage", original.setter(set_ceiling))


def test_storage_change_turns_autoscaling_off_by_sending_a_zero_ceiling(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
):
    _make_moto_accept_a_zero_ceiling_like_aws(monkeypatch)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_instance(DBInstanceIdentifier="demo-orders", MaxAllocatedStorage=500, ApplyImmediately=True)
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "storage_change",
        "demo-orders",
        {"max_allocated_storage_gb": 0},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    entry = finished.plan["change_set"][0]
    assert (entry["attribute"], entry["current"], entry["desired"]) == ("max_allocated_storage", 500, 0)
    with session_factory() as session:
        modify_requests = [
            record.request_parameters
            for record in session.query(ExecutionRecord)
            .filter_by(operation_id=finished.id, api_name="rds:ModifyDBInstance")
            .all()
        ]
    assert [r for r in modify_requests if r.get("MaxAllocatedStorage") == 0]
    assert not rds.describe_db_instances(DBInstanceIdentifier="demo-orders")["DBInstances"][0].get(
        "MaxAllocatedStorage"
    )


def test_storage_change_options_pass_zero_to_the_attribute_values():
    from rdscommander.lifecycle.settings_changes import StorageChangeExecutor
    from rdscommander.resources.model import ResourceKind, ResourceRecord

    record = ResourceRecord(
        kind=ResourceKind.INSTANCE,
        arn="arn:aws:rds:eu-west-2:000000000000:db:x",
        resource_id="x",
        identifier="x",
        environment_slug="sandbox",
        target_key=TARGET_KEY,
        ownership="unmanaged",
        status="available",
        facts={"allocated_storage_gb": 100},
        snapshot_id=1,
    )
    values = StorageChangeExecutor(None).attribute_values(
        StorageChangeOptions(max_allocated_storage_gb=0), record
    )
    assert values == {"max_allocated_storage": 0}


def test_windows_are_validated_with_a_readable_message():
    assert MaintenanceWindowOptions(preferred_maintenance_window="sun:03:00-sun:04:00")
    with pytest.raises(ValidationError, match="ddd:hh:mm-ddd:hh:mm"):
        MaintenanceWindowOptions(preferred_maintenance_window="sunday 3am")
    with pytest.raises(ValidationError, match="hh:mm-hh:mm"):
        BackupSettingsOptions(backup_retention_period_days=7, preferred_backup_window="3-4")


def test_monitoring_options_leave_the_role_to_the_executor_which_knows_the_live_one():
    # The options cannot know whether the instance already has a role; the executor decides.
    assert (
        MonitoringOptions(
            performance_insights_enabled=True, monitoring_interval_seconds=60
        ).monitoring_role_arn
        is None
    )
    assert (
        MonitoringOptions(
            performance_insights_enabled=False, monitoring_interval_seconds=0
        ).monitoring_role_arn
        is None
    )


def test_performance_insights_retention_is_seven_days_or_a_multiple_of_31():
    for retention in (7, 31, 62, 731):
        assert MonitoringOptions(
            performance_insights_enabled=True,
            performance_insights_retention_period_days=retention,
            monitoring_interval_seconds=0,
        )
    with pytest.raises(ValidationError, match="multiple of 31"):
        MonitoringOptions(
            performance_insights_enabled=True,
            performance_insights_retention_period_days=30,
            monitoring_interval_seconds=0,
        )


def test_serverless_capacity_is_ordered_and_in_half_acu_steps():
    with pytest.raises(ValidationError, match="at or above"):
        ServerlessCapacityOptions(min_capacity_acu=4, max_capacity_acu=2)
    with pytest.raises(ValidationError, match="0.5"):
        ServerlessCapacityOptions(min_capacity_acu=0.75, max_capacity_acu=2)


def test_tags_never_touch_console_or_aws_prefixes_and_need_something_to_do():
    with pytest.raises(ValidationError, match="rdscommander:"):
        TagsOptions(set_tags={"rdscommander:managed": "false"})
    with pytest.raises(ValidationError, match="aws:"):
        TagsOptions(remove_tags=["aws:cloudformation:stack-name"])
    with pytest.raises(ValidationError, match="nothing to change"):
        TagsOptions()
    assert TagsOptions(set_tags={"team": "orders"}, remove_tags=["old"]).remove_tags == ["old"]


def test_deletion_protection_options_carry_the_flag():
    assert DeletionProtectionOptions(deletion_protection=True).deletion_protection is True


def _recorded(operator_settings, section: str, identifier: str):
    """The rendered resource named `identifier` in the committed record."""
    record = read_record(_workspace_dir(operator_settings))
    return next(item for item in getattr(record.render_input, section) if item.identifier == identifier)


def _recorded_group(operator_settings, name: str):
    record = read_record(_workspace_dir(operator_settings))
    return next(g for g in record.render_input.instance_parameter_groups if g.name == name)


def test_backup_settings_change_direct_sets_retention_and_window(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "backup_settings_change",
        "demo-orders",
        {"backup_retention_period_days": 14, "preferred_backup_window": "02:00-03:00"},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    desired = {e["attribute"]: e["desired"] for e in finished.plan["change_set"]}
    assert desired == {"backup_retention_period": 14, "preferred_backup_window": "02:00-03:00"}
    assert _recorded(operator_settings, "instances", "demo-orders").backup_retention_period == 14


def test_backup_settings_change_refuses_to_turn_backups_off_for_a_source_of_replicas(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "backup_settings_change",
        "demo-orders",
        {"backup_retention_period_days": 0},
    )
    assert finished.state == OperationState.FAILED
    [diagnostic] = [
        d for d in finished.diagnostic_reports if d.diagnostic_kind == "backups_required_for_replicas"
    ]
    assert "demo-orders-replica" in diagnostic.detail


def test_backup_settings_change_refuses_a_cluster_member_and_points_at_the_cluster(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "backup_settings_change",
        "demo-aurora-writer",
        {"backup_retention_period_days": 7},
    )
    assert finished.state == OperationState.FAILED
    [diagnostic] = [
        d for d in finished.diagnostic_reports if d.diagnostic_kind == "backups_live_on_the_cluster"
    ]
    assert "set this on the cluster" in diagnostic.detail


def test_a_cluster_backup_change_explains_the_same_change_on_its_members(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    # AWS reports a cluster's backup retention and window on every member too; the walkthrough's History
    # listed the members' copies as changed outside the console.
    from rdscommander.control_db.models import utc_now
    from rdscommander.history.change_events import explain_event

    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "backup_settings_change",
        "demo-aurora",
        {"backup_retention_period_days": 14},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    member_arns = {
        arn_for(session_factory, sandbox_snapshot, member)
        for member in ("demo-aurora-writer", "demo-aurora-reader")
    }
    assert set(finished.plan["affected_resource_arns"]) == member_arns
    with session_factory() as session:
        for member_arn in member_arns:
            assert explain_event(session, member_arn, None, utc_now()) == ("operation", finished.id)


def test_maintenance_window_change_direct_updates_the_cluster_record(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "maintenance_window_change",
        "demo-aurora",
        {"preferred_maintenance_window": "sun:03:00-sun:04:00"},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert (
        _recorded(operator_settings, "clusters", "demo-aurora").preferred_maintenance_window
        == "sun:03:00-sun:04:00"
    )


def _make_moto_report_performance_insights_like_aws(monkeypatch):
    """AWS describes PerformanceInsightsEnabled; moto 5.x accepts the request but never reports it."""
    from moto.rds.models import DBInstance

    monkeypatch.setattr(
        DBInstance,
        "performance_insights_enabled",
        property(lambda self: bool(getattr(self, "enable_performance_insights", False))),
        raising=False,
    )


def test_monitoring_change_direct_turns_performance_insights_on(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
):
    _make_moto_report_performance_insights_like_aws(monkeypatch)
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "monitoring_change",
        "demo-orders",
        {"performance_insights_enabled": True, "monitoring_interval_seconds": 0},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    attributes = {e["attribute"] for e in finished.plan["change_set"]}
    assert "performance_insights_enabled" in attributes
    assert "monitoring_role_arn" not in attributes
    assert _recorded(operator_settings, "instances", "demo-orders").performance_insights_enabled is True


def test_deletion_protection_change_direct_protects_the_cluster(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "deletion_protection_change",
        "demo-aurora",
        {"deletion_protection": True},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert _recorded(operator_settings, "clusters", "demo-aurora").deletion_protection is True


def test_tags_change_direct_tags_a_parameter_group_and_keeps_the_console_tag(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    rds = boto3.client("rds", region_name="eu-west-2")
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    rds.add_tags_to_resource(
        ResourceName=group_arn,
        Tags=[{"Key": "rdscommander:managed", "Value": "true"}, {"Key": "old", "Value": "x"}],
    )
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "tags_change",
        "demo-postgres16-tuned",
        {"set_tags": {"team": "orders"}, "remove_tags": ["old"]},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    live = {t["Key"]: t["Value"] for t in rds.list_tags_for_resource(ResourceName=group_arn)["TagList"]}
    assert live["team"] == "orders" and "old" not in live and live["rdscommander:managed"] == "true"
    tags = _recorded_group(operator_settings, "demo-postgres16-tuned").tags
    assert tags["team"] == "orders" and "old" not in tags
    assert "rdscommander:managed" not in tags  # the rendered model leaves console tags to the provider


def test_serverless_capacity_change_is_refused_for_a_cluster_that_is_not_serverless(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
):
    from fastapi.testclient import TestClient

    from rdscommander.main import create_application
    from rdscommander.resources.identity import encode_resource_id

    direct_config = console_config.model_copy(deep=True)
    direct_config.environment("sandbox").change_management = "direct"
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=direct_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    refused = client.post(
        "/api/v1/operations",
        headers={"X-CSRF-Token": token},
        json={
            "kind": "serverless_capacity_change",
            "environment_slug": "sandbox",
            "resource_id": encode_resource_id(arn_for(session_factory, sandbox_snapshot, "demo-aurora")),
            "options": {"min_capacity_acu": 0.5, "max_capacity_acu": 4},
            "idempotency_key": "serverless-1",
        },
    )
    assert refused.status_code == 422
    assert "serverless" in refused.text


def test_serverless_capacity_change_sends_both_ends_of_the_range():
    from rdscommander.lifecycle.settings_changes import ServerlessCapacityChangeExecutor
    from rdscommander.resources.model import ResourceKind, ResourceRecord

    record = ResourceRecord(
        kind=ResourceKind.CLUSTER,
        arn="arn:aws:rds:eu-west-2:000000000000:cluster:x",
        resource_id="x",
        identifier="x",
        environment_slug="sandbox",
        target_key=TARGET_KEY,
        ownership="unmanaged",
        status="available",
        facts={"serverless_v2_min_capacity": 0.5, "serverless_v2_max_capacity": 2.0},
        snapshot_id=1,
    )
    values = ServerlessCapacityChangeExecutor(None).attribute_values(
        ServerlessCapacityOptions(min_capacity_acu=1, max_capacity_acu=8), record
    )
    assert values == {"serverless_v2_min_capacity": 1.0, "serverless_v2_max_capacity": 8.0}
    from rdscommander.operations.attribute_change import AttributeChange

    change = AttributeChange(record.arn, "aws_rds_cluster.x", values, "scale")
    assert change.values == values


def test_tags_change_values_set_and_remove_tags_and_summarise_the_counts():
    from rdscommander.lifecycle.settings_changes import TagsChangeExecutor

    executor = TagsChangeExecutor(None)
    options = TagsOptions(set_tags={"team": "orders"}, remove_tags=["old"])
    assert executor.attribute_values(options, None) == {"tags.team": "orders", "tags.old": None}
    assert executor.summary(options) == "set 1 tags, remove 1"


MONITORING_ROLE_ARN = "arn:aws:iam::123456789012:role/rds-monitoring"
AURORA_WRITER_ADDRESS = "aws_rds_cluster_instance.demo_aurora_writer"


def _adopt_aurora_estate(session_factory, operator_settings, config_store, console_config, sandbox_snapshot):
    """Adopt the Aurora cluster with its members, groups and subnet group; returns runner and service."""
    from tests.drift.test_detect_executor import _adopt

    return _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        [
            "demo-aurora-pg16-tuned",
            "demo-private-subnets",
            "demo-aurora",
            "demo-aurora-writer",
            "demo-aurora-reader",
        ],
        [
            "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned",
            "aws_db_subnet_group.demo_private_subnets",
            "aws_rds_cluster.demo_aurora",
            AURORA_WRITER_ADDRESS,
            "aws_rds_cluster_instance.demo_aurora_reader",
        ],
        "settings-adopt-aurora",
    )


def test_maintenance_window_change_direct_sets_the_window_of_an_aurora_member(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "maintenance_window_change",
        "demo-aurora-writer",
        {"preferred_maintenance_window": "sun:03:00-sun:04:00"},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    [entry] = finished.plan["change_set"]
    assert (entry["address"], entry["attribute"], entry["desired"], entry["downtime"]) == (
        AURORA_WRITER_ADDRESS,
        "preferred_maintenance_window",
        "sun:03:00-sun:04:00",
        "none",
    )
    live = boto3.client("rds", region_name="eu-west-2").describe_db_instances(
        DBInstanceIdentifier="demo-aurora-writer"
    )["DBInstances"][0]
    assert live["PreferredMaintenanceWindow"] == "sun:03:00-sun:04:00"
    assert (
        _recorded(operator_settings, "cluster_instances", "demo-aurora-writer").preferred_maintenance_window
        == "sun:03:00-sun:04:00"
    )


def test_monitoring_change_in_terraform_mode_renders_the_role_and_retention_of_an_aurora_member(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
):
    runner, service = _adopt_aurora_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    update = {
        "resource_changes": [
            {
                "address": AURORA_WRITER_ADDRESS,
                "type": "aws_rds_cluster_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"monitoring_interval": 0},
                    "after": {"monitoring_interval": 60, "monitoring_role_arn": MONITORING_ROLE_ARN},
                },
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora-writer")
    operation = service.create(
        "monitoring_change",
        "sandbox",
        TARGET_KEY,
        {
            "options": {
                "performance_insights_enabled": True,
                "performance_insights_retention_period_days": 31,
                "monitoring_interval_seconds": 60,
                "monitoring_role_arn": MONITORING_ROLE_ARN,
            },
            "resource_arn": arn,
        },
        "monitoring-member-tf",
        "phil",
        resource_arn=arn,
        title="Change monitoring: demo-aurora-writer",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    rendered = (_workspace_dir(operator_settings) / "clusters.tf").read_text()
    writer_block = rendered.split('resource "aws_rds_cluster_instance" "demo_aurora_writer"')[1].split(
        "\nresource "
    )[0]
    assert re.search(r"monitoring_interval\s*=\s*60", writer_block)
    assert re.search(rf'monitoring_role_arn\s*=\s*"{re.escape(MONITORING_ROLE_ARN)}"', writer_block)
    assert re.search(r"performance_insights_retention_period\s*=\s*31", writer_block)


def _set_live_and_refresh(session_factory, operator_settings, config_store, change_live):
    """Change the simulated AWS with `change_live(rds_client)`, then take a new inventory snapshot."""
    from rdscommander.main import build_executors, build_services
    from rdscommander.operations.runner import OperationRunner

    change_live(boto3.client("rds", region_name="eu-west-2"))
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        operator_settings,
        config_store,
        services,
    )
    OperationService(session_factory).create(
        "inventory_refresh", "sandbox", None, {}, "settings-refresh", "phil"
    )
    runner.run_once()


def test_backup_settings_change_in_terraform_mode_warns_that_turning_backups_on_interrupts_service(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
):
    # Terraform mode renders apply_immediately = true, so turning backups on (0 to 7) happens at once with
    # its brief outage; the review read the change without the current retention and said "none".
    from rdscommander.parameters.direct_api import BACKUPS_TURNED_ON_OR_OFF_DOWNTIME

    _set_live_and_refresh(
        session_factory,
        operator_settings,
        config_store,
        lambda rds: rds.modify_db_instance(
            DBInstanceIdentifier="demo-orders-replica", BackupRetentionPeriod=0, ApplyImmediately=True
        ),
    )
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    with session_factory() as session:
        from rdscommander.resources.queries import load_resource

        replica = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-orders-replica")
        )
        assert replica is not None and replica.facts["backup_retention_period"] == 0
    update = {
        "resource_changes": [
            {
                "address": "aws_db_instance.demo_orders_replica",
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"backup_retention_period": 0},
                    "after": {"backup_retention_period": 7},
                },
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    arn = arn_for(session_factory, sandbox_snapshot, "demo-orders-replica")
    operation = service.create(
        "backup_settings_change",
        "sandbox",
        TARGET_KEY,
        {"options": {"backup_retention_period_days": 7}, "resource_arn": arn},
        "backups-on-tf",
        "phil",
        resource_arn=arn,
        title="Change backup settings: demo-orders-replica",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert awaiting.plan["downtime"] == BACKUPS_TURNED_ON_OR_OFF_DOWNTIME


BACKUPS_DELETED_SENTENCE = "existing automated backups are deleted and point-in-time restore is lost"


def test_turning_backups_off_direct_asks_for_the_identifier_and_says_the_backups_are_deleted(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "backup_settings_change",
        "demo-orders-replica",
        {"backup_retention_period_days": 0},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert finished.approval_required_text == "demo-orders-replica"
    assert BACKUPS_DELETED_SENTENCE in finished.plan["downtime"]


def test_lengthening_backup_retention_direct_needs_no_typed_identifier(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "backup_settings_change",
        "demo-orders-replica",
        {"backup_retention_period_days": 14},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert finished.approval_required_text is None
    assert BACKUPS_DELETED_SENTENCE not in finished.plan["downtime"]


def test_turning_backups_off_in_terraform_mode_asks_for_the_identifier_and_says_the_backups_are_deleted(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
):
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    update = {
        "resource_changes": [
            {
                "address": "aws_db_instance.demo_orders_replica",
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"backup_retention_period": 1},
                    "after": {"backup_retention_period": 0},
                },
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, {"resource_changes": []}]
    )
    arn = arn_for(session_factory, sandbox_snapshot, "demo-orders-replica")
    operation = service.create(
        "backup_settings_change",
        "sandbox",
        TARGET_KEY,
        {"options": {"backup_retention_period_days": 0}, "resource_arn": arn},
        "backups-off-tf",
        "phil",
        resource_arn=arn,
        title="Change backup settings: demo-orders-replica",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert awaiting.approval_required_text == "demo-orders-replica"
    assert BACKUPS_DELETED_SENTENCE in awaiting.plan["downtime"]
    assert "outage" in awaiting.plan["downtime"]


def test_the_storage_review_says_aws_waits_six_hours_between_storage_changes(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "storage_change",
        "demo-orders",
        {"allocated_storage_gb": 200},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert "six hours" in finished.plan["downtime"]


def test_serverless_capacity_change_direct_sends_both_ends_when_only_the_maximum_changes(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
):
    from botocore.client import BaseClient

    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_cluster(
        DBClusterIdentifier="demo-aurora",
        ServerlessV2ScalingConfiguration={"MinCapacity": 0.5, "MaxCapacity": 4.0},
        ApplyImmediately=True,
    )
    modify_cluster_requests: list[dict] = []
    real_make_api_call = BaseClient._make_api_call

    def recording(self, operation_name, api_params):
        if operation_name == "ModifyDBCluster":
            modify_cluster_requests.append(dict(api_params))
        return real_make_api_call(self, operation_name, api_params)

    monkeypatch.setattr(BaseClient, "_make_api_call", recording)
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "serverless_capacity_change",
        "demo-aurora",
        {"min_capacity_acu": 0.5, "max_capacity_acu": 8},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    [request] = modify_cluster_requests
    assert request["ServerlessV2ScalingConfiguration"] == {"MinCapacity": 0.5, "MaxCapacity": 8.0}


def _turn_enhanced_monitoring_on_in_aws(session_factory, operator_settings, config_store):
    _set_live_and_refresh(
        session_factory,
        operator_settings,
        config_store,
        lambda rds: rds.modify_db_instance(
            DBInstanceIdentifier="demo-orders",
            MonitoringInterval=60,
            MonitoringRoleArn=MONITORING_ROLE_ARN,
            ApplyImmediately=True,
        ),
    )


def test_toggling_performance_insights_keeps_the_live_monitoring_role_without_asking_for_it(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
):
    # The form pre-fills the live interval (60) but not the role; the change is only Performance Insights.
    _make_moto_report_performance_insights_like_aws(monkeypatch)
    _turn_enhanced_monitoring_on_in_aws(session_factory, operator_settings, config_store)
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "monitoring_change",
        "demo-orders",
        {"performance_insights_enabled": True, "monitoring_interval_seconds": 60},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    assert {e["attribute"] for e in finished.plan["change_set"]} == {"performance_insights_enabled"}


def test_a_new_monitoring_interval_without_a_role_uses_the_live_role_and_shows_it_in_the_review(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
):
    _make_moto_report_performance_insights_like_aws(monkeypatch)
    _turn_enhanced_monitoring_on_in_aws(session_factory, operator_settings, config_store)
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "monitoring_change",
        "demo-orders",
        {"performance_insights_enabled": False, "monitoring_interval_seconds": 30},
    )
    assert finished.state == OperationState.SUCCEEDED, [d.detail for d in finished.diagnostic_reports]
    desired = {e["attribute"]: e["desired"] for e in finished.plan["change_set"]}
    assert desired["monitoring_interval"] == 30
    assert desired["monitoring_role_arn"] == MONITORING_ROLE_ARN


def test_enhanced_monitoring_without_a_role_is_refused_when_the_instance_has_none(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
):
    finished, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "monitoring_change",
        "demo-orders",
        {"performance_insights_enabled": False, "monitoring_interval_seconds": 60},
    )
    assert finished.state == OperationState.FAILED
    [diagnostic] = [d for d in finished.diagnostic_reports if d.diagnostic_kind == "monitoring_role_required"]
    assert "demo-orders" in diagnostic.detail
    assert "role" in diagnostic.fix_suggestion
