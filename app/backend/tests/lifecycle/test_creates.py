"""Create a read replica and add an Aurora reader, in both modes."""

from __future__ import annotations

import re

import boto3
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from rdscommander.catalogue.catalogue import GROUP_REPLICAS_AND_SCALING, definition_for, replica_source
from rdscommander.control_db.models import ExecutionRecord, ResourceChangeEvent
from rdscommander.lifecycle.creates import ClusterReaderCreateExecutor, ReadReplicaCreateExecutor
from rdscommander.lifecycle.options import ClusterReaderCreateOptions, ReadReplicaCreateOptions
from rdscommander.main import create_application
from rdscommander.operations.create_change import NEW_RESOURCE_DOWNTIME
from rdscommander.operations.enums import OperationState
from rdscommander.operations.service import OperationService
from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.record import read_record
from rdscommander.terraform.state_addresses import ADOPTED_STATUS
from tests.drift.test_detect_executor import _adopt_estate
from tests.lifecycle.test_removals import _adopt_aurora
from tests.lifecycle.test_settings_changes import _run
from tests.operations.test_change_strategy import _direct_store, _runner, _workspace_dir
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _prepare_repo

NEW_REPLICA = "demo-orders-replica-2"
NEW_REPLICA_ADDRESS = "aws_db_instance.demo_orders_replica_2"
NEW_READER = "demo-aurora-reader-2"
NEW_READER_ADDRESS = "aws_rds_cluster_instance.demo_aurora_reader_2"


def _sibling(arn: str, identifier: str) -> str:
    """The ARN of instance `identifier` in the account and region of `arn` (the simulator's account)."""
    return ":".join(arn.split(":")[:5]) + f":db:{identifier}"


def _describe(identifier: str) -> dict:
    return boto3.client("rds", region_name="eu-west-2").describe_db_instances(
        DBInstanceIdentifier=identifier
    )["DBInstances"][0]


def _direct_client(session_factory, operator_settings, console_config) -> tuple[TestClient, dict]:
    direct_config = console_config.model_copy(deep=True)
    direct_config.environment("sandbox").change_management = "direct"
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=direct_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    return client, {"X-CSRF-Token": token}


def _post(client, headers, kind: str, resource_arn: str, options: dict, key: str):  # noqa: ANN202
    return client.post(
        "/api/v1/operations",
        headers=headers,
        json={
            "kind": kind,
            "environment_slug": "sandbox",
            "resource_id": encode_resource_id(resource_arn),
            "options": options,
            "idempotency_key": key,
        },
    )


def _scripted_create(address: str, resource_type: str, identifier: str) -> dict:
    return {
        "resource_changes": [
            {
                "address": address,
                "type": resource_type,
                "change": {"actions": ["create"], "before": None, "after": {"identifier": identifier}},
            }
        ]
    }


def _adoption_status(session_factory, address: str):  # noqa: ANN202
    from rdscommander.control_db.models import ResourceAdoption

    with session_factory() as session:
        row = session.query(ResourceAdoption).filter_by(terraform_address=address).one()
        return row.adoption_status, row.resource_arn


def _refresh(session_factory, operator_settings, config_store, key: str):  # noqa: ANN202
    """Refresh the sandbox inventory (direct mode) and return the new snapshot."""
    from rdscommander.inventory.queries import latest_snapshot_for_target

    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    OperationService(session_factory).create("inventory_refresh", "sandbox", None, {}, key, "phil")
    runner.run_once()
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        session.expunge_all()
    return snapshot


def _inventory_arn(session_factory, identifier: str) -> str:
    """The ARN the latest inventory reports for instance `identifier` (history matches on this one)."""
    from rdscommander.control_db.models import InventoryDbInstance
    from rdscommander.inventory.queries import latest_snapshot_for_target

    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        row = (
            session.query(InventoryDbInstance)
            .filter_by(snapshot_id=snapshot.id, db_instance_identifier=identifier)
            .one()
        )
        return row.db_instance_arn


