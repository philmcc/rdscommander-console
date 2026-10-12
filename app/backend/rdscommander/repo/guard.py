"""Refuse to plan or apply from a checkout that is dirty, behind, or missing the last applied commit."""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field

from rdscommander.execution_records.service import ExecutionRecorder, SubprocessResult
from rdscommander.logging_setup import get_logger
from rdscommander.repo.git_runner import run_git_recorded

logger = get_logger(__name__)


class CheckoutProblem(BaseModel):
    """One reason the checkout is unusable."""

    code: str
    detail: str
    fix_suggestion: str


class CheckoutGuardReport(BaseModel):
    """Result of the guard."""

    problems: list[CheckoutProblem] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """No problems."""
        return not self.problems

    def summary(self) -> str:
        """One line per problem."""
        return "\n".join(f"{p.code}: {p.detail}" for p in self.problems)


def _staged_paths_outside(staged: SubprocessResult, workspace_relative_dir: Path) -> list[str]:
    """The staged paths (from `git diff --cached --name-only`) that are not under the workspace."""
    if not staged.succeeded:
        return []
    return [
        path
        for path in staged.standard_output.splitlines()
        if path and not PurePosixPath(path).is_relative_to(PurePosixPath(workspace_relative_dir.as_posix()))
    ]


def uncommitted_paths_from_porcelain(porcelain_output: str) -> list[str]:
    """The paths listed by `git status --porcelain` (the new path of a rename), sorted.

    Example:
        >>> uncommitted_paths_from_porcelain(" M terraform/x/b.tf" + chr(10) + "?? terraform/x/a.tf")
        ['terraform/x/a.tf', 'terraform/x/b.tf']
    """
    paths: list[str] = []
    for line in porcelain_output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return sorted(paths)


def _uncommitted_files_belong_to_the_operation(
    run: Callable[..., SubprocessResult],
    workspace_relative_dir: Path,
    operation_owns_uncommitted_files: Callable[[list[str]], bool] | None,
    operation_id: uuid.UUID | None,
) -> bool:
    """Whether the operation says the workspace's uncommitted files, listed one per file, are its own."""
    if operation_owns_uncommitted_files is None:
        return False
    listing = run(
        "-c",
        "core.quotePath=false",
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        workspace_relative_dir.as_posix(),
    )
    if not listing.succeeded:
        return False
    paths = uncommitted_paths_from_porcelain(listing.standard_output)
    if not operation_owns_uncommitted_files(paths):
        return False
    logger.info(
        "checkout_guard_accepted_the_operations_own_files",
        operation_id=str(operation_id) if operation_id else None,
        workspace_relative_dir=workspace_relative_dir.as_posix(),
        uncommitted_paths=paths,
    )
    return True


