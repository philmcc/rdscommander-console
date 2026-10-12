"""Options for the lifecycle operations: instance and cluster settings, actions, creates and removals."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rdscommander.catalogue.definitions import ui_field
from rdscommander.lifecycle.restore_window import format_utc, parse_utc

ApplyAt = Literal["now", "maintenance_window"]
INSTANCE_CLASS_PATTERN = r"^(db\.serverless|db\.[a-z0-9]+\.[a-z0-9]+)$"
"""`db.<family>.<size>`, or `db.serverless` (an Aurora Serverless v2 member, as the picker offers it)."""
INSTANCE_CLASS_REFUSAL = "an instance class starts with db. and looks like db.m6g.xlarge, or is db.serverless"
_DAY = "(mon|tue|wed|thu|fri|sat|sun)"
WINDOW_PATTERN = rf"^{_DAY}:[0-2][0-9]:[0-5][0-9]-{_DAY}:[0-2][0-9]:[0-5][0-9]$"
BACKUP_WINDOW_PATTERN = r"^[0-2][0-9]:[0-5][0-9]-[0-2][0-9]:[0-5][0-9]$"


def apply_at_field(order: int) -> Any:
    """The shared `apply_at` option: now, or queued for the maintenance window (direct mode only).

    Args:
        order: Position of the field within its form.

    Returns:
        A pydantic `Field` defaulting to `now`.

    Example:
        >>> apply_at_field(2).default
        'now'
    """
    return Field(
        default="now",
        json_schema_extra=ui_field(
            "choice",
            "When AWS applies the change. Terraform mode always applies now.",
            order=order,
            label="Apply",
            labels={"now": "Now", "maintenance_window": "In the next maintenance window"},
        ),
    )


class InstanceClassChangeOptions(BaseModel):
    """Change the instance class of an instance or cluster member.

    Attributes:
        instance_class: The new class, such as db.m6g.xlarge.
        apply_at: Now, or in the next maintenance window.
    """

    model_config = ConfigDict(extra="forbid")
    instance_class: str = Field(
        pattern=INSTANCE_CLASS_PATTERN,
        json_schema_extra=ui_field(
            "instance_class",
            "An RDS instance class such as db.m6g.xlarge, or db.serverless for Aurora Serverless v2. "
            "The list shows classes AWS offers for this engine and version.",
            order=1,
            label="Instance class",
        ),
    )
    apply_at: ApplyAt = apply_at_field(2)

    @field_validator("instance_class", mode="before")
    @classmethod
    def _looks_like_an_rds_class(cls, value: Any) -> Any:
        """Refuse a class that does not start with `db.`, in a sentence; the pattern stays in the schema."""
        if isinstance(value, str) and not re.match(INSTANCE_CLASS_PATTERN, value):
            raise ValueError(INSTANCE_CLASS_REFUSAL)
        return value


class StorageChangeOptions(BaseModel):
    """Change the storage of a standalone instance; storage only grows.

    Attributes:
        allocated_storage_gb: New size in GiB; must not be below the current size.
        max_allocated_storage_gb: Autoscaling ceiling in GiB, or 0 to turn autoscaling off.
        storage_type: gp2, gp3, io1 or io2.
        iops: Provisioned IOPS; io1 and io2 only, and gp3 above its baseline.
        storage_throughput: gp3 throughput in MiB/s.
        apply_at: Now, or in the next maintenance window.
    """

    model_config = ConfigDict(extra="forbid")
    allocated_storage_gb: int | None = Field(
        default=None,
        ge=20,
        le=65536,
        json_schema_extra=ui_field(
            "number", "Size in GiB. Storage can only grow.", order=1, label="Allocated storage (GiB)"
        ),
    )
    max_allocated_storage_gb: int | None = Field(
        default=None,
        ge=0,
        le=65536,
        json_schema_extra=ui_field(
            "number",
            "Autoscaling ceiling in GiB; 0 turns autoscaling off.",
            order=2,
            label="Autoscaling ceiling (GiB)",
        ),
    )
    storage_type: Literal["gp2", "gp3", "io1", "io2"] | None = Field(
        default=None,
        json_schema_extra=ui_field(
            "choice",
            "The storage type.",
            order=3,
            label="Storage type",
            labels={"gp2": "gp2", "gp3": "gp3", "io1": "io1", "io2": "io2"},
        ),
    )
    iops: int | None = Field(
        default=None,
        ge=1000,
        le=256000,
        json_schema_extra=ui_field(
            "number", "Provisioned IOPS. Only io1, io2 and gp3 take a value.", order=4, label="IOPS"
        ),
    )
    storage_throughput: int | None = Field(
        default=None,
        ge=125,
        le=4000,
        json_schema_extra=ui_field("number", "gp3 throughput in MiB/s.", order=5, label="Throughput (MiB/s)"),
    )
    apply_at: ApplyAt = apply_at_field(6)

    @model_validator(mode="after")
    def _consistent(self) -> StorageChangeOptions:
        """At least one setting is chosen, and IOPS and throughput match the storage type."""
        chosen = (
            self.allocated_storage_gb,
            self.max_allocated_storage_gb,
            self.storage_type,
            self.iops,
            self.storage_throughput,
        )
        if all(value is None for value in chosen):
            raise ValueError("choose at least one storage setting to change")
        if self.iops is not None and self.storage_type == "gp2":
            raise ValueError(
                "IOPS can be set on io1 or io2 storage, or on gp3 above its baseline, not on gp2"
            )
        if self.storage_throughput is not None and self.storage_type not in (None, "gp3"):
            raise ValueError("throughput applies to gp3 storage only")
        return self


class MultiAzChangeOptions(BaseModel):
    """Turn Multi-AZ on or off for a standalone instance.

    Attributes:
        multi_az: True to add a standby in another availability zone.
        apply_at: Now, or in the next maintenance window.
    """

    model_config = ConfigDict(extra="forbid")
    multi_az: bool = Field(
        json_schema_extra=ui_field(
            "boolean",
            "A standby in another availability zone, with automatic failover.",
            order=1,
            label="Multi-AZ",
        ),
    )
    apply_at: ApplyAt = apply_at_field(2)


def _readable_window(value: Any, pattern: str, shape: str) -> Any:
    """Refuse a string that is not a window, in a sentence naming the shape."""
    if isinstance(value, str) and not re.match(pattern, value):
        raise ValueError(f"a window looks like {shape} in UTC")
    return value


class BackupSettingsOptions(BaseModel):
    """Backups of an instance or cluster.

    Attributes:
        backup_retention_period_days: 0 disables automated backups; 1 to 35 keeps them that many days.
        preferred_backup_window: hh:mm-hh:mm in UTC, or None to let AWS choose.
        copy_tags_to_snapshot: Whether snapshots inherit the resource's tags; None leaves it as is.
        apply_at: Now, or in the next maintenance window.
    """

    model_config = ConfigDict(extra="forbid")
    backup_retention_period_days: int = Field(
        ge=0,
        le=35,
        json_schema_extra=ui_field(
            "number",
            "Days to keep automated backups; 0 turns them off (not allowed for a source of replicas).",
            order=1,
            label="Retention (days)",
        ),
    )
    preferred_backup_window: str | None = Field(
        default=None,
        pattern=BACKUP_WINDOW_PATTERN,
        json_schema_extra=ui_field(
            "window",
            "Daily window as hh:mm-hh:mm in UTC, at least 30 minutes.",
            order=2,
            label="Backup window",
            window_kind="daily",
        ),
    )
    copy_tags_to_snapshot: bool | None = Field(
        default=None,
        json_schema_extra=ui_field(
            "boolean", "Snapshots carry the resource's tags.", order=3, label="Copy tags to snapshots"
        ),
    )
    apply_at: ApplyAt = apply_at_field(4)

    @field_validator("preferred_backup_window", mode="before")
    @classmethod
    def _looks_like_a_daily_window(cls, value: Any) -> Any:
        """Refuse a backup window that is not hh:mm-hh:mm, in a sentence."""
        return _readable_window(value, BACKUP_WINDOW_PATTERN, "hh:mm-hh:mm")


class MaintenanceWindowOptions(BaseModel):
    """The weekly maintenance window of an instance or cluster.

    Attributes:
        preferred_maintenance_window: ddd:hh:mm-ddd:hh:mm in UTC, at least 30 minutes.
    """

    model_config = ConfigDict(extra="forbid")
    preferred_maintenance_window: str = Field(
        pattern=WINDOW_PATTERN,
        json_schema_extra=ui_field(
            "window",
            "Weekly window as ddd:hh:mm-ddd:hh:mm in UTC, at least 30 minutes.",
            order=1,
            label="Maintenance window",
            window_kind="weekly",
        ),
    )

    @field_validator("preferred_maintenance_window", mode="before")
    @classmethod
    def _looks_like_a_weekly_window(cls, value: Any) -> Any:
        """Refuse a maintenance window that is not ddd:hh:mm-ddd:hh:mm, in a sentence."""
        return _readable_window(value, WINDOW_PATTERN, "ddd:hh:mm-ddd:hh:mm")


class MonitoringOptions(BaseModel):
    """Performance Insights and enhanced monitoring of an instance or cluster member.

    Attributes:
        performance_insights_enabled: Turn Performance Insights on or off.
        performance_insights_retention_period_days: 7 (free tier), a multiple of 31 up to 713 or 731;
            None keeps it.
        monitoring_interval_seconds: Enhanced monitoring granularity; 0 turns it off.
        monitoring_role_arn: IAM role for enhanced monitoring. When the interval is not 0 and this is
            None, the change keeps the role the resource has; one without a role is refused at review.
        apply_at: Now, or in the next maintenance window.
    """

    model_config = ConfigDict(extra="forbid")
    performance_insights_enabled: bool = Field(
        json_schema_extra=ui_field(
            "boolean",
            "Query-level performance data in the AWS console.",
            order=1,
            label="Performance Insights",
        )
    )
    performance_insights_retention_period_days: int | None = Field(
        default=None,
        json_schema_extra=ui_field(
            "number",
            "7 for the free tier, a multiple of 31 up to 713, or 731.",
            order=2,
            label="Insights retention (days)",
        ),
    )
    monitoring_interval_seconds: Literal[0, 1, 5, 10, 15, 30, 60] = Field(
        json_schema_extra=ui_field(
            "choice",
            "Enhanced monitoring granularity; 0 turns it off.",
            order=3,
            label="Enhanced monitoring",
            labels={
                "0": "Off",
                "1": "1 s",
                "5": "5 s",
                "10": "10 s",
                "15": "15 s",
                "30": "30 s",
                "60": "60 s",
            },
        )
    )
    monitoring_role_arn: str | None = Field(
        default=None,
        pattern=r"^arn:aws[a-z-]*:iam::\d{12}:role/.+$",
        json_schema_extra=ui_field(
            "text",
            "The IAM role enhanced monitoring publishes with, as an ARN. Leave it empty to keep the "
            "role the resource has.",
            order=4,
            label="Monitoring role ARN",
        ),
    )
    apply_at: ApplyAt = apply_at_field(5)

    @model_validator(mode="after")
    def _role_with_interval(self) -> MonitoringOptions:
        """Insights retention is 7, a multiple of 31 up to 713, or 731.

        Whether enhanced monitoring has a role depends on the live resource, so the executor checks it.
        """
        retention = self.performance_insights_retention_period_days
        if (
            retention is not None
            and retention not in (7, 731)
            and (retention % 31 != 0 or not 0 < retention < 731)
        ):
            raise ValueError("Performance Insights retention is 7 days, a multiple of 31 up to 713, or 731")
        return self


class DeletionProtectionOptions(BaseModel):
    """Deletion protection of an instance or cluster.

    Attributes:
        deletion_protection: True refuses deletes in AWS until it is turned off.
    """

    model_config = ConfigDict(extra="forbid")
    deletion_protection: bool = Field(
        json_schema_extra=ui_field(
            "boolean",
            "AWS refuses to delete the resource while this is on.",
            order=1,
            label="Deletion protection",
        )
    )


class ServerlessCapacityOptions(BaseModel):
    """Aurora Serverless v2 capacity range of a cluster.

    Attributes:
        min_capacity_acu: Minimum ACUs, in steps of 0.5.
        max_capacity_acu: Maximum ACUs, at or above the minimum.
    """

    model_config = ConfigDict(extra="forbid")
    min_capacity_acu: float = Field(
        ge=0.5,
        le=128,
        json_schema_extra=ui_field(
            "number", "Lowest capacity in ACUs, in steps of 0.5.", order=1, label="Minimum ACUs", step=0.5
        ),
    )
    max_capacity_acu: float = Field(
        ge=0.5,
        le=128,
        json_schema_extra=ui_field(
            "number",
            "Highest capacity in ACUs, at or above the minimum.",
            order=2,
            label="Maximum ACUs",
            step=0.5,
        ),
    )

    @model_validator(mode="after")
    def _ordered_half_steps(self) -> ServerlessCapacityOptions:
        """Capacities move in steps of 0.5 ACU and the maximum is not below the minimum."""
        for value in (self.min_capacity_acu, self.max_capacity_acu):
            if (value * 2) % 1 != 0:
                raise ValueError("capacity moves in steps of 0.5 ACU")
        if self.max_capacity_acu < self.min_capacity_acu:
            raise ValueError("the maximum must be at or above the minimum")
        return self


class TagsOptions(BaseModel):
    """Set and remove tags on a resource; console and AWS tags are never touched.

    Attributes:
        set_tags: Tags to set, `{key: value}`.
        remove_tags: Tag keys to remove.
    """

    model_config = ConfigDict(extra="forbid")
    set_tags: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra=ui_field("tags", "Tags to add or change.", order=1, label="Set tags"),
    )
    remove_tags: list[str] = Field(
        default_factory=list,
        json_schema_extra=ui_field(
            "tags", "Tag keys to remove.", order=2, label="Remove tags", keys_only=True
        ),
    )

    @model_validator(mode="after")
    def _allowed_and_non_empty(self) -> TagsOptions:
        """No console or AWS tag keys, and at least one change."""
        for key in (*self.set_tags, *self.remove_tags):
            if key.startswith(("rdscommander:", "aws:")):
                raise ValueError(
                    f"the tag {key} starts with rdscommander: or aws: and cannot be changed here"
                )
        if not self.set_tags and not self.remove_tags:
            raise ValueError("nothing to change: set or remove at least one tag")
        return self


class RebootInstanceOptions(BaseModel):
    """Reboot an instance.

    Attributes:
        force_failover: Multi-AZ only: fail over to the standby instead of restarting in place.
    """

    model_config = ConfigDict(extra="forbid")
    force_failover: bool = Field(
        default=False,
        json_schema_extra=ui_field(
            "boolean",
            "Multi-AZ only: fail over to the standby instead of restarting in place.",
            order=1,
            label="Force failover",
        ),
    )


class FailoverOptions(BaseModel):
    """Fail an Aurora cluster over to another member.

    Attributes:
        target_member_identifier: The member to promote; None lets AWS pick the highest-priority reader.
    """

    model_config = ConfigDict(extra="forbid")
    target_member_identifier: str | None = Field(
        default=None,
        json_schema_extra=ui_field(
            "resource",
            "The member to promote; leave empty to let AWS choose the highest-priority reader.",
            order=1,
            label="Target member",
            resource_kind="instance",
            members_of_resource=True,
        ),
    )

    @field_validator("target_member_identifier", mode="before")
    @classmethod
    def _empty_means_let_aws_choose(cls, value: Any) -> Any:
        """An empty or blank target is no target: the help text says to leave it empty."""
        if isinstance(value, str) and not value.strip():
            return None
        return value


class PendingMaintenanceOptions(BaseModel):
    """Apply, schedule or undo a pending maintenance action.

    Attributes:
        action: The action name from the pending maintenance list, such as system-update.
        opt_in_type: Apply now, in the next maintenance window, or undo an earlier opt-in.
    """

    model_config = ConfigDict(extra="forbid")
    action: str = Field(
        min_length=1,
        max_length=64,
        json_schema_extra=ui_field(
            "text",
            "The action name from the pending maintenance list, e.g. system-update.",
            order=1,
            label="Maintenance action",
        ),
    )
    opt_in_type: Literal["immediate", "next-maintenance", "undo-opt-in"] = Field(
        json_schema_extra=ui_field(
            "choice",
            "When AWS applies the action, or whether to undo an earlier choice.",
            order=2,
            label="Apply",
            labels={
                "immediate": "Apply now",
                "next-maintenance": "Apply in the next maintenance window",
                "undo-opt-in": "Undo the opt-in",
            },
        )
    )


SNAPSHOT_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9-]{0,254}$"
"""A snapshot name: lower case, starts with a letter, letters, digits and hyphens."""


def readable_snapshot_name(value: Any) -> Any:
    """Refuse a snapshot name RDS would refuse, in a sentence: shape, double hyphens and a trailing hyphen.

    Args:
        value: The proposed name; anything but a string is left to the type check.

    Returns:
        The value unchanged when RDS accepts it.

    Raises:
        ValueError: The name breaks one of RDS's naming rules.

    Example:
        >>> readable_snapshot_name("demo-orders-before-change")
        'demo-orders-before-change'
    """
    if not isinstance(value, str):
        return value
    if not re.match(SNAPSHOT_IDENTIFIER_PATTERN, value):
        raise ValueError(
            "a snapshot name starts with a lower-case letter and has only lower-case letters, digits "
            "and hyphens, up to 255 characters"
        )
    if "--" in value:
        raise ValueError("a snapshot name cannot contain two hyphens in a row")
    if value.endswith("-"):
        raise ValueError("a snapshot name cannot end with a hyphen")
    return value


class TakeSnapshotOptions(BaseModel):
    """Take a manual snapshot of an instance or cluster.

    Attributes:
        snapshot_identifier: The new snapshot's name.
        tags: Tags to put on the snapshot, `{key: value}`.
    """

    model_config = ConfigDict(extra="forbid")
    snapshot_identifier: str = Field(
        pattern=SNAPSHOT_IDENTIFIER_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "A name that starts with a lower-case letter and has only lower-case letters, digits and "
            "hyphens, such as demo-orders-before-change.",
            order=1,
            label="Snapshot name",
        ),
    )
    tags: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra=ui_field("tags", "Tags to put on the snapshot.", order=2, label="Tags"),
    )

    @field_validator("snapshot_identifier", mode="before")
    @classmethod
    def _readable_snapshot_name(cls, value: Any) -> Any:
        """Refuse a name RDS would refuse, in a sentence (`readable_snapshot_name`)."""
        return readable_snapshot_name(value)

    @field_validator("tags")
    @classmethod
    def _no_console_or_aws_tags(cls, value: dict[str, str]) -> dict[str, str]:
        """No console or AWS tag keys: those are never written by the console."""
        for key in value:
            if key.startswith(("rdscommander:", "aws:")):
                raise ValueError(f"the tag {key} starts with rdscommander: or aws: and cannot be set here")
        return value


class PromoteReplicaOptions(BaseModel):
    """Promote a read replica to a standalone instance.

    Attributes:
        backup_retention_period_days: Days of automated backups the promoted instance keeps; a replica
            often has none, and a standalone instance should have some.
    """

    model_config = ConfigDict(extra="forbid")
    backup_retention_period_days: int = Field(
        default=7,
        ge=1,
        le=35,
        json_schema_extra=ui_field(
            "number",
            "Days to keep automated backups once the replica is a standalone instance (1 to 35).",
            order=1,
            label="Backup retention (days)",
        ),
    )


RESOURCE_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9-]{0,62}$"
"""An instance or cluster identifier: lower case, starts with a letter, letters, digits and hyphens, 1-63."""
StorageType = Literal["gp2", "gp3", "io1", "io2"]


def readable_resource_identifier(value: Any, what: str) -> Any:
    """Refuse an identifier RDS would refuse, in a sentence: shape, double hyphens and a trailing hyphen.

    Args:
        value: The proposed identifier; anything but a string is left to the type check.
        what: What the identifier names, such as `a replica name`, for the sentence.

    Returns:
        The value unchanged when RDS accepts it.

    Raises:
        ValueError: The identifier breaks one of RDS's naming rules.

    Example:
        >>> readable_resource_identifier("demo-orders-replica-2", "a replica name")
        'demo-orders-replica-2'
    """
    if not isinstance(value, str):
        return value
    if not re.match(RESOURCE_IDENTIFIER_PATTERN, value):
        raise ValueError(
            f"{what} starts with a lower-case letter and has only lower-case letters, digits and hyphens, "
            "up to 63 characters"
        )
    if "--" in value:
        raise ValueError(f"{what} cannot contain two hyphens in a row")
    if value.endswith("-"):
        raise ValueError(f"{what} cannot end with a hyphen")
    return value


def _instance_class_field(order: int) -> Any:
    """The instance class option of a create, offered from the classes AWS has for the engine version."""
    return Field(
        pattern=INSTANCE_CLASS_PATTERN,
        json_schema_extra=ui_field(
            "instance_class",
            "An RDS instance class such as db.m6g.large. "
            "The list shows classes AWS offers for this engine and version.",
            order=order,
            label="Instance class",
        ),
    )


def _readable_instance_class(value: Any) -> Any:
    """Refuse a class that does not look like `db.<family>.<size>` or `db.serverless`, in a sentence."""
    if isinstance(value, str) and not re.match(INSTANCE_CLASS_PATTERN, value):
        raise ValueError(INSTANCE_CLASS_REFUSAL)
    return value


class ReadReplicaCreateOptions(BaseModel):
    """Create a read replica of an instance.

    Attributes:
        replica_identifier: The new replica's identifier.
        instance_class: The replica's instance class.
        multi_az: A standby for the replica in another availability zone.
        storage_type: The replica's storage type; the source's when not given.
        iops: Provisioned IOPS; io1 and io2 need it, gp3 takes it above its baseline.
    """

    model_config = ConfigDict(extra="forbid")
    replica_identifier: str = Field(
        pattern=RESOURCE_IDENTIFIER_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "A name that starts with a lower-case letter and has only lower-case letters, digits and "
            "hyphens, such as demo-orders-replica-2.",
            order=1,
            label="Replica name",
        ),
    )
    instance_class: str = _instance_class_field(2)
    multi_az: bool = Field(
        default=False,
        json_schema_extra=ui_field(
            "boolean", "A standby for the replica in another availability zone.", order=3, label="Multi-AZ"
        ),
    )
    storage_type: StorageType | None = Field(
        default=None,
        json_schema_extra=ui_field(
            "choice",
            "The replica's storage type; leave empty to use the source's.",
            order=4,
            label="Storage type",
            labels={"gp2": "gp2", "gp3": "gp3", "io1": "io1", "io2": "io2"},
        ),
    )
    iops: int | None = Field(
        default=None,
        ge=1000,
        le=256000,
        json_schema_extra=ui_field(
            "number", "Provisioned IOPS. Only io1, io2 and gp3 take a value.", order=5, label="IOPS"
        ),
    )

    @field_validator("replica_identifier", mode="before")
    @classmethod
    def _readable_replica_name(cls, value: Any) -> Any:
        """Refuse a name RDS would refuse, in a sentence."""
        return readable_resource_identifier(value, "a replica name")

    @field_validator("instance_class", mode="before")
    @classmethod
    def _looks_like_an_rds_class(cls, value: Any) -> Any:
        """Refuse a class that does not start with `db.`, in a sentence; the pattern stays in the schema."""
        return _readable_instance_class(value)

    @model_validator(mode="after")
    def _iops_match_the_storage_type(self) -> ReadReplicaCreateOptions:
        """IOPS go with io1, io2 or gp3, and io1 and io2 need them."""
        if self.iops is not None and self.storage_type == "gp2":
            raise ValueError(
                "IOPS can be set on io1 or io2 storage, or on gp3 above its baseline, not on gp2"
            )
        if self.storage_type in ("io1", "io2") and self.iops is None:
            raise ValueError("io1 and io2 storage needs IOPS; give a value from 1000")
        return self


class ClusterReaderCreateOptions(BaseModel):
    """Add a reader instance to an Aurora cluster.

    Attributes:
        reader_identifier: The new reader's identifier.
        instance_class: The reader's instance class.
        promotion_tier: Failover priority, 0 (first) to 15 (last).
        parameter_group_name: An instance parameter group of the cluster's family; AWS's default when None.
            The picker's ARN is accepted and kept as the group's name.
        performance_insights_enabled: Turn Performance Insights on for the reader.
    """

    model_config = ConfigDict(extra="forbid")
    reader_identifier: str = Field(
        pattern=RESOURCE_IDENTIFIER_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "A name that starts with a lower-case letter and has only lower-case letters, digits and "
            "hyphens, such as demo-aurora-reader-2.",
            order=1,
            label="Reader name",
        ),
    )
    instance_class: str = _instance_class_field(2)
    promotion_tier: int = Field(
        default=1,
        ge=0,
        le=15,
        json_schema_extra=ui_field(
            "number",
            "Failover priority from 0 (promoted first) to 15 (promoted last).",
            order=3,
            label="Promotion tier",
        ),
    )
    parameter_group_name: str | None = Field(
        default=None,
        json_schema_extra=ui_field(
            "resource",
            "An instance parameter group of the cluster's family; leave empty for the AWS default group.",
            order=4,
            label="Parameter group",
            resource_kind="parameter_group",
            instance_group_of_cluster_family=True,
        ),
    )
    performance_insights_enabled: bool = Field(
        default=True,
        json_schema_extra=ui_field(
            "boolean", "Collect Performance Insights for the reader.", order=5, label="Performance Insights"
        ),
    )

    @field_validator("reader_identifier", mode="before")
    @classmethod
    def _readable_reader_name(cls, value: Any) -> Any:
        """Refuse a name RDS would refuse, in a sentence."""
        return readable_resource_identifier(value, "a reader name")

    @field_validator("instance_class", mode="before")
    @classmethod
    def _looks_like_an_rds_class(cls, value: Any) -> Any:
        """Refuse a class that does not start with `db.`, in a sentence; the pattern stays in the schema."""
        return _readable_instance_class(value)

    @field_validator("parameter_group_name", mode="before")
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


SECURITY_GROUP_ID_PATTERN = r"^sg-[0-9a-f]{8,17}$"
"""A VPC security group id, such as sg-0123456789abcdef0."""


def _new_identifier_field() -> Any:
    """The name of the instance or cluster a restore creates."""
    return Field(
        pattern=RESOURCE_IDENTIFIER_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "A name that starts with a lower-case letter and has only lower-case letters, digits and "
            "hyphens, such as demo-orders-restored. A cluster's first instance is named <name>-1.",
            order=1,
            label="New name",
        ),
    )


def _subnet_group_field(order: int) -> Any:
    """The subnet group the restored resource is placed in."""
    return Field(
        min_length=1,
        max_length=255,
        json_schema_extra=ui_field(
            "subnet_group",
            "The DB subnet group the restored resource is placed in, from this environment's subnet groups.",
            order=order,
            label="Subnet group",
        ),
    )


def _restore_parameter_group_field(order: int) -> Any:
    """The parameter group of the restored resource; AWS's default group when empty."""
    return Field(
        default=None,
        json_schema_extra=ui_field(
            "resource",
            "A parameter group of the family of the engine version restored: an instance group for an "
            "instance, a cluster group for an Aurora cluster. Leave empty for the AWS default group.",
            order=order,
            label="Parameter group",
            resource_kind="parameter_group",
            group_of_restored_family=True,
        ),
    )


