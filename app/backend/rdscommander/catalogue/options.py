"""Options models. Every field carries UI hints through `ui_field`."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rdscommander.catalogue.definitions import ui_field
from rdscommander.config.models import AWS_REGION_PATTERN, SLUG_PATTERN, ChangeManagementMode

AWS_REGION_REGULAR_EXPRESSION = re.compile(AWS_REGION_PATTERN)
RESERVED_ENVIRONMENT_SLUGS = frozenset({"compare", "import"})
"""Slugs that would shadow the console's own routes, `/environments/compare` and `/environments/import`."""


class NoOptions(BaseModel):
    """Operations without options."""

    model_config = ConfigDict(extra="forbid")


class InventoryRefreshOptions(BaseModel):
    """Refresh every target of the environment, or only the listed ones."""

    model_config = ConfigDict(extra="forbid")
    target_keys: list[str] = Field(
        default_factory=list,
        json_schema_extra=ui_field(
            "target_picker",
            "Leave empty to refresh every target in the environment.",
            order=1,
            label="Targets",
        ),
    )


class TerraformAdoptionOptions(BaseModel):
    """Which resources to bring under Terraform."""

    model_config = ConfigDict(extra="forbid")
    target_key: str = Field(
        json_schema_extra=ui_field(
            "target_picker", "The account and region holding the resources.", order=1, label="Target"
        )
    )
    resource_arns: list[str] = Field(
        min_length=1,
        json_schema_extra=ui_field(
            "resource_picker",
            "Choose one or more resources not yet under Terraform. Parameter and subnet groups are adopted "
            "before clusters, and clusters before instances.",
            order=2,
            label="Resources to adopt",
            kinds=["subnet_group", "parameter_group", "cluster", "instance"],
            ownership=["unmanaged"],
        ),
    )


class DriftResolveOptions(BaseModel):
    """Accept reality into the record, or restore the record to AWS."""

    model_config = ConfigDict(extra="forbid")
    resource_arn: str = Field(
        json_schema_extra=ui_field("hidden", "The drifted resource.", order=1, label="Resource")
    )
    choice: Literal["accept", "restore"] = Field(
        json_schema_extra=ui_field(
            "choice",
            "Accept keeps what AWS has and updates the record. "
            "Restore changes AWS back to the record through the normal review.",
            order=2,
            label="Resolution",
            labels={"accept": "Accept into the record", "restore": "Restore from the record"},
        )
    )


