from datetime import datetime, timedelta

import boto3
import pytest

from rdscommander.control_db.models import (
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryDbSnapshot,
    InventorySnapshot,
    Operation,
    OperationStepAttempt,
    ResourceChangeEvent,
    TerraformRun,
    TerraformWorkspace,
    utc_now,
)
from rdscommander.history.change_events import (
    compute_change_events,
    explain_event,
    recent_change_events_for,
    resources_in_snapshot,
    review_change_event,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.main import build_executors, build_services
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from tests.terraform.conftest import TARGET_KEY, arn_for


def _refresh(session_factory, operator_settings, config_store, key: str):
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "r",
        operator_settings,
        config_store,
        services,
    )
    operation = OperationService(session_factory).create(
        "inventory_refresh", "sandbox", None, {}, key, "phil"
    )
    runner.run_once()
    return operation


def _add_operation(
    session_factory,
    resource_arn: str,
    state: str,
    idempotency_key: str,
    started_at_utc: datetime | None,
    finished_at_utc: datetime | None,
):
    """Insert an operation acting on `resource_arn` and return its id; a RUNNING one is leased elsewhere."""
    with session_factory() as session:
        operation = Operation(
            operation_kind="parameter_group_attach",
            state=state,
            environment_slug="sandbox",
            target_key=TARGET_KEY,
            plan={},
            idempotency_key=idempotency_key,
            created_by_operator_name="phil",
            resource_arn=resource_arn,
            title="x",
            started_at_utc=started_at_utc,
            finished_at_utc=finished_at_utc,
            lease_owner="another-runner" if state == "RUNNING" else None,
            lease_expires_at_utc=utc_now() + timedelta(hours=1) if state == "RUNNING" else None,
        )
        session.add(operation)
        session.commit()
        return operation.id


def _all_events(session_factory) -> list[ResourceChangeEvent]:
    with session_factory() as session:
        events = session.query(ResourceChangeEvent).order_by(ResourceChangeEvent.id).all()
        session.expunge_all()
        return events


def test_events_are_written_for_attribute_parameter_and_removal_changes(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders", DBInstanceClass="db.r6g.xlarge", ApplyImmediately=True
    )
    rds.modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[
            {
                "ParameterName": "work_mem",
                "ParameterValue": "32768",
                "ApplyMethod": "immediate",
                "Source": "user",
            }
        ],
    )
    rds.delete_db_instance(DBInstanceIdentifier="demo-orders-replica", SkipFinalSnapshot=True)
    _refresh(session_factory, operator_settings, config_store, "history-1")
    events = _all_events(session_factory)
    by_key = {(e.resource_arn.rsplit(":", 1)[-1], e.attribute_path): e for e in events}
    assert by_key[("demo-orders", "db_instance_class")].value_before == "db.m6g.large"
    assert by_key[("demo-orders", "db_instance_class")].value_after == "db.r6g.xlarge"
    assert by_key[("demo-postgres16-tuned", "parameter.work_mem")].value_before == "16384"
    assert by_key[("demo-postgres16-tuned", "parameter.work_mem")].value_after == "32768"
    removed = by_key[("demo-orders-replica", "removed")]
    assert removed.value_before == "available" and removed.value_after is None
    assert all(e.explanation == "outside_console" and e.operation_id is None for e in events)
    assert all(e.target_key == TARGET_KEY and e.environment_slug == "sandbox" for e in events)
    assert all(e.reviewed_at_utc is None and e.reviewed_by_operator_name is None for e in events)


def test_the_first_snapshot_of_a_target_produces_no_events(sandbox_snapshot, session_factory) -> None:
    assert _all_events(session_factory) == []