def _security_groups_field(order: int) -> Any:
    """The VPC security groups of the restored resource; at least one."""
    return Field(
        min_length=1,
        json_schema_extra=ui_field(
            "security_groups",
            "VPC security group ids such as sg-0123456789abcdef0; at least one, in the subnet group's VPC.",
            order=order,
            label="Security groups",
            item_pattern=SECURITY_GROUP_ID_PATTERN,
        ),
    )


def _deletion_protection_field(order: int) -> Any:
    """Deletion protection on the restored resource; on unless turned off."""
    return Field(
        default=True,
        json_schema_extra=ui_field(
            "boolean",
            "Refuse deletes of the restored resource until protection is turned off.",
            order=order,
            label="Deletion protection",
        ),
    )


def _readable_security_group_ids(value: Any) -> Any:
    """Refuse an id that is not a security group id, or one given twice, in a sentence."""
    if not isinstance(value, list):
        return value
    for group_id in value:
        if not isinstance(group_id, str) or not re.match(SECURITY_GROUP_ID_PATTERN, group_id):
            raise ValueError(f"{group_id!r} is not a security group id; one looks like sg-0123456789abcdef0")
    if len(set(value)) != len(value):
        raise ValueError("each security group can be given once")
    return value


def _group_name_or_none(value: Any) -> Any:
    """An empty choice is no group; the picker's ARN becomes the group's name."""
    if not isinstance(value, str):
        return value
    if not value.strip():
        return None
    if value.startswith("arn:"):
        return value.rsplit(":", 1)[-1]
    return value


