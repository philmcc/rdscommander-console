"""Remove an Aurora reader in both modes, and the rules that keep the writer out."""

from __future__ import annotations

import re

import boto3
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from rdscommander.catalogue.catalogue import (
    GROUP_REPLICAS_AND_SCALING,
    definition_for,
    removable_reader,
)
from rdscommander.catalogue.options import NoOptions
from rdscommander.control_db.models import ExecutionRecord
from rdscommander.lifecycle.removals import READER_REMOVAL_DOWNTIME, RemoveReaderExecutor
from rdscommander.main import create_application
from rdscommander.operations.enums import OperationState
from rdscommander.operations.service import OperationService
from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.record import read_record
from tests.drift.test_detect_executor import _adopt
from tests.lifecycle.test_settings_changes import _run
from tests.operations.approval_helpers import approve_as_reviewed
from tests.operations.test_change_strategy import _direct_store, _runner, _workspace_dir
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner, _prepare_repo

READER = "demo-aurora-reader"
READER_ADDRESS = "aws_rds_cluster_instance.demo_aurora_reader"


def _instance_exists(identifier: str) -> bool:
    rds = boto3.client("rds", region_name="eu-west-2")
    try:
        rds.describe_db_instances(DBInstanceIdentifier=identifier)
    except ClientError as error:
        assert error.response["Error"]["Code"] == "DBInstanceNotFound"
        return False
    return True


def _adopt_aurora(session_factory, operator_settings, config_store, console_config, sandbox_snapshot):  # noqa: ANN202
    """Adopt the Aurora cluster with its members, groups and subnet group; returns runner and service."""
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
            READER,
        ],
        [
            "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned",
            "aws_db_subnet_group.demo_private_subnets",
            "aws_rds_cluster.demo_aurora",
            "aws_rds_cluster_instance.demo_aurora_writer",
            READER_ADDRESS,
        ],
        "remove-reader-adopt",
    )


def _create_terraform_removal(service, session_factory, sandbox_snapshot, key: str):  # noqa: ANN202
    arn = arn_for(session_factory, sandbox_snapshot, READER)
    return service.create(
        "cluster_reader_remove",
        "sandbox",
        TARGET_KEY,
        {"options": {}, "resource_arn": arn},
        key,
        "phil",
        resource_arn=arn,
        title="Remove reader: demo-aurora-reader",
    )


def test_remove_reader_direct_deletes_the_member_and_drops_it_from_the_record(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_reader_remove",
        READER,
        {},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert final.approval_required_text == READER
    [entry] = final.plan["change_set"]
    assert (entry["address"], entry["attribute"], entry["current"], entry["desired"]) == (
        READER_ADDRESS,
        "presence",
        "member of demo-aurora",
        None,
    )
    assert entry["downtime"] == READER_REMOVAL_DOWNTIME
    assert not _instance_exists(READER)
    assert _instance_exists("demo-aurora-writer")
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert [member.identifier for member in recorded.cluster_instances] == ["demo-aurora-writer"]
    delete = next(a for a in final.step_attempts if a.step_name == "apply_direct")
    assert delete.output == {"submitted": {"deleted": READER}}


def test_remove_reader_calls_delete_without_a_final_snapshot(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_reader_remove",
        READER,
        {},
    )
    with session_factory() as session:
        [call] = session.query(ExecutionRecord).filter_by(
            operation_id=final.id, record_kind="aws_api", api_name="rds:DeleteDBInstance"
        )
    assert call.request_parameters["SkipFinalSnapshot"] is True
    assert "FinalDBSnapshotIdentifier" not in call.request_parameters


def test_remove_reader_refuses_the_writer_in_the_executor_before_approval(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_reader_remove",
        "demo-aurora-writer",
        {},
    )
    assert final.state == OperationState.FAILED
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "cluster_writer_cannot_be_removed"
    )
    assert "writer of demo-aurora" in diagnostic.detail
    assert "fail the cluster over" in diagnostic.fix_suggestion
    assert _instance_exists("demo-aurora-writer")


def test_remove_reader_refuses_a_member_that_became_the_writer_since_the_inventory(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").failover_db_cluster(
        DBClusterIdentifier="demo-aurora", TargetDBInstanceIdentifier=READER
    )
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_reader_remove",
        READER,
        {},
    )
    assert final.state == OperationState.FAILED
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "cluster_writer_cannot_be_removed"
    )
    assert "the inventory is stale" in diagnostic.detail
    assert _instance_exists(READER)


def test_remove_reader_refuses_a_reader_with_deletion_protection(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier=READER, DeletionProtection=True, ApplyImmediately=True
    )
    _prepare_repo(operator_settings)
    # Refresh so the inventory shows the protection, as it would in use.
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    service.create("inventory_refresh", "sandbox", None, {}, "protect-refresh", "phil")
    runner.run_once()
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_reader_remove",
        READER,
        {},
    )
    assert final.state == OperationState.FAILED
    assert any(d.diagnostic_kind == "deletion_protection_is_on" for d in final.diagnostic_reports)
    assert _instance_exists(READER)


def test_the_writer_is_identified_from_the_cluster_members_in_the_inventory(
    session_factory, console_config, sandbox_snapshot
) -> None:
    """Instance records carry `facts["is_cluster_writer"]`, taken from the cluster's `DBClusterMembers`."""
    with session_factory() as session:
        flags = {
            identifier: load_resource(
                session, console_config, arn_for(session_factory, sandbox_snapshot, identifier)
            ).facts["is_cluster_writer"]
            for identifier in ("demo-aurora-writer", READER, "demo-orders", "demo-orders-replica")
        }
    assert flags == {
        "demo-aurora-writer": True,
        READER: False,
        "demo-orders": False,
        "demo-orders-replica": False,
    }


def test_removable_reader_explains_every_refusal(session_factory, console_config, sandbox_snapshot) -> None:
    environment = console_config.environment("sandbox")
    with session_factory() as session:
        reader, writer, orders, cluster = (
            load_resource(session, console_config, arn_for(session_factory, sandbox_snapshot, name))
            for name in (READER, "demo-aurora-writer", "demo-orders", "demo-aurora")
        )
    adopted_reader = reader.model_copy(update={"ownership": "adopted"})
    assert removable_reader(adopted_reader, environment).ok
    assert "adopt it first" in removable_reader(reader, environment).reason
    assert removable_reader(writer, environment).reason == (
        "this instance is the writer of demo-aurora; fail over to another member first"
    )
    assert removable_reader(orders, environment).reason == (
        "only a reader of an Aurora cluster can be removed this way"
    )
    assert removable_reader(cluster, environment).reason == "applies to instances"
    assert removable_reader(None, environment).reason == "pick an Aurora reader"
    busy = adopted_reader.model_copy(update={"status": "rebooting"})
    assert (
        removable_reader(busy, environment).reason == "the instance is rebooting; wait until it is available"
    )


