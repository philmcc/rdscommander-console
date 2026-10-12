"""Remove an Aurora reader, and decommission an instance or a cluster: the operations that delete resources.

`MemberRemovalChange` is a `DesiredChange` that takes one cluster member out of the rendered model
(Terraform mode: the plan deletes it) or deletes it through the API (direct mode). The executor lets the
plain delete of the touched address through the Terraform policy and refuses to touch the writer.

Decommission is two changes under one approval (`DecommissionExecutor`, a `ChainedChangeExecutor`): an
`AttributeChange` turns deletion protection off and names the final snapshot (Terraform reads that name from
its state when it deletes), then `ResourceDeleteChange` deletes the instance, or the cluster's members and
then the cluster, after AWS takes the final snapshot. It refuses before approval while anything the console
can see still depends on the resource. A read replica is deleted without a snapshot (its data lives on its
source); when its deletion protection is already off there is nothing to do before the delete, so the first
change's steps after the approval are skipped.

Example:
    >>> change = MemberRemovalChange("arn:aws:rds:eu-west-2:000000000000:db:demo-aurora-reader")
    >>> change.summary
    'remove Aurora reader'
"""

from __future__ import annotations

from typing import Any

from rdscommander.catalogue.catalogue import status_allows_changes
from rdscommander.lifecycle import direct_api as lifecycle_api
from rdscommander.lifecycle.options import DecommissionOptions
from rdscommander.logging_setup import get_logger
from rdscommander.operations.attribute_change import AttributeChange
from rdscommander.operations.chained_change import SECOND_PHASE, ChainedChangeExecutor
from rdscommander.operations.change_models import (
    ChangeDiagnosticError,
    ChangeImpact,
    ChangeSetEntry,
    DesiredChange,
    VerificationResult,
)
from rdscommander.operations.change_strategy import (
    CHANGE_ERRORS,
    FOREIGN_OWNERSHIP_PREFIX,
    ResourceChangeExecutor,
)
from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.parameters.direct_api import TERMINAL_RESOURCE_STATUSES
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.names import terraform_address_for
from rdscommander.terraform.render_input import RenderWorkspaceInput

logger = get_logger(__name__)

READER_REMOVAL_DOWNTIME = "none for the cluster; connections to this reader drop"
"""What the approver of a reader removal should expect."""
TERMINAL_STATUSES_WHILE_DELETING = TERMINAL_RESOURCE_STATUSES - {"deleting", "deleted"}
"""Statuses a deleting instance never recovers from; `deleting` itself is the expected way there."""
DECOMMISSION_PLAN_KEY = "decommission"
"""Plan entry with what the review found: identifier, kind, members and the final snapshot's name."""
FIRST_PHASE_STEPS_AFTER_APPROVAL = frozenset(
    {
        "apply",
        "verify_clean_plan",
        "commit_to_checkout",
        "refresh_resource",
        "apply_direct",
        "verify_direct",
        "record_to_checkout",
    }
)
"""The first change's steps after the approval: skipped when a replica's first change has nothing to do."""
__all__ = [
    "READER_REMOVAL_DOWNTIME",
    "ClusterMembersStillDeleting",
    "ClusterNotAvailable",
    "ClusterWriterCannotBeRemoved",
    "DecommissionExecutor",
    "DeletionProtectionIsOn",
    "DependantsBlockDecommission",
    "FinalSnapshotNameTaken",
    "MemberRemovalChange",
    "RemoveReaderExecutor",
    "ResourceDeleteChange",
    "ResourceNotAvailable",
    "decommission_downtime",
    "replica_decommission_downtime",
]


