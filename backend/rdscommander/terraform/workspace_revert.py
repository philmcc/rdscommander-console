"""Put a Terraform workspace back the way the last commit has it.

An operation that writes the workspace files before it is certain to commit them must never leave them
behind: a dirty workspace makes the checkout guard refuse every later operation on that target. Executors
call `revert_workspace` when such an operation fails before anything changed in AWS, and implement
`on_cancelled` with `revert_workspace_of_cancelled_operation` so that rejecting an operation that is
waiting for approval cleans up as well.

Example:
    >>> revert_workspace(context, workspace, "policy_check")  # doctest: +SKIP
    True
"""

from __future__ import annotations

from pathlib import Path

from rdscommander.control_db.models import TerraformWorkspace
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.logging_setup import get_logger
from rdscommander.operations.executor import StepContext
from rdscommander.repo.git_runner import run_git_recorded
from rdscommander.repo.guard import uncommitted_paths_from_porcelain
from rdscommander.terraform.record import RECORD_FILE_NAME
from rdscommander.terraform.renderer import MANAGED_FILE_NAMES
from rdscommander.terraform.workspace_fingerprint import FILES_WRITTEN_BY_INIT, operation_owner_check

logger = get_logger(__name__)

CHECK_CHECKOUT_STEP = "check_checkout"
CLEANUP_STEP = "cleanup"


def revert_workspace(
    context: StepContext, workspace: TerraformWorkspace, step_name: str | None = None
) -> bool:
    """Put the workspace directory back as `HEAD` has it.

    Unstages the workspace, restores the tracked files (including managed files that a render deleted),
    then deletes the renderer's files, the record and the provider lock file `init` writes when `HEAD` does
    not have them (a rejected first operation of a target must not leave the lock file to dirty the next).
    Nothing outside the workspace directory is touched, so another environment's uncommitted work is safe.
    Every git command goes through the recorded runner.

    Args:
        context: The running step's context (recorder, repository root, operation and target).
        workspace: The workspace row; its `workspace_relative_dir` is the only path touched.
        step_name: Step the git invocations are recorded under; defaults to `cleanup`.

    Returns:
        True when the tracked files were restored; False when `git checkout` failed, in which case the
        operator has to run `git checkout -- <workspace>` by hand.

    Example:
        >>> revert_workspace(context, workspace, "policy_check")  # doctest: +SKIP
        True
    """
    relative_dir = Path(workspace.workspace_relative_dir)
    directory = context.settings.repo_root / relative_dir
    git_arguments = {
        "operation_id": context.operation.id,
        "step_name": step_name or CLEANUP_STEP,
        "target_key": context.operation.target_key,
    }

    def git(*arguments: str):  # noqa: ANN202
        return run_git_recorded(
            context.services.execution_recorder,
            context.settings.repo_root,
            list(arguments),
            **git_arguments,
        )

    git("reset", "-q", "HEAD", "--", relative_dir.as_posix())
    tracked = git("ls-tree", "-r", "--name-only", "HEAD", "--", relative_dir.as_posix())
    tracked_paths = set(tracked.standard_output.splitlines()) if tracked.succeeded else set()
    checkout_succeeded = True
    if tracked_paths:
        checkout_succeeded = git("checkout", "--", relative_dir.as_posix()).succeeded
    for file_name in (*MANAGED_FILE_NAMES, RECORD_FILE_NAME, *FILES_WRITTEN_BY_INIT):
        file_path = directory / file_name
        if file_path.exists() and (relative_dir / file_name).as_posix() not in tracked_paths:
            file_path.unlink()
    if checkout_succeeded:
        logger.info(
            "workspace_reverted",
            operation_id=str(context.operation.id),
            workspace_relative_dir=relative_dir.as_posix(),
        )
        return True
    logger.error(
        "workspace_revert_failed",
        operation_id=str(context.operation.id),
        workspace_relative_dir=relative_dir.as_posix(),
        fix_suggestion=f"run `git checkout -- {relative_dir.as_posix()}` by hand",
    )
    return False


def checkout_holds_the_operations_files(context: StepContext, workspace: TerraformWorkspace) -> bool:
    """Whether this console's checkout holds the files the operation wrote, unchanged since it wrote them.

    The console that rendered the files may have come back under another runner identity (a re-created
    container); the files themselves say whose they are (see `terraform.workspace_fingerprint`).

    Example:
        >>> checkout_holds_the_operations_files(context, workspace)  # doctest: +SKIP
        True
    """
    relative_dir = Path(workspace.workspace_relative_dir).as_posix()
    listing = run_git_recorded(
        context.services.execution_recorder,
        context.settings.repo_root,
        ["-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all", "--", relative_dir],
        operation_id=context.operation.id,
        step_name=CLEANUP_STEP,
        target_key=context.operation.target_key,
    )
    if not listing.succeeded:
        return False
    uncommitted = uncommitted_paths_from_porcelain(listing.standard_output)
    return bool(uncommitted) and operation_owner_check(context, workspace)(uncommitted)


def revert_workspace_of_cancelled_operation(context: StepContext, workspace: TerraformWorkspace) -> bool:
    """Clean up after a rejected operation, on the machine that wrote its files.

    The files of an operation that waits for approval exist only in the checkout of the console that ran
    its checkout guard. That console is recognised by its runner identity or, when the identity changed
    (a re-created container), by the files themselves: uncommitted files that are exactly what the
    operation wrote. A different console finds nothing of the operation to clean and must leave the
    cleanup pending for the right one.

    Args:
        context: The context the runner built for the cancelled operation.
        workspace: The operation's workspace row.

    Returns:
        True when the cleanup was attempted here or there was nothing to clean (the operation never passed
        its checkout guard); False when the files are in another console's checkout, so the runner keeps
        the cleanup pending. A revert that fails is attempted once and reported as a warning diagnostic on
        the operation.

    Example:
        >>> revert_workspace_of_cancelled_operation(context, workspace)  # doctest: +SKIP
        True
    """
    guard_output = context.completed_step_output(CHECK_CHECKOUT_STEP)
    if guard_output is None:
        return True
    same_console = guard_output.get("runner_identity") == context.runner_identity
    if not same_console and not checkout_holds_the_operations_files(context, workspace):
        logger.info(
            "workspace_cleanup_left_for_another_console",
            operation_id=str(context.operation.id),
            owner_runner_identity=guard_output.get("runner_identity"),
            runner_identity=context.runner_identity,
        )
        return False
    if not revert_workspace(context, workspace, "on_cancelled"):
        relative_dir = Path(workspace.workspace_relative_dir).as_posix()
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name="on_cancelled",
            severity="warning",
            diagnostic_kind="workspace_cleanup_failed",
            title="The rejected operation's files could not be removed",
            detail=f"git could not restore {relative_dir} to the last commit",
            fix_suggestion=f"run `git checkout -- {relative_dir}` by hand before the next operation",
        )
        context.session.commit()
    return True
