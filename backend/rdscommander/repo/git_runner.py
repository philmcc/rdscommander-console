"""Run git with full recording."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from rdscommander.execution_records.service import ExecutionRecorder, SubprocessResult


def run_git_recorded(
    recorder: ExecutionRecorder,
    repo_root: Path,
    arguments: list[str],
    *,
    operation_id: uuid.UUID | None,
    step_name: str | None,
    target_key: str | None,
    author: tuple[str, str] | None = None,
) -> SubprocessResult:
    """Run `git <arguments>` in `repo_root` and record it; never raises on non-zero exit."""
    environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if author is not None:
        name, email = author
        environment.update(
            GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email
        )
    return recorder.record_subprocess(
        record_kind="git",
        command=["git", *arguments],
        working_directory=repo_root,
        environment=environment,
        operation_id=operation_id,
        step_name=step_name,
        target_key=target_key,
        timeout_seconds=300,
    )
