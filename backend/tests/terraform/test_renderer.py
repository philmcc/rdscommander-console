import re
import shutil
import subprocess
from pathlib import Path

import pytest

from rdscommander.terraform.render_input import build_render_input
from rdscommander.terraform.renderer import MANAGED_FILE_NAMES, render_workspace_files, write_workspace_files
from tests.terraform.conftest import TARGET_KEY, all_snapshot_arns

GOLDEN = Path(__file__).parent / "golden" / "sandbox"


def _render(session_factory, console_config, snapshot, include_imports: bool) -> dict[str, str]:
    target = console_config.target(TARGET_KEY)
    with session_factory() as session:
        render_input = build_render_input(
            session,
            target,
            console_config,
            snapshot,
            all_snapshot_arns(session_factory, snapshot),
            set(),
            include_imports=include_imports,
            simulated_endpoint_url="http://simulated-aws:5000",
        )
    return render_workspace_files(render_input)


def test_renderer_matches_golden_files(session_factory, console_config, sandbox_snapshot) -> None:
    files = _render(session_factory, console_config, sandbox_snapshot, include_imports=True)
    assert set(files) == set(MANAGED_FILE_NAMES)
    missing = []
    for name, content in files.items():
        expected = GOLDEN / name
        if not expected.exists():
            expected.parent.mkdir(parents=True, exist_ok=True)
            expected.write_text(content)
            missing.append(name)
    if missing:
        pytest.fail(f"golden files {missing} were missing and have been written; review them and rerun")
    for name, content in files.items():
        assert content == (GOLDEN / name).read_text(), f"{name} differs from golden"


def test_renderer_rules(session_factory, console_config, sandbox_snapshot) -> None:
    files = _render(session_factory, console_config, sandbox_snapshot, include_imports=False)
    assert "password" not in files["instances.tf"] and "master_password" not in files["clusters.tf"]
    assert "ignore_changes = [engine_version]" in files["instances.tf"]
    assert "skip_final_snapshot" in files["instances.tf"] and "= false" in files["instances.tf"]
    assert "replicate_source_db" in files["instances.tf"]
    assert "aws_db_instance.demo_orders.identifier" in files["instances.tf"]
    assert "aws_db_parameter_group.demo_postgres16_tuned.name" in files["instances.tf"]
    assert "aws_rds_cluster.demo_aurora.id" in files["clusters.tf"]
    assert "imports.tf" not in files
    assert re.search(r"use_lockfile\s+= true", files["backend.tf"])
    assert "rdscommander:managed" in files["providers.tf"]


def test_write_workspace_files_removes_stale_managed_files(tmp_path: Path) -> None:
    (tmp_path / "imports.tf").write_text("stale\n")
    (tmp_path / "custom.tf").write_text("keep\n")
    written = write_workspace_files(tmp_path, {"versions.tf": "terraform {}\n"})
    assert [p.name for p in written] == ["versions.tf"]
    assert not (tmp_path / "imports.tf").exists() and (tmp_path / "custom.tf").exists()


@pytest.mark.skipif(shutil.which("tofu") is None, reason="OpenTofu binary not installed")
def test_golden_files_are_valid_hcl(tmp_path: Path) -> None:
    for path in GOLDEN.glob("*.tf"):
        shutil.copy(path, tmp_path / path.name)
    (tmp_path / "backend.tf").unlink()
    subprocess.run(["tofu", "fmt", "-check", "-diff"], cwd=tmp_path, check=True)
    subprocess.run(
        ["tofu", "init", "-backend=false", "-input=false"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(["tofu", "validate"], cwd=tmp_path, check=True)


def _renders_attribute(text: str, name: str, value: str) -> bool:
    """True when `name = value` appears as an attribute line, whatever alignment `tofu fmt` gave it."""
    return re.search(rf"^\s*{re.escape(name)}\s+=\s+{re.escape(value)}$", text, re.MULTILINE) is not None


def test_instances_render_apply_immediately_and_the_final_snapshot_name(sandbox_render_input):
    files = render_workspace_files(sandbox_render_input)
    assert _renders_attribute(files["instances.tf"], "apply_immediately", "true")
    assert _renders_attribute(files["instances.tf"], "final_snapshot_identifier", '"demo-orders-final"')
    assert _renders_attribute(files["clusters.tf"], "apply_immediately", "true")


def test_a_restored_instance_renders_its_source_and_ignores_it_afterwards(sandbox_render_input):
    updated = sandbox_render_input.model_copy(deep=True)
    instance = next(i for i in updated.instances if i.identifier == "demo-orders")
    instance.snapshot_identifier = "demo-orders-before-upgrade"
    instance.final_snapshot_identifier = "demo-orders-final-2026-10-11"
    files = render_workspace_files(updated)
    assert _renders_attribute(files["instances.tf"], "snapshot_identifier", '"demo-orders-before-upgrade"')
    assert _renders_attribute(
        files["instances.tf"], "final_snapshot_identifier", '"demo-orders-final-2026-10-11"'
    )
    assert "ignore_changes = [engine_version, snapshot_identifier]" in files["instances.tf"]


def test_a_created_instance_renders_a_managed_master_password_and_never_a_password(sandbox_render_input):
    updated = sandbox_render_input.model_copy(deep=True)
    updated.instances[0].manage_master_user_password = True
    files = render_workspace_files(updated)
    assert _renders_attribute(files["instances.tf"], "manage_master_user_password", "true")
    assert "password" not in files["instances.tf"].replace("manage_master_user_password", "")


def test_an_instance_renders_a_zero_autoscaling_ceiling_but_omits_an_unset_one(sandbox_render_input):
    updated = sandbox_render_input.model_copy(deep=True)
    updated.instances[0].max_allocated_storage = 0
    assert _renders_attribute(render_workspace_files(updated)["instances.tf"], "max_allocated_storage", "0")
    updated.instances[0].max_allocated_storage = None
    assert "max_allocated_storage" not in render_workspace_files(updated)["instances.tf"]
