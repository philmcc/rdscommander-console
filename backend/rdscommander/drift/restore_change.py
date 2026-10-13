"""Restoring a drifted resource: the change that reverses a drift finding, run by the change strategy.

`drift_resolve` with the restore choice hands its steps to `_RestoreExecutor`, a `ResourceChangeExecutor`
whose desired change is `RestoreFromRecordChange`: put the recorded values back. Terraform mode plans and
applies the recorded files; direct mode writes the recorded values through the RDS API, tells the approver
how AWS will apply each of them, and reads them back until AWS reports them or a status that will never
converge.

Example:
    >>> restore_step_names(direct_target)[-2:]  # doctest: +SKIP
    ['record_to_checkout', 'close_findings']
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from sqlalchemy import select

from rdscommander.config.models import ResolvedTarget
from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import DriftFinding
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.drift.detect_executor import RECORD_FINDING_KIND
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import (
    ChangeDiagnosticError,
    ChangeImpact,
    ChangeSetEntry,
    DesiredChange,
    VerificationResult,
)
from rdscommander.operations.change_strategy import DIRECT_STEPS, TERRAFORM_STEPS, ResourceChangeExecutor
from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.parameters.changes import family_metadata_for
from rdscommander.parameters.direct_api import (
    NEVER_RESTORED_ATTRIBUTES,
    NO_DOWNTIME,
    PARAMETER_GROUP_TYPES,
    TERMINAL_RESOURCE_STATUSES,
    UnsupportedRestoreError,
    apply_attribute_values,
    apply_profile_for,
    apply_profile_for_values,
    apply_profile_if_known,
    downtime_states_no_interruption,
    ensure_values_restorable,
    read_attribute_values,
    read_pending_modifications,
    read_resource_status,
)
from rdscommander.parameters.metadata import FamilyMetadata
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.record import replace_resource
from rdscommander.terraform.render_input import RenderWorkspaceInput

logger = get_logger(__name__)

CLOSE_FINDINGS_STEP = "close_findings"


class RestoreNotPossibleError(ChangeDiagnosticError):
    """A restore cannot be done at all; the fix suggestion points at accepting the drift instead."""

    diagnostic_kind = "restore_not_possible"
    title = "This drift cannot be restored"
    fix_suggestion = "the resource no longer exists; accept the drift instead"


def _scalar(value: Any) -> Any:
    """A parameter value as a scalar; tolerates `{"value": ...}` as an older record held it."""
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def _impact_not_known_in_advance(attribute: str) -> str:
    """The impact of restoring an attribute the console has no apply profile for."""
    return f"not known in advance for {attribute}; the plan shows how Terraform applies it"


class RestoreFromRecordChange:
    """The reverse of a record drift finding: put the recorded values back.

    Attributes:
        resource_arn: ARN of the drifted resource.
        address: Terraform address of the resource.
        attributes: The finding's `{address, attribute, recorded, live}` entries.
        recorded_input: The committed record's input; Terraform mode puts the resource back as it is there.
        summary: What the change does, for titles.
    """

    summary = "restore recorded values"

    def __init__(
        self,
        resource_arn: str,
        address: str,
        attributes: list[dict[str, Any]],
        recorded_input: RenderWorkspaceInput,
    ) -> None:
        self.resource_arn = resource_arn
        self.address = address
        self.attributes = attributes
        self.recorded_input = recorded_input

    def touched_addresses(self, context: StepContext) -> set[str]:
        """Only the drifted resource."""
        return {self.address}

    def _family_metadata(self, context: StepContext) -> FamilyMetadata | None:
        """The recorded parameter group's family metadata; None for other resources or an unknown family.

        Without it every restored parameter is written to apply at the next reboot, which AWS accepts for
        any parameter, and the review says a reboot is needed.
        """
        resource_type, _, terraform_name = self.address.partition(".")
        if resource_type not in PARAMETER_GROUP_TYPES:
            return None
        groups = (
            self.recorded_input.instance_parameter_groups
            if resource_type == "aws_db_parameter_group"
            else self.recorded_input.cluster_parameter_groups
        )
        family = next((group.family for group in groups if group.terraform_name == terraform_name), None)
        if family is None:
            return None
        scope = "instance" if resource_type == "aws_db_parameter_group" else "cluster"
        try:
            return family_metadata_for(context, family, scope)
        except KeyError as error:
            logger.warning(
                "restore_family_metadata_unavailable",
                operation_id=str(context.operation.id),
                family=family,
                scope=scope,
                error=str(error),
                fix_suggestion="the restore applies every parameter at the next reboot; reboot to apply them",
            )
            return None

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the drifted resource as the record has it."""
        return replace_resource(render_input, self.address, self.recorded_input)

    def expected_impact(self, context: StepContext) -> ChangeImpact:
        """What putting the recorded values back costs, from the attribute profiles (no AWS call).

        Terraform mode has no change set, so this is what the review states. Each drifted attribute is
        described by its profile in `ATTRIBUTE_APPLY_PROFILES` (parameters by their family, tags as tags); an
        attribute the console has no profile for is said to be unknown in advance, so the approver reads
        the plan for it.

        Returns:
            Whether a reboot is needed and the downtime in words, `none` when nothing is interrupted.
        """
        metadata = self._family_metadata(context)
        requires_reboot = False
        downtimes: list[str] = []
        for attribute in (str(entry["attribute"]) for entry in self.attributes):
            profile = apply_profile_if_known(self.address, attribute, metadata)
            if profile is None:
                downtime = _impact_not_known_in_advance(attribute)
            else:
                requires_reboot = requires_reboot or profile.requires_reboot
                downtime = profile.downtime
            if not downtime_states_no_interruption(downtime) and downtime not in downtimes:
                downtimes.append(downtime)
        return ChangeImpact(requires_reboot=requires_reboot, downtime="; ".join(downtimes) or NO_DOWNTIME)

    def interruptions_when_applied(self, context: StepContext) -> list[str]:
        """What the restore interrupts at the moment it is applied, from the attribute profiles (no AWS call).

        A restart, a failover or other downtime that happens immediately counts; a value that waits for the
        next reboot does not (the review still says the reboot is needed). An attribute without a profile
        counts too: how Terraform applies it is not known in advance.

        Returns:
            One description per distinct interruption; empty when applying interrupts nothing.
        """
        metadata = self._family_metadata(context)
        interruptions: list[str] = []
        for attribute in (str(entry["attribute"]) for entry in self.attributes):
            profile = apply_profile_if_known(self.address, attribute, metadata)
            if profile is None:
                interruption = _impact_not_known_in_advance(attribute)
            elif interrupts_service_when_applied(profile.apply_type, profile.downtime):
                interruption = profile.downtime
            else:
                continue
            if interruption not in interruptions:
                interruptions.append(interruption)
        return interruptions

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """The attributes whose live value differs from the recorded one, read afresh.

        Each entry says how AWS applies the write and what the approver should expect, from the attribute
        profiles. When AWS has modifications queued on the resource, a `pending_modifications_will_apply`
        warning names them, because writing with `ApplyImmediately` applies the queue too.

        Raises:
            UnsupportedRestoreError: A drifted attribute is one the console cannot write back, or one AWS
                never takes back (`NEVER_RESTORED_ATTRIBUTES`, an engine version); it is refused here, before
                approval is asked for.
        """
        desired_values = {a["attribute"]: _scalar(a["recorded"]) for a in self.attributes}
        for attribute in desired_values:
            if attribute in NEVER_RESTORED_ATTRIBUTES:
                logger.error(
                    "restore_of_never_restored_attribute_refused",
                    address=self.address,
                    attribute=attribute,
                    fix_suggestion=UnsupportedRestoreError.fix_suggestion,
                )
                raise UnsupportedRestoreError(attribute)
        ensure_values_restorable(self.address, desired_values)
        live = read_attribute_values(
            context, self.address, list(desired_values), step_name="compute_change_set"
        )
        entries = []
        metadata = self._family_metadata(context)
        for attribute, desired in desired_values.items():
            if live.get(attribute) == desired:
                continue
            profile = apply_profile_for_values(
                attribute, apply_profile_for(self.address, attribute, metadata), live.get(attribute), desired
            )
            entries.append(
                ChangeSetEntry(
                    address=self.address,
                    attribute=attribute,
                    current=live.get(attribute),
                    desired=desired,
                    apply_type=profile.apply_type,
                    requires_reboot=profile.requires_reboot,
                    downtime=profile.downtime,
                )
            )
        if entries:
            self._warn_about_queued_modifications(context)
        return entries

    def _warn_about_queued_modifications(self, context: StepContext) -> None:
        """Record a warning when AWS holds modifications this change would apply together with its own."""
        queued = read_pending_modifications(context, self.address, step_name="compute_change_set")
        if not queued:
            return
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name="compute_change_set",
            severity="warning",
            diagnostic_kind="pending_modifications_will_apply",
            title="Queued modifications will be applied together with this change",
            detail=(
                f"{self.resource_arn} has modifications AWS has not applied yet: "
                f"{json.dumps(queued, sort_keys=True, default=str)}"
            ),
            fix_suggestion=(
                "the restore is applied immediately and AWS applies the queued modifications with it; reject "
                "this operation and cancel the queued modifications in AWS if they should not be applied"
            ),
        )
        context.session.commit()

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Write the recorded values back through the RDS API, each parameter as its family applies it."""
        return apply_attribute_values(
            context,
            self.address,
            {e.attribute: e.desired for e in entries},
            metadata=self._family_metadata(context),
        )

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """Read the attributes back; the restore holds when each has the recorded value.

        Instances and clusters also report their status: one that will never converge (a failed
        modification, a full disk, a deleted resource) makes the result terminal. Parameter groups have no
        status and are never terminal.
        """
        live = read_attribute_values(context, self.address, [e.attribute for e in entries])
        mismatched = [e.attribute for e in entries if live.get(e.attribute) != e.desired]
        if not mismatched:
            return VerificationResult(ok=True, live=live)
        status = read_resource_status(context, self.address)
        terminal = status in TERMINAL_RESOURCE_STATUSES
        detail = f"still not at the recorded value: {', '.join(mismatched)}"
        if terminal:
            detail = (
                f"{detail}; AWS reports the resource as {status}, which it will not recover from by waiting"
            )
        return VerificationResult(ok=False, live=live, pending=[], detail=detail, terminal=terminal)


def interrupts_service_when_applied(apply_type: str, downtime: str) -> bool:
    """Whether a write interrupts service when it is applied: immediate, with downtime other than none.

    A write that applies at the next reboot interrupts nothing now; the reboot is what interrupts.

    Example:
        >>> interrupts_service_when_applied("immediate", "a restart of the instance")
        True
        >>> interrupts_service_when_applied("pending-reboot", "a reboot of the instance applies it")
        False
    """
    return apply_type == "immediate" and not downtime_states_no_interruption(downtime)


class _RestoreExecutor(ResourceChangeExecutor):
    """The change executor behind the restore choice of `drift_resolve`.

    A restore that interrupts service when it is applied (a restart, a failover, other immediate downtime)
    asks the approver to type the resource's identifier, whatever the catalogue's destructive and
    disruptive flags say about `drift_resolve` in general. Values that wait for the next reboot do not ask
    for it; the review states the reboot (`requires_reboot` and the downtime) all the same.
    """

    operation_kind = "drift_resolve"
    refuses_open_record_drift = False  # restoring is how the drift is resolved

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Direct mode: the change set, then the identifier requirement when an entry interrupts service."""
        outcome = super()._execute_compute_change_set(context)
        if outcome.status == StepOutcomeStatus.COMPLETED:
            interruptions = [
                str(entry["downtime"])
                for entry in context.operation.plan.get("change_set", [])
                if interrupts_service_when_applied(str(entry["apply_type"]), str(entry["downtime"]))
            ]
            self._require_identifier_when_service_is_interrupted(context, interruptions)
        return outcome

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: the files, then the identifier requirement when the restore interrupts service."""
        outcome = super()._execute_render_desired(context)
        if outcome.status == StepOutcomeStatus.COMPLETED:
            change = self.desired_change(context)
            if isinstance(change, RestoreFromRecordChange):
                self._require_identifier_when_service_is_interrupted(
                    context, change.interruptions_when_applied(context)
                )
        return outcome

    def _require_identifier_when_service_is_interrupted(
        self, context: StepContext, interruptions: list[str]
    ) -> None:
        """Make the approver type the resource's identifier when applying the restore interrupts service."""
        if not interruptions:
            return
        resource_arn = context.operation.resource_arn or ""
        record = load_resource(context.session, context.console_config, resource_arn)
        identifier = record.identifier if record is not None else resource_arn.rsplit(":", 1)[-1]
        context.operation.approval_required_text = identifier
        context.session.commit()
        logger.info(
            "restore_requires_typed_identifier",
            operation_id=str(context.operation.id),
            identifier=identifier,
            downtime=interruptions,
        )

    def _guard_resource(self, context: StepContext, step_name: str) -> StepOutcome | None:
        """A finding keyed by Terraform address is for a resource that left AWS: it can only be accepted."""
        if not (context.operation.resource_arn or "").startswith("arn:"):
            return self._fail_for_change_error(
                context,
                step_name,
                RestoreNotPossibleError(
                    f"{context.operation.resource_arn} no longer exists in AWS, so there is nothing to "
                    "restore"
                ),
            )
        return super()._guard_resource(context, step_name)

    def desired_change(self, context: StepContext) -> DesiredChange:
        """The reverse of the resource's open drift finding.

        Raises:
            ValueError: The resource has no open drift or the workspace has no record to restore from.
        """
        findings = list(
            context.session.execute(
                select(DriftFinding)
                .where(
                    DriftFinding.resource_arn == context.operation.resource_arn,
                    DriftFinding.resolved_at_utc.is_(None),
                )
                .order_by(DriftFinding.id.desc())
            ).scalars()
        )
        if not findings:
            raise ValueError(f"{context.operation.resource_arn} has no open drift to restore")
        finding = next((f for f in findings if f.finding_kind == RECORD_FINDING_KIND), findings[0])
        recorded_input = self._recorded_input(context)
        if recorded_input is None:
            raise ValueError("there is no record to restore from; adopt the resources into Terraform first")
        address = context.operation.plan.get("address") or next(
            (a["address"] for a in finding.attributes if a.get("address")), ""
        )
        if not address:
            raise ValueError(f"cannot tell which Terraform resource {context.operation.resource_arn} is")
        return RestoreFromRecordChange(
            finding.resource_arn, str(address), list(finding.attributes), recorded_input
        )