class SnapshotRestoreOptions(BaseModel):
    """Restore a snapshot to a new instance, or a cluster snapshot to a new Aurora cluster with one writer.

    Attributes:
        new_identifier: The restored instance's or cluster's identifier; a cluster's writer is `<name>-1`.
        instance_class: The restored instance's class, or the class of the cluster's writer.
        subnet_group_name: The DB subnet group the restored resource is placed in.
        parameter_group_name: An instance group (instance snapshot) or cluster group (cluster snapshot) of the
            snapshot's engine family; AWS's default when None. The picker's ARN is kept as the name.
        vpc_security_group_ids: The restored resource's VPC security groups.
        multi_az: A standby in another availability zone (instances only).
        publicly_accessible: Give the restored resource a public address.
        deletion_protection: Refuse deletes until protection is turned off.
    """

    model_config = ConfigDict(extra="forbid")
    new_identifier: str = _new_identifier_field()
    instance_class: str = _instance_class_field(2)
    subnet_group_name: str = _subnet_group_field(3)
    parameter_group_name: str | None = _restore_parameter_group_field(4)
    vpc_security_group_ids: list[str] = _security_groups_field(5)
    multi_az: bool = Field(
        default=False,
        json_schema_extra=ui_field(
            "boolean",
            "A standby in another availability zone. Instances only: an Aurora cluster adds readers instead.",
            order=6,
            label="Multi-AZ",
        ),
    )
    publicly_accessible: bool = Field(
        default=False,
        json_schema_extra=ui_field(
            "boolean",
            "Give the restored resource an address reachable from the internet.",
            order=7,
            label="Publicly accessible",
        ),
    )
    deletion_protection: bool = _deletion_protection_field(8)

    @field_validator("new_identifier", mode="before")
    @classmethod
    def _readable_new_name(cls, value: Any) -> Any:
        """Refuse a name RDS would refuse, in a sentence."""
        return readable_resource_identifier(value, "the new name")

    @field_validator("instance_class", mode="before")
    @classmethod
    def _looks_like_an_rds_class(cls, value: Any) -> Any:
        """Refuse a class that does not start with `db.`, in a sentence; the pattern stays in the schema."""
        return _readable_instance_class(value)

    @field_validator("parameter_group_name", mode="before")
    @classmethod
    def _group_name_from_name_or_arn(cls, value: Any) -> Any:
        """An empty choice is no group; the picker's ARN becomes the group's name."""
        return _group_name_or_none(value)

    @field_validator("vpc_security_group_ids", mode="before")
    @classmethod
    def _security_group_ids(cls, value: Any) -> Any:
        """Refuse an id that is not a security group id, or one given twice, in a sentence."""
        return _readable_security_group_ids(value)


