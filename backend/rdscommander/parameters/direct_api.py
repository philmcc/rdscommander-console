"""Direct AWS API calls for parameter groups, reboots and the restore of recorded attribute values.

Restoring a drifted resource in a direct-mode environment puts the recorded values back through the RDS
API. Attributes are named the way the record and drift findings name them: `parameters.<name>` for the
user-set parameters of a parameter group, `tags.<key>` for tags and the renderer's own attribute names
(`instance_class`, `backup_retention_period`, ...) for the rest. Anything the console cannot write back is
refused with `UnsupportedRestoreError` before a single call is made.

The parameter group operations (create, edit, attach, delete, reboot) use the group functions further
down: `group_parameters`, `modify_group`, `reset_group`, `create_group`, `delete_group`, `group_exists`,
`attach_group`, `attached_group`, `reboot_instance` and `instance_status`. A group's scope is `instance`
or `cluster`; every call is recorded under the step name it is given.

Example:
    >>> apply_attribute_values(  # doctest: +SKIP
    ...     context, "aws_db_parameter_group.demo_postgres16_tuned", {"parameters.work_mem": "16384"}
    ... )
    {'parameters.work_mem': '16384'}
    >>> read_attribute_values(  # doctest: +SKIP
    ...     context, "aws_db_parameter_group.demo_postgres16_tuned", ["parameters.work_mem"]
    ... )
    {'parameters.work_mem': '16384'}
    >>> modify_group(  # doctest: +SKIP
    ...     context, "demo-postgres16-tuned", "instance", {"work_mem": "65536"}, family_metadata
    ... )
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from botocore.client import BaseClient

from rdscommander.aws.rds_reader import UNSUPPORTED_MARKER, RdsReader, SimulatorUnsupportedAction
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ApplyType, ChangeDiagnosticError
from rdscommander.operations.executor import StepContext
from rdscommander.parameters.metadata import FamilyMetadata
from rdscommander.terraform.hcl import to_hcl
from rdscommander.terraform.record import read_record
from rdscommander.terraform.render_input import RenderedParameterGroup, RenderWorkspaceInput
from rdscommander.terraform.workspaces import ensure_workspace_record, workspace_directory

logger = get_logger(__name__)

PARAMETER_ATTRIBUTE_PREFIX = "parameters."
TAG_ATTRIBUTE_PREFIX = "tags."
PARAMETER_GROUP_TYPES = ("aws_db_parameter_group", "aws_rds_cluster_parameter_group")
MAXIMUM_PARAMETERS_PER_REQUEST = 20
"""RDS accepts at most this many parameters in one modify request."""
GROUP_REFERENCE_PATTERN = re.compile(
    r"^(aws_db_parameter_group|aws_rds_cluster_parameter_group)\.([a-z0-9_]+)\.name$"
)


class UnsupportedRestoreError(ChangeDiagnosticError):
    """The console cannot write this attribute back to AWS.

    Attributes:
        attribute: The attribute that cannot be restored, e.g. `publicly_accessible`.
    """

    diagnostic_kind = "restore_not_supported"
    title = "The console cannot write this attribute back"
    fix_suggestion = "accept this drift into the record or change the attribute by hand"

    def __init__(self, attribute: str) -> None:
        """Remember which attribute was refused."""
        self.attribute = attribute
        super().__init__(f"{attribute} cannot be written back to AWS by the console")


@dataclass(frozen=True)
class _ModifiableAttribute:
    """How one record attribute maps onto the modify request and the describe response.

    A dotted field (`Parent.Child`) is nested: it is written as `{"Parent": {"Child": value}}`, merged with
    its siblings into one dict, and read from `response["Parent"]["Child"]`.
    """

    request_field: str
    description_field: str


_INSTANCE_CLASS = _ModifiableAttribute("DBInstanceClass", "DBInstanceClass")
_BACKUP_RETENTION = _ModifiableAttribute("BackupRetentionPeriod", "BackupRetentionPeriod")
_BACKUP_WINDOW = _ModifiableAttribute("PreferredBackupWindow", "PreferredBackupWindow")
_MAINTENANCE_WINDOW = _ModifiableAttribute("PreferredMaintenanceWindow", "PreferredMaintenanceWindow")
_DELETION_PROTECTION = _ModifiableAttribute("DeletionProtection", "DeletionProtection")
_AUTO_MINOR_UPGRADE = _ModifiableAttribute("AutoMinorVersionUpgrade", "AutoMinorVersionUpgrade")
_ENGINE_VERSION = _ModifiableAttribute("EngineVersion", "EngineVersion")
"""Written on standalone instances and clusters only: an Aurora member runs the version of its cluster."""
_PERFORMANCE_INSIGHTS = _ModifiableAttribute("EnablePerformanceInsights", "PerformanceInsightsEnabled")
_MONITORING_INTERVAL = _ModifiableAttribute("MonitoringInterval", "MonitoringInterval")
_COPY_TAGS = _ModifiableAttribute("CopyTagsToSnapshot", "CopyTagsToSnapshot")
_STORAGE_TYPE = _ModifiableAttribute("StorageType", "StorageType")
_IOPS = _ModifiableAttribute("Iops", "Iops")
_STORAGE_THROUGHPUT = _ModifiableAttribute("StorageThroughput", "StorageThroughput")
_PI_RETENTION = _ModifiableAttribute(
    "PerformanceInsightsRetentionPeriod", "PerformanceInsightsRetentionPeriod"
)
_MONITORING_ROLE = _ModifiableAttribute("MonitoringRoleArn", "MonitoringRoleArn")
_PROMOTION_TIER = _ModifiableAttribute("PromotionTier", "PromotionTier")
_SERVERLESS_MIN = _ModifiableAttribute(
    "ServerlessV2ScalingConfiguration.MinCapacity", "ServerlessV2ScalingConfiguration.MinCapacity"
)
_SERVERLESS_MAX = _ModifiableAttribute(
    "ServerlessV2ScalingConfiguration.MaxCapacity", "ServerlessV2ScalingConfiguration.MaxCapacity"
)
PENDING_WINDOW_DOWNTIME = "none now; AWS applies it in the next maintenance window"
"""The downtime text of a change AWS holds until the next maintenance window."""

INSTANCE_ATTRIBUTES: dict[str, _ModifiableAttribute] = {
    "instance_class": _INSTANCE_CLASS,
    "allocated_storage": _ModifiableAttribute("AllocatedStorage", "AllocatedStorage"),
    "max_allocated_storage": _ModifiableAttribute("MaxAllocatedStorage", "MaxAllocatedStorage"),
    "backup_retention_period": _BACKUP_RETENTION,
    "preferred_backup_window": _BACKUP_WINDOW,
    "preferred_maintenance_window": _MAINTENANCE_WINDOW,
    "deletion_protection": _DELETION_PROTECTION,
    "multi_az": _ModifiableAttribute("MultiAZ", "MultiAZ"),
    "auto_minor_version_upgrade": _AUTO_MINOR_UPGRADE,
    "performance_insights_enabled": _PERFORMANCE_INSIGHTS,
    "monitoring_interval": _MONITORING_INTERVAL,
    "copy_tags_to_snapshot": _COPY_TAGS,
    "storage_type": _STORAGE_TYPE,
    "iops": _IOPS,
    "storage_throughput": _STORAGE_THROUGHPUT,
    "performance_insights_retention_period": _PI_RETENTION,
    "monitoring_role_arn": _MONITORING_ROLE,
    "engine_version": _ENGINE_VERSION,
}
CLUSTER_INSTANCE_ATTRIBUTES: dict[str, _ModifiableAttribute] = {
    "instance_class": _INSTANCE_CLASS,
    "auto_minor_version_upgrade": _AUTO_MINOR_UPGRADE,
    "performance_insights_enabled": _PERFORMANCE_INSIGHTS,
    "monitoring_interval": _MONITORING_INTERVAL,
    "promotion_tier": _PROMOTION_TIER,
    "performance_insights_retention_period": _PI_RETENTION,
    "monitoring_role_arn": _MONITORING_ROLE,
    "preferred_maintenance_window": _MAINTENANCE_WINDOW,
}
CLUSTER_ATTRIBUTES: dict[str, _ModifiableAttribute] = {
    "backup_retention_period": _BACKUP_RETENTION,
    "preferred_backup_window": _BACKUP_WINDOW,
    "preferred_maintenance_window": _MAINTENANCE_WINDOW,
    "deletion_protection": _DELETION_PROTECTION,
    "copy_tags_to_snapshot": _COPY_TAGS,
    "serverless_v2_min_capacity": _SERVERLESS_MIN,
    "serverless_v2_max_capacity": _SERVERLESS_MAX,
    "auto_minor_version_upgrade": _AUTO_MINOR_UPGRADE,
    "engine_version": _ENGINE_VERSION,
}
MODIFIABLE_ATTRIBUTES_BY_TYPE: dict[str, dict[str, _ModifiableAttribute]] = {
    "aws_db_instance": INSTANCE_ATTRIBUTES,
    "aws_rds_cluster_instance": CLUSTER_INSTANCE_ATTRIBUTES,
    "aws_rds_cluster": CLUSTER_ATTRIBUTES,
}
GROUP_REFERENCE_ATTRIBUTE_BY_TYPE = {
    "aws_db_instance": "parameter_group_reference",
    "aws_rds_cluster_instance": "parameter_group_reference",
    "aws_rds_cluster": "cluster_parameter_group_reference",
}
GROUP_TYPE_BY_RESOURCE_TYPE = {
    "aws_db_instance": "aws_db_parameter_group",
    "aws_rds_cluster_instance": "aws_db_parameter_group",
    "aws_rds_cluster": "aws_rds_cluster_parameter_group",
}
TERMINAL_RESOURCE_STATUSES = frozenset(
    {
        "failed",
        "incompatible-parameters",
        "incompatible-network",
        "incompatible-option-group",
        "incompatible-restore",
        "inaccessible-encryption-credentials",
        "storage-full",
        "deleting",
        "deleted",
    }
)
"""Instance and cluster statuses a modification never recovers from by waiting."""


@dataclass(frozen=True)
class AttributeApplyProfile:
    """How AWS applies a change to one attribute, as the approver should read it.

    Attributes:
        apply_type: `immediate`, `pending-reboot` or `pending-window`.
        requires_reboot: Whether the change only takes effect after a reboot.
        downtime: What the approver should expect; `none` when nothing is interrupted.
        honours_apply_immediately: Whether AWS can hold the change for the maintenance window when
            `ApplyImmediately` is false. False for tags and for settings AWS applies at once whatever
            the flag says (the ones that never appear in `PendingModifiedValues`).
    """

    apply_type: ApplyType
    requires_reboot: bool
    downtime: str
    honours_apply_immediately: bool = True


NO_DOWNTIME = "none"
"""The downtime text of a change that interrupts nothing; a qualified form reads `none (...)`."""
_IMMEDIATE_WITHOUT_DOWNTIME = AttributeApplyProfile("immediate", False, NO_DOWNTIME)
_APPLIED_AT_ONCE_WITHOUT_DOWNTIME = AttributeApplyProfile(
    "immediate", False, NO_DOWNTIME, honours_apply_immediately=False
)
"""A setting AWS applies at once whatever `ApplyImmediately` says: it never appears in
`PendingModifiedValues` (the DescribeDBInstances/DescribeDBClusters list of held modifications)."""
_GROUP_ASSIGNMENT_PROFILE = AttributeApplyProfile(
    "pending-reboot", True, "a reboot of the instance applies the new parameter group"
)
ATTRIBUTE_APPLY_PROFILES: dict[str, AttributeApplyProfile] = {
    "instance_class": AttributeApplyProfile(
        "immediate", False, "a restart of the instance (Multi-AZ fails over to the standby)"
    ),
    "allocated_storage": AttributeApplyProfile(
        "immediate", False, "none (storage optimisation runs in the background)"
    ),
    "max_allocated_storage": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "multi_az": AttributeApplyProfile("immediate", False, "none (conversion runs in the background)"),
    "backup_retention_period": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "preferred_backup_window": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "preferred_maintenance_window": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "deletion_protection": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "auto_minor_version_upgrade": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "performance_insights_enabled": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "monitoring_interval": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "copy_tags_to_snapshot": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "parameter_group_reference": _GROUP_ASSIGNMENT_PROFILE,
    "cluster_parameter_group_reference": _GROUP_ASSIGNMENT_PROFILE,
    "storage_type": AttributeApplyProfile(
        "immediate", False, "none (storage conversion runs in the background)"
    ),
    "iops": AttributeApplyProfile("immediate", False, "none (storage optimisation runs in the background)"),
    "storage_throughput": AttributeApplyProfile(
        "immediate", False, "none (storage optimisation runs in the background)"
    ),
    "performance_insights_retention_period": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "monitoring_role_arn": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "promotion_tier": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "serverless_v2_min_capacity": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "serverless_v2_max_capacity": _APPLIED_AT_ONCE_WITHOUT_DOWNTIME,
    "engine_version": AttributeApplyProfile(
        "immediate", False, "a restart of the instance (Multi-AZ fails over)"
    ),
}
"""Per instance and cluster attribute the restore can write: how AWS applies it and what the approver
should expect. Tags (`tags.<key>`) are immediate without downtime; parameters have their own profile."""
BACKUPS_TURNED_ON_OR_OFF_DOWNTIME = "a brief outage while automated backups are turned on or off"
_BACKUPS_TURNED_ON_OR_OFF = AttributeApplyProfile("immediate", False, BACKUPS_TURNED_ON_OR_OFF_DOWNTIME)
"""Retention between 0 and a non-zero value: AWS holds it for the window unless `ApplyImmediately`, and
applying it can interrupt the database."""


def backup_retention_apply_profile(current: Any, desired: Any) -> AttributeApplyProfile:
    """How AWS applies a backup retention change, which depends on the values on both sides.

    A change from one non-zero retention to another is applied as soon as possible whatever
    `ApplyImmediately` says. Turning automated backups on (0 to non-zero) or off (non-zero to 0) is held for
    the maintenance window unless `ApplyImmediately`, and can cause an outage. An unknown current value
    (None) counts as non-zero, so only a desired 0 is taken as turning backups off.

    Args:
        current: The retention AWS has now, in days, or None when it is not known.
        desired: The retention asked for, in days.

    Returns:
        The profile for this particular change.

    Example:
        >>> backup_retention_apply_profile(1, 14).honours_apply_immediately
        False
        >>> backup_retention_apply_profile(0, 7).downtime
        'a brief outage while automated backups are turned on or off'
    """
    turning_off = desired == 0 and current != 0
    turning_on = current == 0 and desired not in (0, None)
    return _BACKUPS_TURNED_ON_OR_OFF if turning_off or turning_on else _APPLIED_AT_ONCE_WITHOUT_DOWNTIME


def apply_profile_for_values(
    attribute: str, profile: AttributeApplyProfile, current: Any, desired: Any
) -> AttributeApplyProfile:
    """`profile` refined by the values of this change, for the attributes whose timing depends on them.

    Example:
        >>> apply_profile_for_values("backup_retention_period", profile, 0, 7).honours_apply_immediately
        True
    """
    if attribute == "backup_retention_period":
        return backup_retention_apply_profile(current, desired)
    return profile


NEVER_RESTORED_ATTRIBUTES: frozenset[str] = frozenset({"engine_version"})
"""Writable attributes a drift restore never writes back: AWS upgrades an engine version but never takes it
back down, so restoring a recorded older version can only be refused by AWS after approval."""
TAG_APPLY_PROFILE = _APPLIED_AT_ONCE_WITHOUT_DOWNTIME
STATIC_PARAMETER_APPLY_PROFILE = AttributeApplyProfile(
    "pending-reboot", True, "a reboot of every instance using the group applies the restored values"
)
"""A static parameter, or one the family metadata does not know: AWS applies it at the next reboot."""
DYNAMIC_PARAMETER_APPLY_PROFILE = _IMMEDIATE_WITHOUT_DOWNTIME
"""A dynamic parameter: AWS applies it at once, without a reboot."""


def _resource_type(address: str) -> str:
    return address.partition(".")[0]


def _is_parameter_group(address: str) -> bool:
    return _resource_type(address) in PARAMETER_GROUP_TYPES


def _is_restorable(address: str, attribute: str) -> bool:
    """Whether `attribute` of the resource at `address` is one the console writes back."""
    resource_type = _resource_type(address)
    if resource_type in PARAMETER_GROUP_TYPES:
        return any(
            attribute.startswith(prefix) and len(attribute) > len(prefix)
            for prefix in (PARAMETER_ATTRIBUTE_PREFIX, TAG_ATTRIBUTE_PREFIX)
        )
    if resource_type not in MODIFIABLE_ATTRIBUTES_BY_TYPE:
        return False
    if attribute.startswith(TAG_ATTRIBUTE_PREFIX):
        return len(attribute) > len(TAG_ATTRIBUTE_PREFIX)
    return (
        attribute in MODIFIABLE_ATTRIBUTES_BY_TYPE[resource_type]
        or attribute == GROUP_REFERENCE_ATTRIBUTE_BY_TYPE[resource_type]
    )


def ensure_attributes_restorable(address: str, attributes: list[str]) -> None:
    """Refuse, before any AWS call, an attribute list the console cannot write back.

    Args:
        address: Terraform address of the resource, e.g. `aws_db_instance.demo_orders`.
        attributes: Attribute names as drift findings use them.

    Raises:
        UnsupportedRestoreError: At least one attribute cannot be restored; names the first.

    Example:
        >>> ensure_attributes_restorable("aws_db_instance.demo_orders", ["instance_class", "tags.team"])
        >>> ensure_attributes_restorable("aws_db_instance.demo_orders", ["publicly_accessible"])
        Traceback (most recent call last):
        ...
        rdscommander.parameters.direct_api.UnsupportedRestoreError: publicly_accessible cannot be ...
    """
    for attribute in attributes:
        if not _is_restorable(address, attribute):
            logger.error(
                "restore_attribute_unsupported",
                address=address,
                attribute=attribute,
                fix_suggestion="accept this drift into the record or change the attribute by hand",
            )
            raise UnsupportedRestoreError(attribute)


def ensure_values_restorable(address: str, values: dict[str, Any]) -> None:
    """Refuse, before any AWS call, values the console cannot write back.

    On top of `ensure_attributes_restorable`, a parameter or attribute whose value is None would have to
    be reset to an engine default or removed, which the console does not do yet; only a tag can be
    removed.

    Args:
        address: Terraform address of the resource.
        values: `{attribute: value}` as they would be written.

    Raises:
        UnsupportedRestoreError: An attribute cannot be restored, or would be reset to nothing.

    Example:
        >>> ensure_values_restorable("aws_db_instance.demo_orders", {"tags.team": None})
    """
    ensure_attributes_restorable(address, list(values))
    for attribute, value in values.items():
        if value is None and not attribute.startswith(TAG_ATTRIBUTE_PREFIX):
            logger.error(
                "restore_reset_unsupported",
                address=address,
                attribute=attribute,
                fix_suggestion="accept this drift into the record or change the attribute by hand",
            )
            raise UnsupportedRestoreError(attribute)


def _rds_client(context: StepContext, step_name: str) -> BaseClient:
    return context.services.aws_sessions.client(
        context.target, "rds", operation_id=context.operation.id, step_name=step_name
    )


def _physical_identifier(context: StepContext) -> str:
    """The AWS identifier of the resource the operation acts on, from its ARN."""
    return (context.operation.resource_arn or "").rsplit(":", 1)[-1]


def recorded_render_input(context: StepContext) -> RenderWorkspaceInput | None:
    """The committed record of the operation's target, or None when nothing has been recorded.

    Args:
        context: The running step's context.

    Returns:
        The renderer input the committed Terraform files were produced from.

    Raises:
        ValueError: The record file exists but cannot be read.

    Example:
        >>> recorded_render_input(context)  # doctest: +SKIP
        RenderWorkspaceInput(target_key='sandbox/000000000000/eu-west-2', ...)
    """
    workspace = ensure_workspace_record(context.session, context.target, context.console_config)
    recorded = read_record(workspace_directory(context.settings.repo_root, workspace))
    return recorded.render_input if recorded is not None else None


def _recorded_groups(recorded: RenderWorkspaceInput | None, group_type: str) -> list[RenderedParameterGroup]:
    if recorded is None:
        return []
    return (
        recorded.instance_parameter_groups
        if group_type == "aws_db_parameter_group"
        else recorded.cluster_parameter_groups
    )


def _group_name_from_reference(
    context: StepContext, resource_type: str, attribute: str, reference: Any
) -> str:
    """The AWS name of the parameter group a recorded reference points at.

    A reference is either a quoted literal (`"default.postgres16"`) or the name of a group in the same
    workspace (`aws_db_parameter_group.demo_postgres16_tuned.name`), which the record resolves.
    """
    if not isinstance(reference, str):
        raise UnsupportedRestoreError(attribute)
    if reference.startswith('"'):
        try:
            return str(json.loads(reference))
        except ValueError as error:
            raise UnsupportedRestoreError(attribute) from error
    match = GROUP_REFERENCE_PATTERN.match(reference)
    if match is None or match.group(1) != GROUP_TYPE_BY_RESOURCE_TYPE[resource_type]:
        raise UnsupportedRestoreError(attribute)
    group = next(
        (
            g
            for g in _recorded_groups(recorded_render_input(context), match.group(1))
            if g.terraform_name == match.group(2)
        ),
        None,
    )
    if group is None:
        raise UnsupportedRestoreError(attribute)
    return group.name


def _reference_from_group_name(
    context: StepContext, resource_type: str, group_name: str | None
) -> str | None:
    """A parameter group's name in the form the record uses: a reference when the record holds the group."""
    if group_name is None:
        return None
    group_type = GROUP_TYPE_BY_RESOURCE_TYPE[resource_type]
    group = next(
        (g for g in _recorded_groups(recorded_render_input(context), group_type) if g.name == group_name),
        None,
    )
    return f"{group_type}.{group.terraform_name}.name" if group is not None else to_hcl(group_name)