class _ReplicaBuildingTerraformRunner(FakeTerraformRunner):
    """A scripted Terraform whose apply creates the replica in the simulated account, as tofu would.

    AWS gives the replica no automated backups of its own, unlike its source; the record as built must say so.
    """

    def apply(
        self, workspace_dir, target, settings, *, operation_id, step_name, plan_file_name="console.tfplan"
    ):  # noqa: ANN001, ANN201
        rds = boto3.client("rds", region_name="eu-west-2")
        rds.create_db_instance_read_replica(
            DBInstanceIdentifier=NEW_REPLICA,
            SourceDBInstanceIdentifier="demo-orders",
            DBInstanceClass="db.m6g.large",
            Tags=[{"Key": "rdscommander:managed", "Value": "true"}],
        )
        rds.modify_db_instance(
            DBInstanceIdentifier=NEW_REPLICA, BackupRetentionPeriod=0, ApplyImmediately=True
        )
        return super().apply(
            workspace_dir,
            target,
            settings,
            operation_id=operation_id,
            step_name=step_name,
            plan_file_name=plan_file_name,
        )


# ----- read replica -------------------------------------------------------------------------------------


def test_create_read_replica_direct_creates_it_links_the_operation_and_records_it(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "replica_create",
        "demo-orders",
        {"replica_identifier": NEW_REPLICA, "instance_class": "db.m6g.large"},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert _describe(NEW_REPLICA)["ReadReplicaSourceDBInstanceIdentifier"] == "demo-orders"
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    assert final.resource_arn == orders_arn, "the operation stays on the instance it was launched from"
    replica_arn = _sibling(orders_arn, NEW_REPLICA)
    assert final.plan["created_resource_arns"] == [replica_arn]
    assert final.plan["touched_addresses"] == [NEW_REPLICA_ADDRESS]
    presence = final.plan["change_set"][0]
    assert (presence["attribute"], presence["current"], presence["desired"]) == (
        "presence",
        None,
        f"aws_db_instance {NEW_REPLICA}",
    )
    assert presence["downtime"] == NEW_RESOURCE_DOWNTIME
    assert {e["attribute"]: e["desired"] for e in final.plan["change_set"][1:]}["instance_class"] == (
        "db.m6g.large"
    )
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    replica = next(i for i in recorded.instances if i.identifier == NEW_REPLICA)
    assert replica.replicate_source_reference == "aws_db_instance.demo_orders.identifier"
    # Direct mode records after the closing refresh, so the record is the replica as AWS built it.
    described = _describe(NEW_REPLICA)
    assert replica.backup_retention_period == described["BackupRetentionPeriod"]
    assert replica.port == described["Endpoint"]["Port"]
    assert final.plan["created_resource_arns"] == [_inventory_arn(session_factory, NEW_REPLICA)]
    with session_factory() as session:
        [event] = session.query(ResourceChangeEvent).filter_by(
            resource_arn=replica_arn, attribute_path="created"
        )
    assert event.explanation == "operation" and event.operation_id == final.id


def test_create_read_replica_sends_the_chosen_settings_and_the_console_tag(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "replica_create",
        "demo-orders",
        {
            "replica_identifier": NEW_REPLICA,
            "instance_class": "db.m6g.xlarge",
            "multi_az": True,
            "storage_type": "io1",
            "iops": 3000,
        },
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    with session_factory() as session:
        [call] = session.query(ExecutionRecord).filter_by(
            operation_id=final.id, record_kind="aws_api", api_name="rds:CreateDBInstanceReadReplica"
        )
    assert call.request_parameters["DBInstanceIdentifier"] == NEW_REPLICA
    assert call.request_parameters["SourceDBInstanceIdentifier"] == "demo-orders"
    assert call.request_parameters["DBInstanceClass"] == "db.m6g.xlarge"
    assert call.request_parameters["MultiAZ"] is True
    assert (call.request_parameters["StorageType"], call.request_parameters["Iops"]) == ("io1", 3000)
    assert call.request_parameters["Tags"] == [{"Key": "rdscommander:managed", "Value": "true"}]


def test_create_read_replica_refuses_an_existing_identifier_before_approval(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "replica_create",
        "demo-orders",
        {"replica_identifier": "demo-orders-replica", "instance_class": "db.m6g.large"},
    )
    assert final.state == OperationState.FAILED
    [failed_step] = [a for a in final.step_attempts if a.outcome_status == "failed"]
    assert failed_step.step_name == "compute_change_set"
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "resource_already_exists")
    assert "demo-orders-replica already exists" in diagnostic.detail
    assert final.approved_at_utc is None


def test_create_read_replica_in_terraform_mode_allows_the_plain_create(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    created = _scripted_create(NEW_REPLICA_ADDRESS, "aws_db_instance", NEW_REPLICA)
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[created, created, {"resource_changes": []}]
    )
    runner.services.terraform_runner = fake
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "replica_create",
        "sandbox",
        TARGET_KEY,
        {
            "options": {
                "replica_identifier": NEW_REPLICA,
                "instance_class": "db.m6g.large",
                "multi_az": True,
            },
            "resource_arn": orders_arn,
        },
        "replica-create-tf",
        "phil",
        resource_arn=orders_arn,
        title=f"Create read replica: {NEW_REPLICA}",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert awaiting.plan["touched_addresses"] == [NEW_REPLICA_ADDRESS]
    assert awaiting.plan["downtime"] == NEW_RESOURCE_DOWNTIME and awaiting.plan["requires_reboot"] is False
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "apply" in fake.calls
    assert final.resource_arn == orders_arn
    assert final.plan["created_resource_arns"] == [_sibling(orders_arn, NEW_REPLICA)]
    as_built = next(a for a in final.step_attempts if a.step_name == "record_as_built")
    assert as_built.output["recorded_as_built"] is False, "the scripted apply created nothing in AWS"
    instances_tf = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    new_block = instances_tf[instances_tf.index(f'"{NEW_REPLICA.replace("-", "_")}"') :]
    assert re.search(r"replicate_source_db\s+=\s+aws_db_instance\.demo_orders\.identifier", new_block)
    assert re.search(r"multi_az\s+=\s+true", new_block.split("\n}\n")[0])
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    replica = next(i for i in recorded.instances if i.identifier == NEW_REPLICA)
    assert (replica.instance_class, replica.multi_az, replica.publicly_accessible, replica.tags) == (
        "db.m6g.large",
        True,
        False,
        {},
    )
    assert replica.manage_master_user_password is False and replica.snapshot_identifier is None
    status, adopted_arn = _adoption_status(session_factory, NEW_REPLICA_ADDRESS)
    assert status == ADOPTED_STATUS and adopted_arn.endswith(f":db:{NEW_REPLICA}")


def test_create_read_replica_in_terraform_mode_refuses_an_existing_identifier_before_rendering(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    fake = FakeTerraformRunner(runner.services.execution_recorder)
    runner.services.terraform_runner = fake
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "replica_create",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"replica_identifier": "demo-orders-replica", "instance_class": "db.m6g.large"},
            "resource_arn": orders_arn,
        },
        "replica-create-tf-taken",
        "phil",
        resource_arn=orders_arn,
    )
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    assert any(d.diagnostic_kind == "resource_already_exists" for d in final.diagnostic_reports)
    assert "init" not in fake.calls