class ClusterWriterCannotBeRemoved(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """The instance is the writer of its cluster; only readers are removed here."""

    diagnostic_kind = "cluster_writer_cannot_be_removed"
    title = "The writer cannot be removed"
    fix_suggestion = "fail the cluster over to another member first, then remove the old writer as a reader"


class ClusterNotAvailable(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """AWS does not report the cluster as available with a writer, so no member can be removed safely."""

    diagnostic_kind = "cluster_not_available"
    title = "The cluster is not ready for a member to be removed"
    fix_suggestion = (
        "wait until the cluster is available with a writer (check its events in AWS), then try again"
    )


class DeletionProtectionIsOn(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """AWS refuses to delete an instance whose deletion protection is on."""

    diagnostic_kind = "deletion_protection_is_on"
    title = "Deletion protection is on"
    fix_suggestion = "turn deletion protection off (Deletion protection) and run this operation again"


class MemberRemovalChange:
    """Remove one reader from its Aurora cluster.

    Attributes:
        resource_arn: ARN of the reader instance.
        summary: What the change does, for titles.

    Example:
        >>> MemberRemovalChange(reader_arn).summary
        'remove Aurora reader'
    """

    def __init__(self, resource_arn: str) -> None:
        """Describe the removal; nothing is changed until a strategy runs it."""
        self.resource_arn = resource_arn
        self.summary = "remove Aurora reader"

    def _member(self, context: StepContext) -> tuple[ResourceRecord, str]:
        """The reader's record and the Terraform address it has or would have.

        Raises:
            KeyError: The instance is not in the latest inventory.
        """
        record = load_resource(context.session, context.console_config, self.resource_arn)
        if record is None:
            raise KeyError(f"{self.resource_arn} is not in the latest inventory")
        address = record.terraform_address or terraform_address_for(
            "aws_rds_cluster_instance", record.identifier
        )
        return record, address

    def ensure_removable(self, context: StepContext) -> ResourceRecord:
        """The reader's record, provided it is a cluster member that is not the writer.

        The inventory says which member is the writer (`facts["is_cluster_writer"]`).

        Raises:
            KeyError: The instance is not in the latest inventory.
            ValueError: The instance is not an Aurora cluster member.
            ClusterWriterCannotBeRemoved: The instance is its cluster's writer.
            DeletionProtectionIsOn: AWS would refuse the delete.
        """
        record, _ = self._member(context)
        cluster_identifier = record.relationships.get("cluster_identifier")
        if record.kind != ResourceKind.INSTANCE or not cluster_identifier:
            raise ValueError(f"{record.identifier} is not a member of an Aurora cluster")
        if record.facts.get("is_cluster_writer"):
            raise ClusterWriterCannotBeRemoved(f"{record.identifier} is the writer of {cluster_identifier}")
        if record.facts.get("deletion_protection"):
            raise DeletionProtectionIsOn(f"{record.identifier} has deletion protection turned on")
        return record

    def cluster_arn(self, context: StepContext) -> str:
        """The ARN of the reader's cluster, built from the reader's own ARN (same region and account)."""
        record, _ = self._member(context)
        cluster_identifier = str(record.relationships["cluster_identifier"])
        prefix = record.arn.rsplit(":", 2)[0]
        return f"{prefix}:cluster:{cluster_identifier}"

    def ensure_live_cluster(self, context: StepContext, step_name: str) -> str:
        """Read the cluster from AWS now and refuse unless it is available and the reader is not its writer.

        Runs before approval in both modes: the inventory may be older than a failover.

        Args:
            context: The running step's context.
            step_name: Step the AWS call is recorded under.

        Returns:
            The cluster's ARN, which history attributes the membership change to.

        Raises:
            ClusterNotAvailable: The cluster is not `available`, or lists no writer.
            ClusterWriterCannotBeRemoved: The reader is the writer now.
        """
        record, _ = self._member(context)
        cluster_identifier = str(record.relationships["cluster_identifier"])
        cluster_state, live_writer = lifecycle_api.cluster_status(context, cluster_identifier, step_name)
        if cluster_state != "available":
            raise ClusterNotAvailable(f"{cluster_identifier} is {cluster_state}; wait until it is available")
        if live_writer is None:
            raise ClusterNotAvailable(f"{cluster_identifier} lists no writer; wait until it has one")
        if live_writer == record.identifier:
            raise ClusterWriterCannotBeRemoved(
                f"{record.identifier} is the writer of {cluster_identifier} now (the inventory is stale)"
            )
        return self.cluster_arn(context)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """Only the reader itself."""
        return {self._member(context)[1]}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the render input without the member (a copy)."""
        updated = render_input.model_copy(deep=True)
        terraform_name = terraform_address_for("aws_rds_cluster_instance", self._identifier()).partition(".")[
            2
        ]
        updated.cluster_instances[:] = [
            member for member in updated.cluster_instances if member.terraform_name != terraform_name
        ]
        return updated

    def _identifier(self) -> str:
        """The instance identifier at the end of the ARN."""
        return self.resource_arn.rsplit(":", 1)[-1]

    def expected_impact(self, context: StepContext) -> ChangeImpact:
        """Terraform mode: nothing restarts, but connections to the reader drop."""
        return ChangeImpact(requires_reboot=False, downtime=READER_REMOVAL_DOWNTIME)

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """The member's presence, from something to nothing (`ensure_live_cluster` checks AWS).

        Raises:
            ClusterWriterCannotBeRemoved: The inventory says the instance is the writer.
        """
        record = self.ensure_removable(context)
        cluster_identifier = str(record.relationships["cluster_identifier"])
        return [
            ChangeSetEntry(
                address=self._member(context)[1],
                attribute="presence",
                current=f"member of {cluster_identifier}",
                desired=None,
                apply_type="immediate",
                requires_reboot=False,
                downtime=READER_REMOVAL_DOWNTIME,
            )
        ]

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Delete the reader without a final snapshot: it holds no data of its own."""
        record, _ = self._member(context)
        lifecycle_api.delete_instance(context, record.identifier, None, "apply_direct")
        return {"deleted": record.identifier}

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """AWS no longer lists the instance; a status it will not recover from stops the wait."""
        identifier = self._identifier()
        description = lifecycle_api.instance_describe(context, identifier, "verify_direct")
        if description is None:
            return VerificationResult(ok=True, live={"exists": False})
        instance_state = description["DBInstanceStatus"]
        return VerificationResult(
            ok=False,
            live={"exists": True, "status": instance_state},
            detail=f"{identifier} is {instance_state}",
            terminal=instance_state in TERMINAL_STATUSES_WHILE_DELETING,
        )


class RemoveReaderExecutor(ResourceChangeExecutor):
    """Delete an Aurora reader in both modes; Terraform mode lets the plain delete of the reader through."""

    operation_kind = "cluster_reader_remove"
    allows_delete = True

    def desired_change(self, context: StepContext) -> MemberRemovalChange:
        """The removal of the operation's reader."""
        return MemberRemovalChange(context.operation.resource_arn or "")

    def _check_before_approval(self, context: StepContext, step_name: str) -> StepOutcome | None:
        """The failed outcome when the reader cannot be removed now, else None; both modes run it.

        The inventory check (member, not the writer, no deletion protection) and the live cluster check run
        before any file is rendered or change set computed. The cluster, whose membership changes, is kept
        in `plan["affected_resource_arns"]` so change history explains that change by this operation; the
        touched address stays the reader alone, so the Terraform policy is not widened.
        """
        try:
            change = self.desired_change(context)
            change.ensure_removable(context)
            cluster_arn = change.ensure_live_cluster(context, step_name)
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, step_name, error)
        self._store_plan_values(context, affected_resource_arns=[cluster_arn])
        return None

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: refuse a reader that cannot be removed before any file is rendered."""
        refusal = self._check_before_approval(context, "render_desired")
        return refusal or super()._execute_render_desired(context)

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Direct mode: refuse a reader that cannot be removed before the change set is computed."""
        refusal = self._check_before_approval(context, "compute_change_set")
        return refusal or super()._execute_compute_change_set(context)

    def _execute_await_approval(self, context: StepContext) -> StepOutcome:
        """Ask for the reader's identifier to be typed, whatever created the operation, then pause."""
        record = load_resource(context.session, context.console_config, context.operation.resource_arn or "")
        if record is not None and context.operation.approval_required_text is None:
            context.operation.approval_required_text = record.identifier
            context.session.commit()
        return super()._execute_await_approval(context)


# ----- decommission ---------------------------------------------------------------------------------------


class DependantsBlockDecommission(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """Something the console can see still depends on the resource.

    Read replicas (instances or Aurora clusters, in any region), membership of a global database, or
    cluster members the console may not delete.
    """

    diagnostic_kind = "dependants_block_decommission"
    title = "Something still depends on this database"
    fix_suggestion = (
        "remove the cluster from its global database, promote or decommission the read replicas, or adopt or "
        "hand over the cluster members named in the detail, then decommission again"
    )


class FinalSnapshotNameRequired(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """Only a read replica is deleted without a final snapshot; everything else needs the name."""

    diagnostic_kind = "final_snapshot_name_required"
    title = "A final snapshot name is needed"
    fix_suggestion = (
        "give the final snapshot a name, then decommission again; only a read replica is deleted without one"
    )


class FinalSnapshotNameTaken(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """AWS refuses a final snapshot name that another snapshot already has."""

    diagnostic_kind = "final_snapshot_name_taken"
    title = "A snapshot with the final snapshot's name already exists"
    fix_suggestion = "choose another final snapshot name, then decommission again"


class ResourceNotAvailable(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """AWS does not report the resource as available, so it is not decommissioned now."""

    diagnostic_kind = "resource_not_available"
    title = "The resource is not available"
    fix_suggestion = (
        "wait until AWS reports it as available (check its events in AWS), then decommission again"
    )


class ClusterMembersStillDeleting(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """The cluster's members were deleted but AWS still lists them, so the cluster cannot be deleted yet."""

    diagnostic_kind = "cluster_members_still_deleting"
    title = "The cluster's members are still being deleted"
    fix_suggestion = (
        "wait until AWS no longer lists the members, delete the cluster in AWS with the final snapshot named "
        "in the detail, then refresh inventory and run Detect drift"
    )


def decommission_downtime(final_snapshot_identifier: str) -> str:
    """What the approver of a decommission should expect.

    Example:
        >>> decommission_downtime("demo-orders-final")
        'the database is deleted after the final snapshot demo-orders-final completes'
    """
    return f"the database is deleted after the final snapshot {final_snapshot_identifier} completes"


def replica_decommission_downtime(source_identifier: str) -> str:
    """What the approver of a read replica's decommission should expect.

    Example:
        >>> replica_decommission_downtime("demo-orders")
        'the read replica is deleted; its data lives on its source demo-orders, so no snapshot is taken'
    """
    return (
        f"the read replica is deleted; its data lives on its source {source_identifier}, so no snapshot is "
        "taken"
    )


DELETE_SUBMITTED_STATUSES = frozenset({"deleting", "deleted"})
"""Statuses AWS reports once a delete has been submitted; asking again would be refused."""


def _delete_not_yet_submitted(description: dict[str, Any] | None, status_key: str) -> bool:
    """True when AWS lists the resource and no delete of it has been submitted yet.

    Example:
        >>> _delete_not_yet_submitted({"DBInstanceStatus": "deleting"}, "DBInstanceStatus")
        False
    """
    return description is not None and description.get(status_key) not in DELETE_SUBMITTED_STATUSES


def _arn_prefix(resource_arn: str) -> str:
    """`arn:aws:rds:<region>:<account>` of an instance or cluster ARN."""
    return resource_arn.rsplit(":", 2)[0]


class ResourceDeleteChange:
    """Delete an instance, or a cluster with its members, after a final snapshot; a read replica without one.

    Attributes:
        resource_arn: ARN of the instance or cluster.
        identifier: Its identifier.
        resource_kind: `instance` or `cluster`.
        final_snapshot_identifier: Name of the snapshot AWS takes before the delete; None for a read replica,
            whose data lives on its source.
        member_identifiers: A cluster's members, deleted first without a snapshot of their own (the cluster
            holds the data); empty for an instance.
        replica_source_identifier: The source of a read replica; None otherwise.
        summary: What the change does, for titles and the review.

    Example:
        >>> ResourceDeleteChange(arn, "demo-temp", "instance", "demo-temp-final").summary  # doctest: +SKIP
        'delete demo-temp after the final snapshot demo-temp-final'
    """

    def __init__(
        self,
        resource_arn: str,
        identifier: str,
        resource_kind: str,
        final_snapshot_identifier: str | None,
        member_identifiers: list[str] | None = None,
        replica_source_identifier: str | None = None,
    ) -> None:
        """Describe the delete; nothing is changed until a strategy runs it."""
        self.resource_arn = resource_arn
        self.identifier = identifier
        self.resource_kind = resource_kind
        self.final_snapshot_identifier = final_snapshot_identifier
        self.member_identifiers = sorted(member_identifiers or [])
        self.replica_source_identifier = replica_source_identifier
        self.summary = (
            f"delete {identifier} after the final snapshot {final_snapshot_identifier}"
            if final_snapshot_identifier
            else f"delete the read replica {identifier} without a final snapshot"
        )

    def downtime(self) -> str:
        """What the approver should expect: the database goes after its final snapshot, a replica at once."""
        if self.final_snapshot_identifier:
            return decommission_downtime(self.final_snapshot_identifier)
        return replica_decommission_downtime(self.replica_source_identifier or "")

    @property
    def is_cluster(self) -> bool:
        """True when the change deletes a cluster (and its members)."""
        return self.resource_kind == "cluster"

    def _address(self) -> str:
        resource_type = "aws_rds_cluster" if self.is_cluster else "aws_db_instance"
        return terraform_address_for(resource_type, self.identifier)

    def _member_addresses(self) -> list[str]:
        return [terraform_address_for("aws_rds_cluster_instance", m) for m in self.member_identifiers]

    def member_arns(self) -> list[str]:
        """The members' ARNs (same region and account as the cluster)."""
        return [f"{_arn_prefix(self.resource_arn)}:db:{member}" for member in self.member_identifiers]

    def final_snapshot_arn(self) -> str | None:
        """The ARN the final snapshot will have; None when the delete takes no snapshot (a read replica)."""
        if not self.final_snapshot_identifier:
            return None
        snapshot_type = "cluster-snapshot" if self.is_cluster else "snapshot"
        return f"{_arn_prefix(self.resource_arn)}:{snapshot_type}:{self.final_snapshot_identifier}"

    def touched_addresses(self, context: StepContext) -> set[str]:
        """The resource and, for a cluster, every member: all of them are deleted."""
        return {self._address(), *self._member_addresses()}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the render input without the resource and its members (a copy)."""
        updated = render_input.model_copy(deep=True)
        terraform_name = self._address().partition(".")[2]
        if self.is_cluster:
            member_names = {address.partition(".")[2] for address in self._member_addresses()}
            updated.clusters[:] = [c for c in updated.clusters if c.terraform_name != terraform_name]
            updated.cluster_instances[:] = [
                m for m in updated.cluster_instances if m.terraform_name not in member_names
            ]
        else:
            updated.instances[:] = [i for i in updated.instances if i.terraform_name != terraform_name]
        return updated

    def expected_impact(self, context: StepContext) -> ChangeImpact:
        """Terraform mode: the database is gone once the final snapshot is taken (a replica at once)."""
        return ChangeImpact(requires_reboot=False, downtime=self.downtime())

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """One `presence` entry per deleted resource, members first as they are deleted first."""
        downtime = self.downtime()
        entries = [
            ChangeSetEntry(
                address=address,
                attribute="presence",
                current=f"member of {self.identifier}",
                desired=None,
                apply_type="immediate",
                requires_reboot=False,
                downtime=downtime,
            )
            for address in self._member_addresses()
        ]
        entries.append(
            ChangeSetEntry(
                address=self._address(),
                attribute="presence",
                current=f"{self.resource_kind} {self.identifier}",
                desired=None,
                apply_type="immediate",
                requires_reboot=False,
                downtime=downtime,
            )
        )
        return entries

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Delete the members without a snapshot, wait until they are gone, then the resource with one.

        Each delete is skipped when AWS no longer lists the resource or lists it as `deleting` (an earlier
        attempt submitted it), so the step can run again after a failure or restart part-way.

        Raises:
            ClusterMembersStillDeleting: The members are still listed when the wait is used up.
        """
        deleted: list[str] = []
        for member in self.member_identifiers:
            description = lifecycle_api.instance_describe(context, member, "apply_direct")
            if _delete_not_yet_submitted(description, "DBInstanceStatus"):
                lifecycle_api.delete_instance(context, member, None, "apply_direct")
                deleted.append(member)
        if self.is_cluster:
            self._wait_until_members_are_gone(context)
            description = lifecycle_api.cluster_describe(context, self.identifier, "apply_direct")
            if _delete_not_yet_submitted(description, "Status"):
                lifecycle_api.delete_cluster(
                    context, self.identifier, self.final_snapshot_identifier, "apply_direct"
                )
                deleted.append(self.identifier)
        elif _delete_not_yet_submitted(
            lifecycle_api.instance_describe(context, self.identifier, "apply_direct"), "DBInstanceStatus"
        ):
            lifecycle_api.delete_instance(
                context, self.identifier, self.final_snapshot_identifier, "apply_direct"
            )
            deleted.append(self.identifier)
        logger.info(
            "decommission_deletes_submitted",
            operation_id=str(context.operation.id),
            deleted=deleted,
            final_snapshot_identifier=self.final_snapshot_identifier,
        )
        return {"deleted": deleted, "final_snapshot_identifier": self.final_snapshot_identifier}

    def _wait_until_members_are_gone(self, context: StepContext) -> None:
        """Poll until AWS lists none of the members; AWS refuses to delete a cluster that has members."""
        timeout_seconds = context.settings.direct_verify_timeout_seconds
        poll_seconds = context.settings.direct_verify_poll_seconds
        waited_seconds = 0.0
        while True:
            remaining = [
                member
                for member in self.member_identifiers
                if lifecycle_api.instance_describe(context, member, "apply_direct") is not None
            ]
            if not remaining:
                return
            if waited_seconds >= timeout_seconds:
                logger.error(
                    "decommission_members_still_listed",
                    operation_id=str(context.operation.id),
                    cluster_identifier=self.identifier,
                    remaining_members=remaining,
                    fix_suggestion=ClusterMembersStillDeleting.fix_suggestion,
                )
                raise ClusterMembersStillDeleting(
                    f"{', '.join(remaining)} still listed after {int(waited_seconds)} seconds; delete "
                    f"{self.identifier} with the final snapshot {self.final_snapshot_identifier} once they "
                    "are gone"
                )
            context.services.sleep(poll_seconds)
            waited_seconds += poll_seconds

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """AWS lists none of the deleted resources; a status it will not recover from stops the wait."""
        still_listed: dict[str, str] = {}
        for member in self.member_identifiers:
            description = lifecycle_api.instance_describe(context, member, "verify_direct")
            if description is not None:
                still_listed[member] = description["DBInstanceStatus"]
        if self.is_cluster:
            description = lifecycle_api.cluster_describe(context, self.identifier, "verify_direct")
            if description is not None:
                still_listed[self.identifier] = description["Status"]
        else:
            description = lifecycle_api.instance_describe(context, self.identifier, "verify_direct")
            if description is not None:
                still_listed[self.identifier] = description["DBInstanceStatus"]
        if not still_listed:
            return VerificationResult(ok=True, live={"exists": False})
        return VerificationResult(
            ok=False,
            live={"still_listed": still_listed},
            detail="; ".join(f"{identifier} is {state}" for identifier, state in still_listed.items()),
            terminal=any(state in TERMINAL_STATUSES_WHILE_DELETING for state in still_listed.values()),
        )


class DecommissionExecutor(ChainedChangeExecutor):
    """Turn deletion protection off and name the final snapshot, then delete; one approval for both.

    The review refuses while the console can see something that depends on the resource (an instance's
    read replicas; a cluster member owned by other tooling, or not adopted in Terraform mode), while AWS
    does not report it as available, and when a snapshot already has the final snapshot's name. A read
    replica is deleted without a final snapshot (the name in the options is not used); when its first
    change has nothing to do (deletion protection already off), that change's steps after the approval are
    skipped, so only the delete runs and is committed.
    """

    operation_kind = "decommission"
    allows_delete = True
    first_phase_may_be_empty = True
    """Deletion protection may already be off: the first change then has nothing to send in direct mode."""

    def _options(self, context: StepContext) -> DecommissionOptions:
        return DecommissionOptions.model_validate(context.operation.plan["options"])

    def _record(self, context: StepContext) -> ResourceRecord:
        """The resource's record in the latest inventory.

        Raises:
            KeyError: The resource is not in the latest inventory.
        """
        resource_arn = context.operation.resource_arn or ""
        record = load_resource(context.session, context.console_config, resource_arn)
        if record is None:
            raise KeyError(f"{resource_arn} is not in the latest inventory; refresh inventory first")
        return record

    def _subject(self, context: StepContext) -> dict[str, Any]:
        """What the review found (`plan["decommission"]`); later steps never read the inventory again.

        Raises:
            KeyError: The review has not run (the plan has no `decommission` entry).
        """
        subject = context.operation.plan.get(DECOMMISSION_PLAN_KEY)
        if not subject:
            raise KeyError("the decommission was not reviewed; create the operation again")
        return dict(subject)

    # --- the two changes -------------------------------------------------------------------------

    def first_change(self, context: StepContext) -> AttributeChange:
        """Deletion protection off, and the final snapshot named in the rendered model (render only).

        A read replica takes no final snapshot: only its deletion protection is turned off.
        """
        subject = self._subject(context)
        resource_type = "aws_rds_cluster" if subject["resource_kind"] == "cluster" else "aws_db_instance"
        if not subject.get("final_snapshot_identifier"):
            return AttributeChange(
                resource_arn=context.operation.resource_arn or "",
                address=terraform_address_for(resource_type, subject["identifier"]),
                values={"deletion_protection": False},
                summary=f"turn deletion protection off on {subject['identifier']}",
            )
        return AttributeChange(
            resource_arn=context.operation.resource_arn or "",
            address=terraform_address_for(resource_type, subject["identifier"]),
            values={
                "deletion_protection": False,
                "final_snapshot_identifier": subject["final_snapshot_identifier"],
            },
            summary=(
                f"turn deletion protection off on {subject['identifier']} and name its final snapshot "
                f"{subject['final_snapshot_identifier']}"
            ),
        )

    def second_change(self, context: StepContext) -> ResourceDeleteChange:
        """The delete of the resource (and a cluster's members) with the final snapshot."""
        subject = self._subject(context)
        return ResourceDeleteChange(
            resource_arn=context.operation.resource_arn or "",
            identifier=subject["identifier"],
            resource_kind=subject["resource_kind"],
            final_snapshot_identifier=subject.get("final_snapshot_identifier"),
            member_identifiers=list(subject.get("member_identifiers", [])),
            replica_source_identifier=subject.get("read_replica_source_identifier"),
        )

    # --- the review ------------------------------------------------------------------------------

    def _check_before_approval(self, context: StepContext, step_name: str) -> StepOutcome | None:
        """Refuse what cannot be decommissioned now, in both modes, before anything is rendered or computed.

        On success the plan keeps what was found (`plan["decommission"]`), so the steps after the approval
        work from what was approved, not from an inventory that changes as the resource is deleted.
        """
        try:
            options = self._options(context)
            record = self._record(context)
            if record.kind == ResourceKind.CLUSTER:
                subject = self._review_cluster(context, record, step_name)
            elif record.kind == ResourceKind.INSTANCE:
                subject = self._review_instance(context, record, step_name)
            else:
                raise ValueError(f"{record.identifier} is not an instance or cluster")
            scope: lifecycle_api.SnapshotScope = (
                "cluster" if record.kind == ResourceKind.CLUSTER else "instance"
            )
            name = (
                None if subject.get("read_replica_source_identifier") else options.final_snapshot_identifier
            )
            if name is None and not subject.get("read_replica_source_identifier"):
                raise FinalSnapshotNameRequired(
                    f"{record.identifier} is not a read replica, so AWS takes a final snapshot before the "
                    "delete and the snapshot needs a name"
                )
            if (
                name is not None
                and lifecycle_api.snapshot_status(context, scope, name, step_name) is not None
            ):
                raise FinalSnapshotNameTaken(f"a {scope} snapshot named {name} already exists")
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, step_name, error)
        subject["final_snapshot_identifier"] = name
        subject["confirm_dependants_removed"] = options.confirm_dependants_removed
        self._store_plan_values(context, **{DECOMMISSION_PLAN_KEY: subject})
        logger.info(
            "decommission_reviewed",
            operation_id=str(context.operation.id),
            identifier=record.identifier,
            resource_kind=subject["resource_kind"],
            member_identifiers=subject["member_identifiers"],
        )
        return None

    def _review_instance(
        self, context: StepContext, record: ResourceRecord, step_name: str
    ) -> dict[str, Any]:
        """The instance's subject, refused when it is a cluster member, has replicas, or is busy.

        A read replica's subject names its source (`read_replica_source_identifier`): it is deleted without a
        final snapshot, since its data lives on the source.
        """
        cluster_identifier = record.relationships.get("cluster_identifier")
        if cluster_identifier:
            raise ValueError(
                f"{record.identifier} is a member of {cluster_identifier}; decommission the cluster, or "
                "remove this reader"
            )
        description = lifecycle_api.instance_describe(context, record.identifier, step_name)
        if description is None:
            raise KeyError(f"{record.identifier} no longer exists in AWS; refresh inventory")
        replicas = sorted(
            set(record.relationships.get("replica_identifiers") or [])
            | set(description.get("ReadReplicaDBInstanceIdentifiers") or [])
            | set(description.get("ReadReplicaDBClusterIdentifiers") or [])
        )
        if replicas:
            raise DependantsBlockDecommission(
                f"{record.identifier} has read replicas that replicate from it: {', '.join(replicas)}"
            )
        instance_state = description.get("DBInstanceStatus")
        if not status_allows_changes(instance_state):
            raise ResourceNotAvailable(f"{record.identifier} is {instance_state}; wait until it is available")
        source = description.get("ReadReplicaSourceDBInstanceIdentifier") or record.relationships.get(
            "read_replica_source_identifier"
        )
        return {
            "identifier": record.identifier,
            "resource_kind": "instance",
            "member_identifiers": [],
            "read_replica_source_identifier": source or None,
        }

    def _review_cluster(self, context: StepContext, record: ResourceRecord, step_name: str) -> dict[str, Any]:
        """The cluster's subject with its members as AWS lists them now, refused when one may not be deleted.

        A member owned by other tooling is never deleted here; in Terraform mode every member must be
        adopted, because Terraform deletes only what it manages and AWS keeps a cluster that has members.
        A member of a global database, or a cluster other clusters replicate from, is refused too: AWS
        refuses `DeleteDBCluster` for a global database member only after every instance is deleted, which
        would leave a cluster with no instances, no final snapshot and deletion protection off.
        """
        description = lifecycle_api.cluster_describe(context, record.identifier, step_name)
        if description is None:
            raise KeyError(f"{record.identifier} no longer exists in AWS; refresh inventory")
        dependants: list[str] = []
        global_cluster_identifier = description.get("GlobalClusterIdentifier")
        if global_cluster_identifier:
            dependants.append(
                f"it is a member of the global database {global_cluster_identifier} (remove it from the "
                "global database first)"
            )
        replica_cluster_identifiers = sorted(description.get("ReadReplicaIdentifiers") or [])
        if replica_cluster_identifiers:
            dependants.append(f"read replicas replicate from it: {', '.join(replica_cluster_identifiers)}")
        if dependants:
            raise DependantsBlockDecommission(f"{record.identifier}: {'; '.join(dependants)}")
        live_members = sorted(
            member["DBInstanceIdentifier"] for member in description.get("DBClusterMembers", [])
        )
        ownership: dict[str, str] = dict(record.facts.get("member_instance_ownership") or {})
        blockers: list[str] = []
        for member in live_members:
            member_ownership = ownership.get(member)
            if member_ownership is None:
                blockers.append(f"{member} is not in the inventory yet (refresh inventory)")
            elif member_ownership.startswith(FOREIGN_OWNERSHIP_PREFIX):
                blockers.append(f"{member} is {member_ownership.replace('_', ' ')}")
            elif not context.target.is_direct and member_ownership != "adopted":
                blockers.append(f"{member} is not adopted into Terraform (adopt it first)")
        if blockers:
            raise DependantsBlockDecommission(
                f"{record.identifier} has members the console may not delete: {'; '.join(blockers)}"
            )
        cluster_state = description.get("Status")
        if not status_allows_changes(cluster_state):
            raise ResourceNotAvailable(f"{record.identifier} is {cluster_state}; wait until it is available")
        return {
            "identifier": record.identifier,
            "resource_kind": "cluster",
            "member_identifiers": live_members,
        }

    def _second_phase_review_entries(
        self, context: StepContext, second: DesiredChange
    ) -> list[ChangeSetEntry] | None:
        """The delete's entries, one per deleted resource: they need no AWS call, so the review shows them."""
        return second.change_set(context)

    def _first_change_still_applied(self, context: StepContext, commit_phrase: str) -> tuple[str, str] | None:
        """A failed delete leaves deletion protection off; the diagnostic says so and how to put it back.

        None when the first change was skipped (a replica whose protection was already off): nothing changed.
        """
        try:
            identifier = self._subject(context)["identifier"]
        except KeyError:
            return super()._first_change_still_applied(context, commit_phrase)
        if self._first_phase_skipped(context):
            return None
        return (
            f"Deletion protection on {identifier} is now off ({commit_phrase}); this failure does not turn "
            "it back on.",
            f"deletion protection on {identifier} is now off ({commit_phrase}); turn it back on with "
            "Deletion protection unless you retry the decommission",
        )

    def _addresses_allowed_to_be_deleted(self, context: StepContext, touched: set[str]) -> frozenset[str]:
        """Only the second change deletes; a first-phase plan that deletes anything is refused."""
        if self.current_phase != SECOND_PHASE:
            return frozenset()
        return super()._addresses_allowed_to_be_deleted(context, touched)

    def _first_phase_skipped(self, context: StepContext) -> bool:
        """True for a read replica whose first change has nothing to do: its review found no change.

        Direct mode: the first change set is empty. Terraform mode: the first plan has no changes (the state
        already has deletion protection off and the final snapshot skipped).
        """
        subject = context.operation.plan.get(DECOMMISSION_PLAN_KEY) or {}
        if not subject.get("read_replica_source_identifier"):
            return False
        if context.target.is_direct:
            return not context.operation.plan.get("change_set")
        first_plan_summary = context.operation.plan.get("plan_summary")
        if first_plan_summary is None:
            return False
        return all(
            change.get("actions") == ["no-op"] for change in first_plan_summary.get("resource_changes", [])
        )

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """The first change's steps after the approval are skipped when that change has nothing to do."""
        if step_name in FIRST_PHASE_STEPS_AFTER_APPROVAL and self._first_phase_skipped(context):
            subject = self._subject(context)
            return StepObservation(
                state=ObservationState.SATISFIED,
                output={
                    "skipped": (
                        f"deletion protection on the read replica {subject['identifier']} is already off; "
                        "nothing to do before the delete"
                    )
                },
            )
        return super().observe(step_name, context)

    def _execute_await_approval(self, context: StepContext) -> StepOutcome:
        """Ask for the identifier to be typed, whatever created the operation, then pause."""
        if context.operation.approval_required_text is None:
            context.operation.approval_required_text = self._subject(context)["identifier"]
            context.session.commit()
        return super()._execute_await_approval(context)

    # --- the second phase: history attribution and the record of what AWS already did ------------

    def _name_what_the_delete_affects(self, context: StepContext) -> None:
        """Members disappear and the final snapshot appears; history attributes both to this operation.

        A read replica's delete takes no snapshot, so nothing is listed as created.
        """
        change = self.second_change(context)
        created = list(context.operation.plan.get("created_resource_arns") or [])
        snapshot_arn = change.final_snapshot_arn()
        if snapshot_arn is not None and snapshot_arn not in created:
            created.append(snapshot_arn)
        self._store_plan_values(
            context, affected_resource_arns=change.member_arns(), created_resource_arns=created
        )

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Direct mode: before the second change set, name the members and the final snapshot for history."""
        if self.current_phase == SECOND_PHASE:
            try:
                self._name_what_the_delete_affects(context)
            except CHANGE_ERRORS as error:
                return self._fail_for_change_error(context, "compute_change_set", error)
        return super()._execute_compute_change_set(context)

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: before the second render, name the members and the final snapshot for history."""
        if self.current_phase == SECOND_PHASE:
            try:
                self._name_what_the_delete_affects(context)
            except CHANGE_ERRORS as error:
                return self._fail_for_change_error(context, "render_desired", error)
        return super()._execute_render_desired(context)

    def _execute_apply_direct(self, context: StepContext) -> StepOutcome:
        """Direct mode: a delete that fails part-way leaves AWS ahead of the record; the plan says so."""
        if self.current_phase != SECOND_PHASE:
            return super()._execute_apply_direct(context)
        try:
            outcome = super()._execute_apply_direct(context)
        except Exception:
            self._mark_applied_without_record(context)
            raise
        if outcome.status != StepOutcomeStatus.COMPLETED:
            self._mark_applied_without_record(context)
        return outcome