class PointInTimeRestoreOptions(BaseModel):
    """Restore an instance, or an Aurora cluster with a new writer, to a point in time as a new resource.

    Attributes:
        new_identifier: The restored instance's or cluster's identifier; a cluster's writer is `<name>-1`.
        restore_time_utc: The time to restore to, ISO 8601 UTC such as 2026-10-11T10:00:00Z; None to restore
            to the latest restorable time. Any zone is accepted and kept as UTC; a time without a zone is UTC.
        use_latest_restorable_time: Restore to the latest restorable time; giving a restore time turns it off.
        instance_class: The restored instance's class, or the class of the cluster's writer.
        subnet_group_name: The DB subnet group the restored resource is placed in.
        parameter_group_name: An instance group (instance) or cluster group (cluster) of the source's engine
            family; AWS's default when None. The picker's ARN is kept as the name.
        vpc_security_group_ids: The restored resource's VPC security groups.
        deletion_protection: Refuse deletes until protection is turned off.
    """

    model_config = ConfigDict(extra="forbid")
    new_identifier: str = _new_identifier_field()
    restore_time_utc: str | None = Field(
        default=None,
        json_schema_extra=ui_field(
            "datetime",
            "A time inside the restore window, in UTC, such as 2026-10-11T10:00:00Z. Leave empty to restore "
            "to the latest restorable time.",
            order=2,
            label="Restore to",
        ),
    )
    use_latest_restorable_time: bool = Field(
        default=True,
        json_schema_extra=ui_field(
            "boolean",
            "Restore to the latest time AWS can restore to; turned off when a restore time is given.",
            order=3,
            label="Latest restorable time",
        ),
    )
    instance_class: str = _instance_class_field(4)
    subnet_group_name: str = _subnet_group_field(5)
    parameter_group_name: str | None = _restore_parameter_group_field(6)
    vpc_security_group_ids: list[str] = _security_groups_field(7)
    deletion_protection: bool = _deletion_protection_field(8)

    @model_validator(mode="before")
    @classmethod
    def _a_restore_time_means_not_the_latest(cls, data: Any) -> Any:
        """A restore time given without saying otherwise restores to that time, not the latest."""
        if not isinstance(data, dict):
            return data
        restore_time = data.get("restore_time_utc")
        if isinstance(restore_time, str) and not restore_time.strip():
            data = {**data, "restore_time_utc": None}
        elif restore_time is not None and "use_latest_restorable_time" not in data:
            data = {**data, "use_latest_restorable_time": False}
        return data

    @field_validator("restore_time_utc", mode="before")
    @classmethod
    def _readable_restore_time(cls, value: Any) -> Any:
        """Read the time as UTC and keep it as `YYYY-MM-DDTHH:MM:SSZ`; refuse anything else in a sentence."""
        if value is None:
            return None
        parsed = parse_utc(value)
        if parsed is None:
            raise ValueError("a restore time looks like 2026-10-11T10:00:00Z (UTC)")
        return format_utc(parsed)

    @model_validator(mode="after")
    def _a_time_or_the_latest(self) -> PointInTimeRestoreOptions:
        """Exactly one of a restore time and the latest restorable time."""
        if self.restore_time_utc is not None and self.use_latest_restorable_time:
            raise ValueError("choose a restore time or the latest restorable time, not both")
        if self.restore_time_utc is None and not self.use_latest_restorable_time:
            raise ValueError("choose a restore time, or restore to the latest restorable time")
        return self

    @field_validator("new_identifier", mode="before")
    @classmethod
    def _readable_new_name(cls, value: Any) -> Any:
        """Refuse a name RDS would refuse, in a sentence."""
        return readable_resource_identifier(value, "the new name")

    @field_validator("instance_class", mode="before")
    @classmethod
    def _looks_like_an_rds_class(cls, value: Any) -> Any:
        """Refuse a class that does not start with `db.`, in a sentence; the pattern stays in the schema."""
        return _readable_instance_class(value)

    @field_validator("parameter_group_name", mode="before")
    @classmethod
    def _group_name_from_name_or_arn(cls, value: Any) -> Any:
        """An empty choice is no group; the picker's ARN becomes the group's name."""
        return _group_name_or_none(value)

    @field_validator("vpc_security_group_ids", mode="before")
    @classmethod
    def _security_group_ids(cls, value: Any) -> Any:
        """Refuse an id that is not a security group id, or one given twice, in a sentence."""
        return _readable_security_group_ids(value)