def test_remove_reader_through_the_api_refuses_the_writer_and_asks_for_the_identifier(
    session_factory, operator_settings, console_config, sandbox_snapshot, moto_aws
) -> None:
    direct_config = console_config.model_copy(deep=True)
    direct_config.environment("sandbox").change_management = "direct"
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=direct_config, session_factory=session_factory
        )
    )
    token = client.get("/api/v1/bootstrap").json()["csrf_token"]
    headers = {"X-CSRF-Token": token}

    def create(identifier: str, key: str):  # noqa: ANN202
        return client.post(
            "/api/v1/operations",
            headers=headers,
            json={
                "kind": "cluster_reader_remove",
                "environment_slug": "sandbox",
                "resource_id": encode_resource_id(arn_for(session_factory, sandbox_snapshot, identifier)),
                "options": {},
                "idempotency_key": key,
            },
        )

    refused = create("demo-aurora-writer", "remove-writer")
    assert refused.status_code == 422 and "writer" in refused.json()["detail"]
    accepted = create(READER, "remove-reader")
    assert accepted.status_code == 202
    detail = client.get(f"/api/v1/operations/{accepted.json()['operation_id']}").json()
    assert detail["approval_required_text"] == READER
    assert detail["title"] == "Remove reader: demo-aurora-reader"


def test_remove_reader_definition_is_destructive_and_refuses_open_drift() -> None:
    definition = definition_for("cluster_reader_remove")
    assert definition.group == GROUP_REPLICAS_AND_SCALING
    assert definition.destructive is True and definition.changes_the_resource is True
    assert definition.options_model is NoOptions
    assert definition.applicability is removable_reader
    assert RemoveReaderExecutor.allows_delete is True
    assert RemoveReaderExecutor.refuses_open_record_drift is True