def _chunks(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return [
        items[start : start + MAXIMUM_PARAMETERS_PER_REQUEST]
        for start in range(0, len(items), MAXIMUM_PARAMETERS_PER_REQUEST)
    ]


def _apply_tag_values(client: BaseClient, resource_arn: str, values: dict[str, Any]) -> None:
    """Set the `tags.<key>` entries of `values` on the resource, and remove those whose value is None."""
    tags_to_set = [
        {"Key": attribute.removeprefix(TAG_ATTRIBUTE_PREFIX), "Value": str(value)}
        for attribute, value in values.items()
        if attribute.startswith(TAG_ATTRIBUTE_PREFIX) and value is not None
    ]
    tag_keys_to_remove = [
        attribute.removeprefix(TAG_ATTRIBUTE_PREFIX)
        for attribute, value in values.items()
        if attribute.startswith(TAG_ATTRIBUTE_PREFIX) and value is None
    ]
    if tags_to_set:
        client.add_tags_to_resource(ResourceName=resource_arn, Tags=tags_to_set)
    if tag_keys_to_remove:
        client.remove_tags_from_resource(ResourceName=resource_arn, TagKeys=tag_keys_to_remove)


def _apply_parameter_values(
    client: BaseClient,
    address: str,
    group_name: str,
    values: dict[str, Any],
    metadata: FamilyMetadata | None,
    resource_arn: str = "",
) -> None:
    _apply_tag_values(client, resource_arn, values)
    values = {a: v for a, v in values.items() if a.startswith(PARAMETER_ATTRIBUTE_PREFIX)}
    parameters = [
        {
            "ParameterName": name,
            "ParameterValue": str(value),
            "ApplyMethod": _apply_method_for(name, metadata),
        }
        for name, value in (
            (attribute.removeprefix(PARAMETER_ATTRIBUTE_PREFIX), value) for attribute, value in values.items()
        )
    ]
    for chunk in _chunks(parameters):
        if _resource_type(address) == "aws_db_parameter_group":
            client.modify_db_parameter_group(DBParameterGroupName=group_name, Parameters=chunk)
        else:
            client.modify_db_cluster_parameter_group(DBClusterParameterGroupName=group_name, Parameters=chunk)


def _put_request_field(request: dict[str, Any], request_field: str, value: Any) -> None:
    """Set `request_field` in `request`; a dotted field joins its siblings in one nested dict.

    Example:
        >>> request: dict[str, Any] = {}
        >>> _put_request_field(request, "ServerlessV2ScalingConfiguration.MinCapacity", 0.5)
        >>> _put_request_field(request, "ServerlessV2ScalingConfiguration.MaxCapacity", 4.0)
        >>> request
        {'ServerlessV2ScalingConfiguration': {'MinCapacity': 0.5, 'MaxCapacity': 4.0}}
    """
    parent, separator, child = request_field.partition(".")
    if separator:
        request.setdefault(parent, {})[child] = value
    else:
        request[request_field] = value


def _described_field(description: dict[str, Any], description_field: str) -> Any:
    """Read `description_field` from a describe response; a dotted field reads from the nested dict.

    Example:
        >>> _described_field({"ServerlessV2ScalingConfiguration": {"MinCapacity": 0.5}},
        ...                  "ServerlessV2ScalingConfiguration.MinCapacity")
        0.5
    """
    parent, separator, child = description_field.partition(".")
    if not separator:
        return description.get(description_field)
    return (description.get(parent) or {}).get(child)


def resource_modify_request(resource_type: str, values: dict[str, Any]) -> dict[str, Any]:
    """The `ModifyDBInstance` / `ModifyDBCluster` fields for attribute values (tags and group aside).

    Args:
        resource_type: `aws_db_instance`, `aws_rds_cluster_instance` or `aws_rds_cluster`.
        values: `{attribute: value}` of modifiable attributes; `tags.*` and the group reference are skipped
            (`apply_attribute_values` sends those its own way).

    Returns:
        The request fields, such as `{"EnablePerformanceInsights": True}`.

    Raises:
        KeyError: An attribute is not modifiable on `resource_type`.

    Example:
        >>> resource_modify_request("aws_db_instance", {"performance_insights_enabled": True})
        {'EnablePerformanceInsights': True}
    """
    modifiable = MODIFIABLE_ATTRIBUTES_BY_TYPE[resource_type]
    reference_attribute = GROUP_REFERENCE_ATTRIBUTE_BY_TYPE[resource_type]
    request: dict[str, Any] = {}
    for attribute, value in values.items():
        if attribute.startswith(TAG_ATTRIBUTE_PREFIX) or attribute == reference_attribute:
            continue
        _put_request_field(request, modifiable[attribute].request_field, value)
    return request


def _apply_resource_values(
    context: StepContext,
    client: BaseClient,
    address: str,
    identifier: str,
    values: dict[str, Any],
    apply_immediately: bool = True,
    resource_arn: str = "",
) -> None:
    resource_type = _resource_type(address)
    reference_attribute = GROUP_REFERENCE_ATTRIBUTE_BY_TYPE[resource_type]
    request = resource_modify_request(resource_type, values)
    if reference_attribute in values:
        group_name = _group_name_from_reference(
            context, resource_type, reference_attribute, values[reference_attribute]
        )
        cluster = resource_type == "aws_rds_cluster"
        request["DBClusterParameterGroupName" if cluster else "DBParameterGroupName"] = group_name
    if request:
        if resource_type == "aws_rds_cluster":
            client.modify_db_cluster(
                DBClusterIdentifier=identifier, ApplyImmediately=apply_immediately, **request
            )
        else:
            client.modify_db_instance(
                DBInstanceIdentifier=identifier, ApplyImmediately=apply_immediately, **request
            )
    _apply_tag_values(client, resource_arn, values)


def apply_attribute_values(
    context: StepContext,
    address: str,
    values: dict[str, Any],
    step_name: str = "apply_direct",
    metadata: FamilyMetadata | None = None,
    apply_immediately: bool = True,
    resource_arn: str | None = None,
) -> dict[str, Any]:
    """Write attribute values to the resource the operation acts on, or to `resource_arn` when given.

    Parameters go in batches of at most 20, each with the apply method its family requires (at once when
    dynamic, at the next reboot when static or unknown); instance and cluster attributes are applied
    immediately.

    Args:
        context: The running step's context; `context.operation.resource_arn` names the resource.
        address: Terraform address of the resource; its type picks the API.
        values: `{attribute: value}`; a tag with value None is removed.
        step_name: Step the AWS calls are recorded under.
        metadata: The parameter group's family, to apply each parameter as the family requires; without it
            every parameter is applied at the next reboot, which AWS accepts for any parameter.
        apply_immediately: Whether an instance or cluster modification is applied at once (the default) or
            queued for the next maintenance window; tags and parameters are unaffected.
        resource_arn: The resource written to, when it is not the operation's (a resource the operation
            created, such as a restored instance).

    Returns:
        What was submitted, `{attribute: value}`.

    Raises:
        UnsupportedRestoreError: An attribute cannot be written back, or a parameter or other attribute
            would have to be reset to nothing; raised before any call is made.

    Example:
        >>> apply_attribute_values(  # doctest: +SKIP
        ...     context, "aws_db_instance.demo_orders", {"backup_retention_period": 7, "tags.team": "data"}
        ... )
        {'backup_retention_period': 7, 'tags.team': 'data'}
    """
    ensure_values_restorable(address, values)
    client = _rds_client(context, step_name)
    target_arn = resource_arn if resource_arn is not None else context.operation.resource_arn or ""
    identifier = target_arn.rsplit(":", 1)[-1]
    logger.info(
        "restore_values_applying",
        operation_id=str(context.operation.id),
        address=address,
        identifier=identifier,
        attributes=sorted(values),
    )
    if _is_parameter_group(address):
        _apply_parameter_values(client, address, identifier, values, metadata, target_arn)
    else:
        _apply_resource_values(context, client, address, identifier, values, apply_immediately, target_arn)
    return dict(values)


def _described_resource(client: BaseClient, address: str, identifier: str) -> dict[str, Any]:
    if _resource_type(address) == "aws_rds_cluster":
        return client.describe_db_clusters(DBClusterIdentifier=identifier)["DBClusters"][0]
    return client.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]


