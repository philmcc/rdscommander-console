"""Create read replicas, Aurora readers, and instances and clusters from the template, in both modes.

A replica and a reader are `ResourceCreateChange`s launched from an existing resource: the operation stays
on that resource (the source instance, the cluster) and lists the new resource in
`plan["created_resource_arns"]`.
Terraform mode renders the new resource next to the one it was launched from, referring to it by address when
it is rendered in the same workspace; direct mode calls `CreateDBInstanceReadReplica` or `CreateDBInstance`.

An instance or a cluster created from the template is launched from the environment: the operation points at
the new resource once it exists. The template fixes what every created resource has
(`INSTANCE_TEMPLATE_DEFAULTS`, `CLUSTER_TEMPLATE_DEFAULTS`, `CLUSTER_MEMBER_TEMPLATE_DEFAULTS`), among them a
master password AWS creates and keeps in Secrets Manager (`manage_master_user_password`), so no password ever
passes through the console. A cluster is created with its members `<name>-1` (the writer) to `<name>-<n>`.

Example:
    >>> ReadReplicaCreate(TARGET_KEY, orders_record, options).address  # doctest: +SKIP
    'aws_db_instance.demo_orders_replica_2'
"""

from __future__ import annotations

from typing import Any, NoReturn

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from rdscommander.control_db.models import InventoryDbInstance
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.lifecycle import direct_api as lifecycle_api
from rdscommander.lifecycle.options import (
    ClusterCreateOptions,
    ClusterReaderCreateOptions,
    InstanceCreateOptions,
    ReadReplicaCreateOptions,
)
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeDiagnosticError, ChangeSetEntry
from rdscommander.operations.change_strategy import RecordUnreadableError
from rdscommander.operations.create_change import (
    AVAILABLE_STATUS,
    CONSOLE_MANAGED_TAGS,
    NEW_RESOURCE_DOWNTIME,
    ResourceCreateChange,
    ResourceCreateExecutor,
    record_created_resource,
    sibling_arn_of,
)
from rdscommander.operations.executor import StepContext
from rdscommander.resources.model import ResourceRecord
from rdscommander.resources.queries import find_parameter_group, load_resource
from rdscommander.terraform.hcl import to_hcl
from rdscommander.terraform.names import terraform_address_for, terraform_resource_name
from rdscommander.terraform.record import RECORD_FILE_NAME, RESOURCE_SECTIONS, read_record
from rdscommander.terraform.render_input import (
    RenderedCluster,
    RenderedClusterInstance,
    RenderedInstance,
    RenderWorkspaceInput,
    build_render_input,
)
from rdscommander.terraform.workspaces import (
    adopted_terraform_addresses,
    ensure_workspace_record,
    workspace_directory,
)

logger = get_logger(__name__)

DEFAULT_PARAMETER_GROUP_PREFIX = "default."
"""AWS's default parameter groups are never rendered, so a reference to one is always a literal."""
INSTANCE_TEMPLATE_DEFAULTS: dict[str, Any] = {
    "storage_encrypted": True,
    "backup_retention_period": 7,
    "performance_insights_enabled": True,
    "deletion_protection": True,
    "auto_minor_version_upgrade": True,
    "copy_tags_to_snapshot": True,
    "manage_master_user_password": True,
    "monitoring_interval": 0,
}
"""What every instance created from the template has; these are not options."""
CLUSTER_TEMPLATE_DEFAULTS: dict[str, Any] = {
    "storage_encrypted": True,
    "backup_retention_period": 7,
    "deletion_protection": True,
    "auto_minor_version_upgrade": True,
    "copy_tags_to_snapshot": True,
    "manage_master_user_password": True,
}
"""What every cluster created from the template has; it is provisioned, not Serverless v2."""
CLUSTER_MEMBER_TEMPLATE_DEFAULTS: dict[str, Any] = {
    "publicly_accessible": False,
    "auto_minor_version_upgrade": True,
    "performance_insights_enabled": True,
    "monitoring_interval": 0,
    "promotion_tier": 1,
}
"""What every member of a cluster created from the template has; the writer is created first."""
__all__ = [
    "CLUSTER_MEMBER_TEMPLATE_DEFAULTS",
    "CLUSTER_TEMPLATE_DEFAULTS",
    "ClusterCreate",
    "ClusterCreateExecutor",
    "ClusterMemberNotCreatedError",
    "ClusterReaderCreate",
    "ClusterReaderCreateExecutor",
    "INSTANCE_TEMPLATE_DEFAULTS",
    "InstanceCreate",
    "InstanceCreateExecutor",
    "ReadReplicaCreate",
    "ReadReplicaCreateExecutor",
    "rendered_instance_of",
]


def _adopted_addresses(context: StepContext) -> set[str]:
    """The Terraform addresses already under the target's workspace."""
    workspace = ensure_workspace_record(context.session, context.target, context.console_config)
    context.session.commit()
    return adopted_terraform_addresses(context.session, workspace)


def _rendered_in_workspace(context: StepContext, resource_type: str, identifier: str) -> bool:
    """Whether the workspace renders the resource: all of them in direct mode, adopted ones otherwise."""
    if context.target.is_direct:
        return True
    return terraform_address_for(resource_type, identifier) in _adopted_addresses(context)


def _reference_or_literal(context: StepContext, resource_type: str, identifier: str, attribute: str) -> str:
    """`aws_x.y.attr` when the workspace renders the resource, otherwise the quoted identifier."""
    if _rendered_in_workspace(context, resource_type, identifier):
        return f"{terraform_address_for(resource_type, identifier)}.{attribute}"
    return to_hcl(identifier)


