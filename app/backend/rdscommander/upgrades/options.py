"""Options for the upgrade operations: the auto minor upgrade setting, and minor and major engine upgrades."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rdscommander.catalogue.definitions import ui_field
from rdscommander.lifecycle.options import ENGINE_VERSION_PATTERN, ApplyAt, apply_at_field


class AutoMinorUpgradeOptions(BaseModel):
    """Whether AWS applies new minor versions on its own in the maintenance window.

    Attributes:
        auto_minor_version_upgrade: True lets AWS upgrade the minor version in the maintenance window.

    Example:
        >>> AutoMinorUpgradeOptions(auto_minor_version_upgrade=False).auto_minor_version_upgrade
        False
    """

    model_config = ConfigDict(extra="forbid")
    auto_minor_version_upgrade: bool = Field(
        json_schema_extra=ui_field(
            "boolean",
            "AWS upgrades to new minor versions on its own in the maintenance window.",
            order=1,
            label="Auto minor upgrade",
        )
    )


class MinorUpgradeOptions(BaseModel):
    """Upgrade an instance or cluster to a newer minor version of the same major version.

    Attributes:
        target_engine_version: A version AWS lists as a minor upgrade target of the current one, e.g. 16.6.
        apply_at: Now, or in the next maintenance window (direct mode only).

    Example:
        >>> MinorUpgradeOptions(target_engine_version="16.6").apply_at
        'now'
    """

    model_config = ConfigDict(extra="forbid")
    target_engine_version: str = Field(
        pattern=ENGINE_VERSION_PATTERN,
        json_schema_extra=ui_field(
            "engine_version",
            "A full version such as 16.6. The list shows the minor versions AWS offers as upgrades of the "
            "current version; the instance restarts (Multi-AZ fails over).",
            order=1,
            label="Target version",
            upgrade_kind="minor",
        ),
    )
    apply_at: ApplyAt = apply_at_field(2)


ParameterGroupChoice = Literal["existing", "build"]
"""`build` reuses or builds groups of the target family; `existing` uses the groups the operator names."""
GROUP_NAME_SUFFIX_PATTERN = r"^[a-z0-9-]{0,40}$"


def _target_family_group_field(order: int, scope: str, help_text: str, label: str) -> Any:
    """A parameter group of the target version's family, of one scope; used with `existing` only."""
    return Field(
        default=None,
        json_schema_extra=ui_field(
            "resource",
            help_text,
            order=order,
            label=label,
            resource_kind="parameter_group",
            group_scope=scope,
            group_of_chosen_version_family=True,
        ),
    )


class MajorUpgradeOptions(BaseModel):
    """Upgrade an instance or cluster to a new major version through a blue/green deployment.

    Attributes:
        target_engine_version: A version AWS lists as a major upgrade target of the current one, e.g. 17.2.
        parameter_group_choice: `build` (default) reuses the groups an earlier upgrade build made for this
            resource in the target family, or builds them; `existing` uses the groups named below.
        instance_parameter_group_name: With `existing`: the instance parameter group of the target family
            (for a cluster, the one its members get).
        cluster_parameter_group_name: With `existing`, for a cluster: its cluster parameter group of the
            target family.
        group_name_suffix: With `build`: text appended to the names of the groups built (and, when given,
            only groups of exactly that name are reused).

    Example:
        >>> MajorUpgradeOptions(target_engine_version="17.2").parameter_group_choice
        'build'
    """

    model_config = ConfigDict(extra="forbid")
    target_engine_version: str = Field(
        pattern=ENGINE_VERSION_PATTERN,
        json_schema_extra=ui_field(
            "engine_version",
            "A full version such as 17.2. The list shows the major versions AWS offers as upgrades of the "
            "current version; a green copy is created on it and production switches over only after a "
            "second approval.",
            order=1,
            label="Target version",
            upgrade_kind="major",
        ),
    )
    parameter_group_choice: ParameterGroupChoice = Field(
        default="build",
        json_schema_extra=ui_field(
            "choice",
            "Build carries the current groups' overrides to the target family (or reuses groups built "
            "for this resource earlier); Existing uses groups you name.",
            order=2,
            label="Parameter groups",
            labels={"build": "Build from the current groups", "existing": "Use existing groups"},
        ),
    )
    instance_parameter_group_name: str | None = _target_family_group_field(
        3,
        "instance",
        "An instance parameter group of the target version's family; needed with Use existing groups.",
        "Instance parameter group",
    )
    cluster_parameter_group_name: str | None = _target_family_group_field(
        4,
        "cluster",
        "A cluster parameter group of the target version's family; needed with Use existing groups on a "
        "cluster.",
        "Cluster parameter group",
    )
    group_name_suffix: str = Field(
        default="",
        pattern=GROUP_NAME_SUFFIX_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "Up to 40 lowercase letters, digits and hyphens. Optional; added to the names of the groups "
            "built.",
            order=5,
            label="Name suffix",
        ),
    )

    @field_validator("instance_parameter_group_name", "cluster_parameter_group_name", mode="before")
    @classmethod
    def _group_name_from_name_or_arn(cls, value: Any) -> Any:
        """An empty choice is no group; the picker's ARN becomes the group's name."""
        if not isinstance(value, str):
            return value
        if not value.strip():
            return None
        if value.startswith("arn:"):
            return value.rsplit(":", 1)[-1]
        return value

    @model_validator(mode="after")
    def _group_names_go_with_existing_groups(self) -> MajorUpgradeOptions:
        """Group names go with `existing` only, which needs at least the instance group.

        The cluster group is checked against the resource later; `build` refuses names, which it would
        otherwise ignore silently.
        """
        named = self.instance_parameter_group_name or self.cluster_parameter_group_name
        if self.parameter_group_choice == "existing" and not self.instance_parameter_group_name:
            raise ValueError("name the instance parameter group of the target family to use existing groups")
        if self.parameter_group_choice == "build" and named:
            raise ValueError("parameter group names are used only with existing groups; choose existing")
        return self


DEPLOYMENT_ID_PATTERN = r"^bgd-[0-9a-z]{1,60}$"


class BlueGreenCleanupOptions(BaseModel):
    """Delete a blue/green deployment that a major upgrade left in place (failed or rejected).

    Attributes:
        delete_green: Delete the green instance or cluster with the deployment (default). Off keeps the
            green side as standalone databases that belong to no deployment.
        deployment_id: The deployment to delete; the resource's deployment left in place when empty (the
            inbox fills it in).

    Example:
        >>> BlueGreenCleanupOptions().delete_green
        True
    """

    model_config = ConfigDict(extra="forbid")
    delete_green: bool = Field(
        default=True,
        json_schema_extra=ui_field(
            "boolean",
            "Delete the green copy with the deployment. Off keeps it as a standalone database that belongs "
            "to no deployment; production is never touched.",
            order=1,
            label="Delete the green side",
        ),
    )
    deployment_id: str | None = Field(
        default=None,
        pattern=DEPLOYMENT_ID_PATTERN,
        json_schema_extra=ui_field(
            "hidden",
            "The deployment to delete, such as bgd-0123456789abcdef; the one left on the resource when "
            "empty.",
            order=2,
            label="Deployment",
        ),
    )