def _described_group_name(address: str, description: dict[str, Any]) -> str | None:
    if _resource_type(address) == "aws_rds_cluster":
        return description.get("DBClusterParameterGroup")
    groups = description.get("DBParameterGroups") or []
    return groups[0].get("DBParameterGroupName") if groups else None


def read_attribute_values(
    context: StepContext,
    address: str,
    attributes: list[str],
    step_name: str = "verify_direct",
    resource_arn: str | None = None,
) -> dict[str, Any]:
    """Read the live value of each attribute from AWS, in the form the record holds it.

    Parameters read as their user-set value, or None when the parameter is at its default. A tag reads as
    None when absent. A parameter group reference reads as the reference the record uses for that group.

    Args:
        context: The running step's context; `context.operation.resource_arn` names the resource.
        address: Terraform address of the resource; its type picks the API.
        attributes: Attribute names as drift findings use them.
        step_name: Step the AWS calls are recorded under.
        resource_arn: The resource read, when it is not the operation's (a resource the operation created).

    Returns:
        `{attribute: live value}` for every requested attribute.

    Raises:
        UnsupportedRestoreError: An attribute is not one the console reads back.

    Example:
        >>> read_attribute_values(  # doctest: +SKIP
        ...     context, "aws_db_parameter_group.demo_postgres16_tuned", ["parameters.work_mem"]
        ... )
        {'parameters.work_mem': '16384'}
    """
    ensure_attributes_restorable(address, attributes)
    client = _rds_client(context, step_name)
    target_arn = resource_arn if resource_arn is not None else context.operation.resource_arn or ""
    identifier = target_arn.rsplit(":", 1)[-1]
    reader = RdsReader(client)
    resource_type = _resource_type(address)
    if _is_parameter_group(address):
        parameters = (
            reader.list_parameters(identifier)
            if resource_type == "aws_db_parameter_group"
            else reader.list_cluster_parameters(identifier)
        )
        user_values = {
            parameter["ParameterName"]: parameter.get("ParameterValue")
            for parameter in parameters
            if (parameter.get("Source") or "user") == "user"
        }
        group_tags = (
            reader.list_tags(target_arn)
            if any(attribute.startswith(TAG_ATTRIBUTE_PREFIX) for attribute in attributes)
            else {}
        )
        return {
            attribute: (
                group_tags.get(attribute.removeprefix(TAG_ATTRIBUTE_PREFIX))
                if attribute.startswith(TAG_ATTRIBUTE_PREFIX)
                else user_values.get(attribute.removeprefix(PARAMETER_ATTRIBUTE_PREFIX))
            )
            for attribute in attributes
        }
    description = _described_resource(client, address, identifier)
    tags = (
        reader.list_tags(target_arn)
        if any(attribute.startswith(TAG_ATTRIBUTE_PREFIX) for attribute in attributes)
        else {}
    )
    live: dict[str, Any] = {}
    for attribute in attributes:
        if attribute.startswith(TAG_ATTRIBUTE_PREFIX):
            live[attribute] = tags.get(attribute.removeprefix(TAG_ATTRIBUTE_PREFIX))
        elif attribute == GROUP_REFERENCE_ATTRIBUTE_BY_TYPE[resource_type]:
            live[attribute] = _reference_from_group_name(
                context, resource_type, _described_group_name(address, description)
            )
        else:
            live[attribute] = _described_field(
                description, MODIFIABLE_ATTRIBUTES_BY_TYPE[resource_type][attribute].description_field
            )
    return live


