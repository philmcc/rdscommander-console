"""Lifecycle actions on the direct-action base: one AWS call in both modes, reviewed, settled and refreshed.

`reboot_instance` reboots one instance, or every instance of a parameter group that is pending a reboot.
`cluster_failover` swaps an Aurora cluster's writer, `apply_pending_maintenance` opts a resource in to a
pending maintenance action, and `snapshot_take` / `snapshot_delete` create and remove manual snapshots.
None of them changes anything the Terraform files or the record describe, so none reconciles.

Example:
    >>> executor = RebootInstanceExecutor(config_store)  # doctest: +SKIP
    >>> executors = {executor.operation_kind: executor}  # doctest: +SKIP
"""

from __future__ import annotations

from typing import Any

from rdscommander.aws.rds_reader import SimulatorUnsupportedAction
from rdscommander.catalogue.catalogue import FOREIGN_OWNERSHIP_PREFIX, status_allows_changes
from rdscommander.control_db.models import Operation
from rdscommander.lifecycle import direct_api as lifecycle_api
from rdscommander.lifecycle.options import (
    FailoverOptions,
    PendingMaintenanceOptions,
    PromoteReplicaOptions,
    RebootInstanceOptions,
    TakeSnapshotOptions,
)
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeDiagnosticError, VerificationResult
from rdscommander.operations.create_change import CREATED_RESOURCE_ARNS_PLAN_KEY
from rdscommander.operations.direct_action import ActionReview, DirectActionExecutor
from rdscommander.operations.enums import StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepOutcome
from rdscommander.parameters import direct_api
from rdscommander.parameters.direct_api import TERMINAL_RESOURCE_STATUSES
from rdscommander.parameters.executors import (
    ForeignOwnedRebootRefused,
    describe_instance_statuses,
    refuse_foreign_owned_reboot,
)
from rdscommander.resources.model import ResourceKind, ResourceRecord, terraform_resource_type_for
from rdscommander.resources.queries import load_resource, pending_maintenance_for
from rdscommander.terraform.names import terraform_address_for

logger = get_logger(__name__)

REBOOT_IDENTIFIERS_PLAN_KEY = "reboot_identifiers"
"""Plan key holding the instances a `reboot_instance` operation reboots, fixed when approval is asked for."""
REBOOT_DOWNTIME = "a restart of each instance (Multi-AZ fails over when forced)"
"""What the approver of a reboot should expect."""
FAILOVER_DOWNTIME = "connections drop while the writer changes (usually under a minute)"
"""What the approver of a failover should expect."""
SNAPSHOT_DOWNTIME = "none (a brief I/O pause on single-AZ instances)"
"""What the approver of a snapshot should expect."""
PROMOTE_DOWNTIME = (
    "the replica restarts and is unavailable for a few minutes; replication from its source stops for good"
)
"""What the approver of a promotion should expect."""
MANUAL_SNAPSHOT_TYPE = "manual"
"""`SnapshotType` of a snapshot somebody took; the only kind the console deletes."""
__all__ = [
    "CREATED_RESOURCE_ARNS_PLAN_KEY",
    "FAILOVER_DOWNTIME",
    "REBOOT_DOWNTIME",
    "REBOOT_IDENTIFIERS_PLAN_KEY",
    "SNAPSHOT_DOWNTIME",
    "ApplyPendingMaintenanceExecutor",
    "ClusterFailoverExecutor",
    "PROMOTE_DOWNTIME",
    "DeleteSnapshotExecutor",
    "ForceFailoverNeedsMultiAzRefused",
    "ForeignOwnedRebootRefused",
    "ForeignOwnedResourceRefused",
    "ForeignOwnedSnapshotSourceRefused",
    "NotAdoptedRefused",
    "PromoteReplicaExecutor",
    "RebootInstanceExecutor",
    "SimulatorCannotApplyMaintenance",
    "TakeSnapshotExecutor",
    "refuse_foreign_owned_reboot",
]


