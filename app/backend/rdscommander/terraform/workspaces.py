"""Workspace records and directories."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig, ResolvedTarget
from rdscommander.control_db.models import ResourceAdoption, TerraformWorkspace


def ensure_workspace_record(
    session: Session, target: ResolvedTarget, console_config: ConsoleConfig
) -> TerraformWorkspace:
    """Get or create the workspace row for `target`; caller commits."""
    existing = session.execute(
        select(TerraformWorkspace).where(TerraformWorkspace.target_key == target.target_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    workspace = TerraformWorkspace(
        target_key=target.target_key,
        workspace_relative_dir=target.workspace_relative_dir.as_posix(),
        state_bucket_name=target.state_bucket_name(
            console_config.terraform.state_bucket_name_template, console_config.company.company_slug
        ),
        state_key=target.state_key,
    )
    session.add(workspace)
    session.flush()
    return workspace


def workspace_directory(repo_root: Path, workspace: TerraformWorkspace) -> Path:
    """Absolute workspace directory."""
    return repo_root / workspace.workspace_relative_dir


def adopted_terraform_addresses(session: Session, workspace: TerraformWorkspace) -> set[str]:
    """Terraform addresses already imported into `workspace`.

    Args:
        session: Control database session.
        workspace: The workspace row.

    Returns:
        Addresses of the resources whose adoption status is `adopted`.

    Example:
        >>> adopted_terraform_addresses(session, workspace)  # doctest: +SKIP
        {'aws_db_parameter_group.demo_postgres16_tuned'}
    """
    return set(
        session.execute(
            select(ResourceAdoption.terraform_address).where(
                ResourceAdoption.workspace_id == workspace.id,
                ResourceAdoption.adoption_status == "adopted",
            )
        ).scalars()
    )
