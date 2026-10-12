"""Create an instance or an Aurora cluster from the template, in both modes, with a managed password."""

from __future__ import annotations

import json
import re
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from rdscommander.catalogue.catalogue import GROUP_LIFECYCLE, actions_for_environment, definition_for
from rdscommander.control_db.models import DriftFinding, ExecutionRecord, ResourceChangeEvent
from rdscommander.lifecycle import direct_api as lifecycle_api
from rdscommander.lifecycle.creates import (
    CLUSTER_TEMPLATE_DEFAULTS,
    INSTANCE_TEMPLATE_DEFAULTS,
    ClusterCreateExecutor,
    InstanceCreateExecutor,
)
from rdscommander.lifecycle.options import ClusterCreateOptions, InstanceCreateOptions
from rdscommander.operations.create_change import NEW_RESOURCE_DOWNTIME
from rdscommander.operations.enums import OperationState
from rdscommander.operations.service import OperationService
from rdscommander.resources.model import ResourceKind
from rdscommander.terraform.record import read_record
from tests.lifecycle.test_creates import _direct_client, _git_output, _scripted_create
from tests.operations.test_change_strategy import _direct_store, _runner, _workspace_dir
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _prepare_repo

NEW_INSTANCE = "demo-reports"
NEW_INSTANCE_ADDRESS = "aws_db_instance.demo_reports"
NEW_CLUSTER = "demo-analytics"
NEW_CLUSTER_ADDRESS = "aws_rds_cluster.demo_analytics"
WRITER = "demo-analytics-1"
READER = "demo-analytics-2"
WRITER_ADDRESS = "aws_rds_cluster_instance.demo_analytics_1"
READER_ADDRESS = "aws_rds_cluster_instance.demo_analytics_2"
PASSWORD_ASSIGNMENT = re.compile(r"\bpassword\s*=|master_password|MasterUserPassword\b")
"""A password set in the files: `password =`, `master_password`; `manage_master_user_password` is not one."""


def _rds():  # noqa: ANN202
    return boto3.client("rds", region_name="eu-west-2")


def _security_group_id() -> str:
    """The id of the simulated estate's `demo-db-access` security group."""
    groups = boto3.client("ec2", region_name="eu-west-2").describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["demo-db-access"]}]
    )["SecurityGroups"]
    return groups[0]["GroupId"]


def _instance_options(**overrides: Any) -> dict[str, Any]:
    return {
        "target_key": TARGET_KEY,
        "identifier": NEW_INSTANCE,
        "engine_version": "16.4",
        "instance_class": "db.m6g.large",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": [_security_group_id()],
        "parameter_group_name": "demo-postgres16-tuned",
        **overrides,
    }


def _cluster_options(**overrides: Any) -> dict[str, Any]:
    return {
        "target_key": TARGET_KEY,
        "identifier": NEW_CLUSTER,
        "engine_version": "16.4",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": [_security_group_id()],
        "cluster_parameter_group_name": "demo-aurora-pg16-tuned",
        "writer_instance_class": "db.r6g.large",
        "reader_count": 1,
        **overrides,
    }


def _create(service: OperationService, kind: str, options: dict[str, Any], key: str):  # noqa: ANN202
    """Create the operation the way the route does for an environment-level create."""
    title = {"instance_create": "Create instance", "cluster_create": "Create cluster"}[kind]
    return service.create(
        kind,
        "sandbox",
        TARGET_KEY,
        {**options, "options": options, "resource_arn": None, "resource_kind": None},
        key,
        "phil",
        title=f"{title}: {options['identifier']}",
    )


def _run_direct(session_factory, operator_settings, config_store, kind: str, options: dict[str, Any]):  # noqa: ANN202
    """Create the operation in direct mode, run it to approval, approve and run it to the end."""
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    operation = _create(service, kind, options, f"{kind}-direct")
    runner.run_once()
    if service.get(operation.id).state == OperationState.FAILED:
        return service.get(operation.id)
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    service.approve(operation.id, "sam")
    runner.run_once()
    return service.get(operation.id)


