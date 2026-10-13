"""Recognise a workspace whose uncommitted files are exactly the ones an operation wrote.

A Terraform-mode operation renders its files, then waits for approval with the workspace dirty on purpose.
The checkout guard refuses a dirty workspace, and the cleanup of a rejected operation only touches files in
the checkout of the console that wrote them, which both used to recognise by the runner identity alone. A
re-created container can come back under another identity; the files are still the operation's own.

So the step that writes the files records `workspace_files_sha256`, a fingerprint of every file the console
manages in the workspace, in its output. A dirty workspace belongs to the operation when every uncommitted
file is one the console manages (or the provider lock file `init` writes) and the managed files still have
the fingerprint the operation recorded. A hand edit since then, or any other file, is not the operation's.

Example:
    >>> fingerprint = workspace_files_fingerprint(workspace_dir)  # doctest: +SKIP
    >>> uncommitted_files_are_the_operations_own(  # doctest: +SKIP
    ...     workspace_dir, "terraform/sandbox/000000000000/eu-west-2",
    ...     ["terraform/sandbox/000000000000/eu-west-2/parameter_groups.tf"], {fingerprint})
    True
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection, Iterable
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from rdscommander.control_db.models import OperationStepAttempt, TerraformWorkspace
from rdscommander.operations.enums import StepOutcomeStatus
from rdscommander.operations.executor import StepContext
from rdscommander.terraform.record import RECORD_FILE_NAME
from rdscommander.terraform.renderer import MANAGED_FILE_NAMES

WORKSPACE_FILES_FINGERPRINT_KEY = "workspace_files_sha256"
"""The step-output key under which a writing step records the fingerprint of what it wrote."""
FINGERPRINTED_FILE_NAMES: tuple[str, ...] = (*MANAGED_FILE_NAMES, RECORD_FILE_NAME)
"""The files the console writes into a workspace; their content (or absence) is the fingerprint."""
FILES_WRITTEN_BY_INIT: tuple[str, ...] = (".terraform.lock.hcl",)
"""Uncommitted files `init` leaves that an operation may own besides the fingerprinted ones."""


def workspace_files_fingerprint(workspace_dir: Path) -> str:
    """The SHA-256 of every console-managed file in `workspace_dir`: its name and content, or its absence.

    Args:
        workspace_dir: The target's workspace directory.

    Returns:
        A hex digest that changes when any managed file is written, edited or removed.

    Example:
        >>> len(workspace_files_fingerprint(workspace_dir))  # doctest: +SKIP
        64
    """
    digest = hashlib.sha256()
    for name in sorted(FINGERPRINTED_FILE_NAMES):
        path = workspace_dir / name
        content_digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "absent"
        digest.update(f"{name}\0{content_digest}\n".encode())
    return digest.hexdigest()


def recorded_workspace_fingerprints(context: StepContext) -> set[str]:
    """Every fingerprint the operation's completed steps recorded for the files they wrote.

    Example:
        >>> recorded_workspace_fingerprints(context)  # doctest: +SKIP
        {'5f1c...'}
    """
    outputs: Iterable[dict | None] = context.session.execute(
        select(OperationStepAttempt.output).where(
            OperationStepAttempt.operation_id == context.operation.id,
            OperationStepAttempt.outcome_status == StepOutcomeStatus.COMPLETED.value,
        )
    ).scalars()
    return {
        str(output[WORKSPACE_FILES_FINGERPRINT_KEY])
        for output in outputs
        if isinstance(output, dict) and output.get(WORKSPACE_FILES_FINGERPRINT_KEY)
    }


def uncommitted_files_are_the_operations_own(
    workspace_dir: Path,
    workspace_relative_dir: str,
    uncommitted_paths: list[str],
    operation_fingerprints: Collection[str],
) -> bool:
    """Whether the workspace's uncommitted files are exactly what the operation wrote.

    Args:
        workspace_dir: The workspace directory on this console's checkout.
        workspace_relative_dir: The same directory relative to the repository root, as git lists paths.
        uncommitted_paths: Every uncommitted path in the workspace, one per file, relative to the root.
        operation_fingerprints: The fingerprints the operation recorded (`recorded_workspace_fingerprints`).

    Returns:
        True when the operation recorded a fingerprint, every uncommitted file is a console-managed file
        (or the lock file `init` writes), and the managed files still have a recorded fingerprint.

    Example:
        >>> uncommitted_files_are_the_operations_own(  # doctest: +SKIP
        ...     workspace_dir, "terraform/x", ["terraform/x/notes.txt"], {fingerprint})
        False
    """
    if not operation_fingerprints:
        return False
    workspace = PurePosixPath(workspace_relative_dir)
    owned_names = (*FINGERPRINTED_FILE_NAMES, *FILES_WRITTEN_BY_INIT)
    owned_paths = {(workspace / name).as_posix() for name in owned_names}
    if any(path not in owned_paths for path in uncommitted_paths):
        return False
    return workspace_files_fingerprint(workspace_dir) in operation_fingerprints


def operation_owner_check(context: StepContext, workspace: TerraformWorkspace) -> Callable[[list[str]], bool]:
    """The check `check_checkout` asks whether the workspace's uncommitted files are this operation's own.

    Args:
        context: The running step's context; its operation's recorded fingerprints are read once here.
        workspace: The operation's workspace row.

    Returns:
        A function of the uncommitted paths (relative to the repository root) that answers
        `uncommitted_files_are_the_operations_own` for this console's checkout.

    Example:
        >>> owns = operation_owner_check(context, workspace)  # doctest: +SKIP
        >>> check_checkout(..., operation_owns_uncommitted_files=owns)  # doctest: +SKIP
    """
    fingerprints = recorded_workspace_fingerprints(context)
    workspace_dir = context.settings.repo_root / workspace.workspace_relative_dir
    relative_dir = Path(workspace.workspace_relative_dir).as_posix()

    def owns(uncommitted_paths: list[str]) -> bool:
        return uncommitted_files_are_the_operations_own(
            workspace_dir, relative_dir, uncommitted_paths, fingerprints
        )

    return owns