ENGINE_VERSION_PATTERN = r"^\d+\.\d+(\.\d+)?$"
"""A full engine version such as 16.4; a create needs the minor version as well as the major."""
MASTER_USERNAME_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$"
"""A PostgreSQL master user name: a letter, then letters, digits and underscores, 1-63 characters."""
DATABASE_NAME_PATTERN = r"^[a-zA-Z][a-zA-Z0-9_]{0,62}$"
"""A PostgreSQL database name the create makes: a letter, then letters, digits and underscores."""
CLUSTER_MEMBER_SUFFIX_LENGTH = 2
"""Characters a cluster member's identifier adds to the cluster's (`-1` to `-6`)."""


def _target_key_field(what: str) -> Any:
    """The account and region a create puts the new resource in."""
    return Field(
        json_schema_extra=ui_field(
            "target_picker",
            f"The account and region the {what} is created in.",
            group="Where",
            order=1,
            label="Target",
        )
    )


def _created_identifier_field(example: str, label: str, extra_help: str = "") -> Any:
    """The name of the instance or cluster a create makes."""
    return Field(
        pattern=RESOURCE_IDENTIFIER_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "A name that starts with a lower-case letter and has only lower-case letters, digits and "
            f"hyphens, such as {example}.{extra_help}",
            group="Where",
            order=2,
            label=label,
        ),
    )


