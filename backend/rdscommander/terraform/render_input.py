"""Build the renderer's input from a snapshot plus the set of resources that belong in the workspace."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig, ResolvedTarget
from rdscommander.control_db.models import (
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryParameterGroup,
    InventoryParameterValue,
    InventorySnapshot,
    InventorySubnetGroup,
)
from rdscommander.terraform.hcl import to_hcl
from rdscommander.terraform.names import terraform_address_for, terraform_resource_name

IMPORTABLE_RESOURCE_TYPES = (
    "aws_db_parameter_group",
    "aws_rds_cluster_parameter_group",
    "aws_db_subnet_group",
    "aws_rds_cluster",
    "aws_rds_cluster_instance",
    "aws_db_instance",
)
HIDDEN_TAG_PREFIXES = ("aws:", "rdscommander:")


def _visible_tags(tags: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in tags.items() if not k.startswith(HIDDEN_TAG_PREFIXES)}


class RenderedParameter(BaseModel):
    """A user-set parameter."""

    name: str
    value: str
    apply_method: str


class RenderedParameterGroup(BaseModel):
    """Instance or cluster parameter group."""

    terraform_name: str
    name: str
    family: str
    description: str
    parameters: list[RenderedParameter]
    tags: dict[str, str]


class RenderedSubnetGroup(BaseModel):
    """Subnet group."""

    terraform_name: str
    name: str
    description: str
    subnet_ids: list[str]
    tags: dict[str, str]


class RestorePointInTime(BaseModel):
    """A point-in-time restore source, rendered as the provider's `restore_to_point_in_time` block.

    Attributes:
        source_identifier: The instance or cluster the restore reads from.
        restore_time_utc: ISO-8601 time to restore to; None with `use_latest_restorable_time`.
        use_latest_restorable_time: Restore to the latest restorable time instead of a fixed one.

    Example:
        >>> RestorePointInTime(source_identifier="demo-orders", restore_time_utc=None,
        ...                    use_latest_restorable_time=True).use_latest_restorable_time
        True
    """

    source_identifier: str
    restore_time_utc: str | None = None
    use_latest_restorable_time: bool = False

    @model_validator(mode="after")
    def _one_of_time_or_latest(self) -> RestorePointInTime:
        if (self.restore_time_utc is None) == (not self.use_latest_restorable_time):
            raise ValueError("give exactly one of restore_time_utc or use_latest_restorable_time")
        return self


class RenderedCluster(BaseModel):
    """Aurora cluster.

    Attributes:
        apply_immediately: Render `apply_immediately` so modifications do not wait for the window.
        final_snapshot_identifier: Name of the snapshot taken on deletion; None renders `<identifier>-final`.
        snapshot_identifier: Snapshot the cluster is restored from when it is created.
        restore_to_point_in_time: Point-in-time restore source used when the cluster is created.
        manage_master_user_password: Let RDS keep the master password in Secrets Manager.
    """

    terraform_name: str
    identifier: str
    engine: str
    engine_version: str
    engine_mode: str | None
    master_username: str
    database_name: str | None
    port: int | None
    db_subnet_group_reference: str | None
    vpc_security_group_ids: list[str]
    cluster_parameter_group_reference: str | None
    backup_retention_period: int | None
    preferred_backup_window: str | None
    preferred_maintenance_window: str | None
    storage_encrypted: bool
    kms_key_id: str | None
    deletion_protection: bool
    copy_tags_to_snapshot: bool
    auto_minor_version_upgrade: bool
    serverless_v2_min_capacity: float | None
    serverless_v2_max_capacity: float | None
    tags: dict[str, str]
    apply_immediately: bool = True
    final_snapshot_identifier: str | None = None
    snapshot_identifier: str | None = None
    restore_to_point_in_time: RestorePointInTime | None = None
    manage_master_user_password: bool = False


class RenderedClusterInstance(BaseModel):
    """Aurora cluster member.

    Attributes:
        preferred_maintenance_window: The member's own weekly maintenance window; None leaves it to AWS.
        performance_insights_retention_period: Days of Performance Insights data, rendered while it is on.
        monitoring_role_arn: Role enhanced monitoring publishes with, rendered while the interval is not 0.
        apply_immediately: Render `apply_immediately` so modifications do not wait for the window.
        depends_on_member: Address of the member Terraform must create first (a new cluster's writer, so the
            readers do not race it to become the writer); render-only, never drift, and absent from a render
            of the inventory.
    """

    terraform_name: str
    identifier: str
    cluster_reference: str
    instance_class: str
    engine: str
    engine_version: str
    parameter_group_reference: str | None
    publicly_accessible: bool
    auto_minor_version_upgrade: bool
    performance_insights_enabled: bool
    monitoring_interval: int
    promotion_tier: int
    tags: dict[str, str]
    preferred_maintenance_window: str | None = None
    performance_insights_retention_period: int | None = None
    monitoring_role_arn: str | None = None
    apply_immediately: bool = True
    depends_on_member: str | None = None


class RenderedInstance(BaseModel):
    """RDS instance or read replica.

    Attributes:
        apply_immediately: Render `apply_immediately` so modifications do not wait for the window.
        final_snapshot_identifier: Name of the snapshot taken on deletion; None renders `<identifier>-final`.
        snapshot_identifier: Snapshot the instance is restored from when it is created.
        restore_to_point_in_time: Point-in-time restore source used when the instance is created.
        manage_master_user_password: Let RDS keep the master password in Secrets Manager.
    """

    terraform_name: str
    identifier: str
    replicate_source_reference: str | None
    engine: str
    engine_version: str
    instance_class: str
    allocated_storage: int | None
    max_allocated_storage: int | None
    storage_type: str | None
    iops: int | None
    storage_throughput: int | None
    master_username: str | None
    db_name: str | None
    port: int | None
    db_subnet_group_reference: str | None
    vpc_security_group_ids: list[str]
    parameter_group_reference: str | None
    multi_az: bool
    publicly_accessible: bool
    backup_retention_period: int | None
    preferred_backup_window: str | None
    preferred_maintenance_window: str | None
    deletion_protection: bool
    storage_encrypted: bool
    kms_key_id: str | None
    performance_insights_enabled: bool
    performance_insights_retention_period: int | None
    monitoring_interval: int
    monitoring_role_arn: str | None
    auto_minor_version_upgrade: bool
    copy_tags_to_snapshot: bool
    ca_cert_identifier: str | None
    tags: dict[str, str]
    apply_immediately: bool = True
    final_snapshot_identifier: str | None = None
    snapshot_identifier: str | None = None
    restore_to_point_in_time: RestorePointInTime | None = None
    manage_master_user_password: bool = False


class ImportBlock(BaseModel):
    """One `import {}` block."""

    to_address: str
    import_id: str


class RenderWorkspaceInput(BaseModel):
    """Everything the templates need."""

    target_key: str
    aws_region: str
    aws_profile_name: str | None
    simulated: bool
    simulated_endpoint_url: str | None
    required_version: str
    aws_provider_version: str
    state_bucket_name: str
    state_key: str
    instance_parameter_groups: list[RenderedParameterGroup] = Field(default_factory=list)
    cluster_parameter_groups: list[RenderedParameterGroup] = Field(default_factory=list)
    subnet_groups: list[RenderedSubnetGroup] = Field(default_factory=list)
    clusters: list[RenderedCluster] = Field(default_factory=list)
    cluster_instances: list[RenderedClusterInstance] = Field(default_factory=list)
    instances: list[RenderedInstance] = Field(default_factory=list)
    import_blocks: list[ImportBlock] = Field(default_factory=list)


def _reference_or_literal(
    rendered_names: dict[str, str], resource_type: str, name: str | None, attribute: str
) -> str | None:
    """`aws_x.y.attr` when `name` is rendered in this workspace, otherwise a quoted literal."""
    if name is None:
        return None
    address = rendered_names.get(f"{resource_type}:{name}")
    return f"{address}.{attribute}" if address else to_hcl(name)


def _kms_key_arn(raw: dict[str, Any]) -> str | None:
    """Only a real key ARN is rendered; anything else (including simulator placeholders) is left to AWS."""
    value = raw.get("KmsKeyId")
    return value if isinstance(value, str) and value.startswith("arn:") else None


def _security_group_ids(raw: dict[str, Any]) -> list[str]:
    return sorted(
        g["VpcSecurityGroupId"] for g in raw.get("VpcSecurityGroups", []) if g.get("VpcSecurityGroupId")
    )


def build_render_input(
    session: Session,
    target: ResolvedTarget,
    console_config: ConsoleConfig,
    snapshot: InventorySnapshot,
    selected_arns: set[str],
    already_adopted_addresses: set[str],
    include_imports: bool,
    simulated_endpoint_url: str | None,
    include_all: bool = False,
    overlay: Callable[[RenderWorkspaceInput], RenderWorkspaceInput] | None = None,
) -> RenderWorkspaceInput:
    """Workspace content is every resource already adopted plus every selected resource.

    Import blocks are written only for resources that are not yet adopted.

    Args:
        session: Control database session.
        target: The target whose workspace is rendered.
        console_config: Loaded console configuration.
        snapshot: Inventory snapshot to render from.
        selected_arns: ARNs selected for adoption; rendered in addition to what is already adopted.
        already_adopted_addresses: Terraform addresses already imported into the workspace.
        include_imports: Whether to emit `import {}` blocks for resources that are not yet adopted.
        simulated_endpoint_url: Endpoint of the simulated AWS, used only for simulated targets.
        include_all: Render every non-default resource of the snapshot, whatever its ownership. This is
            the record of a direct-mode environment, which has no adoption step.
        overlay: Applied to the finished input before it is returned; lets a caller substitute the
            values a change wants (for example a restore) without touching the inventory.

    Returns:
        The renderer input.

    Example:
        >>> build_render_input(  # doctest: +SKIP
        ...     session, target, console_config, snapshot, set(), adopted_addresses,
        ...     include_imports=False, simulated_endpoint_url=None, include_all=target.is_direct,
        ... ).instances[0].identifier
        'demo-orders'
    """
    render_input = RenderWorkspaceInput(
        target_key=target.target_key,
        aws_region=target.aws_region,
        aws_profile_name=target.aws_profile_name,
        simulated=target.simulated,
        simulated_endpoint_url=simulated_endpoint_url if target.simulated else None,
        required_version=console_config.terraform.required_opentofu_version,
        aws_provider_version=console_config.terraform.aws_provider_version,
        state_bucket_name=target.state_bucket_name(console_config.terraform.state_bucket_name_template),
        state_key=target.state_key,
    )
    groups = (
        session.execute(
            select(InventoryParameterGroup).where(InventoryParameterGroup.snapshot_id == snapshot.id)
        )
        .scalars()
        .all()
    )
    subnet_groups = (
        session.execute(select(InventorySubnetGroup).where(InventorySubnetGroup.snapshot_id == snapshot.id))
        .scalars()
        .all()
    )
    clusters = (
        session.execute(select(InventoryDbCluster).where(InventoryDbCluster.snapshot_id == snapshot.id))
        .scalars()
        .all()
    )
    instances = (
        session.execute(select(InventoryDbInstance).where(InventoryDbInstance.snapshot_id == snapshot.id))
        .scalars()
        .all()
    )

    def wanted(resource_type: str, identifier: str, arn: str, ownership: str) -> bool:
        # Adopted means under Terraform (an adoption row or Terraform state), never the ownership tag: a
        # group the console created in direct mode carries the tag too.
        if include_all:
            return True
        address = terraform_address_for(resource_type, identifier)
        return arn in selected_arns or address in already_adopted_addresses

    def group_type(group: InventoryParameterGroup) -> str:
        return (
            "aws_db_parameter_group" if group.group_scope == "instance" else "aws_rds_cluster_parameter_group"
        )

    rendered_names: dict[str, str] = {}
    chosen_groups = [
        g
        for g in groups
        if not g.is_default
        and wanted(group_type(g), g.parameter_group_name, g.parameter_group_arn, g.ownership)
    ]
    for group in chosen_groups:
        key = f"{group_type(group)}:{group.parameter_group_name}"
        rendered_names[key] = terraform_address_for(group_type(group), group.parameter_group_name)
    chosen_subnet_groups = [
        s
        for s in subnet_groups
        if wanted("aws_db_subnet_group", s.db_subnet_group_name, s.db_subnet_group_arn, s.ownership)
    ]
    for subnet_group in chosen_subnet_groups:
        key = f"aws_db_subnet_group:{subnet_group.db_subnet_group_name}"
        rendered_names[key] = terraform_address_for("aws_db_subnet_group", subnet_group.db_subnet_group_name)
    chosen_clusters = [
        c
        for c in clusters
        if wanted("aws_rds_cluster", c.db_cluster_identifier, c.db_cluster_arn, c.ownership)
    ]
    for cluster in chosen_clusters:
        key = f"aws_rds_cluster:{cluster.db_cluster_identifier}"
        rendered_names[key] = terraform_address_for("aws_rds_cluster", cluster.db_cluster_identifier)

    def instance_type(instance: InventoryDbInstance) -> str:
        return "aws_rds_cluster_instance" if instance.db_cluster_identifier else "aws_db_instance"

    chosen_instances = [
        i
        for i in instances
        if wanted(instance_type(i), i.db_instance_identifier, i.db_instance_arn, i.ownership)
    ]
    for instance in chosen_instances:
        key = f"{instance_type(instance)}:{instance.db_instance_identifier}"
        rendered_names[key] = terraform_address_for(instance_type(instance), instance.db_instance_identifier)

    for group in sorted(chosen_groups, key=lambda g: g.parameter_group_name):
        values = session.execute(
            select(InventoryParameterValue)
            .where(
                InventoryParameterValue.parameter_group_id == group.id,
                InventoryParameterValue.source == "user",
            )
            .order_by(InventoryParameterValue.parameter_name)
        ).scalars()
        rendered = RenderedParameterGroup(
            terraform_name=terraform_resource_name(group.parameter_group_name),
            name=group.parameter_group_name,
            family=group.parameter_group_family,
            description=group.description or "Managed by the console",
            parameters=[
                RenderedParameter(
                    name=v.parameter_name,
                    value=v.parameter_value or "",
                    apply_method=v.apply_method
                    or ("pending-reboot" if v.apply_type == "static" else "immediate"),
                )
                for v in values
                if v.parameter_value is not None
            ],
            tags=_visible_tags(group.tags),
        )
        if group.group_scope == "instance":
            render_input.instance_parameter_groups.append(rendered)
        else:
            render_input.cluster_parameter_groups.append(rendered)
    for subnet_group in sorted(chosen_subnet_groups, key=lambda s: s.db_subnet_group_name):
        render_input.subnet_groups.append(
            RenderedSubnetGroup(
                terraform_name=terraform_resource_name(subnet_group.db_subnet_group_name),
                name=subnet_group.db_subnet_group_name,
                description=subnet_group.description or "Managed by the console",
                subnet_ids=sorted(str(s) for s in subnet_group.subnet_ids),
                tags=_visible_tags(subnet_group.tags),
            )
        )
    for cluster in sorted(chosen_clusters, key=lambda c: c.db_cluster_identifier):
        raw = cluster.raw_description
        scaling = raw.get("ServerlessV2ScalingConfiguration") or {}
        render_input.clusters.append(
            RenderedCluster(
                terraform_name=terraform_resource_name(cluster.db_cluster_identifier),
                identifier=cluster.db_cluster_identifier,
                engine=cluster.engine,
                engine_version=cluster.engine_version,
                engine_mode=None if raw.get("EngineMode") in (None, "provisioned") else raw.get("EngineMode"),
                master_username=raw.get("MasterUsername", ""),
                database_name=raw.get("DatabaseName"),
                port=raw.get("Port"),
                db_subnet_group_reference=_reference_or_literal(
                    rendered_names, "aws_db_subnet_group", cluster.db_subnet_group_name, "name"
                ),
                vpc_security_group_ids=_security_group_ids(raw),
                cluster_parameter_group_reference=_reference_or_literal(
                    rendered_names,
                    "aws_rds_cluster_parameter_group",
                    cluster.cluster_parameter_group_name,
                    "name",
                ),
                backup_retention_period=raw.get("BackupRetentionPeriod"),
                preferred_backup_window=raw.get("PreferredBackupWindow"),
                preferred_maintenance_window=raw.get("PreferredMaintenanceWindow"),
                storage_encrypted=bool(raw.get("StorageEncrypted", False)),
                kms_key_id=_kms_key_arn(raw),
                deletion_protection=bool(raw.get("DeletionProtection", False)),
                copy_tags_to_snapshot=bool(raw.get("CopyTagsToSnapshot", False)),
                auto_minor_version_upgrade=bool(raw.get("AutoMinorVersionUpgrade", True)),
                serverless_v2_min_capacity=scaling.get("MinCapacity"),
                serverless_v2_max_capacity=scaling.get("MaxCapacity"),
                tags=_visible_tags(cluster.tags),
                manage_master_user_password=bool(raw.get("MasterUserSecret")),
            )
        )
    for instance in sorted(chosen_instances, key=lambda i: i.db_instance_identifier):
        raw = instance.raw_description
        if instance.db_cluster_identifier:
            render_input.cluster_instances.append(
                RenderedClusterInstance(
                    terraform_name=terraform_resource_name(instance.db_instance_identifier),
                    identifier=instance.db_instance_identifier,
                    cluster_reference=_reference_or_literal(
                        rendered_names, "aws_rds_cluster", instance.db_cluster_identifier, "id"
                    )
                    or "",
                    instance_class=instance.db_instance_class,
                    engine=instance.engine,
                    engine_version=instance.engine_version,
                    parameter_group_reference=_reference_or_literal(
                        rendered_names, "aws_db_parameter_group", instance.parameter_group_name, "name"
                    ),
                    publicly_accessible=bool(raw.get("PubliclyAccessible", False)),
                    auto_minor_version_upgrade=bool(raw.get("AutoMinorVersionUpgrade", True)),
                    performance_insights_enabled=bool(raw.get("PerformanceInsightsEnabled", False)),
                    monitoring_interval=int(raw.get("MonitoringInterval", 0) or 0),
                    promotion_tier=int(raw.get("PromotionTier", 1) or 1),
                    tags=_visible_tags(instance.tags),
                    preferred_maintenance_window=raw.get("PreferredMaintenanceWindow"),
                    performance_insights_retention_period=raw.get("PerformanceInsightsRetentionPeriod"),
                    monitoring_role_arn=raw.get("MonitoringRoleArn"),
                )
            )
            continue
        render_input.instances.append(
            RenderedInstance(
                terraform_name=terraform_resource_name(instance.db_instance_identifier),
                identifier=instance.db_instance_identifier,
                replicate_source_reference=_reference_or_literal(
                    rendered_names, "aws_db_instance", instance.read_replica_source_identifier, "identifier"
                ),
                engine=instance.engine,
                engine_version=instance.engine_version,
                instance_class=instance.db_instance_class,
                allocated_storage=raw.get("AllocatedStorage"),
                max_allocated_storage=raw.get("MaxAllocatedStorage"),
                storage_type=raw.get("StorageType"),
                iops=raw.get("Iops"),
                storage_throughput=raw.get("StorageThroughput"),
                master_username=raw.get("MasterUsername"),
                db_name=raw.get("DBName"),
                port=(raw.get("Endpoint") or {}).get("Port"),
                db_subnet_group_reference=_reference_or_literal(
                    rendered_names, "aws_db_subnet_group", instance.db_subnet_group_name, "name"
                ),
                vpc_security_group_ids=_security_group_ids(raw),
                parameter_group_reference=_reference_or_literal(
                    rendered_names, "aws_db_parameter_group", instance.parameter_group_name, "name"
                ),
                multi_az=bool(raw.get("MultiAZ", False)),
                publicly_accessible=bool(raw.get("PubliclyAccessible", False)),
                backup_retention_period=raw.get("BackupRetentionPeriod"),
                preferred_backup_window=raw.get("PreferredBackupWindow"),
                preferred_maintenance_window=raw.get("PreferredMaintenanceWindow"),
                deletion_protection=bool(raw.get("DeletionProtection", False)),
                storage_encrypted=bool(raw.get("StorageEncrypted", False)),
                kms_key_id=_kms_key_arn(raw),
                performance_insights_enabled=bool(raw.get("PerformanceInsightsEnabled", False)),
                performance_insights_retention_period=raw.get("PerformanceInsightsRetentionPeriod"),
                monitoring_interval=int(raw.get("MonitoringInterval", 0) or 0),
                monitoring_role_arn=raw.get("MonitoringRoleArn"),
                auto_minor_version_upgrade=bool(raw.get("AutoMinorVersionUpgrade", True)),
                copy_tags_to_snapshot=bool(raw.get("CopyTagsToSnapshot", False)),
                ca_cert_identifier=raw.get("CACertificateIdentifier"),
                tags=_visible_tags(instance.tags),
                manage_master_user_password=bool(raw.get("MasterUserSecret")),
            )
        )

    if include_imports:
        for key, address in sorted(rendered_names.items()):
            _, identifier = key.split(":", 1)
            if address in already_adopted_addresses:
                continue
            render_input.import_blocks.append(ImportBlock(to_address=address, import_id=identifier))
    return overlay(render_input) if overlay else render_input
