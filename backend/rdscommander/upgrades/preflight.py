"""The checks a major upgrade runs before it asks to create the green side, each pass, warn or fail.

* `pending_maintenance`: maintenance AWS has queued for the resource (or a cluster's members) could
  interfere with the deployment: warn.
* `backups`: blue/green needs automated backups; a retention under one day fails.
* `in_progress_operations`: another console operation on the resource (or a member) that is queued,
  running or awaiting approval fails.
* `replicas`: read replicas are listed with their fate (they are not carried to green): warn.
* `class_and_storage`: the instance class (each member's, for a cluster) and, for an instance, its storage
  type must be orderable for the target version (`DescribeOrderableDBInstanceOptions`); a missing class or
  storage type fails. When AWS lists nothing the simulated account warns that it cannot say, a real account
  fails (nothing can be ordered on that version).
* `extensions`: the console cannot read the installed extensions: warn, saying so.
* `blue_side_name`: an instance (or, for a cluster, a cluster) named `<identifier>-old1` still exists, most
  likely an earlier upgrade's blue side; AWS would name this switchover's blue side `-old2`, so it fails
  until that one is decommissioned.
* `logical_replication`: a blue/green deployment of RDS for PostgreSQL keeps green in step through logical
  replication, so `rds.logical_replication` must be 1 on the blue side's parameter group (an instance's DB
  parameter group; an Aurora cluster's cluster parameter group); off (or left at its default of 0) fails, and
  a group the latest inventory does not hold warns.

Live values (retention, replicas, class, storage) are read from AWS under the step `preflight`; pending
maintenance, member records and parameter groups come from the latest inventory.

Example:
    >>> checks = run_preflight(context, orders_record, "17.2")  # doctest: +SKIP
    >>> [(check.name, check.outcome) for check in checks]  # doctest: +SKIP
    [('pending_maintenance', 'pass'), ('backups', 'pass'), ..., ('extensions', 'warn')]
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.aws.rds_reader import UNSUPPORTED_MARKER
from rdscommander.control_db.models import Operation
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.lifecycle.direct_api import cluster_describe, instance_describe
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import OperationState
from rdscommander.operations.executor import StepContext
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import find_parameter_group, list_resources, pending_maintenance_for
from rdscommander.upgrades.blue_green_api import BLUE_SIDE_SUFFIX

logger = get_logger(__name__)

PreflightOutcome = Literal["pass", "warn", "fail"]
"""`pass` needs nothing; `warn` is for the approver to read; `fail` stops the upgrade."""
PREFLIGHT_STEP_NAME = "preflight"
REPLICAS_NOT_CARRIED_SENTENCE = "replicas are not carried to green; recreate them after"
IN_PROGRESS_STATES = (
    OperationState.QUEUED.value,
    OperationState.RUNNING.value,
    OperationState.AWAITING_APPROVAL.value,
)
CHECK_TITLES = {
    "pending_maintenance": "Pending maintenance",
    "backups": "Automated backups",
    "in_progress_operations": "Operations in progress",
    "replicas": "Read replicas",
    "class_and_storage": "Instance class and storage on the target version",
    "extensions": "Extensions",
    "blue_side_name": "Name of the blue side after the switchover",
    "logical_replication": "Logical replication",
}
"""Title of each check, as the review's checklist shows it."""
SEVERITY_BY_OUTCOME = {"pass": "info", "warn": "warning", "fail": "error"}
NOTHING_TO_DO = "nothing to do"


class PreflightCheck(BaseModel):
    """One preflight check's result.

    Attributes:
        name: The check, a key of `CHECK_TITLES`.
        outcome: `pass`, `warn` or `fail`.
        detail: What was found, in a sentence.
        fix_suggestion: What to do about a warning or failure; empty for a pass.

    Example:
        >>> PreflightCheck(name="backups", outcome="pass", detail="retention 7 days").fix_suggestion
        ''
    """

    name: str
    outcome: PreflightOutcome
    detail: str
    fix_suggestion: str = ""