def test_replica_needs_backups_on_the_source(
    session_factory, operator_settings, console_config, config_store, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=0, ApplyImmediately=True
    )
    snapshot = _refresh(session_factory, operator_settings, config_store, "no-backups")
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    response = _post(
        client,
        headers,
        "replica_create",
        arn_for(session_factory, snapshot, "demo-orders"),
        {"replica_identifier": NEW_REPLICA, "instance_class": "db.m6g.large"},
        "replica-without-backups",
    )
    assert response.status_code == 422
    assert "backups" in response.json()["detail"]


def test_create_read_replica_through_the_api_titles_the_operation_with_the_new_identifier(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    taken = _post(
        client,
        headers,
        "replica_create",
        orders_arn,
        {"replica_identifier": "demo-orders-replica", "instance_class": "db.m6g.large"},
        "replica-taken",
    )
    assert taken.status_code == 422 and "demo-orders-replica" in taken.json()["detail"]
    accepted = _post(
        client,
        headers,
        "replica_create",
        orders_arn,
        {"replica_identifier": NEW_REPLICA, "instance_class": "db.m6g.large"},
        "replica-new",
    )
    assert accepted.status_code == 202, accepted.json()
    detail = client.get(f"/api/v1/operations/{accepted.json()['operation_id']}").json()
    assert detail["title"] == f"Create read replica: {NEW_REPLICA}"
    assert detail["approval_required_text"] is None


def test_replica_source_explains_every_refusal(session_factory, console_config, sandbox_snapshot) -> None:
    environment = console_config.environment("sandbox")
    with session_factory() as session:
        orders, replica, writer, cluster = (
            load_resource(session, console_config, arn_for(session_factory, sandbox_snapshot, name))
            for name in ("demo-orders", "demo-orders-replica", "demo-aurora-writer", "demo-aurora")
        )
    adopted_orders = orders.model_copy(update={"ownership": "adopted"})
    assert replica_source(adopted_orders, environment).ok
    assert "adopt it first" in replica_source(orders, environment).reason
    assert replica_source(None, environment).reason == "pick an instance"
    assert replica_source(cluster, environment).reason == "applies to instances"
    assert replica_source(writer, environment).reason == (
        "this instance is a member of demo-aurora; add a reader to the cluster instead"
    )
    busy = adopted_orders.model_copy(update={"status": "modifying"})
    assert replica_source(busy, environment).reason == "the instance is modifying; wait until it is available"
    without_backups = adopted_orders.model_copy(
        update={"facts": {**adopted_orders.facts, "backup_retention_period": 0}}
    )
    assert "automated backups are off" in replica_source(without_backups, environment).reason
    managed_password = adopted_orders.model_copy(
        update={"raw": {**adopted_orders.raw, "MasterUserSecret": {"SecretArn": "arn:aws:secretsmanager:x"}}}
    )
    assert replica_source(managed_password, environment).reason == (
        "its master password is managed by Secrets Manager; AWS cannot create a read replica of it"
    )
    assert replica_source(replica.model_copy(update={"ownership": "adopted"}), environment).ok is (
        (replica.facts.get("backup_retention_period") or 0) > 0
    )


def test_replica_create_options_explain_refused_values_in_sentences() -> None:
    accepted = ReadReplicaCreateOptions(replica_identifier=NEW_REPLICA, instance_class="db.m6g.large")
    assert accepted.multi_az is False and accepted.storage_type is None and accepted.iops is None
    refusals = {
        "Demo": "starts with a lower-case letter",
        "demo--orders": "two hyphens in a row",
        "demo-orders-": "cannot end with a hyphen",
    }
    for identifier, sentence in refusals.items():
        with pytest.raises(ValidationError, match=sentence):
            ReadReplicaCreateOptions(replica_identifier=identifier, instance_class="db.m6g.large")
    with pytest.raises(ValidationError, match="not on gp2"):
        ReadReplicaCreateOptions(
            replica_identifier=NEW_REPLICA, instance_class="db.m6g.large", storage_type="gp2", iops=3000
        )
    with pytest.raises(ValidationError, match="io1 and io2 storage needs IOPS"):
        ReadReplicaCreateOptions(
            replica_identifier=NEW_REPLICA, instance_class="db.m6g.large", storage_type="io1"
        )


# ----- Aurora reader ------------------------------------------------------------------------------------


def test_add_aurora_reader_direct_creates_the_member(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_reader_add",
        "demo-aurora",
        {"reader_identifier": NEW_READER, "instance_class": "db.r6g.large", "promotion_tier": 2},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    cluster = boto3.client("rds", region_name="eu-west-2").describe_db_clusters(
        DBClusterIdentifier="demo-aurora"
    )["DBClusters"][0]
    assert NEW_READER in [member["DBInstanceIdentifier"] for member in cluster["DBClusterMembers"]]
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    assert final.resource_arn == cluster_arn
    assert final.plan["created_resource_arns"] == [_sibling(cluster_arn, NEW_READER)]
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    reader = next(member for member in recorded.cluster_instances if member.identifier == NEW_READER)
    assert reader.promotion_tier == 2
    assert reader.cluster_reference == "aws_rds_cluster.demo_aurora.id"
    with session_factory() as session:
        [call] = session.query(ExecutionRecord).filter_by(
            operation_id=final.id, record_kind="aws_api", api_name="rds:CreateDBInstance"
        )
    assert call.request_parameters["DBClusterIdentifier"] == "demo-aurora"
    assert call.request_parameters["Engine"] == "aurora-postgresql"
    assert call.request_parameters["PromotionTier"] == 2
    assert call.request_parameters["EnablePerformanceInsights"] is True
    assert "DBParameterGroupName" not in call.request_parameters
    assert call.request_parameters["Tags"] == [{"Key": "rdscommander:managed", "Value": "true"}]


def test_add_aurora_reader_in_terraform_mode_allows_the_plain_create(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_aurora(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    created = _scripted_create(NEW_READER_ADDRESS, "aws_rds_cluster_instance", NEW_READER)
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[created, created, {"resource_changes": []}]
    )
    runner.services.terraform_runner = fake
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = service.create(
        "cluster_reader_add",
        "sandbox",
        TARGET_KEY,
        {
            "options": {
                "reader_identifier": NEW_READER,
                "instance_class": "db.r6g.large",
                "promotion_tier": 3,
                "performance_insights_enabled": False,
            },
            "resource_arn": cluster_arn,
        },
        "reader-add-tf",
        "phil",
        resource_arn=cluster_arn,
        title=f"Add reader: {NEW_READER}",
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    service.approve(operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    clusters_tf = (_workspace_dir(operator_settings) / "clusters.tf").read_text()
    new_block = clusters_tf[clusters_tf.index('"demo_aurora_reader_2"') :].split("\n}\n")[0]
    assert re.search(r"cluster_identifier\s+=\s+aws_rds_cluster\.demo_aurora\.id", new_block)
    assert re.search(r"promotion_tier\s+=\s+3", new_block)
    assert re.search(r"performance_insights_enabled\s+=\s+false", new_block)
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    reader = next(member for member in recorded.cluster_instances if member.identifier == NEW_READER)
    assert (reader.engine, reader.engine_version, reader.monitoring_interval) == (
        "aurora-postgresql",
        "16.4",
        0,
    )
    assert reader.parameter_group_reference is None
    assert final.plan["created_resource_arns"] == [_sibling(cluster_arn, NEW_READER)]
    status, adopted_arn = _adoption_status(session_factory, NEW_READER_ADDRESS)
    assert status == ADOPTED_STATUS and adopted_arn.endswith(f":db:{NEW_READER}")


def test_add_aurora_reader_takes_a_parameter_group_of_the_cluster_family_only(
    session_factory, operator_settings, console_config, config_store, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").create_db_parameter_group(
        DBParameterGroupName="demo-aurora-members",
        DBParameterGroupFamily="aurora-postgresql16",
        Description="Aurora member parameters",
    )
    snapshot = _refresh(session_factory, operator_settings, config_store, "reader-group-refresh")
    client, headers = _direct_client(session_factory, operator_settings, console_config)
    cluster_arn = arn_for(session_factory, snapshot, "demo-aurora")
    wrong_family = _post(
        client,
        headers,
        "cluster_reader_add",
        cluster_arn,
        {
            "reader_identifier": NEW_READER,
            "instance_class": "db.r6g.large",
            "parameter_group_name": "demo-postgres16-tuned",
        },
        "reader-wrong-family",
    )
    assert wrong_family.status_code == 422
    assert "aurora-postgresql16" in " ".join(wrong_family.json()["problems"])
    group_arn = arn_for(session_factory, snapshot, "demo-aurora-members")
    accepted = _post(
        client,
        headers,
        "cluster_reader_add",
        cluster_arn,
        {
            "reader_identifier": NEW_READER,
            "instance_class": "db.r6g.large",
            "parameter_group_name": group_arn,
        },
        "reader-right-family",
    )
    assert accepted.status_code == 202, accepted.json()
    detail = client.get(f"/api/v1/operations/{accepted.json()['operation_id']}").json()
    assert detail["title"] == f"Add reader: {NEW_READER}"
    assert detail["plan"]["options"]["parameter_group_name"] == "demo-aurora-members"


def test_cluster_reader_create_options_have_tier_bounds_and_take_a_group_name_or_arn() -> None:
    options = ClusterReaderCreateOptions(reader_identifier=NEW_READER, instance_class="db.r6g.large")
    assert (options.promotion_tier, options.performance_insights_enabled, options.parameter_group_name) == (
        1,
        True,
        None,
    )
    for tier in (-1, 16):
        with pytest.raises(ValidationError):
            ClusterReaderCreateOptions(
                reader_identifier=NEW_READER, instance_class="db.r6g.large", promotion_tier=tier
            )
    by_arn = ClusterReaderCreateOptions(
        reader_identifier=NEW_READER,
        instance_class="db.r6g.large",
        parameter_group_name="arn:aws:rds:eu-west-2:000000000000:pg:demo-aurora-members",
    )
    assert by_arn.parameter_group_name == "demo-aurora-members"
    blank = ClusterReaderCreateOptions(
        reader_identifier=NEW_READER, instance_class="db.r6g.large", parameter_group_name=" "
    )
    assert blank.parameter_group_name is None


def test_create_definitions_are_replicas_and_scaling_and_do_not_change_their_source() -> None:
    for kind, executor in (
        ("replica_create", ReadReplicaCreateExecutor),
        ("cluster_reader_add", ClusterReaderCreateExecutor),
    ):
        definition = definition_for(kind)
        assert definition.group == GROUP_REPLICAS_AND_SCALING
        assert definition.changes_the_resource is False
        assert definition.destructive is False and definition.disruptive is False
        assert executor.allows_create is True and executor.operation_kind == kind
    assert definition_for("replica_create").title == "Create read replica"
    assert definition_for("cluster_reader_add").title == "Add reader"


# ----- record as built (Terraform mode) -----------------------------------------------------------------


def _git_output(operator_settings, *args: str) -> str:
    from tests.terraform.test_adoption_executor import _git

    return _git(*args, cwd=operator_settings.repo_root)


def _create_replica_in_terraform_mode(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, plan_scripts, key
):  # noqa: ANN001, ANN202
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    fake = _ReplicaBuildingTerraformRunner(runner.services.execution_recorder, plan_scripts=plan_scripts)
    runner.services.terraform_runner = fake
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "replica_create",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"replica_identifier": NEW_REPLICA, "instance_class": "db.m6g.large"},
            "resource_arn": orders_arn,
        },
        key,
        "phil",
        resource_arn=orders_arn,
        title=f"Create read replica: {NEW_REPLICA}",
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    service.approve(operation.id, "sam")
    runner.run_once()
    return runner, service, service.get(operation.id)


def test_create_read_replica_in_terraform_mode_records_the_replica_as_aws_built_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import DriftFinding

    created = _scripted_create(NEW_REPLICA_ADDRESS, "aws_db_instance", NEW_REPLICA)
    clean = {"resource_changes": []}
    runner, service, final = _create_replica_in_terraform_mode(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        [created, created, clean, clean],
        "replica-as-built",
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [a.step_name for a in final.step_attempts][-2:] == ["refresh_resource", "record_as_built"]
    assert "plan:record_as_built" in runner.services.terraform_runner.calls
    subject = f"Create read replica: {NEW_REPLICA} (phil, approved by sam)"
    assert _git_output(operator_settings, "log", "-2", "--format=%s").splitlines() == [
        f"{subject} (record as built)",
        subject,
    ]
    assert _git_output(operator_settings, "status", "--porcelain") == ""
    described = _describe(NEW_REPLICA)
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    replica = next(i for i in recorded.instances if i.identifier == NEW_REPLICA)
    assert replica.backup_retention_period == described["BackupRetentionPeriod"] == 0
    assert replica.port == described["Endpoint"]["Port"]
    assert replica.replicate_source_reference == "aws_db_instance.demo_orders.identifier"
    first_record = _git_output(
        operator_settings,
        "show",
        "HEAD~1:terraform/sandbox/000000000000/eu-west-2/console-record.json",
    )
    assert '"backup_retention_period": 1' in first_record, "the first commit recorded the desired render"
    assert final.plan["created_resource_arns"] == [_inventory_arn(session_factory, NEW_REPLICA)]

    detect = service.create("drift_detect", "sandbox", None, {"options": {}}, "after-as-built", "phil")
    runner.run_once()
    assert service.get(detect.id).state == OperationState.SUCCEEDED
    with session_factory() as session:
        replica_findings = [
            finding
            for finding in session.query(DriftFinding).filter_by(resolved_at_utc=None, finding_kind="record")
            if NEW_REPLICA_ADDRESS
            in {finding.resource_arn, *(attribute.get("address") for attribute in finding.attributes)}
        ]
    assert replica_findings == []


def test_a_record_as_built_that_does_not_plan_clean_fails_and_leaves_the_first_commit(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    created = _scripted_create(NEW_REPLICA_ADDRESS, "aws_db_instance", NEW_REPLICA)
    clean = {"resource_changes": []}
    residual = {
        "resource_changes": [
            {
                "address": NEW_REPLICA_ADDRESS,
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"backup_retention_period": 0},
                    "after": {"backup_retention_period": 1},
                },
            }
        ]
    }
    _, _, final = _create_replica_in_terraform_mode(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        [created, created, clean, residual],
        "replica-as-built-dirty",
    )
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "record_as_built_not_clean")
    assert "Detect drift" in diagnostic.fix_suggestion
    assert _git_output(operator_settings, "log", "-1", "--format=%s") == (
        f"Create read replica: {NEW_REPLICA} (phil, approved by sam)"
    )
    assert _git_output(operator_settings, "status", "--porcelain") == ""
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    replica = next(i for i in recorded.instances if i.identifier == NEW_REPLICA)
    assert replica.backup_retention_period is None, "the first commit's record stands: a new replica has none"


# ----- fix round 1: storage of a replica, review of what the create copies --------------------------------


def test_a_replica_of_a_gp3_source_with_baseline_iops_renders_no_iops(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders", StorageType="gp3", Iops=3000, ApplyImmediately=True
    )
    assert _describe("demo-orders")["Iops"] == 3000
    refresh_runner, _ = _runner(session_factory, operator_settings, config_store)
    OperationService(session_factory).create("inventory_refresh", "sandbox", None, {}, "gp3-refresh", "phil")
    refresh_runner.run_once()
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    orders = next(i for i in recorded.instances if i.identifier == "demo-orders")
    assert (orders.storage_type, orders.iops) == ("gp3", 3000), "the source's record carries the baseline"
    created = _scripted_create(NEW_REPLICA_ADDRESS, "aws_db_instance", NEW_REPLICA)
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[created, created, {"resource_changes": []}]
    )
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    operation = service.create(
        "replica_create",
        "sandbox",
        TARGET_KEY,
        {
            "options": {"replica_identifier": NEW_REPLICA, "instance_class": "db.m6g.large"},
            "resource_arn": orders_arn,
        },
        "replica-gp3",
        "phil",
        resource_arn=orders_arn,
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    instances_tf = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    replica_block = instances_tf[instances_tf.index('"demo_orders_replica_2"') :].split("\n}\n")[0]
    assert "iops" not in replica_block and "storage_type" not in replica_block
    orders_block = instances_tf[instances_tf.index('"demo_orders"') :].split("\n}\n")[0]
    assert re.search(r"iops\s+=\s+3000", orders_block)


def test_the_replica_review_shows_the_security_groups_and_deletion_protection_it_copies(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "replica_create",
        "demo-orders",
        {"replica_identifier": NEW_REPLICA, "instance_class": "db.m6g.large"},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    source_groups = sorted(g["VpcSecurityGroupId"] for g in _describe("demo-orders")["VpcSecurityGroups"])
    review = {entry["attribute"]: entry["desired"] for entry in final.plan["change_set"]}
    assert review["vpc_security_group_ids"] == source_groups
    assert review["deletion_protection"] is False
    with session_factory() as session:
        [call] = session.query(ExecutionRecord).filter_by(
            operation_id=final.id, record_kind="aws_api", api_name="rds:CreateDBInstanceReadReplica"
        )
    assert call.request_parameters["VpcSecurityGroupIds"] == source_groups
    assert call.request_parameters["DeletionProtection"] is False
    assert call.request_parameters["DBParameterGroupName"] == "demo-postgres16-tuned"