def _created_engine_version_field(engine: str) -> Any:
    """The engine version of a create, offered from the versions known for `engine`."""
    return Field(
        pattern=ENGINE_VERSION_PATTERN,
        json_schema_extra=ui_field(
            "engine_version",
            "A full version such as 16.4. It decides the family of the parameter group.",
            group="Engine",
            order=3,
            label="Engine version",
            engine=engine,
        ),
    )


def _created_subnet_group_field(order: int) -> Any:
    """The subnet group a create places the new resource in."""
    return Field(
        min_length=1,
        max_length=255,
        json_schema_extra=ui_field(
            "subnet_group",
            "The DB subnet group the new resource is placed in, from this environment's subnet groups.",
            group="Network",
            order=order,
            label="Subnet group",
        ),
    )


def _created_security_groups_field(order: int) -> Any:
    """The VPC security groups of a created resource; at least one."""
    return Field(
        min_length=1,
        json_schema_extra=ui_field(
            "security_groups",
            "VPC security group ids such as sg-0123456789abcdef0; at least one, in the subnet group's VPC.",
            group="Network",
            order=order,
            label="Security groups",
            item_pattern=SECURITY_GROUP_ID_PATTERN,
        ),
    )


def _master_username_field(order: int) -> Any:
    """The master user a create makes; its password is kept by Secrets Manager, never by the console."""
    return Field(
        default="postgres_admin",
        pattern=MASTER_USERNAME_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "A letter, then letters, digits and underscores, up to 63 characters. Its password is created "
            "and kept in AWS Secrets Manager; the console never sees it.",
            group="Access",
            order=order,
            label="Master user name",
        ),
    )


def _database_name_field(order: int) -> Any:
    """The database a create makes, or None for none."""
    return Field(
        default=None,
        pattern=DATABASE_NAME_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "A letter, then letters, digits and underscores, up to 63 characters. Leave empty to create "
            "no database besides postgres.",
            group="Access",
            order=order,
            label="Database name",
        ),
    )


def _port_field(order: int) -> Any:
    """The port the new resource listens on."""
    return Field(
        default=5432,
        ge=1150,
        le=65535,
        json_schema_extra=ui_field(
            "number",
            "The port PostgreSQL listens on, from 1150 to 65535.",
            group="Network",
            order=order,
            label="Port",
        ),
    )


def _readable_master_username(value: Any) -> Any:
    """Refuse a master user name PostgreSQL would refuse, in a sentence; the pattern stays in the schema."""
    if isinstance(value, str) and not re.match(MASTER_USERNAME_PATTERN, value):
        raise ValueError(
            "a master user name must start with a letter and contain only letters, digits and underscores, "
            "up to 63 characters"
        )
    return value