def member_records(context: StepContext, record: ResourceRecord) -> list[ResourceRecord]:
    """A cluster's member instances from the latest inventory; empty for an instance.

    Example:
        >>> [member.identifier for member in member_records(context, aurora)]  # doctest: +SKIP
        ['demo-aurora-reader', 'demo-aurora-writer']
    """
    if record.kind != ResourceKind.CLUSTER:
        return []
    member_identifiers = set(record.relationships.get("member_identifiers") or [])
    return [
        member
        for member in list_resources(
            context.session, context.console_config, record.environment_slug, ResourceKind.INSTANCE
        )
        if member.target_key == record.target_key and member.identifier in member_identifiers
    ]


def _check_pending_maintenance(
    context: StepContext, record: ResourceRecord, members: list[ResourceRecord]
) -> PreflightCheck:
    """Warn about every maintenance action AWS has queued for the resource or its members."""
    pending: list[str] = []
    for subject in [record, *members]:
        for row in pending_maintenance_for(context.session, subject.arn, record.snapshot_id):
            pending.append(f"{subject.identifier}: {row.action} ({row.description})")
    if not pending:
        return PreflightCheck(name="pending_maintenance", outcome="pass", detail="no maintenance is pending")
    return PreflightCheck(
        name="pending_maintenance",
        outcome="warn",
        detail="pending maintenance could interfere with the deployment: " + "; ".join(pending),
        fix_suggestion="apply or defer the pending maintenance first, or make sure it is not applied "
        "while the green side is created",
    )


def _check_backups(identifier: str, live: dict[str, Any] | None) -> PreflightCheck:
    """Fail unless automated backups are kept for at least one day."""
    if live is None:
        return PreflightCheck(
            name="backups",
            outcome="fail",
            detail=f"AWS does not know {identifier}",
            fix_suggestion="refresh inventory; the resource may have been deleted or renamed",
        )
    retention_days = int(live.get("BackupRetentionPeriod") or 0)
    if retention_days >= 1:
        return PreflightCheck(
            name="backups", outcome="pass", detail=f"automated backups are kept for {retention_days} days"
        )
    return PreflightCheck(
        name="backups",
        outcome="fail",
        detail=f"automated backups are off on {identifier} (retention {retention_days} days); "
        "a blue/green deployment needs them",
        fix_suggestion="turn automated backups on (retention of at least one day) with Backup settings, "
        "then create the upgrade again",
    )


def _check_in_progress_operations(
    context: StepContext, record: ResourceRecord, members: list[ResourceRecord]
) -> PreflightCheck:
    """Fail when another console operation on the resource or a member is queued, running or paused."""
    others = operations_in_progress_on(
        context.session, [record.arn, *(member.arn for member in members)], context.operation.id
    )
    if not others:
        return PreflightCheck(
            name="in_progress_operations", outcome="pass", detail="no other operation is in progress"
        )
    listed = describe_operations(others)
    return PreflightCheck(
        name="in_progress_operations",
        outcome="fail",
        detail=f"other operations on {record.identifier} are in progress: {listed}",
        fix_suggestion="let them finish, or reject the ones awaiting approval, then create the upgrade again",
    )


def operations_in_progress_on(
    session: Session, resource_arns: list[str], excluding_operation_id: uuid.UUID
) -> list[Operation]:
    """Console operations on any of `resource_arns` queued, running or awaiting approval, oldest first.

    Example:
        >>> operations_in_progress_on(session, [orders_arn], upgrade.id)  # doctest: +SKIP
        [<Operation ...>]
    """
    return list(
        session.execute(
            select(Operation)
            .where(
                Operation.resource_arn.in_(resource_arns),
                Operation.state.in_(IN_PROGRESS_STATES),
                Operation.id != excluding_operation_id,
            )
            .order_by(Operation.created_at_utc)
        ).scalars()
    )