def apply_profile_for(
    address: str, attribute: str, metadata: FamilyMetadata | None = None
) -> AttributeApplyProfile:
    """How AWS applies a write to `attribute` of the resource at `address`.

    Args:
        address: Terraform address of the resource.
        attribute: Attribute name as drift findings use it.
        metadata: The parameter group's family; a parameter it lists as dynamic applies at once, any other
            parameter at the next reboot.

    Returns:
        The attribute's profile: `parameters.<name>` follow the family's apply type, tags have their own,
        everything else comes from `ATTRIBUTE_APPLY_PROFILES`.

    Raises:
        UnsupportedRestoreError: The console cannot write the attribute.

    Example:
        >>> apply_profile_for("aws_db_instance.demo_orders", "instance_class").apply_type
        'immediate'
    """
    ensure_attributes_restorable(address, [attribute])
    if attribute.startswith(PARAMETER_ATTRIBUTE_PREFIX):
        name = attribute.removeprefix(PARAMETER_ATTRIBUTE_PREFIX)
        if _apply_method_for(name, metadata) == "immediate":
            return DYNAMIC_PARAMETER_APPLY_PROFILE
        return STATIC_PARAMETER_APPLY_PROFILE
    if attribute.startswith(TAG_ATTRIBUTE_PREFIX):
        return TAG_APPLY_PROFILE
    return ATTRIBUTE_APPLY_PROFILES[attribute]


