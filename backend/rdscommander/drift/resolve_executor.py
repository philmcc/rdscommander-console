"""`drift_resolve`: accept reality into the record for one resource, or restore reality to the record.

Accepting changes nothing in AWS, so it does not pause for approval. It is per resource: the new record is
the recorded input with only the chosen resource replaced by its live rendering, so the drift of every other
resource stays visible. In Terraform mode state is brought in line (`apply -refresh-only`) and the chosen
resource must then plan clean. The files and the record are committed; if anything fails after the files
were rewritten, the workspace is reverted to the last commit.

Restoring changes AWS, so it runs through the environment's change strategy (`operations/change_strategy`):
Terraform mode plans and applies the recorded files, direct mode puts the recorded values back through the
RDS API; both pause for approval, and the findings of the resource are closed once the change is verified.

Example:
    >>> OperationService(session_factory).create(  # doctest: +SKIP
    ...     "drift_resolve", "sandbox", target_key,
    ...     {"options": {"resource_arn": arn, "choice": "accept"}, "resource_arn": arn},
    ...     "resolve-1", "phil", resource_arn=arn, title="Resolve drift: demo-postgres16-tuned",
    ... )
    <Operation ...>
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import DriftFinding, Operation, utc_now
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.drift.detect_executor import arns_by_terraform_address, build_live_render_input
from rdscommander.drift.restore_change import (
    execute_restore_step,
    observe_restore_step,
    on_restore_cancelled,
    restore_step_names,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import ObservationState, OperationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.repo.commit import GitCommitError, commit_paths, operation_commit_message
from rdscommander.repo.guard import check_checkout
from rdscommander.terraform.plan_parser import PlanSummary, summarise_plan_json
from rdscommander.terraform.record import RECORD_FILE_NAME, read_record, replace_resource
from rdscommander.terraform.renderer import render_workspace_files, write_workspace_files
from rdscommander.terraform.workspace_revert import revert_workspace
from rdscommander.terraform.workspaces import ensure_workspace_record, workspace_directory

logger = get_logger(__name__)

ACCEPT_STEPS = [
    "check_checkout",
    "render_live",
    "refresh_state",
    "verify_clean_plan",
    "commit_record",
    "close_findings",
]
ACCEPT_CHOICE = "accept"
STEPS_THAT_MAY_LEAVE_REWRITTEN_FILES = ("render_live", "refresh_state", "verify_clean_plan", "commit_record")
ADOPTION_IN_PROGRESS_STATES = (
    OperationState.QUEUED.value,
    OperationState.RUNNING.value,
    OperationState.AWAITING_APPROVAL.value,
)
PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN = (0, 2)
REVERTED_SENTENCE = "The workspace files were reverted to the last commit."


class DriftResolveExecutor:
    """Resolves drift for one resource."""

    operation_kind = "drift_resolve"

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read the live configuration from `config_store` whenever a step needs it."""
        self.config_store = config_store

    def _choice(self, operation: Operation) -> str:
        return str(operation.plan.get("options", {}).get("choice", ACCEPT_CHOICE))

    def step_names(self, operation: Operation) -> list[str]:
        """The accept steps, or the change strategy's steps followed by closing the findings."""
        if self._choice(operation) == ACCEPT_CHOICE:
            return list(ACCEPT_STEPS)
        return restore_step_names(self.config_store.current.target(operation.target_key or ""))

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """Each step completes once per operation; the checkout guard runs again on another runner."""
        if self._choice(context.operation) != ACCEPT_CHOICE:
            return observe_restore_step(self, step_name, context)
        if step_name == "check_checkout":
            # The guard protects the checkout of the machine that will write files, so a resume on
            # another console must run it again against its own checkout.
            previous = context.completed_step_output(step_name) or {}
            same_runner = previous.get("runner_identity") == context.runner_identity
            return StepObservation(
                state=ObservationState.SATISFIED if same_runner else ObservationState.ABSENT
            )
        completed = context.step_already_completed(step_name)
        return StepObservation(state=ObservationState.SATISFIED if completed else ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Run the step: `_accept_<step>` for the accept choice, the change strategy's step otherwise.

        An exception raised after the workspace files may have been rewritten reverts them before it
        propagates; the runner turns it into the failure diagnostic.
        """
        if self._choice(context.operation) != ACCEPT_CHOICE:
            return execute_restore_step(self, step_name, context)
        handler = getattr(self, f"_accept_{step_name}")
        if step_name not in STEPS_THAT_MAY_LEAVE_REWRITTEN_FILES:
            return handler(context)
        try:
            return handler(context)
        except Exception:
            self._revert_workspace(context, step_name)
            raise

    def on_cancelled(self, context: StepContext) -> bool:
        """Clean up a rejected restore (accepting never waits for approval, so it is never rejected).

        Returns:
            True when the cleanup is done; False when another console holds the rendered files.
        """
        if self._choice(context.operation) == ACCEPT_CHOICE:
            return True
        return on_restore_cancelled(self, context)

    # Helpers -----------------------------------------------------------------

    def _workspace_dir(self, context: StepContext) -> Path:
        workspace = ensure_workspace_record(context.session, context.target, context.console_config)
        return workspace_directory(context.settings.repo_root, workspace)

    def _fail(
        self,
        context: StepContext,
        step_name: str,
        diagnostic_kind: str,
        title: str,
        detail: str,
        fix_suggestion: str,
    ) -> StepOutcome:
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

    def _fail_and_revert(
        self,
        context: StepContext,
        step_name: str,
        diagnostic_kind: str,
        title: str,
        detail: str,
        fix_suggestion: str,
    ) -> StepOutcome:
        """Revert the workspace, then fail with a diagnostic that says so."""
        reverted_sentence = self._revert_workspace(context, step_name)
        full_detail = f"{detail}\n\n{reverted_sentence}" if detail else reverted_sentence
        return self._fail(context, step_name, diagnostic_kind, title, full_detail, fix_suggestion)

    def _revert_workspace(self, context: StepContext, step_name: str) -> str:
        """Put the workspace back as `HEAD` has it; returns the sentence for the diagnostic."""
        workspace = ensure_workspace_record(context.session, context.target, context.console_config)
        if revert_workspace(context, workspace, step_name):
            return REVERTED_SENTENCE
        relative_dir = Path(workspace.workspace_relative_dir).as_posix()
        return (
            f"Reverting the workspace files failed; run `git checkout -- {relative_dir}` "
            "before planning again."
        )

    def _chosen_address(self, context: StepContext) -> str | None:
        """Terraform address of the resource being resolved, or None when it cannot be told.

        The plan carries it when the operation was prepared from a finding. Otherwise it is read from the
        open findings, which name their address in every attribute, and failing that from the latest
        snapshot. A resource that left the inventory is keyed by its address already.
        """
        planned_address = context.operation.plan.get("address")
        if planned_address:
            return str(planned_address)
        resource_key = context.operation.resource_arn
        if not resource_key:
            return None
        if not resource_key.startswith("arn:"):
            return resource_key
        finding_addresses = {
            attribute["address"]
            for finding in context.session.execute(
                select(DriftFinding).where(
                    DriftFinding.resource_arn == resource_key, DriftFinding.resolved_at_utc.is_(None)
                )
            ).scalars()
            for attribute in finding.attributes
            if attribute.get("address")
        }
        if len(finding_addresses) == 1:
            return next(iter(finding_addresses))
        snapshot = latest_snapshot_for_target(context.session, context.target.target_key)
        if snapshot is None:
            return None
        addresses_by_arn = {
            arn: address for address, arn in arns_by_terraform_address(context.session, snapshot.id).items()
        }
        return addresses_by_arn.get(resource_key)

    # Accept ------------------------------------------------------------------

    def _accept_check_checkout(self, context: StepContext) -> StepOutcome:
        """The same guard the adoption runs: clean `terraform/`, not behind upstream, last commit present."""
        workspace = ensure_workspace_record(context.session, context.target, context.console_config)
        context.session.commit()
        report = check_checkout(
            context.services.execution_recorder,
            context.settings.repo_root,
            Path(workspace.workspace_relative_dir),
            workspace.last_applied_commit_sha,
            context.settings.allow_checkout_without_upstream,
            operation_id=context.operation.id,
            step_name="check_checkout",
            target_key=context.target.target_key,
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

    def _accept_render_live(self, context: StepContext) -> StepOutcome:
        """Replace the chosen resource in the record by its live rendering; write files and record."""
        session, target = context.session, context.target
        snapshot = latest_snapshot_for_target(session, target.target_key)
        if snapshot is None:
            return self._fail(
                context,
                "render_live",
                "no_inventory_snapshot",
                "There is no inventory snapshot to accept",
                f"{target.target_key} has never been inventoried",
                "refresh inventory for this environment, then create a new resolution",
            )
        maximum_snapshot_age = timedelta(minutes=context.settings.max_inventory_age_minutes)
        if datetime.now(UTC) - snapshot.captured_at_utc > maximum_snapshot_age:
            return self._fail(
                context,
                "render_live",
                "stale_inventory",
                "Inventory snapshot is too old to accept from",
                f"latest snapshot for {target.target_key} was captured "
                f"{snapshot.captured_at_utc.isoformat()}; the limit is "
                f"{context.settings.max_inventory_age_minutes} minutes",
                "refresh inventory for this environment, then create a new resolution",
            )
        adoption_operation = session.execute(
            select(Operation)
            .where(
                Operation.operation_kind == "terraform_adoption",
                Operation.target_key == target.target_key,
                Operation.state.in_(ADOPTION_IN_PROGRESS_STATES),
            )
            .order_by(Operation.created_at_utc)
            .limit(1)
        ).scalar_one_or_none()
        if adoption_operation is not None:
            return self._fail(
                context,
                "render_live",
                "adoption_in_progress",
                "A Terraform adoption is in progress for this target",
                f"operation {adoption_operation.id} ({adoption_operation.title or 'adoption'}) is "
                f"{adoption_operation.state}; it is writing the same workspace",
                "finish or reject that adoption first, then create a new resolution",
            )
        workspace = ensure_workspace_record(session, target, context.console_config)
        directory = workspace_directory(context.settings.repo_root, workspace)
        try:
            recorded = read_record(directory)
        except ValueError as error:
            return self._fail(
                context,
                "render_live",
                "record_unreadable",
                "The Terraform record file cannot be read",
                f"{directory / RECORD_FILE_NAME}: {error}",
                "restore console-record.json from git (git checkout -- <file>), then create a new resolution",
            )
        address = self._chosen_address(context)
        if recorded is None or address is None:
            return self._fail(
                context,
                "render_live",
                "nothing_to_accept_into",
                "There is no recorded resource to update",
                (
                    f"{target.target_key} has no {RECORD_FILE_NAME}"
                    if recorded is None
                    else f"{context.operation.resource_arn} cannot be matched to a Terraform address"
                ),
                "adopt the resources into Terraform first, or run Detect drift and resolve from its finding",
            )
        live = build_live_render_input(
            session,
            target,
            context.console_config,
            snapshot,
            workspace,
            context.settings.simulated_aws_endpoint_url,
        )
        accepted = replace_resource(recorded.render_input, address, live)
        files = render_workspace_files(accepted, format_binary=context.settings.terraform_binary)
        written = write_workspace_files(directory, files, record=accepted, snapshot_id=snapshot.id)
        session.commit()
        logger.info(
            "drift_accepted_into_record",
            operation_id=str(context.operation.id),
            address=address,
            snapshot_id=snapshot.id,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"address": address, "files": [path.name for path in written], "snapshot_id": snapshot.id},
        )

    def _accept_refresh_state(self, context: StepContext) -> StepOutcome:
        """Terraform mode: bring state in line with AWS (`apply -refresh-only`); direct mode has no state."""
        if context.target.is_direct:
            return StepOutcome(status=StepOutcomeStatus.COMPLETED, detail="direct mode has no state")
        result = context.services.terraform_runner.apply_refresh_only(
            self._workspace_dir(context),
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="refresh_state",
        )
        if not result.succeeded:
            return self._fail_and_revert(
                context,
                "refresh_state",
                "terraform_refresh_failed",
                "Refreshing state failed",
                result.standard_error,
                "open the execution record; fix credentials or backend access and create a new resolution",
            )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED)

    def _accept_verify_clean_plan(self, context: StepContext) -> StepOutcome:
        """Terraform mode: the chosen resource must plan clean; other resources may still show their drift."""
        if context.target.is_direct:
            return StepOutcome(status=StepOutcomeStatus.COMPLETED, detail="direct mode has no state")
        address = self._chosen_address(context)
        run = context.services.terraform_runner.plan(
            self._workspace_dir(context),
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="verify_clean_plan",
        )
        if run.result.exit_code not in PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN or (
            run.result.exit_code == 2 and not run.plan_json
        ):
            return self._fail_and_revert(
                context,
                "verify_clean_plan",
                "terraform_plan_failed",
                "Terraform plan failed while verifying the accepted drift",
                run.result.standard_error or "the plan reported changes but its JSON could not be read",
                "open the execution record; fix the reported credential, backend or configuration "
                "problem, then create a new resolution",
            )
        summary = summarise_plan_json(run.plan_json) if run.plan_json else PlanSummary()
        changes = [change for change in summary.resource_changes if change.actions != ["no-op"]]
        residual_changes = [
            f"{change.address}: {', '.join(change.actions)} on "
            f"{', '.join(change.changed_attribute_names) or 'unknown attributes'}"
            for change in changes
            if change.address == address
        ]
        if residual_changes:
            return self._fail_and_revert(
                context,
                "verify_clean_plan",
                "residual_plan_changes",
                "Plan is not clean after accepting drift",
                "\n".join(residual_changes),
                "the renderer does not cover the listed attributes; add them to the renderer (and golden "
                "tests), then create a new resolution. Nothing was committed; Terraform state keeps the "
                "refreshed values, which match AWS",
            )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={
                "address": address,
                "other_addresses_with_changes": sorted({c.address for c in changes if c.address != address}),
            },
        )

    def _accept_commit_record(self, context: StepContext) -> StepOutcome:
        """Commit the workspace (files and record) under the operator's identity; the console never pushes."""
        workspace = ensure_workspace_record(context.session, context.target, context.console_config)
        # Accepting has no approval pause, so the commit names the operator and no approver.
        message = operation_commit_message(
            context.operation.title or self.operation_kind,
            context.operation.created_by_operator_name,
            context.operation.approved_by_operator_name,
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
                step_name="commit_record",
                target_key=context.target.target_key,
            )
        except GitCommitError as error:
            return self._fail_and_revert(
                context,
                "commit_record",
                "commit_failed",
                "Committing the accepted record failed",
                str(error),
                "open the execution record for the git error (identity, hooks, repository state), "
                "then create a new resolution",
            )
        workspace.last_applied_commit_sha = commit_sha
        context.session.commit()
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"commit_sha": commit_sha})

    def _accept_close_findings(self, context: StepContext) -> StepOutcome:
        """Close the open findings of the chosen resource only: the record now matches reality for it."""
        closed_count = 0
        for finding in context.session.execute(
            select(DriftFinding).where(
                DriftFinding.resource_arn == context.operation.resource_arn,
                DriftFinding.resolved_at_utc.is_(None),
            )
        ).scalars():
            finding.resolved_at_utc = utc_now()
            finding.resolved_by_operation_id = context.operation.id
            closed_count += 1
        context.session.commit()
        logger.info(
            "drift_findings_closed",
            resource_arn=context.operation.resource_arn,
            closed_count=closed_count,
            operation_id=str(context.operation.id),
        )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"closed_count": closed_count})
