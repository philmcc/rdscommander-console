"""Bring the rendered files and the record in line with an action that changed rendered attributes.

An action (a promotion, a maintenance action that upgrades the engine) changes AWS through the API in both
modes. When what it changed is something the Terraform files describe, the files and `console-record.json`
are re-rendered for the touched addresses only, from the snapshot the action's closing refresh took, and
committed under the operation's name:

* Terraform mode writes the files, runs `init`, brings state in line with AWS (`apply -refresh-only`) and
  requires a plan with no changes before it commits. A plan with changes means the files cannot describe
  what AWS now has; the files are put back as the last commit has them and the operator resolves it
  through Detect drift.
* Direct mode writes the files and the record and commits.

Both close the open drift findings on the touched addresses, as a committed change does.

Example:
    >>> reconcile_touched_addresses(context, {"aws_db_instance.demo_orders"})  # doctest: +SKIP
    StepOutcome(status=<StepOutcomeStatus.COMPLETED: 'completed'>, ...)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from rdscommander.control_db.models import ResourceAdoption, TerraformRun, TerraformWorkspace, utc_now
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeDiagnosticError
from rdscommander.operations.change_strategy import (
    PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN,
    RecordUnreadableError,
    close_drift_findings_for_addresses,
    with_touched_replaced,
)
from rdscommander.operations.enums import StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepOutcome
from rdscommander.parameters.direct_api import recorded_render_input
from rdscommander.repo.commit import GitCommitError, commit_paths, operation_commit_message
from rdscommander.repo.guard import CheckoutGuardReport, check_checkout
from rdscommander.terraform.plan_parser import PlanSummary, redact_plan_json, summarise_plan_json
from rdscommander.terraform.record import RECORD_FILE_NAME
from rdscommander.terraform.render_input import ImportBlock, RenderWorkspaceInput, build_render_input
from rdscommander.terraform.renderer import IMPORTS_FILE_NAME, render_workspace_files, write_workspace_files
from rdscommander.terraform.runner import mark_terraform_apply_invoked
from rdscommander.terraform.workspace_fingerprint import (
    WORKSPACE_FILES_FINGERPRINT_KEY,
    recorded_workspace_fingerprints,
    uncommitted_files_are_the_operations_own,
    workspace_files_fingerprint,
)
from rdscommander.terraform.workspace_revert import revert_workspace
from rdscommander.terraform.workspaces import (
    adopted_terraform_addresses,
    ensure_workspace_record,
    workspace_directory,
)

logger = get_logger(__name__)

RECONCILE_STEP_NAME = "reconcile"
RECONCILE_FILES_FINGERPRINT_PLAN_KEY = "reconcile_files_sha256"
"""Plan key holding the fingerprint of the files the reconcile step wrote, stored before it verifies.