def rendered_instance_of(context: StepContext, identifier: str) -> RenderedInstance:
    """The rendered model of a standalone instance, as the workspace has it or would render it now.

    Terraform mode takes an adopted instance from the committed record (what the workspace's files say);
    any other instance, and every instance in direct mode, is rendered afresh from the latest inventory.

    Args:
        context: The running step's context.
        identifier: The instance's identifier.

    Returns:
        The instance's `RenderedInstance`.

    Raises:
        KeyError: The instance is not in the latest inventory.
        ValueError: The target was never inventoried, or the instance is an Aurora member.
        RecordUnreadableError: The committed record cannot be read.

    Example:
        >>> rendered_instance_of(context, "demo-orders").instance_class  # doctest: +SKIP
        'db.m6g.large'
    """
    target = context.target
    workspace = ensure_workspace_record(context.session, target, context.console_config)
    context.session.commit()
    adopted = adopted_terraform_addresses(context.session, workspace)
    if not target.is_direct and terraform_address_for("aws_db_instance", identifier) in adopted:
        directory = workspace_directory(context.settings.repo_root, workspace)
        try:
            recorded = read_record(directory)
        except ValueError as error:
            raise RecordUnreadableError(f"{directory / RECORD_FILE_NAME}: {error}") from error
        if recorded is not None:
            for instance in recorded.render_input.instances:
                if instance.identifier == identifier:
                    return instance
    snapshot = latest_snapshot_for_target(context.session, target.target_key)
    if snapshot is None:
        raise ValueError(f"{target.target_key} has never been inventoried; refresh inventory first")
    row = context.session.execute(
        select(InventoryDbInstance).where(
            InventoryDbInstance.snapshot_id == snapshot.id,
            InventoryDbInstance.db_instance_identifier == identifier,
        )
    ).scalar_one_or_none()
    if row is None:
        raise KeyError(f"{identifier} is not in the latest inventory of {target.target_key}")
    fresh = build_render_input(
        context.session,
        target,
        context.console_config,
        snapshot,
        {row.db_instance_arn},
        adopted,
        include_imports=False,
        simulated_endpoint_url=context.settings.simulated_aws_endpoint_url,
        include_all=target.is_direct,
    )
    for instance in fresh.instances:
        if instance.identifier == identifier:
            return instance
    raise ValueError(f"{identifier} is a member of an Aurora cluster, not a standalone instance")


def _launching_record(context: StepContext) -> ResourceRecord:
    """The record of the resource the operation was launched from.

    Raises:
        KeyError: It is not in the latest inventory.
    """
    arn = context.operation.resource_arn or ""
    record = load_resource(context.session, context.console_config, arn)
    if record is None:
        raise KeyError(
            f"{arn} is not in the latest inventory; refresh inventory and create the operation again"
        )
    return record


def _instance_status(context: StepContext, identifier: str, step_name: str) -> str | None:
    """The instance's status, or None while AWS does not list it."""
    description = lifecycle_api.instance_describe(context, identifier, step_name)
    return None if description is None else description["DBInstanceStatus"]


