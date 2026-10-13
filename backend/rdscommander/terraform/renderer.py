"""Render workspace files from a `RenderWorkspaceInput`; output is normalised with the binary's `fmt`."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from rdscommander.terraform.hcl import to_hcl
from rdscommander.terraform.record import write_record
from rdscommander.terraform.render_input import RenderWorkspaceInput

MANAGED_FILE_NAMES = (
    "versions.tf",
    "backend.tf",
    "providers.tf",
    "parameter_groups.tf",
    "cluster_parameter_groups.tf",
    "subnet_groups.tf",
    "clusters.tf",
    "instances.tf",
    "imports.tf",
)
RESOURCE_FILE_NAMES = (
    "parameter_groups.tf",
    "cluster_parameter_groups.tf",
    "subnet_groups.tf",
    "clusters.tf",
    "instances.tf",
)
IMPORTS_FILE_NAME = "imports.tf"

_environment = Environment(
    loader=PackageLoader("rdscommander.terraform", "templates"),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    autoescape=False,  # noqa: S701 - HCL, not HTML
)
_environment.filters["hcl"] = to_hcl


def render_workspace_files(
    render_input: RenderWorkspaceInput, format_binary: str | None = "tofu"
) -> dict[str, str]:
    """Return `{file name: content}`; imports only with import blocks; empty resource files omitted."""
    files: dict[str, str] = {}
    context = render_input.model_dump()
    for name in MANAGED_FILE_NAMES:
        if name == IMPORTS_FILE_NAME and not render_input.import_blocks:
            continue
        content = _environment.get_template(f"{name}.j2").render(**context)
        if name in RESOURCE_FILE_NAMES and not content.strip():
            continue
        files[name] = content
    if format_binary and shutil.which(format_binary):
        files = _format(files, format_binary)
    return files


def _format(files: dict[str, str], binary: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as scratch:
        for name, content in files.items():
            (Path(scratch) / name).write_text(content, encoding="utf-8")
        subprocess.run([binary, "fmt"], cwd=scratch, check=True, capture_output=True)
        return {name: (Path(scratch) / name).read_text(encoding="utf-8") for name in files}


def write_workspace_files(
    workspace_dir: Path,
    files: dict[str, str],
    record: RenderWorkspaceInput | None = None,
    snapshot_id: int | None = None,
) -> list[Path]:
    """Write `files` into `workspace_dir`, removing stale managed files; other files are untouched.

    Args:
        workspace_dir: The target's workspace directory; created when missing.
        files: `{file name: content}` from `render_workspace_files`.
        record: The renderer input the files were produced from. When given, `console-record.json` is
            written beside them so drift can be found later; when omitted the record is left as it is.
        snapshot_id: Inventory snapshot `record` was built from, stored in the record file.

    Returns:
        The managed files written, followed by the record file when `record` was given.

    Example:
        >>> write_workspace_files(workspace_dir, files, record=render_input, snapshot_id=12)  # doctest: +SKIP
        [PosixPath('.../parameter_groups.tf'), PosixPath('.../console-record.json')]
    """
    workspace_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name in MANAGED_FILE_NAMES:
        path = workspace_dir / name
        if name in files:
            if not path.exists() or path.read_text(encoding="utf-8") != files[name]:
                path.write_text(files[name], encoding="utf-8")
            written.append(path)
        elif path.exists():
            path.unlink()
    if record is not None:
        written.append(write_record(workspace_dir, record, snapshot_id))
    return written
