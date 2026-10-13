"""The reboot on the direct-action base, and its catalogue rule."""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from rdscommander.catalogue.catalogue import (
    GROUP_INSTANCES_AND_CLUSTERS,
    GROUP_REPLICAS_AND_SCALING,
    definition_for,
    failover_possible,
    has_pending_maintenance,
    manual_snapshot,
    promotable_replica,
    rebootable,
    snapshot_source,
)
from rdscommander.control_db.models import ExecutionRecord, InventoryDbInstance, InventoryPendingMaintenance
from rdscommander.lifecycle import direct_api as lifecycle_api
from rdscommander.lifecycle.actions import (
    FAILOVER_DOWNTIME,
    PROMOTE_DOWNTIME,
    REBOOT_DOWNTIME,
    SNAPSHOT_DOWNTIME,
    PromoteReplicaExecutor,
    RebootInstanceExecutor,
)
from rdscommander.lifecycle.options import (
    FailoverOptions,
    PendingMaintenanceOptions,
    PromoteReplicaOptions,
    RebootInstanceOptions,
    TakeSnapshotOptions,
)
from rdscommander.operations.direct_action import DirectActionExecutor
from rdscommander.operations.enums import OperationState
from rdscommander.parameters import direct_api
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import load_resource, recent_operations_for
from rdscommander.terraform.record import read_record
from tests.drift.test_detect_executor import _adopt_estate
from tests.lifecycle.test_settings_changes import _run
from tests.operations.approval_helpers import approve_as_reviewed
from tests.operations.test_change_strategy import _workspace_dir
from tests.terraform.conftest import TARGET_KEY, arn_for
from tests.terraform.test_adoption_executor import FakeTerraformRunner


def test_the_reboot_is_a_direct_action_that_never_reconciles(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    assert issubclass(RebootInstanceExecutor, DirectActionExecutor)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "reboot_instance",
        "demo-orders",
        {"force_failover": False},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    review = final.plan["action_review"]
    assert review["what"] == ["reboot demo-orders"]
    assert review["identifiers"] == ["demo-orders"]
    assert review["requires_reboot"] is True and review["reconcile"] is False
    assert (
        review["downtime"]
        == REBOOT_DOWNTIME
        == "a restart of each instance (Multi-AZ fails over when forced)"
    )
    assert final.plan["reboot_identifiers"] == ["demo-orders"]
    assert [a.step_name for a in final.step_attempts if a.outcome_status == "completed"] == [
        "await_approval",
        "perform",
        "await_settled",
        "refresh_resource",
    ]
    perform = next(a for a in final.step_attempts if a.step_name == "perform")
    assert perform.output == {"rebooted": ["demo-orders"]}


def test_a_forced_failover_reboot_says_so_in_the_review_and_in_the_call(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "reboot_instance",
        "demo-orders",
        {"force_failover": True},
    )
    assert final.plan["action_review"]["what"] == ["reboot demo-orders with failover"]
    with session_factory() as session:
        [call] = session.query(ExecutionRecord).filter_by(
            operation_id=final.id, record_kind="aws_api", api_name="rds:RebootDBInstance"
        )
        assert call.step_name == "perform"
        assert call.request_parameters["ForceFailover"] is True


def test_a_forced_failover_reboot_is_refused_for_an_instance_without_a_standby_before_any_reboot(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    # AWS refuses ForceFailover on a single-AZ instance after approval; the review refuses it instead. The
    # inventory still says Multi-AZ, so the refusal comes from the live description.
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_instance(DBInstanceIdentifier="demo-orders-replica", MultiAZ=False, ApplyImmediately=True)
    assert not rds.describe_db_instances(DBInstanceIdentifier="demo-orders-replica")["DBInstances"][0][
        "MultiAZ"
    ]
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "reboot_instance",
        "demo-orders-replica",
        {"force_failover": True},
    )
    assert final.state == OperationState.FAILED
    diagnostic = _failed_diagnostic(final, "force_failover_needs_multi_az")
    assert "demo-orders-replica" in diagnostic.detail
    assert "without failover" in diagnostic.fix_suggestion
    with session_factory() as session:
        assert (
            not session.query(ExecutionRecord)
            .filter_by(operation_id=final.id, api_name="rds:RebootDBInstance")
            .all()
        )


def test_the_reboot_definition_is_an_instance_action_with_the_rebootable_rule() -> None:
    definition = definition_for("reboot_instance")
    assert definition.title == "Reboot"
    assert definition.group == GROUP_INSTANCES_AND_CLUSTERS
    assert definition.applicability is rebootable
    assert definition.options_model is RebootInstanceOptions
    assert definition.summary == (
        "Reboot an instance, or every instance using a parameter group that is pending a reboot, with or "
        "without failover."
    )


def test_rebootable_offers_any_available_instance_and_a_group_only_with_pending_instances(
    session_factory, console_config, sandbox_snapshot
) -> None:
    environment = console_config.environment("sandbox")
    with session_factory() as session:
        orders = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-orders")
        )
        group = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
        )
    assert rebootable(orders, environment).ok
    stopped = orders.model_copy(update={"status": "stopped"})
    assert rebootable(stopped, environment).reason == "the instance is stopped"
    assert not rebootable(group, environment).ok
    assert "pending a reboot" in rebootable(group, environment).reason
    assert rebootable(None, environment).reason == "pick an instance or a parameter group"