def _empty_is_none(value: Any) -> Any:
    """An empty text field is no value."""
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _group_name_from_arn(value: Any) -> Any:
    """The picker's ARN becomes the group's name; anything else is left to the type check."""
    if isinstance(value, str) and value.startswith("arn:"):
        return value.rsplit(":", 1)[-1]
    return value


class InstanceCreateOptions(BaseModel):
    """Create a PostgreSQL instance from the console's template.

    The template fixes what every created instance has: encrypted storage, 7 days of automated backups,
    Performance Insights, deletion protection, automatic minor upgrades, tags copied to snapshots, no enhanced
    monitoring, and a master password created and kept by AWS Secrets Manager. The options are what differs.

    Attributes:
        target_key: The account and region the instance is created in.
        identifier: The new instance's identifier.
        engine_version: The PostgreSQL version, such as 16.4.
        instance_class: The instance class.
        allocated_storage_gb: Storage in GiB.
        max_allocated_storage_gb: Storage autoscaling ceiling in GiB; 0 for no autoscaling.
        storage_type: gp3 or gp2.
        subnet_group_name: The DB subnet group the instance is placed in.
        vpc_security_group_ids: The instance's VPC security groups.
        parameter_group_name: An instance parameter group of the family `postgres<major>`; required, and not
            one of AWS's default groups. The picker's ARN is kept as the name.
        master_username: The master user; its password is kept in Secrets Manager.
        db_name: A database to create, or None.
        multi_az: A standby in another availability zone.
        publicly_accessible: Give the instance a public address.
        port: The port PostgreSQL listens on.
    """

    model_config = ConfigDict(extra="forbid")
    target_key: str = _target_key_field("instance")
    identifier: str = _created_identifier_field("demo-reports", "Instance name")
    engine_version: str = _created_engine_version_field("postgres")
    instance_class: str = Field(
        pattern=INSTANCE_CLASS_PATTERN,
        json_schema_extra=ui_field(
            "instance_class",
            "An RDS instance class such as db.m6g.large. "
            "The list shows classes AWS offers for this engine and version.",
            group="Engine",
            order=4,
            label="Instance class",
            engine="postgres",
        ),
    )
    allocated_storage_gb: int = Field(
        default=100,
        ge=20,
        le=65536,
        json_schema_extra=ui_field(
            "number",
            "Size in GiB, from 20 to 65536.",
            group="Storage",
            order=5,
            label="Allocated storage (GiB)",
        ),
    )
    max_allocated_storage_gb: int = Field(
        default=0,
        ge=0,
        le=65536,
        json_schema_extra=ui_field(
            "number",
            "Autoscaling ceiling in GiB, above the allocated storage; 0 for no autoscaling.",
            group="Storage",
            order=6,
            label="Autoscaling ceiling (GiB)",
        ),
    )
    storage_type: Literal["gp3", "gp2"] = Field(
        default="gp3",
        json_schema_extra=ui_field(
            "choice",
            "The storage type. gp3 is the default; provisioned IOPS storage is set afterwards with Change "
            "storage.",
            group="Storage",
            order=7,
            label="Storage type",
            labels={"gp3": "gp3", "gp2": "gp2"},
        ),
    )
    subnet_group_name: str = _created_subnet_group_field(8)
    vpc_security_group_ids: list[str] = _created_security_groups_field(9)
    parameter_group_name: str = Field(
        min_length=1,
        json_schema_extra=ui_field(
            "resource",
            "An instance parameter group of the chosen version's family, such as postgres16. AWS default "
            "groups cannot be changed later, so a group of your own is required.",
            group="Engine",
            order=10,
            label="Parameter group",
            resource_kind="parameter_group",
            group_scope="instance",
            group_of_chosen_version_family=True,
        ),
    )
    master_username: str = _master_username_field(11)
    db_name: str | None = _database_name_field(12)
    multi_az: bool = Field(
        default=False,
        json_schema_extra=ui_field(
            "boolean",
            "A standby in another availability zone.",
            group="Availability",
            order=13,
            label="Multi-AZ",
        ),
    )
    publicly_accessible: bool = Field(
        default=False,
        json_schema_extra=ui_field(
            "boolean",
            "Give the instance an address reachable from the internet.",
            group="Network",
            order=14,
            label="Publicly accessible",
        ),
    )
    port: int = _port_field(15)

    @field_validator("identifier", mode="before")
    @classmethod
    def _readable_name(cls, value: Any) -> Any:
        """Refuse a name RDS would refuse, in a sentence."""
        return readable_resource_identifier(value, "an instance name")

    @field_validator("instance_class", mode="before")
    @classmethod
    def _looks_like_an_rds_class(cls, value: Any) -> Any:
        """Refuse a class that does not start with `db.`, in a sentence; the pattern stays in the schema."""
        return _readable_instance_class(value)

    @field_validator("parameter_group_name", mode="before")
    @classmethod
    def _group_name_from_name_or_arn(cls, value: Any) -> Any:
        """The picker's ARN becomes the group's name."""
        return _group_name_from_arn(value)

    @field_validator("master_username", mode="before")
    @classmethod
    def _readable_master_user(cls, value: Any) -> Any:
        """Refuse a master user name PostgreSQL would refuse, in a sentence."""
        return _readable_master_username(value)

    @field_validator("db_name", mode="before")
    @classmethod
    def _no_database_when_empty(cls, value: Any) -> Any:
        """An empty name creates no database."""
        return _empty_is_none(value)

    @field_validator("vpc_security_group_ids", mode="before")
    @classmethod
    def _security_group_ids(cls, value: Any) -> Any:
        """Refuse an id that is not a security group id, or one given twice, in a sentence."""
        return _readable_security_group_ids(value)

    @model_validator(mode="after")
    def _ceiling_above_the_storage(self) -> InstanceCreateOptions:
        """An autoscaling ceiling, when given, is above the allocated storage."""
        if 0 < self.max_allocated_storage_gb <= self.allocated_storage_gb:
            raise ValueError(
                f"the autoscaling ceiling must be above the allocated storage of {self.allocated_storage_gb} "
                "GiB, or 0 for no autoscaling"
            )
        return self