class EnvironmentImportOptions(BaseModel):
    """Add an AWS account and its regions as an environment.

    Attributes:
        slug: Short name of the new environment, used in URLs and in the repository.
        display_name: Name shown in the environment switcher.
        is_production: Whether the environment is production, which the interface marks everywhere.
        aws_profile_name: Profile from the AWS CLI config on this machine; unset for the simulated account.
        simulated: Use the simulated AWS account instead of a profile.
        regions: Regions to import; empty means every enabled region is scanned and the ones holding
            PostgreSQL resources are kept.
        change_management: `terraform` or `direct`.
    """

    model_config = ConfigDict(extra="forbid")
    slug: str = Field(
        pattern=SLUG_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "2 to 40 lowercase letters, digits and hyphens, starting with a letter, for example staging. "
            "Used in URLs and in the repository.",
            group="Identity",
            order=1,
            label="Short name",
        ),
    )
    display_name: str = Field(
        min_length=1,
        max_length=80,
        json_schema_extra=ui_field(
            "text", "Shown in the environment switcher.", group="Identity", order=2, label="Display name"
        ),
    )
    is_production: bool = Field(
        default=False,
        json_schema_extra=ui_field(
            "boolean",
            "Production environments are marked red everywhere.",
            group="Identity",
            order=3,
            label="Production environment",
        ),
    )
    aws_profile_name: str | None = Field(
        default=None,
        json_schema_extra=ui_field(
            "profile",
            "A profile from ~/.aws/config on this machine.",
            group="Account",
            order=4,
            label="AWS profile",
        ),
    )
    simulated: bool = Field(
        default=False,
        json_schema_extra=ui_field(
            "boolean",
            "Use the simulated AWS account instead of a profile.",
            group="Account",
            order=5,
            label="Simulated account",
        ),
    )
    regions: list[str] = Field(
        default_factory=list,
        json_schema_extra=ui_field(
            "regions",
            "AWS region names such as eu-west-2. Leave empty to scan every enabled region and keep the "
            "ones with PostgreSQL resources.",
            group="Account",
            order=6,
            label="Regions",
        ),
    )
    change_management: ChangeManagementMode = Field(
        default="terraform",
        json_schema_extra=ui_field(
            "choice",
            "Terraform: changes go through plan and apply. "
            "Direct: changes go through the AWS API and Terraform files are kept as the record.",
            group="Mode",
            order=7,
            label="Change management",
            labels={"terraform": "Terraform", "direct": "Direct"},
        ),
    )

    @field_validator("slug")
    @classmethod
    def _slug_is_not_reserved(cls, slug: str) -> str:
        """A slug must not be the name of one of the console's own environment pages."""
        if slug in RESERVED_ENVIRONMENT_SLUGS:
            raise ValueError(
                f"{slug!r} is reserved for a page of the console; choose another slug, for example {slug}-env"
            )
        return slug

    @field_validator("regions")
    @classmethod
    def _regions_are_region_names_without_repeats(cls, regions: list[str]) -> list[str]:
        """Each region looks like `eu-west-2`; repeats are dropped, order is kept."""
        malformed = [region for region in regions if not AWS_REGION_REGULAR_EXPRESSION.match(region)]
        if malformed:
            raise ValueError(f"not AWS region names: {', '.join(malformed)}")
        return list(dict.fromkeys(regions))

    @model_validator(mode="after")
    def _profile_or_simulated(self) -> EnvironmentImportOptions:
        """Exactly one of a profile and the simulated account."""
        if self.simulated == bool(self.aws_profile_name):
            raise ValueError("choose either a profile or the simulated account")
        return self


class ParameterGroupCreateOptions(BaseModel):
    """Create a parameter group from family defaults or by cloning.

    Attributes:
        target_key: Account and region the group is created in.
        name: Name of the new group.
        scope: `instance` for instance groups, `cluster` for Aurora cluster groups.
        family: Engine family, e.g. `postgres16`.
        description: Description shown in AWS.
        source: `defaults` starts empty, `clone` starts from another group's overrides.
        clone_from_arn: ARN of the group to clone; required when `source` is `clone`.
        parameters: Overrides to set on the new group on top of a clone's.
    """

    model_config = ConfigDict(extra="forbid")
    target_key: str = Field(
        json_schema_extra=ui_field(
            "target",
            "The account and region the group is created in.",
            group="Where",
            order=1,
            label="Target",
        )
    )
    name: str = Field(
        pattern=r"^[a-z][a-z0-9-]{0,254}$",
        json_schema_extra=ui_field(
            "text",
            "Up to 255 lowercase letters, digits and hyphens, starting with a letter, for example "
            "orders-postgres16.",
            group="Group",
            order=2,
            label="Name",
        ),
    )
    scope: Literal["instance", "cluster"] = Field(
        json_schema_extra=ui_field(
            "choice",
            "Instance groups apply to instances; cluster groups to Aurora clusters.",
            group="Group",
            order=3,
            label="Scope",
            labels={"instance": "Instance", "cluster": "Cluster"},
        )
    )
    family: str = Field(
        json_schema_extra=ui_field(
            "family", "Engine family, for example postgres16.", group="Group", order=4, label="Family"
        )
    )
    description: str = Field(
        min_length=1,
        max_length=255,
        json_schema_extra=ui_field(
            "text",
            "Shown in AWS with the group, up to 255 characters.",
            group="Group",
            order=5,
            label="Description",
        ),
    )
    source: Literal["defaults", "clone"] = Field(
        default="defaults",
        json_schema_extra=ui_field(
            "choice",
            "Start from the family defaults or copy another group's overrides.",
            group="Contents",
            order=6,
            label="Start from",
            labels={"defaults": "Family defaults", "clone": "Clone a group"},
        ),
    )
    clone_from_arn: str | None = Field(
        default=None,
        json_schema_extra=ui_field(
            "resource",
            "The group whose overrides the new group starts with; needed when cloning.",
            group="Contents",
            order=7,
            label="Clone from",
            resource_kind="parameter_group",
        ),
    )
    parameters: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra=ui_field(
            "parameters", "Overrides to set on the new group.", group="Contents", order=8, label="Parameters"
        ),
    )