# ----- failover, pending maintenance, take and delete snapshot -------------------------------------------


def _seed_pending_maintenance(session_factory, identifier: str, action: str = "system-update") -> str:
    """Put a pending maintenance row on `identifier` in its latest inventory snapshot; returns the ARN."""
    with session_factory() as session:
        row = session.query(InventoryDbInstance).filter_by(db_instance_identifier=identifier).one()
        session.add(
            InventoryPendingMaintenance(
                snapshot_id=row.snapshot_id,
                resource_arn=row.db_instance_arn,
                action=action,
                description="A pending update",
            )
        )
        session.commit()
        return row.db_instance_arn


def _failed_diagnostic(final, kind: str):
    return next(d for d in final.diagnostic_reports if d.diagnostic_kind == kind)


def test_failover_swaps_the_writer_and_records_the_review(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_failover",
        "demo-aurora",
        {"target_member_identifier": "demo-aurora-reader"},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    review = final.plan["action_review"]
    assert review["what"] == ["fail over demo-aurora from demo-aurora-writer to demo-aurora-reader"]
    assert review["requires_reboot"] is False and review["reconcile"] is False
    assert review["downtime"] == FAILOVER_DOWNTIME
    cluster = boto3.client("rds", region_name="eu-west-2").describe_db_clusters(
        DBClusterIdentifier="demo-aurora"
    )["DBClusters"][0]
    [writer] = [m["DBInstanceIdentifier"] for m in cluster["DBClusterMembers"] if m["IsClusterWriter"]]
    assert writer == "demo-aurora-reader"
    assert [a.step_name for a in final.step_attempts if a.outcome_status == "completed"] == [
        "await_approval",
        "perform",
        "await_settled",
        "refresh_resource",
    ]
    definition = definition_for("cluster_failover")
    assert definition.disruptive is True and definition.pauses_for_approval is True


def test_failover_without_a_target_lets_aws_pick_and_names_that_in_the_review(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_failover",
        "demo-aurora",
        {},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert final.plan["action_review"]["what"] == [
        "fail over demo-aurora from demo-aurora-writer to the member AWS picks"
    ]
    with session_factory() as session:
        [call] = session.query(ExecutionRecord).filter_by(
            operation_id=final.id, record_kind="aws_api", api_name="rds:FailoverDBCluster"
        )
        assert "TargetDBInstanceIdentifier" not in call.request_parameters
    perform = next(a for a in final.step_attempts if a.step_name == "perform")
    assert perform.output["writer_before"] == "demo-aurora-writer"
    cluster = boto3.client("rds", region_name="eu-west-2").describe_db_clusters(
        DBClusterIdentifier="demo-aurora"
    )["DBClusters"][0]
    [writer] = [m["DBInstanceIdentifier"] for m in cluster["DBClusterMembers"] if m["IsClusterWriter"]]
    assert writer != "demo-aurora-writer"


def test_failover_without_a_target_that_changes_nothing_does_not_settle(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    monkeypatch.setattr(lifecycle_api, "failover_cluster", lambda *args, **kwargs: None)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_failover",
        "demo-aurora",
        {},
    )
    assert final.state == OperationState.FAILED
    failed = next(a for a in final.step_attempts if a.outcome_status == "failed")
    assert failed.step_name == "await_settled"
    assert "the writer is still demo-aurora-writer" in failed.detail


def test_failover_refuses_a_target_that_is_already_the_live_writer_even_if_the_inventory_is_stale(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").failover_db_cluster(
        DBClusterIdentifier="demo-aurora", TargetDBInstanceIdentifier="demo-aurora-reader"
    )
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_failover",
        "demo-aurora",
        {"target_member_identifier": "demo-aurora-reader"},
    )
    assert final.state == OperationState.FAILED
    detail = next(a for a in final.step_attempts if a.outcome_status == "failed").detail
    assert "demo-aurora-reader is already the writer of demo-aurora" in detail


def test_failover_is_refused_while_the_target_member_is_not_available(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    real_instance_status = direct_api.instance_status

    def reader_is_modifying(context, identifier, step_name="await_available"):
        if identifier == "demo-aurora-reader":
            return "modifying"
        return real_instance_status(context, identifier, step_name)

    monkeypatch.setattr(direct_api, "instance_status", reader_is_modifying)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_failover",
        "demo-aurora",
        {"target_member_identifier": "demo-aurora-reader"},
    )
    assert final.state == OperationState.FAILED
    failed = next(a for a in final.step_attempts if a.outcome_status == "failed")
    assert failed.step_name == "await_approval"
    assert "demo-aurora-reader is modifying; wait until every member is available" in failed.detail
    assert not any(a.step_name == "perform" for a in final.step_attempts)


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("demo-orders", "demo-orders is not a member of demo-aurora"),
        ("demo-aurora-writer", "demo-aurora-writer is already the writer of demo-aurora"),
    ],
)
def test_failover_refuses_a_target_that_is_not_a_member_or_is_already_the_writer(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, target, expected
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "cluster_failover",
        "demo-aurora",
        {"target_member_identifier": target},
    )
    assert final.state == OperationState.FAILED
    assert expected in next(a for a in final.step_attempts if a.outcome_status == "failed").detail


