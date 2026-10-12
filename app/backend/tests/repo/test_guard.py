import os
import subprocess
from pathlib import Path

from rdscommander.execution_records.service import ExecutionRecorder
from rdscommander.repo.guard import check_checkout

ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=ENV, text=True, capture_output=True, check=True
    ).stdout.strip()


def _repo_with_upstream(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    git("init", "-q", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    repo = tmp_path / "repo"
    git("clone", "-q", str(origin), str(repo), cwd=tmp_path)
    (repo / "terraform").mkdir()
    (repo / "terraform" / "README.md").write_text("tf\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("push", "-q", "-u", "origin", "main", cwd=repo)
    return repo, origin


def _check(session_factory, repo: Path, sha: str | None = None, allow: bool = False):
    return check_checkout(
        ExecutionRecorder(session_factory),
        repo,
        Path("terraform/x"),
        sha,
        allow,
        operation_id=None,
        step_name="check",
        target_key=None,
    )


def test_clean_synced_checkout_passes(session_factory, tmp_path: Path) -> None:
    repo, _ = _repo_with_upstream(tmp_path)
    report = _check(session_factory, repo)
    assert report.ok, report.problems


def test_dirty_terraform_directory_is_refused_with_fix(session_factory, tmp_path: Path) -> None:
    repo, _ = _repo_with_upstream(tmp_path)
    (repo / "terraform" / "x").mkdir()
    (repo / "terraform" / "x" / "stray.tf").write_text("x\n")
    report = _check(session_factory, repo)
    assert [p.code for p in report.problems] == ["dirty_terraform_directory"]
    assert "git status" in report.problems[0].fix_suggestion
    assert "terraform/x" in report.problems[0].fix_suggestion


def test_a_dirty_sibling_workspace_does_not_block_another_workspace(session_factory, tmp_path: Path) -> None:
    repo, _ = _repo_with_upstream(tmp_path)
    (repo / "terraform" / "y").mkdir()
    (repo / "terraform" / "y" / "waiting-for-approval.tf").write_text("x\n")
    (repo / "terraform" / "stray-outside-any-workspace.tf").write_text("x\n")
    assert _check(session_factory, repo).ok, "terraform/x is clean, whatever terraform/y holds"
    blocked = check_checkout(
        ExecutionRecorder(session_factory),
        repo,
        Path("terraform/y"),
        None,
        False,
        operation_id=None,
        step_name="check",
        target_key=None,
    )
    assert [p.code for p in blocked.problems] == ["dirty_terraform_directory"]


def test_checkout_behind_origin_is_refused(session_factory, tmp_path: Path) -> None:
    repo, origin = _repo_with_upstream(tmp_path)
    other = tmp_path / "other"
    git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    (other / "terraform" / "new.tf").write_text("y\n")
    git("add", "-A", cwd=other)
    git("commit", "-q", "-m", "remote change", cwd=other)
    git("push", "-q", "origin", "main", cwd=other)
    report = _check(session_factory, repo)
    assert [p.code for p in report.problems] == ["behind_upstream"]
    assert "git pull" in report.problems[0].fix_suggestion


def test_missing_last_applied_commit_is_refused_even_when_ahead(session_factory, tmp_path: Path) -> None:
    repo, origin = _repo_with_upstream(tmp_path)
    other = tmp_path / "other"
    git("clone", "-q", str(origin), str(other), cwd=tmp_path)
    (other / "terraform" / "applied.tf").write_text("z\n")
    git("add", "-A", cwd=other)
    git("commit", "-q", "-m", "applied elsewhere, not pushed", cwd=other)
    unpushed_sha = git("rev-parse", "HEAD", cwd=other)
    (repo / "notes.md").write_text("local\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "local ahead", cwd=repo)
    report = _check(session_factory, repo, sha=unpushed_sha)
    assert [p.code for p in report.problems] == ["missing_applied_commit"]
    assert unpushed_sha[:12] in report.problems[0].fix_suggestion


def test_no_upstream_is_refused_unless_allowed(session_factory, tmp_path: Path) -> None:
    repo = tmp_path / "solo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "a").write_text("a\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "a", cwd=repo)
    assert [p.code for p in _check(session_factory, repo).problems] == ["no_upstream"]
    assert _check(session_factory, repo, allow=True).ok


def test_files_staged_outside_the_workspace_are_refused_with_the_way_to_unstage_them(
    session_factory, tmp_path: Path
) -> None:
    repo, _ = _repo_with_upstream(tmp_path)
    (repo / "terraform" / "y").mkdir()
    (repo / "terraform" / "y" / "left behind.tf").write_text("x\n")
    git("add", "-A", "--", "terraform/y", cwd=repo)
    report = _check(session_factory, repo)
    assert [p.code for p in report.problems] == ["staged_changes_outside_workspace"]
    [problem] = report.problems
    assert "terraform/y/left behind.tf" in problem.detail
    assert problem.fix_suggestion.startswith("unstage them with `git restore --staged -- ")
    assert "'terraform/y/left behind.tf'" in problem.fix_suggestion


def test_files_staged_inside_the_workspace_are_the_dirty_check_s_business_only(
    session_factory, tmp_path: Path
) -> None:
    repo, _ = _repo_with_upstream(tmp_path)
    (repo / "terraform" / "x").mkdir()
    (repo / "terraform" / "x" / "staged.tf").write_text("x\n")
    git("add", "-A", "--", "terraform/x", cwd=repo)
    assert [p.code for p in _check(session_factory, repo).problems] == ["dirty_terraform_directory"]


def test_a_dirty_workspace_passes_only_when_the_operation_owns_every_file_listed_one_by_one(
    session_factory, tmp_path: Path
) -> None:
    """The owner check sees each uncommitted file, even inside a directory git has never tracked."""
    repo, _ = _repo_with_upstream(tmp_path)
    (repo / "terraform" / "x").mkdir()
    (repo / "terraform" / "x" / "parameter_groups.tf").write_text("rendered\n")
    (repo / "terraform" / "x" / "instances.tf").write_text("rendered\n")
    seen: list[list[str]] = []

    def owned(paths: list[str]) -> bool:
        seen.append(paths)
        return True

    report = check_checkout(
        ExecutionRecorder(session_factory),
        repo,
        Path("terraform/x"),
        None,
        False,
        operation_id=None,
        step_name="check",
        target_key=None,
        operation_owns_uncommitted_files=owned,
    )
    assert report.ok, report.problems
    assert seen == [["terraform/x/instances.tf", "terraform/x/parameter_groups.tf"]]
    refused = check_checkout(
        ExecutionRecorder(session_factory),
        repo,
        Path("terraform/x"),
        None,
        False,
        operation_id=None,
        step_name="check",
        target_key=None,
        operation_owns_uncommitted_files=lambda paths: False,
    )
    assert [p.code for p in refused.problems] == ["dirty_terraform_directory"]