def describe_operations(operations: list[Operation]) -> str:
    """`<title> (<id>, <state>)` of each operation, separated by semicolons.

    Example:
        >>> describe_operations([reboot])  # doctest: +SKIP
        'Reboot: demo-orders (6f1c..., RUNNING)'
    """
    return "; ".join(
        f"{operation.title or operation.operation_kind} ({operation.id}, {operation.state})"
        for operation in operations
    )


def _check_replicas(record: ResourceRecord, live: dict[str, Any] | None) -> PreflightCheck:
    """List the read replicas, which stay with the blue side; warn when there are any."""
    live = live or {}
    if record.kind == ResourceKind.CLUSTER:
        replicas = list(live.get("ReadReplicaIdentifiers") or [])
    else:
        replicas = list(live.get("ReadReplicaDBInstanceIdentifiers") or []) + list(
            live.get("ReadReplicaDBClusterIdentifiers") or []
        )
    if not replicas:
        return PreflightCheck(name="replicas", outcome="pass", detail="no read replicas")
    identifiers = ", ".join(identifier.rsplit(":", 1)[-1] for identifier in replicas)
    return PreflightCheck(
        name="replicas",
        outcome="warn",
        detail=f"{identifiers}: {REPLICAS_NOT_CARRIED_SENTENCE}",
        fix_suggestion="after the switchover, create the replicas again from the upgraded resource and "
        "decommission the old ones",
    )


def _orderable_options(context: StepContext, engine: str, engine_version: str) -> list[dict[str, Any]] | None:
    """`DescribeOrderableDBInstanceOptions` for the version; None when the simulated account cannot answer."""
    rds = context.services.aws_sessions.client(
        context.target, "rds", operation_id=context.operation.id, step_name=PREFLIGHT_STEP_NAME
    )
    options: list[dict[str, Any]] = []
    try:
        for page in rds.get_paginator("describe_orderable_db_instance_options").paginate(
            Engine=engine, EngineVersion=engine_version
        ):
            options += page["OrderableDBInstanceOptions"]
    except Exception as error:  # noqa: BLE001 - only the simulator's "not implemented" is translated
        if UNSUPPORTED_MARKER in str(error):
            return None
        raise
    return options


def _check_class_and_storage(
    context: StepContext,
    record: ResourceRecord,
    members: list[ResourceRecord],
    live: dict[str, Any] | None,
    target_engine_version: str,
) -> PreflightCheck:
    """Fail when the target version does not offer an instance's class, or its storage type with it."""
    engine = str(record.facts.get("engine") or "postgres")
    if record.kind == ResourceKind.CLUSTER:
        classes_by_identifier = {
            member.identifier: str(member.facts.get("db_instance_class")) for member in members
        }
        storage_type = None
    else:
        live = live or {}
        classes_by_identifier = {
            record.identifier: str(live.get("DBInstanceClass") or record.facts.get("db_instance_class"))
        }
        storage_type = live.get("StorageType") or record.facts.get("storage_type")
    options = _orderable_options(context, engine, target_engine_version)
    if not options:
        if context.target.simulated:
            return PreflightCheck(
                name="class_and_storage",
                outcome="warn",
                detail=f"the simulated account lists no orderable options for {engine} "
                f"{target_engine_version}, so the class and storage could not be checked",
                fix_suggestion="check the class against the target version on a real account",
            )
        return PreflightCheck(
            name="class_and_storage",
            outcome="fail",
            detail=f"AWS offers no instance class for {engine} {target_engine_version} in this region",
            fix_suggestion="choose another target version",
        )
    offered_classes = {str(option.get("DBInstanceClass")) for option in options}
    missing = sorted(
        f"{identifier} ({instance_class})"
        for identifier, instance_class in classes_by_identifier.items()
        if instance_class not in offered_classes
    )
    if missing:
        return PreflightCheck(
            name="class_and_storage",
            outcome="fail",
            detail=f"{engine} {target_engine_version} does not offer the instance class of "
            f"{', '.join(missing)}",
            fix_suggestion="change the instance class to one the target version offers, then create the "
            "upgrade again",
        )
    if storage_type:
        [instance_class] = classes_by_identifier.values()
        offered_storage = {
            str(option["StorageType"])
            for option in options
            if option.get("DBInstanceClass") == instance_class and option.get("StorageType")
        }
        if offered_storage and storage_type not in offered_storage:
            return PreflightCheck(
                name="class_and_storage",
                outcome="fail",
                detail=f"{engine} {target_engine_version} offers {instance_class} only with "
                f"{', '.join(sorted(offered_storage))} storage, not {storage_type}",
                fix_suggestion="change the storage type to one the target version offers with this class, "
                "then create the upgrade again",
            )
    return PreflightCheck(
        name="class_and_storage",
        outcome="pass",
        detail=f"{engine} {target_engine_version} offers "
        f"{', '.join(sorted(set(classes_by_identifier.values())))}"
        + (f" with {storage_type} storage" if storage_type else ""),
    )


