"""Validated shape of the `config/` directory in the workspace.

Example:
    >>> config = ConsoleConfig.model_validate(raw)
    >>> config.target("production/123456789012/eu-west-2").workspace_relative_dir
    PosixPath('terraform/production/123456789012/eu-west-2')
"""

from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG_PATTERN = r"^[a-z][a-z0-9-]{1,39}$"
AWS_REGION_PATTERN = r"^[a-z]{2}-[a-z]+-\d$"
ChangeManagementMode = Literal["terraform", "direct"]
STATE_BUCKET_TEMPLATE_FIELDS = frozenset({"aws_account_id", "aws_region"})
S3_BUCKET_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{1,61})[a-z0-9]$")


class EnvironmentTarget(BaseModel):
    """One AWS account in one region."""

    model_config = ConfigDict(extra="forbid")

    aws_account_id: str = Field(pattern=r"^\d{12}$")
    aws_region: str = Field(pattern=AWS_REGION_PATTERN)
    aws_profile_name: str | None = None
    simulated: bool = False

    @model_validator(mode="after")
    def _profile_matches_mode(self) -> EnvironmentTarget:
        if self.simulated and self.aws_profile_name is not None:
            raise ValueError("a simulated target must not set aws_profile_name")
        if not self.simulated and not self.aws_profile_name:
            raise ValueError("aws_profile_name is required for a real target")
        return self


class EnvironmentConfig(BaseModel):
    """One named environment such as production or staging."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(pattern=SLUG_PATTERN)
    display_name: str = Field(min_length=1)
    is_production: bool = False
    requires_approval: bool = True
    change_management: ChangeManagementMode = "terraform"
    allow_direct_changes_to_foreign_owned: bool = False
    inventory_refresh_minutes: int = Field(default=15, ge=1, le=1440)
    drift_check_hours: int = Field(default=24, ge=1, le=168)
    targets: list[EnvironmentTarget] = Field(min_length=1)

    @model_validator(mode="after")
    def _approval_is_mandatory(self) -> EnvironmentConfig:
        if not self.requires_approval:
            raise ValueError(
                "requires_approval must be true in this release; unattended applies are not supported yet"
            )
        return self


class TerraformConfig(BaseModel):
    """`config/terraform.yaml`."""

    model_config = ConfigDict(extra="forbid")

    required_opentofu_version: str = "~> 1.10"
    aws_provider_version: str = "~> 6.0"
    state_bucket_name_template: str = "rdscommander-state-{aws_account_id}-{aws_region}"

    @field_validator("state_bucket_name_template")
    @classmethod
    def _template_uses_known_fields_only(cls, template: str) -> str:
        """Refuse a template that names anything but the account id and region.

        Raises:
            ValueError: The template names another field,.
        """
        unknown_fields = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(template)
            if field_name and field_name not in STATE_BUCKET_TEMPLATE_FIELDS
        }
        if unknown_fields:
            raise ValueError(
                f"state_bucket_name_template names unknown fields {sorted(unknown_fields)}; "
                f"use only {sorted(STATE_BUCKET_TEMPLATE_FIELDS)} in config/terraform.yaml"
            )
        return template


class ResolvedTarget(BaseModel):
    """A target joined with its environment; the unit of inventory, workspace, and AWS session."""

    model_config = ConfigDict(frozen=True)

    environment_slug: str
    environment_display_name: str
    requires_approval: bool
    aws_account_id: str
    aws_region: str
    aws_profile_name: str | None
    simulated: bool
    change_management: ChangeManagementMode = "terraform"
    allow_direct_changes_to_foreign_owned: bool = False

    @property
    def is_direct(self) -> bool:
        """True when changes go straight to the AWS API instead of through Terraform."""
        return self.change_management == "direct"

    @property
    def target_key(self) -> str:
        """`<environment_slug>/<aws_account_id>/<aws_region>`."""
        return f"{self.environment_slug}/{self.aws_account_id}/{self.aws_region}"

    @property
    def workspace_relative_dir(self) -> Path:
        """Terraform workspace directory relative to the repository root."""
        return Path("terraform") / self.environment_slug / self.aws_account_id / self.aws_region

    @property
    def state_key(self) -> str:
        """Object key of the Terraform state inside the state bucket."""
        return f"rdscommander/{self.target_key}/terraform.tfstate"

    def state_bucket_name(self, template: str) -> str:
        """Render and validate the state bucket name for this target."""
        name = template.format(aws_account_id=self.aws_account_id, aws_region=self.aws_region)
        if not S3_BUCKET_NAME_PATTERN.match(name) or ".." in name:
            raise ValueError(
                f"rendered S3 bucket name {name!r} breaks S3 naming rules "
                "(lowercase, 3-63 chars, no underscores); "
                "fix state_bucket_name_template in config/terraform.yaml"
            )
        return name


class ConsoleConfig(BaseModel):
    """Everything under `config/`."""

    model_config = ConfigDict(extra="forbid")

    environments: list[EnvironmentConfig] = Field(min_length=1)
    terraform: TerraformConfig = Field(default_factory=TerraformConfig)

    @model_validator(mode="after")
    def _reject_duplicates(self) -> ConsoleConfig:
        slugs: set[str] = set()
        keys: set[str] = set()
        for environment in self.environments:
            if environment.slug in slugs:
                raise ValueError(f"duplicate environment slug: {environment.slug}")
            slugs.add(environment.slug)
            for target in environment.targets:
                key = f"{environment.slug}/{target.aws_account_id}/{target.aws_region}"
                if key in keys:
                    raise ValueError(f"duplicate target: {key}")
                keys.add(key)
        return self

    def environment(self, slug: str) -> EnvironmentConfig:
        """Return the environment named `slug`."""
        for environment in self.environments:
            if environment.slug == slug:
                return environment
        known = ", ".join(e.slug for e in self.environments)
        raise KeyError(f"unknown environment {slug!r}; known: {known}")

    def all_targets(self) -> list[ResolvedTarget]:
        """Every target in configuration order."""
        resolved: list[ResolvedTarget] = []
        for environment in self.environments:
            for target in environment.targets:
                resolved.append(
                    ResolvedTarget(
                        environment_slug=environment.slug,
                        environment_display_name=environment.display_name,
                        requires_approval=environment.requires_approval,
                        aws_account_id=target.aws_account_id,
                        aws_region=target.aws_region,
                        aws_profile_name=target.aws_profile_name,
                        simulated=target.simulated,
                        change_management=environment.change_management,
                        allow_direct_changes_to_foreign_owned=environment.allow_direct_changes_to_foreign_owned,
                    )
                )
        return resolved

    def targets_for_environment(self, slug: str) -> list[ResolvedTarget]:
        """Targets of one environment."""
        return [t for t in self.all_targets() if t.environment_slug == slug]

    def target(self, target_key: str) -> ResolvedTarget:
        """Return the target with `target_key`."""
        for resolved in self.all_targets():
            if resolved.target_key == target_key:
                return resolved
        known = ", ".join(t.target_key for t in self.all_targets())
        raise KeyError(f"unknown target {target_key!r}; known: {known}")

    @property
    def has_simulated_targets(self) -> bool:
        """True when any target is simulated."""
        return any(t.simulated for t in self.all_targets())