def test_apply_pending_maintenance_fails_cleanly_on_the_simulator(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _seed_pending_maintenance(session_factory, "demo-orders")
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "apply_pending_maintenance",
        "demo-orders",
        {"action": "system-update", "opt_in_type": "next-maintenance"},
    )
    assert final.state == OperationState.FAILED
    failed = next(a for a in final.step_attempts if a.outcome_status == "failed")
    assert failed.step_name == "perform"
    diagnostic = _failed_diagnostic(final, "simulator_unsupported")
    assert "ApplyPendingMaintenanceAction" in diagnostic.detail
    assert final.plan["action_review"]["downtime"] == "none now; AWS applies it in the window"
    assert final.approval_required_text is None


def test_apply_pending_maintenance_with_a_scripted_client_submits_the_opt_in(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    orders_arn = _seed_pending_maintenance(session_factory, "demo-orders")
    calls: list[tuple] = []
    monkeypatch.setattr(
        lifecycle_api,
        "apply_pending_maintenance",
        lambda context, arn, action, opt_in_type, step_name="perform": calls.append(
            (arn, action, opt_in_type, step_name)
        ),
    )
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "apply_pending_maintenance",
        "demo-orders",
        {"action": "system-update", "opt_in_type": "immediate"},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert calls == [(orders_arn, "system-update", "immediate", "perform")]
    assert final.approval_required_text == "demo-orders"
    assert final.plan["action_review"]["downtime"] == "the action's own interruption, now"


def test_apply_pending_maintenance_refuses_an_action_that_is_not_pending(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    _seed_pending_maintenance(session_factory, "demo-orders")
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "apply_pending_maintenance",
        "demo-orders",
        {"action": "db-upgrade", "opt_in_type": "immediate"},
    )
    assert final.state == OperationState.FAILED
    detail = next(a for a in final.step_attempts if a.outcome_status == "failed").detail
    assert detail == "db-upgrade is not pending on demo-orders; pending: system-update"


def test_take_snapshot_creates_it_tags_it_and_links_the_operation_to_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_take",
        "demo-orders",
        {"snapshot_identifier": "demo-orders-before-change", "tags": {"reason": "test"}},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert final.plan["action_review"]["what"] == [
        "take a manual snapshot demo-orders-before-change of demo-orders"
    ]
    assert final.plan["action_review"]["downtime"] == SNAPSHOT_DOWNTIME
    snapshot = boto3.client("rds", region_name="eu-west-2").describe_db_snapshots(
        DBSnapshotIdentifier="demo-orders-before-change"
    )["DBSnapshots"][0]
    assert snapshot["Status"] == "available"
    assert {"Key": "reason", "Value": "test"} in snapshot["TagList"]
    assert final.plan["created_resource_arns"] == [snapshot["DBSnapshotArn"]]
    assert final.resource_arn.endswith(":db:demo-orders")
    assert "source_resource_arn" not in final.plan
    with session_factory() as session:
        assert [operation.id for operation in recent_operations_for(session, final.resource_arn)] == [
            final.id
        ]
        record = load_resource(session, console_config, snapshot["DBSnapshotArn"])
    assert record is not None and record.facts["snapshot_type"] == "manual"


def test_take_snapshot_refuses_a_name_in_use(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").create_db_snapshot(
        DBSnapshotIdentifier="taken", DBInstanceIdentifier="demo-orders"
    )
    taken, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_take",
        "demo-orders",
        {"snapshot_identifier": "taken"},
    )
    assert taken.state == OperationState.FAILED
    assert (
        "a snapshot named taken already exists"
        in next(a for a in taken.step_attempts if a.outcome_status == "failed").detail
    )