def _check_extensions(context: StepContext, target_engine_version: str) -> PreflightCheck:
    """Warn that the console cannot read the installed extensions (and the simulator has none)."""
    if context.target.simulated:
        return PreflightCheck(
            name="extensions",
            outcome="warn",
            detail="extension checks are not available on this account: the simulated account runs no "
            "databases",
            fix_suggestion="check the extensions on a real account before upgrading there",
        )
    return PreflightCheck(
        name="extensions",
        outcome="warn",
        detail="the console cannot read the installed extensions",
        fix_suggestion=f"connect to the database and compare `pg_extension` with the versions "
        f"{target_engine_version} offers (`pg_available_extension_versions` on a test instance) before "
        "the switchover",
    )


def _check_blue_side_name(context: StepContext, record: ResourceRecord) -> PreflightCheck:
    """Fail while `<identifier>-old1` exists: the switchover's blue side must get exactly that name."""
    blue_side_identifier = f"{record.identifier}{BLUE_SIDE_SUFFIX}"
    if record.kind == ResourceKind.CLUSTER:
        existing = cluster_describe(context, blue_side_identifier, PREFLIGHT_STEP_NAME)
    else:
        existing = instance_describe(context, blue_side_identifier, PREFLIGHT_STEP_NAME)
    if existing is None:
        return PreflightCheck(
            name="blue_side_name",
            outcome="pass",
            detail=f"the blue side will be kept as {blue_side_identifier}",
        )
    return PreflightCheck(
        name="blue_side_name",
        outcome="fail",
        detail=f"{blue_side_identifier} already exists (most likely the blue side of an earlier upgrade); "
        f"AWS would keep this upgrade's blue side under another name",
        fix_suggestion=f"decommission {blue_side_identifier} first (the inbox offers Decommission on an "
        "earlier upgrade's blue side), then create the upgrade again",
    )


LOGICAL_REPLICATION_PARAMETER = "rds.logical_replication"
LOGICAL_REPLICATION_ON_VALUES = frozenset({"1", "on", "true"})