def apply_profile_if_known(
    address: str, attribute: str, metadata: FamilyMetadata | None = None
) -> AttributeApplyProfile | None:
    """How AWS applies a write to `attribute`, or None when the console has no profile for it.

    Unlike `apply_profile_for` this neither raises nor logs: Terraform mode restores every attribute the
    record holds, including those the console cannot write back itself, and only needs to know which ones
    it can describe.

    Args:
        address: Terraform address of the resource.
        attribute: Attribute name as drift findings use it.
        metadata: The parameter group's family, for `parameters.<name>`.

    Returns:
        The attribute's profile, or None for an attribute without one (for example `publicly_accessible`).

    Example:
        >>> apply_profile_if_known("aws_db_instance.demo_orders", "publicly_accessible") is None
        True
    """
    if not _is_restorable(address, attribute):
        return None
    return apply_profile_for(address, attribute, metadata)


def downtime_states_no_interruption(downtime: str) -> bool:
    """Whether a downtime text says nothing is interrupted: `none`, or `none (...)` with a qualification.

    Example:
        >>> downtime_states_no_interruption("none (storage optimisation runs in the background)")
        True
        >>> downtime_states_no_interruption("a restart of the instance (Multi-AZ fails over to the standby)")
        False
    """
    return downtime == NO_DOWNTIME or downtime.startswith(f"{NO_DOWNTIME} ")