class ReadReplicaCreate(ResourceCreateChange):
    """A read replica of a standalone instance, in the same account and region.

    Attributes:
        source: The record of the instance replicated.
        options: The replica's settings.

    Example:
        >>> ReadReplicaCreate(TARGET_KEY, orders_record, options).summary  # doctest: +SKIP
        'create read replica demo-orders-replica-2 of demo-orders'
    """

    def __init__(self, target_key: str, source: ResourceRecord, options: ReadReplicaCreateOptions) -> None:
        """Describe the replica; nothing is created until a strategy runs the change."""
        super().__init__(
            target_key,
            options.replica_identifier,
            "aws_db_instance",
            f"create read replica {options.replica_identifier} of {source.identifier}",
        )
        self.source = source
        self.options = options

    def rendered(self, context: StepContext) -> RenderedInstance:
        """The source's rendered model as a replica: its own name, class, Multi-AZ and storage, no tags.

        Storage type, IOPS and throughput come from the options only. Copied from the source they could not
        be sent as they are: a gp3 source under 400 GiB reports its baseline IOPS, which a create refuses, and
        an io1 source's type needs IOPS the options may not give. Left out, the replica takes the source's.
        No backup settings are rendered: the create call takes none (AWS sets a new replica's), and rendered,
        Terraform's provider would modify the new replica to the source's while direct mode would not.
        """
        source_model = rendered_instance_of(context, self.source.identifier)
        return source_model.model_copy(
            deep=True,
            update={
                "terraform_name": terraform_resource_name(self.identifier),
                "identifier": self.identifier,
                "replicate_source_reference": _reference_or_literal(
                    context, "aws_db_instance", self.source.identifier, "identifier"
                ),
                "instance_class": self.options.instance_class,
                "storage_type": self.options.storage_type,
                "iops": self.options.iops,
                "storage_throughput": None,
                "multi_az": self.options.multi_az,
                "backup_retention_period": None,
                "preferred_backup_window": None,
                "publicly_accessible": False,
                "tags": {},
                "final_snapshot_identifier": None,
                "snapshot_identifier": None,
                "restore_to_point_in_time": None,
                "manage_master_user_password": False,
            },
        )

    def _rendered_model(self, context: StepContext) -> RenderedInstance:
        """The prepared rendered replica, worked out now when the change was not prepared."""
        rendered = self.rendered_cached if self.rendered_cached is not None else self.rendered(context)
        assert isinstance(rendered, RenderedInstance)
        return rendered

    def create_arguments(self, context: StepContext) -> dict[str, Any]:
        """The `CreateDBInstanceReadReplica` request: what the rendered replica carries that the call takes.

        Direct and Terraform mode create the same replica this way. The call takes no maintenance window, and
        what a replica inherits (engine, storage size, subnet group, users, backups, encryption) is not sent.
        """
        rendered = self._rendered_model(context)
        arguments: dict[str, Any] = {
            "DBInstanceIdentifier": rendered.identifier,
            "SourceDBInstanceIdentifier": self.source.identifier,
            "DBInstanceClass": rendered.instance_class,
            "MultiAZ": rendered.multi_az,
            "PubliclyAccessible": rendered.publicly_accessible,
            "DeletionProtection": rendered.deletion_protection,
            "AutoMinorVersionUpgrade": rendered.auto_minor_version_upgrade,
            "CopyTagsToSnapshot": rendered.copy_tags_to_snapshot,
            "EnablePerformanceInsights": rendered.performance_insights_enabled,
            "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
        }
        optional = {
            "StorageType": rendered.storage_type,
            "Iops": rendered.iops,
            "StorageThroughput": rendered.storage_throughput,
            "VpcSecurityGroupIds": list(rendered.vpc_security_group_ids) or None,
            "DBParameterGroupName": self.source.relationships.get("parameter_group_name")
            if rendered.parameter_group_reference
            else None,
            "CACertificateIdentifier": rendered.ca_cert_identifier,
        }
        if rendered.performance_insights_enabled:
            optional["PerformanceInsightsRetentionPeriod"] = rendered.performance_insights_retention_period
        if rendered.monitoring_interval:
            optional["MonitoringInterval"] = rendered.monitoring_interval
            optional["MonitoringRoleArn"] = rendered.monitoring_role_arn
        arguments.update({key: value for key, value in optional.items() if value is not None})
        return arguments

    def create(self, context: StepContext) -> str:
        """`CreateDBInstanceReadReplica` with what the rendered replica carries and the console's tag."""
        return lifecycle_api.create_read_replica(context, self.create_arguments(context), "apply_direct")

    def expected_arn(self, context: StepContext) -> str:
        """The new instance's ARN, in the account and region of its source."""
        return sibling_arn_of(self.source.arn, self.resource_type, self.identifier)

    def exists(self, context: StepContext, step_name: str = "compute_change_set") -> bool:
        """Whether AWS has an instance with the replica's identifier."""
        return lifecycle_api.instance_describe(context, self.identifier, step_name) is not None

    def status(self, context: StepContext, step_name: str = "verify_direct") -> str | None:
        """The replica's status, or None while AWS does not list it."""
        return _instance_status(context, self.identifier, step_name)

    def review_entries(self, context: StepContext) -> list[tuple[str, Any]]:
        """Source, class, Multi-AZ, storage, version, security groups and deletion protection, for the review.

        The security groups and deletion protection are copied from the source, so the approver sees them.
        """
        entries: list[tuple[str, Any]] = [
            ("replicate_source", self.source.identifier),
            ("instance_class", self.options.instance_class),
            ("multi_az", self.options.multi_az),
            ("storage_type", self.options.storage_type or self.source.facts.get("storage_type")),
        ]
        if self.options.iops is not None:
            entries.append(("iops", self.options.iops))
        rendered = self._rendered_model(context)
        entries += [
            ("engine_version", self.source.facts.get("engine_version")),
            ("vpc_security_group_ids", list(rendered.vpc_security_group_ids)),
            ("deletion_protection", rendered.deletion_protection),
        ]
        return entries


