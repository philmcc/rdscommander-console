"""Restore a snapshot, or an instance or cluster to a point in time, as a new resource, in both modes."""

from __future__ import annotations

import re

import boto3
import pytest
from pydantic import ValidationError

from rdscommander.catalogue.catalogue import (
    GROUP_SNAPSHOTS,
    definition_for,
    pitr_source,
    restorable_snapshot,
)
from rdscommander.control_db.models import (
    DiagnosticReport,
    DriftFinding,
    ExecutionRecord,
    InventoryDbInstance,
)
from rdscommander.lifecycle.options import PointInTimeRestoreOptions, SnapshotRestoreOptions
from rdscommander.lifecycle.restores import PointInTimeRestoreExecutor, SnapshotRestoreExecutor
from rdscommander.operations.create_change import NEW_RESOURCE_DOWNTIME
from rdscommander.operations.enums import OperationState
from rdscommander.operations.service import OperationService
from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.record import read_record
from tests.drift.test_detect_executor import _adopt_estate
from tests.lifecycle.test_creates import _direct_client, _git_output, _post, _scripted_create
from tests.lifecycle.test_removals import _adopt_aurora
from tests.lifecycle.test_settings_changes import _run
from tests.operations.test_change_strategy import _direct_store, _runner, _workspace_dir
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _prepare_repo

RESTORED = "demo-orders-restored"
RESTORED_ADDRESS = "aws_db_instance.demo_orders_restored"
RESTORED_CLUSTER = "demo-aurora-restored"
RESTORED_CLUSTER_ADDRESS = "aws_rds_cluster.demo_aurora_restored"
RESTORED_WRITER = "demo-aurora-restored-1"
RESTORED_WRITER_ADDRESS = "aws_rds_cluster_instance.demo_aurora_restored_1"
CONSOLE_TAG_LIST = [{"Key": "rdscommander:managed", "Value": "true"}]
PASSWORD_ASSIGNMENT = re.compile(r"\bpassword\s*=|master_password|MasterUserPassword\b")
"""A password set in the files: `password =`, `master_password`; `manage_master_user_password` is not one."""


@pytest.fixture(autouse=True)
def _moto_reports_performance_insights_like_aws(monkeypatch) -> None:  # noqa: ANN001
    """A restored instance's follow-up modify turns Performance Insights on and is read back to confirm it."""
    from tests.lifecycle.test_settings_changes import _make_moto_report_performance_insights_like_aws

    _make_moto_report_performance_insights_like_aws(monkeypatch)


def _rds():  # noqa: ANN202
    return boto3.client("rds", region_name="eu-west-2")


def _security_group_id() -> str:
    """The id of the simulated estate's `demo-db-access` security group."""
    groups = boto3.client("ec2", region_name="eu-west-2").describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["demo-db-access"]}]
    )["SecurityGroups"]
    return groups[0]["GroupId"]


def _instance_options(**overrides) -> dict:  # noqa: ANN003
    return {
        "new_identifier": RESTORED,
        "instance_class": "db.m6g.large",
        "subnet_group_name": "demo-private-subnets",
        "parameter_group_name": "demo-postgres16-tuned",
        "vpc_security_group_ids": [_security_group_id()],
        **overrides,
    }


def _cluster_options(**overrides) -> dict:  # noqa: ANN003
    return {
        "new_identifier": RESTORED_CLUSTER,
        "instance_class": "db.r6g.large",
        "subnet_group_name": "demo-private-subnets",
        "parameter_group_name": "demo-aurora-pg16-tuned",
        "vpc_security_group_ids": [_security_group_id()],
        **overrides,
    }


def _refresh(session_factory, operator_settings, config_store, key: str, direct: bool) -> None:
    """Refresh the sandbox inventory so the snapshots taken in the simulated account are in it."""
    store = _direct_store(config_store) if direct else config_store
    runner, _ = _runner(session_factory, operator_settings, store)
    OperationService(session_factory).create("inventory_refresh", "sandbox", None, {}, key, "phil")
    runner.run_once()


def _instance_snapshot_arn(session_factory, operator_settings, config_store, direct: bool = True) -> str:
    """Snapshot demo-orders in the simulated account, refresh the inventory and return the snapshot's ARN."""
    arn = _rds().create_db_snapshot(
        DBSnapshotIdentifier="demo-orders-before-restore", DBInstanceIdentifier="demo-orders"
    )["DBSnapshot"]["DBSnapshotArn"]
    if direct:
        _prepare_repo(operator_settings)
    _refresh(session_factory, operator_settings, config_store, "snapshot-refresh", direct)
    return arn


def _cluster_snapshot_arn(session_factory, operator_settings, config_store, direct: bool = True) -> str:
    """Snapshot demo-aurora in the simulated account, refresh the inventory and return the snapshot's ARN."""
    arn = _rds().create_db_cluster_snapshot(
        DBClusterSnapshotIdentifier="demo-aurora-before-restore", DBClusterIdentifier="demo-aurora"
    )["DBClusterSnapshot"]["DBClusterSnapshotArn"]
    if direct:
        _prepare_repo(operator_settings)
    _refresh(session_factory, operator_settings, config_store, "cluster-snapshot-refresh", direct)
    return arn


def _sibling(arn: str, arn_kind: str, identifier: str) -> str:
    return ":".join(arn.split(":")[:5]) + f":{arn_kind}:{identifier}"


def _restore_call(session_factory, operation_id, api_name: str) -> dict:
    with session_factory() as session:
        [call] = session.query(ExecutionRecord).filter_by(
            operation_id=operation_id, record_kind="aws_api", api_name=api_name
        )
        return call.request_parameters


# ----- instance snapshot, direct mode ---------------------------------------------------------------------