def test_take_snapshot_refuses_a_cluster_member(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    member, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_take",
        "demo-aurora-reader",
        {"snapshot_identifier": "member-snapshot"},
    )
    assert member.state == OperationState.FAILED
    assert (
        "snapshot the cluster instead"
        in next(a for a in member.step_attempts if a.outcome_status == "failed").detail
    )


def test_take_snapshot_of_a_cluster_uses_the_cluster_snapshot_calls(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_take",
        "demo-aurora",
        {"snapshot_identifier": "demo-aurora-before-change"},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert final.plan["created_resource_arns"][0].endswith(":cluster-snapshot:demo-aurora-before-change")
    assert final.resource_arn.endswith(":cluster:demo-aurora")
    snapshot = boto3.client("rds", region_name="eu-west-2").describe_db_cluster_snapshots(
        DBClusterSnapshotIdentifier="demo-aurora-before-change"
    )["DBClusterSnapshots"][0]
    assert snapshot["Status"] == "available"


def test_take_snapshot_of_a_foreign_owned_instance_is_refused_unless_the_environment_allows_it(
    session_factory, console_config, sandbox_snapshot
) -> None:
    environment = console_config.environment("sandbox")
    with session_factory() as session:
        orders = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-orders")
        )
    foreign = orders.model_copy(update={"ownership": "foreign_cloudformation"})
    assert snapshot_source(foreign, environment).reason == "owned by other tooling; read-only here"
    allowed = environment.model_copy(update={"allow_direct_changes_to_foreign_owned": True})
    assert snapshot_source(foreign, allowed).ok
    assert snapshot_source(orders, environment).ok


def test_delete_snapshot_requires_a_manual_snapshot_and_removes_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    taken, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_take",
        "demo-orders",
        {"snapshot_identifier": "demo-orders-to-delete"},
    )
    assert taken.state == OperationState.SUCCEEDED, [d.detail for d in taken.diagnostic_reports]
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_delete",
        "demo-orders-to-delete",
        {},
        resource_arn=taken.plan["created_resource_arns"][0],
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert final.plan["action_review"]["what"] == [
        "delete snapshot demo-orders-to-delete; this cannot be undone"
    ]
    with pytest.raises(ClientError) as raised:
        boto3.client("rds", region_name="eu-west-2").describe_db_snapshots(
            DBSnapshotIdentifier="demo-orders-to-delete"
        )
    assert raised.value.response["Error"]["Code"] == "DBSnapshotNotFound"
    definition = definition_for("snapshot_delete")
    assert definition.destructive is True and definition.pauses_for_approval is True
    with session_factory() as session:
        assert load_resource(session, console_config, taken.plan["created_resource_arns"][0]) is None