class ClusterReaderCreate(ResourceCreateChange):
    """A reader instance in an Aurora cluster.

    Attributes:
        cluster: The record of the cluster the reader joins.
        options: The reader's settings.

    Example:
        >>> ClusterReaderCreate(TARGET_KEY, aurora_record, options).address  # doctest: +SKIP
        'aws_rds_cluster_instance.demo_aurora_reader_2'
    """

    def __init__(self, target_key: str, cluster: ResourceRecord, options: ClusterReaderCreateOptions) -> None:
        """Describe the reader; nothing is created until a strategy runs the change."""
        super().__init__(
            target_key,
            options.reader_identifier,
            "aws_rds_cluster_instance",
            f"add reader {options.reader_identifier} to {cluster.identifier}",
        )
        self.cluster = cluster
        self.options = options

    def _engine(self) -> str:
        return str(self.cluster.facts.get("engine") or "aurora-postgresql")

    def rendered(self, context: StepContext) -> RenderedClusterInstance:
        """A cluster member of the cluster's engine and version, with the chosen class, tier and group."""
        group_name = self.options.parameter_group_name
        group_reference = None
        if group_name is not None:
            group_reference = (
                to_hcl(group_name)
                if group_name.startswith(DEFAULT_PARAMETER_GROUP_PREFIX)
                else _reference_or_literal(context, "aws_db_parameter_group", group_name, "name")
            )
        return RenderedClusterInstance(
            terraform_name=terraform_resource_name(self.identifier),
            identifier=self.identifier,
            cluster_reference=_reference_or_literal(
                context, "aws_rds_cluster", self.cluster.identifier, "id"
            ),
            instance_class=self.options.instance_class,
            engine=self._engine(),
            engine_version=str(self.cluster.facts.get("engine_version") or ""),
            parameter_group_reference=group_reference,
            publicly_accessible=False,
            auto_minor_version_upgrade=bool(self.cluster.raw.get("AutoMinorVersionUpgrade", True)),
            performance_insights_enabled=self.options.performance_insights_enabled,
            monitoring_interval=0,
            promotion_tier=self.options.promotion_tier,
            tags={},
        )

    def create_arguments(self, context: StepContext) -> dict[str, Any]:
        """The `CreateDBInstance` request: everything the rendered member carries that the call takes.

        The member's engine version is the cluster's, so it is not sent.
        """
        rendered = self.rendered_cached if self.rendered_cached is not None else self.rendered(context)
        assert isinstance(rendered, RenderedClusterInstance)
        arguments: dict[str, Any] = {
            "DBInstanceIdentifier": rendered.identifier,
            "DBClusterIdentifier": self.cluster.identifier,
            "Engine": rendered.engine,
            "DBInstanceClass": rendered.instance_class,
            "PromotionTier": rendered.promotion_tier,
            "PubliclyAccessible": rendered.publicly_accessible,
            "AutoMinorVersionUpgrade": rendered.auto_minor_version_upgrade,
            "EnablePerformanceInsights": rendered.performance_insights_enabled,
            "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
        }
        if self.options.parameter_group_name is not None:
            arguments["DBParameterGroupName"] = self.options.parameter_group_name
        if rendered.monitoring_interval:
            arguments["MonitoringInterval"] = rendered.monitoring_interval
        return arguments

    def create(self, context: StepContext) -> str:
        """`CreateDBInstance` in the cluster with what the rendered member carries and the console's tag."""
        return lifecycle_api.create_cluster_member(context, self.create_arguments(context), "apply_direct")

    def expected_arn(self, context: StepContext) -> str:
        """The new instance's ARN, in the account and region of its cluster."""
        return sibling_arn_of(self.cluster.arn, self.resource_type, self.identifier)

    def exists(self, context: StepContext, step_name: str = "compute_change_set") -> bool:
        """Whether AWS has an instance with the reader's identifier."""
        return lifecycle_api.instance_describe(context, self.identifier, step_name) is not None

    def status(self, context: StepContext, step_name: str = "verify_direct") -> str | None:
        """The reader's status, or None while AWS does not list it."""
        return _instance_status(context, self.identifier, step_name)

    def review_entries(self, context: StepContext) -> list[tuple[str, Any]]:
        """Cluster, class, tier, engine version, parameter group and Performance Insights."""
        return [
            ("cluster", self.cluster.identifier),
            ("instance_class", self.options.instance_class),
            ("promotion_tier", self.options.promotion_tier),
            ("engine_version", self.cluster.facts.get("engine_version")),
            ("parameter_group", self.options.parameter_group_name or "the AWS default group"),
            ("performance_insights_enabled", self.options.performance_insights_enabled),
        ]


class ReadReplicaCreateExecutor(ResourceCreateExecutor):
    """Create a read replica of the operation's instance, in both modes."""

    operation_kind = "replica_create"

    def build_change(self, context: StepContext) -> ReadReplicaCreate:
        """The replica from `plan["options"]` and the source instance's record.

        Raises:
            KeyError: The source is not in the latest inventory.
            ValueError: The options are not valid.
        """
        options = ReadReplicaCreateOptions.model_validate(context.operation.plan.get("options", {}))
        return ReadReplicaCreate(context.target.target_key, _launching_record(context), options)


class ClusterReaderCreateExecutor(ResourceCreateExecutor):
    """Add a reader to the operation's Aurora cluster, in both modes."""

    operation_kind = "cluster_reader_add"

    def build_change(self, context: StepContext) -> ClusterReaderCreate:
        """The reader from `plan["options"]` and the cluster's record.

        Raises:
            KeyError: The cluster is not in the latest inventory.
            ValueError: The options are not valid.
        """
        options = ClusterReaderCreateOptions.model_validate(context.operation.plan.get("options", {}))
        return ClusterReaderCreate(context.target.target_key, _launching_record(context), options)


class ClusterMemberNotCreatedError(ChangeDiagnosticError):
    """A created cluster exists, but AWS refused to create one of its members."""

    diagnostic_kind = "cluster_member_not_created"
    title = "The new cluster is missing some of its instances"


def _parameter_group_reference(context: StepContext, resource_type: str, group_name: str) -> str:
    """A chosen parameter group: by address when the workspace renders it, else its quoted name."""
    if group_name.startswith(DEFAULT_PARAMETER_GROUP_PREFIX):
        return to_hcl(group_name)
    return _reference_or_literal(context, resource_type, group_name, "name")


def arn_basis_of(context: StepContext, scope: str, group_name: str) -> str | None:
    """The ARN of the chosen parameter group in the inventory, which a created resource's ARN is built beside.

    A create launched from the environment has no source to take its account from; the parameter group it
    uses is in the same account and region, as AWS (or the simulator) reports them.

    Args:
        context: The running step's context.
        scope: `instance` or `cluster`.
        group_name: The chosen group's name.

    Returns:
        The group's ARN, or None when the latest inventory does not have it.

    Example:
        >>> arn_basis_of(context, "instance", "demo-postgres16-tuned")  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:pg:demo-postgres16-tuned'
    """
    group = find_parameter_group(
        context.session, context.console_config, context.target.target_key, scope, group_name
    )
    return None if group is None else group.arn