def test_remove_reader_in_terraform_mode_allows_the_plain_delete(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_aurora(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    assert "demo_aurora_reader" in (_workspace_dir(operator_settings) / "clusters.tf").read_text()
    deleted = {
        "resource_changes": [
            {
                "address": READER_ADDRESS,
                "type": "aws_rds_cluster_instance",
                "change": {"actions": ["delete"], "before": {"identifier": READER}, "after": None},
            }
        ]
    }
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[deleted, deleted, {"resource_changes": []}]
    )
    runner.services.terraform_runner = fake
    arn = arn_for(session_factory, sandbox_snapshot, READER)
    operation = service.create(
        "cluster_reader_remove",
        "sandbox",
        TARGET_KEY,
        {"options": {}, "resource_arn": arn},
        "remove-reader-tf",
        "phil",
        resource_arn=arn,
        title="Remove reader: demo-aurora-reader",
    )
    runner.run_once()
    awaiting = service.get(operation.id)
    assert awaiting.state == OperationState.AWAITING_APPROVAL, [d.detail for d in awaiting.diagnostic_reports]
    assert awaiting.approval_required_text == READER
    assert awaiting.plan["downtime"] == READER_REMOVAL_DOWNTIME
    assert awaiting.plan["touched_addresses"] == [READER_ADDRESS]
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "apply" in fake.calls
    assert "demo_aurora_reader" not in (_workspace_dir(operator_settings) / "clusters.tf").read_text()
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert [member.identifier for member in recorded.cluster_instances] == ["demo-aurora-writer"]


# ----- fix round 1: history attribution and the live cluster check in both modes ---------------------------


def test_the_cluster_membership_change_of_a_reader_removal_is_explained_by_the_operation(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import ResourceChangeEvent

    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_reader_remove",
        READER,
        {},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    assert final.plan["affected_resource_arns"] == [cluster_arn]
    assert final.plan["touched_addresses"] == [READER_ADDRESS]
    with session_factory() as session:
        [event] = session.query(ResourceChangeEvent).filter_by(
            resource_arn=cluster_arn, attribute_path="member_identifiers"
        )
    assert event.explanation == "operation" and event.operation_id == final.id


def test_an_operation_explains_a_change_to_a_resource_listed_in_its_affected_resource_arns(
    session_factory, sandbox_snapshot
) -> None:
    from datetime import timedelta

    from rdscommander.control_db.models import Operation, utc_now
    from rdscommander.history.change_events import explain_event

    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    with session_factory() as session:
        operation = Operation(
            operation_kind="cluster_reader_remove",
            state="SUCCEEDED",
            environment_slug="sandbox",
            target_key=TARGET_KEY,
            plan={"affected_resource_arns": [cluster_arn]},
            idempotency_key="affected-1",
            created_by_operator_name="phil",
            resource_arn="arn:aws:rds:eu-west-2:000000000000:db:somewhere-else",
            title="x",
            started_at_utc=utc_now() - timedelta(minutes=1),
            finished_at_utc=utc_now(),
        )
        session.add(operation)
        session.commit()
        operation_id = operation.id
        explained = explain_event(session, cluster_arn, utc_now() - timedelta(minutes=5), utc_now())
        unrelated = explain_event(
            session,
            "arn:aws:rds:eu-west-2:000000000000:cluster:other",
            utc_now() - timedelta(minutes=5),
            utc_now(),
        )
    assert explained == ("operation", operation_id)
    assert unrelated == ("outside_console", None)


def test_remove_reader_in_terraform_mode_refuses_a_member_that_became_the_writer(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_aurora(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    boto3.client("rds", region_name="eu-west-2").failover_db_cluster(
        DBClusterIdentifier="demo-aurora", TargetDBInstanceIdentifier=READER
    )
    runner.services.terraform_runner = FakeTerraformRunner(runner.services.execution_recorder)
    operation = _create_terraform_removal(service, session_factory, sandbox_snapshot, "tf-stale-writer")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.FAILED
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "cluster_writer_cannot_be_removed"
    )
    assert "the inventory is stale" in diagnostic.detail
    assert "affected_resource_arns" not in final.plan
    assert _instance_exists(READER)


@pytest.mark.parametrize("mode", ["terraform", "direct"])
def test_remove_reader_refuses_while_the_live_cluster_is_not_available(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
    monkeypatch,
    mode,
) -> None:
    from rdscommander.lifecycle import direct_api as lifecycle_api

    real_cluster_status = lifecycle_api.cluster_status
    monkeypatch.setattr(
        lifecycle_api,
        "cluster_status",
        lambda context, identifier, step_name="await_settled": (
            "failing-over",
            real_cluster_status(context, identifier, step_name)[1],
        ),
    )
    if mode == "terraform":
        runner, service = _adopt_aurora(
            session_factory, operator_settings, config_store, console_config, sandbox_snapshot
        )
        runner.services.terraform_runner = FakeTerraformRunner(runner.services.execution_recorder)
        operation = _create_terraform_removal(service, session_factory, sandbox_snapshot, "tf-failing-over")
        runner.run_once()
        final = service.get(operation.id)
    else:
        final, _ = _run(
            session_factory,
            operator_settings,
            config_store,
            sandbox_snapshot,
            "cluster_reader_remove",
            READER,
            {},
        )
    assert final.state == OperationState.FAILED
    diagnostic = next(d for d in final.diagnostic_reports if d.diagnostic_kind == "cluster_not_available")
    assert "demo-aurora is failing-over" in diagnostic.detail
    assert _instance_exists(READER)


def test_remove_reader_refuses_a_cluster_that_lists_no_writer(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.lifecycle import direct_api as lifecycle_api

    monkeypatch.setattr(
        lifecycle_api, "cluster_status", lambda context, identifier, step_name="x": ("available", None)
    )
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_reader_remove",
        READER,
        {},
    )
    assert final.state == OperationState.FAILED
    assert any(d.diagnostic_kind == "cluster_not_available" for d in final.diagnostic_reports)
    assert _instance_exists(READER)


# ----- decommission: deletion protection off and the final snapshot named, then the delete -----------------

TEMP = "demo-temp"
TEMP_ADDRESS = "aws_db_instance.demo_temp"
TEMP_FINAL = "demo-temp-final-2026"
AURORA_FINAL = "demo-aurora-final-2026"
DECOMMISSION_STEP_TAIL = [
    "then_compute_change_set",
    "then_policy_check",
    "then_apply_direct",
    "then_verify_direct",
    "then_refresh_resource",
    "then_record_to_checkout",
]


def _create_temp_instance(deletion_protection: bool = True) -> str:
    """A standalone instance with no replicas, its password kept by Secrets Manager; returns its ARN."""
    response = boto3.client("rds", region_name="eu-west-2").create_db_instance(
        DBInstanceIdentifier=TEMP,
        Engine="postgres",
        EngineVersion="16.4",
        DBInstanceClass="db.m6g.large",
        AllocatedStorage=20,
        MasterUsername="app_admin",
        ManageMasterUserPassword=True,
        BackupRetentionPeriod=1,
        DeletionProtection=deletion_protection,
    )
    return response["DBInstance"]["DBInstanceArn"]


def _refresh(session_factory, operator_settings, config_store, key: str, direct: bool = True) -> None:  # noqa: ANN001
    if not (operator_settings.repo_root / ".git").exists():
        _prepare_repo(operator_settings)
    store = _direct_store(config_store) if direct else config_store
    runner, _ = _runner(session_factory, operator_settings, store)
    service = OperationService(session_factory)
    service.create("inventory_refresh", "sandbox", None, {}, key, "phil")
    runner.run_once()


def _decommission_direct(  # noqa: ANN202
    session_factory, operator_settings, config_store, arn: str, identifier: str, final_name: str | None
):
    """Create and run a direct-mode decommission; returns (paused or failed, final) operations.

    A final snapshot name of None leaves the option out, as the form does for a read replica.
    """
    if not (operator_settings.repo_root / ".git").exists():
        _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    options = {} if final_name is None else {"final_snapshot_identifier": final_name}
    operation = service.create(
        "decommission",
        "sandbox",
        TARGET_KEY,
        {"options": options, "resource_arn": arn},
        f"decommission-{identifier}",
        "phil",
        resource_arn=arn,
        title=f"Decommission: {identifier}",
    )
    runner.run_once()
    paused = service.get(operation.id)
    if paused.state != OperationState.AWAITING_APPROVAL:
        return paused, paused
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    return paused, service.get(operation.id)


def _db_snapshot_exists(name: str, cluster: bool = False) -> bool:
    rds = boto3.client("rds", region_name="eu-west-2")
    try:
        if cluster:
            rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=name)
        else:
            rds.describe_db_snapshots(DBSnapshotIdentifier=name)
    except ClientError:
        return False
    return True


def _cluster_exists(identifier: str) -> bool:
    try:
        boto3.client("rds", region_name="eu-west-2").describe_db_clusters(DBClusterIdentifier=identifier)
    except ClientError:
        return False
    return True


def _commit_subjects(operator_settings, count: int) -> list[str]:  # noqa: ANN001
    from tests.terraform.test_adoption_executor import _git

    return _git("log", f"-{count}", "--format=%s", cwd=operator_settings.repo_root).splitlines()


def test_decommission_direct_turns_protection_off_takes_the_final_snapshot_and_deletes(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import ResourceChangeEvent

    arn = _create_temp_instance()
    _refresh(session_factory, operator_settings, config_store, "decommission-seen")
    paused, final = _decommission_direct(
        session_factory, operator_settings, config_store, arn, TEMP, TEMP_FINAL
    )
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.approval_required_text == TEMP
    assert [(e["attribute"], e["current"], e["desired"]) for e in paused.plan["change_set"]] == [
        ("deletion_protection", True, False)
    ]
    second_phase = paused.plan["second_phase"]
    assert (second_phase["summary"], second_phase["what"]) == (
        f"delete {TEMP} after the final snapshot {TEMP_FINAL}",
        [TEMP_ADDRESS],
    )
    assert [(e["address"], e["attribute"], e["desired"]) for e in second_phase["change_set"]] == [
        (TEMP_ADDRESS, "presence", None)
    ]
    assert (
        paused.plan["downtime"] == f"the database is deleted after the final snapshot {TEMP_FINAL} completes"
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert [a.step_name for a in final.step_attempts][-6:] == DECOMMISSION_STEP_TAIL
    assert not _instance_exists(TEMP)
    assert _db_snapshot_exists(TEMP_FINAL)
    [presence] = final.plan["change_set_phase_2"]
    assert (presence["address"], presence["attribute"], presence["desired"]) == (
        TEMP_ADDRESS,
        "presence",
        None,
    )
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert TEMP not in {instance.identifier for instance in recorded.instances}
    assert "demo-orders" in {instance.identifier for instance in recorded.instances}
    assert _commit_subjects(operator_settings, 2) == [
        f"Decommission: {TEMP} (phil, approved by sam) (phase 2)",
        f"Decommission: {TEMP} (phil, approved by sam)",
    ]
    with session_factory() as session:
        [delete] = session.query(ExecutionRecord).filter_by(
            operation_id=final.id, record_kind="aws_api", api_name="rds:DeleteDBInstance"
        )
        [removed] = session.query(ResourceChangeEvent).filter_by(resource_arn=arn, attribute_path="removed")
        [snapshot_created] = session.query(ResourceChangeEvent).filter_by(
            resource_arn=f"{arn.rsplit(':', 2)[0]}:snapshot:{TEMP_FINAL}", attribute_path="created"
        )
    assert delete.step_name == "then_apply_direct"
    assert delete.request_parameters["FinalDBSnapshotIdentifier"] == TEMP_FINAL
    assert delete.request_parameters["SkipFinalSnapshot"] is False
    assert (removed.explanation, removed.operation_id) == ("operation", final.id)
    assert (snapshot_created.explanation, snapshot_created.operation_id) == ("operation", final.id)


def test_decommission_refuses_an_instance_with_replicas_naming_them(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    failed, _ = _decommission_direct(
        session_factory, operator_settings, config_store, orders_arn, "demo-orders", "demo-orders-final"
    )
    assert failed.state == OperationState.FAILED
    diagnostic = next(
        d for d in failed.diagnostic_reports if d.diagnostic_kind == "dependants_block_decommission"
    )
    assert "demo-orders-replica" in diagnostic.detail
    assert diagnostic.step_name == "compute_change_set"
    assert "await_approval" not in {a.step_name for a in failed.step_attempts}
    assert _instance_exists("demo-orders") and _instance_exists("demo-orders-replica")
    assert not _db_snapshot_exists("demo-orders-final")


def test_decommission_of_a_cluster_removes_the_members_first(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import ResourceChangeEvent

    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    member_arns = sorted(
        arn_for(session_factory, sandbox_snapshot, member) for member in ("demo-aurora-writer", READER)
    )
    paused, final = _decommission_direct(
        session_factory, operator_settings, config_store, cluster_arn, "demo-aurora", AURORA_FINAL
    )
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["change_set"] == [], "deletion protection is already off: the first phase has no entry"
    assert paused.plan["second_phase"]["what"] == [
        "aws_rds_cluster.demo_aurora",
        "aws_rds_cluster_instance.demo_aurora_reader",
        "aws_rds_cluster_instance.demo_aurora_writer",
    ]
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert not _cluster_exists("demo-aurora")
    assert not _instance_exists("demo-aurora-writer") and not _instance_exists(READER)
    assert _db_snapshot_exists(AURORA_FINAL, cluster=True)
    assert sorted(final.plan["affected_resource_arns"]) == member_arns
    assert final.resource_arn == cluster_arn
    with session_factory() as session:
        deletes = [
            (record.api_name, record.request_parameters)
            for record in session.query(ExecutionRecord)
            .filter(
                ExecutionRecord.operation_id == final.id,
                ExecutionRecord.api_name.in_(["rds:DeleteDBInstance", "rds:DeleteDBCluster"]),
            )
            .order_by(ExecutionRecord.id)
        ]
        removed = {
            event.resource_arn: (event.explanation, event.operation_id)
            for event in session.query(ResourceChangeEvent).filter_by(attribute_path="removed")
        }
    assert [api_name for api_name, _ in deletes] == [
        "rds:DeleteDBInstance",
        "rds:DeleteDBInstance",
        "rds:DeleteDBCluster",
    ]
    assert all(parameters["SkipFinalSnapshot"] is True for _, parameters in deletes[:2])
    assert deletes[2][1]["FinalDBSnapshotIdentifier"] == AURORA_FINAL
    assert {arn: removed[arn] for arn in [cluster_arn, *member_arns]} == {
        arn: ("operation", final.id) for arn in [cluster_arn, *member_arns]
    }
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert recorded.clusters == [] and recorded.cluster_instances == []


def test_decommission_refuses_a_final_snapshot_name_that_already_exists(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    arn = _create_temp_instance()
    boto3.client("rds", region_name="eu-west-2").create_db_snapshot(
        DBSnapshotIdentifier=TEMP_FINAL, DBInstanceIdentifier=TEMP
    )
    _refresh(session_factory, operator_settings, config_store, "decommission-taken")
    failed, _ = _decommission_direct(session_factory, operator_settings, config_store, arn, TEMP, TEMP_FINAL)
    assert failed.state == OperationState.FAILED
    diagnostic = next(
        d for d in failed.diagnostic_reports if d.diagnostic_kind == "final_snapshot_name_taken"
    )
    assert TEMP_FINAL in diagnostic.detail
    assert _instance_exists(TEMP)


def test_decommission_refuses_a_cluster_with_a_member_owned_by_other_tooling(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import InventoryDbInstance

    with session_factory() as session:
        row = (
            session.query(InventoryDbInstance)
            .filter_by(snapshot_id=sandbox_snapshot.id, db_instance_identifier=READER)
            .one()
        )
        row.ownership = "foreign_cloudformation"
        session.commit()
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    failed, _ = _decommission_direct(
        session_factory, operator_settings, config_store, cluster_arn, "demo-aurora", AURORA_FINAL
    )
    assert failed.state == OperationState.FAILED
    diagnostic = next(
        d for d in failed.diagnostic_reports if d.diagnostic_kind == "dependants_block_decommission"
    )
    assert READER in diagnostic.detail and "cloudformation" in diagnostic.detail
    assert _cluster_exists("demo-aurora") and _instance_exists(READER)


def test_decommission_refuses_a_resource_aws_does_not_report_as_available(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.lifecycle import direct_api as lifecycle_api

    arn = _create_temp_instance()
    _refresh(session_factory, operator_settings, config_store, "decommission-busy")
    real_describe = lifecycle_api.instance_describe

    def backing_up(context, identifier, step_name="await_settled"):  # noqa: ANN001, ANN202
        description = real_describe(context, identifier, step_name)
        return {**description, "DBInstanceStatus": "backing-up"} if description else description

    monkeypatch.setattr(lifecycle_api, "instance_describe", backing_up)
    failed, _ = _decommission_direct(session_factory, operator_settings, config_store, arn, TEMP, TEMP_FINAL)
    assert failed.state == OperationState.FAILED
    diagnostic = next(d for d in failed.diagnostic_reports if d.diagnostic_kind == "resource_not_available")
    assert f"{TEMP} is backing-up" in diagnostic.detail
    assert _instance_exists(TEMP)


def test_decommission_in_terraform_mode_applies_the_protection_change_then_the_delete(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.inventory.queries import latest_snapshot_for_target
    from tests.drift.test_detect_executor import _adopt
    from tests.terraform.test_adoption_executor import _git

    _create_temp_instance()
    _refresh(session_factory, operator_settings, config_store, "decommission-tf-seen", direct=False)
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        session.expunge_all()
    runner, service = _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        snapshot,
        [TEMP],
        [TEMP_ADDRESS],
        "decommission-adopt",
        prepare_repo=False,
    )
    update = {
        "resource_changes": [
            {
                "address": TEMP_ADDRESS,
                "type": "aws_db_instance",
                "change": {
                    "actions": ["update"],
                    "before": {"deletion_protection": True, "final_snapshot_identifier": f"{TEMP}-final"},
                    "after": {"deletion_protection": False, "final_snapshot_identifier": TEMP_FINAL},
                },
            }
        ]
    }
    delete = {
        "resource_changes": [
            {
                "address": TEMP_ADDRESS,
                "type": "aws_db_instance",
                "change": {"actions": ["delete"], "before": {"identifier": TEMP}, "after": None},
            }
        ]
    }
    clean = {"resource_changes": []}
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, clean, delete, delete, clean]
    )
    runner.services.terraform_runner = fake
    arn = arn_for(session_factory, snapshot, TEMP)
    operation = service.create(
        "decommission",
        "sandbox",
        TARGET_KEY,
        {"options": {"final_snapshot_identifier": TEMP_FINAL}, "resource_arn": arn},
        "decommission-tf",
        "phil",
        resource_arn=arn,
        title=f"Decommission: {TEMP}",
    )
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.approval_required_text == TEMP
    assert paused.plan["second_phase"]["what"] == [TEMP_ADDRESS]
    assert "deleted after the final snapshot" in paused.plan["downtime"]
    instances_file = _workspace_dir(operator_settings) / "instances.tf"
    assert re.search(rf'final_snapshot_identifier\s+= "{TEMP_FINAL}"', instances_file.read_text())
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert fake.calls.count("apply") == 2 and "plan:then_plan" in fake.calls
    assert not instances_file.exists() or "demo_temp" not in instances_file.read_text()
    assert TEMP not in {
        i.identifier for i in read_record(_workspace_dir(operator_settings)).render_input.instances
    }
    phase_one_file = _git(
        "show",
        "HEAD~1:terraform/sandbox/000000000000/eu-west-2/instances.tf",
        cwd=operator_settings.repo_root,
    )
    assert re.search(rf'final_snapshot_identifier\s+= "{TEMP_FINAL}"', phase_one_file)
    assert re.search(r"deletion_protection\s+= false", phase_one_file)
    assert _commit_subjects(operator_settings, 2) == [
        f"Decommission: {TEMP} (phil, approved by sam) (phase 2)",
        f"Decommission: {TEMP} (phil, approved by sam)",
    ]


def test_decommission_of_a_cluster_in_terraform_mode_deletes_the_cluster_and_its_members(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_aurora(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    removed_addresses = [
        "aws_rds_cluster.demo_aurora",
        "aws_rds_cluster_instance.demo_aurora_reader",
        "aws_rds_cluster_instance.demo_aurora_writer",
    ]
    update = {
        "resource_changes": [
            {
                "address": "aws_rds_cluster.demo_aurora",
                "type": "aws_rds_cluster",
                "change": {
                    "actions": ["update"],
                    "before": {"final_snapshot_identifier": "demo-aurora-final"},
                    "after": {"final_snapshot_identifier": AURORA_FINAL},
                },
            }
        ]
    }
    delete = {
        "resource_changes": [
            {
                "address": address,
                "type": address.split(".")[0],
                "change": {"actions": ["delete"], "before": {}, "after": None},
            }
            for address in removed_addresses
        ]
    }
    clean = {"resource_changes": []}
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[update, update, clean, delete, delete, clean]
    )
    runner.services.terraform_runner = fake
    arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = service.create(
        "decommission",
        "sandbox",
        TARGET_KEY,
        {"options": {"final_snapshot_identifier": AURORA_FINAL}, "resource_arn": arn},
        "decommission-cluster-tf",
        "phil",
        resource_arn=arn,
        title="Decommission: demo-aurora",
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert final.plan["touched_addresses_phase_2"] == removed_addresses
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert recorded.clusters == [] and recorded.cluster_instances == []
    clusters_file = _workspace_dir(operator_settings) / "clusters.tf"
    assert not clusters_file.exists() or "demo_aurora" not in clusters_file.read_text()


def test_decommission_in_terraform_mode_refuses_a_cluster_whose_member_is_not_adopted(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """Terraform deletes only what it manages; a member left out would keep the cluster from being deleted."""
    from tests.drift.test_detect_executor import _adopt

    runner, service = _adopt(
        session_factory,
        operator_settings,
        config_store,
        console_config,
        sandbox_snapshot,
        ["demo-aurora-pg16-tuned", "demo-private-subnets", "demo-aurora", "demo-aurora-writer"],
        [
            "aws_rds_cluster_parameter_group.demo_aurora_pg16_tuned",
            "aws_db_subnet_group.demo_private_subnets",
            "aws_rds_cluster.demo_aurora",
            "aws_rds_cluster_instance.demo_aurora_writer",
        ],
        "decommission-partial-adopt",
    )
    runner.services.terraform_runner = FakeTerraformRunner(runner.services.execution_recorder)
    arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = service.create(
        "decommission",
        "sandbox",
        TARGET_KEY,
        {"options": {"final_snapshot_identifier": AURORA_FINAL}, "resource_arn": arn},
        "decommission-partial",
        "phil",
        resource_arn=arn,
        title="Decommission: demo-aurora",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    diagnostic = next(
        d for d in failed.diagnostic_reports if d.diagnostic_kind == "dependants_block_decommission"
    )
    assert READER in diagnostic.detail and "adopt" in diagnostic.detail


def test_decommissionable_explains_every_refusal(session_factory, console_config, sandbox_snapshot) -> None:
    from rdscommander.catalogue.catalogue import decommissionable

    environment = console_config.environment("sandbox")
    direct_environment = environment.model_copy(update={"change_management": "direct"})
    with session_factory() as session:
        orders, replica, reader, cluster, group = (
            load_resource(session, console_config, arn_for(session_factory, sandbox_snapshot, name))
            for name in ("demo-orders", "demo-orders-replica", READER, "demo-aurora", "demo-postgres16-tuned")
        )
    assert decommissionable(cluster, direct_environment).ok
    assert "adopt it first" in decommissionable(cluster, environment).reason
    assert decommissionable(None, environment).reason == "pick an instance or cluster"
    assert decommissionable(group, environment).reason == "applies to instances and clusters"
    assert decommissionable(reader, direct_environment).reason == (
        "this instance is a member of demo-aurora; decommission the cluster, or remove this reader"
    )
    assert decommissionable(replica, direct_environment).ok, "a replica is deleted without a snapshot"
    assert decommissionable(orders, direct_environment).reason == (
        "its read replica demo-orders-replica depends on it; promote or decommission it first"
    )
    busy = cluster.model_copy(update={"status": "backing-up"})
    assert decommissionable(busy, direct_environment).reason == "it is backing-up; wait until it is available"


def test_decommission_definition_is_destructive_and_names_its_final_snapshot() -> None:
    from pydantic import ValidationError

    from rdscommander.catalogue.catalogue import GROUP_LIFECYCLE, decommissionable
    from rdscommander.lifecycle.options import DecommissionOptions
    from rdscommander.lifecycle.removals import DecommissionExecutor
    from rdscommander.operations.attribute_change import RENDER_ONLY_ATTRIBUTES

    definition = definition_for("decommission")
    assert definition.group == GROUP_LIFECYCLE
    assert definition.destructive is True and definition.changes_the_resource is True
    assert definition.applicability is decommissionable
    assert definition.options_model is DecommissionOptions
    assert DecommissionExecutor.allows_delete is True
    assert "final_snapshot_identifier" in RENDER_ONLY_ATTRIBUTES
    assert DecommissionOptions(final_snapshot_identifier=TEMP_FINAL).confirm_dependants_removed is False
    for bad_name in ("Demo-Final", "final--x", "final-"):
        with pytest.raises(ValidationError):
            DecommissionOptions(final_snapshot_identifier=bad_name)


def test_decommission_in_terraform_mode_refuses_a_first_phase_plan_that_deletes(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """The delete is allowed for the second change only; the protection change must not delete anything."""
    runner, service = _adopt_aurora(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    deleting = {
        "resource_changes": [
            {
                "address": "aws_rds_cluster.demo_aurora",
                "type": "aws_rds_cluster",
                "change": {"actions": ["delete"], "before": {}, "after": None},
            }
        ]
    }
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[deleting]
    )
    arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = service.create(
        "decommission",
        "sandbox",
        TARGET_KEY,
        {"options": {"final_snapshot_identifier": AURORA_FINAL}, "resource_arn": arn},
        "decommission-early-delete",
        "phil",
        resource_arn=arn,
        title="Decommission: demo-aurora",
    )
    runner.run_once()
    failed = service.get(operation.id)
    assert failed.state == OperationState.FAILED
    assert any(
        d.diagnostic_kind == "policy_refused" and d.step_name == "policy_check"
        for d in failed.diagnostic_reports
    )


# ----- fix round 1: the second phase in the review, drift refused before approval, a resumed delete ---------

AURORA_REMOVED_ADDRESSES = [
    "aws_rds_cluster_instance.demo_aurora_reader",
    "aws_rds_cluster_instance.demo_aurora_writer",
    "aws_rds_cluster.demo_aurora",
]


def test_the_review_of_a_cluster_decommission_shows_every_deleted_resource_before_approval(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """Deletion protection is already off, so the first change set is empty; the review shows the delete."""
    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = service.create(
        "decommission",
        "sandbox",
        TARGET_KEY,
        {"options": {"final_snapshot_identifier": AURORA_FINAL}, "resource_arn": cluster_arn},
        "decommission-review",
        "phil",
        resource_arn=cluster_arn,
        title="Decommission: demo-aurora",
    )
    runner.run_once()
    client = TestClient(
        create_application(
            settings=operator_settings, console_config=console_config, session_factory=session_factory
        )
    )

    def detail_rows() -> list[tuple[str, str, object]]:
        detail = client.get(f"/api/v1/operations/{operation.id}").json()
        return [(e["address"], e["attribute"], e["desired"]) for e in detail["change_set"]]

    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert [e["address"] for e in paused.plan["second_phase"]["change_set"]] == AURORA_REMOVED_ADDRESSES
    assert detail_rows() == [(address, "presence", None) for address in AURORA_REMOVED_ADDRESSES]
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    assert service.get(operation.id).state == OperationState.SUCCEEDED
    assert detail_rows() == [(address, "presence", None) for address in AURORA_REMOVED_ADDRESSES]


def test_the_terraform_review_of_a_decommission_keeps_the_second_change_set_too(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    runner, service = _adopt_aurora(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    runner.services.terraform_runner = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[{"resource_changes": []}]
    )
    arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = service.create(
        "decommission",
        "sandbox",
        TARGET_KEY,
        {"options": {"final_snapshot_identifier": AURORA_FINAL}, "resource_arn": arn},
        "decommission-tf-review",
        "phil",
        resource_arn=arn,
        title="Decommission: demo-aurora",
    )
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert [e["address"] for e in paused.plan["second_phase"]["change_set"]] == AURORA_REMOVED_ADDRESSES


def test_open_record_drift_on_a_member_refuses_the_decommission_before_approval(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from rdscommander.control_db.models import DriftFinding
    from tests.terraform.test_adoption_executor import _git

    _prepare_repo(operator_settings)
    commits_before = _git("rev-list", "--count", "HEAD", cwd=operator_settings.repo_root)
    with session_factory() as session:
        session.add(
            DriftFinding(
                resource_arn=READER_ADDRESS,
                environment_slug="sandbox",
                target_key=TARGET_KEY,
                finding_kind="record",
                attributes=[],
            )
        )
        session.commit()
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    failed, _ = _decommission_direct(
        session_factory, operator_settings, config_store, cluster_arn, "demo-aurora", AURORA_FINAL
    )
    assert failed.state == OperationState.FAILED
    [diagnostic] = [d for d in failed.diagnostic_reports if d.diagnostic_kind == "open_drift_on_resource"]
    assert diagnostic.step_name == "compute_change_set"
    assert "Resolve drift" in diagnostic.fix_suggestion
    assert "await_approval" not in {a.step_name for a in failed.step_attempts}
    assert _git("rev-list", "--count", "HEAD", cwd=operator_settings.repo_root) == commits_before
    assert _cluster_exists("demo-aurora") and _instance_exists(READER)


def test_a_resumed_cluster_delete_does_not_delete_members_aws_still_lists_as_deleting(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    """A restart during the member wait runs the delete step again while AWS still lists the members."""
    from rdscommander.lifecycle import direct_api as lifecycle_api

    _prepare_repo(operator_settings)
    runner, _ = _runner(session_factory, operator_settings, _direct_store(config_store))
    service = OperationService(session_factory)
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operation = service.create(
        "decommission",
        "sandbox",
        TARGET_KEY,
        {"options": {"final_snapshot_identifier": AURORA_FINAL}, "resource_arn": cluster_arn},
        "decommission-resume",
        "phil",
        resource_arn=cluster_arn,
        title="Decommission: demo-aurora",
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    # The interrupted first attempt already submitted the member deletes; AWS lists them as deleting a while.
    rds = boto3.client("rds", region_name="eu-west-2")
    for member in ("demo-aurora-writer", READER):
        rds.delete_db_instance(DBInstanceIdentifier=member, SkipFinalSnapshot=True)
    deleting_reads_left = {"demo-aurora-writer": 3, READER: 3}
    real_describe = lifecycle_api.instance_describe

    def still_deleting(context, identifier, step_name="await_settled"):  # noqa: ANN001, ANN202
        if deleting_reads_left.get(identifier, 0) > 0:
            deleting_reads_left[identifier] -= 1
            return {"DBInstanceIdentifier": identifier, "DBInstanceStatus": "deleting"}
        return real_describe(context, identifier, step_name)

    monkeypatch.setattr(lifecycle_api, "instance_describe", still_deleting)
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    with session_factory() as session:
        api_names = [
            record.api_name
            for record in session.query(ExecutionRecord).filter(
                ExecutionRecord.operation_id == final.id,
                ExecutionRecord.api_name.in_(["rds:DeleteDBInstance", "rds:DeleteDBCluster"]),
            )
        ]
    assert api_names == ["rds:DeleteDBCluster"]
    assert deleting_reads_left == {"demo-aurora-writer": 0, READER: 0}
    assert not _cluster_exists("demo-aurora") and _db_snapshot_exists(AURORA_FINAL, cluster=True)


# ----- final fix wave: a failed delete says deletion protection is already off ----------------------------


def _first_phase_commit_sha(operation) -> str:  # noqa: ANN001
    attempt = next(a for a in operation.step_attempts if a.step_name == "record_to_checkout" and a.output)
    return attempt.output["commit_sha"]


def test_a_decommission_whose_delete_aws_refuses_says_deletion_protection_is_already_off(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.lifecycle import direct_api as lifecycle_api

    arn = _create_temp_instance()
    _refresh(session_factory, operator_settings, config_store, "decommission-refused-seen")

    def refused(context, identifier, final_snapshot_identifier, step_name="perform"):  # noqa: ANN001, ANN202
        raise ClientError(
            {"Error": {"Code": "InvalidDBInstanceState", "Message": "the instance is being modified"}},
            "DeleteDBInstance",
        )

    monkeypatch.setattr(lifecycle_api, "delete_instance", refused)
    _, final = _decommission_direct(session_factory, operator_settings, config_store, arn, TEMP, TEMP_FINAL)
    assert final.state == OperationState.FAILED
    described = boto3.client("rds", region_name="eu-west-2").describe_db_instances(DBInstanceIdentifier=TEMP)
    assert described["DBInstances"][0]["DeletionProtection"] is False, "phase 1 is applied"
    sha = _first_phase_commit_sha(final)
    [diagnostic] = [d for d in final.diagnostic_reports if d.severity == "error"]
    assert diagnostic.step_name == "then_apply_direct"
    assert "InvalidDBInstanceState" in diagnostic.detail
    assert f"Deletion protection on {TEMP} is now off (committed as {sha})" in diagnostic.detail
    assert diagnostic.fix_suggestion.endswith(
        f"deletion protection on {TEMP} is now off (committed as {sha}); turn it back on with Deletion "
        "protection unless you retry the decommission"
    )


def test_a_cluster_decommission_whose_members_never_go_says_deletion_protection_is_off(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.lifecycle import direct_api as lifecycle_api

    real_describe = lifecycle_api.instance_describe
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    operator_settings.direct_verify_timeout_seconds = 0

    def members_stay(context, identifier, step_name="await_settled"):  # noqa: ANN001, ANN202
        description = real_describe(context, identifier, step_name)
        if description is None and identifier in ("demo-aurora-writer", READER):
            return {"DBInstanceIdentifier": identifier, "DBInstanceStatus": "deleting"}
        return description

    monkeypatch.setattr(lifecycle_api, "instance_describe", members_stay)
    _, final = _decommission_direct(
        session_factory, operator_settings, config_store, cluster_arn, "demo-aurora", AURORA_FINAL
    )
    assert final.state == OperationState.FAILED
    sha = _first_phase_commit_sha(final)
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "cluster_members_still_deleting"
    )
    assert diagnostic.step_name == "then_apply_direct"
    assert "Deletion protection on demo-aurora is now off" in diagnostic.detail
    assert f"(committed as {sha}); turn it back on with Deletion protection" in diagnostic.fix_suggestion
    assert _cluster_exists("demo-aurora")


# ----- final fix wave: global databases and replica clusters block a decommission before approval ---------


def _cluster_described_with(monkeypatch, **extra) -> None:  # noqa: ANN001, ANN003
    """The live describe of demo-aurora carries `extra`: moto models no global database or replica cluster."""
    from rdscommander.lifecycle import direct_api as lifecycle_api

    real_describe = lifecycle_api.cluster_describe

    def described(context, identifier, step_name="await_settled"):  # noqa: ANN001, ANN202
        description = real_describe(context, identifier, step_name)
        if description is not None and identifier == "demo-aurora":
            return {**description, **extra}
        return description

    monkeypatch.setattr(lifecycle_api, "cluster_describe", described)


def _assert_refused_before_approval_naming(final, *names: str) -> None:  # noqa: ANN001
    assert final.state == OperationState.FAILED
    diagnostic = next(
        d for d in final.diagnostic_reports if d.diagnostic_kind == "dependants_block_decommission"
    )
    for name in names:
        assert name in diagnostic.detail, (name, diagnostic.detail)
    assert diagnostic.step_name == "compute_change_set"
    assert "await_approval" not in {a.step_name for a in final.step_attempts}


def test_decommission_refuses_a_cluster_that_is_a_global_database_member(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    _cluster_described_with(monkeypatch, GlobalClusterIdentifier="demo-global")
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    failed, _ = _decommission_direct(
        session_factory, operator_settings, config_store, cluster_arn, "demo-aurora", AURORA_FINAL
    )
    _assert_refused_before_approval_naming(failed, "global database demo-global")
    assert _cluster_exists("demo-aurora") and _instance_exists("demo-aurora-writer")


def test_decommission_refuses_a_cluster_with_replica_clusters_naming_them(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    replica_arn = "arn:aws:rds:eu-west-1:000000000000:cluster:demo-aurora-dr"
    _cluster_described_with(monkeypatch, ReadReplicaIdentifiers=[replica_arn])
    cluster_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    failed, _ = _decommission_direct(
        session_factory, operator_settings, config_store, cluster_arn, "demo-aurora", AURORA_FINAL
    )
    _assert_refused_before_approval_naming(failed, replica_arn)
    assert _cluster_exists("demo-aurora")


def test_decommission_refuses_an_instance_that_aurora_clusters_replicate_from(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    from rdscommander.lifecycle import direct_api as lifecycle_api

    arn = _create_temp_instance()
    _refresh(session_factory, operator_settings, config_store, "decommission-cluster-replica-seen")
    real_describe = lifecycle_api.instance_describe

    def with_a_replica_cluster(context, identifier, step_name="await_settled"):  # noqa: ANN001, ANN202
        description = real_describe(context, identifier, step_name)
        if description is not None and identifier == TEMP:
            return {**description, "ReadReplicaDBClusterIdentifiers": ["demo-temp-aurora-replica"]}
        return description

    monkeypatch.setattr(lifecycle_api, "instance_describe", with_a_replica_cluster)
    failed, _ = _decommission_direct(session_factory, operator_settings, config_store, arn, TEMP, TEMP_FINAL)
    _assert_refused_before_approval_naming(failed, "demo-temp-aurora-replica")
    assert _instance_exists(TEMP) and not _db_snapshot_exists(TEMP_FINAL)


# ----- final fix wave: a read replica is decommissioned without a snapshot -------------------------------

ORDERS_REPLICA = "demo-orders-replica"
ORDERS_REPLICA_ADDRESS = "aws_db_instance.demo_orders_replica"
FIRST_PHASE_STEPS_AFTER_APPROVAL_DIRECT = [
    "apply_direct",
    "verify_direct",
    "refresh_resource",
    "record_to_checkout",
]


def _api_calls(session_factory, operation_id, api_name: str) -> list[dict]:  # noqa: ANN001
    with session_factory() as session:
        return [
            record.request_parameters
            for record in session.query(ExecutionRecord).filter_by(
                operation_id=operation_id, record_kind="aws_api", api_name=api_name
            )
        ]


def _assert_deleted_without_a_snapshot(session_factory, final) -> None:  # noqa: ANN001
    [delete] = _api_calls(session_factory, final.id, "rds:DeleteDBInstance")
    assert delete["DBInstanceIdentifier"] == ORDERS_REPLICA
    assert delete["SkipFinalSnapshot"] is True and "FinalDBSnapshotIdentifier" not in delete
    assert not [arn for arn in final.plan.get("created_resource_arns") or [] if ":snapshot:" in arn]
    assert not _instance_exists(ORDERS_REPLICA) and _instance_exists("demo-orders")
    assert not _db_snapshot_exists("demo-orders-replica-final-2026")


def test_decommission_of_a_read_replica_deletes_it_without_a_snapshot_and_skips_the_empty_first_phase(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from tests.terraform.test_adoption_executor import _git

    replica_arn = arn_for(session_factory, sandbox_snapshot, ORDERS_REPLICA)
    _prepare_repo(operator_settings)
    commits_before = int(_git("rev-list", "--count", "HEAD", cwd=operator_settings.repo_root))
    paused, final = _decommission_direct(
        session_factory,
        operator_settings,
        config_store,
        replica_arn,
        ORDERS_REPLICA,
        "demo-orders-replica-final-2026",
    )
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.approval_required_text == ORDERS_REPLICA
    assert paused.plan["change_set"] == [], "deletion protection is already off"
    second_phase = paused.plan["second_phase"]
    assert second_phase["summary"] == f"delete the read replica {ORDERS_REPLICA} without a final snapshot"
    assert "data lives on its source demo-orders" in paused.plan["downtime"]
    assert "no snapshot is taken" in paused.plan["downtime"]
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    _assert_deleted_without_a_snapshot(session_factory, final)
    skipped = {
        a.step_name: a.output
        for a in final.step_attempts
        if a.step_name in FIRST_PHASE_STEPS_AFTER_APPROVAL_DIRECT
    }
    assert set(skipped) == set(FIRST_PHASE_STEPS_AFTER_APPROVAL_DIRECT)
    assert all("skipped" in (output or {}) for output in skipped.values()), skipped
    commits_after = int(_git("rev-list", "--count", "HEAD", cwd=operator_settings.repo_root))
    assert commits_after - commits_before == 1, "only the delete is committed"
    assert _commit_subjects(operator_settings, 1) == [
        f"Decommission: {ORDERS_REPLICA} (phil, approved by sam) (phase 2)"
    ]
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert ORDERS_REPLICA not in {instance.identifier for instance in recorded.instances}


def test_decommission_of_a_protected_read_replica_turns_protection_off_first_then_deletes_without_a_snapshot(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier=ORDERS_REPLICA, DeletionProtection=True, ApplyImmediately=True
    )
    _refresh(session_factory, operator_settings, config_store, "decommission-protected-replica-seen")
    replica_arn = arn_for(session_factory, sandbox_snapshot, ORDERS_REPLICA)
    paused, final = _decommission_direct(
        session_factory,
        operator_settings,
        config_store,
        replica_arn,
        ORDERS_REPLICA,
        "demo-orders-replica-final-2026",
    )
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert [(e["attribute"], e["current"], e["desired"]) for e in paused.plan["change_set"]] == [
        ("deletion_protection", True, False)
    ]
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    _assert_deleted_without_a_snapshot(session_factory, final)
    assert _commit_subjects(operator_settings, 2) == [
        f"Decommission: {ORDERS_REPLICA} (phil, approved by sam) (phase 2)",
        f"Decommission: {ORDERS_REPLICA} (phil, approved by sam)",
    ]


def test_decommissioning_the_replica_clears_the_way_for_its_source(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    replica_arn = arn_for(session_factory, sandbox_snapshot, ORDERS_REPLICA)
    _, replica_final = _decommission_direct(
        session_factory, operator_settings, config_store, replica_arn, ORDERS_REPLICA, "unused-final"
    )
    assert replica_final.state == OperationState.SUCCEEDED, [
        d.detail for d in replica_final.diagnostic_reports
    ]
    from rdscommander.inventory.queries import latest_snapshot_for_target

    with session_factory() as session:
        latest = latest_snapshot_for_target(session, TARGET_KEY)
        session.expunge_all()
    orders_arn = arn_for(session_factory, latest, "demo-orders")
    paused, _ = _decommission_direct(
        session_factory, operator_settings, config_store, orders_arn, "demo-orders", "demo-orders-final"
    )
    assert not [d for d in paused.diagnostic_reports if d.diagnostic_kind == "dependants_block_decommission"]
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]


def test_decommission_of_a_read_replica_in_terraform_mode_renders_no_final_snapshot_and_deletes_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    from tests.drift.test_detect_executor import _adopt_estate

    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    delete = {
        "resource_changes": [
            {
                "address": ORDERS_REPLICA_ADDRESS,
                "type": "aws_db_instance",
                "change": {"actions": ["delete"], "before": {"identifier": ORDERS_REPLICA}, "after": None},
            }
        ]
    }
    clean = {"resource_changes": []}
    fake = FakeTerraformRunner(
        runner.services.execution_recorder, plan_scripts=[clean, delete, delete, clean]
    )
    runner.services.terraform_runner = fake
    arn = arn_for(session_factory, sandbox_snapshot, ORDERS_REPLICA)
    operation = service.create(
        "decommission",
        "sandbox",
        TARGET_KEY,
        {"options": {"final_snapshot_identifier": "unused-final"}, "resource_arn": arn},
        "decommission-replica-tf",
        "phil",
        resource_arn=arn,
        title=f"Decommission: {ORDERS_REPLICA}",
    )
    runner.run_once()
    paused = service.get(operation.id)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.approval_required_text == ORDERS_REPLICA
    instances_tf = (_workspace_dir(operator_settings) / "instances.tf").read_text()
    block = instances_tf[instances_tf.index('"demo_orders_replica"') :].split("\n}\n")[0]
    assert re.search(r"skip_final_snapshot\s+=\s+true", block)
    assert "final_snapshot_identifier" not in block
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert fake.calls.count("apply") == 1, "the empty first phase is not applied"
    assert final.plan["touched_addresses_phase_2"] == [ORDERS_REPLICA_ADDRESS]
    assert not [arn for arn in final.plan.get("created_resource_arns") or [] if ":snapshot:" in arn]
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    assert ORDERS_REPLICA not in {instance.identifier for instance in recorded.instances}
    assert "demo_orders_replica" not in (_workspace_dir(operator_settings) / "instances.tf").read_text()


def test_decommission_options_do_not_require_a_final_snapshot_name() -> None:
    """A read replica takes no final snapshot, so the form must let the name stay empty."""
    from rdscommander.lifecycle.options import DecommissionOptions

    assert DecommissionOptions().final_snapshot_identifier is None
    assert DecommissionOptions(final_snapshot_identifier=None).confirm_dependants_removed is False


def test_decommission_of_an_instance_without_a_final_snapshot_name_is_refused_before_approval(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    arn = _create_temp_instance()
    _refresh(session_factory, operator_settings, config_store, "decommission-no-name")
    failed, _ = _decommission_direct(session_factory, operator_settings, config_store, arn, TEMP, None)
    assert failed.state == OperationState.FAILED
    diagnostic = next(
        d for d in failed.diagnostic_reports if d.diagnostic_kind == "final_snapshot_name_required"
    )
    assert TEMP in diagnostic.detail
    assert "read replica" in diagnostic.fix_suggestion
    assert _instance_exists(TEMP)


def test_decommission_of_a_read_replica_needs_no_final_snapshot_name(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    replica_arn = arn_for(session_factory, sandbox_snapshot, ORDERS_REPLICA)
    paused, final = _decommission_direct(
        session_factory, operator_settings, config_store, replica_arn, ORDERS_REPLICA, None
    )
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
    assert paused.plan["second_phase"]["summary"] == (
        f"delete the read replica {ORDERS_REPLICA} without a final snapshot"
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    _assert_deleted_without_a_snapshot(session_factory, final)


def test_decommission_of_an_instance_still_optimising_its_storage_is_offered_and_reviewed(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    """AWS allows a delete while storage-optimization runs; only another storage change waits."""
    from rdscommander.lifecycle import direct_api as lifecycle_api

    arn = _create_temp_instance()
    _refresh(session_factory, operator_settings, config_store, "decommission-optimising")
    real_describe = lifecycle_api.instance_describe

    def still_optimising(context, identifier, step_name="await_settled"):  # noqa: ANN001, ANN202
        description = real_describe(context, identifier, step_name)
        if description is not None and identifier == TEMP:
            return {**description, "DBInstanceStatus": "storage-optimization"}
        return description

    monkeypatch.setattr(lifecycle_api, "instance_describe", still_optimising)
    paused, _ = _decommission_direct(session_factory, operator_settings, config_store, arn, TEMP, TEMP_FINAL)
    assert paused.state == OperationState.AWAITING_APPROVAL, [d.detail for d in paused.diagnostic_reports]