def test_restore_from_snapshot_direct_creates_the_instance_from_it(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    snapshot_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_restore",
        None,
        _instance_options(),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    described = _rds().describe_db_instances(DBInstanceIdentifier=RESTORED)["DBInstances"][0]
    assert described["DBInstanceStatus"] == "available"
    assert final.resource_arn == snapshot_arn, "the operation stays on the snapshot it was launched from"
    assert final.plan["created_resource_arns"] == [_sibling(snapshot_arn, "db", RESTORED)]
    assert final.plan["touched_addresses"] == [RESTORED_ADDRESS]
    review = {entry["attribute"]: entry["desired"] for entry in final.plan["change_set"]}
    assert review["presence"] == f"aws_db_instance {RESTORED}"
    assert review["restore_source"] == "demo-orders-before-restore"
    assert review["vpc_security_group_ids"] == [_security_group_id()]
    assert final.plan["change_set"][0]["downtime"] == NEW_RESOURCE_DOWNTIME
    request = _restore_call(session_factory, final.id, "rds:RestoreDBInstanceFromDBSnapshot")
    assert request["DBInstanceIdentifier"] == RESTORED
    assert request["DBSnapshotIdentifier"] == "demo-orders-before-restore"
    assert request["DBSubnetGroupName"] == "demo-private-subnets"
    assert request["DBParameterGroupName"] == "demo-postgres16-tuned"
    assert request["VpcSecurityGroupIds"] == [_security_group_id()]
    assert request["DeletionProtection"] is True and request["MultiAZ"] is False
    assert request["Tags"] == CONSOLE_TAG_LIST
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    restored = next(instance for instance in recorded.instances if instance.identifier == RESTORED)
    assert restored.snapshot_identifier == "demo-orders-before-restore", "direct mode keeps the create source"
    assert restored.instance_class == described["DBInstanceClass"]
    instances_tf = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    block = instances_tf[instances_tf.index('"demo_orders_restored"') :].split("\n}\n")[0]
    assert re.search(r"ignore_changes = \[.*snapshot_identifier.*\]", block)


def test_a_direct_restore_leaves_no_record_drift_on_the_restored_instance(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    snapshot_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_restore",
        None,
        _instance_options(),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    detect = OperationService(session_factory).create(
        "drift_detect", "sandbox", None, {"options": {}}, "after-restore", "phil"
    )
    runner.run_once()
    assert OperationService(session_factory).get(detect.id).state == OperationState.SUCCEEDED
    with session_factory() as session:
        findings = [
            finding
            for finding in session.query(DriftFinding).filter_by(resolved_at_utc=None)
            if any(attribute.get("address") == RESTORED_ADDRESS for attribute in finding.attributes)
        ]
    assert findings == [], [finding.attributes for finding in findings]


def test_restore_refuses_an_identifier_that_already_exists(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    snapshot_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_restore",
        None,
        _instance_options(new_identifier="demo-orders"),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.FAILED
    [failed_step] = [a for a in final.step_attempts if a.outcome_status == "failed"]
    assert failed_step.step_name == "compute_change_set"
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "resource_already_exists")
    assert "demo-orders already exists" in diagnostic.detail
    assert final.approved_at_utc is None


def test_restore_through_the_api_titles_the_operation_and_checks_the_placement(
    session_factory, operator_settings, console_config, config_store, sandbox_snapshot, moto_aws
) -> None:
    snapshot_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store)
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    refusals = {
        "taken": (_instance_options(new_identifier="demo-orders"), "an instance named demo-orders"),
        "subnet": (_instance_options(subnet_group_name="nope"), "no DB subnet group named nope"),
        "family": (
            _instance_options(parameter_group_name="demo-aurora-pg16-tuned"),
            "no instance parameter group named demo-aurora-pg16-tuned",
        ),
    }
    for key, (options, problem) in refusals.items():
        response = _post(client, headers, "snapshot_restore", snapshot_arn, options, f"restore-{key}")
        assert response.status_code == 422, (key, response.json())
        assert problem in " ".join(response.json()["problems"]), (key, response.json())
    bad_group = _post(
        client,
        headers,
        "snapshot_restore",
        snapshot_arn,
        _instance_options(vpc_security_group_ids=["default"]),
        "restore-bad-sg",
    )
    assert bad_group.status_code == 422
    assert "is not a security group id" in " ".join(bad_group.json()["problems"])
    accepted = _post(client, headers, "snapshot_restore", snapshot_arn, _instance_options(), "restore-ok")
    assert accepted.status_code == 202, accepted.json()
    detail = client.get(f"/api/v1/operations/{accepted.json()['operation_id']}").json()
    assert detail["title"] == f"Restore to new instance or cluster: {RESTORED}"
    assert detail["approval_required_text"] is None
    assert detail["resource_arn"] == snapshot_arn


# ----- instance snapshot, Terraform mode -----------------------------------------------------------------


class _RestoringTerraformRunner(FakeTerraformRunner):
    """A scripted Terraform whose apply restores the snapshot in the simulated account, as tofu would."""

    def apply(
        self, workspace_dir, target, settings, *, operation_id, step_name, plan_file_name="console.tfplan"
    ):  # noqa: ANN001, ANN201
        _rds().restore_db_instance_from_db_snapshot(
            DBInstanceIdentifier=RESTORED,
            DBSnapshotIdentifier="demo-orders-before-restore",
            DBInstanceClass="db.m6g.large",
            DBSubnetGroupName="demo-private-subnets",
            DBParameterGroupName="demo-postgres16-tuned",
            VpcSecurityGroupIds=[_security_group_id()],
            DeletionProtection=True,
            BackupRetentionPeriod=7,
            Tags=CONSOLE_TAG_LIST,
        )
        return super().apply(
            workspace_dir,
            target,
            settings,
            operation_id=operation_id,
            step_name=step_name,
            plan_file_name=plan_file_name,
        )


def _restore_in_terraform_mode(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, runner_class, plans
):  # noqa: ANN001, ANN202
    snapshot_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store, direct=False)
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    fake = runner_class(runner.services.execution_recorder, plan_scripts=plans)
    runner.services.terraform_runner = fake
    operation = service.create(
        "snapshot_restore",
        "sandbox",
        TARGET_KEY,
        {"options": _instance_options(), "resource_arn": snapshot_arn},
        "restore-tf",
        "phil",
        resource_arn=snapshot_arn,
        title=f"Restore to new instance or cluster: {RESTORED}",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert awaiting.plan["touched_addresses"] == [RESTORED_ADDRESS]
    service.approve(operation.id, "sam")
    runner.run_once()
    return fake, snapshot_arn, service.get(operation.id)


def test_restore_in_terraform_mode_renders_the_snapshot_source_and_allows_the_create(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    created = _scripted_create(RESTORED_ADDRESS, "aws_db_instance", RESTORED)
    fake, snapshot_arn, final = _restore_in_terraform_mode(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        FakeTerraformRunner,
        [created, created, {"resource_changes": []}],
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "apply" in fake.calls
    assert final.resource_arn == snapshot_arn
    assert final.plan["created_resource_arns"] == [_sibling(snapshot_arn, "db", RESTORED)]
    instances_tf = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    block = instances_tf[instances_tf.index('"demo_orders_restored"') :].split("\n}\n")[0]
    assert re.search(r'snapshot_identifier\s+=\s+"demo-orders-before-restore"', block)
    assert re.search(r"ignore_changes = \[engine_version, snapshot_identifier\]", block)
    assert re.search(r"db_subnet_group_name\s+=\s+aws_db_subnet_group\.demo_private_subnets\.name", block)
    assert re.search(r"parameter_group_name\s+=\s+aws_db_parameter_group\.demo_postgres16_tuned\.name", block)
    assert re.search(r"deletion_protection\s+=\s+true", block)
    assert re.search(r"manage_master_user_password\s+=\s+true", block)
    assert not PASSWORD_ASSIGNMENT.search(block), "no password is ever rendered"


def test_restore_in_terraform_mode_records_the_instance_as_built_with_its_snapshot(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    created = _scripted_create(RESTORED_ADDRESS, "aws_db_instance", RESTORED)
    clean = {"resource_changes": []}
    _, _, final = _restore_in_terraform_mode(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        _RestoringTerraformRunner,
        [created, created, clean, clean],
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    as_built = next(a for a in final.step_attempts if a.step_name == "record_as_built")
    assert as_built.output.get("recorded_as_built") is not False, as_built.output
    assert _git_output(operator_settings, "log", "-1", "--format=%s").endswith("(record as built)")
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    restored = next(instance for instance in recorded.instances if instance.identifier == RESTORED)
    assert restored.snapshot_identifier == "demo-orders-before-restore"


# ----- cluster snapshot ----------------------------------------------------------------------------------


def test_restore_a_cluster_snapshot_direct_creates_the_cluster_and_its_writer(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    snapshot_arn = _cluster_snapshot_arn(session_factory, operator_settings, config_store)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_restore",
        None,
        _cluster_options(),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    cluster = _rds().describe_db_clusters(DBClusterIdentifier=RESTORED_CLUSTER)["DBClusters"][0]
    assert [(m["DBInstanceIdentifier"], m["IsClusterWriter"]) for m in cluster["DBClusterMembers"]] == [
        (RESTORED_WRITER, True)
    ]
    assert final.resource_arn == snapshot_arn
    assert final.plan["touched_addresses"] == [RESTORED_CLUSTER_ADDRESS, RESTORED_WRITER_ADDRESS]
    assert final.plan["created_resource_arns"] == [
        _sibling(snapshot_arn, "cluster", RESTORED_CLUSTER),
        _sibling(snapshot_arn, "db", RESTORED_WRITER),
    ]
    presences = [e["desired"] for e in final.plan["change_set"] if e["attribute"] == "presence"]
    assert presences == [f"aws_rds_cluster {RESTORED_CLUSTER}", f"aws_rds_cluster_instance {RESTORED_WRITER}"]
    request = _restore_call(session_factory, final.id, "rds:RestoreDBClusterFromSnapshot")
    assert request["SnapshotIdentifier"] == "demo-aurora-before-restore"
    assert request["Engine"] == "aurora-postgresql"
    assert request["DBClusterParameterGroupName"] == "demo-aurora-pg16-tuned"
    assert request["DBSubnetGroupName"] == "demo-private-subnets"
    writer_request = _restore_call(session_factory, final.id, "rds:CreateDBInstance")
    assert writer_request["DBClusterIdentifier"] == RESTORED_CLUSTER
    assert writer_request["DBInstanceClass"] == "db.r6g.large"
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    restored = next(c for c in recorded.clusters if c.identifier == RESTORED_CLUSTER)
    assert restored.snapshot_identifier == "demo-aurora-before-restore"
    writer = next(m for m in recorded.cluster_instances if m.identifier == RESTORED_WRITER)
    assert writer.cluster_reference == f"{RESTORED_CLUSTER_ADDRESS}.id"


def test_restore_a_cluster_snapshot_in_terraform_mode_renders_the_cluster_and_its_writer(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    snapshot_arn = _cluster_snapshot_arn(session_factory, operator_settings, config_store, direct=False)
    runner, service = _adopt_aurora(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    created = {
        "resource_changes": [
            _scripted_create(RESTORED_CLUSTER_ADDRESS, "aws_rds_cluster", RESTORED_CLUSTER)[
                "resource_changes"
            ][0],
            _scripted_create(RESTORED_WRITER_ADDRESS, "aws_rds_cluster_instance", RESTORED_WRITER)[
                "resource_changes"
            ][0],
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[created, created, {"resource_changes": []}]
    )
    operation = service.create(
        "snapshot_restore",
        "sandbox",
        TARGET_KEY,
        {"options": _cluster_options(), "resource_arn": snapshot_arn},
        "restore-cluster-tf",
        "phil",
        resource_arn=snapshot_arn,
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert final.plan["created_resource_arns"] == [
        _sibling(snapshot_arn, "cluster", RESTORED_CLUSTER),
        _sibling(snapshot_arn, "db", RESTORED_WRITER),
    ]
    clusters_tf = (_workspace_dir(operator_settings) / "clusters.tf").read_text()
    cluster_block = clusters_tf[clusters_tf.index('"demo_aurora_restored"') :].split("\n}\n")[0]
    assert re.search(r'snapshot_identifier\s+=\s+"demo-aurora-before-restore"', cluster_block)
    assert re.search(r"manage_master_user_password\s+=\s+true", cluster_block)
    assert not PASSWORD_ASSIGNMENT.search(clusters_tf), "no password is ever rendered"
    assert re.search(
        r"db_cluster_parameter_group_name\s+=\s+aws_rds_cluster_parameter_group\.demo_aurora_pg16_tuned\.name",
        cluster_block,
    )
    writer_block = clusters_tf[clusters_tf.index('"demo_aurora_restored_1"') :].split("\n}\n")[0]
    assert re.search(r"cluster_identifier\s+=\s+aws_rds_cluster\.demo_aurora_restored\.id", writer_block)
    assert re.search(r'instance_class\s+=\s+"db\.r6g\.large"', writer_block)


def test_restore_of_a_cluster_snapshot_refuses_multi_az_and_a_taken_writer_name(
    session_factory, operator_settings, console_config, config_store, sandbox_snapshot, moto_aws
) -> None:
    snapshot_arn = _cluster_snapshot_arn(session_factory, operator_settings, config_store)
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    multi_az = _post(
        client, headers, "snapshot_restore", snapshot_arn, _cluster_options(multi_az=True), "restore-maz"
    )
    assert multi_az.status_code == 422 and "add a reader" in " ".join(multi_az.json()["problems"])
    instance_group = _post(
        client,
        headers,
        "snapshot_restore",
        snapshot_arn,
        _cluster_options(parameter_group_name="demo-postgres16-tuned"),
        "restore-instance-group",
    )
    assert instance_group.status_code == 422
    assert "no cluster parameter group named demo-postgres16-tuned" in " ".join(
        instance_group.json()["problems"]
    )
    _rds().create_db_instance(
        DBInstanceIdentifier=RESTORED_WRITER,
        DBClusterIdentifier="demo-aurora",
        Engine="aurora-postgresql",
        DBInstanceClass="db.r6g.large",
    )
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        None,
        "snapshot_restore",
        None,
        _cluster_options(),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "resource_already_exists")
    assert f"{RESTORED_WRITER} already exists" in diagnostic.detail


# ----- rule, options and definition ----------------------------------------------------------------------


def test_restorable_snapshot_explains_every_refusal(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    snapshot_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store)
    environment = console_config.environment("sandbox")
    with session_factory() as session:
        snapshot = load_resource(session, console_config, snapshot_arn)
    assert restorable_snapshot(snapshot, environment).ok
    automated = snapshot.model_copy(update={"facts": {**snapshot.facts, "snapshot_type": "automated"}})
    assert restorable_snapshot(automated, environment).ok
    shared = snapshot.model_copy(update={"facts": {**snapshot.facts, "snapshot_type": "shared"}})
    assert "copy it into this account" in restorable_snapshot(shared, environment).reason
    creating = snapshot.model_copy(update={"status": "creating"})
    assert restorable_snapshot(creating, environment).reason == (
        "the snapshot is creating; wait until it is available"
    )
    assert restorable_snapshot(None, environment).reason == "pick a snapshot"


def test_snapshot_restore_options_explain_refused_values_in_sentences() -> None:
    options = SnapshotRestoreOptions(
        new_identifier=RESTORED,
        instance_class="db.m6g.large",
        subnet_group_name="demo-private-subnets",
        vpc_security_group_ids=["sg-0123456789abcdef0"],
        parameter_group_name="arn:aws:rds:eu-west-2:000000000000:pg:demo-postgres16-tuned",
    )
    assert options.parameter_group_name == "demo-postgres16-tuned"
    assert (options.multi_az, options.publicly_accessible, options.deletion_protection) == (
        False,
        False,
        True,
    )
    base = options.model_dump()
    for field_name, value, sentence in (
        ("new_identifier", "demo--restored", "two hyphens in a row"),
        ("vpc_security_group_ids", ["sg-0123"], "is not a security group id"),
        ("vpc_security_group_ids", ["sg-0123456789abcdef0", "sg-0123456789abcdef0"], "given once"),
        ("vpc_security_group_ids", [], "at least 1"),
    ):
        with pytest.raises(ValidationError, match=sentence):
            SnapshotRestoreOptions.model_validate({**base, field_name: value})


def test_restores_are_snapshots_operations_that_do_not_change_what_they_read() -> None:
    for kind, executor in (
        ("snapshot_restore", SnapshotRestoreExecutor),
        ("pitr_restore", PointInTimeRestoreExecutor),
    ):
        definition = definition_for(kind)
        assert definition.group == GROUP_SNAPSHOTS
        assert definition.changes_the_resource is False
        assert definition.destructive is False and definition.disruptive is False
        assert executor.allows_create is True and executor.operation_kind == kind
    assert definition_for("pitr_restore").title == "Point-in-time restore"


# ----- point-in-time restore -----------------------------------------------------------------------------

PITR = "demo-orders-pitr"
PITR_ADDRESS = "aws_db_instance.demo_orders_pitr"
PITR_CLUSTER = "demo-aurora-pitr"
PITR_CLUSTER_ADDRESS = "aws_rds_cluster.demo_aurora_pitr"
PITR_WRITER = "demo-aurora-pitr-1"


def _pitr_options(**overrides) -> dict:  # noqa: ANN003
    return {
        "new_identifier": PITR,
        "instance_class": "db.m6g.large",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": [_security_group_id()],
        **overrides,
    }


def test_point_in_time_restore_direct_uses_the_latest_restorable_time(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "pitr_restore",
        "demo-orders",
        _pitr_options(use_latest_restorable_time=True, parameter_group_name="demo-postgres16-tuned"),
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    assert final.resource_arn == orders_arn, "the operation stays on the instance it restores"
    assert final.plan["created_resource_arns"] == [_sibling(orders_arn, "db", PITR)]
    assert _rds().describe_db_instances(DBInstanceIdentifier=PITR)["DBInstances"][0]["DBInstanceStatus"] == (
        "available"
    )
    request = _restore_call(session_factory, final.id, "rds:RestoreDBInstanceToPointInTime")
    assert request["SourceDBInstanceIdentifier"] == "demo-orders"
    assert request["TargetDBInstanceIdentifier"] == PITR
    assert request["UseLatestRestorableTime"] is True and "RestoreTime" not in request
    assert request["DBParameterGroupName"] == "demo-postgres16-tuned"
    assert request["DeletionProtection"] is True and request["Tags"] == CONSOLE_TAG_LIST
    review = {entry["attribute"]: entry["desired"] for entry in final.plan["change_set"]}
    assert (review["restore_source"], review["restore_time"]) == ("demo-orders", "the latest restorable time")
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    restored = next(instance for instance in recorded.instances if instance.identifier == PITR)
    assert restored.restore_to_point_in_time is not None
    assert restored.restore_to_point_in_time.source_identifier == "demo-orders"
    assert restored.restore_to_point_in_time.use_latest_restorable_time is True


def test_point_in_time_restore_direct_sends_the_chosen_time(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "pitr_restore",
        "demo-orders",
        _pitr_options(restore_time_utc="2026-10-11T10:00:00Z"),
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    request = _restore_call(session_factory, final.id, "rds:RestoreDBInstanceToPointInTime")
    assert request["RestoreTime"] == "2026-10-11T10:00:00Z" and "UseLatestRestorableTime" not in request
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    restored = next(instance for instance in recorded.instances if instance.identifier == PITR)
    assert restored.restore_to_point_in_time.restore_time_utc == "2026-10-11T10:00:00Z"


def test_point_in_time_restore_needs_a_time_inside_the_window(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    outside = _post(
        client,
        headers,
        "pitr_restore",
        orders_arn,
        _pitr_options(restore_time_utc="2020-01-01T00:00:00Z"),
        "old",
    )
    assert outside.status_code == 422, outside.json()
    assert "outside the restore window of demo-orders" in outside.json()["detail"]
    window = client.get(f"/api/v1/lifecycle/restore-window/{encode_resource_id(orders_arn)}").json()
    [problem] = outside.json()["problems"]
    assert f"{window['earliest_utc']} to {window['latest_restorable_utc']}" in problem
    inside = _post(
        client,
        headers,
        "pitr_restore",
        orders_arn,
        _pitr_options(restore_time_utc=window["latest_restorable_utc"]),
        "inside",
    )
    assert inside.status_code == 202, inside.json()
    detail = client.get(f"/api/v1/operations/{inside.json()['operation_id']}").json()
    assert detail["title"] == f"Point-in-time restore: {PITR}"
    assert detail["plan"]["restore_window"]["earliest_utc"] == window["earliest_utc"]
    assert detail["plan"]["restore_window"]["latest_restorable_utc"] == window["latest_restorable_utc"]
    assert detail["resource_arn"] == orders_arn and detail["approval_required_text"] is None


def test_point_in_time_restore_warns_when_the_window_is_unknown(
    session_factory, operator_settings, console_config, config_store, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        row = session.query(InventoryDbInstance).filter_by(db_instance_arn=orders_arn).one()
        row.raw_description = {k: v for k, v in row.raw_description.items() if k != "LatestRestorableTime"}
        session.commit()
    _prepare_repo(operator_settings)
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    created = _post(
        client,
        headers,
        "pitr_restore",
        orders_arn,
        _pitr_options(restore_time_utc="2020-01-01T00:00:00Z"),
        "unk",
    )
    assert created.status_code == 202, created.json()
    operation_id = created.json()["operation_id"]
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    runner.run_once()
    service = OperationService(session_factory)
    awaiting = service.get(operation_id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert "latest restorable time" in awaiting.plan["restore_window_unknown"]
    with session_factory() as session:
        [warning] = session.query(DiagnosticReport).filter_by(
            operation_id=awaiting.id, diagnostic_kind="restore_window_unknown"
        )
        assert warning.severity == "warning" and warning.step_name == "compute_change_set"
        assert "before approving" in warning.fix_suggestion


def test_point_in_time_restore_of_a_cluster_direct_creates_the_cluster_and_its_writer(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "pitr_restore",
        "demo-aurora",
        _pitr_options(new_identifier=PITR_CLUSTER, instance_class="db.r6g.large"),
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    cluster = _rds().describe_db_clusters(DBClusterIdentifier=PITR_CLUSTER)["DBClusters"][0]
    assert [m["DBInstanceIdentifier"] for m in cluster["DBClusterMembers"]] == [PITR_WRITER]
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    assert final.resource_arn == aurora_arn
    assert final.plan["created_resource_arns"] == [
        _sibling(aurora_arn, "cluster", PITR_CLUSTER),
        _sibling(aurora_arn, "db", PITR_WRITER),
    ]
    request = _restore_call(session_factory, final.id, "rds:RestoreDBClusterToPointInTime")
    assert request["SourceDBClusterIdentifier"] == "demo-aurora"
    window = _rds().describe_db_clusters(DBClusterIdentifier="demo-aurora")["DBClusters"][0][
        "PreferredMaintenanceWindow"
    ]
    [(step_name, modify)] = _modify_calls(session_factory, final.id, "rds:ModifyDBCluster")
    assert step_name == "verify_direct" and modify["DBClusterIdentifier"] == PITR_CLUSTER
    assert modify["PreferredMaintenanceWindow"] == window and modify["ApplyImmediately"] is True
    assert request["DBClusterIdentifier"] == PITR_CLUSTER and request["UseLatestRestorableTime"] is True
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    restored = next(c for c in recorded.clusters if c.identifier == PITR_CLUSTER)
    assert restored.restore_to_point_in_time.source_identifier == "demo-aurora"
    assert any(m.identifier == PITR_WRITER for m in recorded.cluster_instances)


def test_point_in_time_restore_in_terraform_mode_renders_the_restore_block(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    created = _scripted_create(PITR_ADDRESS, "aws_db_instance", PITR)
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[created, created, {"resource_changes": []}]
    )
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "pitr_restore",
        "sandbox",
        TARGET_KEY,
        {"options": _pitr_options(restore_time_utc="2026-10-11T10:00:00Z"), "resource_arn": orders_arn},
        "pitr-tf",
        "phil",
        resource_arn=orders_arn,
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert final.plan["created_resource_arns"] == [_sibling(orders_arn, "db", PITR)]
    instances_tf = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    block = instances_tf[instances_tf.index('"demo_orders_pitr"') :].split("\n}\n")[0]
    assert re.search(r'source_db_instance_identifier\s+=\s+"demo-orders"', block)
    assert re.search(r'restore_time\s+=\s+"2026-10-11T10:00:00Z"', block)
    assert re.search(r"ignore_changes = \[.*restore_to_point_in_time.*\]", block)
    assert "replicate_source_db" not in block and not PASSWORD_ASSIGNMENT.search(block)
    assert re.search(r"manage_master_user_password\s+=\s+true", block)


def test_pitr_source_explains_every_refusal(session_factory, console_config, sandbox_snapshot) -> None:
    environment = console_config.environment("sandbox")
    with session_factory() as session:
        orders, writer, cluster, group = (
            load_resource(session, console_config, arn_for(session_factory, sandbox_snapshot, name))
            for name in ("demo-orders", "demo-aurora-writer", "demo-aurora", "demo-postgres16-tuned")
        )
    assert pitr_source(orders, environment).ok and pitr_source(cluster, environment).ok
    assert pitr_source(None, environment).reason == "pick an instance or cluster"
    assert pitr_source(group, environment).reason == "applies to instances and clusters"
    assert pitr_source(writer, environment).reason == (
        "this instance is a member of demo-aurora; restore the cluster instead"
    )
    stopped = orders.model_copy(update={"status": "stopped"})
    assert pitr_source(stopped, environment).reason == "it is stopped; wait until it is available"
    without_backups = orders.model_copy(update={"facts": {**orders.facts, "backup_retention_period": 0}})
    assert "automated backups are off" in pitr_source(without_backups, environment).reason


def test_point_in_time_restore_options_take_a_time_or_the_latest() -> None:
    base = {
        "new_identifier": PITR,
        "instance_class": "db.m6g.large",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": ["sg-0123456789abcdef0"],
    }
    latest = PointInTimeRestoreOptions.model_validate(base)
    assert (latest.restore_time_utc, latest.use_latest_restorable_time) == (None, True)
    timed = PointInTimeRestoreOptions.model_validate(
        {**base, "restore_time_utc": "2026-10-11T12:00:00+02:00"}
    )
    assert (timed.restore_time_utc, timed.use_latest_restorable_time) == ("2026-10-11T10:00:00Z", False)
    blank = PointInTimeRestoreOptions.model_validate({**base, "restore_time_utc": " "})
    assert blank.restore_time_utc is None and blank.use_latest_restorable_time is True
    for options, sentence in (
        ({"restore_time_utc": "yesterday"}, "a restore time looks like"),
        ({"restore_time_utc": "2026-10-11T10:00:00Z", "use_latest_restorable_time": True}, "not both"),
        ({"use_latest_restorable_time": False}, "choose a restore time, or restore to the latest"),
    ):
        with pytest.raises(ValidationError, match=sentence):
            PointInTimeRestoreOptions.model_validate({**base, **options})


# ----- addendum: settings the restore calls do not take, and a writer that cannot be created -------------


def _modify_calls(session_factory, operation_id, api_name: str = "rds:ModifyDBInstance") -> list:
    with session_factory() as session:
        return [
            (call.step_name, call.request_parameters)
            for call in session.query(ExecutionRecord).filter_by(
                operation_id=operation_id, record_kind="aws_api", api_name=api_name
            )
        ]


def test_a_direct_snapshot_restore_turns_on_what_the_restore_call_cannot_set(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from tests.lifecycle.test_settings_changes import _make_moto_report_performance_insights_like_aws

    _make_moto_report_performance_insights_like_aws(monkeypatch)
    snapshot_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_restore",
        None,
        _instance_options(),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    [(step_name, request)] = _modify_calls(session_factory, final.id)
    assert step_name == "verify_direct"
    assert request["DBInstanceIdentifier"] == RESTORED and request["ApplyImmediately"] is True
    assert request["EnablePerformanceInsights"] is True
    described = _rds().describe_db_instances(DBInstanceIdentifier=RESTORED)["DBInstances"][0]
    assert described["PerformanceInsightsEnabled"] is True
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    restored = next(instance for instance in recorded.instances if instance.identifier == RESTORED)
    assert restored.performance_insights_enabled is True, "both modes build the same instance"


def test_a_direct_point_in_time_restore_copies_the_source_s_window_and_monitoring_by_a_follow_up_modify(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _rds().modify_db_instance(
        DBInstanceIdentifier="demo-orders",
        PreferredMaintenanceWindow="sun:03:00-sun:03:30",
        MonitoringInterval=60,
        MonitoringRoleArn="arn:aws:iam::123456789012:role/rds-monitoring",
        ApplyImmediately=True,
    )
    _prepare_repo(operator_settings)
    _refresh(session_factory, operator_settings, config_store, "monitoring-refresh", direct=True)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "pitr_restore",
        "demo-orders",
        _pitr_options(),
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    [(step_name, request)] = _modify_calls(session_factory, final.id)
    assert step_name == "verify_direct" and request["DBInstanceIdentifier"] == PITR
    assert request["PreferredMaintenanceWindow"] == "sun:03:00-sun:03:30"
    assert (request["MonitoringInterval"], request["MonitoringRoleArn"]) == (
        60,
        "arn:aws:iam::123456789012:role/rds-monitoring",
    )
    assert request["EnablePerformanceInsights"] is True
    restore_request = _restore_call(session_factory, final.id, "rds:RestoreDBInstanceToPointInTime")
    assert "PreferredMaintenanceWindow" not in restore_request and "MonitoringInterval" not in restore_request


def test_a_cluster_restore_whose_writer_cannot_be_created_says_how_to_recover(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from botocore.exceptions import ClientError

    from rdscommander.lifecycle import direct_api

    def refuse_the_member(context, arguments, step_name="apply_direct"):  # noqa: ANN001, ANN202
        raise ClientError(
            {"Error": {"Code": "InsufficientDBInstanceCapacity", "Message": "no capacity"}},
            "CreateDBInstance",
        )

    snapshot_arn = _cluster_snapshot_arn(session_factory, operator_settings, config_store)
    monkeypatch.setattr(direct_api, "create_cluster_member", refuse_the_member)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_restore",
        None,
        _cluster_options(),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.FAILED
    assert _rds().describe_db_clusters(DBClusterIdentifier=RESTORED_CLUSTER)["DBClusters"][0]
    assert final.plan["applied_without_record"] is True
    assert final.plan["created_resource_arns"] == [_sibling(snapshot_arn, "cluster", RESTORED_CLUSTER)]
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "restore_writer_not_created"
    )
    assert diagnostic.step_name == "apply_direct"
    assert "no capacity" in diagnostic.detail
    assert diagnostic.fix_suggestion == (
        f"the cluster {RESTORED_CLUSTER} exists without a writer; run Refresh inventory, then Add Aurora "
        "reader on it to create its first member, or Decommission it"
    )


# ----- fix round 1: the follow-up modify is confirmed, and its failure says how to recover ----------------


def _monitored_source(session_factory, operator_settings, config_store) -> None:
    """demo-orders with enhanced monitoring, so a restore of it sends MonitoringInterval in the follow-up."""
    _rds().modify_db_instance(
        DBInstanceIdentifier="demo-orders",
        MonitoringInterval=60,
        MonitoringRoleArn="arn:aws:iam::123456789012:role/rds-monitoring",
        ApplyImmediately=True,
    )
    _prepare_repo(operator_settings)
    _refresh(session_factory, operator_settings, config_store, "monitoring-refresh", direct=True)


def _report_the_old_monitoring_interval(monkeypatch, reads_before_it_lands: int | None) -> list[int]:
    """AWS keeps describing the restored instance's old MonitoringInterval for a while (None: for ever).

    Returns the list the read-backs of the restored instance are counted in.
    """
    from rdscommander.parameters import direct_api

    real_described_resource = direct_api._described_resource
    reads: list[int] = []

    def described_resource(client, address, identifier):  # noqa: ANN001, ANN202
        description = real_described_resource(client, address, identifier)
        if identifier != PITR:
            return description
        reads.append(1)
        if reads_before_it_lands is None or len(reads) <= reads_before_it_lands:
            return {**description, "MonitoringInterval": 0}
        return description

    monkeypatch.setattr(direct_api, "_described_resource", described_resource)
    return reads


def test_verification_waits_until_the_follow_up_settings_read_back(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _monitored_source(session_factory, operator_settings, config_store)
    reads = _report_the_old_monitoring_interval(monkeypatch, reads_before_it_lands=3)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "pitr_restore",
        "demo-orders",
        _pitr_options(),
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert len(reads) == 4, "three read-backs still showed the old interval; the fourth passed"
    assert final.plan["post_create_values_applied"]["monitoring_interval"] == 60
    assert final.plan["post_create_values_applied"]["performance_insights_enabled"] is True


def test_verification_fails_without_a_record_when_the_follow_up_settings_never_land(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    operator_settings.direct_verify_timeout_seconds = 30
    operator_settings.direct_verify_poll_seconds = 10.0
    _monitored_source(session_factory, operator_settings, config_store)
    _report_the_old_monitoring_interval(monkeypatch, reads_before_it_lands=None)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "pitr_restore",
        "demo-orders",
        _pitr_options(),
    )
    assert final.state == OperationState.FAILED
    [failed_step] = [a for a in final.step_attempts if a.outcome_status == "failed"]
    assert failed_step.step_name == "verify_direct"
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "direct_change_not_verified"
    )
    assert "monitoring_interval" in diagnostic.detail
    assert final.plan["applied_without_record"] is True


def test_a_follow_up_modify_that_aws_refuses_says_how_to_recover(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from botocore.exceptions import ClientError

    from rdscommander.operations import create_change

    def refuse_the_modify(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ClientError(
            {"Error": {"Code": "InvalidParameterCombination", "Message": "not for this class"}},
            "ModifyDBInstance",
        )

    snapshot_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store)
    monkeypatch.setattr(create_change, "apply_attribute_values", refuse_the_modify)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_restore",
        None,
        _instance_options(),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.FAILED
    assert _rds().describe_db_instances(DBInstanceIdentifier=RESTORED)["DBInstances"][0]
    assert final.plan["applied_without_record"] is True
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "post_create_settings_not_applied"
    )
    assert diagnostic.step_name == "verify_direct" and "not for this class" in diagnostic.detail
    assert diagnostic.fix_suggestion == (
        f"{RESTORED} was created without performance_insights_enabled; run Refresh inventory, then the "
        "Monitoring or Maintenance window change on it"
    )


# ----- final fix wave: a restore asks AWS to manage the restored master password --------------------------


def _assert_no_master_password_in_any_request(session_factory, operation_id) -> None:  # noqa: ANN001
    """No AWS request of the operation carries a `MasterUserPassword` key (the console never holds one)."""
    import json

    with session_factory() as session:
        bodies = [
            record.request_parameters
            for record in session.query(ExecutionRecord).filter_by(
                operation_id=operation_id, record_kind="aws_api"
            )
        ]
    assert bodies
    for body in bodies:
        assert '"MasterUserPassword"' not in json.dumps(body), body


def _recorded_instance(operator_settings, identifier: str):  # noqa: ANN001, ANN202
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    return next(instance for instance in recorded.instances if instance.identifier == identifier)


def test_a_direct_snapshot_restore_of_an_instance_asks_aws_to_manage_the_master_password(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    snapshot_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_restore",
        None,
        _instance_options(),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    request = _restore_call(session_factory, final.id, "rds:RestoreDBInstanceFromDBSnapshot")
    # The recorder redacts every key naming a password; AWS reporting the secret shows what was asked.
    assert "ManageMasterUserPassword" in request
    described = _rds().describe_db_instances(DBInstanceIdentifier=RESTORED)["DBInstances"][0]
    assert described["MasterUserSecret"]["SecretArn"].startswith("arn:aws:secretsmanager:")
    _assert_no_master_password_in_any_request(session_factory, final.id)
    assert _recorded_instance(operator_settings, RESTORED).manage_master_user_password is True


def test_a_direct_point_in_time_restore_of_an_instance_asks_aws_to_manage_the_master_password(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "pitr_restore",
        "demo-orders",
        _pitr_options(),
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    request = _restore_call(session_factory, final.id, "rds:RestoreDBInstanceToPointInTime")
    assert "ManageMasterUserPassword" in request
    described = _rds().describe_db_instances(DBInstanceIdentifier=PITR)["DBInstances"][0]
    assert described["MasterUserSecret"]["SecretArn"].startswith("arn:aws:secretsmanager:")
    _assert_no_master_password_in_any_request(session_factory, final.id)
    assert _recorded_instance(operator_settings, PITR).manage_master_user_password is True


@pytest.mark.parametrize("restore_kind", ["snapshot_restore", "pitr_restore"])
def test_a_direct_cluster_restore_asks_aws_to_manage_the_master_password_in_the_follow_up_modify(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, restore_kind
) -> None:
    """No cluster restore call takes `ManageMasterUserPassword`; the one modify after the restore sends it."""
    if restore_kind == "snapshot_restore":
        launching_arn = _cluster_snapshot_arn(session_factory, operator_settings, config_store)
        options, launching_name, cluster_identifier = _cluster_options(), None, RESTORED_CLUSTER
        restore_api_name = "rds:RestoreDBClusterFromSnapshot"
    else:
        launching_arn = None
        options = _pitr_options(new_identifier=PITR_CLUSTER, instance_class="db.r6g.large")
        launching_name, cluster_identifier = "demo-aurora", PITR_CLUSTER
        restore_api_name = "rds:RestoreDBClusterToPointInTime"
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        restore_kind,
        launching_name,
        options,
        **({"resource_arn": launching_arn} if launching_arn else {}),
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "ManageMasterUserPassword" not in _restore_call(session_factory, final.id, restore_api_name)
    [(step_name, modify)] = _modify_calls(session_factory, final.id, "rds:ModifyDBCluster")
    assert step_name == "verify_direct" and modify["DBClusterIdentifier"] == cluster_identifier
    assert "ManageMasterUserPassword" in modify and modify["ApplyImmediately"] is True
    described = _rds().describe_db_clusters(DBClusterIdentifier=cluster_identifier)["DBClusters"][0]
    assert described["MasterUserSecret"]["SecretArn"].startswith("arn:aws:secretsmanager:")
    _assert_no_master_password_in_any_request(session_factory, final.id)
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    restored = next(c for c in recorded.clusters if c.identifier == cluster_identifier)
    assert restored.manage_master_user_password is True


@pytest.mark.parametrize("restore_kind", ["snapshot_restore", "pitr_restore", "cluster_snapshot_restore"])
def test_the_review_of_a_direct_restore_says_aws_keeps_a_new_master_password_in_secrets_manager(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, restore_kind
) -> None:
    """The approver sees that the restored resource's master password is a new one kept in Secrets Manager.

    Every restore sends `ManageMasterUserPassword`, so the password the snapshot or source carried no longer
    works on the restored resource; applications need the new secret. The change set says so.
    """
    if restore_kind == "snapshot_restore":
        launching_arn = _instance_snapshot_arn(session_factory, operator_settings, config_store)
        kind, launching_name, options = "snapshot_restore", None, _instance_options()
    elif restore_kind == "pitr_restore":
        launching_arn = None
        kind, launching_name, options = "pitr_restore", "demo-orders", _pitr_options()
    else:
        launching_arn = _cluster_snapshot_arn(session_factory, operator_settings, config_store)
        kind, launching_name, options = "snapshot_restore", None, _cluster_options()
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        kind,
        launching_name,
        options,
        **({"resource_arn": launching_arn} if launching_arn else {}),
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    review = {entry["attribute"]: entry["desired"] for entry in final.plan["change_set"]}
    assert "manage_master_user_password" in review, sorted(review)
    assert "Secrets Manager" in str(review["manage_master_user_password"])
    assert "new" in str(review["manage_master_user_password"])


def test_a_terraform_restore_renders_the_managed_password_and_never_a_password(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    created = _scripted_create(RESTORED_ADDRESS, "aws_db_instance", RESTORED)
    _, _, final = _restore_in_terraform_mode(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        FakeTerraformRunner,
        [created, created, {"resource_changes": []}],
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    instances_tf = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    block = instances_tf[instances_tf.index('"demo_orders_restored"') :].split("\n}\n")[0]
    assert re.search(r"manage_master_user_password\s+=\s+true", block)
    assert not PASSWORD_ASSIGNMENT.search(instances_tf), "no password is ever rendered"


def test_a_cluster_restore_whose_managed_password_modify_aws_refuses_says_how_to_recover(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from botocore.exceptions import ClientError
    from moto.rds.models import RDSBackend

    def refuse_the_modify(self, kwargs):  # noqa: ANN001, ANN202
        raise ClientError(
            {"Error": {"Code": "InvalidDBClusterStateFault", "Message": "the cluster is busy"}},
            "ModifyDBCluster",
        )

    snapshot_arn = _cluster_snapshot_arn(session_factory, operator_settings, config_store)
    monkeypatch.setattr(RDSBackend, "modify_db_cluster", refuse_the_modify)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_restore",
        None,
        _cluster_options(),
        resource_arn=snapshot_arn,
    )
    assert final.state == OperationState.FAILED
    assert final.plan["applied_without_record"] is True
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "post_create_settings_not_applied"
    )
    assert diagnostic.step_name == "verify_direct" and "the cluster is busy" in diagnostic.detail
    assert diagnostic.fix_suggestion.startswith(
        f"{RESTORED_CLUSTER} was restored without ManageMasterUserPassword"
    )
    assert "Manage master credentials in AWS Secrets Manager" in diagnostic.fix_suggestion