def test_automated_snapshots_cannot_be_deleted_from_the_console(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    environment = console_config.environment("sandbox")
    boto3.client("rds", region_name="eu-west-2").create_db_snapshot(
        DBSnapshotIdentifier="manual-one", DBInstanceIdentifier="demo-orders"
    )
    snapshot = ResourceRecord(
        kind=ResourceKind.SNAPSHOT,
        arn="arn:aws:rds:eu-west-2:000000000000:snapshot:rds:demo-orders-2026-10-11",
        resource_id="x",
        identifier="rds:demo-orders-2026-10-11",
        environment_slug="sandbox",
        target_key="sandbox/000000000000/eu-west-2",
        ownership="unmanaged",
        status="available",
        snapshot_id=1,
        facts={"snapshot_type": "automated", "snapshot_scope": "instance"},
    )
    assert "automated" in manual_snapshot(snapshot, environment).reason
    manual = snapshot.model_copy(update={"facts": {"snapshot_type": "manual", "snapshot_scope": "instance"}})
    assert manual_snapshot(manual, environment).ok
    creating = manual.model_copy(update={"status": "creating"})
    assert "creating" in manual_snapshot(creating, environment).reason


def test_failover_and_maintenance_rules_explain_themselves(
    session_factory, console_config, sandbox_snapshot
) -> None:
    environment = console_config.environment("sandbox")
    with session_factory() as session:
        cluster = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-aurora")
        )
        orders = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-orders")
        )
    assert failover_possible(cluster, environment).ok
    assert failover_possible(orders, environment).reason == "applies to clusters"
    single = cluster.model_copy(
        update={"relationships": {**cluster.relationships, "member_identifiers": ["a"]}}
    )
    assert "at least two members" in failover_possible(single, environment).reason
    assert (
        has_pending_maintenance(orders, environment).reason
        == "AWS has no maintenance pending on this resource"
    )
    pending = orders.model_copy(
        update={"facts": {**orders.facts, "pending_maintenance_actions": ["system-update"]}}
    )
    assert has_pending_maintenance(pending, environment).ok


def test_an_empty_failover_target_means_let_aws_choose() -> None:
    assert FailoverOptions(target_member_identifier="").target_member_identifier is None
    assert FailoverOptions(target_member_identifier="  ").target_member_identifier is None
    assert FailoverOptions().target_member_identifier is None
    assert FailoverOptions(target_member_identifier="a-b").target_member_identifier == "a-b"


def test_take_snapshot_options_refuse_names_and_tags_rds_or_the_console_would_refuse() -> None:
    assert TakeSnapshotOptions(snapshot_identifier="a-b1").tags == {}
    for bad, fragment in (
        ("1abc", "starts with a lower-case letter"),
        ("Abc", "starts with a lower-case letter"),
        ("a--b", "two hyphens"),
        ("ab-", "cannot end with a hyphen"),
    ):
        with pytest.raises(ValidationError, match=fragment):
            TakeSnapshotOptions(snapshot_identifier=bad)
    with pytest.raises(ValidationError, match="rdscommander:"):
        TakeSnapshotOptions(snapshot_identifier="ok", tags={"rdscommander:managed": "x"})
    with pytest.raises(ValidationError):
        PendingMaintenanceOptions(action="system-update", opt_in_type="whenever")


# ----- promote a read replica -----------------------------------------------------------------------------

REPLICA = "demo-orders-replica"
REPLICA_ADDRESS = "aws_db_instance.demo_orders_replica"


