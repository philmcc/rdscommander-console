from pathlib import Path
from types import SimpleNamespace

from rdscommander.control_db.models import TerraformWorkspace
from rdscommander.execution_records.service import ExecutionRecorder
from rdscommander.terraform.workspace_revert import revert_workspace
from tests.repo.test_guard import git


def _context_and_workspace(session_factory, repo: Path, relative_dir: str):
    context = SimpleNamespace(
        operation=SimpleNamespace(id=None, target_key=None),
        settings=SimpleNamespace(repo_root=repo),
        services=SimpleNamespace(execution_recorder=ExecutionRecorder(session_factory)),
    )
    return context, TerraformWorkspace(workspace_relative_dir=relative_dir)


def test_revert_restores_tracked_files_and_removes_new_managed_files_in_that_workspace_only(
    session_factory, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    for workspace in ("a", "b"):
        (repo / "terraform" / workspace).mkdir(parents=True)
        (repo / "terraform" / workspace / "parameter_groups.tf").write_text("committed\n")
    (repo / "terraform" / "a" / "instances.tf").write_text("committed\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    # Workspace a: an edit, a deletion, a new managed file, a new record and a file the console ignores.
    (repo / "terraform" / "a" / "parameter_groups.tf").write_text("rendered but never approved\n")
    (repo / "terraform" / "a" / "instances.tf").unlink()
    (repo / "terraform" / "a" / "subnet_groups.tf").write_text("new\n")
    (repo / "terraform" / "a" / "console-record.json").write_text("{}\n")
    (repo / "terraform" / "a" / "notes.txt").write_text("somebody's notes\n")
    # Workspace b belongs to someone else's work in progress.
    (repo / "terraform" / "b" / "parameter_groups.tf").write_text("work in progress\n")
    context, workspace = _context_and_workspace(session_factory, repo, "terraform/a")

    assert revert_workspace(context, workspace, "policy_check") is True

    assert (repo / "terraform" / "a" / "parameter_groups.tf").read_text() == "committed\n"
    assert (repo / "terraform" / "a" / "instances.tf").read_text() == "committed\n"
    assert not (repo / "terraform" / "a" / "subnet_groups.tf").exists()
    assert not (repo / "terraform" / "a" / "console-record.json").exists()
    assert (repo / "terraform" / "a" / "notes.txt").exists(), "files the console does not manage are left"
    assert (repo / "terraform" / "b" / "parameter_groups.tf").read_text() == "work in progress\n"
    assert git("status", "--porcelain", "--", "terraform/a", cwd=repo).splitlines() == [
        "?? terraform/a/notes.txt"
    ]


def test_revert_of_a_workspace_that_was_never_committed_removes_what_the_console_wrote(
    session_factory, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("repo\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    (repo / "terraform" / "a").mkdir(parents=True)
    (repo / "terraform" / "a" / "instances.tf").write_text("new\n")
    (repo / "terraform" / "a" / "console-record.json").write_text("{}\n")
    context, workspace = _context_and_workspace(session_factory, repo, "terraform/a")

    assert revert_workspace(context, workspace) is True

    assert git("status", "--porcelain", "--", "terraform", cwd=repo) == ""


def test_revert_of_a_never_committed_workspace_removes_the_provider_lock_file_init_wrote(
    session_factory, tmp_path: Path
) -> None:
    # Rejecting the first Terraform-mode operation of a target left `.terraform.lock.hcl` behind, and the
    # checkout guard then refused every later operation with `dirty_terraform_directory`.
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "README.md").write_text("repo\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    (repo / "terraform" / "a").mkdir(parents=True)
    (repo / "terraform" / "a" / "instances.tf").write_text("new\n")
    (repo / "terraform" / "a" / ".terraform.lock.hcl").write_text("# written by init\n")
    context, workspace = _context_and_workspace(session_factory, repo, "terraform/a")

    assert revert_workspace(context, workspace, "on_cancelled") is True

    assert git("status", "--porcelain", "--untracked-files=all", "--", "terraform", cwd=repo) == ""


def test_revert_keeps_a_committed_provider_lock_file(session_factory, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    (repo / "terraform" / "a").mkdir(parents=True)
    (repo / "terraform" / "a" / ".terraform.lock.hcl").write_text("# committed\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    (repo / "terraform" / "a" / ".terraform.lock.hcl").write_text("# upgraded by init\n")
    context, workspace = _context_and_workspace(session_factory, repo, "terraform/a")

    assert revert_workspace(context, workspace) is True

    assert (repo / "terraform" / "a" / ".terraform.lock.hcl").read_text() == "# committed\n"
