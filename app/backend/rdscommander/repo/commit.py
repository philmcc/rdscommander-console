"""Commit console-written files under the operator's identity."""

from __future__ import annotations

import uuid
from pathlib import Path

from rdscommander.execution_records.service import ExecutionRecorder
from rdscommander.repo.git_runner import run_git_recorded


class GitCommitError(Exception):
    """A git command needed for committing failed."""


def operation_commit_message(title: str, operator_name: str, approver_name: str | None) -> str:
    """The subject of the commit an operation makes: what it did, who did it and who approved it.

    A commit never names an approval that did not happen: without a recorded approver (accepting drift,
    for instance, has no approval pause) only the operator is named.

    Args:
        title: What the operation did, e.g. `Edit parameters: demo-postgres16-tuned`.
        operator_name: Who created the operation.
        approver_name: Who approved it, or None when nobody did.

    Returns:
        `<title> (<operator>, approved by <approver>)`, or `<title> (<operator>)` without an approver.

    Example:
        >>> operation_commit_message("Resolve drift: demo-postgres16-tuned", "phil", None)
        'Resolve drift: demo-postgres16-tuned (phil)'
    """
    if approver_name:
        return f"{title} ({operator_name}, approved by {approver_name})"
    return f"{title} ({operator_name})"


def current_head_sha(
    recorder: ExecutionRecorder,
    repo_root: Path,
    *,
    operation_id: uuid.UUID | None,
    step_name: str | None,
    target_key: str | None,
) -> str:
    """`git rev-parse HEAD`."""
    result = run_git_recorded(
        recorder,
        repo_root,
        ["rev-parse", "HEAD"],
        operation_id=operation_id,
        step_name=step_name,
        target_key=target_key,
    )
    if not result.succeeded:
        raise GitCommitError(result.standard_error.strip())
    return result.standard_output.strip()


def commit_paths(
    recorder: ExecutionRecorder,
    repo_root: Path,
    relative_paths: list[Path],
    message: str,
    author_name: str,
    author_email: str,
    *,
    operation_id: uuid.UUID | None,
    step_name: str | None,
    target_key: str | None,
    allow_empty: bool = False,
) -> str:
    """Stage `relative_paths`, commit only them if anything changed, and return HEAD.

    The commit names its paths (`git commit -- <paths>`), so files that are staged elsewhere in the
    repository, for instance by another environment's change that failed after staging, are never swept
    into this commit and stay staged.

    Args:
        recorder: Records every git invocation.
        repo_root: The repository root.
        relative_paths: Paths, relative to `repo_root`, to stage.
        message: The commit message.
        author_name: Commit author and committer name.
        author_email: Commit author and committer email.
        operation_id: Operation the git invocations belong to.
        step_name: Step the git invocations belong to.
        target_key: Target the git invocations belong to.
        allow_empty: Commit even when nothing is staged, so that an operation that changed AWS without
            changing a file still leaves its commit in the history.

    Returns:
        The SHA of HEAD after the call.

    Raises:
        GitCommitError: A git command failed (including an `allow_empty` commit when git knows none of
            the paths).
    """
    common = {"operation_id": operation_id, "step_name": step_name, "target_key": target_key}
    add = run_git_recorded(
        recorder, repo_root, ["add", "-A", "--", *[p.as_posix() for p in relative_paths]], **common
    )
    if not add.succeeded:
        raise GitCommitError(add.standard_error.strip())
    paths = [p.as_posix() for p in relative_paths]
    staged = run_git_recorded(recorder, repo_root, ["diff", "--cached", "--quiet", "--", *paths], **common)
    if staged.exit_code == 0 and not allow_empty:
        return current_head_sha(recorder, repo_root, **common)
    commit_arguments = [
        "commit",
        "-q",
        *(["--allow-empty"] if allow_empty else []),
        "-m",
        message,
        "--",
        *paths,
    ]
    commit = run_git_recorded(
        recorder, repo_root, commit_arguments, author=(author_name, author_email), **common
    )
    if not commit.succeeded:
        raise GitCommitError(commit.standard_error.strip())
    return current_head_sha(recorder, repo_root, **common)