def _check_logical_replication(context: StepContext, record: ResourceRecord) -> PreflightCheck:
    """Fail unless `rds.logical_replication` is on in the blue side's group; warn when it cannot be read.

    The group is the instance's DB parameter group, or an Aurora cluster's cluster parameter group, read from
    the latest inventory (its user-set parameters; a parameter left at its default is off).
    """
    if record.kind == ResourceKind.CLUSTER:
        scope, group_name = "cluster", record.relationships.get("cluster_parameter_group_name")
    else:
        scope, group_name = "instance", record.relationships.get("parameter_group_name")
    if not group_name:
        return PreflightCheck(
            name="logical_replication",
            outcome="warn",
            detail=f"the latest inventory names no {scope} parameter group for {record.identifier}, so "
            f"{LOGICAL_REPLICATION_PARAMETER} could not be read",
            fix_suggestion=f"refresh inventory, then check that {LOGICAL_REPLICATION_PARAMETER} is 1 on "
            f"{record.identifier}'s {scope} parameter group",
        )
    group = find_parameter_group(
        context.session, context.console_config, record.target_key, scope, group_name
    )
    if group is None:
        return PreflightCheck(
            name="logical_replication",
            outcome="warn",
            detail=f"{scope} parameter group {group_name} is not in the latest inventory, so "
            f"{LOGICAL_REPLICATION_PARAMETER} could not be read",
            fix_suggestion=f"refresh inventory, then check that {LOGICAL_REPLICATION_PARAMETER} is 1 on "
            f"{group_name}",
        )
    entry = (group.facts.get("parameters") or {}).get(LOGICAL_REPLICATION_PARAMETER)
    value = str(entry.get("value")) if isinstance(entry, dict) and entry.get("value") is not None else None
    if value is not None and value.lower() in LOGICAL_REPLICATION_ON_VALUES:
        return PreflightCheck(
            name="logical_replication",
            outcome="pass",
            detail=f"{LOGICAL_REPLICATION_PARAMETER} is {value} on {group_name}",
        )
    shown_value = value if value is not None else "0 (the default)"
    return PreflightCheck(
        name="logical_replication",
        outcome="fail",
        detail=f"{LOGICAL_REPLICATION_PARAMETER} is {shown_value} on {group_name}; a blue/green deployment "
        "needs logical replication on",
        fix_suggestion=f"set {LOGICAL_REPLICATION_PARAMETER} to 1 on {group_name} (Edit parameters), reboot "
        f"{record.identifier} (it is a static parameter), then run the upgrade",
    )


def run_preflight(
    context: StepContext, record: ResourceRecord, target_engine_version: str
) -> list[PreflightCheck]:
    """Run every check for an upgrade of `record` to `target_engine_version`.

    Args:
        context: The running step's context; AWS calls are recorded under `preflight`.
        record: The instance or cluster being upgraded, from the latest inventory.
        target_engine_version: The version the green side will run.

    Returns:
        One check per name in `CHECK_TITLES`, in that order.

    Example:
        >>> [check.outcome for check in run_preflight(context, record, "17.2")]  # doctest: +SKIP
        ['pass', 'pass', 'pass', 'warn', 'warn', 'warn']
    """
    logger.info(
        "major_upgrade_preflight_started",
        operation_id=str(context.operation.id),
        resource_identifier=record.identifier,
        target_engine_version=target_engine_version,
    )
    members = member_records(context, record)
    if record.kind == ResourceKind.CLUSTER:
        live = cluster_describe(context, record.identifier, PREFLIGHT_STEP_NAME)
    else:
        live = instance_describe(context, record.identifier, PREFLIGHT_STEP_NAME)
    checks = [
        _check_pending_maintenance(context, record, members),
        _check_backups(record.identifier, live),
        _check_in_progress_operations(context, record, members),
        _check_replicas(record, live),
        _check_class_and_storage(context, record, members, live, target_engine_version),
        _check_extensions(context, target_engine_version),
        _check_blue_side_name(context, record),
        _check_logical_replication(context, record),
    ]
    logger.info(
        "major_upgrade_preflight_completed",
        operation_id=str(context.operation.id),
        resource_identifier=record.identifier,
        outcomes={check.name: check.outcome for check in checks},
    )
    return checks


def record_preflight_checks(
    session: Session, operation_id: uuid.UUID, step_name: str, checks: list[PreflightCheck]
) -> None:
    """File each check as a diagnostic of the operation, so the review's checklist shows it (caller commits).

    A pass is `info`, a warning `warning`, a failure `error`; the kind is `preflight_<name>`.

    Example:
        >>> record_preflight_checks(session, operation.id, "preflight", checks)  # doctest: +SKIP
    """
    for check in checks:
        record_diagnostic(
            session,
            operation_id=operation_id,
            step_name=step_name,
            severity=SEVERITY_BY_OUTCOME[check.outcome],
            diagnostic_kind=f"preflight_{check.name}",
            title=CHECK_TITLES.get(check.name, check.name),
            detail=check.detail,
            fix_suggestion=check.fix_suggestion or NOTHING_TO_DO,
        )
