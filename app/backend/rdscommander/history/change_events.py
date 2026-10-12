"""Diff consecutive snapshots per resource and persist attribute-level events.

Change events are derived data: they are written once from a pair of inventory snapshots and never
edited afterwards. The only later mutation is a review, which sets who looked at an outside-console
change and when.

Two kinds of AWS housekeeping are kept out of the history so they cannot flood the outside-console
queue: automated snapshots (they appear and expire on AWS's schedule) and lifecycle statuses such as
`modifying` or `backing-up` (they flip between refreshes). A resource's status is still recorded as the
value of its `created` and `removed` events.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import and_, case, exists, or_, select
from sqlalchemy.orm import Session

from rdscommander.config.models import ResolvedTarget
from rdscommander.control_db.models import (
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryDbSnapshot,
    InventoryParameterGroup,
    InventoryParameterValue,
    InventorySnapshot,
    Operation,
    OperationStepAttempt,
    ResourceChangeEvent,
    TerraformRun,
    utc_now,
)
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import SECOND_PHASE_STEP_PREFIX, ObservationState, OperationState
from rdscommander.terraform.names import terraform_address_for
from rdscommander.terraform.runner import TERRAFORM_APPLY_INVOKED_PLAN_KEY

logger = get_logger(__name__)

EXPLANATION_OPERATION = "operation"
EXPLANATION_OUTSIDE_CONSOLE = "outside_console"
ATTRIBUTE_PATH_CREATED = "created"
ATTRIBUTE_PATH_REMOVED = "removed"
MANUAL_SNAPSHOT_TYPE = "manual"
EXPLAINING_OPERATION_STATES = (
    OperationState.SUCCEEDED.value,
    OperationState.FAILED.value,
    OperationState.RUNNING.value,
)
CHANGED_AWS_BEFORE_PAUSE_PLAN_KEY = "changed_aws_before_pause"
"""Plan key, true once an operation changed AWS before a later approval (a major upgrade's green side).

Such an operation explains what it created (`plan["created_resource_arns"]` only, never the resource it
acts on) while it waits for that approval (`AWAITING_APPROVAL`, or `QUEUED` once approved) and after it was
rejected (`CANCELLED`), as well as everything it targets in the usual states.
"""
PAUSED_OR_CANCELLED_STATES = (
    OperationState.AWAITING_APPROVAL.value,
    OperationState.QUEUED.value,
    OperationState.CANCELLED.value,
)
OPERATION_KINDS_THAT_NEVER_CHANGE_AWS = ("inventory_refresh", "drift_detect", "environment_import")
"""Kinds that only read AWS. Accepting drift (`drift_resolve`, `choice: accept`) writes only the record."""
DIRECT_APPLY_STEP_NAME = "apply_direct"
"""The direct strategy's step that calls the AWS API: once it has started, AWS may have changed."""
TERRAFORM_APPLY_RUN_KIND = "apply"
"""The `terraform_runs` kind recorded once the binary's apply has returned, however it ended."""
CLOSING_REFRESH_STEP_NAME = "refresh_resource"
"""The step that ends a change or an adoption with a new snapshot; its output names it as `snapshot_id`."""
CLOSING_REFRESH_SNAPSHOT_OUTPUT_KEY = "snapshot_id"
"""The key of the closing refresh step's output that holds the id of the snapshot it took."""
CLOSING_REFRESH_STEP_NAMES = (
    CLOSING_REFRESH_STEP_NAME,
    f"{SECOND_PHASE_STEP_PREFIX}{CLOSING_REFRESH_STEP_NAME}",
)
"""The closing refresh of a change, and of a chained operation's second change (after the first's)."""

INSTANCE_RAW_ATTRIBUTE_KEYS: dict[str, str] = {
    "engine_version": "EngineVersion",
    "db_instance_class": "DBInstanceClass",
    "allocated_storage_gb": "AllocatedStorage",
    "max_allocated_storage_gb": "MaxAllocatedStorage",
    "storage_type": "StorageType",
    "iops": "Iops",
    "multi_az": "MultiAZ",
    "backup_retention_period": "BackupRetentionPeriod",
    "preferred_backup_window": "PreferredBackupWindow",
    "preferred_maintenance_window": "PreferredMaintenanceWindow",
    "deletion_protection": "DeletionProtection",
    "performance_insights_enabled": "PerformanceInsightsEnabled",
    "monitoring_interval": "MonitoringInterval",
    "auto_minor_version_upgrade": "AutoMinorVersionUpgrade",
    "publicly_accessible": "PubliclyAccessible",
}
CLUSTER_RAW_ATTRIBUTE_KEYS: dict[str, str] = {
    "engine_version": "EngineVersion",
    "backup_retention_period": "BackupRetentionPeriod",
    "preferred_backup_window": "PreferredBackupWindow",
    "preferred_maintenance_window": "PreferredMaintenanceWindow",
    "deletion_protection": "DeletionProtection",
}
QUEUED_FOR_WINDOW_APPLY_TYPE = "pending-window"
"""The change set apply type of a value AWS holds until the maintenance window (`apply_at: window`)."""
CHANGE_SET_ATTRIBUTE_BY_HISTORY_ATTRIBUTE: dict[str, str] = {
    **{attribute_name: attribute_name for attribute_name in INSTANCE_RAW_ATTRIBUTE_KEYS},
    **{attribute_name: attribute_name for attribute_name in CLUSTER_RAW_ATTRIBUTE_KEYS},
    "db_instance_class": "instance_class",
    "allocated_storage_gb": "allocated_storage",
    "max_allocated_storage_gb": "max_allocated_storage",
}
"""History attribute name to the attribute an operation's change set names, for queued-change matching."""

InventoryRow = InventoryDbInstance | InventoryDbCluster | InventoryParameterGroup | InventoryDbSnapshot


def _tag_attributes(tags: dict[str, Any]) -> dict[str, Any]:
    """Tags as `tag.<key>` attributes."""
    return {f"tag.{tag_key}": tag_value for tag_key, tag_value in tags.items()}


def _instance_attributes(instance_row: InventoryDbInstance) -> dict[str, Any]:
    """Flat attributes of one database instance row."""
    raw_description = instance_row.raw_description
    attributes: dict[str, Any] = {
        attribute_name: raw_description.get(raw_key)
        for attribute_name, raw_key in INSTANCE_RAW_ATTRIBUTE_KEYS.items()
    }
    attributes.update(
        {
            "parameter_group_name": instance_row.parameter_group_name,
            "db_subnet_group_name": instance_row.db_subnet_group_name,
            "ownership": instance_row.ownership,
        }
    )
    attributes.update(_tag_attributes(instance_row.tags))
    return attributes


def _cluster_attributes(cluster_row: InventoryDbCluster) -> dict[str, Any]:
    """Flat attributes of one database cluster row."""
    raw_description = cluster_row.raw_description
    attributes: dict[str, Any] = {
        attribute_name: raw_description.get(raw_key)
        for attribute_name, raw_key in CLUSTER_RAW_ATTRIBUTE_KEYS.items()
    }
    attributes.update(
        {
            "cluster_parameter_group_name": cluster_row.cluster_parameter_group_name,
            "member_identifiers": sorted(str(member) for member in cluster_row.member_instance_identifiers),
            "ownership": cluster_row.ownership,
        }
    )
    attributes.update(_tag_attributes(cluster_row.tags))
    return attributes


def _parameter_group_attributes(
    parameter_group_row: InventoryParameterGroup, parameter_values: Iterable[InventoryParameterValue]
) -> dict[str, Any]:
    """Flat attributes of one parameter group row; only user-set parameters count, as `parameter.<name>`."""
    attributes: dict[str, Any] = {
        "description": parameter_group_row.description,
        "parameter_group_family": parameter_group_row.parameter_group_family,
        "ownership": parameter_group_row.ownership,
    }
    attributes.update(
        {
            f"parameter.{parameter_value.parameter_name}": parameter_value.parameter_value
            for parameter_value in parameter_values
            if parameter_value.source == "user"
        }
    )
    attributes.update(_tag_attributes(parameter_group_row.tags))
    return attributes


def _database_snapshot_attributes(database_snapshot_row: InventoryDbSnapshot) -> dict[str, Any]:
    """Flat attributes of one database or cluster snapshot row."""
    return {
        "snapshot_type": database_snapshot_row.snapshot_type,
        "engine_version": database_snapshot_row.engine_version,
        **_tag_attributes(database_snapshot_row.tags),
    }


def resource_attributes(
    record_row: InventoryRow, parameter_values: Iterable[InventoryParameterValue] = ()
) -> dict[str, Any]:
    """Flat attribute map of one inventory row, the unit that snapshots are compared in.

    Args:
        record_row: An instance, cluster, parameter group or database snapshot row.
        parameter_values: The parameter values of `record_row` when it is a parameter group; only those
            whose source is `user` become attributes. Ignored for every other kind of row.

    Returns:
        Attribute name to JSON-compatible value. Tags appear as `tag.<key>` and user-set parameters as
        `parameter.<name>`. Lifecycle statuses are not attributes; see `lifecycle_status`.

    Raises:
        TypeError: `record_row` is not one of the supported inventory row kinds.

    Example:
        >>> resource_attributes(instance_row)["db_instance_class"]
        'db.m6g.large'
    """
    if isinstance(record_row, InventoryDbInstance):
        return _instance_attributes(record_row)
    if isinstance(record_row, InventoryDbCluster):
        return _cluster_attributes(record_row)
    if isinstance(record_row, InventoryParameterGroup):
        return _parameter_group_attributes(record_row, parameter_values)
    if isinstance(record_row, InventoryDbSnapshot):
        return _database_snapshot_attributes(record_row)
    logger.error(
        "change_history_unsupported_row_kind",
        row_type=type(record_row).__name__,
        fix_suggestion="pass an instance, cluster, parameter group or database snapshot inventory row",
    )
    raise TypeError(f"no change history attributes for inventory row type {type(record_row).__name__}")


def lifecycle_status(record_row: InventoryRow) -> str | None:
    """The lifecycle status AWS reports for an inventory row, e.g. `available` or `modifying`.

    Args:
        record_row: An instance, cluster, parameter group or database snapshot row.

    Returns:
        The instance, cluster or snapshot status; None for parameter groups, which have none.

    Example:
        >>> lifecycle_status(instance_row)
        'available'
    """
    if isinstance(record_row, InventoryDbInstance):
        return record_row.db_instance_status
    if isinstance(record_row, InventoryDbCluster | InventoryDbSnapshot):
        return record_row.status
    return None


def terraform_address_of(record_row: InventoryRow) -> str | None:
    """The Terraform address the console gives the resource of an inventory row.

    Args:
        record_row: An instance, cluster, parameter group or database snapshot row.

    Returns:
        `<type>.<name>` for instances, cluster members, clusters and parameter groups; None for database
        snapshots, which the console does not manage in Terraform.

    Example:
        >>> terraform_address_of(orders_instance_row)
        'aws_db_instance.demo_orders'
    """
    if isinstance(record_row, InventoryDbInstance):
        resource_type = "aws_rds_cluster_instance" if record_row.db_cluster_identifier else "aws_db_instance"
        return terraform_address_for(resource_type, record_row.db_instance_identifier)
    if isinstance(record_row, InventoryDbCluster):
        return terraform_address_for("aws_rds_cluster", record_row.db_cluster_identifier)
    if isinstance(record_row, InventoryParameterGroup):
        resource_type = (
            "aws_db_parameter_group"
            if record_row.group_scope == "instance"
            else "aws_rds_cluster_parameter_group"
        )
        return terraform_address_for(resource_type, record_row.parameter_group_name)
    return None


@dataclass(frozen=True)
class SnapshotResources:
    """What change history reads from one inventory snapshot.

    Attributes:
        attributes_by_arn: ARN to the flat attributes that are compared between snapshots.
        lifecycle_status_by_arn: ARN to the lifecycle status, kept apart because it is not compared.
        terraform_address_by_arn: ARN to the resource's Terraform address (None for database snapshots),
            which names the resource in an operation's `touched_addresses`.
    """

    attributes_by_arn: dict[str, dict[str, Any]]
    lifecycle_status_by_arn: dict[str, str | None]
    terraform_address_by_arn: dict[str, str | None]


def read_snapshot_resources(session: Session, snapshot_id: int) -> SnapshotResources:
    """Attributes and lifecycle status of every change-history resource in one inventory snapshot.

    Only manual database and cluster snapshots are included: automated ones appear and expire on AWS's
    schedule and would bury real changes. They stay in the inventory.

    Args:
        session: Control database session.
        snapshot_id: Id of the inventory snapshot to read.

    Returns:
        The attributes and statuses, keyed by ARN.

    Example:
        >>> read_snapshot_resources(session, 12).lifecycle_status_by_arn[orders_arn]
        'available'
    """
    attributes_by_arn: dict[str, dict[str, Any]] = {}
    lifecycle_status_by_arn: dict[str, str | None] = {}
    terraform_address_by_arn: dict[str, str | None] = {}

    def add_resource(
        resource_arn: str, record_row: InventoryRow, parameter_values: Iterable[InventoryParameterValue] = ()
    ) -> None:
        attributes_by_arn[resource_arn] = resource_attributes(record_row, parameter_values)
        lifecycle_status_by_arn[resource_arn] = lifecycle_status(record_row)
        terraform_address_by_arn[resource_arn] = terraform_address_of(record_row)

    for instance_row in session.execute(
        select(InventoryDbInstance).where(InventoryDbInstance.snapshot_id == snapshot_id)
    ).scalars():
        add_resource(instance_row.db_instance_arn, instance_row)
    for cluster_row in session.execute(
        select(InventoryDbCluster).where(InventoryDbCluster.snapshot_id == snapshot_id)
    ).scalars():
        add_resource(cluster_row.db_cluster_arn, cluster_row)
    parameter_group_rows = list(
        session.execute(
            select(InventoryParameterGroup).where(InventoryParameterGroup.snapshot_id == snapshot_id)
        ).scalars()
    )
    user_parameter_values_by_group_id: dict[int, list[InventoryParameterValue]] = defaultdict(list)
    for parameter_value in session.execute(
        select(InventoryParameterValue).where(
            InventoryParameterValue.parameter_group_id.in_([row.id for row in parameter_group_rows]),
            InventoryParameterValue.source == "user",
        )
    ).scalars():
        user_parameter_values_by_group_id[parameter_value.parameter_group_id].append(parameter_value)
    for parameter_group_row in parameter_group_rows:
        add_resource(
            parameter_group_row.parameter_group_arn,
            parameter_group_row,
            user_parameter_values_by_group_id[parameter_group_row.id],
        )
    for database_snapshot_row in session.execute(
        select(InventoryDbSnapshot).where(
            InventoryDbSnapshot.snapshot_id == snapshot_id,
            InventoryDbSnapshot.snapshot_type == MANUAL_SNAPSHOT_TYPE,
        )
    ).scalars():
        add_resource(database_snapshot_row.db_snapshot_arn, database_snapshot_row)
    return SnapshotResources(attributes_by_arn, lifecycle_status_by_arn, terraform_address_by_arn)


def resources_in_snapshot(session: Session, snapshot_id: int) -> dict[str, dict[str, Any]]:
    """ARN to flat attributes for every change-history resource in one inventory snapshot.

    Args:
        session: Control database session.
        snapshot_id: Id of the inventory snapshot to read.

    Returns:
        Attributes (see `resource_attributes`) of every instance, cluster, parameter group and manual
        database snapshot captured in the inventory snapshot, keyed by ARN. Automated snapshots are left
        out, and so are lifecycle statuses (see `read_snapshot_resources`).

    Example:
        >>> attributes_by_arn = resources_in_snapshot(session, 12)
        >>> attributes_by_arn["arn:aws:rds:eu-west-2:000000000000:db:demo-orders"]["multi_az"]
        True
    """
    return read_snapshot_resources(session, snapshot_id).attributes_by_arn


def _operation_that_queued_the_observed_value(
    session: Session,
    resource_arn: str,
    terraform_address: str | None,
    target_key: str | None,
    attribute_path: str,
    observed_value: Any,
    window_end: datetime,
) -> uuid.UUID | None:
    """The succeeded operation whose value, queued for the maintenance window, AWS has now applied.

    AWS applies a queued change days after the operation's attribution window closed. It is still the
    operation's when: the operation targets this resource (its `resource_arn`, or `terraform_address` among
    its `touched_addresses` in `target_key`); it is the latest succeeded operation, started by `window_end`,
    whose change set names this resource and attribute; that entry was queued (`pending-window`) with the
    observed value as its desired value (the value the record holds); and no change of the attribute has
    been observed since that operation finished, so this is the queued value landing and not a later
    outside change back to it.

    Returns:
        The operation's id, or None when no operation queued the observed value.
    """
    change_set_attribute = CHANGE_SET_ATTRIBUTE_BY_HISTORY_ATTRIBUTE.get(attribute_path)
    if change_set_attribute is None or terraform_address is None:
        return None
    targets_the_resource = [Operation.resource_arn == resource_arn]
    if target_key is not None:
        targets_the_resource.append(
            (Operation.target_key == target_key)
            & Operation.plan["touched_addresses"].contains([terraform_address])
        )
    latest_operation_on_the_attribute = session.execute(
        select(Operation)
        .where(
            or_(*targets_the_resource),
            Operation.state == OperationState.SUCCEEDED.value,
            Operation.started_at_utc <= window_end,
            Operation.plan["change_set"].contains(
                [{"address": terraform_address, "attribute": change_set_attribute}]
            ),
        )
        .order_by(Operation.started_at_utc.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest_operation_on_the_attribute is None:
        return None
    change_set_entry = next(
        entry
        for entry in latest_operation_on_the_attribute.plan["change_set"]
        if entry.get("address") == terraform_address and entry.get("attribute") == change_set_attribute
    )
    if (
        change_set_entry.get("apply_type") != QUEUED_FOR_WINDOW_APPLY_TYPE
        or change_set_entry.get("desired") != observed_value
    ):
        return None
    operation_finished_at = (
        latest_operation_on_the_attribute.finished_at_utc or latest_operation_on_the_attribute.started_at_utc
    )
    observed_since_the_operation = session.execute(
        select(ResourceChangeEvent.id)
        .join(InventorySnapshot, InventorySnapshot.id == ResourceChangeEvent.snapshot_after_id)
        .where(
            ResourceChangeEvent.resource_arn == resource_arn,
            ResourceChangeEvent.attribute_path == attribute_path,
            InventorySnapshot.captured_at_utc > operation_finished_at,
        )
        .limit(1)
    ).first()
    if observed_since_the_operation is not None:
        return None
    return latest_operation_on_the_attribute.id


def explain_event(
    session: Session,
    resource_arn: str,
    window_start: datetime | None,
    window_end: datetime,
    terraform_address: str | None = None,
    target_key: str | None = None,
    attribute_path: str | None = None,
    observed_value: Any = None,
) -> tuple[str, uuid.UUID | None]:
    """Whether a console operation accounts for a change observed between two snapshots.

    Only an operation that can change AWS explains a change: reads (refresh, drift detection, import) and
    accepting drift, which writes only the record, never do. An operation targets the resource when the
    resource is its `resource_arn`, one of the resources an adoption selected, one it listed in
    `plan["created_resource_arns"]` (a snapshot it took), `plan["affected_resource_arns"]` (a resource whose
    attributes change as a side effect, such as the cluster a removed reader belonged to), or (in
    `target_key`) one of the Terraform addresses it touched, which is how a create names a resource that
    did not exist yet. It
    explains a change when it targets the resource, has changed AWS (state `SUCCEEDED`,
    `FAILED` or `RUNNING`; a queued or awaiting-approval operation has touched nothing, unless its plan
    says `changed_aws_before_pause`, as a major upgrade waiting for its switchover does: that one, paused
    or rejected, explains only the resources in its `created_resource_arns`) and its
    attribution window overlaps the window: it started no later than `window_end`, and its attribution
    window had not ended by `window_start`. An operation whose closing `refresh_resource` step took a
    snapshot (the step output's `snapshot_id`) has an attribution window that ends at that snapshot's
    capture time, which comes a little before the operation is marked finished: a change first observed
    in a later snapshot is never that operation's, so the window must start before the closing capture.
    Only the latest closing refresh the operation ran counts (a chained operation's
    `then_refresh_resource` comes after its first change's `refresh_resource`); while that refresh is
    still running it has no snapshot yet, so the window is still open.
    Any other operation's window ends when it finished (an unfinished one overlaps everything after its
    start), and it qualifies when it had not finished before `window_start`. A failed operation
    counts only when it got as far as changing AWS, whatever happened next (an apply that failed half-way
    may have changed AWS): it started its `apply_direct` step (the direct strategy's API calls), it invoked
    Terraform's apply (marked `terraform_apply_invoked` just before the binary runs, or with an `apply`
    run recorded), or it is marked `applied_without_record`. One that failed before touched nothing, so a
    change in its window is still an outside change: a stale inventory, a guard's refusal, nothing to
    change, or an `apply` step that refused at its pre-apply plan (`terraform_plan_failed`,
    `plan_changed_since_approval`), usually because of that very change. When several qualify, a succeeded
    one wins, then the one that started last.

    A value an operation queued for the maintenance window lands after its window closed: when no
    operation's window explains the change and `attribute_path` is given, the change is still the
    operation's if it queued exactly `observed_value` for this attribute (see
    `_operation_that_queued_the_observed_value`).

    Args:
        session: Control database session.
        resource_arn: The resource that changed.
        window_start: Capture time of the earlier snapshot; None means the window has no lower bound.
        window_end: Capture time of the later snapshot.
        terraform_address: The resource's Terraform address, matched against `touched_addresses`.
        target_key: The target the resource is in; addresses are matched only within it.
        attribute_path: The changed attribute, e.g. `db_instance_class`; None skips the queued-change match.
        observed_value: The attribute's value in the later snapshot.

    Returns:
        `("operation", operation_id)` for the best qualifying operation, otherwise
        `("outside_console", None)`.

    Example:
        >>> explain_event(session, orders_arn, previous_captured_at, new_captured_at)
        ('outside_console', None)
    """
    targets_the_resource = [
        Operation.resource_arn == resource_arn,
        Operation.plan["selected_resources"].contains([{"resource_arn": resource_arn}]),
        Operation.plan["created_resource_arns"].contains([resource_arn]),
        Operation.plan["affected_resource_arns"].contains([resource_arn]),
    ]
    if terraform_address is not None and target_key is not None:
        targets_the_resource.append(
            (Operation.target_key == target_key)
            & Operation.plan["touched_addresses"].contains([terraform_address])
        )
    accepts_drift = (Operation.operation_kind == "drift_resolve") & Operation.plan[
        "choice"
    ].astext.is_not_distinct_from("accept")
    started_the_direct_api_calls = exists(
        select(OperationStepAttempt.id).where(
            OperationStepAttempt.operation_id == Operation.id,
            OperationStepAttempt.step_name == DIRECT_APPLY_STEP_NAME,
        )
    )
    invoked_the_terraform_apply = Operation.plan.contains({TERRAFORM_APPLY_INVOKED_PLAN_KEY: True}) | exists(
        select(TerraformRun.id).where(
            TerraformRun.operation_id == Operation.id,
            TerraformRun.run_kind == TERRAFORM_APPLY_RUN_KIND,
        )
    )
    reached_aws = (
        (Operation.state != OperationState.FAILED.value)
        | started_the_direct_api_calls
        | invoked_the_terraform_apply
        | Operation.plan.contains({"applied_without_record": True})
    )
    # A paused or rejected operation that changed AWS before its pause explains only what it created (a
    # major upgrade's green side): production is untouched until the switchover, so a change to it while
    # the upgrade waits stays an outside change.
    explains_while_paused = (
        Operation.state.in_(PAUSED_OR_CANCELLED_STATES)
        & Operation.plan.contains({CHANGED_AWS_BEFORE_PAUSE_PLAN_KEY: True})
        & Operation.plan["created_resource_arns"].contains([resource_arn])
    )
    statement = select(Operation).where(
        (Operation.state.in_(EXPLAINING_OPERATION_STATES) & or_(*targets_the_resource))
        | explains_while_paused,
        Operation.operation_kind.not_in(OPERATION_KINDS_THAT_NEVER_CHANGE_AWS),
        ~accepts_drift,
        reached_aws,
        Operation.started_at_utc <= window_end,
    )
    if window_start is not None:
        latest_closing_snapshot_id = (
            select(OperationStepAttempt.output[CLOSING_REFRESH_SNAPSHOT_OUTPUT_KEY].as_integer())
            .where(
                OperationStepAttempt.operation_id == Operation.id,
                OperationStepAttempt.step_name.in_(CLOSING_REFRESH_STEP_NAMES),
                OperationStepAttempt.observation_state.is_distinct_from(ObservationState.SATISFIED.value),
            )
            .order_by(OperationStepAttempt.id.desc())
            .limit(1)
            .correlate(Operation)
            .scalar_subquery()
        )
        closing_snapshot_captured_at = (
            select(InventorySnapshot.captured_at_utc)
            .where(InventorySnapshot.id == latest_closing_snapshot_id)
            .scalar_subquery()
        )
        statement = statement.where(
            or_(
                and_(
                    closing_snapshot_captured_at.is_not(None),
                    closing_snapshot_captured_at > window_start,
                ),
                and_(
                    closing_snapshot_captured_at.is_(None),
                    Operation.finished_at_utc.is_(None) | (Operation.finished_at_utc >= window_start),
                ),
            )
        )
    explaining_operation = session.execute(
        statement.order_by(
            case((Operation.state == OperationState.SUCCEEDED.value, 0), else_=1),
            Operation.started_at_utc.desc(),
        ).limit(1)
    ).scalar_one_or_none()
    if explaining_operation is None:
        if attribute_path is not None:
            queuing_operation_id = _operation_that_queued_the_observed_value(
                session,
                resource_arn,
                terraform_address,
                target_key,
                attribute_path,
                observed_value,
                window_end,
            )
            if queuing_operation_id is not None:
                return EXPLANATION_OPERATION, queuing_operation_id
        return EXPLANATION_OUTSIDE_CONSOLE, None
    return EXPLANATION_OPERATION, explaining_operation.id


def compute_change_events(
    session: Session, target: ResolvedTarget, previous_snapshot_id: int | None, new_snapshot_id: int
) -> list[ResourceChangeEvent]:
    """Persist one event per attribute that differs between two snapshots of a target.

    The first snapshot of a target has nothing to compare with and produces no events. Afterwards a
    resource that vanished gets a `removed` event, one that appeared gets a `created` event (both carry
    the resource's lifecycle status), and a resource present in both gets one event per changed
    attribute. Automated snapshots and lifecycle status flips produce no events. Each event is
    attributed to the console operation whose attribution window overlaps the two captures (it ends at
    the operation's closing snapshot when it took one, else when it finished; see `explain_event`), else
    to the operation that queued the observed value for the maintenance window, or to `outside_console`.

    Args:
        session: Control database session; the caller commits.
        target: The target both snapshots belong to.
        previous_snapshot_id: The target's snapshot before the new one, or None for the first snapshot.
        new_snapshot_id: The snapshot just written.

    Returns:
        The events that were persisted (flushed, not committed), ordered by resource then attribute.

    Raises:
        ValueError: A snapshot id does not exist, so no honest comparison can be made.

    Example:
        >>> [e.attribute_path for e in compute_change_events(session, target, 11, 12)]
        ['db_instance_class', 'parameter.work_mem']
    """
    if previous_snapshot_id is None:
        logger.info(
            "change_events_skipped_for_first_snapshot",
            target_key=target.target_key,
            snapshot_id=new_snapshot_id,
        )
        return []
    logger.info(
        "change_events_computation_started",
        target_key=target.target_key,
        previous_snapshot_id=previous_snapshot_id,
        new_snapshot_id=new_snapshot_id,
    )
    new_snapshot = session.get(InventorySnapshot, new_snapshot_id)
    previous_snapshot = session.get(InventorySnapshot, previous_snapshot_id)
    if new_snapshot is None or previous_snapshot is None:
        missing_snapshot_id = new_snapshot_id if new_snapshot is None else previous_snapshot_id
        logger.error(
            "change_events_snapshot_not_found",
            target_key=target.target_key,
            snapshot_id=missing_snapshot_id,
            fix_suggestion="compare snapshot ids returned by write_snapshot or latest_snapshot_for_target",
        )
        raise ValueError(f"inventory snapshot {missing_snapshot_id} not found")
    resources_before = read_snapshot_resources(session, previous_snapshot_id)
    resources_after = read_snapshot_resources(session, new_snapshot_id)
    attributes_before = resources_before.attributes_by_arn
    attributes_after = resources_after.attributes_by_arn
    window_start = previous_snapshot.captured_at_utc
    window_end = new_snapshot.captured_at_utc
    explanation_by_arn: dict[str, tuple[str, uuid.UUID | None]] = {}
    events: list[ResourceChangeEvent] = []

    def record_change(resource_arn: str, attribute_path: str, value_before: Any, value_after: Any) -> None:
        terraform_address = resources_after.terraform_address_by_arn.get(
            resource_arn, resources_before.terraform_address_by_arn.get(resource_arn)
        )
        if resource_arn not in explanation_by_arn:
            explanation_by_arn[resource_arn] = explain_event(
                session, resource_arn, window_start, window_end, terraform_address, target.target_key
            )
        explanation, explaining_operation_id = explanation_by_arn[resource_arn]
        if (
            explanation == EXPLANATION_OUTSIDE_CONSOLE
            and attribute_path in CHANGE_SET_ATTRIBUTE_BY_HISTORY_ATTRIBUTE
        ):
            explanation, explaining_operation_id = explain_event(
                session,
                resource_arn,
                window_start,
                window_end,
                terraform_address,
                target.target_key,
                attribute_path=attribute_path,
                observed_value=value_after,
            )
        events.append(
            ResourceChangeEvent(
                resource_arn=resource_arn,
                environment_slug=target.environment_slug,
                target_key=target.target_key,
                snapshot_before_id=previous_snapshot_id,
                snapshot_after_id=new_snapshot_id,
                attribute_path=attribute_path,
                value_before=value_before,
                value_after=value_after,
                explanation=explanation,
                operation_id=explaining_operation_id,
            )
        )

    for resource_arn in sorted(set(attributes_before) - set(attributes_after)):
        record_change(
            resource_arn,
            ATTRIBUTE_PATH_REMOVED,
            resources_before.lifecycle_status_by_arn[resource_arn],
            None,
        )
    for resource_arn in sorted(set(attributes_after) - set(attributes_before)):
        record_change(
            resource_arn,
            ATTRIBUTE_PATH_CREATED,
            None,
            resources_after.lifecycle_status_by_arn[resource_arn],
        )
    for resource_arn in sorted(set(attributes_before) & set(attributes_after)):
        resource_before = attributes_before[resource_arn]
        resource_after = attributes_after[resource_arn]
        for attribute_path in sorted(set(resource_before) | set(resource_after)):
            if resource_before.get(attribute_path) != resource_after.get(attribute_path):
                record_change(
                    resource_arn,
                    attribute_path,
                    resource_before.get(attribute_path),
                    resource_after.get(attribute_path),
                )
    session.add_all(events)
    session.flush()
    logger.info(
        "change_events_written",
        target_key=target.target_key,
        previous_snapshot_id=previous_snapshot_id,
        new_snapshot_id=new_snapshot_id,
        event_count=len(events),
        outside_console_count=sum(event.explanation == EXPLANATION_OUTSIDE_CONSOLE for event in events),
    )
    return events


def recent_change_events_for(
    session: Session, resource_arn: str, limit: int = 50
) -> list[ResourceChangeEvent]:
    """The latest change events of one resource.

    Args:
        session: Control database session.
        resource_arn: ARN of the resource.
        limit: Maximum number of events to return.

    Returns:
        Events newest first.

    Example:
        >>> recent_change_events_for(session, "arn:aws:rds:eu-west-2:000000000000:db:demo-orders", limit=5)
        [<ResourceChangeEvent ...>]
    """
    return list(
        session.execute(
            select(ResourceChangeEvent)
            .where(ResourceChangeEvent.resource_arn == resource_arn)
            .order_by(ResourceChangeEvent.id.desc())
            .limit(limit)
        ).scalars()
    )


def review_change_event(session: Session, event_id: int, operator_name: str) -> ResourceChangeEvent:
    """Mark a change event as reviewed; nothing else about the event changes.

    Args:
        session: Control database session; the caller commits.
        event_id: Id of the change event.
        operator_name: Operator doing the review.

    Returns:
        The event with `reviewed_by_operator_name` and `reviewed_at_utc` set.

    Raises:
        KeyError: No change event has `event_id`.

    Example:
        >>> review_change_event(session, 42, "sam").reviewed_by_operator_name
        'sam'
    """
    event = session.get(ResourceChangeEvent, event_id)
    if event is None:
        logger.error(
            "change_event_review_failed",
            event_id=event_id,
            fix_suggestion="list change events with GET /api/v1/history/changes and review an id from it",
        )
        raise KeyError(f"change event {event_id} not found")
    event.reviewed_by_operator_name = operator_name
    event.reviewed_at_utc = utc_now()
    logger.info("change_event_reviewed", event_id=event_id, operator_name=operator_name)
    return event
