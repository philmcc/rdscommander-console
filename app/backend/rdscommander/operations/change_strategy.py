"""One executor base for every operation that changes a resource, in Terraform or direct mode.

An operation that changes a resource says what it wants once, as a `DesiredChange`. This module runs it
through the strategy the environment uses:

* Terraform mode makes sure the state bucket exists, renders the files, plans, pauses for approval,
  applies the approved plan, verifies the plan is clean, commits the files and the record, and takes a
  new inventory snapshot (a snapshot it cannot take does not undo the committed change).
* Direct mode reads the resource afresh, shows the change set, pauses for approval, calls the AWS API,
  reads the values back until AWS reports them (waiting while AWS applies asynchronously), takes a new
  inventory snapshot and commits the files and the record.

In both modes the approval pause is mandatory, the checkout guard runs first, resources owned by other
tooling are read-only unless the environment allows otherwise, the console commits under the operator's
identity and never pushes, and the commit message is `<title> (<operator>, approved by <approver>)`.

The record (`console-record.json`) describes committed files only, so neither strategy writes it before the
change has happened: Terraform mode writes it when it commits, direct mode when it records the result.
Both build it as the recorded input with only the touched addresses replaced, so the drift of every other
resource stays visible.

Example:
    >>> class SetWorkMemExecutor(ResourceChangeExecutor):  # doctest: +SKIP
    ...     operation_kind = "parameter_group_set_work_mem"
    ...
    ...     def desired_change(self, context):
    ...         return SetWorkMem(context.operation.resource_arn, context.operation.plan["options"]["value"])
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.aws.errors import diagnose_exception
from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import (
    ChangeSet,
    DriftFinding,
    InventorySnapshot,
    Operation,
    OperationStepAttempt,
    TerraformRun,
    TerraformWorkspace,
    utc_now,
)
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.drift.open_findings import (
    OPEN_DRIFT_DIAGNOSTIC_KIND,
    describe_open_drift,
    open_record_drift_findings,
    resolve_drift_first,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.inventory.refresh_executor import refresh_after_terraform_commit, refresh_target_now
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import (
    ApplyType,
    ChangeDiagnosticError,
    ChangeImpact,
    ChangeSetEntry,
    DesiredChange,
    VerificationResult,
)
from rdscommander.operations.enums import SECOND_PHASE_STEP_PREFIX, ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.repo.commit import GitCommitError, commit_paths, operation_commit_message
from rdscommander.repo.guard import check_checkout
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.plan_parser import PlanSummary, redact_plan_json, summarise_plan_json
from rdscommander.terraform.policy import evaluate_change_policy
from rdscommander.terraform.record import (
    RECORD_FILE_NAME,
    read_record,
    replace_resource,
    resource_identifiers,
)
from rdscommander.terraform.render_input import RenderWorkspaceInput, build_render_input
from rdscommander.terraform.renderer import render_workspace_files, write_workspace_files
from rdscommander.terraform.runner import mark_terraform_apply_invoked
from rdscommander.terraform.state_addresses import record_terraform_changes
from rdscommander.terraform.state_bucket import ensure_state_bucket
from rdscommander.terraform.workspace_fingerprint import (
    WORKSPACE_FILES_FINGERPRINT_KEY,
    operation_owner_check,
    workspace_files_fingerprint,
)
from rdscommander.terraform.workspace_revert import (
    revert_workspace,
    revert_workspace_of_cancelled_operation,
)
from rdscommander.terraform.workspaces import (
    adopted_terraform_addresses,
    ensure_workspace_record,
    workspace_directory,
)

logger = get_logger(__name__)

TERRAFORM_STEPS = [
    "check_checkout",
    "ensure_state_bucket",
    "render_desired",
    "init",
    "plan",
    "policy_check",
    "await_approval",
    "apply",
    "verify_clean_plan",
    "commit_to_checkout",
    "refresh_resource",
]
DIRECT_STEPS = [
    "check_checkout",
    "compute_change_set",
    "policy_check",
    "await_approval",
    "apply_direct",
    "verify_direct",
    "refresh_resource",
    "record_to_checkout",
]
STEPS_SATISFIED_BY_PRIOR_COMPLETION = (
    "ensure_state_bucket",
    "render_desired",
    "init",
    "plan",
    "policy_check",
    "compute_change_set",
    "apply",
    "apply_direct",
    "verify_direct",
    "verify_clean_plan",
    "refresh_resource",
    "commit_to_checkout",
    "record_to_checkout",
    "record_as_built",
)
CLOSING_REFRESH_STEP_NAMES = ("refresh_resource", f"{SECOND_PHASE_STEP_PREFIX}refresh_resource")
"""Steps that refresh the inventory after a change, including a chained operation's second one."""
PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN = (0, 2)
FOREIGN_OWNERSHIP_PREFIX = "foreign_"
STEPS_THAT_MAY_LEAVE_REWRITTEN_FILES = ("render_desired", "init", "plan", "policy_check")
"""Terraform steps that run before anything is applied: a failure in them reverts the workspace files."""
REVERTED_SENTENCE = "The workspace files were reverted to the last commit."
SUBMITTED_TO_AWS_SENTENCE = (
    "The change was submitted to AWS and may still be applying; the record does not describe it yet."
)
RECOVER_AFTER_SUBMISSION = (
    "refresh inventory; when the values show, run Detect drift and accept the drift into the record"
)
__all__ = [
    "DIRECT_STEPS",
    "TERRAFORM_STEPS",
    "ApplyType",
    "ChangeDiagnosticError",
    "ChangeImpact",
    "ChangeSetEntry",
    "DesiredChange",
    "RecordUnreadableError",
    "ResourceChangeExecutor",
    "VerificationResult",
    "close_drift_findings_for_addresses",
    "with_touched_replaced",
]


class RecordUnreadableError(ChangeDiagnosticError):
    """`console-record.json` exists but cannot be read."""

    diagnostic_kind = "record_unreadable"
    title = "The Terraform record file cannot be read"
    fix_suggestion = (
        "restore console-record.json from git (git checkout -- <file>), then create the operation again"
    )