class InstanceCreate(ResourceCreateChange):
    """A standalone PostgreSQL instance from the template (`INSTANCE_TEMPLATE_DEFAULTS`) and the options.

    The operation points at the new instance once it exists (`link_operation_to_created`).

    Attributes:
        options: The instance's settings.
        arn_basis: The ARN of a resource in the same account and region (the chosen parameter group), or None
            to build the new ARN from the target's configured account.

    Example:
        >>> InstanceCreate(TARGET_KEY, options, None).address  # doctest: +SKIP
        'aws_db_instance.demo_reports'
    """

    def __init__(self, target_key: str, options: InstanceCreateOptions, arn_basis: str | None) -> None:
        """Describe the instance; nothing is created until a strategy runs the change."""
        super().__init__(
            target_key,
            options.identifier,
            "aws_db_instance",
            f"create instance {options.identifier}",
            link_operation_to_created=True,
        )
        self.options = options
        self.arn_basis = arn_basis

    def rendered(self, context: StepContext) -> RenderedInstance:
        """The template's fixed settings with the chosen engine, class, storage, placement and access.

        No maintenance or backup window, KMS key or certificate is chosen, so AWS picks them.
        """
        options = self.options
        template = INSTANCE_TEMPLATE_DEFAULTS
        return RenderedInstance(
            terraform_name=terraform_resource_name(self.identifier),
            identifier=self.identifier,
            replicate_source_reference=None,
            engine="postgres",
            engine_version=options.engine_version,
            instance_class=options.instance_class,
            allocated_storage=options.allocated_storage_gb,
            max_allocated_storage=options.max_allocated_storage_gb or None,
            storage_type=options.storage_type,
            iops=None,
            storage_throughput=None,
            master_username=options.master_username,
            db_name=options.db_name,
            port=options.port,
            db_subnet_group_reference=_reference_or_literal(
                context, "aws_db_subnet_group", options.subnet_group_name, "name"
            ),
            vpc_security_group_ids=list(options.vpc_security_group_ids),
            parameter_group_reference=_parameter_group_reference(
                context, "aws_db_parameter_group", options.parameter_group_name
            ),
            multi_az=options.multi_az,
            publicly_accessible=options.publicly_accessible,
            backup_retention_period=template["backup_retention_period"],
            preferred_backup_window=None,
            preferred_maintenance_window=None,
            deletion_protection=template["deletion_protection"],
            storage_encrypted=template["storage_encrypted"],
            kms_key_id=None,
            performance_insights_enabled=template["performance_insights_enabled"],
            performance_insights_retention_period=None,
            monitoring_interval=template["monitoring_interval"],
            monitoring_role_arn=None,
            auto_minor_version_upgrade=template["auto_minor_version_upgrade"],
            copy_tags_to_snapshot=template["copy_tags_to_snapshot"],
            ca_cert_identifier=None,
            tags={},
            manage_master_user_password=template["manage_master_user_password"],
        )

    def _rendered_model(self, context: StepContext) -> RenderedInstance:
        """The prepared rendered instance, worked out now when the change was not prepared."""
        rendered = self.rendered_cached if self.rendered_cached is not None else self.rendered(context)
        assert isinstance(rendered, RenderedInstance)
        return rendered

    def create_arguments(self, context: StepContext) -> dict[str, Any]:
        """The `CreateDBInstance` request: everything the rendered instance carries, and never a password.

        `ManageMasterUserPassword` asks AWS to create the master password and keep it in Secrets Manager.
        """
        rendered = self._rendered_model(context)
        arguments: dict[str, Any] = {
            "DBInstanceIdentifier": rendered.identifier,
            "Engine": rendered.engine,
            "EngineVersion": rendered.engine_version,
            "DBInstanceClass": rendered.instance_class,
            "AllocatedStorage": rendered.allocated_storage,
            "StorageType": rendered.storage_type,
            "MasterUsername": rendered.master_username,
            "ManageMasterUserPassword": rendered.manage_master_user_password,
            "Port": rendered.port,
            "DBSubnetGroupName": self.options.subnet_group_name,
            "VpcSecurityGroupIds": list(rendered.vpc_security_group_ids),
            "DBParameterGroupName": self.options.parameter_group_name,
            "MultiAZ": rendered.multi_az,
            "PubliclyAccessible": rendered.publicly_accessible,
            "BackupRetentionPeriod": rendered.backup_retention_period,
            "StorageEncrypted": rendered.storage_encrypted,
            "DeletionProtection": rendered.deletion_protection,
            "EnablePerformanceInsights": rendered.performance_insights_enabled,
            "AutoMinorVersionUpgrade": rendered.auto_minor_version_upgrade,
            "CopyTagsToSnapshot": rendered.copy_tags_to_snapshot,
            "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
        }
        optional: dict[str, Any] = {
            "MaxAllocatedStorage": rendered.max_allocated_storage,
            "Iops": rendered.iops,
            "StorageThroughput": rendered.storage_throughput,
            "DBName": rendered.db_name,
            "PreferredBackupWindow": rendered.preferred_backup_window,
            "PreferredMaintenanceWindow": rendered.preferred_maintenance_window,
            "KmsKeyId": rendered.kms_key_id,
            "CACertificateIdentifier": rendered.ca_cert_identifier,
        }
        if rendered.performance_insights_enabled:
            optional["PerformanceInsightsRetentionPeriod"] = rendered.performance_insights_retention_period
        if rendered.monitoring_interval:
            optional["MonitoringInterval"] = rendered.monitoring_interval
            optional["MonitoringRoleArn"] = rendered.monitoring_role_arn
        arguments.update({key: value for key, value in optional.items() if value is not None})
        return arguments

    def create(self, context: StepContext) -> str:
        """`CreateDBInstance` with the template, the options and the console's tag."""
        return lifecycle_api.create_instance(context, self.create_arguments(context), "apply_direct")

    def create_only_fields(self) -> dict[str, Any]:
        """The managed master password, which the record keeps however AWS reports it."""
        return {"manage_master_user_password": INSTANCE_TEMPLATE_DEFAULTS["manage_master_user_password"]}

    def expected_arn(self, context: StepContext) -> str:
        """The new instance's ARN, beside the chosen parameter group's when the inventory has it."""
        if self.arn_basis:
            return sibling_arn_of(self.arn_basis, self.resource_type, self.identifier)
        return super().expected_arn(context)

    def exists(self, context: StepContext, step_name: str = "compute_change_set") -> bool:
        """Whether AWS has an instance with this identifier."""
        return lifecycle_api.instance_describe(context, self.identifier, step_name) is not None

    def status(self, context: StepContext, step_name: str = "verify_direct") -> str | None:
        """The new instance's status, or None while AWS does not list it."""
        return _instance_status(context, self.identifier, step_name)

    def review_entries(self, context: StepContext) -> list[tuple[str, Any]]:
        """Class, storage, engine version, placement, parameter group and access, for the review."""
        options = self.options
        entries: list[tuple[str, Any]] = [
            ("instance_class", options.instance_class),
            ("allocated_storage", options.allocated_storage_gb),
            ("storage_type", options.storage_type),
        ]
        if options.max_allocated_storage_gb:
            entries.append(("max_allocated_storage", options.max_allocated_storage_gb))
        return entries + [
            ("engine_version", options.engine_version),
            ("subnet_group", options.subnet_group_name),
            ("parameter_group", options.parameter_group_name),
            ("vpc_security_group_ids", list(options.vpc_security_group_ids)),
            ("multi_az", options.multi_az),
            ("publicly_accessible", options.publicly_accessible),
        ]