def test_promote_replica_stops_replication_and_reconciles_the_rendered_instance_in_direct_mode(
    session_factory,
    operator_settings,
    config_store,
    sandbox_snapshot,
    moto_aws,
    moto_promotion_ends_replication,
) -> None:
    assert issubclass(PromoteReplicaExecutor, DirectActionExecutor)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "replica_promote",
        REPLICA,
        {"backup_retention_period_days": 9},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    review = final.plan["action_review"]
    assert review["what"] == [
        "promote demo-orders-replica to a standalone instance; replication from demo-orders stops"
    ]
    assert review["identifiers"] == [REPLICA]
    assert review["reconcile"] is True and review["requires_reboot"] is True
    assert review["downtime"] == PROMOTE_DOWNTIME
    assert final.approval_required_text == REPLICA
    assert final.plan["touched_addresses"] == [REPLICA_ADDRESS]
    assert [a.step_name for a in final.step_attempts if a.outcome_status == "completed"] == [
        "await_approval",
        "perform",
        "await_settled",
        "refresh_resource",
        "reconcile",
    ]
    perform = next(a for a in final.step_attempts if a.step_name == "perform")
    assert perform.output == {"promoted": REPLICA, "backup_retention_period_days": 9}
    described = boto3.client("rds", region_name="eu-west-2").describe_db_instances(
        DBInstanceIdentifier=REPLICA
    )["DBInstances"][0]
    assert not described.get("ReadReplicaSourceDBInstanceIdentifier")
    assert described["BackupRetentionPeriod"] == 9
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    replica = next(i for i in recorded.instances if i.identifier == REPLICA)
    assert replica.replicate_source_reference is None
    assert replica.engine_version
    assert replica.backup_retention_period == 9


def test_promote_replica_in_terraform_mode_reconciles_with_refresh_only_and_a_clean_plan(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
    moto_promotion_ends_replication,
) -> None:
    runner, service = _adopt_estate(
        session_factory, operator_settings, config_store, console_config, sandbox_snapshot
    )
    instances_file = _workspace_dir(operator_settings) / "instances.tf"
    assert "replicate_source_db" in instances_file.read_text()
    fake = FakeTerraformRunner(runner.services.execution_recorder, plan_scripts=[{"resource_changes": []}])
    runner.services.terraform_runner = fake
    arn = arn_for(session_factory, sandbox_snapshot, REPLICA)
    operation = service.create(
        "replica_promote",
        "sandbox",
        TARGET_KEY,
        {"options": {"backup_retention_period_days": 7}, "resource_arn": arn},
        "promote-tf",
        "phil",
        resource_arn=arn,
        title="Promote replica: demo-orders-replica",
    )
    runner.run_once()
    assert service.get(operation.id).state == OperationState.AWAITING_APPROVAL
    assert "apply_refresh_only" not in fake.calls
    approve_as_reviewed(service, operation.id, "sam")
    runner.run_once()
    final = service.get(operation.id)
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    assert "apply_refresh_only" in fake.calls and "plan:reconcile" in fake.calls
    assert "apply" not in fake.calls
    rendered = instances_file.read_text()
    assert "replicate_source_db" not in rendered
    assert "demo_orders_replica" in rendered
    recorded = read_record(_workspace_dir(operator_settings)).render_input
    replica = next(i for i in recorded.instances if i.identifier == REPLICA)
    assert replica.replicate_source_reference is None and replica.engine_version


def test_promote_is_refused_while_the_source_is_not_available(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, monkeypatch
) -> None:
    real_instance_status = direct_api.instance_status

    def source_is_modifying(context, identifier, step_name="await_available"):
        if identifier == "demo-orders":
            return "modifying"
        return real_instance_status(context, identifier, step_name)

    monkeypatch.setattr(direct_api, "instance_status", source_is_modifying)
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "replica_promote",
        REPLICA,
        {},
    )
    assert final.state == OperationState.FAILED
    failed = next(a for a in final.step_attempts if a.outcome_status == "failed")
    assert failed.step_name == "await_approval"
    assert "the source demo-orders is modifying; wait until it is available" in failed.detail
    assert not any(a.step_name == "perform" for a in final.step_attempts)


def test_promote_is_refused_for_an_instance_that_is_not_a_replica(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "replica_promote",
        "demo-orders",
        {},
    )
    assert final.state == OperationState.FAILED
    assert (
        "demo-orders is not a read replica"
        in next(a for a in final.step_attempts if a.outcome_status == "failed").detail
    )


