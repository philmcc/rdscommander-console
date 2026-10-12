from pathlib import Path

from rdscommander.execution_records.service import ExecutionRecorder
from rdscommander.repo.commit import commit_paths, current_head_sha, operation_commit_message
from tests.repo.test_guard import git


def test_commit_paths_commits_under_the_given_identity(session_factory, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "seed").write_text("s\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "seed", cwd=repo)
    (repo / "terraform" / "prod").mkdir(parents=True)
    (repo / "terraform" / "prod" / "main.tf").write_text("resource {}\n")
    recorder = ExecutionRecorder(session_factory)
    common = {"operation_id": None, "step_name": "commit", "target_key": None}
    sha = commit_paths(
        recorder,
        repo,
        [Path("terraform/prod")],
        "Adopt production resources",
        "Phil",
        "phil@example.invalid",
        **common,
    )
    assert sha == current_head_sha(recorder, repo, **common)
    assert (
        git("log", "-1", "--format=%an <%ae> %s", cwd=repo)
        == "Phil <phil@example.invalid> Adopt production resources"
    )
    again = commit_paths(
        recorder, repo, [Path("terraform/prod")], "nothing", "Phil", "phil@example.invalid", **common
    )
    assert again == sha


def test_commit_paths_can_record_an_empty_commit_when_asked(session_factory, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "terraform").mkdir()
    (repo / "terraform" / "main.tf").write_text("resource {}\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "seed", cwd=repo)
    recorder = ExecutionRecorder(session_factory)
    common = {"operation_id": None, "step_name": "commit", "target_key": None}
    seed_sha = current_head_sha(recorder, repo, **common)
    unchanged = commit_paths(
        recorder, repo, [Path("terraform")], "nothing", "Phil", "phil@example.invalid", **common
    )
    assert unchanged == seed_sha
    recorded = commit_paths(
        recorder,
        repo,
        [Path("terraform")],
        "Resolve drift: demo (phil, approved by sam)",
        "Phil",
        "phil@example.invalid",
        allow_empty=True,
        **common,
    )
    assert recorded != seed_sha
    assert git("log", "-1", "--format=%s", cwd=repo) == "Resolve drift: demo (phil, approved by sam)"
    assert git("rev-list", "--count", "HEAD", cwd=repo) == "2"


def _repo_with_two_workspaces(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    for workspace in ("a", "b"):
        (repo / "terraform" / workspace).mkdir(parents=True)
        (repo / "terraform" / workspace / "parameter_groups.tf").write_text("committed\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def test_a_commit_never_includes_another_workspaces_staged_files(session_factory, tmp_path: Path) -> None:
    repo = _repo_with_two_workspaces(tmp_path)
    # Another environment's change failed after `git add` and left its file staged.
    (repo / "terraform" / "b" / "parameter_groups.tf").write_text("left staged by a failed commit\n")
    (repo / "terraform" / "b" / "new.tf").write_text("new and staged\n")
    git("add", "-A", "--", "terraform/b", cwd=repo)
    (repo / "terraform" / "a" / "parameter_groups.tf").write_text("change a\n")
    recorder = ExecutionRecorder(session_factory)
    common = {"operation_id": None, "step_name": "commit", "target_key": None}

    sha = commit_paths(
        recorder,
        repo,
        [Path("terraform/a")],
        "change a (phil, approved by sam)",
        "Phil",
        "phil@example.invalid",
        **common,
    )

    assert sha == current_head_sha(recorder, repo, **common)
    assert git("show", "--name-only", "--format=", "HEAD", cwd=repo).splitlines() == [
        "terraform/a/parameter_groups.tf"
    ]
    assert sorted(git("diff", "--cached", "--name-only", cwd=repo).splitlines()) == [
        "terraform/b/new.tf",
        "terraform/b/parameter_groups.tf",
    ], "the other workspace's files are still staged, untouched"


def test_another_workspaces_staged_files_do_not_turn_a_no_change_into_a_commit(
    session_factory, tmp_path: Path
) -> None:
    repo = _repo_with_two_workspaces(tmp_path)
    (repo / "terraform" / "b" / "parameter_groups.tf").write_text("left staged\n")
    git("add", "-A", "--", "terraform/b", cwd=repo)
    recorder = ExecutionRecorder(session_factory)
    common = {"operation_id": None, "step_name": "commit", "target_key": None}
    seed_sha = current_head_sha(recorder, repo, **common)

    unchanged = commit_paths(
        recorder, repo, [Path("terraform/a")], "nothing for a", "Phil", "phil@example.invalid", **common
    )

    assert unchanged == seed_sha
    assert git("diff", "--cached", "--name-only", cwd=repo) == "terraform/b/parameter_groups.tf"


def test_an_empty_commit_for_one_workspace_leaves_another_workspaces_staged_files_out(
    session_factory, tmp_path: Path
) -> None:
    repo = _repo_with_two_workspaces(tmp_path)
    (repo / "terraform" / "b" / "parameter_groups.tf").write_text("left staged\n")
    git("add", "-A", "--", "terraform/b", cwd=repo)
    recorder = ExecutionRecorder(session_factory)
    common = {"operation_id": None, "step_name": "commit", "target_key": None}

    commit_paths(
        recorder,
        repo,
        [Path("terraform/a")],
        "applied without a file change",
        "Phil",
        "phil@example.invalid",
        allow_empty=True,
        **common,
    )

    assert git("show", "--name-only", "--format=%s", "HEAD", cwd=repo) == "applied without a file change"
    assert git("diff", "--cached", "--name-only", cwd=repo) == "terraform/b/parameter_groups.tf"


def test_a_commit_of_a_workspace_records_new_changed_and_deleted_files(
    session_factory, tmp_path: Path
) -> None:
    repo = _repo_with_two_workspaces(tmp_path)
    (repo / "terraform" / "a" / "parameter_groups.tf").unlink()
    (repo / "terraform" / "a" / "instances.tf").write_text("new\n")
    recorder = ExecutionRecorder(session_factory)
    common = {"operation_id": None, "step_name": "commit", "target_key": None}
    commit_paths(recorder, repo, [Path("terraform/a")], "rewrite a", "Phil", "phil@example.invalid", **common)
    assert sorted(git("show", "--name-status", "--format=", "HEAD", cwd=repo).splitlines()) == [
        "A\tterraform/a/instances.tf",
        "D\tterraform/a/parameter_groups.tf",
    ]
    assert git("status", "--porcelain", cwd=repo) == ""


def test_operation_commit_message_names_the_approver_only_when_an_approval_was_recorded() -> None:
    assert (
        operation_commit_message("Edit parameters: demo-postgres16-tuned", "phil", "sam")
        == "Edit parameters: demo-postgres16-tuned (phil, approved by sam)"
    )
    assert (
        operation_commit_message("Resolve drift: demo-postgres16-tuned", "phil", None)
        == "Resolve drift: demo-postgres16-tuned (phil)"
    ), "a commit never claims an approval that did not happen"