def test_a_refresh_that_finds_nothing_new_produces_no_events(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _refresh(session_factory, operator_settings, config_store, "history-quiet")
    assert _all_events(session_factory) == []


def test_a_manual_snapshot_appearing_produces_a_created_event(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").create_db_snapshot(
        DBSnapshotIdentifier="demo-orders-manual", DBInstanceIdentifier="demo-orders"
    )
    _refresh(session_factory, operator_settings, config_store, "history-created")
    [created] = _all_events(session_factory)
    assert created.attribute_path == "created"
    assert created.resource_arn.endswith(":snapshot:demo-orders-manual")
    assert created.value_before is None and created.value_after == "available"
    assert created.explanation == "outside_console"


def test_an_automated_snapshot_appearing_produces_no_event(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").create_db_instance(
        DBInstanceIdentifier="demo-new-instance",
        Engine="postgres",
        EngineVersion="16.3",
        DBInstanceClass="db.t4g.micro",
        MasterUsername="u",
        MasterUserPassword="password1",
        AllocatedStorage=20,
    )
    _refresh(session_factory, operator_settings, config_store, "history-automated")
    with session_factory() as session:
        latest = latest_snapshot_for_target(session, TARGET_KEY)
        automated_snapshot_arns = {
            row.db_snapshot_arn
            for row in session.query(InventoryDbSnapshot).filter_by(
                snapshot_id=latest.id, snapshot_type="automated"
            )
        }
    events = _all_events(session_factory)
    assert any(":snapshot:rds:demo-new-instance" in arn for arn in automated_snapshot_arns)
    assert not {e.resource_arn for e in events} & automated_snapshot_arns
    [instance_created] = [e for e in events if e.resource_arn.endswith(":db:demo-new-instance")]
    assert instance_created.attribute_path == "created" and instance_created.value_after == "available"


def test_status_flips_of_instances_clusters_and_snapshots_produce_no_event(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").create_db_snapshot(
        DBSnapshotIdentifier="demo-orders-manual", DBInstanceIdentifier="demo-orders"
    )
    _refresh(session_factory, operator_settings, config_store, "history-status-a")
    _refresh(session_factory, operator_settings, config_store, "history-status-b")
    with session_factory() as session:
        previous_snapshot, new_snapshot = (
            session.query(InventorySnapshot)
            .filter_by(target_key=TARGET_KEY)
            .order_by(InventorySnapshot.id.desc())
            .limit(2)
            .all()[::-1]
        )
        session.query(InventoryDbInstance).filter_by(snapshot_id=previous_snapshot.id).update(
            {"db_instance_status": "modifying"}
        )
        session.query(InventoryDbCluster).filter_by(snapshot_id=previous_snapshot.id).update(
            {"status": "backing-up"}
        )
        session.query(InventoryDbSnapshot).filter_by(snapshot_id=previous_snapshot.id).update(
            {"status": "creating"}
        )
        session.commit()
        events = compute_change_events(
            session, console_config.target(TARGET_KEY), previous_snapshot.id, new_snapshot.id
        )
    assert events == []


def test_events_inside_an_operation_window_are_attributed_to_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    explaining_id = _add_operation(
        session_factory,
        orders_arn,
        "SUCCEEDED",
        "explain-1",
        started_at_utc=utc_now() - timedelta(minutes=1),
        finished_at_utc=utc_now(),
    )
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=7, ApplyImmediately=True
    )
    _refresh(session_factory, operator_settings, config_store, "history-2")
    with session_factory() as session:
        event = session.query(ResourceChangeEvent).filter_by(attribute_path="backup_retention_period").one()
        assert event.explanation == "operation" and event.operation_id == explaining_id


@pytest.mark.parametrize("state", ["FAILED", "RUNNING"])
def test_events_during_a_failed_or_still_running_operation_are_attributed_to_it(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws, state
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    explaining_id = _add_operation(
        session_factory,
        orders_arn,
        state,
        f"explain-{state}",
        started_at_utc=utc_now() - timedelta(minutes=1),
        finished_at_utc=None if state == "RUNNING" else utc_now(),
    )
    if state == "FAILED":
        _add_step_attempt(session_factory, explaining_id, "apply_direct", "completed")  # it reached AWS
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=7, ApplyImmediately=True
    )
    _refresh(session_factory, operator_settings, config_store, f"history-{state}")
    with session_factory() as session:
        event = session.query(ResourceChangeEvent).filter_by(attribute_path="backup_retention_period").one()
        assert event.explanation == "operation" and event.operation_id == explaining_id


def _add_operation_with_plan(
    session_factory, operation_kind: str, resource_arn: str | None, plan: dict, key: str, target_key: str
):
    """Insert a succeeded operation that ran a minute ago with `plan`; return its id."""
    with session_factory() as session:
        operation = Operation(
            operation_kind=operation_kind,
            state="SUCCEEDED",
            environment_slug="sandbox",
            target_key=target_key,
            plan=plan,
            idempotency_key=key,
            created_by_operator_name="phil",
            resource_arn=resource_arn,
            title="x",
            started_at_utc=utc_now() - timedelta(minutes=1),
            finished_at_utc=utc_now(),
        )
        session.add(operation)
        session.commit()
        return operation.id