class ParameterGroupEditOptions(BaseModel):
    """Set or reset parameters.

    Attributes:
        parameters: Values to set, by parameter name.
        reset: Parameters to return to the family default.
    """

    model_config = ConfigDict(extra="forbid")
    parameters: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra=ui_field(
            "parameters", "Values to set; static parameters need a reboot.", order=1, label="Parameters"
        ),
    )
    reset: list[str] = Field(
        default_factory=list,
        json_schema_extra=ui_field(
            "hidden", "Parameters to return to the family default.", order=2, label="Parameters to reset"
        ),
    )


class ParameterGroupAttachOptions(BaseModel):
    """Attach a group to an instance or cluster.

    Attributes:
        parameter_group_arn: ARN of the group to attach; an instance takes an instance group, a cluster a
            cluster group, and the group's family must be the family of the group attached now.
        reboot: `none` leaves static parameters pending until the next reboot (the instance page offers the
            reboot); `now` reboots after attaching and waits for the instances to report in sync. RDS does
            not reboot for pending parameter changes in the maintenance window, so there is no such option.
    """

    model_config = ConfigDict(extra="forbid")
    parameter_group_arn: str = Field(
        json_schema_extra=ui_field(
            "resource",
            "A group of the same family and scope as the group attached now.",
            order=1,
            label="Parameter group",
            resource_kind="parameter_group",
            same_family_and_scope_as_resource=True,
        )
    )
    reboot: Literal["none", "now"] = Field(
        default="none",
        json_schema_extra=ui_field(
            "choice",
            "Static parameters apply after a reboot; without one they stay pending until the next reboot.",
            order=2,
            label="Reboot",
            labels={
                "none": "Do not reboot (static parameters stay pending)",
                "now": "Reboot now after attaching",
            },
        ),
    )


UPGRADE_GROUP_SCOPES = ("instance", "cluster")
"""The scopes of the groups an upgrade build creates, and so the keys its review edits may use."""


class UpgradeGroupBuildOptions(BaseModel):
    """Build parameter groups for the version an instance or cluster will be upgraded to.

    Attributes:
        target_engine_version: The version it will be upgraded to, e.g. `17.2`; it decides the family.
        name_suffix: Optional text appended to the names of the new groups.
        overrides: The operator's edits from the review step, per scope (`instance`, `cluster`): parameter
            name in the target family to value. They are applied on top of what the classification carries.
    """

    model_config = ConfigDict(extra="forbid")
    target_engine_version: str = Field(
        pattern=r"^\d+(\.\d+){0,2}$",
        json_schema_extra=ui_field(
            "engine_version",
            "A version number such as 17 or 17.2. The version you will upgrade to; it decides the family.",
            order=1,
            label="Target engine version",
        ),
    )
    name_suffix: str = Field(
        default="",
        pattern=r"^[a-z0-9-]{0,40}$",
        json_schema_extra=ui_field(
            "text",
            "Up to 40 lowercase letters, digits and hyphens. Optional; added to the names of the new groups.",
            order=2,
            label="Name suffix",
        ),
    )
    overrides: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        json_schema_extra=ui_field(
            "upgrade_review", "Edits made on the review step, per scope.", order=3, label="Review edits"
        ),
    )

    @field_validator("overrides")
    @classmethod
    def _edits_name_a_scope_that_can_be_built(
        cls, overrides: dict[str, dict[str, str]]
    ) -> dict[str, dict[str, str]]:
        """Each key of `overrides` is `instance` or `cluster`; any other scope would be silently ignored."""
        unknown = sorted(set(overrides) - set(UPGRADE_GROUP_SCOPES))
        if unknown:
            raise ValueError(f"unknown scope {', '.join(unknown)}; use {' or '.join(UPGRADE_GROUP_SCOPES)}")
        return overrides