class ClusterCreateOptions(BaseModel):
    """Create an Aurora PostgreSQL cluster with a writer and readers from the console's template.

    The template fixes encrypted storage, 7 days of automated backups, deletion protection, Performance
    Insights on every member, and a master password created and kept by AWS Secrets Manager; the cluster is
    provisioned (not Serverless v2). The members are `<identifier>-1` (the writer), then `-2` and on.

    Attributes:
        target_key: The account and region the cluster is created in.
        identifier: The new cluster's identifier; its members are `<identifier>-1` and on.
        engine_version: The Aurora PostgreSQL version, such as 16.4.
        subnet_group_name: The DB subnet group the cluster is placed in.
        vpc_security_group_ids: The cluster's VPC security groups.
        cluster_parameter_group_name: A cluster parameter group of the family `aurora-postgresql<major>`;
            required, and not one of AWS's default groups. The picker's ARN is kept as the name.
        master_username: The master user; its password is kept in Secrets Manager.
        database_name: A database to create, or None.
        port: The port the cluster listens on.
        writer_instance_class: The writer's instance class.
        reader_count: Readers to create besides the writer, 0 to 5.
        reader_instance_class: The readers' class; the writer's when None.
    """

    model_config = ConfigDict(extra="forbid")
    target_key: str = _target_key_field("cluster")
    identifier: str = _created_identifier_field(
        "demo-analytics", "Cluster name", " Its instances are named <name>-1 (the writer), <name>-2 and on."
    )
    engine_version: str = _created_engine_version_field("aurora-postgresql")
    subnet_group_name: str = _created_subnet_group_field(4)
    vpc_security_group_ids: list[str] = _created_security_groups_field(5)
    cluster_parameter_group_name: str = Field(
        min_length=1,
        json_schema_extra=ui_field(
            "resource",
            "A cluster parameter group of the chosen version's family, such as aurora-postgresql16. AWS "
            "default groups cannot be changed later, so a group of your own is required.",
            group="Engine",
            order=6,
            label="Cluster parameter group",
            resource_kind="parameter_group",
            group_scope="cluster",
            group_of_chosen_version_family=True,
        ),
    )
    master_username: str = _master_username_field(7)
    database_name: str | None = _database_name_field(8)
    port: int = _port_field(9)
    writer_instance_class: str = Field(
        pattern=INSTANCE_CLASS_PATTERN,
        json_schema_extra=ui_field(
            "instance_class",
            "The writer's instance class, such as db.r6g.large. "
            "The list shows classes AWS offers for this engine and version.",
            group="Instances",
            order=10,
            label="Writer instance class",
            engine="aurora-postgresql",
        ),
    )
    reader_count: int = Field(
        default=1,
        ge=0,
        le=5,
        json_schema_extra=ui_field(
            "number",
            "Readers to create besides the writer, from 0 to 5. A reader is also the failover target.",
            group="Instances",
            order=11,
            label="Readers",
        ),
    )
    reader_instance_class: str | None = Field(
        default=None,
        pattern=INSTANCE_CLASS_PATTERN,
        json_schema_extra=ui_field(
            "instance_class",
            "The readers' instance class; leave empty to use the writer's.",
            group="Instances",
            order=12,
            label="Reader instance class",
            engine="aurora-postgresql",
        ),
    )

    @field_validator("identifier", mode="before")
    @classmethod
    def _readable_name(cls, value: Any) -> Any:
        """Refuse a name RDS would refuse, or one too long for its members' names, in a sentence."""
        value = readable_resource_identifier(value, "a cluster name")
        if isinstance(value, str) and len(value) > 63 - CLUSTER_MEMBER_SUFFIX_LENGTH:
            raise ValueError(
                f"a cluster name has at most {63 - CLUSTER_MEMBER_SUFFIX_LENGTH} characters, so the names "
                "of its instances (<name>-1 and on) fit in 63"
            )
        return value

    @field_validator("writer_instance_class", "reader_instance_class", mode="before")
    @classmethod
    def _looks_like_an_rds_class(cls, value: Any) -> Any:
        """Refuse a class that does not start with `db.`, in a sentence; an empty reader class is None."""
        return _readable_instance_class(_empty_is_none(value))

    @field_validator("cluster_parameter_group_name", mode="before")
    @classmethod
    def _group_name_from_name_or_arn(cls, value: Any) -> Any:
        """The picker's ARN becomes the group's name."""
        return _group_name_from_arn(value)

    @field_validator("master_username", mode="before")
    @classmethod
    def _readable_master_user(cls, value: Any) -> Any:
        """Refuse a master user name PostgreSQL would refuse, in a sentence."""
        return _readable_master_username(value)

    @field_validator("database_name", mode="before")
    @classmethod
    def _no_database_when_empty(cls, value: Any) -> Any:
        """An empty name creates no database."""
        return _empty_is_none(value)

    @field_validator("vpc_security_group_ids", mode="before")
    @classmethod
    def _security_group_ids(cls, value: Any) -> Any:
        """Refuse an id that is not a security group id, or one given twice, in a sentence."""
        return _readable_security_group_ids(value)

    def member_identifiers(self) -> list[str]:
        """The members to create, writer first: `<identifier>-1` to `<identifier>-<1 + reader_count>`.

        Example:
            >>> options = ClusterCreateOptions(
            ...     target_key="t", identifier="demo-analytics", engine_version="16.4",
            ...     subnet_group_name="s", vpc_security_group_ids=["sg-0123456789abcdef0"],
            ...     cluster_parameter_group_name="g", writer_instance_class="db.r6g.large",
            ... )
            >>> options.member_identifiers()
            ['demo-analytics-1', 'demo-analytics-2']
        """
        return [f"{self.identifier}-{position}" for position in range(1, self.reader_count + 2)]


class DecommissionOptions(BaseModel):
    """Decommission an instance, or a cluster with its members, after a final snapshot.

    Attributes:
        final_snapshot_identifier: Name of the snapshot AWS takes before it deletes the resource; it must not
            exist yet. None for a read replica, which is deleted without a snapshot; the executor refuses
            anything else without a name before approval.
        confirm_dependants_removed: The operator's statement that nothing outside AWS (applications, jobs,
            DNS) still uses the database; the console cannot see those, so it records the statement only.
    """

    model_config = ConfigDict(extra="forbid")
    final_snapshot_identifier: str | None = Field(
        default=None,
        pattern=SNAPSHOT_IDENTIFIER_PATTERN,
        json_schema_extra=ui_field(
            "text",
            "A name that starts with a lower-case letter and has only lower-case letters, digits and "
            "hyphens, such as demo-orders-final. AWS takes this snapshot before it deletes the database. "
            "Leave it empty for a read replica, which is deleted without a snapshot.",
            order=1,
            label="Final snapshot name",
        ),
    )
    confirm_dependants_removed: bool = Field(
        default=False,
        json_schema_extra=ui_field(
            "boolean",
            "Nothing outside AWS (applications, jobs, DNS names) still uses this database. The console "
            "cannot check this; replicas and cluster members it can see are checked before approval.",
            order=2,
            label="Nothing else uses it",
        ),
    )

    @field_validator("final_snapshot_identifier", mode="before")
    @classmethod
    def _readable_final_snapshot_name(cls, value: Any) -> Any:
        """Refuse a name RDS would refuse, in a sentence (`readable_snapshot_name`); an empty name is None."""
        if value is None or value == "":
            return None
        return readable_snapshot_name(value)