def _is_nothing_queued(value: Any) -> bool:
    """None and empty containers say nothing is queued; False and 0 are values AWS will apply."""
    return value is None or (isinstance(value, dict | list | tuple | set) and not value)


def read_pending_modifications(
    context: StepContext, address: str, step_name: str = "compute_change_set"
) -> dict[str, Any]:
    """Modifications AWS has queued on the resource that have not been applied yet.

    A write with `ApplyImmediately` also applies everything already queued (for instance a class change
    chosen for the next maintenance window), so the review must say so.

    Args:
        context: The running step's context; `context.operation.resource_arn` names the resource.
        address: Terraform address of the resource.
        step_name: Step the AWS calls are recorded under.

    Returns:
        `{AWS field: queued value}`; empty when nothing is queued, and always empty for parameter groups.
        A queued `False` or `0` (`MultiAZ: False`, `BackupRetentionPeriod: 0`) is a modification like any
        other; only None and empty containers count as nothing.

    Example:
        >>> read_pending_modifications(context, "aws_db_instance.demo_orders")  # doctest: +SKIP
        {'DBInstanceClass': 'db.r6g.xlarge'}
    """
    if _is_parameter_group(address):
        return {}
    description = _described_resource(_rds_client(context, step_name), address, _physical_identifier(context))
    return {
        field: value
        for field, value in (description.get("PendingModifiedValues") or {}).items()
        if not _is_nothing_queued(value)
    }


def queued_attribute_values(resource_type: str, pending_modified_values: dict[str, Any]) -> dict[str, Any]:
    """Name the queued modifications of a described instance or cluster by the attribute they change.

    AWS lists queued modifications under its own field names (`BackupRetentionPeriod`); the record uses
    attribute names (`backup_retention_period`). Fields the console does not write are left out.

    Args:
        resource_type: Terraform type of the resource, e.g. `aws_db_instance`.
        pending_modified_values: `PendingModifiedValues` of the describe response.

    Returns:
        `{attribute: queued value}` for the queued fields that map to a writable attribute.

    Example:
        >>> queued_attribute_values("aws_db_instance", {"BackupRetentionPeriod": 14, "Port": 5433})
        {'backup_retention_period': 14}
    """
    attribute_by_field = {
        modifiable.request_field.partition(".")[0]: attribute
        for attribute, modifiable in MODIFIABLE_ATTRIBUTES_BY_TYPE.get(resource_type, {}).items()
        if "." not in modifiable.request_field
    }
    return {
        attribute_by_field[field]: value
        for field, value in (pending_modified_values or {}).items()
        if field in attribute_by_field and not _is_nothing_queued(value)
    }