class RestoreHost(Protocol):
    """What `DriftResolveExecutor` offers the restore steps."""

    config_store: ConfigStoreProtocol

    def _accept_close_findings(self, context: StepContext) -> StepOutcome:
        """Close the open findings of the operation's resource."""
        ...


def restore_step_names(target: ResolvedTarget) -> list[str]:
    """Steps for the restore choice of `drift_resolve`: the mode's strategy, then closing the findings.

    Args:
        target: The operation's target; its change-management mode picks the strategy.

    Returns:
        The strategy's step names followed by `close_findings`.

    Example:
        >>> restore_step_names(direct_target)[-2:]  # doctest: +SKIP
        ['record_to_checkout', 'close_findings']
    """
    return [*(DIRECT_STEPS if target.is_direct else TERRAFORM_STEPS), CLOSE_FINDINGS_STEP]


def observe_restore_step(executor: RestoreHost, step_name: str, context: StepContext) -> StepObservation:
    """Observe one restore step on behalf of `DriftResolveExecutor`.

    Args:
        executor: The `DriftResolveExecutor`; it supplies the configuration store.
        step_name: A name from `restore_step_names`.
        context: The step context.

    Returns:
        The observation.
    """
    if step_name == CLOSE_FINDINGS_STEP:
        completed = context.step_already_completed(step_name)
        return StepObservation(state=ObservationState.SATISFIED if completed else ObservationState.ABSENT)
    return _RestoreExecutor(executor.config_store).observe(step_name, context)


def execute_restore_step(executor: RestoreHost, step_name: str, context: StepContext) -> StepOutcome:
    """Run one restore step on behalf of `DriftResolveExecutor`.

    Args:
        executor: The `DriftResolveExecutor`; it closes the findings itself.
        step_name: A name from `restore_step_names`.
        context: The step context.

    Returns:
        The step's outcome.
    """
    if step_name == CLOSE_FINDINGS_STEP:
        return executor._accept_close_findings(context)
    return _RestoreExecutor(executor.config_store).execute(step_name, context)


def on_restore_cancelled(executor: RestoreHost, context: StepContext) -> bool:
    """Clean up a rejected restore: its Terraform-mode files are removed again.

    Args:
        executor: The `DriftResolveExecutor`.
        context: The context the runner built for the cancelled operation.

    Returns:
        True when the cleanup is done; False when another console holds the files.
    """
    return _RestoreExecutor(executor.config_store).on_cancelled(context)