The step is one unit of work, so a restart in the middle of it leaves its files uncommitted with no
completed attempt to carry the fingerprint; the plan keeps it so the resumed step accepts them as its own.
"""
CREATE_ONLY_INSTANCE_ATTRIBUTES = (
    "snapshot_identifier",
    "restore_to_point_in_time",
    "manage_master_user_password",
)
"""How an instance was created; AWS never reports them, so an entry moved to another address keeps them."""
STATE_REMOVE_NOTHING_TO_REMOVE = "No matching objects found"
"""What `state rm` says when state does not hold the address (a resumed re-import removed it already)."""
RECONCILE_NOT_CLEAN_FIX = (
    "run Detect drift and resolve it (accept what AWS has into the record); the action itself is done in AWS"
)
AFTER_ACTION_SENTENCE = "The action is done in AWS; the record does not describe it yet."


@dataclass(frozen=True)
class StateMove:
    """An object Terraform state tracks at `source_address` that now answers to another AWS name.

    Reconcile moves the state entry to `destination_address` (`state mv`) before re-importing the source
    address, renders the object there from the fresh snapshot (keeping the source entry's create-only
    fields), and once committed records it as adopted, so the console keeps managing it.

    Attributes:
        source_address: The address state tracks it at now, e.g. `aws_db_instance.demo_orders`.
        destination_address: Its address from now on, e.g. `aws_db_instance.demo_orders_old1`.
        resource_arn: Its ARN under the new name.
        resource_identifier: Its identifier under the new name, e.g. `demo-orders-old1`.
        resource_type: Its Terraform type, e.g. `aws_db_instance`.
    """

    source_address: str
    destination_address: str
    resource_arn: str
    resource_identifier: str
    resource_type: str


def _operation_owns_uncommitted_files(
    context: StepContext, workspace: TerraformWorkspace
) -> Callable[[list[str]], bool]:
    """The owner check the guard asks: are the uncommitted files exactly what this operation wrote?

    The fingerprints come from the operation's completed steps and from the plan, where the reconcile step
    keeps the fingerprint of its own files from the moment it writes them.
    """
    fingerprints = recorded_workspace_fingerprints(context)
    from_plan = context.operation.plan.get(RECONCILE_FILES_FINGERPRINT_PLAN_KEY)
    if from_plan:
        fingerprints.add(str(from_plan))
    workspace_dir = workspace_directory(context.settings.repo_root, workspace)
    relative_dir = Path(workspace.workspace_relative_dir).as_posix()

    def owns(uncommitted_paths: list[str]) -> bool:
        return uncommitted_files_are_the_operations_own(
            workspace_dir, relative_dir, uncommitted_paths, fingerprints
        )

    return owns


def reconcile_checkout_report(context: StepContext, step_name: str) -> CheckoutGuardReport:
    """The checkout guard the reconcile step runs, for the executor to run earlier, at approval.

    The workspace must be clean (or hold exactly the files this operation's reconcile step already wrote),
    nothing may be staged elsewhere and the branch must not be behind its upstream. Asking at approval
    keeps an operator from approving an action whose reconcile could not commit.

    Args:
        context: The running step's context.
        step_name: Step the git invocations are recorded under.

    Returns:
        The guard's report; `report.ok` is False when the checkout is not ready.

    Example:
        >>> reconcile_checkout_report(context, "await_approval").ok  # doctest: +SKIP
        True
    """
    workspace = ensure_workspace_record(context.session, context.target, context.console_config)
    context.session.commit()
    return check_checkout(
        context.services.execution_recorder,
        context.settings.repo_root,
        Path(workspace.workspace_relative_dir),
        workspace.last_applied_commit_sha,
        context.settings.allow_checkout_without_upstream,
        operation_id=context.operation.id,
        step_name=step_name,
        target_key=context.target.target_key,
        operation_owns_uncommitted_files=_operation_owns_uncommitted_files(context, workspace),
    )


def checkout_guard_fix_suggestion(report: CheckoutGuardReport) -> str:
    """The guard's fixes, joined as one sentence fragment."""
    return "; ".join(problem.fix_suggestion for problem in report.problems)


def _fail(
    context: StepContext, step_name: str, kind: str, title: str, detail: str, fix_suggestion: str
) -> StepOutcome:
    """Mark the action applied without a record, record a diagnostic and fail the step.

    Every reconcile failure comes after the action changed AWS, so change history must still attribute
    the change to the operation.
    """
    context.operation.plan = {**context.operation.plan, "applied_without_record": True}
    context.session.commit()
    record_diagnostic(
        context.session,
        operation_id=context.operation.id,
        step_name=step_name,
        severity="error",
        diagnostic_kind=kind,
        title=title,
        detail=f"{detail}\n\n{AFTER_ACTION_SENTENCE}" if detail else AFTER_ACTION_SENTENCE,
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
    context: StepContext,
    workspace: TerraformWorkspace,
    step_name: str,
    kind: str,
    title: str,
    detail: str,
    fix_suggestion: str,
) -> StepOutcome:
    """Put the workspace back as the last commit has it, then fail; the checkout guard stays satisfied."""
    if not revert_workspace(context, workspace, step_name):
        relative_dir = Path(workspace.workspace_relative_dir).as_posix()
        fix_suggestion = f"run `git checkout -- {relative_dir}`, then {fix_suggestion}"
    return _fail(context, step_name, kind, title, detail, fix_suggestion)


def _record_terraform_run(
    context: StepContext,
    workspace: TerraformWorkspace,
    run_kind: str,
    exit_code: int,
    execution_record_id: int | None,
    plan_json: dict | None = None,
    summary: PlanSummary | None = None,
) -> None:
    """Keep the redacted plan of this invocation with the operation, as the change strategy does."""
    context.session.add(
        TerraformRun(
            workspace_id=workspace.id,
            operation_id=context.operation.id,
            run_kind=run_kind,
            plan_json=redact_plan_json(plan_json) if plan_json else None,
            plan_summary=summary.model_dump(mode="json") if summary else None,
            exit_code=exit_code,
            execution_record_id=execution_record_id,
        )
    )
    context.session.commit()


def _with_create_only_fields_moved(
    recorded: RenderWorkspaceInput | None, reconciled: RenderWorkspaceInput, state_moves: list[StateMove]
) -> RenderWorkspaceInput:
    """`reconciled` with each moved instance keeping the create-only fields its recorded entry had."""
    if recorded is None:
        return reconciled
    recorded_by_name = {f"aws_db_instance.{item.terraform_name}": item for item in recorded.instances}
    for move in state_moves:
        source = recorded_by_name.get(move.source_address)
        destination = next(
            (
                item
                for item in reconciled.instances
                if f"aws_db_instance.{item.terraform_name}" == move.destination_address
            ),
            None,
        )
        if source is None or destination is None:
            continue
        for attribute in CREATE_ONLY_INSTANCE_ATTRIBUTES:
            setattr(destination, attribute, getattr(source, attribute))
    return reconciled


def _reconciled_input(
    context: StepContext,
    workspace: TerraformWorkspace,
    addresses: set[str],
    state_moves: list[StateMove] | None = None,
) -> tuple[RenderWorkspaceInput, int]:
    """The recorded input with only `addresses` taken from a fresh render of the latest snapshot.

    The destinations of `state_moves` are rendered as adopted (they are under Terraform once moved).

    Returns:
        The input to write and the id of the snapshot it was rendered from.

    Raises:
        ValueError: The target has never been inventoried.
        RecordUnreadableError: `console-record.json` exists but cannot be read.
    """
    target = context.target
    snapshot = latest_snapshot_for_target(context.session, target.target_key)
    if snapshot is None:
        raise ValueError(f"{target.target_key} has never been inventoried; refresh inventory first")
    try:
        recorded = recorded_render_input(context)
    except ValueError as error:
        directory = workspace_directory(context.settings.repo_root, workspace)
        raise RecordUnreadableError(f"{directory / RECORD_FILE_NAME}: {error}") from error
    fresh = build_render_input(
        context.session,
        target,
        context.console_config,
        snapshot,
        set(),
        adopted_terraform_addresses(context.session, workspace)
        | {move.destination_address for move in state_moves or []},
        include_imports=False,
        simulated_endpoint_url=context.settings.simulated_aws_endpoint_url,
        include_all=target.is_direct,
    )
    reconciled = with_touched_replaced(recorded, addresses, fresh)
    return _with_create_only_fields_moved(recorded, reconciled, state_moves or []), snapshot.id


def _verify_with_terraform(
    context: StepContext,
    workspace: TerraformWorkspace,
    workspace_dir: Path,
    step_name: str,
    finish_note: str = "",
) -> StepOutcome | None:
    """`init`, `apply -refresh-only`, then a plan that must propose nothing; the failed outcome, or None.

    `finish_note` (after a switchover: what state already reflects and the commands that finish by hand)
    takes the place of "then run Detect drift" in every fix suggestion.
    """
    then_resolve = finish_note or ", then run Detect drift and resolve it"
    runner = context.services.terraform_runner
    arguments = {"operation_id": context.operation.id, "step_name": step_name}
    initialised = runner.init(workspace_dir, context.target, context.settings, **arguments)
    _record_terraform_run(context, workspace, "init", initialised.exit_code, initialised.record_id)
    if not initialised.succeeded:
        return _fail_and_revert(
            context,
            workspace,
            step_name,
            "terraform_init_failed",
            "Terraform init failed while reconciling the files",
            initialised.standard_error[-2000:],
            "open the execution record and fix the provider or backend problem" + then_resolve,
        )
    refreshed = runner.apply_refresh_only(workspace_dir, context.target, context.settings, **arguments)
    _record_terraform_run(context, workspace, "refresh", refreshed.exit_code, refreshed.record_id)
    if not refreshed.succeeded:
        return _fail_and_revert(
            context,
            workspace,
            step_name,
            "terraform_refresh_failed",
            "Refreshing Terraform state failed while reconciling the files",
            refreshed.standard_error[-2000:],
            "open the execution record and fix credentials or backend access" + then_resolve,
        )
    run = runner.plan(workspace_dir, context.target, context.settings, **arguments)
    if run.result.exit_code not in PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN:
        _record_terraform_run(context, workspace, "plan", run.result.exit_code, run.result.record_id)
        return _fail_and_revert(
            context,
            workspace,
            step_name,
            "terraform_plan_failed",
            "Terraform plan failed while reconciling the files",
            run.result.standard_error[-2000:],
            "open the execution record and fix the reported problem" + then_resolve,
        )
    summary = summarise_plan_json(run.plan_json) if run.plan_json else PlanSummary()
    _record_terraform_run(
        context, workspace, "verify_plan", run.result.exit_code, run.result.record_id, run.plan_json, summary
    )
    if not run.has_changes:
        return None
    residual_changes = [
        f"{change.address}: {', '.join(change.actions)} on "
        f"{', '.join(change.changed_attribute_names) or 'unknown attributes'}"
        for change in summary.resource_changes
        if change.actions != ["no-op"]
    ]
    return _fail_and_revert(
        context,
        workspace,
        step_name,
        "reconcile_not_clean",
        "The files do not describe what AWS has after the action",
        "\n".join(residual_changes) or "the plan proposes changes",
        RECONCILE_NOT_CLEAN_FIX + finish_note,
    )


def _reimport_into_state(
    context: StepContext,
    workspace: TerraformWorkspace,
    workspace_dir: Path,
    record_input: RenderWorkspaceInput,
    import_ids: dict[str, str],
    step_name: str,
    state_moves: list[StateMove] | None = None,
    moves_in_state: list[StateMove] | None = None,
) -> StepOutcome | None:
    """Point state at the objects that now answer to `import_ids`' addresses: `state rm`, then import.

    The import goes through the same `import {}` blocks an adoption writes (`imports.tf`), planned and
    applied; the plan may only import (no create, update or delete), and `imports.tf` is removed again
    before the files are verified and committed. AWS is not changed.

    Each of `state_moves` runs first (`state mv`, which leaves state as it was when it fails), so the
    object the source address tracked keeps being managed at its new address. State is listed before: a
    move whose destination is already in state and whose source is not was done by an earlier attempt
    and is skipped. With both present, `state show` tells them apart: the destination tracking the moved
    identifier and the source tracking its import id means an earlier attempt did the move and the import,
    and both are skipped; anything else is refused (`terraform_state_move_conflict`). Every move whose
    destination state now tracks is appended to `moves_in_state`, so the caller can keep the record in
    line on a failure.

    Returns:
        The failed outcome, or None once every address is imported.
    """
    runner = context.services.terraform_runner
    arguments = {"operation_id": context.operation.id, "step_name": step_name}
    addresses = sorted(import_ids)
    logger.info("reconcile_reimport_started", operation_id=str(context.operation.id), addresses=addresses)
    # Until the moves run, state still tracks the blue side at the production address.
    before_move_note = _switchover_note(context, state_moves or [], import_ids)
    initialised = runner.init(workspace_dir, context.target, context.settings, **arguments)
    _record_terraform_run(context, workspace, "init", initialised.exit_code, initialised.record_id)
    if not initialised.succeeded:
        return _fail_and_revert(
            context,
            workspace,
            step_name,
            "terraform_init_failed",
            "Terraform init failed while importing the switched resource",
            initialised.standard_error[-2000:],
            "open the execution record and fix the provider or backend problem"
            + (before_move_note or ", then run Detect drift and resolve it"),
        )
    in_state: set[str] = set()
    if state_moves:
        listed = runner.state_list(workspace_dir, context.target, context.settings, **arguments)
        if not listed.succeeded:
            return _fail_and_revert(
                context,
                workspace,
                step_name,
                "terraform_state_list_failed",
                "Terraform could not list its state",
                listed.standard_error[-2000:],
                "open the execution record and fix the backend problem; state is unchanged"
                + (before_move_note or ", so run Detect drift and resolve it"),
            )
        in_state = {line.strip() for line in listed.standard_output.splitlines() if line.strip()}
    already_imported: set[str] = set()
    for move in state_moves or []:
        source, destination = move.source_address, move.destination_address
        if destination in in_state and source in in_state:
            identifiers = {
                address: _state_identifier(context, workspace_dir, address, step_name)
                for address in (source, destination)
            }
            if (
                identifiers[source] == import_ids.get(source)
                and identifiers[destination] == move.resource_identifier
            ):
                # An earlier attempt moved the blue side and imported production before it stopped.
                logger.info(
                    "reconcile_state_move_and_import_already_done",
                    operation_id=str(context.operation.id),
                    source_address=source,
                    source_identifier=identifiers[source],
                    destination_address=destination,
                    destination_identifier=identifiers[destination],
                )
                already_imported.add(source)
                if moves_in_state is not None:
                    moves_in_state.append(move)
                continue
            if moves_in_state is not None:
                # State tracks the destination: the caller keeps it described, so no plan destroys it.
                moves_in_state.append(move)
            return _fail_and_revert(
                context,
                workspace,
                step_name,
                "terraform_state_move_conflict",
                "State already tracks both the production and the blue side address",
                f"state tracks {source} (identifier {identifiers[source] or 'unreadable'}) and "
                f"{destination} (identifier {identifiers[destination] or 'unreadable'}); the console "
                "cannot tell which one is the blue side",
                f"check both in the workspace with `terraform state show {source}` and `terraform state show "
                f"{destination}`, remove the wrong one with `terraform state rm`, then run Detect drift and "
                "resolve it",
            )
        if destination in in_state:
            logger.info(
                "reconcile_state_move_already_done",
                operation_id=str(context.operation.id),
                source_address=source,
                destination_address=destination,
            )
            if moves_in_state is not None:
                moves_in_state.append(move)
            continue
        moved = runner.state_move(
            workspace_dir,
            context.target,
            context.settings,
            move.source_address,
            move.destination_address,
            **arguments,
        )
        if not moved.succeeded:
            source = move.source_address
            return _fail_and_revert(
                context,
                workspace,
                step_name,
                "terraform_state_move_failed",
                "Terraform could not move the blue side to its own address",
                moved.standard_error[-2000:],
                "state is unchanged; fix the cause in the detail"
                + (before_move_note or ", then run Detect drift and resolve it"),
            )
        if moves_in_state is not None:
            moves_in_state.append(move)
    moved_note = _switchover_note(context, moves_in_state or [], import_ids, state_already_moved=True)
    addresses = [address for address in addresses if address not in already_imported]
    if not addresses:
        logger.info("reconcile_reimport_already_done", operation_id=str(context.operation.id))
        return None
    for address in addresses:
        removed = runner.state_remove(workspace_dir, context.target, context.settings, address, **arguments)
        if not removed.succeeded and STATE_REMOVE_NOTHING_TO_REMOVE not in removed.standard_error:
            return _fail_and_revert(
                context,
                workspace,
                step_name,
                "terraform_state_remove_failed",
                "Terraform could not forget the replaced resource",
                removed.standard_error[-2000:],
                "open the execution record and fix the backend problem"
                + (
                    moved_note
                    or f", then run `terraform state rm {address}` and `terraform import {address} "
                    f"{import_ids[address]}` in the workspace"
                ),
            )
    with_imports = record_input.model_copy(
        update={
            "import_blocks": [
                ImportBlock(to_address=address, import_id=import_ids[address]) for address in addresses
            ]
        }
    )
    imports_file = workspace_dir / IMPORTS_FILE_NAME
    imports_file.write_text(
        render_workspace_files(with_imports, format_binary=context.settings.terraform_binary)[
            IMPORTS_FILE_NAME
        ]
    )
    not_imported_fix = f"state no longer holds {', '.join(addresses)}" + (
        moved_note
        or ": run `terraform import` for "
        + ", ".join(f"{address} {import_ids[address]}" for address in addresses)
        + " in the workspace (or adopt the resource again), then run Detect drift and resolve it"
    )
    run = runner.plan(workspace_dir, context.target, context.settings, **arguments)
    summary = summarise_plan_json(run.plan_json) if run.plan_json else PlanSummary()
    _record_terraform_run(
        context, workspace, "plan", run.result.exit_code, run.result.record_id, run.plan_json, summary
    )
    other_changes = [
        f"{change.address}: {', '.join(change.actions)}"
        for change in summary.resource_changes
        if change.actions != ["no-op"]
    ]
    if run.result.exit_code not in PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN or other_changes:
        imports_file.unlink(missing_ok=True)
        return _fail_and_revert(
            context,
            workspace,
            step_name,
            "reconcile_import_not_clean",
            "Importing the switched resource would change more than state",
            "\n".join(other_changes) or run.result.standard_error[-2000:],
            not_imported_fix,
        )
    mark_terraform_apply_invoked(context.session, context.operation)
    applied = runner.apply(workspace_dir, context.target, context.settings, **arguments)
    _record_terraform_run(context, workspace, "apply", applied.exit_code, applied.record_id)
    imports_file.unlink(missing_ok=True)
    if not applied.succeeded:
        return _fail_and_revert(
            context,
            workspace,
            step_name,
            "terraform_apply_failed",
            "Terraform could not import the switched resource",
            applied.standard_error[-2000:],
            not_imported_fix,
        )
    logger.info("reconcile_reimport_completed", operation_id=str(context.operation.id), addresses=addresses)
    return None


def _state_identifier(context: StepContext, workspace_dir: Path, address: str, step_name: str) -> str:
    """The AWS identifier state tracks at `address` (`state show`); empty when it cannot be read."""
    shown = context.services.terraform_runner.state_show(
        workspace_dir,
        context.target,
        context.settings,
        address,
        operation_id=context.operation.id,
        step_name=step_name,
    )
    if not shown.result.succeeded:
        logger.warning(
            "reconcile_state_show_failed",
            operation_id=str(context.operation.id),
            terraform_address=address,
            fix_suggestion="open the execution record for the binary's error; the reconcile treats the "
            "address as unknown",
        )
    return str(shown.attributes.get("identifier") or "")


def switchover_finish_by_hand(
    context: StepContext,
    production_address: str,
    blue_side_address: str,
    production_identifier: str,
    *,
    state_already_moved: bool = False,
    already_imported: bool = False,
) -> str:
    """The fix-suggestion clause that finishes a switchover's Terraform reconcile by hand.

    After a blue/green switchover, state tracks the blue instance at the production address until the
    reconcile moves it to its own address and imports production again; any plan applied before that
    would change the blue side. The commands run in the target's workspace, with the configured binary,
    and skip what state already reflects.

    Args:
        context: The running step's context (settings and target give the workspace and binary).
        production_address: e.g. `aws_db_instance.demo_orders`.
        blue_side_address: e.g. `aws_db_instance.demo_orders_old1`.
        production_identifier: The import id of production, e.g. `demo-orders`.
        state_already_moved: State tracks the blue side at `blue_side_address` already.
        already_imported: Production is imported at `production_address` already.

    Returns:
        The clause, e.g. "before any plan is applied in this workspace, finish by hand: run `cd ...;
        tofu state mv ...; tofu import ...; tofu plan`, ...".

    Example:
        >>> switchover_finish_by_hand(  # doctest: +SKIP
        ...     context, "aws_db_instance.demo_orders", "aws_db_instance.demo_orders_old1", "demo-orders"
        ... )
        'before any plan is applied in this workspace, finish by hand: run `cd /repo/terraform/...; ...`'
    """
    binary = context.settings.terraform_binary
    workspace_dir = context.settings.repo_root / context.target.workspace_relative_dir
    commands = [f"cd {workspace_dir}"]
    if not state_already_moved:
        commands.append(f"{binary} state mv {production_address} {blue_side_address}")
    if not already_imported:
        commands.append(f"{binary} import {production_address} {production_identifier}")
    commands.append(f"{binary} plan")
    return (
        f"before any plan is applied in this workspace, finish by hand: run `{'; '.join(commands)}`, check "
        "the plan without applying it, then run Detect drift and accept what AWS has into the record"
    )


def _switchover_note(
    context: StepContext,
    state_moves: list[StateMove],
    import_ids: dict[str, str],
    *,
    state_already_moved: bool = False,
    already_imported: bool = False,
) -> str:
    """What a failure after a switchover adds to its fix suggestion; empty when nothing is to be moved.

    Once state moved the blue side, it says the committed record keeps the moved resources there (so no
    plan destroys them); then it names the commands that finish by hand (`switchover_finish_by_hand`).
    """
    moves = [move for move in state_moves if move.source_address in import_ids]
    if not moves:
        return ""
    note = ""
    if state_already_moved:
        moved = ", ".join(f"{move.source_address} to {move.destination_address}" for move in moves)
        note += (
            f"; state already moved {moved}, and the committed record keeps the moved resources there, so no "
            "plan destroys them"
        )
    for move in moves:
        note += "; " + switchover_finish_by_hand(
            context,
            move.source_address,
            move.destination_address,
            import_ids[move.source_address],
            state_already_moved=state_already_moved,
            already_imported=already_imported,
        )
    return note


def _keep_moved_in_record(
    context: StepContext,
    workspace: TerraformWorkspace,
    workspace_dir: Path,
    record_input: RenderWorkspaceInput,
    moves_in_state: list[StateMove],
    snapshot_id: int,
    step_name: str,
    finish_note: str = "",
) -> None:
    """After a failure that reverted the files, commit the moved resources into the record and files.

    State tracks them at their new addresses already; without them in the files the next plan in the
    workspace would destroy them (the blue side of an upgrade). The committed record is kept as it was,
    with only the moved addresses added from this attempt's render, and they are recorded as adopted.
    When that commit fails, the diagnostic's fix suggestion ends with `finish_note` (the commands that
    finish the reconcile by hand).
    """
    destinations = {move.destination_address for move in moves_in_state}
    try:
        kept = with_touched_replaced(recorded_render_input(context), destinations, record_input)
        files = render_workspace_files(kept, format_binary=context.settings.terraform_binary)
        write_workspace_files(workspace_dir, files, record=kept, snapshot_id=snapshot_id)
        commit_sha = commit_paths(
            context.services.execution_recorder,
            context.settings.repo_root,
            [Path(workspace.workspace_relative_dir)],
            f"Keep {', '.join(sorted(destinations))} under Terraform after a failed reconcile of "
            f"{context.operation.title or context.operation.operation_kind}",
            context.settings.git_author_name,
            context.settings.git_author_email,
            operation_id=context.operation.id,
            step_name=step_name,
            target_key=context.target.target_key,
            allow_empty=True,
        )
    except (GitCommitError, ValueError) as error:
        context.session.rollback()
        fix_suggestion = (
            f"add {', '.join(sorted(destinations))} to the files by hand (state tracks them there) or move "
            "them back with `terraform state mv`, before any other plan is applied in this workspace"
            + finish_note
        )
        logger.error(
            "reconcile_moved_resources_not_kept",
            operation_id=str(context.operation.id),
            destination_addresses=sorted(destinations),
            error=str(error),
            fix_suggestion=fix_suggestion,
        )
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name=step_name,
            severity="error",
            diagnostic_kind="moved_resources_not_recorded",
            title="State tracks resources the files do not describe",
            detail=str(error),
            fix_suggestion=fix_suggestion,
        )
        context.session.commit()
        return
    workspace.last_applied_commit_sha = commit_sha
    workspace.last_applied_at_utc = utc_now()
    _record_moved_as_adopted(context, workspace, moves_in_state)
    context.session.commit()
    logger.info(
        "reconcile_moved_resources_kept",
        operation_id=str(context.operation.id),
        destination_addresses=sorted(destinations),
        commit_sha=commit_sha,
    )