class ForceFailoverNeedsMultiAzRefused(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """A reboot with failover was asked for an instance that has no standby to fail over to."""

    diagnostic_kind = "force_failover_needs_multi_az"
    title = "Only a Multi-AZ instance can fail over on reboot"
    fix_suggestion = (
        "reboot without failover, or turn Multi-AZ on for these instances first and then reboot with failover"
    )


class RebootInstanceExecutor(DirectActionExecutor):
    """Reboot one instance, or every instance of a parameter group that is pending a reboot.

    Which instances are rebooted is fixed when approval is asked for, so the approver sees the list and
    exactly those instances are rebooted. Every instance the reboot would touch must not be owned by other
    tooling, unless the environment allows direct changes to foreign-owned resources.
    """

    operation_kind = "reboot_instance"
    not_settled_diagnostic_kind = "instance_not_available"
    not_settled_title = "Instances did not return to available"

    def _options(self, context: StepContext) -> RebootInstanceOptions:
        return RebootInstanceOptions.model_validate(context.operation.plan.get("options", {}))

    def _identifiers_to_reboot(self, context: StepContext) -> list[str]:
        """The instance itself, or every instance using the group that is pending a reboot.

        Raises:
            KeyError: The resource is not in the latest inventory.
            ValueError: No instance is pending a reboot.
            ForeignOwnedRebootRefused: An instance the reboot would touch is owned by other tooling.
        """
        record = load_resource(context.session, context.console_config, context.operation.resource_arn or "")
        if record is None:
            raise KeyError(f"{context.operation.resource_arn} is not in the latest inventory")
        if record.kind != ResourceKind.PARAMETER_GROUP:
            refuse_foreign_owned_reboot(context, {record.identifier: record.ownership})
            return [record.identifier]
        identifiers = list(record.facts.get("pending_reboot_instances", []))
        if not identifiers:
            raise ValueError(f"no instance using {record.identifier} is pending a reboot")
        refuse_foreign_owned_reboot(context, record.facts.get("pending_reboot_instance_ownership", {}))
        return identifiers

    def _refuse_failover_without_standby(self, context: StepContext, identifiers: list[str]) -> None:
        """Refuse a forced failover when any instance to reboot is not Multi-AZ now (read from AWS).

        AWS refuses `ForceFailover` on a single-AZ instance only when the reboot is called, after approval,
        and a group reboot could then stop part way through the list.

        Raises:
            ForceFailoverNeedsMultiAzRefused: Names every instance without a standby.
        """
        without_standby = [
            identifier
            for identifier in identifiers
            if (described := lifecycle_api.instance_describe(context, identifier, "await_approval"))
            is not None
            and not described.get("MultiAZ", False)
        ]
        if not without_standby:
            return
        logger.error(
            "reboot_with_failover_refused",
            operation_id=str(context.operation.id),
            instances_without_standby=without_standby,
            fix_suggestion=ForceFailoverNeedsMultiAzRefused.fix_suggestion,
        )
        raise ForceFailoverNeedsMultiAzRefused(
            f"{', '.join(without_standby)} {'is' if len(without_standby) == 1 else 'are'} not Multi-AZ, "
            "so there is no standby to fail over to"
        )

    def review(self, context: StepContext) -> ActionReview:
        """The instances to reboot, each as a sentence; a forced failover says so.

        Raises:
            ForceFailoverNeedsMultiAzRefused: Failover was asked and an instance to reboot is not Multi-AZ.
        """
        identifiers = self._identifiers_to_reboot(context)
        if self._options(context).force_failover:
            self._refuse_failover_without_standby(context, identifiers)
        suffix = " with failover" if self._options(context).force_failover else ""
        return ActionReview(
            what=[f"reboot {identifier}{suffix}" for identifier in identifiers],
            identifiers=identifiers,
            requires_reboot=True,
            downtime=REBOOT_DOWNTIME,
            reconcile=False,
        )

    def _execute_await_approval(self, context: StepContext) -> StepOutcome:
        """The base review and pause; the identifiers are also kept where the review screen reads them."""
        outcome = super()._execute_await_approval(context)
        if outcome.status == StepOutcomeStatus.AWAITING_APPROVAL:
            context.operation.plan = {
                **context.operation.plan,
                REBOOT_IDENTIFIERS_PLAN_KEY: self.planned_review(context).identifiers,
            }
            context.session.commit()
        return outcome

    def perform(self, context: StepContext) -> dict[str, Any]:
        """Reboot each approved instance."""
        force_failover = self._options(context).force_failover
        identifiers = self.planned_review(context).identifiers
        for identifier in identifiers:
            direct_api.reboot_instance(context, identifier, force_failover, "perform")
        return {"rebooted": identifiers}

    def settled(self, context: StepContext) -> VerificationResult:
        """Every rebooted instance is available (or optimising storage); a terminal status stops the wait."""
        statuses = {
            identifier: direct_api.instance_status(context, identifier, "await_settled")
            for identifier in self.planned_review(context).identifiers
        }
        not_available = {
            identifier: status for identifier, status in statuses.items() if not status_allows_changes(status)
        }
        return VerificationResult(
            ok=not not_available,
            live=statuses,
            detail=describe_instance_statuses(not_available),
            terminal=any(status in TERMINAL_RESOURCE_STATUSES for status in not_available.values()),
        )


def _load_target_record(context: StepContext) -> ResourceRecord:
    """The operation's resource from the latest inventory.

    Raises:
        KeyError: The resource is not in the latest inventory.
    """
    record = load_resource(context.session, context.console_config, context.operation.resource_arn or "")
    if record is None:
        raise KeyError(f"{context.operation.resource_arn} is not in the latest inventory")
    return record


def _identifier_of_arn(arn: str) -> str:
    """The identifier at the end of an RDS ARN."""
    return arn.rsplit(":", 1)[-1]


def _is_cluster_arn(arn: str) -> bool:
    """Whether an RDS ARN names a cluster (`cluster`) rather than an instance (`db`)."""
    return arn.split(":")[5:6] == ["cluster"]


def _snapshot_scope_of_arn(arn: str) -> lifecycle_api.SnapshotScope:
    """Whether an RDS snapshot ARN names a cluster snapshot (`cluster-snapshot`) or an instance snapshot."""
    return "cluster" if arn.split(":")[5:6] == ["cluster-snapshot"] else "instance"


def _resource_status(context: StepContext, arn: str, step_name: str) -> str:
    """The status AWS reports now for the instance or cluster `arn` names."""
    identifier = _identifier_of_arn(arn)
    if _is_cluster_arn(arn):
        return lifecycle_api.cluster_status(context, identifier, step_name)[0]
    return direct_api.instance_status(context, identifier, step_name)


class ForeignOwnedSnapshotSourceRefused(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """The resource to snapshot is owned by other tooling, which leaves it read-only here."""

    diagnostic_kind = "foreign_owned_resource"
    title = "Resources owned by other tooling cannot be changed here"
    fix_suggestion = (
        "take the snapshot in the tooling that owns the resource, or allow direct changes to foreign-owned "
        "resources in this environment's configuration"
    )


class SimulatorCannotApplyMaintenance(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """The simulated account does not implement `ApplyPendingMaintenanceAction`."""

    diagnostic_kind = "simulator_unsupported"
    title = "The simulated account cannot apply maintenance"
    fix_suggestion = (
        "apply the maintenance action against a real AWS account; the simulator does not implement it"
    )


class ClusterFailoverExecutor(DirectActionExecutor):
    """Fail an Aurora cluster over to another member, and wait for the writer to change.

    The review names the current writer and the target. Every member must be available and none may be
    owned by other tooling (unless the environment allows direct changes to foreign-owned resources).
    """

    operation_kind = "cluster_failover"
    not_settled_diagnostic_kind = "cluster_not_available"
    not_settled_title = "The cluster did not finish failing over"

    def _target_member_identifier(self, context: StepContext) -> str | None:
        """The member the operator chose, or None to let AWS pick."""
        options = FailoverOptions.model_validate(context.operation.plan.get("options", {}))
        return options.target_member_identifier

    def review(self, context: StepContext) -> ActionReview:
        """Check the cluster can fail over and say from which writer to which member.

        The writer is read from AWS now, not from the inventory, which may be stale.

        Raises:
            KeyError: The cluster is not in the latest inventory.
            ValueError: Not a cluster, fewer than two members, a bad target, or a member not available.
            ForeignOwnedRebootRefused: A member is owned by other tooling.
        """
        record = _load_target_record(context)
        if record.kind != ResourceKind.CLUSTER:
            raise ValueError(f"{record.identifier} is not a cluster; a failover applies to clusters")
        members = list(record.relationships.get("member_identifiers", []))
        if len(members) < 2:
            raise ValueError(f"{record.identifier} has {len(members)} member; a failover needs at least two")
        cluster_state, writer = lifecycle_api.cluster_status(context, record.identifier, "await_approval")
        target = self._target_member_identifier(context)
        if target is not None:
            if target not in members:
                raise ValueError(f"{target} is not a member of {record.identifier}")
            if target == writer:
                raise ValueError(f"{target} is already the writer of {record.identifier}")
        if cluster_state != "available":
            raise ValueError(f"{record.identifier} is {cluster_state}; wait until it is available")
        for member in members:
            member_status = direct_api.instance_status(context, member, "await_approval")
            if member_status != "available":
                raise ValueError(f"{member} is {member_status}; wait until every member is available")
        refuse_foreign_owned_reboot(context, record.facts.get("member_instance_ownership", {}))
        return ActionReview(
            what=[f"fail over {record.identifier} from {writer} to {target or 'the member AWS picks'}"],
            identifiers=[record.identifier],
            requires_reboot=False,
            downtime=FAILOVER_DOWNTIME,
            reconcile=False,
        )

    def perform(self, context: StepContext) -> dict[str, Any]:
        """`FailoverDBCluster`, to the chosen member when there is one; the writer before is kept."""
        identifier = _identifier_of_arn(context.operation.resource_arn or "")
        target = self._target_member_identifier(context)
        _, writer_before = lifecycle_api.cluster_status(context, identifier, "perform")
        lifecycle_api.failover_cluster(context, identifier, target, "perform")
        return {
            "failed_over": identifier,
            "target_member_identifier": target,
            "writer_before": writer_before,
        }

    def settled(self, context: StepContext) -> VerificationResult:
        """The cluster is `available` and the writer changed: to the target when one was chosen."""
        identifier = _identifier_of_arn(context.operation.resource_arn or "")
        target = self._target_member_identifier(context)
        writer_before = (context.completed_step_output("perform") or {}).get("writer_before")
        cluster_state, writer = lifecycle_api.cluster_status(context, identifier, "await_settled")
        problems = []
        if cluster_state != "available":
            problems.append(f"{identifier} is {cluster_state}")
        if target is not None and writer != target:
            problems.append(f"the writer is {writer}, not yet {target}")
        elif target is None and writer == writer_before:
            problems.append(f"the writer is still {writer}")
        return VerificationResult(
            ok=not problems,
            live={"status": cluster_state, "writer_identifier": writer},
            detail="; ".join(problems),
            terminal=cluster_state in TERMINAL_RESOURCE_STATUSES,
        )


class ApplyPendingMaintenanceExecutor(DirectActionExecutor):
    """Opt an instance or cluster in to a pending maintenance action, or undo the opt-in.

    Applying now interrupts the resource, so the executor asks for the identifier to be typed at review
    time (the catalogue flag cannot depend on an option).
    """

    operation_kind = "apply_pending_maintenance"
    not_settled_diagnostic_kind = "resource_not_available"
    not_settled_title = "The resource did not return to available"

    def _options(self, context: StepContext) -> PendingMaintenanceOptions:
        return PendingMaintenanceOptions.model_validate(context.operation.plan.get("options", {}))

    def review(self, context: StepContext) -> ActionReview:
        """The chosen action must be pending on the resource; applying it now needs the identifier typed.

        Raises:
            KeyError: The resource is not in the latest inventory.
            ValueError: Not an instance or cluster, or the action is not pending on it.
        """
        record = _load_target_record(context)
        if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
            raise ValueError(f"{record.identifier} is not an instance or cluster")
        options = self._options(context)
        pending = pending_maintenance_for(context.session, record.arn, record.snapshot_id)
        pending_actions = sorted({row.action for row in pending})
        if options.action not in pending_actions:
            listing = ", ".join(pending_actions) if pending_actions else "none"
            raise ValueError(f"{options.action} is not pending on {record.identifier}; pending: {listing}")
        opt_in_sentence = {
            "immediate": "apply it now",
            "next-maintenance": "apply it in the next maintenance window",
            "undo-opt-in": "undo the opt-in",
        }[options.opt_in_type]
        immediate = options.opt_in_type == "immediate"
        if immediate:
            context.operation.approval_required_text = record.identifier
            context.session.commit()
        return ActionReview(
            what=[f"{options.action} on {record.identifier}: {opt_in_sentence}"],
            identifiers=[record.identifier],
            requires_reboot=False,
            downtime=(
                "the action's own interruption, now"
                if immediate
                else "none now; AWS applies it in the window"
            ),
            reconcile=False,
        )

    def perform(self, context: StepContext) -> dict[str, Any]:
        """`ApplyPendingMaintenanceAction`; on the simulator the step fails saying it is not implemented."""
        options = self._options(context)
        arn = context.operation.resource_arn or ""
        try:
            lifecycle_api.apply_pending_maintenance(
                context, arn, options.action, options.opt_in_type, "perform"
            )
        except SimulatorUnsupportedAction as error:
            raise SimulatorCannotApplyMaintenance(
                "the simulated account does not implement ApplyPendingMaintenanceAction, "
                f"so {options.action} was not submitted"
            ) from error
        return {"action": options.action, "opt_in_type": options.opt_in_type}

    def settled(self, context: StepContext) -> VerificationResult:
        """The resource is available (or optimising storage); a terminal status stops the wait at once."""
        arn = context.operation.resource_arn or ""
        resource_status = _resource_status(context, arn, "await_settled")
        return VerificationResult(
            ok=status_allows_changes(resource_status),
            live={"status": resource_status},
            detail=""
            if status_allows_changes(resource_status)
            else f"{_identifier_of_arn(arn)} is {resource_status}",
            terminal=resource_status in TERMINAL_RESOURCE_STATUSES,
        )


class TakeSnapshotExecutor(DirectActionExecutor):
    """Take a manual snapshot of an instance or cluster, tag it and wait for it to be available.

    The operation stays on the source resource (`resource_arn`), so it is listed among that resource's
    operations; the new snapshot's ARN goes in `plan["created_resource_arns"]`, which change history reads
    to attribute the snapshot's appearance in the inventory to this operation.
    """

    operation_kind = "snapshot_take"
    not_settled_diagnostic_kind = "snapshot_not_available"
    not_settled_title = "The snapshot did not become available"

    def _options(self, context: StepContext) -> TakeSnapshotOptions:
        return TakeSnapshotOptions.model_validate(context.operation.plan.get("options", {}))

    def review(self, context: StepContext) -> ActionReview:
        """The source must be available and not owned by other tooling, and the name must be free.

        Raises:
            KeyError: The resource is not in the latest inventory.
            ValueError: Not an instance or cluster, a cluster member, not available, or the name is taken.
            ForeignOwnedSnapshotSourceRefused: The source is owned by other tooling.
        """
        record = _load_target_record(context)
        if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
            raise ValueError(f"{record.identifier} is not an instance or cluster")
        cluster_identifier = record.relationships.get("cluster_identifier")
        if record.kind == ResourceKind.INSTANCE and cluster_identifier:
            raise ValueError(
                f"{record.identifier} is a member of {cluster_identifier}; snapshot the cluster instead"
            )
        if not status_allows_changes(record.status):
            raise ValueError(f"{record.identifier} is {record.status}; wait until it is available")
        if record.ownership.startswith(FOREIGN_OWNERSHIP_PREFIX) and not (
            context.target.allow_direct_changes_to_foreign_owned
        ):
            raise ForeignOwnedSnapshotSourceRefused(
                f"{record.identifier} is {record.ownership.replace('_', ' ')}; the console leaves resources "
                "owned by other tooling read-only"
            )
        snapshot_identifier = self._options(context).snapshot_identifier
        scope: lifecycle_api.SnapshotScope = "cluster" if record.kind == ResourceKind.CLUSTER else "instance"
        if lifecycle_api.snapshot_status(context, scope, snapshot_identifier, "await_approval") is not None:
            raise ValueError(f"a snapshot named {snapshot_identifier} already exists; choose another name")
        return ActionReview(
            what=[f"take a manual snapshot {snapshot_identifier} of {record.identifier}"],
            identifiers=[record.identifier],
            requires_reboot=False,
            downtime=SNAPSHOT_DOWNTIME,
            reconcile=False,
        )

    def perform(self, context: StepContext) -> dict[str, Any]:
        """Create the snapshot with its tags and point the operation at it."""
        options = self._options(context)
        source_arn = context.operation.resource_arn or ""
        scope: lifecycle_api.SnapshotScope = "cluster" if _is_cluster_arn(source_arn) else "instance"
        snapshot_arn = lifecycle_api.create_snapshot(
            context,
            scope,
            _identifier_of_arn(source_arn),
            options.snapshot_identifier,
            options.tags,
            "perform",
        )
        context.operation.plan = {
            **context.operation.plan,
            CREATED_RESOURCE_ARNS_PLAN_KEY: [
                *context.operation.plan.get(CREATED_RESOURCE_ARNS_PLAN_KEY, []),
                snapshot_arn,
            ],
        }
        context.session.commit()
        return {
            "snapshot_arn": snapshot_arn,
            "snapshot_identifier": options.snapshot_identifier,
            "snapshot_scope": scope,
        }

    def settled(self, context: StepContext) -> VerificationResult:
        """The snapshot is `available`; a terminal status stops the wait at once."""
        submitted = context.completed_step_output("perform") or {}
        snapshot_identifier = submitted.get("snapshot_identifier", "")
        snapshot_state = lifecycle_api.snapshot_status(
            context, submitted.get("snapshot_scope", "instance"), snapshot_identifier, "await_settled"
        )
        return VerificationResult(
            ok=snapshot_state == "available",
            live={"status": snapshot_state},
            detail=(
                "" if snapshot_state == "available" else f"snapshot {snapshot_identifier} is {snapshot_state}"
            ),
            terminal=snapshot_state is None or snapshot_state in TERMINAL_RESOURCE_STATUSES,
        )


class DeleteSnapshotExecutor(DirectActionExecutor):
    """Delete a manual snapshot and wait until AWS no longer lists it."""

    operation_kind = "snapshot_delete"
    not_settled_diagnostic_kind = "snapshot_still_present"
    not_settled_title = "The snapshot is still there"

    def review(self, context: StepContext) -> ActionReview:
        """Only an available manual snapshot can be deleted, and not undone.

        Raises:
            KeyError: The snapshot is not in the latest inventory.
            ValueError: Not a snapshot, not manual, or not available.
        """
        record = _load_target_record(context)
        if record.kind != ResourceKind.SNAPSHOT:
            raise ValueError(f"{record.identifier} is not a snapshot")
        snapshot_type = record.facts.get("snapshot_type")
        if snapshot_type != MANUAL_SNAPSHOT_TYPE:
            raise ValueError(
                f"{record.identifier} is an {snapshot_type} snapshot; only manual ones can be deleted here"
            )
        if record.status != "available":
            raise ValueError(f"{record.identifier} is {record.status}; wait until it is available")
        return ActionReview(
            what=[f"delete snapshot {record.identifier}; this cannot be undone"],
            identifiers=[record.identifier],
            requires_reboot=False,
            downtime="none",
            reconcile=False,
        )

    def perform(self, context: StepContext) -> dict[str, Any]:
        """`DeleteDBSnapshot` or `DeleteDBClusterSnapshot`."""
        arn = context.operation.resource_arn or ""
        identifier = _identifier_of_arn(arn)
        lifecycle_api.delete_snapshot(context, _snapshot_scope_of_arn(arn), identifier, "perform")
        return {"deleted": identifier}

    def settled(self, context: StepContext) -> VerificationResult:
        """AWS answers that the snapshot does not exist."""
        arn = context.operation.resource_arn or ""
        identifier = _identifier_of_arn(arn)
        snapshot_state = lifecycle_api.snapshot_status(
            context, _snapshot_scope_of_arn(arn), identifier, "await_settled"
        )
        return VerificationResult(
            ok=snapshot_state is None,
            live={"status": snapshot_state},
            detail="" if snapshot_state is None else f"snapshot {identifier} is {snapshot_state}",
        )


class ForeignOwnedResourceRefused(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """The resource is owned by other tooling, which leaves it read-only here."""

    diagnostic_kind = "foreign_owned_resource"
    title = "Resources owned by other tooling cannot be changed here"
    fix_suggestion = (
        "make the change in the tooling that owns the resource, or allow direct changes to foreign-owned "
        "resources in this environment's configuration"
    )


class NotAdoptedRefused(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """The action rewrites Terraform files for a resource Terraform does not manage yet."""

    diagnostic_kind = "not_adopted"
    title = "Resource is not under Terraform yet"
    fix_suggestion = (
        "adopt it into Terraform first (the Adopt resources action), then create the operation again"
    )


def _refuse_unchangeable_resource(context: StepContext, record: ResourceRecord) -> None:
    """Refuse a resource the console may not change: foreign-owned, or (Terraform mode) not adopted.

    Raises:
        ForeignOwnedResourceRefused: Owned by other tooling and the environment does not allow changes.
        NotAdoptedRefused: Terraform mode and the resource is not adopted.
    """
    if record.ownership.startswith(FOREIGN_OWNERSHIP_PREFIX) and not (
        context.target.allow_direct_changes_to_foreign_owned
    ):
        raise ForeignOwnedResourceRefused(
            f"{record.identifier} is {record.ownership.replace('_', ' ')}; the console leaves resources "
            "owned by other tooling read-only"
        )
    adopted = record.terraform_address is not None or record.ownership == "adopted"
    if not context.target.is_direct and not adopted:
        raise NotAdoptedRefused(
            f"{record.identifier} must be adopted before it can be changed in a Terraform-mode environment"
        )


class PromoteReplicaExecutor(DirectActionExecutor):
    """Promote a read replica to a standalone instance, then bring the rendered files in line.

    The replica and its source must both be available. Promotion ends replication for good and restarts the
    replica; the closing reconcile re-renders the replica as a standalone instance (the full attribute
    block, no `replicate_source_db`) from the inventory snapshot taken after the promotion.
    """

    operation_kind = "replica_promote"
    not_settled_diagnostic_kind = "replica_not_promoted"
    not_settled_title = "The replica did not become a standalone instance"

    def _options(self, context: StepContext) -> PromoteReplicaOptions:
        return PromoteReplicaOptions.model_validate(context.operation.plan.get("options", {}))

    def reconciles_by_default(self, operation: Operation) -> bool:
        """Always: the step list is stable before the review exists."""
        return True

    def review(self, context: StepContext) -> ActionReview:
        """Both instances must be available now (read from AWS, not the inventory) and changeable.

        Raises:
            KeyError: The replica is not in the latest inventory.
            ValueError: Not a replica, or the replica or its source is not available.
            ForeignOwnedResourceRefused: The replica is owned by other tooling.
            NotAdoptedRefused: Terraform mode and the replica is not adopted.
        """
        record = _load_target_record(context)
        source = record.relationships.get("read_replica_source_identifier")
        if record.kind != ResourceKind.INSTANCE or not source:
            raise ValueError(f"{record.identifier} is not a read replica")
        _refuse_unchangeable_resource(context, record)
        context.operation.approval_required_text = record.identifier
        context.session.commit()
        replica_status = direct_api.instance_status(context, record.identifier, "await_approval")
        if not status_allows_changes(replica_status):
            raise ValueError(f"{record.identifier} is {replica_status}; wait until it is available")
        source_identifier = _identifier_of_arn(source)
        if not source.startswith("arn:"):
            source_status = direct_api.instance_status(context, source_identifier, "await_approval")
            if not status_allows_changes(source_status):
                raise ValueError(
                    f"the source {source_identifier} is {source_status}; wait until it is available"
                )
        return ActionReview(
            what=[
                f"promote {record.identifier} to a standalone instance; "
                f"replication from {source_identifier} stops"
            ],
            identifiers=[record.identifier],
            requires_reboot=True,
            downtime=PROMOTE_DOWNTIME,
            reconcile=True,
        )

    def touched_addresses(self, context: StepContext) -> set[str]:
        """The replica's own address; promotion changes nothing else that is rendered."""
        record = _load_target_record(context)
        return {
            record.terraform_address
            or terraform_address_for(terraform_resource_type_for(record), record.identifier)
        }

    def perform(self, context: StepContext) -> dict[str, Any]:
        """`PromoteReadReplica` with the chosen backup retention."""
        identifier = _identifier_of_arn(context.operation.resource_arn or "")
        retention_days = self._options(context).backup_retention_period_days
        lifecycle_api.promote_read_replica(context, identifier, retention_days, "perform")
        return {"promoted": identifier, "backup_retention_period_days": retention_days}

    def settled(self, context: StepContext) -> VerificationResult:
        """The instance is `available` and AWS no longer lists a replication source for it.

        The simulated account (moto) keeps reporting the source after a promotion, so there the source is
        not looked at and `available` is enough.
        """
        identifier = _identifier_of_arn(context.operation.resource_arn or "")
        description = lifecycle_api.instance_describe(context, identifier, "await_settled")
        if description is None:
            return VerificationResult(
                ok=False, live={"status": None}, detail=f"{identifier} is no longer listed", terminal=True
            )
        instance_state = description["DBInstanceStatus"]
        source = description.get("ReadReplicaSourceDBInstanceIdentifier")
        problems = []
        if not status_allows_changes(instance_state):
            problems.append(f"{identifier} is {instance_state}")
        if source and not context.target.simulated:
            problems.append(f"{identifier} still replicates from {_identifier_of_arn(source)}")
        return VerificationResult(
            ok=not problems,
            live={"status": instance_state, "read_replica_source_identifier": source},
            detail="; ".join(problems),
            terminal=instance_state in TERMINAL_RESOURCE_STATUSES,
        )