CHANGE_ERRORS = (KeyError, ValueError, ChangeDiagnosticError)
"""Errors that mean the change cannot be worked out; each becomes a diagnostic instead of a crash."""

DEFERRED_APPLY_TYPES = ("pending-reboot", "pending-window")
"""Apply types AWS holds until a later event; verification accepts them as pending once submitted."""


def with_touched_replaced(
    recorded: RenderWorkspaceInput | None, touched: set[str], source: RenderWorkspaceInput
) -> RenderWorkspaceInput:
    """The recorded input with only the touched addresses taken from `source`.

    Without a record there is nothing to keep, so `source` is the whole record. Every other resource keeps
    what the record says, so its drift stays visible.

    Args:
        recorded: The committed record's input, or None when nothing has been recorded yet.
        touched: Terraform addresses to take from `source` (replaced, added or removed).
        source: A fresh render to take the touched resources from.

    Returns:
        The input the files and the record are written from.

    Example:
        >>> with_touched_replaced(None, {"aws_db_instance.demo_orders"}, fresh) is fresh  # doctest: +SKIP
        True
    """
    if recorded is None:
        return source
    result = recorded
    for address in sorted(touched):
        result = replace_resource(result, address, source)
    return result


def close_drift_findings_for_addresses(session: Session, operation: Operation, addresses: set[str]) -> int:
    """Resolve, by `operation`, the open drift findings on `addresses` in the operation's target.

    Once a change is committed the record describes the touched resources as they now are, and (Terraform
    mode) the plan after it was clean, so their findings no longer hold. A finding belongs to a touched
    resource when it is keyed by a touched address or one of its attributes names one.

    Args:
        session: Control database session; committed here.
        operation: The operation that resolves the findings; its `target_key` scopes them.
        addresses: Terraform addresses the operation changed.

    Returns:
        How many findings were closed.

    Example:
        >>> addresses = {"aws_db_instance.demo_orders"}
        >>> close_drift_findings_for_addresses(session, operation, addresses)  # doctest: +SKIP
        1
    """
    if not addresses:
        return 0
    closed_keys: list[str] = []
    for finding in session.execute(
        select(DriftFinding).where(
            DriftFinding.target_key == operation.target_key,
            DriftFinding.resolved_at_utc.is_(None),
        )
    ).scalars():
        finding_addresses = {
            attribute.get("address") for attribute in finding.attributes if isinstance(attribute, dict)
        }
        if finding.resource_arn in addresses or finding_addresses & addresses:
            finding.resolved_at_utc = utc_now()
            finding.resolved_by_operation_id = operation.id
            closed_keys.append(finding.resource_arn)
    session.commit()
    if closed_keys:
        logger.info(
            "drift_findings_closed_by_change",
            operation_id=str(operation.id),
            target_key=operation.target_key,
            closed_findings=sorted(closed_keys),
        )
    return len(closed_keys)