def _record_moved_as_adopted(
    context: StepContext, workspace: TerraformWorkspace, state_moves: list[StateMove]
) -> None:
    """Record each moved object as adopted at its new address (the caller commits)."""
    for move in state_moves:
        adoption = context.session.execute(
            select(ResourceAdoption).where(
                ResourceAdoption.workspace_id == workspace.id,
                ResourceAdoption.terraform_address == move.destination_address,
            )
        ).scalar_one_or_none()
        if adoption is None:
            adoption = ResourceAdoption(workspace_id=workspace.id, terraform_address=move.destination_address)
            context.session.add(adoption)
        adoption.operation_id = context.operation.id
        adoption.resource_arn = move.resource_arn
        adoption.resource_identifier = move.resource_identifier
        adoption.resource_type = move.resource_type
        adoption.adoption_status = "adopted"
        adoption.adopted_at_utc = utc_now()
        logger.info(
            "reconcile_moved_resource_adopted",
            operation_id=str(context.operation.id),
            terraform_address=move.destination_address,
            resource_identifier=move.resource_identifier,
        )


def reconcile_touched_addresses(
    context: StepContext,
    addresses: set[str],
    step_name: str = RECONCILE_STEP_NAME,
    reimport_import_ids: dict[str, str] | None = None,
    state_moves: list[StateMove] | None = None,
) -> StepOutcome:
    """Re-render and commit the touched addresses after an action changed them in AWS.

    The checkout guard runs first, so the commit holds only what this step wrote. The files and the record
    are the recorded input with only `addresses` replaced from the latest snapshot. Terraform mode then
    refreshes state and requires a clean plan (else fails `reconcile_not_clean` and reverts the files);
    both modes commit as `<title> (<operator>, approved by <approver>)` and close the open drift findings
    on the addresses. Any failure marks `plan["applied_without_record"]`, because AWS already changed.

    In Terraform mode an address of `reimport_import_ids` is first removed from state and imported again
    by the given id, for an action that put another AWS object behind the address (a blue/green switchover
    leaves state tracking the old instance's resource id).

    Args:
        context: The running step's context.
        addresses: Terraform addresses the action changed.
        step_name: Step every git, Terraform and AWS call is recorded under.
        reimport_import_ids: Terraform mode only: `{address: import id}` to import again before verifying.
        state_moves: Terraform mode only: state entries to move before the re-import (see `StateMove`);
            their destinations must be among `addresses`, and are recorded as adopted once committed.

    Returns:
        Completed with `{"commit_sha", "closed_drift_findings", "files", "snapshot_id"}`, or failed with a
        diagnostic.

    Example:
        >>> reconcile_touched_addresses(context, {"aws_db_instance.demo_orders"}).status  # doctest: +SKIP
        <StepOutcomeStatus.COMPLETED: 'completed'>
    """
    workspace = ensure_workspace_record(context.session, context.target, context.console_config)
    context.session.commit()
    relative_dir = Path(workspace.workspace_relative_dir)
    workspace_dir = workspace_directory(context.settings.repo_root, workspace)
    logger.info(
        "action_reconcile_started",
        operation_id=str(context.operation.id),
        target_key=context.target.target_key,
        touched_addresses=sorted(addresses),
    )
    # After a switchover, state tracks the blue side at the production address until this step is done.
    switchover_note = (
        ""
        if context.target.is_direct
        else _switchover_note(context, state_moves or [], reimport_import_ids or {})
    )
    report = reconcile_checkout_report(context, step_name)
    if not report.ok:
        return _fail(
            context,
            step_name,
            "checkout_guard",
            "Repository checkout is not ready for the reconciled files",
            report.summary(),
            checkout_guard_fix_suggestion(report)
            + (switchover_note or "; then run Detect drift and resolve it"),
        )
    try:
        record_input, snapshot_id = _reconciled_input(
            context, workspace, addresses, state_moves if not context.target.is_direct else None
        )
    except ChangeDiagnosticError as error:
        return _fail(
            context, step_name, error.diagnostic_kind, error.title, str(error), error.fix_suggestion or ""
        )
    except (KeyError, ValueError) as error:
        message = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
        return _fail(
            context,
            step_name,
            "reconcile_not_possible",
            "The files could not be re-rendered after the action",
            message,
            "refresh inventory" + (switchover_note or ", then run Detect drift and resolve it"),
        )
    files = render_workspace_files(record_input, format_binary=context.settings.terraform_binary)
    written = write_workspace_files(workspace_dir, files, record=record_input, snapshot_id=snapshot_id)
    written_files_fingerprint = workspace_files_fingerprint(workspace_dir)
    context.operation.plan = {
        **context.operation.plan,
        RECONCILE_FILES_FINGERPRINT_PLAN_KEY: written_files_fingerprint,
    }
    context.session.commit()
    if not context.target.is_direct:
        refusal = None
        moves_in_state: list[StateMove] = []
        if reimport_import_ids:
            refusal = _reimport_into_state(
                context,
                workspace,
                workspace_dir,
                record_input,
                reimport_import_ids,
                step_name,
                state_moves,
                moves_in_state,
            )
        imported = refusal is None
        finish_note = _switchover_note(
            context,
            moves_in_state,
            reimport_import_ids or {},
            state_already_moved=True,
            already_imported=imported and bool(reimport_import_ids),
        )
        if refusal is None:
            refusal = _verify_with_terraform(context, workspace, workspace_dir, step_name, finish_note)
        if refusal is not None:
            if moves_in_state:
                # The files were reverted but state already moved: keep the moved resources described.
                _keep_moved_in_record(
                    context,
                    workspace,
                    workspace_dir,
                    record_input,
                    moves_in_state,
                    snapshot_id,
                    step_name,
                    finish_note,
                )
            return refusal
    message = operation_commit_message(
        context.operation.title or context.operation.operation_kind,
        context.operation.created_by_operator_name,
        context.operation.approved_by_operator_name,
    )
    try:
        commit_sha = commit_paths(
            context.services.execution_recorder,
            context.settings.repo_root,
            [relative_dir],
            message,
            context.settings.git_author_name,
            context.settings.git_author_email,
            operation_id=context.operation.id,
            step_name=step_name,
            target_key=context.target.target_key,
            allow_empty=True,
        )
    except GitCommitError as error:
        return _fail(
            context,
            step_name,
            "commit_failed",
            "Committing the reconciled files failed",
            str(error),
            "open the execution record for the git error (identity, hooks, repository state), commit the "
            "workspace by hand and run Detect drift",
        )
    workspace.last_applied_commit_sha = commit_sha
    workspace.last_applied_at_utc = utc_now()
    if not context.target.is_direct:
        _record_moved_as_adopted(context, workspace, state_moves or [])
    context.session.commit()
    closed_findings = close_drift_findings_for_addresses(context.session, context.operation, addresses)
    logger.info(
        "action_reconciled",
        operation_id=str(context.operation.id),
        target_key=context.target.target_key,
        commit_sha=commit_sha,
        closed_drift_findings=closed_findings,
    )
    return StepOutcome(
        status=StepOutcomeStatus.COMPLETED,
        output={
            "commit_sha": commit_sha,
            "closed_drift_findings": closed_findings,
            "files": [path.name for path in written],
            "snapshot_id": snapshot_id,
            WORKSPACE_FILES_FINGERPRINT_KEY: written_files_fingerprint,
        },
    )