def read_resource_status(
    context: StepContext, address: str, step_name: str = "verify_direct", resource_arn: str | None = None
) -> str | None:
    """The status AWS reports for the resource (`available`, `modifying`, `failed`, ...).

    Args:
        context: The running step's context; `context.operation.resource_arn` names the resource.
        address: Terraform address of the resource.
        step_name: Step the AWS calls are recorded under.
        resource_arn: The resource read, when it is not the operation's (a resource the operation created).

    Returns:
        The status, or None for parameter groups, which have none.

    Example:
        >>> read_resource_status(context, "aws_db_instance.demo_orders")  # doctest: +SKIP
        'available'
    """
    if _is_parameter_group(address):
        return None
    target_arn = resource_arn if resource_arn is not None else context.operation.resource_arn or ""
    description = _described_resource(_rds_client(context, step_name), address, target_arn.rsplit(":", 1)[-1])
    status_field = "Status" if _resource_type(address) == "aws_rds_cluster" else "DBInstanceStatus"
    return description.get(status_field)


# --- parameter groups, attachment and reboot ---------------------------------------------------------

DEFAULT_APPLY_METHOD = "pending-reboot"
"""Used for a parameter the family metadata does not know; valid for every parameter."""


def _apply_method_for(name: str, metadata: FamilyMetadata | None) -> str:
    """The apply method the family requires for `name`: `immediate` if dynamic, `pending-reboot` if static."""
    if metadata is not None and name in metadata.parameters:
        return metadata.parameters[name].apply_method_default
    return DEFAULT_APPLY_METHOD


def group_parameters(
    context: StepContext, group_name: str, scope: str, step_name: str = "describe_parameters"
) -> dict[str, dict[str, Any]]:
    """The user-set parameters of a group as AWS has them now.

    Args:
        context: The running step's context.
        group_name: Name of the parameter group.
        scope: `instance` or `cluster`.
        step_name: Step the AWS calls are recorded under.

    Returns:
        `{name: {"value", "source", "apply_method"}}` for parameters whose source is `user` and that have a
        value; defaults the family supplies are left out.

    Example:
        >>> group_parameters(context, "demo-postgres16-tuned", "instance")["work_mem"]  # doctest: +SKIP
        {'value': '16384', 'source': 'user', 'apply_method': 'immediate'}
    """
    reader = RdsReader(_rds_client(context, step_name))
    items = (
        reader.list_cluster_parameters(group_name)
        if scope == "cluster"
        else reader.list_parameters(group_name)
    )
    return {
        item["ParameterName"]: {
            "value": item.get("ParameterValue"),
            "source": item.get("Source") or "user",
            "apply_method": item.get("ApplyMethod"),
        }
        for item in items
        if item.get("ParameterValue") is not None and (item.get("Source") or "user") == "user"
    }


def modify_group(
    context: StepContext,
    group_name: str,
    scope: str,
    values: dict[str, str],
    metadata: FamilyMetadata,
    step_name: str = "apply_direct",
) -> None:
    """Set parameter values in batches of 20 (the API limit) with the apply method the family requires.

    Args:
        context: The running step's context.
        group_name: Name of the parameter group.
        scope: `instance` or `cluster`.
        values: `{parameter name: value}` to set.
        metadata: The group's family; static parameters are applied at the next reboot, dynamic ones at once.
        step_name: Step the AWS calls are recorded under.

    Example:
        >>> modify_group(  # doctest: +SKIP
        ...     context, "demo-postgres16-tuned", "instance", {"work_mem": "65536"}, family_metadata
        ... )
    """
    client = _rds_client(context, step_name)
    entries = [
        {"ParameterName": name, "ParameterValue": value, "ApplyMethod": _apply_method_for(name, metadata)}
        for name, value in values.items()
    ]
    logger.info(
        "parameter_group_modify_started",
        operation_id=str(context.operation.id),
        group_name=group_name,
        scope=scope,
        parameter_names=sorted(values),
    )
    for chunk in _chunks(entries):
        if scope == "cluster":
            client.modify_db_cluster_parameter_group(DBClusterParameterGroupName=group_name, Parameters=chunk)
        else:
            client.modify_db_parameter_group(DBParameterGroupName=group_name, Parameters=chunk)
    logger.info(
        "parameter_group_modify_completed",
        operation_id=str(context.operation.id),
        group_name=group_name,
        parameter_count=len(entries),
    )


def reset_group(
    context: StepContext,
    group_name: str,
    scope: str,
    names: list[str],
    metadata: FamilyMetadata | None = None,
    step_name: str = "apply_direct",
) -> None:
    """Return parameters to the family default.

    Args:
        context: The running step's context.
        group_name: Name of the parameter group.
        scope: `instance` or `cluster`.
        names: Parameters to reset.
        metadata: The group's family, to pick each parameter's apply method; `pending-reboot` without it.
        step_name: Step the AWS calls are recorded under.

    Raises:
        SimulatorUnsupportedAction: The simulated account does not implement the reset call.

    Example:
        >>> reset_group(context, "demo-postgres16-tuned", "instance", ["work_mem"])  # doctest: +SKIP
    """
    client = _rds_client(context, step_name)
    entries = [{"ParameterName": name, "ApplyMethod": _apply_method_for(name, metadata)} for name in names]
    logger.info(
        "parameter_group_reset_started",
        operation_id=str(context.operation.id),
        group_name=group_name,
        scope=scope,
        parameter_names=sorted(names),
    )
    operation_name = "reset_db_cluster_parameter_group" if scope == "cluster" else "reset_db_parameter_group"
    try:
        for chunk in _chunks(entries):
            if scope == "cluster":
                client.reset_db_cluster_parameter_group(
                    DBClusterParameterGroupName=group_name, Parameters=chunk
                )
            else:
                client.reset_db_parameter_group(DBParameterGroupName=group_name, Parameters=chunk)
    except Exception as error:
        if UNSUPPORTED_MARKER in str(error):
            raise SimulatorUnsupportedAction(operation_name) from error
        raise


