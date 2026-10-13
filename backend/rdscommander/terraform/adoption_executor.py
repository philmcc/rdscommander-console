"""`terraform_adoption`: thirteen idempotent steps from checkout guard to commit and refresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select

from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import (
    Operation,
    ResourceAdoption,
    TerraformRun,
    TerraformWorkspace,
    utc_now,
)
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.drift.detect_executor import arns_by_terraform_address
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.inventory.refresh_executor import refresh_after_terraform_commit, refresh_target_now
from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.repo.commit import commit_paths, operation_commit_message
from rdscommander.repo.guard import check_checkout
from rdscommander.terraform.adoption_plan import AdoptionPlan
from rdscommander.terraform.plan_parser import PlanSummary, redact_plan_json, summarise_plan_json
from rdscommander.terraform.policy import evaluate_adoption_policy
from rdscommander.terraform.record import (
    RECORD_FILE_NAME,
    read_record,
    replace_resource,
    resource_addresses,
    write_record,
)
from rdscommander.terraform.render_input import RenderWorkspaceInput, build_render_input
from rdscommander.terraform.renderer import IMPORTS_FILE_NAME, render_workspace_files, write_workspace_files
from rdscommander.terraform.runner import mark_terraform_apply_invoked
from rdscommander.terraform.state_addresses import (
    TerraformStateUnreadableError,
    read_state_addresses,
    sync_adoptions_with_state,
)
from rdscommander.terraform.state_bucket import ensure_state_bucket
from rdscommander.terraform.workspace_fingerprint import (
    WORKSPACE_FILES_FINGERPRINT_KEY,
    operation_owner_check,
    workspace_files_fingerprint,
)
from rdscommander.terraform.workspace_revert import revert_workspace_of_cancelled_operation
from rdscommander.terraform.workspaces import (
    adopted_terraform_addresses,
    ensure_workspace_record,
    workspace_directory,
)

STEP_NAMES = [
    "check_checkout",
    "ensure_state_bucket",
    "render_workspace",
    "write_import_blocks",
    "init",
    "plan",
    "policy_check",
    "await_approval",
    "apply",
    "verify_clean_plan",
    "remove_import_blocks",
    "commit_to_checkout",
    "refresh_resource",
]
STEPS_SATISFIED_BY_PRIOR_COMPLETION = (
    "plan",
    "policy_check",
    "apply",
    "verify_clean_plan",
    "commit_to_checkout",
    "refresh_resource",
)
SATISFIED = StepObservation(state=ObservationState.SATISFIED)
ABSENT = StepObservation(state=ObservationState.ABSENT)


class TerraformAdoptionExecutor:
    """Imports selected resources into the target's workspace."""

    operation_kind = "terraform_adoption"

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read the live configuration from `config_store` whenever a step needs it."""
        self.config_store = config_store

    def step_names(self, operation: Operation) -> list[str]:
        """Fixed order."""
        return list(STEP_NAMES)

    def on_cancelled(self, context: StepContext) -> bool:
        """Clean up after the adoption was rejected while waiting for approval.

        The rendered files and import blocks are removed again, so that the checkout guard does not
        refuse the next operation on this target.

        Returns:
            True when the cleanup is done; False when another console holds the files.
        """
        return revert_workspace_of_cancelled_operation(context, self._workspace(context))

    # Helpers -----------------------------------------------------------------

    def _plan(self, context: StepContext) -> AdoptionPlan:
        return AdoptionPlan.model_validate(context.operation.plan)

    def _workspace(self, context: StepContext) -> TerraformWorkspace:
        workspace = ensure_workspace_record(context.session, context.target, self.config_store.current)
        context.session.commit()
        return workspace

    def _workspace_dir(self, context: StepContext) -> Path:
        return workspace_directory(context.settings.repo_root, self._workspace(context))

    def _rendered_files(self, context: StepContext, include_imports: bool) -> dict[str, str]:
        workspace = self._workspace(context)
        snapshot = latest_snapshot_for_target(context.session, context.target.target_key)
        if snapshot is None:
            raise RuntimeError("no inventory snapshot; refresh inventory first")
        adopted_rows = context.session.execute(
            select(ResourceAdoption).where(
                ResourceAdoption.workspace_id == workspace.id, ResourceAdoption.adoption_status == "adopted"
            )
        ).scalars()
        adopted = {r.terraform_address for r in adopted_rows}
        render_input = build_render_input(
            context.session,
            context.target,
            self.config_store.current,
            snapshot,
            {r.resource_arn for r in self._plan(context).selected_resources},
            adopted,
            include_imports=include_imports,
            simulated_endpoint_url=context.settings.simulated_aws_endpoint_url,
        )
        render_input = self._with_earlier_adoptions_from_record(context, render_input)
        return render_workspace_files(render_input, format_binary=context.settings.terraform_binary)

    def _learn_adoptions_from_state(self, context: StepContext, snapshot_id: int) -> None:
        """Give what Terraform state holds an adoption row before rendering.

        The files declare every adopted resource; one adopted by a console with another control database
        (or created by a Terraform-mode change) has no row here yet, and leaving it out of the files would
        plan its destruction.

        Raises:
            TerraformStateUnreadableError: The state object could not be read.
        """
        workspace = self._workspace(context)
        s3 = context.services.aws_sessions.client(
            context.target, "s3", operation_id=context.operation.id, step_name="render_workspace"
        )
        in_state = read_state_addresses(s3, workspace.state_bucket_name, workspace.state_key)
        sync_adoptions_with_state(
            context.session, workspace, in_state, arns_by_terraform_address(context.session, snapshot_id)
        )

    def _adopted_earlier(self, context: StepContext) -> set[str]:
        """Addresses adopted before this adoption: adopted now, and not among those it selects."""
        adopted = adopted_terraform_addresses(context.session, self._workspace(context))
        return adopted - self._plan(context).selected_addresses

    def _selected_already_in_state(self, context: StepContext) -> set[str]:
        """Selected addresses state already holds: an earlier, partly applied adoption imported them.

        Rendering learned them from state (`_learn_adoptions_from_state`) and wrote no import block for them,
        so the policy counts them as satisfied instead of "selected but not imported by this plan".
        """
        adopted = adopted_terraform_addresses(context.session, self._workspace(context))
        return adopted & self._plan(context).selected_addresses

    def _with_earlier_adoptions_from_record(
        self, context: StepContext, render_input: RenderWorkspaceInput
    ) -> RenderWorkspaceInput:
        """`render_input` with every resource adopted earlier taken from the record, as it was recorded.

        Only the resources this adoption selects come from the snapshot. A resource adopted earlier that
        has drifted keeps its recorded values, so its drift stays visible instead of being folded into the
        adoption's commit. A resource the record lacks (the first adoption, or a workspace adopted before
        the record existed) has nothing to keep and stays as the snapshot has it.

        Raises:
            RuntimeError: The record exists but cannot be read.
        """
        directory = self._workspace_dir(context)
        try:
            recorded = read_record(directory)
        except ValueError as error:
            raise RuntimeError(
                f"{directory / RECORD_FILE_NAME} cannot be read ({error}); restore it from git "
                "(git checkout -- <file>), then create a new adoption"
            ) from error
        if recorded is None:
            return render_input
        kept = sorted(self._adopted_earlier(context) & resource_addresses(recorded.render_input))
        for address in kept:
            render_input = replace_resource(render_input, address, recorded.render_input)
        return render_input

    def _adopted_record_input(self, context: StepContext) -> tuple[RenderWorkspaceInput, int]:
        """What `console-record.json` holds once the adoption has applied: the adopted set.

        The record describes the committed files, so it is built only after the import has applied and the
        plan has verified clean, from the resources the control database now calls adopted. A rejected or
        failed adoption therefore never leaves resources in the record that are not under Terraform. The
        newly adopted resources come from the snapshot; those adopted earlier keep their recorded values
        (see `_with_earlier_adoptions_from_record`).
        """
        workspace = self._workspace(context)
        snapshot = latest_snapshot_for_target(context.session, context.target.target_key)
        if snapshot is None:
            raise RuntimeError("no inventory snapshot; refresh inventory first")
        render_input = build_render_input(
            context.session,
            context.target,
            self.config_store.current,
            snapshot,
            set(),
            adopted_terraform_addresses(context.session, workspace),
            include_imports=False,
            simulated_endpoint_url=context.settings.simulated_aws_endpoint_url,
        )
        return self._with_earlier_adoptions_from_record(context, render_input), snapshot.id

    def _record_run(
        self,
        context: StepContext,
        run_kind: str,
        exit_code: int,
        record_id: int | None,
        plan_json: dict[str, Any] | None = None,
        summary: PlanSummary | None = None,
    ) -> TerraformRun:
        run = TerraformRun(
            workspace_id=self._workspace(context).id,
            operation_id=context.operation.id,
            run_kind=run_kind,
            plan_json=redact_plan_json(plan_json) if plan_json else None,
            plan_summary=summary.model_dump() if summary else None,
            exit_code=exit_code,
            execution_record_id=record_id,
        )
        context.session.add(run)
        context.session.commit()
        return run

    def _record_adoption_status(self, context: StepContext, status: str) -> None:
        """Upsert a `resource_adoptions` row per selected resource with `status`.

        A row that is already `adopted` (state holds it) is never set back to `planned` or `failed`: this
        adoption did not import it, and a refusal or failure does not take it out of state.
        """
        workspace = self._workspace(context)
        for resource in self._plan(context).selected_resources:
            existing = context.session.execute(
                select(ResourceAdoption).where(
                    ResourceAdoption.workspace_id == workspace.id,
                    ResourceAdoption.terraform_address == resource.terraform_address,
                )
            ).scalar_one_or_none()
            if existing is not None and existing.adoption_status == "adopted" and status != "adopted":
                continue
            if existing is None:
                existing = ResourceAdoption(
                    workspace_id=workspace.id,
                    resource_arn=resource.resource_arn,
                    resource_identifier=resource.resource_identifier,
                    resource_type=resource.resource_type,
                    terraform_address=resource.terraform_address,
                    adoption_status=status,
                )
                context.session.add(existing)
            existing.adoption_status = status
            existing.operation_id = context.operation.id
            if status == "adopted":
                existing.adopted_at_utc = utc_now()
        context.session.commit()

    def _approved_summary(self, context: StepContext) -> PlanSummary | None:
        run = context.session.execute(
            select(TerraformRun)
            .where(TerraformRun.operation_id == context.operation.id, TerraformRun.run_kind == "plan")
            .order_by(TerraformRun.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        return PlanSummary.model_validate(run.plan_summary) if run and run.plan_summary else None

    def _fail(
        self, context: StepContext, step_name: str, kind: str, title: str, detail: str, fix_suggestion: str
    ) -> StepOutcome:
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name=step_name,
            severity="error",
            diagnostic_kind=kind,
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

    # Observe -----------------------------------------------------------------

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """Idempotency checks per step."""
        if step_name in STEPS_SATISFIED_BY_PRIOR_COMPLETION:
            return SATISFIED if context.step_already_completed(step_name) else ABSENT
        if step_name == "check_checkout":
            # The guard protects the checkout of the machine that will write files, so a resume on
            # another console must run it again against its own checkout.
            previous = context.completed_step_output(step_name) or {}
            return SATISFIED if previous.get("runner_identity") == context.runner_identity else ABSENT
        if step_name == "ensure_state_bucket":
            s3 = context.services.aws_sessions.client(
                context.target, "s3", operation_id=context.operation.id, step_name=step_name
            )
            try:
                s3.head_bucket(Bucket=self._workspace(context).state_bucket_name)
                return SATISFIED
            except s3.exceptions.ClientError:
                return ABSENT
        if step_name == "render_workspace":
            directory = self._workspace_dir(context)
            files = self._rendered_files(context, include_imports=False)
            current = all(
                (directory / n).exists() and (directory / n).read_text() == c for n, c in files.items()
            )
            return SATISFIED if current else ABSENT
        if step_name == "write_import_blocks":
            if context.step_already_completed("apply"):
                return SATISFIED
            path = self._workspace_dir(context) / IMPORTS_FILE_NAME
            expected = self._rendered_files(context, include_imports=True).get(IMPORTS_FILE_NAME)
            return (
                SATISFIED
                if expected is not None and path.exists() and path.read_text() == expected
                else ABSENT
            )
        if step_name == "init":
            directory = self._workspace_dir(context)
            ready = (directory / ".terraform").is_dir() and (directory / ".terraform.lock.hcl").exists()
            return SATISFIED if ready else ABSENT
        if step_name == "await_approval":
            environment = self.config_store.current.environment(context.target.environment_slug)
            approved = context.operation.approved_by_operator_name or not environment.requires_approval
            return SATISFIED if approved else ABSENT
        if step_name == "remove_import_blocks":
            directory = self._workspace_dir(context)
            if (directory / IMPORTS_FILE_NAME).exists():
                return ABSENT
            expected_record_input, _ = self._adopted_record_input(context)
            try:
                recorded = read_record(directory)
            except ValueError:  # unreadable record: write it again
                return ABSENT
            current = recorded is not None and recorded.render_input == expected_record_input
            return SATISFIED if current else ABSENT
        raise ValueError(f"unknown step {step_name}")

    # Execute -----------------------------------------------------------------

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Do one step."""
        handler = getattr(self, f"_execute_{step_name}")
        return handler(context)

    def _execute_check_checkout(self, context: StepContext) -> StepOutcome:
        """The checkout guard; a resumed adoption's own rendered files and import blocks do not stop it."""
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
            "; ".join(p.fix_suggestion for p in report.problems),
        )

    def _execute_ensure_state_bucket(self, context: StepContext) -> StepOutcome:
        workspace = self._workspace(context)
        s3 = context.services.aws_sessions.client(
            context.target, "s3", operation_id=context.operation.id, step_name="ensure_state_bucket"
        )
        status = ensure_state_bucket(s3, workspace.state_bucket_name, context.target.aws_region)
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"bucket_name": workspace.state_bucket_name, "status": status},
        )

    def _execute_render_workspace(self, context: StepContext) -> StepOutcome:
        snapshot = latest_snapshot_for_target(context.session, context.target.target_key)
        max_age = timedelta(minutes=context.settings.max_inventory_age_minutes)
        if snapshot is None or datetime.now(UTC) - snapshot.captured_at_utc > max_age:
            captured = snapshot.captured_at_utc.isoformat() if snapshot else "never"
            return self._fail(
                context,
                "render_workspace",
                "stale_inventory",
                "Inventory snapshot is too old to adopt from",
                f"latest snapshot for {context.target.target_key} was captured {captured}; "
                f"the limit is {context.settings.max_inventory_age_minutes} minutes",
                "refresh inventory for this environment, then create a new adoption",
            )
        try:
            self._learn_adoptions_from_state(context, snapshot.id)
        except TerraformStateUnreadableError as error:
            return self._fail(
                context,
                "render_workspace",
                error.diagnostic_kind,
                error.title,
                str(error),
                error.fix_suggestion,
            )
        files = self._rendered_files(context, include_imports=False)
        workspace_dir = self._workspace_dir(context)
        written = write_workspace_files(workspace_dir, files)
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={
                "files": [p.name for p in written],
                WORKSPACE_FILES_FINGERPRINT_KEY: workspace_files_fingerprint(workspace_dir),
            },
        )

    def _execute_write_import_blocks(self, context: StepContext) -> StepOutcome:
        files = self._rendered_files(context, include_imports=True)
        workspace_dir = self._workspace_dir(context)
        (workspace_dir / IMPORTS_FILE_NAME).write_text(files[IMPORTS_FILE_NAME], encoding="utf-8")
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={
                "import_count": files[IMPORTS_FILE_NAME].count("import {"),
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
        self._record_run(context, "init", result.exit_code, result.record_id)
        if not result.succeeded:
            return self._fail(
                context,
                "init",
                "terraform_init_failed",
                "Terraform init failed",
                result.standard_error,
                "open the execution record; common causes are missing provider network access "
                "or a bad backend configuration",
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
        if run.result.exit_code not in (0, 2):
            self._record_run(context, "plan", run.result.exit_code, run.result.record_id)
            return self._fail(
                context,
                "plan",
                "terraform_plan_failed",
                "Terraform plan failed",
                run.result.standard_error,
                "open the execution record; fix the reported configuration or credential problem "
                "and create a new adoption",
            )
        summary = summarise_plan_json(run.plan_json)
        self._record_run(context, "plan", run.result.exit_code, run.result.record_id, run.plan_json, summary)
        self._record_adoption_status(context, "planned")
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={
                "fingerprint": summary.fingerprint(),
                "import_count": summary.import_count,
                "update_count": summary.update_count,
            },
        )

    def _execute_policy_check(self, context: StepContext) -> StepOutcome:
        summary = self._approved_summary(context)
        if summary is None:
            return self._fail(
                context,
                "policy_check",
                "plan_missing",
                "No plan to check",
                "the plan step recorded nothing",
                "create a new adoption",
            )
        already_in_state = self._selected_already_in_state(context)
        decision = evaluate_adoption_policy(
            summary,
            self._plan(context).selected_addresses - already_in_state,
            adopted_earlier=self._adopted_earlier(context) | already_in_state,
        )
        if not decision.allowed:
            self._record_adoption_status(context, "failed")
            return self._fail(
                context,
                "policy_check",
                "policy_refusal",
                "Plan refused by policy",
                "\n".join(decision.refusals),
                "adoption never deletes or replaces instances or clusters; investigate why the plan "
                "proposes it (usually an attribute the renderer does not yet cover) and fix the "
                "rendering before retrying",
            )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"refusals": []})

    def _execute_await_approval(self, context: StepContext) -> StepOutcome:
        return StepOutcome(status=StepOutcomeStatus.AWAITING_APPROVAL, detail="plan ready for review")

    def _execute_apply(self, context: StepContext) -> StepOutcome:
        approved = self._approved_summary(context)
        fresh = context.services.terraform_runner.plan(
            self._workspace_dir(context),
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="apply",
        )
        if fresh.result.exit_code not in (0, 2):
            self._record_run(context, "plan", fresh.result.exit_code, fresh.result.record_id)
            return self._fail(
                context,
                "apply",
                "terraform_plan_failed",
                "Pre-apply plan failed",
                fresh.result.standard_error,
                "open the execution record and create a new adoption",
            )
        fresh_summary = summarise_plan_json(fresh.plan_json)
        self._record_run(
            context, "plan", fresh.result.exit_code, fresh.result.record_id, fresh.plan_json, fresh_summary
        )
        if approved is None or fresh_summary.fingerprint() != approved.fingerprint():
            return self._fail(
                context,
                "apply",
                "plan_changed_after_approval",
                "Plan changed since approval",
                "the plan produced right before apply differs from the approved plan",
                "review the latest plan and create a new adoption; nothing was applied",
            )
        mark_terraform_apply_invoked(context.session, context.operation)
        result = context.services.terraform_runner.apply(
            self._workspace_dir(context),
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="apply",
        )
        self._record_run(context, "apply", result.exit_code, result.record_id)
        if result.succeeded:
            # The import is in state now, whatever verification says next.
            self._record_adoption_status(context, "adopted")
        if not result.succeeded:
            return self._fail(
                context,
                "apply",
                "terraform_apply_failed",
                "Terraform apply failed",
                result.standard_error,
                self._unblock_after_failed_apply(context),
            )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED)

    def _unblock_after_failed_apply(self, context: StepContext) -> str:
        """The fix suggestion once an adoption's apply failed: its files stay and block the workspace.

        AWS and state may already hold part of the adoption, so the rendered files are kept; until they are
        committed or discarded the checkout guard refuses every operation in the workspace, a new adoption
        included. The advice covers state holding every selected address (commit without the import
        blocks), some of them (discard and adopt the same selection again) and none (discard and adopt).

        Example:
            >>> executor._unblock_after_failed_apply(context)  # doctest: +SKIP
            'Open the execution record and fix the cause. ...'
        """
        relative_dir = self._workspace(context).workspace_relative_dir
        return (
            "Open the execution record and fix the cause. The files under "
            f"{relative_dir} are kept and not committed, so the checkout guard refuses every operation in "
            "this workspace, a new adoption included, until they are dealt with. Run `tofu state list` in "
            "the workspace. If it lists every address the adoption selected, delete "
            f"{relative_dir}/{IMPORTS_FILE_NAME}, review and commit the rest, then run Detect drift. If it "
            "lists only some of them, do not commit (the files would declare resources state lacks): "
            "discard the files as below, then run a new adoption and select the same resources again; "
            "an address state already holds counts as adopted, so only the rest are imported. If it lists "
            "none of them, discard the "
            f"files (`git checkout -- {relative_dir}`, then delete the new files "
            f"`git status -- {relative_dir}` lists) and run a new adoption"
        )

    def _execute_verify_clean_plan(self, context: StepContext) -> StepOutcome:
        directory = self._workspace_dir(context)
        imports = directory / IMPORTS_FILE_NAME
        if imports.exists():
            imports.unlink()
        run = context.services.terraform_runner.plan(
            directory,
            context.target,
            context.settings,
            operation_id=context.operation.id,
            step_name="verify_clean_plan",
        )
        summary = summarise_plan_json(run.plan_json) if run.plan_json else PlanSummary()
        self._record_run(
            context, "verify_plan", run.result.exit_code, run.result.record_id, run.plan_json or None, summary
        )
        if run.result.exit_code == 0 and not summary.has_changes:
            return StepOutcome(status=StepOutcomeStatus.COMPLETED)
        details = [
            f"{c.address}: {', '.join(c.actions)} on "
            f"{', '.join(c.changed_attribute_names) or 'unknown attributes'}"
            for c in summary.resource_changes
            if c.actions != ["no-op"]
        ]
        return self._fail(
            context,
            "verify_clean_plan",
            "residual_plan_changes",
            "Plan is not clean after apply",
            "\n".join(details) or run.result.standard_error,
            "the renderer does not yet cover the listed attributes; add them to the renderer "
            "(and golden tests), then create a new adoption. Nothing was committed",
        )

    def _execute_remove_import_blocks(self, context: StepContext) -> StepOutcome:
        directory = self._workspace_dir(context)
        path = directory / IMPORTS_FILE_NAME
        if path.exists():
            path.unlink()
        record_input, snapshot_id = self._adopted_record_input(context)
        record_path = write_record(directory, record_input, snapshot_id)
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"record": record_path.name, "snapshot_id": snapshot_id},
        )

    def _execute_commit_to_checkout(self, context: StepContext) -> StepOutcome:
        workspace = self._workspace(context)
        plan = self._plan(context)
        target = context.target
        count = len(plan.selected_resources)
        title = (
            f"Adopt {count} {'resource' if count == 1 else 'resources'} into Terraform: {target.target_key}"
        )
        message = (
            operation_commit_message(
                title, context.operation.created_by_operator_name, context.operation.approved_by_operator_name
            )
            + "\n\n"
            + "\n".join(
                f"- {r.terraform_address} <- {r.resource_identifier}" for r in plan.selected_resources
            )
        )
        sha = commit_paths(
            context.services.execution_recorder,
            context.settings.repo_root,
            [Path(workspace.workspace_relative_dir)],
            message,
            context.settings.git_author_name,
            context.settings.git_author_email,
            operation_id=context.operation.id,
            step_name="commit_to_checkout",
            target_key=target.target_key,
        )
        workspace.last_applied_commit_sha = sha
        workspace.last_applied_at_utc = utc_now()
        workspace.last_plan_summary = (self._approved_summary(context) or PlanSummary()).model_dump()
        context.session.commit()
        self._record_adoption_status(context, "adopted")
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"commit_sha": sha})

    def _execute_refresh_resource(self, context: StepContext) -> StepOutcome:
        """Take a new inventory snapshot, so the adopted resources show as adopted at once.

        Like a Terraform-mode change, the adoption is committed by now, so a refresh that fails does not
        fail it: the step completes and says to run Refresh inventory.
        """
        return refresh_after_terraform_commit(context, refresh_target_now)
