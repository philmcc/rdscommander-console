"""Keep the control database from growing without bound: prune old inventory snapshots and execution records.

The scheduler refreshes every target every `inventory_refresh_minutes` (15 by default), and each refresh
stores every instance, cluster, subnet group, DB snapshot and parameter group of the target, with every
parameter of every group, plus an execution record per AWS call. So after each refresh of a target:

* its inventory snapshots beyond the newest `inventory_snapshot_retention_per_target` are deleted with
  every row that hangs off them, except the snapshots something still points at: a change event (the
  snapshots before and after a change), a drift finding (the snapshot the detection compared), or an
  operation (the snapshot a Terraform-mode change rendered its files from);
* execution records older than `execution_record_retention_days` are deleted when no retained operation
  owns them: records of no operation, and of finished inventory refreshes and drift detections, which the
  scheduler repeats. Every other operation keeps its records for good, and so does a record a Terraform
  run points at.

Example:
    >>> prune_inventory_snapshots(session, "sandbox/000000000000/eu-west-2", retention=96)  # doctest: +SKIP
    {'pruned_snapshot_count': 3, ...}
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import Delete, delete, exists, or_, select
from sqlalchemy.orm import Session

from rdscommander.control_db.models import (
    DriftFinding,
    ExecutionRecord,
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryDbSnapshot,
    InventoryParameterGroup,
    InventoryParameterValue,
    InventoryPendingMaintenance,
    InventorySnapshot,
    InventorySubnetGroup,
    Operation,
    OperationStepAttempt,
    ResourceChangeEvent,
    TerraformRun,
    utc_now,
)
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import OperationState, StepOutcomeStatus

logger = get_logger(__name__)

RENDER_SNAPSHOT_PLAN_KEY = "render_snapshot_id"
DETECT_STEP_PREFIX = "detect:"
REPEATED_READ_ONLY_OPERATION_KINDS = ("inventory_refresh", "drift_detect")
"""Operations the scheduler repeats; once finished, their execution records age out."""
FINISHED_STATES = (
    OperationState.SUCCEEDED.value,
    OperationState.FAILED.value,
    OperationState.CANCELLED.value,
)


def _snapshots_still_referenced(session: Session, target_key: str) -> set[int]:
    """Snapshot ids of `target_key` that a change event, a drift finding or an operation points at."""
    referenced: set[int] = set()
    for before_id, after_id in session.execute(
        select(ResourceChangeEvent.snapshot_before_id, ResourceChangeEvent.snapshot_after_id).where(
            ResourceChangeEvent.target_key == target_key
        )
    ):
        referenced.update(snapshot_id for snapshot_id in (before_id, after_id) if snapshot_id is not None)
    for output in session.execute(
        select(OperationStepAttempt.output)
        .join(DriftFinding, DriftFinding.detected_by_operation_id == OperationStepAttempt.operation_id)
        .where(
            DriftFinding.target_key == target_key,
            OperationStepAttempt.step_name == f"{DETECT_STEP_PREFIX}{target_key}",
            OperationStepAttempt.outcome_status == StepOutcomeStatus.COMPLETED.value,
        )
    ).scalars():
        if isinstance(output, dict) and isinstance(output.get("snapshot_id"), int):
            referenced.add(output["snapshot_id"])
    for plan in session.execute(
        select(Operation.plan).where(
            Operation.target_key == target_key, Operation.plan.has_key(RENDER_SNAPSHOT_PLAN_KEY)
        )
    ).scalars():
        if isinstance(plan.get(RENDER_SNAPSHOT_PLAN_KEY), int):
            referenced.add(plan[RENDER_SNAPSHOT_PLAN_KEY])
    return referenced


def _deleted(session: Session, statement: Delete) -> int:
    """Run a delete and return how many rows it removed."""
    return int(session.execute(statement).rowcount or 0)  # type: ignore[attr-defined]


def prune_inventory_snapshots(session: Session, target_key: str, retention: int) -> dict[str, int]:
    """Delete the target's snapshots beyond the newest `retention` that nothing points at; commits.

    Args:
        session: Control database session.
        target_key: The target just refreshed.
        retention: How many of the newest snapshots to keep whatever points at them (at least 1).

    Returns:
        Counts: snapshots pruned and retained, and the rows deleted per table.

    Example:
        >>> prune_inventory_snapshots(session, target_key, 96)["pruned_snapshot_count"]  # doctest: +SKIP
        0
    """
    newest_first = list(
        session.execute(
            select(InventorySnapshot.id)
            .where(InventorySnapshot.target_key == target_key)
            .order_by(InventorySnapshot.id.desc())
        ).scalars()
    )
    beyond_retention = newest_first[max(retention, 1) :]
    prunable = sorted(set(beyond_retention) - _snapshots_still_referenced(session, target_key))
    counts = {
        "pruned_snapshot_count": len(prunable),
        "retained_snapshot_count": len(newest_first) - len(prunable),
        "pruned_parameter_value_count": 0,
        "pruned_resource_row_count": 0,
    }
    if not prunable:
        return counts
    group_ids = select(InventoryParameterGroup.id).where(InventoryParameterGroup.snapshot_id.in_(prunable))
    counts["pruned_parameter_value_count"] = _deleted(
        session,
        delete(InventoryParameterValue).where(InventoryParameterValue.parameter_group_id.in_(group_ids)),
    )
    for model in (
        InventoryParameterGroup,
        InventoryDbInstance,
        InventoryDbCluster,
        InventorySubnetGroup,
        InventoryDbSnapshot,
        InventoryPendingMaintenance,
    ):
        counts["pruned_resource_row_count"] += _deleted(
            session,
            delete(model).where(model.snapshot_id.in_(prunable)),  # type: ignore[attr-defined]
        )
    _deleted(session, delete(InventorySnapshot).where(InventorySnapshot.id.in_(prunable)))
    session.commit()
    logger.info(
        "inventory_snapshots_pruned",
        target_key=target_key,
        retention=retention,
        pruned_snapshot_ids=prunable,
        **counts,
    )
    return counts


def prune_execution_records(session: Session, retention_days: int) -> int:
    """Delete execution records older than `retention_days` that no retained operation owns; commits.

    Args:
        session: Control database session.
        retention_days: Age in days beyond which such a record goes.

    Returns:
        How many records were deleted.

    Example:
        >>> prune_execution_records(session, 30)  # doctest: +SKIP
        1520
    """
    cutoff = utc_now() - timedelta(days=retention_days)
    repeated_and_finished = select(Operation.id).where(
        Operation.operation_kind.in_(REPEATED_READ_ONLY_OPERATION_KINDS), Operation.state.in_(FINISHED_STATES)
    )
    pruned = _deleted(
        session,
        delete(ExecutionRecord).where(
            ExecutionRecord.started_at_utc < cutoff,
            or_(
                ExecutionRecord.operation_id.is_(None),
                ExecutionRecord.operation_id.in_(repeated_and_finished),
            ),
            ~exists().where(TerraformRun.execution_record_id == ExecutionRecord.id),
        ),
    )
    session.commit()
    if pruned:
        logger.info(
            "execution_records_pruned",
            retention_days=retention_days,
            cutoff_utc=cutoff.isoformat(),
            pruned_execution_record_count=pruned,
        )
    return pruned