def create_group(
    context: StepContext,
    group_name: str,
    scope: str,
    family: str,
    description: str,
    tags: dict[str, str],
    step_name: str = "apply_direct",
) -> str:
    """Create an empty parameter group.

    Args:
        context: The running step's context.
        group_name: Name of the new group.
        scope: `instance` or `cluster`.
        family: Parameter group family, e.g. `postgres16`.
        description: Description shown in AWS.
        tags: Tags to put on the group.
        step_name: Step the AWS calls are recorded under.

    Returns:
        The ARN of the new group.

    Example:
        >>> create_group(  # doctest: +SKIP
        ...     context, "orders-pg16-v2", "instance", "postgres16", "orders v2", {"team": "data"}
        ... )
        'arn:aws:rds:eu-west-2:000000000000:pg:orders-pg16-v2'
    """
    client = _rds_client(context, step_name)
    tag_list = [{"Key": key, "Value": value} for key, value in tags.items()]
    logger.info(
        "parameter_group_create_started",
        operation_id=str(context.operation.id),
        group_name=group_name,
        scope=scope,
        family=family,
    )
    if scope == "cluster":
        return client.create_db_cluster_parameter_group(
            DBClusterParameterGroupName=group_name,
            DBParameterGroupFamily=family,
            Description=description,
            Tags=tag_list,
        )["DBClusterParameterGroup"]["DBClusterParameterGroupArn"]
    return client.create_db_parameter_group(
        DBParameterGroupName=group_name,
        DBParameterGroupFamily=family,
        Description=description,
        Tags=tag_list,
    )["DBParameterGroup"]["DBParameterGroupArn"]


def delete_group(context: StepContext, group_name: str, scope: str, step_name: str = "apply_direct") -> None:
    """Delete a parameter group; AWS refuses one that is in use.

    Args:
        context: The running step's context.
        group_name: Name of the group.
        scope: `instance` or `cluster`.
        step_name: Step the AWS calls are recorded under.

    Example:
        >>> delete_group(context, "unused-pg16", "instance")  # doctest: +SKIP
    """
    client = _rds_client(context, step_name)
    logger.info(
        "parameter_group_delete_started",
        operation_id=str(context.operation.id),
        group_name=group_name,
        scope=scope,
    )
    if scope == "cluster":
        client.delete_db_cluster_parameter_group(DBClusterParameterGroupName=group_name)
    else:
        client.delete_db_parameter_group(DBParameterGroupName=group_name)


def group_exists(
    context: StepContext, group_name: str, scope: str, step_name: str = "compute_change_set"
) -> bool:
    """Whether the group exists in AWS now (the simulator answers an empty list instead of NotFound).

    Args:
        context: The running step's context.
        group_name: Name of the group.
        scope: `instance` or `cluster`.
        step_name: Step the AWS calls are recorded under.

    Returns:
        True when AWS lists the group.

    Example:
        >>> group_exists(context, "demo-postgres16-tuned", "instance")  # doctest: +SKIP
        True
    """
    client = _rds_client(context, step_name)
    try:
        if scope == "cluster":
            return bool(
                client.describe_db_cluster_parameter_groups(DBClusterParameterGroupName=group_name)[
                    "DBClusterParameterGroups"
                ]
            )
        return bool(client.describe_db_parameter_groups(DBParameterGroupName=group_name)["DBParameterGroups"])
    except client.exceptions.DBParameterGroupNotFoundFault:
        return False


def attach_group(
    context: StepContext,
    identifier: str,
    is_cluster: bool,
    group_name: str,
    step_name: str = "apply_direct",
) -> None:
    """Point an instance or a cluster at a parameter group; AWS applies it at once and flags a pending reboot.

    Args:
        context: The running step's context.
        identifier: Instance or cluster identifier.
        is_cluster: True for a cluster, which takes a cluster parameter group.
        group_name: The group to attach.
        step_name: Step the AWS calls are recorded under.

    Example:
        >>> attach_group(context, "demo-orders", False, "orders-pg16-v2")  # doctest: +SKIP
    """
    client = _rds_client(context, step_name)
    logger.info(
        "parameter_group_attach_started",
        operation_id=str(context.operation.id),
        identifier=identifier,
        is_cluster=is_cluster,
        group_name=group_name,
    )
    if is_cluster:
        client.modify_db_cluster(
            DBClusterIdentifier=identifier, DBClusterParameterGroupName=group_name, ApplyImmediately=True
        )
    else:
        client.modify_db_instance(
            DBInstanceIdentifier=identifier, DBParameterGroupName=group_name, ApplyImmediately=True
        )


def attached_group(
    context: StepContext, identifier: str, is_cluster: bool, step_name: str = "verify_direct"
) -> tuple[str, str | None]:
    """The parameter group an instance or cluster uses, and its apply status, as AWS reports them now.

    Args:
        context: The running step's context.
        identifier: Instance or cluster identifier.
        is_cluster: True for a cluster.
        step_name: Step the AWS calls are recorded under.

    Returns:
        `(group name, parameter apply status)`; the status is None for a cluster, and is `in-sync` or
        `pending-reboot` (among others) for an instance.

    Example:
        >>> attached_group(context, "demo-orders", False)  # doctest: +SKIP
        ('demo-postgres16-tuned', 'in-sync')
    """
    client = _rds_client(context, step_name)
    if is_cluster:
        cluster = client.describe_db_clusters(DBClusterIdentifier=identifier)["DBClusters"][0]
        return cluster.get("DBClusterParameterGroup", ""), None
    instance = client.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
    groups = instance.get("DBParameterGroups") or [{}]
    return groups[0].get("DBParameterGroupName", ""), groups[0].get("ParameterApplyStatus")


def reboot_instance(
    context: StepContext, identifier: str, force_failover: bool, step_name: str = "reboot"
) -> None:
    """Reboot an instance.

    Args:
        context: The running step's context.
        identifier: Instance identifier.
        force_failover: Multi-AZ only: fail over to the standby instead of restarting in place.
        step_name: Step the AWS calls are recorded under.

    Example:
        >>> reboot_instance(context, "demo-orders", False)  # doctest: +SKIP
    """
    logger.info(
        "instance_reboot_started",
        operation_id=str(context.operation.id),
        identifier=identifier,
        force_failover=force_failover,
    )
    _rds_client(context, step_name).reboot_db_instance(
        DBInstanceIdentifier=identifier, ForceFailover=force_failover
    )


def instance_status(context: StepContext, identifier: str, step_name: str = "await_available") -> str:
    """The status AWS reports for an instance now (`available`, `rebooting`, ...).

    Args:
        context: The running step's context.
        identifier: Instance identifier.
        step_name: Step the AWS calls are recorded under.

    Returns:
        The instance status.

    Example:
        >>> instance_status(context, "demo-orders")  # doctest: +SKIP
        'available'
    """
    description = _rds_client(context, step_name).describe_db_instances(DBInstanceIdentifier=identifier)
    return description["DBInstances"][0]["DBInstanceStatus"]