def test_a_change_to_every_resource_an_adoption_selected_is_attributed_to_the_adoption(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """An adoption started on one resource tags every selected one; none of it is outside the console."""
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    adoption_id = _add_operation_with_plan(
        session_factory,
        "terraform_adoption",
        orders_arn,
        {
            "target_key": TARGET_KEY,
            "selected_resources": [
                {
                    "resource_arn": group_arn,
                    "terraform_address": "aws_db_parameter_group.demo_postgres16_tuned",
                },
                {"resource_arn": orders_arn, "terraform_address": "aws_db_instance.demo_orders"},
            ],
        },
        "adopt-explains",
        TARGET_KEY,
    )
    boto3.client("rds", region_name="eu-west-2").add_tags_to_resource(
        ResourceName=group_arn, Tags=[{"Key": "rdscommander:managed", "Value": "true"}]
    )
    _refresh(session_factory, operator_settings, config_store, "history-adopt")
    with session_factory() as session:
        events = session.query(ResourceChangeEvent).filter_by(resource_arn=group_arn).all()
        assert {event.attribute_path for event in events} >= {"tag.rdscommander:managed"}
        assert all(event.explanation == "operation" and event.operation_id == adoption_id for event in events)


def test_a_resource_an_operation_created_is_attributed_to_it_through_its_touched_addresses(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """A create has no resource yet; the address it touched names the group that appears."""
    creation_id = _add_operation_with_plan(
        session_factory,
        "parameter_group_create",
        None,
        {
            "options": {"name": "orders-pg16-v2"},
            "touched_addresses": ["aws_db_parameter_group.orders_pg16_v2"],
        },
        "create-explains",
        TARGET_KEY,
    )
    boto3.client("rds", region_name="eu-west-2").create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    _refresh(session_factory, operator_settings, config_store, "history-create")
    with session_factory() as session:
        event = session.query(ResourceChangeEvent).filter_by(attribute_path="created").one()
        assert event.resource_arn.endswith(":pg:orders-pg16-v2")
        assert event.explanation == "operation" and event.operation_id == creation_id


def test_a_snapshot_an_operation_created_is_attributed_to_it_through_its_created_resource_arns(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """A snapshot operation keeps the source as its resource; the snapshot it made is listed in its plan."""
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    snapshot_arn = boto3.client("rds", region_name="eu-west-2").create_db_snapshot(
        DBSnapshotIdentifier="orders-before-change", DBInstanceIdentifier="demo-orders"
    )["DBSnapshot"]["DBSnapshotArn"]
    taking_id = _add_operation_with_plan(
        session_factory,
        "snapshot_take",
        orders_arn,
        {"options": {"snapshot_identifier": "orders-before-change"}, "created_resource_arns": [snapshot_arn]},
        "snapshot-explains",
        TARGET_KEY,
    )
    _refresh(session_factory, operator_settings, config_store, "history-snapshot")
    with session_factory() as session:
        event = (
            session.query(ResourceChangeEvent)
            .filter_by(resource_arn=snapshot_arn, attribute_path="created")
            .one()
        )
        assert event.explanation == "operation" and event.operation_id == taking_id


def test_a_snapshot_nobody_listed_as_created_is_outside_the_console(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    _add_operation_with_plan(
        session_factory,
        "snapshot_take",
        orders_arn,
        {"options": {"snapshot_identifier": "something-else"}, "created_resource_arns": []},
        "snapshot-unlisted",
        TARGET_KEY,
    )
    snapshot_arn = boto3.client("rds", region_name="eu-west-2").create_db_snapshot(
        DBSnapshotIdentifier="orders-by-hand", DBInstanceIdentifier="demo-orders"
    )["DBSnapshot"]["DBSnapshotArn"]
    _refresh(session_factory, operator_settings, config_store, "history-snapshot-unlisted")
    with session_factory() as session:
        event = (
            session.query(ResourceChangeEvent)
            .filter_by(resource_arn=snapshot_arn, attribute_path="created")
            .one()
        )
        assert event.explanation == "outside_console" and event.operation_id is None


def test_touched_addresses_in_another_target_do_not_explain_a_change(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    _add_operation_with_plan(
        session_factory,
        "parameter_group_create",
        None,
        {"touched_addresses": ["aws_db_parameter_group.orders_pg16_v2"]},
        "create-elsewhere",
        "staging/000000000000/eu-west-2",
    )
    boto3.client("rds", region_name="eu-west-2").create_db_parameter_group(
        DBParameterGroupName="orders-pg16-v2", DBParameterGroupFamily="postgres16", Description="v2"
    )
    _refresh(session_factory, operator_settings, config_store, "history-create-elsewhere")
    with session_factory() as session:
        event = session.query(ResourceChangeEvent).filter_by(attribute_path="created").one()
        assert event.explanation == "outside_console" and event.operation_id is None


@pytest.mark.parametrize(
    ("operation_kind", "plan"),
    [
        ("drift_resolve", {"choice": "accept", "options": {"choice": "accept"}}),
        ("drift_detect", {}),
        ("inventory_refresh", {}),
    ],
)
def test_an_operation_that_never_changes_aws_does_not_explain_a_change(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
    operation_kind,
    plan,
) -> None:
    """Accepting drift writes only the record, so a change made in AWS meanwhile is outside the console."""
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    _add_operation_with_plan(
        session_factory, operation_kind, group_arn, plan, f"no-aws-{operation_kind}", TARGET_KEY
    )
    boto3.client("rds", region_name="eu-west-2").modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[{"ParameterName": "work_mem", "ParameterValue": "8192", "ApplyMethod": "immediate"}],
    )
    _refresh(session_factory, operator_settings, config_store, f"history-no-aws-{operation_kind}")
    with session_factory() as session:
        event = session.query(ResourceChangeEvent).filter_by(attribute_path="parameter.work_mem").one()
        assert event.explanation == "outside_console" and event.operation_id is None


def test_restoring_drift_explains_the_change_it_makes(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    group_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    restore_id = _add_operation_with_plan(
        session_factory,
        "drift_resolve",
        group_arn,
        {"choice": "restore", "options": {"choice": "restore"}},
        "restore-explains",
        TARGET_KEY,
    )
    boto3.client("rds", region_name="eu-west-2").modify_db_parameter_group(
        DBParameterGroupName="demo-postgres16-tuned",
        Parameters=[{"ParameterName": "work_mem", "ParameterValue": "8192", "ApplyMethod": "immediate"}],
    )
    _refresh(session_factory, operator_settings, config_store, "history-restore")
    with session_factory() as session:
        event = session.query(ResourceChangeEvent).filter_by(attribute_path="parameter.work_mem").one()
        assert event.explanation == "operation" and event.operation_id == restore_id


def test_an_operation_that_finished_before_the_previous_snapshot_does_not_explain_a_change(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    _add_operation(
        session_factory,
        orders_arn,
        "SUCCEEDED",
        "explain-stale",
        started_at_utc=utc_now() - timedelta(hours=2),
        finished_at_utc=utc_now() - timedelta(hours=1),
    )
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=7, ApplyImmediately=True
    )
    _refresh(session_factory, operator_settings, config_store, "history-stale")
    with session_factory() as session:
        event = session.query(ResourceChangeEvent).filter_by(attribute_path="backup_retention_period").one()
        assert event.explanation == "outside_console" and event.operation_id is None


def test_explain_event_attributes_a_failed_operation_overlapping_the_window(
    session_factory, sandbox_snapshot
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    failed_id = _add_operation(
        session_factory,
        orders_arn,
        "FAILED",
        "explain-failed",
        started_at_utc=utc_now() - timedelta(minutes=1),
        finished_at_utc=utc_now(),
    )
    _add_step_attempt(session_factory, failed_id, "apply_direct", "completed")  # it reached AWS
    with session_factory() as session:
        explanation = explain_event(
            session, orders_arn, utc_now() - timedelta(minutes=5), utc_now() + timedelta(minutes=5)
        )
    assert explanation == ("operation", failed_id)


def test_explain_event_attributes_a_running_operation_that_has_not_finished(
    session_factory, sandbox_snapshot
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    running_id = _add_operation(
        session_factory,
        orders_arn,
        "RUNNING",
        "explain-running",
        started_at_utc=utc_now() - timedelta(hours=3),
        finished_at_utc=None,
    )
    with session_factory() as session:
        explanation = explain_event(
            session, orders_arn, utc_now() - timedelta(minutes=5), utc_now() + timedelta(minutes=5)
        )
    assert explanation == ("operation", running_id)


@pytest.mark.parametrize("state", ["QUEUED", "AWAITING_APPROVAL"])
def test_explain_event_ignores_operations_that_have_not_touched_anything(
    session_factory, sandbox_snapshot, state
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    _add_operation(
        session_factory,
        orders_arn,
        state,
        f"explain-{state}",
        started_at_utc=utc_now() - timedelta(minutes=1),
        finished_at_utc=None,
    )
    with session_factory() as session:
        explanation = explain_event(
            session, orders_arn, utc_now() - timedelta(minutes=5), utc_now() + timedelta(minutes=5)
        )
    assert explanation == ("outside_console", None)


def test_explain_event_ignores_an_operation_that_started_after_the_window_closed(
    session_factory, sandbox_snapshot
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    _add_operation(
        session_factory,
        orders_arn,
        "SUCCEEDED",
        "explain-later",
        started_at_utc=utc_now() + timedelta(minutes=10),
        finished_at_utc=utc_now() + timedelta(minutes=11),
    )
    with session_factory() as session:
        explanation = explain_event(
            session, orders_arn, utc_now() - timedelta(minutes=5), utc_now() + timedelta(minutes=5)
        )
    assert explanation == ("outside_console", None)


def test_explain_event_prefers_a_succeeded_operation_then_the_latest_start(
    session_factory, sandbox_snapshot
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    now = utc_now()
    _add_operation(
        session_factory,
        orders_arn,
        "SUCCEEDED",
        "pick-1",
        now - timedelta(minutes=4),
        now - timedelta(minutes=3),
    )
    newer_succeeded_id = _add_operation(
        session_factory,
        orders_arn,
        "SUCCEEDED",
        "pick-2",
        now - timedelta(minutes=2),
        now - timedelta(minutes=1),
    )
    _add_operation(session_factory, orders_arn, "FAILED", "pick-3", now - timedelta(seconds=30), now)
    _add_operation(session_factory, orders_arn, "RUNNING", "pick-4", now - timedelta(seconds=10), None)
    window = (now - timedelta(minutes=5), now + timedelta(minutes=5))
    with session_factory() as session:
        assert explain_event(session, orders_arn, *window) == ("operation", newer_succeeded_id)


@pytest.mark.parametrize(
    "closing_refresh_output",
    [None, {"refreshed": False, "error": "RuntimeError: the RDS endpoint could not be reached"}],
)
def test_explain_event_ends_an_operations_window_at_its_closing_snapshot_and_otherwise_at_its_finish(
    session_factory, operator_settings, config_store, sandbox_snapshot, moto_aws, closing_refresh_output
) -> None:
    """The closing refresh captures a little before the operation finishes; that capture ends its window.

    The window up to the closing snapshot is the operation's, including one closed by a plain refresh that
    ran while it was still changing AWS. A change first seen after the closing snapshot is not. An operation
    whose closing refresh took no snapshot (none ran, or it failed) keeps the window that ends when it
    finished.
    """
    _refresh(session_factory, operator_settings, config_store, "history-closing-snapshot")
    with session_factory() as session:
        closing_snapshot = latest_snapshot_for_target(session, TARGET_KEY)
        closing_snapshot_id, closing_captured_at = closing_snapshot.id, closing_snapshot.captured_at_utc
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    replica_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders-replica")
    started_at, finished_at = (
        closing_captured_at - timedelta(minutes=1),
        closing_captured_at + timedelta(milliseconds=13),
    )  # the smoke's timing: finished 13 ms after the closing capture
    closed_id = _add_operation(session_factory, orders_arn, "SUCCEEDED", "closed", started_at, finished_at)
    _add_step_attempt(
        session_factory,
        closed_id,
        "refresh_resource",
        "completed",
        {"refreshed": True, "snapshot_id": closing_snapshot_id},
    )
    unclosed_id = _add_operation(
        session_factory, replica_arn, "SUCCEEDED", "unclosed", started_at, finished_at
    )
    if closing_refresh_output is not None:
        _add_step_attempt(
            session_factory, unclosed_id, "refresh_resource", "completed", closing_refresh_output
        )
    mid_operation_window = (
        closing_captured_at - timedelta(seconds=30),
        closing_captured_at - timedelta(seconds=10),
    )
    closing_window = (closing_captured_at - timedelta(seconds=30), closing_captured_at)
    after_closing_window = (closing_captured_at, closing_captured_at + timedelta(seconds=1))
    with session_factory() as session:
        assert explain_event(session, orders_arn, *mid_operation_window) == ("operation", closed_id)
        assert explain_event(session, orders_arn, *closing_window) == ("operation", closed_id)
        assert explain_event(session, orders_arn, *after_closing_window) == ("outside_console", None)
        assert explain_event(session, replica_arn, *after_closing_window) == ("operation", unclosed_id)


def test_the_refresh_step_reports_how_many_change_events_it_wrote(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", DeletionProtection=True, ApplyImmediately=True
    )
    operation = _refresh(session_factory, operator_settings, config_store, "history-count")
    with session_factory() as session:
        attempt = (
            session.query(OperationStepAttempt)
            .filter_by(operation_id=operation.id, outcome_status="completed")
            .one()
        )
        assert attempt.output["change_event_count"] == 1


def test_resources_in_snapshot_flattens_attributes_and_user_set_parameters(
    session_factory, sandbox_snapshot
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    aurora_arn = arn_for(session_factory, sandbox_snapshot, "demo-aurora")
    tuned_arn = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        resources = resources_in_snapshot(session, sandbox_snapshot.id)
        automated_snapshot_arns = {
            row.db_snapshot_arn
            for row in session.query(InventoryDbSnapshot).filter_by(
                snapshot_id=sandbox_snapshot.id, snapshot_type="automated"
            )
        }
    assert resources[orders_arn]["db_instance_class"] == "db.m6g.large"
    assert not {"db_instance_status", "status"} & set(resources[orders_arn])
    assert "status" not in resources[aurora_arn] and resources[aurora_arn]["ownership"]
    assert automated_snapshot_arns and not automated_snapshot_arns & set(resources)
    assert resources[orders_arn]["deletion_protection"] is False
    assert resources[tuned_arn]["parameter.work_mem"] == "16384"
    assert resources[tuned_arn]["parameter.log_min_duration_statement"] == "500"
    assert resources[tuned_arn]["parameter_group_family"] == "postgres16"


def test_compute_change_events_without_a_previous_snapshot_writes_nothing(
    session_factory, console_config, sandbox_snapshot
) -> None:
    target = console_config.target(TARGET_KEY)
    with session_factory() as session:
        assert compute_change_events(session, target, None, sandbox_snapshot.id) == []


def test_review_marks_an_outside_change(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", DeletionProtection=True, ApplyImmediately=True
    )
    _refresh(session_factory, operator_settings, config_store, "history-3")
    with session_factory() as session:
        event = session.query(ResourceChangeEvent).filter_by(attribute_path="deletion_protection").one()
        reviewed = review_change_event(session, event.id, "sam")
        session.commit()
        assert reviewed.reviewed_by_operator_name == "sam" and reviewed.reviewed_at_utc is not None
        assert (reviewed.value_before, reviewed.value_after) == (False, True)


def test_review_of_an_unknown_event_raises_key_error(session_factory) -> None:
    with session_factory() as session, pytest.raises(KeyError, match="999"):
        review_change_event(session, 999, "sam")


def test_recent_change_events_for_returns_newest_first_and_honours_the_limit(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders",
        DeletionProtection=True,
        BackupRetentionPeriod=7,
        ApplyImmediately=True,
    )
    _refresh(session_factory, operator_settings, config_store, "history-recent")
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session:
        events = recent_change_events_for(session, orders_arn)
        limited = recent_change_events_for(session, orders_arn, limit=1)
        elsewhere = recent_change_events_for(session, orders_arn + "-other")
    assert {e.attribute_path for e in events} == {"deletion_protection", "backup_retention_period"}
    assert [e.id for e in events] == sorted((e.id for e in events), reverse=True)
    assert [e.id for e in limited] == [events[0].id] and elsewhere == []


def test_latest_snapshot_is_compared_with_the_one_before_it_each_refresh(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_instance(DBInstanceIdentifier="demo-orders", DeletionProtection=True, ApplyImmediately=True)
    _refresh(session_factory, operator_settings, config_store, "history-a")
    _refresh(session_factory, operator_settings, config_store, "history-b")
    with session_factory() as session:
        latest = latest_snapshot_for_target(session, TARGET_KEY)
        events = session.query(ResourceChangeEvent).all()
    assert len(events) == 1 and events[0].snapshot_after_id != latest.id


def _add_step_attempt(
    session_factory, operation_id, step_name: str, outcome_status: str, output: dict | None = None
) -> None:
    """Record one finished attempt of `step_name` for the operation, with `output` when given."""
    with session_factory() as session:
        session.add(
            OperationStepAttempt(
                operation_id=operation_id,
                step_name=step_name,
                attempt_number=1,
                observation_state="ABSENT",
                outcome_status=outcome_status,
                output=output or {},
                finished_at_utc=utc_now(),
            )
        )
        session.commit()


def test_a_failed_operation_that_never_reached_aws_does_not_explain_an_outside_change(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    refused_id = _add_operation(
        session_factory,
        orders_arn,
        "FAILED",
        "stale-guard",
        started_at_utc=utc_now() - timedelta(minutes=1),
        finished_at_utc=utc_now(),
    )
    _add_step_attempt(session_factory, refused_id, "check_checkout", "completed")
    _add_step_attempt(
        session_factory, refused_id, "compute_change_set", "failed"
    )  # the stale-inventory guard
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders", BackupRetentionPeriod=9, ApplyImmediately=True
    )
    _refresh(session_factory, operator_settings, config_store, "history-stale-guard")
    with session_factory() as session:
        event = session.query(ResourceChangeEvent).filter_by(attribute_path="backup_retention_period").one()
        assert event.explanation == "outside_console" and event.operation_id is None


def _mark_terraform_apply_invoked(session_factory, operation_id) -> None:
    """Mark the operation as the Terraform strategy does just before the binary's apply runs."""
    with session_factory() as session:
        operation = session.get(Operation, operation_id)
        operation.plan = {**operation.plan, "terraform_apply_invoked": True}
        session.commit()


def _add_terraform_run(session_factory, operation_id, run_kind: str) -> None:
    """Record one Terraform invocation of `run_kind` for the operation, in a workspace of its own."""
    with session_factory() as session:
        workspace = TerraformWorkspace(
            target_key=TARGET_KEY,
            workspace_relative_dir=f"terraform/sandbox/{operation_id}",
            state_bucket_name="state-bucket",
            state_key="terraform.tfstate",
        )
        session.add(workspace)
        session.flush()
        session.add(
            TerraformRun(workspace_id=workspace.id, operation_id=operation_id, run_kind=run_kind, exit_code=1)
        )
        session.commit()


def _cancel(session_factory, operation_id) -> None:
    """Take an operation out of the running so the next one is judged on its own."""
    with session_factory() as session:
        session.get(Operation, operation_id).state = "CANCELLED"
        session.commit()


def test_a_failed_operation_explains_a_change_once_it_applied_or_was_applied_without_a_record(
    session_factory, sandbox_snapshot
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    window = (utc_now() - timedelta(minutes=5), utc_now() + timedelta(minutes=5))
    applied_id = _add_operation(
        session_factory,
        orders_arn,
        "FAILED",
        "failed-after-apply",
        utc_now() - timedelta(minutes=2),
        utc_now(),
    )
    _add_step_attempt(session_factory, applied_id, "apply", "completed")
    _mark_terraform_apply_invoked(session_factory, applied_id)
    with session_factory() as session:
        assert explain_event(session, orders_arn, *window) == ("operation", applied_id)
    _cancel(session_factory, applied_id)
    unrecorded_id = _add_operation(
        session_factory,
        orders_arn,
        "FAILED",
        "failed-unrecorded",
        utc_now() - timedelta(minutes=1),
        utc_now(),
    )
    with session_factory() as session:
        assert explain_event(session, orders_arn, *window) == ("outside_console", None)
        unrecorded = session.get(Operation, unrecorded_id)
        unrecorded.plan = {"applied_without_record": True}
        session.commit()
        assert explain_event(session, orders_arn, *window) == ("operation", unrecorded_id)


def test_a_failed_operation_whose_apply_started_explains_a_change_even_when_the_apply_failed_part_way(
    session_factory, sandbox_snapshot
) -> None:
    """An apply that failed half-way may have changed AWS; those changes are the console's own.

    The direct strategy's API calls count once their step started. Terraform's apply counts once the binary
    was invoked: the operation is marked just before the call, and the run is recorded when it returns.
    """
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    window = (utc_now() - timedelta(minutes=5), utc_now() + timedelta(minutes=5))
    for evidence in ("apply_direct_attempt", "terraform_apply_marked", "terraform_apply_run"):
        failed_id = _add_operation(
            session_factory,
            orders_arn,
            "FAILED",
            f"failed-part-way-{evidence}",
            utc_now() - timedelta(minutes=2),
            utc_now(),
        )
        if evidence == "apply_direct_attempt":
            _add_step_attempt(session_factory, failed_id, "apply_direct", "failed")
        else:
            _add_step_attempt(session_factory, failed_id, "apply", "failed")
        if evidence == "terraform_apply_marked":
            _mark_terraform_apply_invoked(session_factory, failed_id)
        if evidence == "terraform_apply_run":
            _add_terraform_run(session_factory, failed_id, "apply")
        with session_factory() as session:
            assert explain_event(session, orders_arn, *window) == ("operation", failed_id), evidence
        _cancel(session_factory, failed_id)


def test_a_terraform_operation_refused_at_its_pre_apply_plan_does_not_explain_an_outside_change(
    session_factory, sandbox_snapshot
) -> None:
    """The `apply` step plans first; a refusal there comes before the binary's apply, so nothing changed.

    The refusal is usually caused by the very change in the window, which therefore stays an outside change.
    """
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    window = (utc_now() - timedelta(minutes=5), utc_now() + timedelta(minutes=5))
    refused_id = _add_operation(
        session_factory,
        orders_arn,
        "FAILED",
        "refused-at-the-pre-apply-plan",
        utc_now() - timedelta(minutes=2),
        utc_now(),
    )
    _add_step_attempt(session_factory, refused_id, "await_approval", "completed")
    _add_step_attempt(session_factory, refused_id, "apply", "failed")  # plan_changed_since_approval
    _add_terraform_run(session_factory, refused_id, "plan")  # the pre-apply plan itself
    with session_factory() as session:
        assert explain_event(session, orders_arn, *window) == ("outside_console", None)


def _add_queued_class_change(session_factory, orders_arn: str, desired_class: str, key: str):
    """A direct-mode class change that succeeded two days ago by queueing the class for the window."""
    with session_factory() as session:
        operation = Operation(
            operation_kind="instance_class_change",
            state="SUCCEEDED",
            environment_slug="sandbox",
            target_key=TARGET_KEY,
            plan={
                "touched_addresses": ["aws_db_instance.demo_orders"],
                "change_set": [
                    {
                        "address": "aws_db_instance.demo_orders",
                        "attribute": "instance_class",
                        "current": "db.m6g.large",
                        "desired": desired_class,
                        "apply_type": "pending-window",
                        "requires_reboot": False,
                        "downtime": "none now; AWS applies it in the next maintenance window",
                    }
                ],
            },
            idempotency_key=key,
            created_by_operator_name="phil",
            resource_arn=orders_arn,
            title="x",
            started_at_utc=utc_now() - timedelta(days=2, minutes=1),
            finished_at_utc=utc_now() - timedelta(days=2),
        )
        session.add(operation)
        session.commit()
        return operation.id


def test_a_change_queued_for_the_window_is_explained_by_its_operation_when_aws_applies_it_days_later(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    queued_id = _add_queued_class_change(session_factory, orders_arn, "db.r6g.xlarge", "queued-class")
    boto3.client("rds", region_name="eu-west-2").modify_db_instance(
        DBInstanceIdentifier="demo-orders",
        DBInstanceClass="db.r6g.xlarge",
        BackupRetentionPeriod=9,
        ApplyImmediately=True,
    )  # what AWS does in the maintenance window, plus an unrelated outside change
    _refresh(session_factory, operator_settings, config_store, "history-queued-applied")
    by_attribute = {e.attribute_path: e for e in _all_events(session_factory)}
    assert by_attribute["db_instance_class"].explanation == "operation"
    assert by_attribute["db_instance_class"].operation_id == queued_id
    assert by_attribute["backup_retention_period"].explanation == "outside_console"


def test_a_value_other_than_the_queued_one_is_an_outside_change(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    _add_queued_class_change(session_factory, orders_arn, "db.r6g.xlarge", "queued-other")
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.modify_db_instance(
        DBInstanceIdentifier="demo-orders", DBInstanceClass="db.r6g.2xlarge", ApplyImmediately=True
    )
    _refresh(session_factory, operator_settings, config_store, "history-queued-other")
    [event] = [e for e in _all_events(session_factory) if e.attribute_path == "db_instance_class"]
    assert event.explanation == "outside_console" and event.operation_id is None


def test_the_queued_value_seen_again_after_another_change_is_an_outside_change(
    session_factory, operator_settings, config_store, console_config, sandbox_snapshot, moto_aws
) -> None:
    """Only the first change of the attribute observed after the operation can be AWS applying its queue."""
    orders_arn = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    _add_queued_class_change(session_factory, orders_arn, "db.r6g.xlarge", "queued-again")
    rds = boto3.client("rds", region_name="eu-west-2")
    for refresh_key, instance_class in (
        ("history-queued-1", "db.r6g.xlarge"),
        ("history-queued-2", "db.m6g.large"),
        ("history-queued-3", "db.r6g.xlarge"),
    ):
        rds.modify_db_instance(
            DBInstanceIdentifier="demo-orders", DBInstanceClass=instance_class, ApplyImmediately=True
        )
        _refresh(session_factory, operator_settings, config_store, refresh_key)
    explanations = [
        e.explanation for e in _all_events(session_factory) if e.attribute_path == "db_instance_class"
    ]
    assert explanations == ["operation", "outside_console", "outside_console"]