class ResourceChangeExecutor:
    """Runs a `DesiredChange` through the environment's strategy. Subclasses implement `desired_change`.

    Subclasses that also create or delete resources set `allows_create` or `allows_delete`; the Terraform
    policy then lets a plain create or delete of the touched addresses through. Every strategy pauses at
    `await_approval`; approval is never optional.
    """

    operation_kind = "abstract"
    allows_create = False
    allows_delete = False
    refuses_open_record_drift = True
    """A change is refused while a resource it touches has open record drift; a restore resolves it."""

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read the live configuration from `config_store` whenever a step needs it."""
        self.config_store = config_store

    def desired_change(self, context: StepContext) -> DesiredChange:
        """Build the change from the operation's plan; subclasses override.

        Raises:
            KeyError: The plan names something that does not exist.
            ValueError: The change cannot be made; the message is shown to the operator.
        """
        raise NotImplementedError

    # --- step plumbing -------------------------------------------------------------------------

    def step_names(self, operation: Operation) -> list[str]:
        """The direct steps in a direct-mode environment, the Terraform steps otherwise."""
        target = self.config_store.current.target(operation.target_key or "")
        return list(DIRECT_STEPS if target.is_direct else TERRAFORM_STEPS)

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """Completed steps stay complete; approval is satisfied once an operator approved."""
        if step_name == "check_checkout":
            # The guard protects the checkout of the machine that will write files, so a resume on
            # another console must run it again against its own checkout.
            previous = context.completed_step_output(step_name) or {}
            same_runner = previous.get("runner_identity") == context.runner_identity
            return StepObservation(
                state=ObservationState.SATISFIED if same_runner else ObservationState.ABSENT
            )
        if step_name == "await_approval":
            approved = context.operation.approved_at_utc is not None
            return StepObservation(state=ObservationState.SATISFIED if approved else ObservationState.ABSENT)
        if step_name in STEPS_SATISFIED_BY_PRIOR_COMPLETION and context.step_already_completed(step_name):
            return StepObservation(state=ObservationState.SATISFIED)
        return StepObservation(state=ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Run `_execute_<step_name>`.

        An exception raised by a Terraform step that runs before anything is applied first puts the
        workspace files back as the last commit has them; the runner then turns it into the diagnostic.
        """
        handler = getattr(self, f"_execute_{step_name}")
        if step_name not in STEPS_THAT_MAY_LEAVE_REWRITTEN_FILES or context.target.is_direct:
            return handler(context)
        try:
            return handler(context)
        except Exception:
            revert_workspace(context, self._workspace(context), step_name)
            raise

    def on_cancelled(self, context: StepContext) -> bool:
        """Clean up after the operation was rejected while waiting for approval.

        Direct mode wrote nothing before approval. Terraform mode rendered the desired files, which are
        removed again so that the checkout guard does not refuse the next operation.

        Returns:
            True when the cleanup is done; False when this console is not the one that holds the files.
        """
        if context.target.is_direct:
            return True
        return revert_workspace_of_cancelled_operation(context, self._workspace(context))

    # --- plan entries ----------------------------------------------------------------------------

    def _plan_value(self, context: StepContext, name: str, default: Any = None) -> Any:
        return context.operation.plan.get(name, default)

    def _store_plan_values(self, context: StepContext, **values: Any) -> None:
        """Write plan entries; the plan is replaced as a whole because in-place edits are not tracked."""
        context.operation.plan = {**context.operation.plan, **values}
        context.session.commit()

    def _entries(self, context: StepContext) -> list[ChangeSetEntry]:
        return [ChangeSetEntry.model_validate(e) for e in self._plan_value(context, "change_set", [])]

    # --- shared helpers --------------------------------------------------------------------------

    def _workspace(self, context: StepContext) -> TerraformWorkspace:
        workspace = ensure_workspace_record(context.session, context.target, context.console_config)
        context.session.commit()
        return workspace

    def _workspace_dir(self, context: StepContext) -> Path:
        return workspace_directory(context.settings.repo_root, self._workspace(context))

    def _fail(
        self,
        context: StepContext,
        step_name: str,
        diagnostic_kind: str,
        title: str,
        detail: str,
        fix_suggestion: str,
    ) -> StepOutcome:
        """Record a diagnostic (which logs the fix suggestion) and fail the step."""
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name=step_name,
            severity="error",
            diagnostic_kind=diagnostic_kind,
            title=title,
            detail=detail,
            fix_suggestion=fix_suggestion,
        )
        context.session.commit()
        return StepOutcome(
            status=StepOutcomeStatus.FAILED,
            detail=detail,
            fix_suggestion=fix_suggestion,
            diagnostic_recorded=True,
        )

    def _fail_for_change_error(self, context: StepContext, step_name: str, error: Exception) -> StepOutcome:
        """The diagnostic for an error raised while working out what the change is.

        A `ChangeDiagnosticError` carries its own diagnostic; any other `KeyError` or `ValueError` becomes
        `change_not_possible` with its message as both detail and fix suggestion.
        """
        if isinstance(error, ChangeDiagnosticError):
            return self._fail(
                context, step_name, error.diagnostic_kind, error.title, str(error), error.fix_suggestion
            )
        message = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
        return self._fail(
            context, step_name, "change_not_possible", "This change is not possible", message, message
        )

    def _fail_and_revert(
        self,
        context: StepContext,
        step_name: str,
        diagnostic_kind: str,
        title: str,
        detail: str,
        fix_suggestion: str,
    ) -> StepOutcome:
        """Put the workspace back as the last commit has it, then fail with a diagnostic that says so.

        Used for every Terraform failure before the apply, so that a refused or failed change never leaves
        rewritten files behind for the checkout guard to trip over.
        """
        reverted = revert_workspace(context, self._workspace(context), step_name)
        sentence = REVERTED_SENTENCE if reverted else self._revert_failed_sentence(context)
        full_detail = f"{detail}\n\n{sentence}" if detail else sentence
        return self._fail(context, step_name, diagnostic_kind, title, full_detail, fix_suggestion)

    def _revert_failed_sentence(self, context: StepContext) -> str:
        relative_dir = Path(self._workspace(context).workspace_relative_dir).as_posix()
        return (
            f"Reverting the workspace files failed; run `git checkout -- {relative_dir}` "
            "before the next operation."
        )

    def _guard_resource(self, context: StepContext, step_name: str) -> StepOutcome | None:
        """Refuse foreign-owned resources (unless allowed) and, in Terraform mode, unadopted ones.

        Returns None when the resource may be changed; when the environment allows changing a foreign-owned
        resource the plan carries `foreign_owner_banner` for the review.
        """
        record = load_resource(context.session, context.console_config, context.operation.resource_arn or "")
        if record is None:
            return self._fail(
                context,
                step_name,
                "resource_not_in_inventory",
                "Resource is not in the latest inventory",
                f"{context.operation.resource_arn} was not found in the latest snapshot",
                "refresh inventory and create the operation again",
            )
        if record.ownership.startswith(FOREIGN_OWNERSHIP_PREFIX):
            if not context.target.allow_direct_changes_to_foreign_owned:
                return self._fail(
                    context,
                    step_name,
                    "foreign_owned_resource",
                    "Resource is owned by other tooling",
                    f"{record.identifier} is {record.ownership.replace('_', ' ')}; the console leaves it "
                    "read-only",
                    "change it in the tooling that owns it, or allow direct changes to foreign-owned "
                    "resources in this environment's configuration",
                )
            owner = record.ownership.removeprefix(FOREIGN_OWNERSHIP_PREFIX)
            context.operation.plan = {**context.operation.plan, "foreign_owner_banner": owner}
            context.session.commit()
        adopted = record.terraform_address is not None or record.ownership == "adopted"
        if not context.target.is_direct and not adopted:
            return self._fail(
                context,
                step_name,
                "not_adopted",
                "Resource is not under Terraform yet",
                f"{record.identifier} must be adopted before it can be changed in a Terraform-mode "
                "environment",
                "adopt it into Terraform first (the Adopt resources action), then create the operation again",
            )
        return None

    def _refuse_open_record_drift(
        self, context: StepContext, step_name: str, touched: list[str]
    ) -> StepOutcome | None:
        """The failed outcome when a touched resource has open record drift, else None.

        Both strategies render or record the touched resources from the latest inventory, which holds the
        drifted value; the commit would claim an approval for it. The drift is resolved first instead.
        """
        if not self.refuses_open_record_drift:
            return None
        findings = open_record_drift_findings(context.session, context.target.target_key, addresses=touched)
        if not findings:
            return None
        return self._fail(
            context,
            step_name,
            OPEN_DRIFT_DIAGNOSTIC_KIND,
            "A resource this change touches has drift that is not resolved",
            f"{describe_open_drift(findings)}. The change would record the drifted value as part of an "
            "approved change.",
            f"{resolve_drift_first(findings)} (Resolve drift: accept what AWS has into the record, or "
            "restore AWS to the record), then create the operation again",
        )

    def _latest_snapshot(self, context: StepContext) -> InventorySnapshot:
        snapshot = latest_snapshot_for_target(context.session, context.target.target_key)
        if snapshot is None:
            raise ValueError(
                f"{context.target.target_key} has never been inventoried; refresh inventory first"
            )
        return snapshot

    def _refuse_stale_snapshot(self, context: StepContext, step_name: str) -> StepOutcome | None:
        """The failed outcome when the latest snapshot is older than `max_inventory_age_minutes`.

        A change rendered or computed from an old snapshot could undo what happened to the resource since,
        so the operation asks for a refresh first, as the adoption and the accept of drift do.
        """
        target = context.target
        snapshot = latest_snapshot_for_target(context.session, target.target_key)
        if snapshot is None:
            return self._fail(
                context,
                step_name,
                "no_inventory_snapshot",
                "There is no inventory snapshot to change from",
                f"{target.target_key} has never been inventoried",
                "refresh inventory for this environment, then create the operation again",
            )
        maximum_snapshot_age = timedelta(minutes=context.settings.max_inventory_age_minutes)
        if datetime.now(UTC) - snapshot.captured_at_utc > maximum_snapshot_age:
            return self._fail(
                context,
                step_name,
                "stale_inventory",
                "Inventory snapshot is too old to change from",
                f"latest snapshot for {target.target_key} was captured "
                f"{snapshot.captured_at_utc.isoformat()}; the limit is "
                f"{context.settings.max_inventory_age_minutes} minutes",
                "refresh inventory for this environment, then create the operation again",
            )
        unrefreshed_operation_id = self._change_not_in_snapshot(context, snapshot)
        if unrefreshed_operation_id is not None:
            return self._fail(
                context,
                step_name,
                "inventory_behind_change",
                "The inventory does not show the last change yet",
                f"operation {unrefreshed_operation_id} changed {target.target_key} after the latest snapshot "
                f"({snapshot.captured_at_utc.isoformat()}) and could not refresh the inventory; a change "
                "worked out from that snapshot could undo it",
                f"run Refresh inventory on the {target.environment_slug} environment, then create the "
                "operation again",
            )
        return None

    def _change_not_in_snapshot(self, context: StepContext, snapshot: InventorySnapshot) -> uuid.UUID | None:
        """The id of a change on the target whose closing refresh failed after `snapshot` was taken, or None.

        A Terraform-mode change that committed but could not refresh (see `_execute_refresh_resource`) is
        not in the inventory until someone refreshes; the next change must not be worked out without it.
        """
        return context.session.execute(
            select(OperationStepAttempt.operation_id)
            .join(Operation, Operation.id == OperationStepAttempt.operation_id)
            .where(
                Operation.target_key == context.target.target_key,
                Operation.id != context.operation.id,
                OperationStepAttempt.step_name.in_(CLOSING_REFRESH_STEP_NAMES),
                OperationStepAttempt.outcome_status == StepOutcomeStatus.COMPLETED.value,
                OperationStepAttempt.output["refreshed"].astext == "false",
                OperationStepAttempt.finished_at_utc > snapshot.captured_at_utc,
            )
            .order_by(OperationStepAttempt.finished_at_utc.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _build_input(
        self,
        context: StepContext,
        snapshot: InventorySnapshot,
        *,
        include_all: bool,
        overlay: Callable[[RenderWorkspaceInput], RenderWorkspaceInput] | None = None,
    ) -> RenderWorkspaceInput:
        """Render `snapshot` for the resources under the workspace, with `overlay` applied if given."""
        workspace = self._workspace(context)
        return build_render_input(
            context.session,
            context.target,
            context.console_config,
            snapshot,
            set(),
            adopted_terraform_addresses(context.session, workspace),
            include_imports=False,
            simulated_endpoint_url=context.settings.simulated_aws_endpoint_url,
            include_all=include_all,
            overlay=overlay,
        )

    def _recorded_input(self, context: StepContext) -> RenderWorkspaceInput | None:
        """The committed record's input, or None when nothing has been recorded yet."""
        directory = self._workspace_dir(context)
        try:
            recorded = read_record(directory)
        except ValueError as error:
            raise RecordUnreadableError(f"{directory / RECORD_FILE_NAME}: {error}") from error
        return recorded.render_input if recorded is not None else None

    def _with_touched_replaced(
        self, recorded: RenderWorkspaceInput | None, touched: set[str], source: RenderWorkspaceInput
    ) -> RenderWorkspaceInput:
        """The recorded input with only the touched addresses from `source` (`with_touched_replaced`)."""
        return with_touched_replaced(recorded, touched, source)

    def _desired_terraform_input(
        self, context: StepContext, change: DesiredChange, snapshot: InventorySnapshot
    ) -> RenderWorkspaceInput:
        """What the committed files will describe: the record with the touched resources as desired."""
        fresh = self._build_input(context, snapshot, include_all=False, overlay=change.apply_to_render_input)
        return self._with_touched_replaced(
            self._recorded_input(context), change.touched_addresses(context), fresh
        )

    def _write_files_and_record(
        self, context: StepContext, record_input: RenderWorkspaceInput, snapshot_id: int
    ) -> list[Path]:
        """The single writer: the rendered files and the record beside them."""
        files = render_workspace_files(record_input, format_binary=context.settings.terraform_binary)
        return write_workspace_files(
            self._workspace_dir(context), files, record=record_input, snapshot_id=snapshot_id
        )

    def _commit(self, context: StepContext, step_name: str, message_suffix: str = "") -> StepOutcome:
        """Commit the workspace as `<title> (<operator>, approved by <approver>)`; the console never pushes.

        The approver is named only when an approval was recorded (see `operation_commit_message`). A step that
        commits a second time for the same operation adds `message_suffix`, such as ` (record as built)`.

        The commit is made even when no file changed (a restore in Terraform mode applies the files as
        they are), so every change leaves its commit in the history.
        """
        workspace = self._workspace(context)
        message = (
            operation_commit_message(
                context.operation.title or self.operation_kind,
                context.operation.created_by_operator_name,
                context.operation.approved_by_operator_name,
            )
            + message_suffix
        )
        try:
            commit_sha = commit_paths(
                context.services.execution_recorder,
                context.settings.repo_root,
                [Path(workspace.workspace_relative_dir)],
                message,
                context.settings.git_author_name,
                context.settings.git_author_email,
                operation_id=context.operation.id,
                step_name=step_name,
                target_key=context.target.target_key,
                allow_empty=True,
            )
        except GitCommitError as error:
            if context.target.is_direct:
                self._mark_applied_without_record(context)
            return self._fail(
                context,
                step_name,
                "commit_failed",
                "Committing the change failed",
                str(error),
                "the change is already in AWS; open the execution record for the git error (identity, hooks, "
                "repository state), commit the workspace by hand and run Detect drift",
            )
        workspace.last_applied_commit_sha = commit_sha
        workspace.last_applied_at_utc = utc_now()
        context.session.commit()
        logger.info(
            "resource_change_committed",
            operation_id=str(context.operation.id),
            target_key=context.target.target_key,
            commit_sha=commit_sha,
        )
        closed_findings = self._close_drift_findings_of_touched_resources(context)
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"commit_sha": commit_sha, "closed_drift_findings": closed_findings},
        )

    def _close_drift_findings_of_touched_resources(self, context: StepContext) -> int:
        """Resolve, by this operation, the open drift findings on the addresses the change touched.

        Returns:
            How many findings were closed (see `close_drift_findings_for_addresses`).
        """
        touched = set(self._plan_value(context, "touched_addresses", []))
        return close_drift_findings_for_addresses(context.session, context.operation, touched)

    def _record_terraform_run(
        self,
        context: StepContext,
        run_kind: str,
        exit_code: int,
        execution_record_id: int | None,
        plan_json: dict[str, Any] | None = None,
        summary: PlanSummary | None = None,
    ) -> None:
        """Keep the redacted plan of this invocation with the operation, as the adoption does."""
        context.session.add(
            TerraformRun(
                workspace_id=self._workspace(context).id,
                operation_id=context.operation.id,
                run_kind=run_kind,
                plan_json=redact_plan_json(plan_json) if plan_json else None,
                plan_summary=summary.model_dump(mode="json") if summary else None,
                exit_code=exit_code,
                execution_record_id=execution_record_id,
            )
        )
        context.session.commit()

    # --- shared steps ----------------------------------------------------------------------------

    def _execute_check_checkout(self, context: StepContext) -> StepOutcome:
        """The resource guard, then the checkout guard: clean `terraform/`, not behind upstream.

        When the guard runs again for an operation that already rendered its files (a console that came
        back under another runner identity resumes it after approval), the workspace may be dirty with
        exactly those files: they are the operation's own and do not stop it.
        """
        guard = self._guard_resource(context, "check_checkout")
        if guard is not None:
            return guard
        workspace = self._workspace(context)
        report = check_checkout(
            context.services.execution_recorder,
            context.settings.repo_root,
            Path(workspace.workspace_relative_dir),
            workspace.last_applied_commit_sha,
            context.settings.allow_checkout_without_upstream,
            operation_id=context.operation.id,
            step_name="check_checkout",
            target_key=context.target.target_key,
            operation_owns_uncommitted_files=operation_owner_check(context, workspace),
        )
        if report.ok:
            return StepOutcome(
                status=StepOutcomeStatus.COMPLETED, output={"runner_identity": context.runner_identity}
            )
        return self._fail(
            context,
            "check_checkout",
            "checkout_guard",
            "Repository checkout is not ready",
            report.summary(),
            "; ".join(problem.fix_suggestion for problem in report.problems),
        )

    def _execute_await_approval(self, context: StepContext) -> StepOutcome:
        """Always pause: every strategy waits for an operator's approval."""
        return StepOutcome(status=StepOutcomeStatus.AWAITING_APPROVAL, detail="ready for review")

    # --- Terraform strategy ----------------------------------------------------------------------

    def _execute_ensure_state_bucket(self, context: StepContext) -> StepOutcome:
        """Create the state bucket `init` configures as the backend, when nothing was adopted before."""
        workspace = self._workspace(context)
        s3 = context.services.aws_sessions.client(
            context.target, "s3", operation_id=context.operation.id, step_name="ensure_state_bucket"
        )
        status = ensure_state_bucket(s3, workspace.state_bucket_name, context.target.aws_region)
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"bucket_name": workspace.state_bucket_name, "status": status},
        )

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Write the desired `.tf` files; the record is written only when the change is committed."""
        stale = self._refuse_stale_snapshot(context, "render_desired")
        if stale is not None:
            return stale
        try:
            snapshot = self._latest_snapshot(context)
            change = self.desired_change(context)
            desired = self._desired_terraform_input(context, change, snapshot)
            touched = sorted(change.touched_addresses(context))
            expected_impact = getattr(change, "expected_impact", None)
            impact = expected_impact(context) if expected_impact is not None else None
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, "render_desired", error)
        drift_refusal = self._refuse_open_record_drift(context, "render_desired", touched)
        if drift_refusal is not None:
            return drift_refusal
        files = render_workspace_files(desired, format_binary=context.settings.terraform_binary)
        workspace_dir = self._workspace_dir(context)
        written = write_workspace_files(workspace_dir, files)
        self._store_plan_values(context, touched_addresses=touched, render_snapshot_id=snapshot.id)
        if impact is not None:
            # Terraform mode has no change set; the change says what the approver should expect.
            self._store_plan_values(context, requires_reboot=impact.requires_reboot, downtime=impact.downtime)
        logger.info(
            "resource_change_rendered",
            operation_id=str(context.operation.id),
            touched_addresses=touched,
            snapshot_id=snapshot.id,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={
                "files": [path.name for path in written],
                "snapshot_id": snapshot.id,
                WORKSPACE_FILES_FINGERPRINT_KEY: workspace_files_fingerprint(workspace_dir),
            },
        )

    def _execute_init(self, context: StepContext) -> StepOutcome:
        result = context.services.terraform_runner.init(
            self._workspace_dir(context),
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="init",
        )
        self._record_terraform_run(context, "init", result.exit_code, result.record_id)
        if not result.succeeded:
            return self._fail_and_revert(
                context,
                "init",
                "terraform_init_failed",
                "Terraform init failed",
                result.standard_error[-2000:],
                "open the execution record; common causes are missing provider network access or a bad "
                "backend configuration",
            )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED)

    def _execute_plan(self, context: StepContext) -> StepOutcome:
        run = context.services.terraform_runner.plan(
            self._workspace_dir(context),
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="plan",
        )
        if run.result.exit_code not in PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN:
            self._record_terraform_run(context, "plan", run.result.exit_code, run.result.record_id)
            return self._fail_and_revert(
                context,
                "plan",
                "terraform_plan_failed",
                "Terraform plan failed",
                run.result.standard_error[-2000:],
                "open the execution record; fix the reported configuration or credential problem and create "
                "the operation again",
            )
        summary = summarise_plan_json(run.plan_json)
        self._record_terraform_run(
            context, "plan", run.result.exit_code, run.result.record_id, run.plan_json, summary
        )
        self._store_plan_values(context, plan_summary=summary.model_dump(mode="json"))
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"has_changes": run.has_changes, "fingerprint": summary.fingerprint()},
        )

    def _execute_policy_check(self, context: StepContext) -> StepOutcome:
        """Only the touched addresses may change: in the plan (Terraform) or the change set (direct)."""
        touched = set(self._plan_value(context, "touched_addresses", []))
        if context.target.is_direct:
            outside = sorted({entry.address for entry in self._entries(context)} - touched)
            if outside:
                return self._fail(
                    context,
                    "policy_check",
                    "policy_refused",
                    "Change set touches resources the operation did not ask for",
                    ", ".join(outside),
                    "this is a bug in the operation's change computation; report it with the operation id",
                )
            return StepOutcome(status=StepOutcomeStatus.COMPLETED)
        summary = PlanSummary.model_validate(self._plan_value(context, "plan_summary", {}))
        decision = evaluate_change_policy(
            summary,
            touched,
            allow_delete_of=self._addresses_allowed_to_be_deleted(context, touched),
            allow_create_of=frozenset(touched) if self.allows_create else frozenset(),
        )
        if not decision.allowed:
            return self._fail_and_revert(
                context,
                "policy_check",
                "policy_refused",
                "Plan refused by policy",
                "; ".join(decision.refusals),
                "the plan would change more than this operation allows; inspect the plan, adopt or fix the "
                "other resources first, then create the operation again",
            )
        if decision.also_brought_up_to_date:
            return StepOutcome(
                status=StepOutcomeStatus.COMPLETED,
                detail="Also brought up to date: " + ", ".join(decision.also_brought_up_to_date),
                output={"also_brought_up_to_date": decision.also_brought_up_to_date},
            )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED)

    def _addresses_allowed_to_be_deleted(self, context: StepContext, touched: set[str]) -> frozenset[str]:
        """Where the Terraform policy lets a plain delete through: the touched addresses if allowed."""
        return frozenset(touched) if self.allows_delete else frozenset()

    def _refuse_unapproved_plan(self, context: StepContext) -> StepOutcome | None:
        """Plan once more; the failed outcome when that plan is not the one that was approved.

        Nothing has been applied yet, so a refusal puts the workspace files back as the last commit has them.
        """
        run = context.services.terraform_runner.plan(
            self._workspace_dir(context),
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="apply",
        )
        if run.result.exit_code not in PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN:
            self._record_terraform_run(context, "plan", run.result.exit_code, run.result.record_id)
            return self._fail_and_revert(
                context,
                "apply",
                "terraform_plan_failed",
                "Pre-apply plan failed",
                run.result.standard_error[-2000:],
                "open the execution record and create the operation again; nothing was applied",
            )
        fresh = summarise_plan_json(run.plan_json)
        self._record_terraform_run(
            context, "plan", run.result.exit_code, run.result.record_id, run.plan_json, fresh
        )
        approved = PlanSummary.model_validate(self._plan_value(context, "plan_summary", {}))
        if fresh.fingerprint() != approved.fingerprint():
            return self._fail_and_revert(
                context,
                "apply",
                "plan_changed_since_approval",
                "The plan changed since it was approved",
                "AWS or the files changed between review and apply",
                "review the new plan by creating the operation again; nothing was applied",
            )
        return None

    def _unblock_after_apply(self, context: StepContext, cause: str) -> str:
        """The fix suggestion once `apply` has run: the files stay, so first get past the checkout guard.

        The rendered files describe what was applied, so they are kept; until they are committed the
        checkout guard refuses every operation in the workspace, Detect drift's resolution included.
        """
        relative_dir = self._workspace(context).workspace_relative_dir
        return (
            f"{cause}. The files under {relative_dir} describe the applied change and are not committed; "
            "the checkout guard refuses every operation in this workspace until they are. Review them "
            f"(`git diff -- {relative_dir}`), commit them (`git add {relative_dir} && git commit`), then run "
            "Detect drift and accept or restore what it finds"
        )

    def _execute_apply(self, context: StepContext) -> StepOutcome:
        """Plan again, apply only if the plan is the one that was approved.

        From the moment `apply` runs the files stay as they are, because AWS may already have changed. The
        operation is marked just before the binary runs, so change history counts it from then on and not
        when the pre-apply plan refused it.
        """
        try:
            refusal = self._refuse_unapproved_plan(context)
        except Exception:
            revert_workspace(context, self._workspace(context), "apply")
            raise
        if refusal is not None:
            return refusal
        mark_terraform_apply_invoked(context.session, context.operation)
        result = context.services.terraform_runner.apply(
            self._workspace_dir(context),
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="apply",
        )
        self._record_terraform_run(context, "apply", result.exit_code, result.record_id)
        if not result.succeeded:
            return self._fail(
                context,
                "apply",
                "terraform_apply_failed",
                "Terraform apply failed",
                result.standard_error[-2000:],
                self._unblock_after_apply(
                    context,
                    "Open the execution record and fix the cause: AWS rejected part of the change and state "
                    "may be partly updated",
                ),
            )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED)

    def _execute_verify_clean_plan(self, context: StepContext) -> StepOutcome:
        """After the apply the plan must propose nothing."""
        run = context.services.terraform_runner.plan(
            self._workspace_dir(context),
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="verify_clean_plan",
        )
        summary = summarise_plan_json(run.plan_json) if run.plan_json else PlanSummary()
        self._record_terraform_run(
            context, "verify_plan", run.result.exit_code, run.result.record_id, run.plan_json or None, summary
        )
        if run.result.exit_code == 0:
            return StepOutcome(status=StepOutcomeStatus.COMPLETED)
        residual_changes = [
            f"{change.address}: {', '.join(change.actions)} on "
            f"{', '.join(change.changed_attribute_names) or 'unknown attributes'}"
            for change in summary.resource_changes
            if change.actions != ["no-op"]
        ]
        return self._fail(
            context,
            "verify_clean_plan",
            "residual_plan_changes",
            "Plan is not clean after apply",
            "\n".join(residual_changes) or run.result.standard_error[-2000:],
            self._unblock_after_apply(context, "AWS reports values the files do not"),
        )

    def _execute_commit_to_checkout(self, context: StepContext) -> StepOutcome:
        """Write the record from the desired input the files were rendered from, then commit."""
        try:
            change = self.desired_change(context)
            rendered_snapshot_id = self._plan_value(context, "render_snapshot_id")
            rendered_snapshot = (
                context.session.get(InventorySnapshot, rendered_snapshot_id)
                if rendered_snapshot_id is not None
                else None
            )
            snapshot = rendered_snapshot or self._latest_snapshot(context)
            desired = self._desired_terraform_input(context, change, snapshot)
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, "commit_to_checkout", error)
        self._write_files_and_record(context, desired, snapshot.id)
        outcome = self._commit(context, "commit_to_checkout")
        if outcome.status == StepOutcomeStatus.COMPLETED:
            self._record_what_state_now_holds(context, desired)
        return outcome

    def _record_what_state_now_holds(self, context: StepContext, desired: RenderWorkspaceInput) -> None:
        """Adoption rows for what the applied plan created, and `removed` for what it destroyed.

        "Adopted" means under Terraform, so a group this change created through Terraform counts as adopted
        at once (it can be changed, and is not offered for adoption), and a deleted one no longer does.
        """
        summary = PlanSummary.model_validate(self._plan_value(context, "plan_summary", {}))
        identifiers = resource_identifiers(desired)
        created = {
            change.address: identifiers.get(change.address, change.address.split(".", 1)[-1])
            for change in summary.resource_changes
            if change.actions == ["create"]
        }
        destroyed = {change.address for change in summary.resource_changes if change.actions == ["delete"]}
        if not created and not destroyed:
            return
        record_terraform_changes(
            context.session,
            self._workspace(context),
            created=created,
            destroyed=destroyed,
            aws_region=context.target.aws_region,
            aws_account_id=context.target.aws_account_id,
            operation_id=context.operation.id,
        )

    # --- direct strategy -------------------------------------------------------------------------

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Read the resource afresh and keep what would change as the review artefact."""
        stale = self._refuse_stale_snapshot(context, "compute_change_set")
        if stale is not None:
            return stale
        try:
            change = self.desired_change(context)
            entries = change.change_set(context)
            touched = sorted(change.touched_addresses(context))
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, "compute_change_set", error)
        drift_refusal = self._refuse_open_record_drift(context, "compute_change_set", touched)
        if drift_refusal is not None:
            return drift_refusal
        if not entries and not self._allows_an_empty_change_set(context):
            return self._fail(
                context,
                "compute_change_set",
                "nothing_to_change",
                "Nothing to change",
                "the resource already has the requested values",
                "no action needed",
            )
        serialised_entries = [entry.model_dump(mode="json") for entry in entries]
        context.session.add(
            ChangeSet(
                operation_id=context.operation.id,
                resource_arn=change.resource_arn,
                mode="direct",
                entries=serialised_entries,
            )
        )
        self._store_plan_values(
            context,
            change_set=serialised_entries,
            touched_addresses=touched,
            requires_reboot=any(entry.requires_reboot for entry in entries),
            downtime=next((entry.downtime for entry in entries if entry.downtime != "none"), "none"),
        )
        logger.info(
            "resource_change_set_computed",
            operation_id=str(context.operation.id),
            resource_arn=change.resource_arn,
            entry_count=len(entries),
        )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"entry_count": len(entries)})

    def _allows_an_empty_change_set(self, context: StepContext) -> bool:
        """Whether a change set with no entries goes on to approval instead of failing `nothing_to_change`.

        Only a step of a chained operation whose other change still does the work allows it (see
        `ChainedChangeExecutor`).
        """
        return False

    def _change_set_row(self, context: StepContext) -> ChangeSet:
        return context.session.execute(
            select(ChangeSet)
            .where(ChangeSet.operation_id == context.operation.id)
            .order_by(ChangeSet.id.desc())
            .limit(1)
        ).scalar_one()

    def _mark_applied_without_record(self, context: StepContext) -> None:
        """Say on the operation that AWS has the change while the repository's record does not."""
        context.operation.plan = {**context.operation.plan, "applied_without_record": True}
        context.session.commit()

    def _execute_apply_direct(self, context: StepContext) -> StepOutcome:
        """Call the AWS API with the approved change set."""
        entries = self._entries(context)
        try:
            change = self.desired_change(context)
            submitted = change.apply_direct(context, entries)
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, "apply_direct", error)
        row = self._change_set_row(context)
        row.applied_at_utc = utc_now()
        context.session.commit()
        logger.info(
            "resource_change_applied_directly",
            operation_id=str(context.operation.id),
            resource_arn=change.resource_arn,
            submitted_attributes=sorted(submitted),
        )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"submitted": submitted})

    def _poll_until_verified(
        self, context: StepContext, change: DesiredChange, entries: list[ChangeSetEntry]
    ) -> tuple[VerificationResult, float]:
        """Read the values back until they hold, AWS says they never will, or the wait is used up.

        Polling stops when the values hold, when the result is terminal, when every attribute that is still
        pending applies at the next reboot or maintenance window (`pending-reboot`, `pending-window`; so
        nothing more will happen now), or after `direct_verify_timeout_seconds` of waiting. Returns the last
        result and how long it waited.
        """
        reboot_attributes = {e.attribute for e in entries if e.apply_type in DEFERRED_APPLY_TYPES}
        timeout_seconds = context.settings.direct_verify_timeout_seconds
        poll_seconds = context.settings.direct_verify_poll_seconds
        waited_seconds = 0.0
        result = change.verify_direct(context, entries)
        while not result.ok and not result.terminal and waited_seconds < timeout_seconds:
            if result.pending and set(result.pending) <= reboot_attributes:
                break  # what is left applies at the next reboot; waiting changes nothing
            logger.info(
                "resource_change_awaiting_aws",
                operation_id=str(context.operation.id),
                waited_seconds=waited_seconds,
                pending=result.pending,
            )
            context.services.sleep(poll_seconds)
            waited_seconds += poll_seconds
            result = change.verify_direct(context, entries)
        return result, waited_seconds

    def _execute_verify_direct(self, context: StepContext) -> StepOutcome:
        """Read the values back until AWS reports them; AWS may still be applying when the call returns.

        The change is in AWS by now, so every failure here says so and says how to get the record right.
        An attribute still pending because AWS applies it at the next reboot or maintenance window
        (`pending-reboot`, `pending-window`) counts as submitted, not as a failure.
        """
        entries = self._entries(context)
        reboot_attributes = {e.attribute for e in entries if e.apply_type == "pending-reboot"}
        deferred_attributes = {e.attribute for e in entries if e.apply_type in DEFERRED_APPLY_TYPES}
        try:
            change = self.desired_change(context)
            result, waited_seconds = self._poll_until_verified(context, change, entries)
        except CHANGE_ERRORS as error:
            self._mark_applied_without_record(context)
            return self._fail_for_change_error(context, "verify_direct", error)
        self._change_set_row(context).verification = result.live
        pending_reboot = [attribute for attribute in result.pending if attribute in reboot_attributes]
        self._store_plan_values(context, pending_reboot_attributes=pending_reboot)
        only_deferred_pending = bool(result.pending) and set(result.pending) <= deferred_attributes
        if result.ok or only_deferred_pending:
            logger.info(
                "resource_change_verified",
                operation_id=str(context.operation.id),
                pending_reboot=pending_reboot,
                waited_seconds=waited_seconds,
            )
            return StepOutcome(
                status=StepOutcomeStatus.COMPLETED,
                output={"live": result.live, "pending_reboot": pending_reboot},
            )
        self._mark_applied_without_record(context)
        reason = result.detail or "values read back differ from the change set"
        fix_suggestion = RECOVER_AFTER_SUBMISSION
        if result.terminal:
            fix_suggestion = f"check the resource's status and events in AWS, then {RECOVER_AFTER_SUBMISSION}"
        return self._fail(
            context,
            "verify_direct",
            "direct_change_not_verified",
            "AWS did not report the requested values",
            f"{reason}\n\n{SUBMITTED_TO_AWS_SENTENCE}",
            fix_suggestion,
        )

    def _execute_refresh_resource(self, context: StepContext) -> StepOutcome:
        """Take a new inventory snapshot so the console sees what the change did.

        Direct mode records from this snapshot, so a refresh that fails stops the change there: the change
        is in AWS (it was verified) while the record does not describe it, so the operation is marked
        `applied_without_record` and the diagnostic says how to get it into the record. Terraform mode
        refreshes after the change is applied and committed: a failure there leaves the change as it is, so
        the step completes, says why in its output and detail, and logs a warning.
        """
        if context.target.is_direct:
            try:
                snapshot = refresh_target_now(context, context.target, "refresh_resource")
            except Exception as error:  # noqa: BLE001 - every failure here leaves a verified change unrecorded
                context.session.rollback()
                return self._fail_after_unrecorded_change(context, error)
            return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"snapshot_id": snapshot.id})
        return refresh_after_terraform_commit(context, refresh_target_now)

    def _fail_after_unrecorded_change(self, context: StepContext, error: Exception) -> StepOutcome:
        """Fail direct mode's closing refresh: the verified change is in AWS but not in the record.

        The diagnosis of the error (an expired login, a refused call) is kept in the detail; the fix
        suggestion is the way to get the change into the record, not a new operation, which would make
        the change again.
        """
        self._mark_applied_without_record(context)
        _, cause, _ = diagnose_exception(error, context.target.aws_profile_name, "")
        environment_slug = context.target.environment_slug
        return self._fail(
            context,
            "refresh_resource",
            "refresh_after_change_failed",
            "The change is in AWS, but the inventory could not be refreshed to record it",
            f"{cause}\n\nThe change was applied and verified; the record does not describe it yet.",
            f"once AWS can be read again (see the detail), run Refresh inventory on the {environment_slug} "
            "environment, then run Detect drift and accept the drift into the record",
        )

    def _overlay_recorded_input(
        self, context: StepContext, fresh: RenderWorkspaceInput
    ) -> RenderWorkspaceInput:
        """What direct mode records for the touched resources, from `fresh` (the closing inventory).

        The base records the inventory as it is; a create keeps what AWS does not report (see
        `ResourceCreateExecutor`).
        """
        return fresh

    def _execute_record_to_checkout(self, context: StepContext) -> StepOutcome:
        """Record what AWS now has for the touched resources only, then commit.

        A direct-mode environment gets its first record here when it has none: the whole estate as AWS has it.
        A value queued for the maintenance window (`pending-window`) is not live yet, so the record takes the
        desired value from the change instead of the describe.
        """
        try:
            snapshot = self._latest_snapshot(context)
            fresh = self._build_input(context, snapshot, include_all=True)
            if any(entry.apply_type == "pending-window" for entry in self._entries(context)):
                fresh = self.desired_change(context).apply_to_render_input(fresh)
            fresh = self._overlay_recorded_input(context, fresh)
            record_input = self._with_touched_replaced(
                self._recorded_input(context), set(self._plan_value(context, "touched_addresses", [])), fresh
            )
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, "record_to_checkout", error)
        self._write_files_and_record(context, record_input, snapshot.id)
        return self._commit(context, "record_to_checkout")
