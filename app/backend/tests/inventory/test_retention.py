"""Retention of inventory snapshots and execution records after each refresh."""

from datetime import timedelta

import boto3
from structlog.testing import capture_logs

from rdscommander.control_db.models import (
    DriftFinding,
    ExecutionRecord,
    InventoryDbInstance,
    InventoryParameterGroup,
    InventoryParameterValue,
    InventorySnapshot,
    Operation,
    OperationStepAttempt,
    ResourceChangeEvent,
    TerraformRun,
    TerraformWorkspace,
    utc_now,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.main import build_executors, build_services
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService
from tests.terraform.conftest import TARGET_KEY


def _refresher(session_factory, settings, config_store):
    services = build_services(settings, session_factory)
    runner = OperationRunner(
        session_factory, build_executors(services, config_store), "r", settings, config_store, services
    )
    service = OperationService(session_factory)

    def refresh(key: str) -> int:
        service.create("inventory_refresh", "sandbox", None, {}, key, "phil")
        runner.run_once()
        with session_factory() as session:
            return latest_snapshot_for_target(session, TARGET_KEY).id

    return refresh


def _finished_operation(session, kind: str, plan: dict | None = None) -> Operation:
    operation = Operation(
        operation_kind=kind,
        state="SUCCEEDED",
        environment_slug="sandbox",
        target_key=TARGET_KEY,
        title=kind,
        plan=plan or {},
        idempotency_key=f"retention-{kind}-{utc_now().timestamp()}",
        created_by_operator_name="phil",
    )
    session.add(operation)
    session.flush()
    return operation


def _snapshot_ids(session_factory) -> list[int]:
    with session_factory() as session:
        return sorted(row.id for row in session.query(InventorySnapshot).filter_by(target_key=TARGET_KEY))


def test_a_refresh_prunes_snapshots_beyond_the_retention_except_those_still_referenced(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
) -> None:
    """Change events, drift findings and operations keep the snapshots they point at; the rest beyond the
    newest `inventory_snapshot_retention_per_target` go, with every row that hangs off them."""
    settings = operator_settings.model_copy(update={"inventory_snapshot_retention_per_target": 2})
    refresh = _refresher(session_factory, settings, config_store)
    unreferenced = sandbox_snapshot.id
    with capture_logs() as logs:
        quiet = refresh("quiet")
        boto3.client("rds", region_name="eu-west-2").modify_db_parameter_group(
            DBParameterGroupName="demo-postgres16-tuned",
            Parameters=[{"ParameterName": "work_mem", "ParameterValue": "65536", "ApplyMethod": "immediate"}],
        )
        changed = refresh("changed")
        with session_factory() as session:
            assert session.query(ResourceChangeEvent).filter_by(snapshot_after_id=changed).count() > 0
        rendered_from = refresh("rendered-from")
        with session_factory() as session:
            _finished_operation(session, "parameter_group_edit", {"render_snapshot_id": rendered_from})
            session.commit()
        detected_on = refresh("detected-on")
        with session_factory() as session:
            detection = _finished_operation(session, "drift_detect")
            session.add(
                OperationStepAttempt(
                    operation_id=detection.id,
                    step_name=f"detect:{TARGET_KEY}",
                    attempt_number=1,
                    observation_state="ABSENT",
                    outcome_status="completed",
                    output={"snapshot_id": detected_on},
                )
            )
            session.add(
                DriftFinding(
                    resource_arn="arn:aws:rds:eu-west-2:123456789012:pg:demo-postgres16-tuned",
                    environment_slug="sandbox",
                    target_key=TARGET_KEY,
                    finding_kind="record",
                    attributes=[],
                    detected_by_operation_id=detection.id,
                )
            )
            session.commit()
        newer = refresh("newer")
        newest = refresh("newest")
    kept = {quiet, changed, rendered_from, detected_on, newer, newest}
    assert set(_snapshot_ids(session_factory)) == kept, (
        "only the unreferenced snapshot beyond the newest two goes"
    )
    assert unreferenced not in kept
    with session_factory() as session:
        assert session.query(InventoryDbInstance).filter_by(snapshot_id=unreferenced).count() == 0
        assert session.query(InventoryParameterGroup).filter_by(snapshot_id=unreferenced).count() == 0
        assert (
            session.query(InventoryParameterValue)
            .join(InventoryParameterGroup)
            .filter(InventoryParameterGroup.snapshot_id == unreferenced)
            .count()
            == 0
        )
    [pruned] = [entry for entry in logs if entry["event"] == "inventory_snapshots_pruned"]
    assert pruned["target_key"] == TARGET_KEY and pruned["pruned_snapshot_count"] == 1
    assert pruned["pruned_parameter_value_count"] > 0 and pruned["pruned_snapshot_ids"] == [unreferenced]


def test_a_refresh_prunes_old_execution_records_that_no_retained_operation_owns(
    session_factory,
    operator_settings,
    config_store,
    console_config,
    sandbox_snapshot,
    moto_aws,
) -> None:
    """Old records of finished refreshes and detections, and of no operation, go; a change keeps its own."""
    refresh = _refresher(session_factory, operator_settings, config_store)
    old = utc_now() - timedelta(days=operator_settings.execution_record_retention_days + 1)

    def record(session, operation_id, started_at_utc) -> int:
        execution_record = ExecutionRecord(
            operation_id=operation_id,
            record_kind="aws_api",
            api_name="rds:DescribeDBInstances",
            succeeded=True,
            started_at_utc=started_at_utc,
            duration_milliseconds=1,
        )
        session.add(execution_record)
        session.flush()
        return execution_record.id

    with session_factory() as session:
        refresh_operation_id = session.get(InventorySnapshot, sandbox_snapshot.id).operation_id
        detection = _finished_operation(session, "drift_detect")
        change = _finished_operation(session, "parameter_group_edit")
        running_refresh = _finished_operation(session, "inventory_refresh")
        running_refresh.state = "RUNNING"
        workspace = TerraformWorkspace(
            target_key=TARGET_KEY,
            workspace_relative_dir="terraform/sandbox/000000000000/eu-west-2",
            state_bucket_name="state",
            state_key="key",
        )
        session.add(workspace)
        session.flush()
        pruned_ids = {
            record(session, refresh_operation_id, old),
            record(session, detection.id, old),
            record(session, None, old),
        }
        kept_ids = {
            record(session, change.id, old),
            record(session, running_refresh.id, old),
            record(session, refresh_operation_id, utc_now()),
        }
        planned = record(session, detection.id, old)
        session.add(
            TerraformRun(workspace_id=workspace.id, run_kind="plan", exit_code=0, execution_record_id=planned)
        )
        kept_ids.add(planned)
        session.commit()
    with capture_logs() as logs:
        refresh("prune-records")
    with session_factory() as session:
        remaining = {row.id for row in session.query(ExecutionRecord)}
    assert kept_ids <= remaining and not pruned_ids & remaining
    [pruned] = [entry for entry in logs if entry["event"] == "execution_records_pruned"]
    assert pruned["pruned_execution_record_count"] >= len(pruned_ids)
    assert pruned["retention_days"] == operator_settings.execution_record_retention_days