def check_checkout(
    recorder: ExecutionRecorder,
    repo_root: Path,
    workspace_relative_dir: Path,
    last_applied_commit_sha: str | None,
    allow_without_upstream: bool,
    *,
    operation_id: uuid.UUID | None,
    step_name: str | None,
    target_key: str | None,
    operation_owns_uncommitted_files: Callable[[list[str]], bool] | None = None,
) -> CheckoutGuardReport:
    """Run the checks: clean workspace, nothing staged elsewhere, not behind upstream, last commit present.

    Only the operation's own workspace directory has to be clean, so that a change waiting for approval in
    one environment does not block the others. Files staged outside the workspace are refused all the same:
    they would not be part of this operation's commit, and someone put them in the index on purpose or a
    failed commit left them there.

    Args:
        recorder: Records every git invocation.
        repo_root: The checkout.
        workspace_relative_dir: The operation's workspace directory, relative to `repo_root`.
        last_applied_commit_sha: The commit that must be in the checkout, if any.
        allow_without_upstream: Accept a branch that tracks no remote branch.
        operation_id: Operation the git invocations are recorded under.
        step_name: Step the git invocations are recorded under.
        target_key: Target the git invocations are recorded under.
        operation_owns_uncommitted_files: For an operation that already wrote its files (and is resumed,
            for instance after approval by a re-created console): given every uncommitted path in the
            workspace, one per file and relative to `repo_root`, it says whether they are exactly what the
            operation wrote. When it says so, the dirty workspace is the operation's own and not refused.

    Example:
        >>> check_checkout(recorder, repo_root, Path("terraform/x"), None, True,  # doctest: +SKIP
        ...                operation_id=None, step_name="check_checkout", target_key=None).ok
        True
    """
    report = CheckoutGuardReport()

    def run(*arguments: str) -> SubprocessResult:
        return run_git_recorded(
            recorder,
            repo_root,
            list(arguments),
            operation_id=operation_id,
            step_name=step_name,
            target_key=target_key,
        )

    status = run("status", "--porcelain", "--", workspace_relative_dir.as_posix())
    if not status.succeeded:
        report.problems.append(
            CheckoutProblem(
                code="not_a_repository",
                detail=status.standard_error.strip(),
                fix_suggestion=f"{repo_root} must be a git checkout of this repository",
            )
        )
        return report
    if status.standard_output.strip() and not _uncommitted_files_belong_to_the_operation(
        run, workspace_relative_dir, operation_owns_uncommitted_files, operation_id
    ):
        report.problems.append(
            CheckoutProblem(
                code="dirty_terraform_directory",
                detail=status.standard_output.strip(),
                fix_suggestion=(
                    f"commit or discard the listed changes under {workspace_relative_dir.as_posix()}/ "
                    f"(`git status -- {workspace_relative_dir.as_posix()}`) before planning"
                ),
            )
        )

    staged_elsewhere = _staged_paths_outside(
        run("-c", "core.quotePath=false", "diff", "--cached", "--name-only"), workspace_relative_dir
    )
    if staged_elsewhere:
        report.problems.append(
            CheckoutProblem(
                code="staged_changes_outside_workspace",
                detail="staged for the next commit but outside "
                f"{workspace_relative_dir.as_posix()}/: {', '.join(staged_elsewhere)}",
                fix_suggestion=(
                    "unstage them with "
                    f"`git restore --staged -- {' '.join(shlex.quote(path) for path in staged_elsewhere)}` "
                    "or commit them yourself, then try again"
                ),
            )
        )

    upstream = run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if not upstream.succeeded:
        if not allow_without_upstream:
            report.problems.append(
                CheckoutProblem(
                    code="no_upstream",
                    detail="the current branch tracks no remote branch",
                    fix_suggestion=(
                        "run `git branch --set-upstream-to=origin/<branch>` or push the branch first"
                    ),
                )
            )
    else:
        fetch = run("fetch", "--quiet")
        if not fetch.succeeded:
            report.problems.append(
                CheckoutProblem(
                    code="fetch_failed",
                    detail=fetch.standard_error.strip(),
                    fix_suggestion="check network access and credentials for the git remote",
                )
            )
        else:
            behind = run("rev-list", "--count", "HEAD..@{upstream}")
            if behind.succeeded and behind.standard_output.strip() not in ("", "0"):
                report.problems.append(
                    CheckoutProblem(
                        code="behind_upstream",
                        detail=(
                            f"{behind.standard_output.strip()} commits behind "
                            f"{upstream.standard_output.strip()}"
                        ),
                        fix_suggestion="run `git pull` so the Terraform files match what was last applied",
                    )
                )

    if last_applied_commit_sha:
        ancestor = run("merge-base", "--is-ancestor", last_applied_commit_sha, "HEAD")
        if not ancestor.succeeded:
            report.problems.append(
                CheckoutProblem(
                    code="missing_applied_commit",
                    detail=(
                        f"commit {last_applied_commit_sha} (last applied for {workspace_relative_dir}) "
                        "is not in this checkout"
                    ),
                    fix_suggestion=(
                        f"ask the operator who last applied to push commit {last_applied_commit_sha[:12]}, "
                        "then `git pull`"
                    ),
                )
            )
    return report