def _request_bodies(session_factory, operation_id) -> list[dict[str, Any]]:
    with session_factory() as session:
        return [
            record.request_parameters or {}
            for record in session.query(ExecutionRecord).filter_by(operation_id=operation_id)
        ]


def _aws_request(session_factory, operation_id, api_name: str) -> list[dict[str, Any]]:
    with session_factory() as session:
        return [
            record.request_parameters
            for record in session.query(ExecutionRecord).filter_by(
                operation_id=operation_id, record_kind="aws_api", api_name=api_name
            )
        ]


def _assert_no_password_was_sent_or_recorded(session_factory, operation_id) -> None:
    """No request body has a `MasterUserPassword` key (`ManageMasterUserPassword` asks AWS to make one)."""
    for body in _request_bodies(session_factory, operation_id):
        assert '"MasterUserPassword"' not in json.dumps(body), body


# ----- instance, direct mode -----------------------------------------------------------------------------


def test_create_instance_direct_builds_the_template_with_a_managed_password_and_no_secret_in_the_console(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final = _run_direct(
        session_factory, operator_settings, config_store, "instance_create", _instance_options()
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    described = _rds().describe_db_instances(DBInstanceIdentifier=NEW_INSTANCE)["DBInstances"][0]
    assert described["StorageEncrypted"] is True
    assert described["BackupRetentionPeriod"] == 7
    assert described["DeletionProtection"] is True
    assert described["MasterUsername"] == "postgres_admin"
    assert described["MasterUserSecret"]["SecretArn"].startswith("arn:aws:secretsmanager:")
    assert final.resource_arn == described["DBInstanceArn"], "the operation points at the new instance"
    assert final.plan["created_resource_arns"] == [described["DBInstanceArn"]]
    assert final.plan["touched_addresses"] == [NEW_INSTANCE_ADDRESS]
    [request] = _aws_request(session_factory, final.id, "rds:CreateDBInstance")
    # The recorder redacts every key naming a password; AWS reporting the secret shows what was asked.
    assert "ManageMasterUserPassword" in request and "MasterUserPassword" not in request
    assert (request["StorageEncrypted"], request["BackupRetentionPeriod"]) == (True, 7)
    assert (request["EnablePerformanceInsights"], request["DeletionProtection"]) == (True, True)
    assert (request["AutoMinorVersionUpgrade"], request["CopyTagsToSnapshot"]) == (True, True)
    assert request["DBParameterGroupName"] == "demo-postgres16-tuned"
    assert request["DBSubnetGroupName"] == "demo-private-subnets"
    assert request["VpcSecurityGroupIds"] == [_security_group_id()]
    assert request["Tags"] == [{"Key": "rdscommander:managed", "Value": "true"}]
    _assert_no_password_was_sent_or_recorded(session_factory, final.id)
    review = {entry["attribute"]: entry["desired"] for entry in final.plan["change_set"]}
    assert review["presence"] == f"aws_db_instance {NEW_INSTANCE}"
    assert review["instance_class"] == "db.m6g.large" and review["allocated_storage"] == 100
    assert review["engine_version"] == "16.4" and review["parameter_group"] == "demo-postgres16-tuned"
    assert review["subnet_group"] == "demo-private-subnets"
    assert review["vpc_security_group_ids"] == [_security_group_id()]
    assert not [attribute for attribute in review if "password" in attribute], "no password in the change set"
    assert final.plan["change_set"][0]["downtime"] == NEW_RESOURCE_DOWNTIME
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    instance = next(i for i in recorded.instances if i.identifier == NEW_INSTANCE)
    assert instance.manage_master_user_password is True
    assert instance.parameter_group_reference == "aws_db_parameter_group.demo_postgres16_tuned.name"
    instances_tf = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    block = instances_tf[instances_tf.index('"demo_reports"') :].split("\n}\n")[0]
    assert re.search(r"manage_master_user_password\s+=\s+true", block)
    assert not PASSWORD_ASSIGNMENT.search(instances_tf)
    with session_factory() as session:
        [event] = session.query(ResourceChangeEvent).filter_by(
            resource_arn=described["DBInstanceArn"], attribute_path="created"
        )
    assert event.explanation == "operation" and event.operation_id == final.id


def test_a_direct_instance_create_leaves_no_record_drift(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final = _run_direct(
        session_factory, operator_settings, config_store, "instance_create", _instance_options()
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    detect = OperationService(session_factory).create(
        "drift_detect", "sandbox", None, {"options": {}}, "after-create", "phil"
    )
    runner.run_once()
    assert OperationService(session_factory).get(detect.id).state == OperationState.SUCCEEDED
    with session_factory() as session:
        findings = [
            finding.attributes
            for finding in session.query(DriftFinding).filter_by(resolved_at_utc=None)
            if any(attribute.get("address") == NEW_INSTANCE_ADDRESS for attribute in finding.attributes)
        ]
    assert findings == []


def test_create_instance_refuses_an_existing_identifier_before_approval(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final = _run_direct(
        session_factory,
        operator_settings,
        config_store,
        "instance_create",
        _instance_options(identifier="demo-orders"),
    )
    assert final.state == OperationState.FAILED
    [failed_step] = [a for a in final.step_attempts if a.outcome_status == "failed"]
    assert failed_step.step_name == "compute_change_set"
    assert any(d.diagnostic_kind == "resource_already_exists" for d in final.diagnostic_reports)
    assert final.approved_at_utc is None


# ----- instance, Terraform mode --------------------------------------------------------------------------


class _InstanceBuildingTerraformRunner(FakeTerraformRunner):
    """A scripted Terraform whose apply creates the instance in the simulated account, as tofu would."""

    def apply(
        self, workspace_dir, target, settings, *, operation_id, step_name, plan_file_name="console.tfplan"
    ):  # noqa: ANN001, ANN201
        _rds().create_db_instance(
            DBInstanceIdentifier=NEW_INSTANCE,
            Engine="postgres",
            EngineVersion="16.4",
            DBInstanceClass="db.m6g.large",
            AllocatedStorage=100,
            StorageType="gp3",
            MasterUsername="postgres_admin",
            ManageMasterUserPassword=True,
            DBSubnetGroupName="demo-private-subnets",
            VpcSecurityGroupIds=[_security_group_id()],
            DBParameterGroupName="demo-postgres16-tuned",
            BackupRetentionPeriod=7,
            StorageEncrypted=True,
            DeletionProtection=True,
            CopyTagsToSnapshot=True,
            Tags=[{"Key": "rdscommander:managed", "Value": "true"}],
        )
        return super().apply(
            workspace_dir,
            target,
            settings,
            operation_id=operation_id,
            step_name=step_name,
            plan_file_name=plan_file_name,
        )


def _create_in_terraform_mode(
    session_factory, operator_settings, config_store, kind: str, options: dict[str, Any], fake
):  # noqa: ANN202
    """Create the operation in Terraform mode with nothing adopted, approve it and run it to the end.

    `fake` is a `FakeTerraformRunner` built without a recorder; it gets the runner's.
    """
    _prepare_repo(operator_settings)
    runner, services = _runner(session_factory, operator_settings, config_store)
    fake.recorder = services.execution_recorder
    services.terraform_runner = fake
    service = OperationService(session_factory)
    operation = _create(service, kind, options, f"{kind}-tf")
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    service.approve(operation.id, "sam")
    runner.run_once()
    return service.get(operation.id)


def test_create_instance_in_terraform_mode_renders_manage_master_user_password(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    created = _scripted_create(NEW_INSTANCE_ADDRESS, "aws_db_instance", NEW_INSTANCE)
    fake = FakeTerraformRunner(None, plan_scripts=[created, created, {"resource_changes": []}])
    final = _create_in_terraform_mode(
        session_factory, operator_settings, config_store, "instance_create", _instance_options(), fake
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "apply" in fake.calls
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    expected_arn = ":".join(group_arn.split(":")[:5]) + f":db:{NEW_INSTANCE}"
    assert final.plan["created_resource_arns"] == [expected_arn]
    assert final.resource_arn == expected_arn
    instances_tf = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    block = instances_tf[instances_tf.index('"demo_reports"') :].split("\n}\n")[0]
    assert re.search(r"manage_master_user_password\s+=\s+true", block)
    assert not PASSWORD_ASSIGNMENT.search(instances_tf), "no password is ever rendered"
    assert re.search(r'username\s+=\s+"postgres_admin"', block)
    assert re.search(r"storage_encrypted\s+=\s+true", block)
    assert re.search(r"backup_retention_period\s+=\s+7", block)
    assert re.search(r"deletion_protection\s+=\s+true", block)
    assert re.search(r"performance_insights_enabled\s+=\s+true", block)
    assert re.search(r'db_subnet_group_name\s+=\s+"demo-private-subnets"', block), "nothing adopted: a name"
    assert re.search(r'parameter_group_name\s+=\s+"demo-postgres16-tuned"', block)
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    instance = next(i for i in recorded.instances if i.identifier == NEW_INSTANCE)
    assert instance.manage_master_user_password is True


def test_create_instance_in_terraform_mode_records_the_instance_as_built_with_its_managed_password(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    created = _scripted_create(NEW_INSTANCE_ADDRESS, "aws_db_instance", NEW_INSTANCE)
    clean = {"resource_changes": []}
    fake = _InstanceBuildingTerraformRunner(None, plan_scripts=[created, created, clean, clean])
    final = _create_in_terraform_mode(
        session_factory, operator_settings, config_store, "instance_create", _instance_options(), fake
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    as_built = next(a for a in final.step_attempts if a.step_name == "record_as_built")
    assert as_built.output.get("recorded_as_built") is not False, as_built.output
    assert _git_output(operator_settings, "log", "-1", "--format=%s").endswith("(record as built)")
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    instance = next(i for i in recorded.instances if i.identifier == NEW_INSTANCE)
    assert instance.manage_master_user_password is True
    assert instance.master_username == "postgres_admin"
    assert not PASSWORD_ASSIGNMENT.search((_workspace_dir(operator_settings) / "instances.tf").read_text())


# ----- cluster -------------------------------------------------------------------------------------------


def test_create_cluster_direct_creates_the_cluster_and_its_members(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final = _run_direct(
        session_factory, operator_settings, config_store, "cluster_create", _cluster_options()
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    cluster = _rds().describe_db_clusters(DBClusterIdentifier=NEW_CLUSTER)["DBClusters"][0]
    assert [(m["DBInstanceIdentifier"], m["IsClusterWriter"]) for m in cluster["DBClusterMembers"]] == [
        (WRITER, True),
        (READER, False),
    ]
    assert cluster["StorageEncrypted"] is True and cluster["DeletionProtection"] is True
    assert cluster["BackupRetentionPeriod"] == 7
    assert cluster["MasterUserSecret"]["SecretArn"].startswith("arn:aws:secretsmanager:")
    assert final.resource_arn == cluster["DBClusterArn"], "the operation points at the new cluster"
    member_arns = [
        _rds().describe_db_instances(DBInstanceIdentifier=member)["DBInstances"][0]["DBInstanceArn"]
        for member in (WRITER, READER)
    ]
    assert final.plan["created_resource_arns"] == [cluster["DBClusterArn"], *member_arns]
    assert final.plan["touched_addresses"] == [NEW_CLUSTER_ADDRESS, WRITER_ADDRESS, READER_ADDRESS]
    presences = [e["desired"] for e in final.plan["change_set"] if e["attribute"] == "presence"]
    assert presences == [
        f"aws_rds_cluster {NEW_CLUSTER}",
        f"aws_rds_cluster_instance {WRITER}",
        f"aws_rds_cluster_instance {READER}",
    ]
    [request] = _aws_request(session_factory, final.id, "rds:CreateDBCluster")
    assert "ManageMasterUserPassword" in request and "MasterUserPassword" not in request
    assert request["DBClusterParameterGroupName"] == "demo-aurora-pg16-tuned"
    assert request["Engine"] == "aurora-postgresql" and request["EngineVersion"] == "16.4"
    member_requests = _aws_request(session_factory, final.id, "rds:CreateDBInstance")
    assert [(r["DBInstanceIdentifier"], r["DBClusterIdentifier"]) for r in member_requests] == [
        (WRITER, NEW_CLUSTER),
        (READER, NEW_CLUSTER),
    ]
    assert all(r["EnablePerformanceInsights"] is True and r["PromotionTier"] == 1 for r in member_requests)
    _assert_no_password_was_sent_or_recorded(session_factory, final.id)
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    created = next(c for c in recorded.clusters if c.identifier == NEW_CLUSTER)
    assert created.manage_master_user_password is True
    assert {m.identifier for m in recorded.cluster_instances} >= {WRITER, READER}
    clusters_tf = (_workspace_dir(operator_settings) / "clusters.tf").read_text()
    assert re.search(r"manage_master_user_password\s+=\s+true", clusters_tf)
    assert not PASSWORD_ASSIGNMENT.search(clusters_tf)


def test_create_cluster_without_readers_creates_only_the_writer(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final = _run_direct(
        session_factory, operator_settings, config_store, "cluster_create", _cluster_options(reader_count=0)
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    cluster = _rds().describe_db_clusters(DBClusterIdentifier=NEW_CLUSTER)["DBClusters"][0]
    assert [m["DBInstanceIdentifier"] for m in cluster["DBClusterMembers"]] == [WRITER]


def test_create_cluster_in_terraform_mode_renders_the_cluster_and_its_members(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    created = {
        "resource_changes": [
            _scripted_create(address, resource_type, identifier)["resource_changes"][0]
            for address, resource_type, identifier in (
                (NEW_CLUSTER_ADDRESS, "aws_rds_cluster", NEW_CLUSTER),
                (WRITER_ADDRESS, "aws_rds_cluster_instance", WRITER),
                (READER_ADDRESS, "aws_rds_cluster_instance", READER),
            )
        ]
    }
    fake = FakeTerraformRunner(None, plan_scripts=[created, created, {"resource_changes": []}])
    final = _create_in_terraform_mode(
        session_factory,
        operator_settings,
        config_store,
        "cluster_create",
        _cluster_options(reader_instance_class="db.r6g.xlarge"),
        fake,
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora-pg16-tuned")
    prefix = ":".join(group_arn.split(":")[:5])
    assert final.plan["created_resource_arns"] == [
        f"{prefix}:cluster:{NEW_CLUSTER}",
        f"{prefix}:db:{WRITER}",
        f"{prefix}:db:{READER}",
    ]
    assert final.resource_arn == f"{prefix}:cluster:{NEW_CLUSTER}"
    clusters_tf = (_workspace_dir(operator_settings) / "clusters.tf").read_text()
    cluster_block = clusters_tf[clusters_tf.index('"demo_analytics"') :].split("\n}\n")[0]
    assert re.search(r"manage_master_user_password\s+=\s+true", cluster_block)
    assert re.search(r'db_cluster_parameter_group_name\s+=\s+"demo-aurora-pg16-tuned"', cluster_block)
    assert re.search(r"storage_encrypted\s+=\s+true", cluster_block)
    assert not PASSWORD_ASSIGNMENT.search(clusters_tf)
    writer_block = clusters_tf[clusters_tf.index('"demo_analytics_1"') :].split("\n}\n")[0]
    reader_block = clusters_tf[clusters_tf.index('"demo_analytics_2"') :].split("\n}\n")[0]
    assert re.search(r"cluster_identifier\s+=\s+aws_rds_cluster\.demo_analytics\.id", writer_block)
    assert re.search(r'instance_class\s+=\s+"db\.r6g\.large"', writer_block)
    assert re.search(r'instance_class\s+=\s+"db\.r6g\.xlarge"', reader_block)
    assert re.search(r"performance_insights_enabled\s+=\s+true", reader_block)


def test_create_cluster_refuses_a_member_name_an_instance_already_has(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _rds().create_db_instance(
        DBInstanceIdentifier=READER,
        Engine="postgres",
        DBInstanceClass="db.m6g.large",
        AllocatedStorage=20,
        MasterUsername="someone",
        MasterUserPassword="not-a-console-value",
    )
    final = _run_direct(
        session_factory, operator_settings, config_store, "cluster_create", _cluster_options()
    )
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "resource_already_exists")
    assert f"{READER} already exists" in diagnostic.detail
    with pytest.raises(ClientError):
        _rds().describe_db_clusters(DBClusterIdentifier=NEW_CLUSTER)


def test_a_cluster_whose_reader_cannot_be_created_says_how_to_recover(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    real_create_member = lifecycle_api.create_cluster_member

    def refuse_the_reader(context, arguments, step_name="apply_direct"):  # noqa: ANN001, ANN202
        if arguments["DBInstanceIdentifier"] == READER:
            raise ClientError(
                {"Error": {"Code": "InsufficientDBInstanceCapacity", "Message": "no capacity"}},
                "CreateDBInstance",
            )
        return real_create_member(context, arguments, step_name)

    monkeypatch.setattr(lifecycle_api, "create_cluster_member", refuse_the_reader)
    final = _run_direct(
        session_factory, operator_settings, config_store, "cluster_create", _cluster_options()
    )
    assert final.state == OperationState.FAILED
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "cluster_member_not_created"
    )
    assert "no capacity" in diagnostic.detail
    assert diagnostic.fix_suggestion == (
        f"the cluster {NEW_CLUSTER} exists with {WRITER} but without {READER}; run Refresh inventory, then "
        "Add reader on it for each one missing"
    )
    assert final.plan["applied_without_record"] is True
    assert len(final.plan["created_resource_arns"]) == 2, "the cluster and its writer are attributed"


def test_a_cluster_whose_writer_cannot_be_created_says_it_has_no_writer(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    def refuse(context, arguments, step_name="apply_direct"):  # noqa: ANN001, ANN202
        raise ClientError({"Error": {"Code": "Throttling", "Message": "slow down"}}, "CreateDBInstance")

    monkeypatch.setattr(lifecycle_api, "create_cluster_member", refuse)
    final = _run_direct(
        session_factory, operator_settings, config_store, "cluster_create", _cluster_options()
    )
    assert final.state == OperationState.FAILED
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "cluster_member_not_created"
    )
    assert diagnostic.fix_suggestion.startswith(f"the cluster {NEW_CLUSTER} exists without a writer")


# ----- requests through the API, options and the catalogue -----------------------------------------------


def _post_create(client, headers, kind: str, options: dict[str, Any], key: str):  # noqa: ANN202
    return client.post(
        "/api/v1/operations",
        headers=headers,
        json={"kind": kind, "environment_slug": "sandbox", "options": options, "idempotency_key": key},
    )


def test_create_instance_requires_a_parameter_group_and_refuses_the_aws_default(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    without_group = _instance_options()
    del without_group["parameter_group_name"]
    missing = _post_create(client, headers, "instance_create", without_group, "create-no-group")
    assert missing.status_code == 422
    assert "parameter_group_name: is required" in missing.json()["problems"]
    default = _post_create(
        client,
        headers,
        "instance_create",
        _instance_options(parameter_group_name="default.postgres16"),
        "dflt",
    )
    assert default.status_code == 422
    assert "default.postgres16 is an AWS default parameter group" in " ".join(default.json()["problems"])
    refusals = {
        "unknown": (_instance_options(parameter_group_name="nope"), "no instance parameter group named nope"),
        "family": (
            _instance_options(engine_version="17.2"),
            "demo-postgres16-tuned is a postgres16 group; postgres 17.2 takes",
        ),
        "scope": (
            _instance_options(parameter_group_name="demo-aurora-pg16-tuned"),
            "no instance parameter group named demo-aurora-pg16-tuned",
        ),
        "subnet": (_instance_options(subnet_group_name="nope"), "no DB subnet group named nope"),
        "taken": (
            _instance_options(identifier="demo-orders"),
            "an instance named demo-orders already exists",
        ),
    }
    for key, (options, problem) in refusals.items():
        response = _post_create(client, headers, "instance_create", options, f"create-{key}")
        assert response.status_code == 422, (key, response.json())
        assert problem in " ".join(response.json()["problems"]), (key, response.json())


def test_create_instance_through_the_api_titles_the_operation_and_puts_it_on_the_target(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    accepted = _post_create(client, headers, "instance_create", _instance_options(), "create-ok")
    assert accepted.status_code == 202, accepted.json()
    detail = client.get(f"/api/v1/operations/{accepted.json()['operation_id']}").json()
    assert detail["title"] == f"Create instance: {NEW_INSTANCE}"
    assert detail["target_key"] == TARGET_KEY
    assert detail["resource_arn"] is None, "it points at the instance once the instance exists"
    assert detail["approval_required_text"] is None


def test_create_cluster_through_the_api_checks_the_cluster_group_and_the_member_names(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    refusals = {
        "default": (
            _cluster_options(cluster_parameter_group_name="default.aurora-postgresql16"),
            "default.aurora-postgresql16 is an AWS default parameter group",
        ),
        "scope": (
            _cluster_options(cluster_parameter_group_name="demo-postgres16-tuned"),
            "no cluster parameter group named demo-postgres16-tuned",
        ),
        "taken": (_cluster_options(identifier="demo-aurora"), "a cluster named demo-aurora already exists"),
    }
    for key, (options, problem) in refusals.items():
        response = _post_create(client, headers, "cluster_create", options, f"cluster-{key}")
        assert response.status_code == 422, (key, response.json())
        assert problem in " ".join(response.json()["problems"]), (key, response.json())
    accepted = _post_create(client, headers, "cluster_create", _cluster_options(), "cluster-ok")
    assert accepted.status_code == 202, accepted.json()
    detail = client.get(f"/api/v1/operations/{accepted.json()['operation_id']}").json()
    assert detail["title"] == f"Create cluster: {NEW_CLUSTER}"


def test_create_options_explain_refused_values_in_sentences() -> None:
    base = {
        "target_key": TARGET_KEY,
        "identifier": NEW_INSTANCE,
        "engine_version": "16.4",
        "instance_class": "db.m6g.large",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": ["sg-0123456789abcdef0"],
        "parameter_group_name": "arn:aws:rds:eu-west-2:000000000000:pg:demo-postgres16-tuned",
    }
    options = InstanceCreateOptions.model_validate({**base, "db_name": " "})
    assert options.parameter_group_name == "demo-postgres16-tuned" and options.db_name is None
    assert (options.master_username, options.storage_type, options.port) == ("postgres_admin", "gp3", 5432)
    for overrides, sentence in (
        ({"identifier": "Bad_Name"}, "an instance name starts with a lower-case letter"),
        ({"max_allocated_storage_gb": 50}, "the autoscaling ceiling must be above the allocated storage"),
        ({"vpc_security_group_ids": ["default"]}, "is not a security group id"),
        (
            {"master_username": "1admin"},
            "a master user name must start with a letter and contain only letters, digits and underscores",
        ),
        ({"storage_type": "io1"}, "Input should be 'gp3' or 'gp2'"),
    ):
        with pytest.raises(ValidationError, match=re.escape(sentence)):
            InstanceCreateOptions.model_validate({**base, **overrides})
    cluster = {
        "target_key": TARGET_KEY,
        "identifier": NEW_CLUSTER,
        "engine_version": "16.4",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": ["sg-0123456789abcdef0"],
        "cluster_parameter_group_name": "demo-aurora-pg16-tuned",
        "writer_instance_class": "db.r6g.large",
        "reader_instance_class": "",
        "reader_count": 2,
    }
    parsed = ClusterCreateOptions.model_validate(cluster)
    assert parsed.reader_instance_class is None
    assert parsed.member_identifiers() == [WRITER, READER, "demo-analytics-3"]
    with pytest.raises(ValidationError, match="less than or equal to 5"):
        ClusterCreateOptions.model_validate({**cluster, "reader_count": 6})
    with pytest.raises(ValidationError, match="at most 61 characters"):
        ClusterCreateOptions.model_validate({**cluster, "identifier": "a" * 62})


def test_the_template_fixes_encryption_backups_protection_and_a_managed_password() -> None:
    assert INSTANCE_TEMPLATE_DEFAULTS == {
        "storage_encrypted": True,
        "backup_retention_period": 7,
        "performance_insights_enabled": True,
        "deletion_protection": True,
        "auto_minor_version_upgrade": True,
        "copy_tags_to_snapshot": True,
        "manage_master_user_password": True,
        "monitoring_interval": 0,
    }
    assert CLUSTER_TEMPLATE_DEFAULTS["manage_master_user_password"] is True
    for model in (InstanceCreateOptions, ClusterCreateOptions):
        assert not [name for name in model.model_fields if "password" in name], "no option carries a password"


def test_creates_are_environment_level_lifecycle_operations_in_both_modes(console_config) -> None:
    for kind, executor in (
        ("instance_create", InstanceCreateExecutor),
        ("cluster_create", ClusterCreateExecutor),
    ):
        definition = definition_for(kind)
        assert executor.operation_kind == kind
        assert definition.group == GROUP_LIFECYCLE
        assert definition.applies_to == frozenset({ResourceKind.ENVIRONMENT})
        assert definition.needs_target is True
        assert definition.modes == frozenset({"terraform", "direct"})
        assert not definition.destructive and not definition.disruptive
    for mode in ("terraform", "direct"):
        environment = console_config.environment("sandbox").model_copy(update={"change_management": mode})
        actions = {action.kind: action for action in actions_for_environment(environment)}
        assert actions["instance_create"].applicable and actions["cluster_create"].applicable


def test_create_cluster_in_terraform_mode_creates_the_readers_after_the_writer(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """tofu creates independent resources in parallel; the first member AWS has becomes the writer."""
    members = [f"{NEW_CLUSTER}-{position}" for position in (1, 2, 3)]
    created = {
        "resource_changes": [
            _scripted_create(NEW_CLUSTER_ADDRESS, "aws_rds_cluster", NEW_CLUSTER)["resource_changes"][0]
        ]
        + [
            _scripted_create(
                f"aws_rds_cluster_instance.{member.replace('-', '_')}", "aws_rds_cluster_instance", member
            )["resource_changes"][0]
            for member in members
        ]
    }
    fake = FakeTerraformRunner(None, plan_scripts=[created, created, {"resource_changes": []}])
    final = _create_in_terraform_mode(
        session_factory,
        operator_settings,
        config_store,
        "cluster_create",
        _cluster_options(reader_count=2),
        fake,
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    clusters_tf = (_workspace_dir(operator_settings) / "clusters.tf").read_text()
    blocks = {
        member: clusters_tf[clusters_tf.index(f'"{member.replace("-", "_")}"') :].split("\n}\n")[0]
        for member in members
    }
    assert "depends_on" not in blocks[WRITER]
    for reader in members[1:]:
        assert re.search(r"depends_on\s+=\s+\[aws_rds_cluster_instance\.demo_analytics_1\]", blocks[reader])
    assert len(re.findall(r"depends_on\s+=", clusters_tf)) == 2


def test_the_order_of_creation_is_not_record_drift(sandbox_render_input) -> None:
    """A reader recorded with `depends_on_member` matches the same reader rendered from the inventory."""
    from rdscommander.terraform.record import diff_records

    recorded = sandbox_render_input.model_copy(deep=True)
    reader = next(m for m in recorded.cluster_instances if m.identifier == "demo-aurora-reader")
    reader.depends_on_member = "aws_rds_cluster_instance.demo_aurora_writer"
    assert diff_records(recorded, sandbox_render_input) == {}