class ClusterCreate(ResourceCreateChange):
    """An Aurora PostgreSQL cluster built from the template, with its writer `<name>-1` and readers.

    The cluster is the change's own resource; its members are rendered and created with it, writer first, so
    the first member AWS has is the writer. The operation points at the cluster once it exists.

    Attributes:
        options: The cluster's settings.
        arn_basis: The ARN of a resource in the same account and region (the chosen cluster parameter group),
            or None to build the new ARNs from the target's configured account.
        member_identifiers: `<name>-1` (the writer) to `<name>-<1 + reader_count>`.
        member_addresses: Their Terraform addresses, in the same order.
        members_rendered_cached: Their rendered models once prepared.

    Example:
        >>> ClusterCreate(TARGET_KEY, options, None).member_identifiers  # doctest: +SKIP
        ['demo-analytics-1', 'demo-analytics-2']
    """

    def __init__(self, target_key: str, options: ClusterCreateOptions, arn_basis: str | None) -> None:
        """Describe the cluster and its members; nothing is created until a strategy runs the change."""
        members = options.member_identifiers()
        super().__init__(
            target_key,
            options.identifier,
            "aws_rds_cluster",
            f"create cluster {options.identifier} with {len(members)} instance(s)",
            link_operation_to_created=True,
        )
        self.options = options
        self.arn_basis = arn_basis
        self.member_identifiers = members
        self.member_addresses = [
            terraform_address_for("aws_rds_cluster_instance", member) for member in members
        ]
        self.members_rendered_cached: list[RenderedClusterInstance] | None = None

    def rendered(self, context: StepContext) -> RenderedCluster:
        """The template's fixed settings with the chosen engine version, placement, group and users."""
        options = self.options
        template = CLUSTER_TEMPLATE_DEFAULTS
        return RenderedCluster(
            terraform_name=terraform_resource_name(self.identifier),
            identifier=self.identifier,
            engine="aurora-postgresql",
            engine_version=options.engine_version,
            engine_mode=None,
            master_username=options.master_username,
            database_name=options.database_name,
            port=options.port,
            db_subnet_group_reference=_reference_or_literal(
                context, "aws_db_subnet_group", options.subnet_group_name, "name"
            ),
            vpc_security_group_ids=list(options.vpc_security_group_ids),
            cluster_parameter_group_reference=_parameter_group_reference(
                context, "aws_rds_cluster_parameter_group", options.cluster_parameter_group_name
            ),
            backup_retention_period=template["backup_retention_period"],
            preferred_backup_window=None,
            preferred_maintenance_window=None,
            storage_encrypted=template["storage_encrypted"],
            kms_key_id=None,
            deletion_protection=template["deletion_protection"],
            copy_tags_to_snapshot=template["copy_tags_to_snapshot"],
            auto_minor_version_upgrade=template["auto_minor_version_upgrade"],
            serverless_v2_min_capacity=None,
            serverless_v2_max_capacity=None,
            tags={},
            manage_master_user_password=template["manage_master_user_password"],
        )

    def _rendered_model(self, context: StepContext) -> RenderedCluster:
        """The prepared rendered cluster, worked out now when the change was not prepared."""
        rendered = self.rendered_cached if self.rendered_cached is not None else self.rendered(context)
        assert isinstance(rendered, RenderedCluster)
        return rendered

    def rendered_members(self, context: StepContext) -> list[RenderedClusterInstance]:
        """The writer, then the readers: the cluster's engine and version, the chosen classes, no tags.

        Each reader depends on the writer, so Terraform creates the writer first and it becomes the writer, as
        direct mode's ordered calls make it.
        """
        cluster = self._rendered_model(context)
        template = CLUSTER_MEMBER_TEMPLATE_DEFAULTS
        writer_address = self.member_addresses[0]
        members: list[RenderedClusterInstance] = []
        for position, identifier in enumerate(self.member_identifiers):
            instance_class = (
                self.options.writer_instance_class
                if position == 0
                else self.options.reader_instance_class or self.options.writer_instance_class
            )
            members.append(
                RenderedClusterInstance(
                    terraform_name=terraform_resource_name(identifier),
                    identifier=identifier,
                    cluster_reference=f"{self.address}.id",
                    instance_class=instance_class,
                    engine=cluster.engine,
                    engine_version=cluster.engine_version,
                    parameter_group_reference=None,
                    publicly_accessible=template["publicly_accessible"],
                    auto_minor_version_upgrade=template["auto_minor_version_upgrade"],
                    performance_insights_enabled=template["performance_insights_enabled"],
                    monitoring_interval=template["monitoring_interval"],
                    promotion_tier=template["promotion_tier"],
                    tags={},
                    depends_on_member=None if position == 0 else writer_address,
                )
            )
        return members

    def _members(self, context: StepContext) -> list[RenderedClusterInstance]:
        """The prepared members, worked out now when the change was not prepared."""
        if self.members_rendered_cached is not None:
            return self.members_rendered_cached
        return self.rendered_members(context)

    def create_arguments(self, context: StepContext) -> dict[str, Any]:
        """The `CreateDBCluster` request: everything the rendered cluster carries, and never a password.

        `ManageMasterUserPassword` asks AWS to create the master password and keep it in Secrets Manager.
        Automatic minor upgrades are a member setting for Aurora, so each member's request carries them.
        """
        rendered = self._rendered_model(context)
        arguments: dict[str, Any] = {
            "DBClusterIdentifier": rendered.identifier,
            "Engine": rendered.engine,
            "EngineVersion": rendered.engine_version,
            "MasterUsername": rendered.master_username,
            "ManageMasterUserPassword": rendered.manage_master_user_password,
            "DBSubnetGroupName": self.options.subnet_group_name,
            "VpcSecurityGroupIds": list(rendered.vpc_security_group_ids),
            "DBClusterParameterGroupName": self.options.cluster_parameter_group_name,
            "BackupRetentionPeriod": rendered.backup_retention_period,
            "StorageEncrypted": rendered.storage_encrypted,
            "DeletionProtection": rendered.deletion_protection,
            "CopyTagsToSnapshot": rendered.copy_tags_to_snapshot,
            "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
        }
        optional: dict[str, Any] = {
            "EngineMode": rendered.engine_mode,
            "DatabaseName": rendered.database_name,
            "Port": rendered.port,
            "PreferredBackupWindow": rendered.preferred_backup_window,
            "PreferredMaintenanceWindow": rendered.preferred_maintenance_window,
            "KmsKeyId": rendered.kms_key_id,
        }
        if rendered.serverless_v2_min_capacity is not None:
            optional["ServerlessV2ScalingConfiguration"] = {
                "MinCapacity": rendered.serverless_v2_min_capacity,
                "MaxCapacity": rendered.serverless_v2_max_capacity,
            }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        return arguments

    def member_arguments(self, context: StepContext, member: RenderedClusterInstance) -> dict[str, Any]:
        """The `CreateDBInstance` request of one member; its engine version is the cluster's, so not sent."""
        arguments: dict[str, Any] = {
            "DBInstanceIdentifier": member.identifier,
            "DBClusterIdentifier": self.identifier,
            "Engine": member.engine,
            "DBInstanceClass": member.instance_class,
            "PromotionTier": member.promotion_tier,
            "PubliclyAccessible": member.publicly_accessible,
            "AutoMinorVersionUpgrade": member.auto_minor_version_upgrade,
            "EnablePerformanceInsights": member.performance_insights_enabled,
            "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
        }
        if member.monitoring_interval:
            arguments["MonitoringInterval"] = member.monitoring_interval
        return arguments

    def create(self, context: StepContext) -> str:
        """`CreateDBCluster`; its members are created next by `apply_direct`."""
        return lifecycle_api.create_cluster(context, self.create_arguments(context), "apply_direct")

    def create_only_fields(self) -> dict[str, Any]:
        """The managed master password, which the record keeps however AWS reports it."""
        return {"manage_master_user_password": CLUSTER_TEMPLATE_DEFAULTS["manage_master_user_password"]}

    # --- the DesiredChange protocol, widened to the members ------------------------------------------

    def prepare(self, context: StepContext) -> None:
        """Work out the cluster's model and its members'."""
        super().prepare(context)
        self.members_rendered_cached = self.rendered_members(context)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """The cluster and every member."""
        return {self.address, *self.member_addresses}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """The render input with the cluster and its members added (a copy).

        Raises:
            ValueError: `prepare` was not called first.
        """
        updated = super().apply_to_render_input(render_input)
        if self.members_rendered_cached is None:
            raise ValueError(f"the create of {self.identifier} was not prepared; call prepare(context) first")
        members = getattr(updated, RESOURCE_SECTIONS["aws_rds_cluster_instance"])
        members[:] = [member for member in members if member.identifier not in self.member_identifiers]
        members.extend(member.model_copy(deep=True) for member in self.members_rendered_cached)
        members.sort(key=lambda member: member.identifier)
        return updated

    def expected_arn(self, context: StepContext) -> str:
        """The new cluster's ARN, beside the chosen cluster parameter group's when the inventory has it."""
        if self.arn_basis:
            return sibling_arn_of(self.arn_basis, self.resource_type, self.identifier)
        return super().expected_arn(context)

    def _member_arn(self, context: StepContext, identifier: str) -> str:
        """A member's ARN, beside the cluster's."""
        return sibling_arn_of(self.expected_arn(context), "aws_rds_cluster_instance", identifier)

    def expected_arns(self, context: StepContext) -> list[str]:
        """The cluster's ARN, then each member's, writer first."""
        return [self.expected_arn(context)] + [
            self._member_arn(context, identifier) for identifier in self.member_identifiers
        ]

    def taken_identifier(self, context: StepContext, step_name: str = "compute_change_set") -> str | None:
        """The cluster's identifier when AWS has it, else the first member name an instance already has."""
        if lifecycle_api.cluster_describe(context, self.identifier, step_name) is not None:
            return self.identifier
        return next(
            (
                identifier
                for identifier in self.member_identifiers
                if lifecycle_api.instance_describe(context, identifier, step_name) is not None
            ),
            None,
        )

    def exists(self, context: StepContext, step_name: str = "compute_change_set") -> bool:
        """Whether AWS already has the cluster, or an instance with one of its members' names."""
        return self.taken_identifier(context, step_name) is not None

    def status(self, context: StepContext, step_name: str = "verify_direct") -> str | None:
        """`available` once the cluster and every member are; else the first status still waited on."""
        cluster = lifecycle_api.cluster_describe(context, self.identifier, step_name)
        if cluster is None or cluster["Status"] != AVAILABLE_STATUS:
            return None if cluster is None else cluster["Status"]
        for identifier in self.member_identifiers:
            member_status = _instance_status(context, identifier, step_name)
            if member_status != AVAILABLE_STATUS:
                return member_status
        return AVAILABLE_STATUS

    def review_entries(self, context: StepContext) -> list[tuple[str, Any]]:
        """Engine version, placement, cluster parameter group and the members, for the review."""
        options = self.options
        return [
            ("engine_version", options.engine_version),
            ("subnet_group", options.subnet_group_name),
            ("cluster_parameter_group", options.cluster_parameter_group_name),
            ("vpc_security_group_ids", list(options.vpc_security_group_ids)),
            ("writer", self.member_identifiers[0]),
            ("reader_count", options.reader_count),
        ]

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """The cluster's presence and settings, then each member's presence and class, writer first."""
        entries = super().change_set(context)
        for address, member in zip(self.member_addresses, self._members(context), strict=True):
            entries.append(
                ChangeSetEntry(
                    address=address,
                    attribute="presence",
                    current=None,
                    desired=f"aws_rds_cluster_instance {member.identifier}",
                    apply_type="immediate",
                    requires_reboot=False,
                    downtime=NEW_RESOURCE_DOWNTIME,
                )
            )
            entries.append(
                ChangeSetEntry(
                    address=address,
                    attribute="instance_class",
                    current=None,
                    desired=member.instance_class,
                    apply_type="immediate",
                    requires_reboot=False,
                    downtime="none",
                )
            )
        return entries

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Create the cluster, then each member, writer first; each is listed among the created resources.

        The cluster is listed before any member is asked for, so history attributes a cluster left without
        some of its members to this operation.

        Raises:
            ClusterMemberNotCreatedError: The cluster exists but AWS refused a member; the operation is marked
                `applied_without_record` and the fix suggestion says how to recover.
        """
        submitted = super().apply_direct(context, entries)
        created_members: list[str] = []
        for member in self._members(context):
            try:
                member_arn = lifecycle_api.create_cluster_member(
                    context, self.member_arguments(context, member), "apply_direct"
                )
            except (ClientError, BotoCoreError) as error:
                self._fail_for_missing_member(context, member.identifier, created_members, error)
            record_created_resource(context, member_arn, link_operation_to_created=False)
            created_members.append(member.identifier)
            logger.info(
                "cluster_member_create_submitted",
                operation_id=str(getattr(context.operation, "id", "")),
                cluster_identifier=self.identifier,
                member_arn=member_arn,
            )
        return {**submitted, "member_identifiers": created_members}

    def _fail_for_missing_member(
        self, context: StepContext, missing: str, created_members: list[str], error: Exception
    ) -> NoReturn:
        """Mark the operation `applied_without_record`, log, and raise with how to recover.

        Raises:
            ClusterMemberNotCreatedError: Always.
        """
        context.operation.plan = {**context.operation.plan, "applied_without_record": True}
        context.session.commit()
        not_created = self.member_identifiers[len(created_members) :]
        if not created_members:
            fix_suggestion = (
                f"the cluster {self.identifier} exists without a writer; run Refresh inventory, then Add "
                f"reader on it to create its first member ({missing}), or Decommission it"
            )
        else:
            fix_suggestion = (
                f"the cluster {self.identifier} exists with {', '.join(created_members)} but without "
                f"{', '.join(not_created)}; run Refresh inventory, then Add reader on it for each one missing"
            )
        logger.error(
            "cluster_member_not_created",
            operation_id=str(getattr(context.operation, "id", "")),
            cluster_identifier=self.identifier,
            member_identifier=missing,
            error=str(error),
            fix_suggestion=fix_suggestion,
        )
        raise ClusterMemberNotCreatedError(
            f"{self.identifier} was created, but creating its instance {missing} failed: {error}",
            fix_suggestion,
        ) from error


class InstanceCreateExecutor(ResourceCreateExecutor):
    """Create an instance from the template in the operation's target, in both modes."""

    operation_kind = "instance_create"

    def build_change(self, context: StepContext) -> InstanceCreate:
        """The instance from `plan["options"]`.

        Raises:
            ValueError: The options are not valid.
        """
        options = InstanceCreateOptions.model_validate(context.operation.plan.get("options", {}))
        return InstanceCreate(
            context.target.target_key,
            options,
            arn_basis_of(context, "instance", options.parameter_group_name),
        )


class ClusterCreateExecutor(ResourceCreateExecutor):
    """Create an Aurora cluster with its writer and readers from the template, in both modes."""

    operation_kind = "cluster_create"

    def build_change(self, context: StepContext) -> ClusterCreate:
        """The cluster and its members from `plan["options"]`.

        Raises:
            ValueError: The options are not valid.
        """
        options = ClusterCreateOptions.model_validate(context.operation.plan.get("options", {}))
        return ClusterCreate(
            context.target.target_key,
            options,
            arn_basis_of(context, "cluster", options.cluster_parameter_group_name),
        )