def test_promote_fails_at_approval_when_the_workspace_is_dirty_before_any_aws_call(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    from tests.terraform.test_adoption_executor import _prepare_repo

    _prepare_repo(operator_settings)
    workspace_dir = _workspace_dir(operator_settings)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "hand_edit.tf").write_text("# uncommitted\n")
    final, _ = _run(
        session_factory, operator_settings, config_store, sandbox_snapshot, "replica_promote", REPLICA, {}
    )
    assert final.state == OperationState.FAILED
    assert _failed_diagnostic(final, "checkout_guard").step_name == "await_approval"
    described = boto3.client("rds", region_name="eu-west-2").describe_db_instances(
        DBInstanceIdentifier=REPLICA
    )["DBInstances"][0]
    assert described["ReadReplicaDBInstanceIdentifiers"] == [] and described.get(
        "ReadReplicaSourceDBInstanceIdentifier"
    )


def test_promote_definition_requires_the_identifier_and_refuses_open_drift() -> None:
    definition = definition_for("replica_promote")
    assert definition.title == "Promote replica"
    assert definition.group == GROUP_REPLICAS_AND_SCALING
    assert definition.disruptive is True and definition.changes_the_resource is True
    assert definition.applicability is promotable_replica
    assert definition.options_model is PromoteReplicaOptions


def test_promotable_replica_offers_only_available_replicas_the_console_may_change(
    session_factory, console_config, sandbox_snapshot
) -> None:
    environment = console_config.environment("sandbox")
    with session_factory() as session:
        replica = load_resource(session, console_config, arn_for(session_factory, sandbox_snapshot, REPLICA))
        orders = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-orders")
        )
        cluster = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-aurora")
        )
    assert promotable_replica(replica.model_copy(update={"ownership": "adopted"}), environment).ok
    assert "adopt it first" in promotable_replica(replica, environment).reason
    assert promotable_replica(orders, environment).reason == "this instance is not a read replica"
    assert promotable_replica(cluster, environment).reason == "applies to instances"
    assert promotable_replica(None, environment).reason == "pick a read replica"
    busy = replica.model_copy(update={"status": "modifying", "ownership": "adopted"})
    assert (
        promotable_replica(busy, environment).reason
        == "the instance is modifying; wait until it is available"
    )
    foreign = replica.model_copy(update={"ownership": "foreign_cloudformation"})
    assert "owned by other tooling" in promotable_replica(foreign, environment).reason


def test_promote_options_keep_retention_between_one_and_thirty_five_days() -> None:
    assert PromoteReplicaOptions().backup_retention_period_days == 7
    for bad in (0, 36):
        with pytest.raises(ValidationError):
            PromoteReplicaOptions(backup_retention_period_days=bad)


def test_promote_settles_on_the_simulator_although_moto_keeps_reporting_the_source(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    """Known limit: moto 5.2.3 never clears ReadReplicaSourceDBInstanceIdentifier, so on a simulated
    target `available` is what settles a promotion (real AWS also needs the source gone)."""
    final, _ = _run(
        session_factory, operator_settings, config_store, sandbox_snapshot, "replica_promote", REPLICA, {}
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
    settled = next(a for a in final.step_attempts if a.step_name == "await_settled")
    assert settled.output["live"] == {"status": "available", "read_replica_source_identifier": "demo-orders"}


def test_a_snapshot_is_taken_while_aws_optimises_the_instance_storage(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws
) -> None:
    # storage-optimization runs for hours after a storage change; AWS takes snapshots meanwhile.
    with session_factory() as session:
        row = (
            session.query(InventoryDbInstance)
            .filter_by(snapshot_id=sandbox_snapshot.id, db_instance_identifier="demo-orders")
            .one()
        )
        row.db_instance_status = "storage-optimization"
        session.commit()
    final, _ = _run(
        session_factory,
        operator_settings,
        config_store,
        sandbox_snapshot,
        "snapshot_take",
        "demo-orders",
        {"snapshot_identifier": "demo-orders-while-optimising"},
    )
    assert final.state == OperationState.SUCCEEDED, [d.detail for d in final.diagnostic_reports]
