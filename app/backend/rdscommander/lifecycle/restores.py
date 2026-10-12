"""Restore a snapshot, or an instance or cluster to a point in time, as a new resource, in both modes.

A restore is a `ResourceCreateChange` launched from the resource it reads: the operation stays on the snapshot
and lists what it created in `plan["created_resource_arns"]`. An instance snapshot becomes one instance; a
cluster snapshot becomes a cluster plus its first instance, the writer `<name>-1`, since an Aurora cluster
serves nothing without one. Terraform mode renders the new resources with `snapshot_identifier` (which
the renderer tells Terraform to ignore after the create); direct mode sends `RestoreDBInstanceFromDBSnapshot`,
or `RestoreDBClusterFromSnapshot` and then `CreateDBInstance` for the writer, each request built from the
rendered model so both modes create the same resource. Either way the record keeps `snapshot_identifier`,
which AWS does not report (`create_only_fields`).

A point-in-time restore is launched from the instance or cluster it reads, and renders the provider's
`restore_to_point_in_time` block instead; direct mode sends `RestoreDBInstanceToPointInTime`, or
`RestoreDBClusterToPointInTime` and the writer's `CreateDBInstance`. The time is checked against the source's
restore window when the operation is created (`restore_window`); when the inventory cannot say what the window
is, the executor records a `restore_window_unknown` warning on the operation instead.

Every restore asks AWS to keep the restored master password in Secrets Manager, so the console never holds
one: an instance restore sends `ManageMasterUserPassword=True` on the restore call; no cluster restore call
takes it, so direct mode sends it in the modify after the restore (`post_create_modify_request`), and
Terraform mode renders `manage_master_user_password = true`, which the provider applies after the restore.

Example:
    >>> restore = SnapshotRestoreCreate(TARGET_KEY, snapshot_record, options)  # doctest: +SKIP
    >>> restore.touched_addresses(context)  # doctest: +SKIP
    {'aws_db_instance.demo_orders_restored'}
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from rdscommander.control_db.models import DiagnosticReport
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.lifecycle import creates
from rdscommander.lifecycle import direct_api as lifecycle_api
from rdscommander.lifecycle.creates import (
    DEFAULT_PARAMETER_GROUP_PREFIX,
    _instance_status,
    _launching_record,
    _reference_or_literal,
)
from rdscommander.lifecycle.options import PointInTimeRestoreOptions, SnapshotRestoreOptions
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeDiagnosticError, ChangeSetEntry
from rdscommander.operations.create_change import (
    AVAILABLE_STATUS,
    CONSOLE_MANAGED_TAGS,
    NEW_RESOURCE_DOWNTIME,
    POST_CREATE_VALUES_APPLIED_PLAN_KEY,
    PostCreateSettingsNotAppliedError,
    RenderedResource,
    ResourceCreateChange,
    ResourceCreateExecutor,
    record_created_resource,
    sibling_arn_of,
)
from rdscommander.operations.executor import StepContext, StepOutcome
from rdscommander.parameters.direct_api import resource_modify_request
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.terraform.hcl import to_hcl
from rdscommander.terraform.names import terraform_address_for, terraform_resource_name
from rdscommander.terraform.record import RESOURCE_SECTIONS
from rdscommander.terraform.render_input import (
    RenderedCluster,
    RenderedClusterInstance,
    RenderedInstance,
    RenderWorkspaceInput,
    RestorePointInTime,
)

logger = get_logger(__name__)

RestoreScope = Literal["instance", "cluster"]
"""Whether a restore creates an instance, or an Aurora cluster with its writer."""
WRITER_IDENTIFIER_SUFFIX = "-1"
"""A restored cluster's first instance is named `<cluster>-1`."""
RESTORED_BACKUP_RETENTION_DAYS = 7
"""Automated backups a snapshot restore keeps, as a created instance or cluster does."""
WRITER_PROMOTION_TIER = 1
"""Failover priority of a restored cluster's writer, AWS's default for a new member."""
RESTORE_WINDOW_UNKNOWN_PLAN_KEY = "restore_window_unknown"
"""Plan key the point-in-time restore's preparer sets, with the reason, when the window could not be read."""
RESTORE_WINDOW_PLAN_KEY = "restore_window"
"""Plan key holding the window the time was checked against: `{earliest_utc, latest_restorable_utc}`."""
RESTORE_WINDOW_UNKNOWN_DIAGNOSTIC = "restore_window_unknown"
LATEST_RESTORABLE_TIME = "the latest restorable time"
MANAGE_RESTORED_MASTER_USER_PASSWORD = True
"""Every restore asks AWS to keep the restored master password in Secrets Manager (spec 8.5)."""
MANAGED_MASTER_PASSWORD_REVIEW = (
    "a new password AWS creates and keeps in Secrets Manager; the console never sees it, and the source's "
    "password does not work on the restored copy"
)
"""What the review says of the restored master password (`manage_master_user_password`), for the approver."""
__all__ = [
    "LATEST_RESTORABLE_TIME",
    "MANAGE_RESTORED_MASTER_USER_PASSWORD",
    "MANAGED_MASTER_PASSWORD_REVIEW",
    "RestoreWriterNotCreatedError",
    "PointInTimeRestoreCreate",
    "PointInTimeRestoreExecutor",
    "RESTORE_WINDOW_PLAN_KEY",
    "RESTORE_WINDOW_UNKNOWN_PLAN_KEY",
    "RestoreCreate",
    "SnapshotRestoreCreate",
    "SnapshotRestoreExecutor",
    "writer_identifier_of",
]


class RestoreOptions(Protocol):
    """What every restore's options carry (`SnapshotRestoreOptions`, `PointInTimeRestoreOptions`)."""

    new_identifier: str
    instance_class: str
    subnet_group_name: str
    parameter_group_name: str | None
    vpc_security_group_ids: list[str]
    deletion_protection: bool


class RestoreWriterNotCreatedError(ChangeDiagnosticError):
    """A restored cluster exists, but AWS refused to create its first instance."""

    diagnostic_kind = "restore_writer_not_created"
    title = "The restored cluster has no writer"


def writer_identifier_of(cluster_identifier: str) -> str:
    """The identifier of a restored cluster's writer.

    Example:
        >>> writer_identifier_of("demo-aurora-restored")
        'demo-aurora-restored-1'
    """
    return f"{cluster_identifier}{WRITER_IDENTIFIER_SUFFIX}"


def _encryption_key_of(raw: dict[str, Any]) -> str | None:
    """The KMS key ARN of an encrypted snapshot, instance or cluster; None when unencrypted or not an ARN.

    The restored resource is encrypted with this key; the renderer writes `kms_key_id` only for an ARN.
    """
    encrypted = bool(raw.get("Encrypted") or raw.get("StorageEncrypted"))
    key = raw.get("KmsKeyId")
    return key if encrypted and isinstance(key, str) and key.startswith("arn:") else None


class RestoreCreate(ResourceCreateChange):
    """Base for restores: one instance, or an Aurora cluster plus its writer `<name>-1`.

    Subclasses build the rendered instance or cluster (`rendered_instance`, `rendered_cluster`), the restore
    request (`create_arguments`) and the restore call (`create`). The writer of a cluster restore is rendered
    and created here, from the restored cluster's engine and version and the options' class.

    Attributes:
        launching: The record the restore was launched from (a snapshot, or the instance or cluster restored).
        scope: `instance` or `cluster`.
        options: The restore's settings (`RestoreOptions`).
        writer_identifier: The writer's identifier for a cluster restore, else None.
        writer_address: The writer's Terraform address for a cluster restore, else None.
        writer_rendered_cached: The writer's rendered model once prepared.
    """

    post_create_fields_by_scope: dict[str, tuple[str, ...]] = {
        "instance": (
            "performance_insights_enabled",
            "performance_insights_retention_period",
            "monitoring_interval",
            "monitoring_role_arn",
            "preferred_maintenance_window",
        ),
        "cluster": ("preferred_maintenance_window",),
    }
    """Rendered fields the restore calls take no parameter for, sent by the modify after the restore."""

    def __init__(
        self,
        target_key: str,
        launching: ResourceRecord,
        scope: RestoreScope,
        options: RestoreOptions,
        summary: str,
    ) -> None:
        """Describe the restore; nothing is created until a strategy runs the change."""
        identifier = options.new_identifier
        super().__init__(
            target_key, identifier, "aws_db_instance" if scope == "instance" else "aws_rds_cluster", summary
        )
        self.launching = launching
        self.scope: RestoreScope = scope
        self.options = options
        self.writer_identifier = writer_identifier_of(identifier) if scope == "cluster" else None
        self.writer_address = (
            terraform_address_for("aws_rds_cluster_instance", self.writer_identifier)
            if self.writer_identifier
            else None
        )
        self.writer_rendered_cached: RenderedClusterInstance | None = None

    # --- what subclasses provide --------------------------------------------------------------------

    def rendered_instance(self, context: StepContext) -> RenderedInstance:
        """The restored instance's rendered model (instance scope)."""
        raise NotImplementedError

    def rendered_cluster(self, context: StepContext) -> RenderedCluster:
        """The restored cluster's rendered model (cluster scope)."""
        raise NotImplementedError

    def create_arguments(self, context: StepContext) -> dict[str, Any]:
        """The restore request, built from the rendered model."""
        raise NotImplementedError

    # --- shared rendering ---------------------------------------------------------------------------

    def rendered(self, context: StepContext) -> RenderedResource:
        """The restored instance or cluster."""
        if self.scope == "instance":
            return self.rendered_instance(context)
        return self.rendered_cluster(context)

    def subnet_group_reference(self, context: StepContext) -> str:
        """The subnet group: by address when the workspace renders it, else its quoted name."""
        return _reference_or_literal(context, "aws_db_subnet_group", self.options.subnet_group_name, "name")

    def parameter_group_reference(self, context: StepContext) -> str | None:
        """The chosen instance or cluster parameter group, by address or name; None for AWS's default."""
        group_name = self.options.parameter_group_name
        if group_name is None:
            return None
        if group_name.startswith(DEFAULT_PARAMETER_GROUP_PREFIX):
            return to_hcl(group_name)
        group_type = (
            "aws_db_parameter_group" if self.scope == "instance" else "aws_rds_cluster_parameter_group"
        )
        return _reference_or_literal(context, group_type, group_name, "name")

    def _rendered_model(self, context: StepContext) -> RenderedResource:
        """The prepared rendered instance or cluster, worked out now when the change was not prepared."""
        return self.rendered_cached if self.rendered_cached is not None else self.rendered(context)

    def rendered_writer(self, context: StepContext) -> RenderedClusterInstance:
        """The restored cluster's writer: the cluster's engine and version, the chosen class, no tags.

        Raises:
            ValueError: The restore is of an instance, which has no writer.
        """
        if self.writer_identifier is None:
            raise ValueError("an instance restore has no cluster writer")
        cluster = self._rendered_model(context)
        assert isinstance(cluster, RenderedCluster)
        return RenderedClusterInstance(
            terraform_name=terraform_resource_name(self.writer_identifier),
            identifier=self.writer_identifier,
            cluster_reference=f"{self.address}.id",
            instance_class=self.options.instance_class,
            engine=cluster.engine,
            engine_version=cluster.engine_version,
            parameter_group_reference=None,
            publicly_accessible=bool(getattr(self.options, "publicly_accessible", False)),
            auto_minor_version_upgrade=cluster.auto_minor_version_upgrade,
            performance_insights_enabled=True,
            monitoring_interval=0,
            promotion_tier=WRITER_PROMOTION_TIER,
            tags={},
        )

    def writer_arguments(self, context: StepContext) -> dict[str, Any]:
        """The `CreateDBInstance` request of the writer: what its rendered model carries that the call takes.

        The member's engine version is the cluster's, so it is not sent.
        """
        writer = self.writer_rendered_cached or self.rendered_writer(context)
        arguments: dict[str, Any] = {
            "DBInstanceIdentifier": writer.identifier,
            "DBClusterIdentifier": self.identifier,
            "Engine": writer.engine,
            "DBInstanceClass": writer.instance_class,
            "PromotionTier": writer.promotion_tier,
            "PubliclyAccessible": writer.publicly_accessible,
            "AutoMinorVersionUpgrade": writer.auto_minor_version_upgrade,
            "EnablePerformanceInsights": writer.performance_insights_enabled,
            "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
        }
        if writer.monitoring_interval:
            arguments["MonitoringInterval"] = writer.monitoring_interval
        return arguments

    # --- the DesiredChange protocol, widened to the writer -------------------------------------------

    def prepare(self, context: StepContext) -> None:
        """Work out the restored resource's model, and its writer's for a cluster."""
        super().prepare(context)
        if self.writer_identifier is not None:
            self.writer_rendered_cached = self.rendered_writer(context)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """The restored resource, and its writer for a cluster."""
        return {self.address} | ({self.writer_address} if self.writer_address else set())

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """The render input with the restored resource added, and its writer for a cluster (a copy).

        Raises:
            ValueError: `prepare` was not called first.
        """
        updated = super().apply_to_render_input(render_input)
        if self.writer_identifier is None:
            return updated
        if self.writer_rendered_cached is None:
            raise ValueError(
                f"the restore of {self.identifier} was not prepared; call prepare(context) first"
            )
        members = getattr(updated, RESOURCE_SECTIONS["aws_rds_cluster_instance"])
        members[:] = [member for member in members if member.identifier != self.writer_identifier]
        members.append(self.writer_rendered_cached.model_copy(deep=True))
        members.sort(key=lambda member: member.identifier)
        return updated

    def expected_arn(self, context: StepContext) -> str:
        """The restored resource's ARN, in the account and region of what it was launched from."""
        return sibling_arn_of(self.launching.arn, self.resource_type, self.identifier)

    def expected_arns(self, context: StepContext) -> list[str]:
        """The restored resource's ARN, then its writer's for a cluster."""
        arns = [self.expected_arn(context)]
        if self.writer_identifier is not None:
            arns.append(
                sibling_arn_of(self.launching.arn, "aws_rds_cluster_instance", self.writer_identifier)
            )
        return arns

    def taken_identifier(self, context: StepContext, step_name: str = "compute_change_set") -> str | None:
        """The restored resource's identifier when AWS has it, else the writer's when an instance has that."""
        if self.scope == "instance":
            taken = lifecycle_api.instance_describe(context, self.identifier, step_name) is not None
            return self.identifier if taken else None
        if lifecycle_api.cluster_describe(context, self.identifier, step_name) is not None:
            return self.identifier
        assert self.writer_identifier is not None
        if lifecycle_api.instance_describe(context, self.writer_identifier, step_name) is not None:
            return self.writer_identifier
        return None

    def exists(self, context: StepContext, step_name: str = "compute_change_set") -> bool:
        """Whether AWS already has the restored resource, or an instance with its writer's name."""
        return self.taken_identifier(context, step_name) is not None

    def status(self, context: StepContext, step_name: str = "verify_direct") -> str | None:
        """`available` once the instance, or the cluster and its writer, are; else the status waited on."""
        if self.scope == "instance":
            return _instance_status(context, self.identifier, step_name)
        cluster = lifecycle_api.cluster_describe(context, self.identifier, step_name)
        if cluster is None or cluster["Status"] != AVAILABLE_STATUS:
            return None if cluster is None else cluster["Status"]
        assert self.writer_identifier is not None
        return _instance_status(context, self.writer_identifier, step_name)

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """The restored resource's presence and settings, then the writer's presence and class."""
        entries = super().change_set(context)
        if self.writer_address is None:
            return entries
        entries.append(
            ChangeSetEntry(
                address=self.writer_address,
                attribute="presence",
                current=None,
                desired=f"aws_rds_cluster_instance {self.writer_identifier}",
                apply_type="immediate",
                requires_reboot=False,
                downtime=NEW_RESOURCE_DOWNTIME,
            )
        )
        entries.append(
            ChangeSetEntry(
                address=self.writer_address,
                attribute="instance_class",
                current=None,
                desired=self.options.instance_class,
                apply_type="immediate",
                requires_reboot=False,
                downtime="none",
            )
        )
        return entries

    def post_create_attribute_values(self, context: StepContext) -> dict[str, Any]:
        """The rendered settings no restore call takes (`post_create_fields_by_scope`), when set.

        Monitoring is sent only when it is on (an interval above 0), with its role.
        """
        rendered = self._rendered_model(context)
        values: dict[str, Any] = {}
        for field_name in self.post_create_fields_by_scope[self.scope]:
            value = getattr(rendered, field_name)
            if value is None:
                continue
            monitoring_off = not getattr(rendered, "monitoring_interval", 0)
            if field_name in ("monitoring_interval", "monitoring_role_arn") and monitoring_off:
                continue
            values[field_name] = value
        return values

    def post_create_modify_request(self, context: StepContext) -> dict[str, Any]:
        """The request fields of the modify sent once the restored resource is available (identifier aside).

        The settings no restore call takes (`post_create_attribute_values`), and for a cluster
        `ManageMasterUserPassword`: no cluster restore call takes it, so this modify hands the restored
        password to Secrets Manager. An instance restore asks for it on the restore call itself.

        Example:
            >>> cluster_restore.post_create_modify_request(context)  # doctest: +SKIP
            {'PreferredMaintenanceWindow': 'sun:03:00-sun:03:30', 'ManageMasterUserPassword': True}
        """
        request = resource_modify_request(self.resource_type, self.post_create_attribute_values(context))
        rendered = self._rendered_model(context)
        if self.scope == "cluster" and rendered.manage_master_user_password:
            request["ManageMasterUserPassword"] = True
        return request

    def _apply_post_create_values(self, context: StepContext) -> dict[str, Any]:
        """Send the follow-up modify once; a cluster's is one `ModifyDBCluster` that manages the password too.

        An instance's goes the way every create's does. A cluster's is sent as `post_create_modify_request`
        builds it; the plan keeps only the attribute values, which verification reads back.

        Raises:
            PostCreateSettingsNotAppliedError: AWS refused the modify; the operation is marked
                `applied_without_record`, since the cluster exists without the settings.
        """
        if self.scope == "instance" or POST_CREATE_VALUES_APPLIED_PLAN_KEY in context.operation.plan:
            return super()._apply_post_create_values(context)
        values = self.post_create_attribute_values(context)
        request = self.post_create_modify_request(context)
        if request:
            rds = context.services.aws_sessions.client(
                context.target, "rds", operation_id=context.operation.id, step_name="verify_direct"
            )
            try:
                rds.modify_db_cluster(DBClusterIdentifier=self.identifier, ApplyImmediately=True, **request)
            except (ClientError, BotoCoreError) as error:
                context.operation.plan = {**context.operation.plan, "applied_without_record": True}
                context.session.commit()
                what = ", ".join(sorted(request))
                fix_suggestion = (
                    f"{self.identifier} was restored without {what}; set them on the cluster in AWS (the "
                    "master password: Manage master credentials in AWS Secrets Manager), then run Refresh "
                    "inventory and Detect drift"
                )
                logger.error(
                    "post_create_settings_not_applied",
                    operation_id=str(getattr(context.operation, "id", "")),
                    address=self.address,
                    request_fields=sorted(request),
                    error=str(error),
                    fix_suggestion=fix_suggestion,
                )
                raise PostCreateSettingsNotAppliedError(
                    f"{self.identifier} was restored, but AWS refused the modify that sets {what}: {error}",
                    fix_suggestion,
                ) from error
            logger.info(
                "post_create_values_applied",
                operation_id=str(getattr(context.operation, "id", "")),
                address=self.address,
                request_fields=sorted(request),
            )
        context.operation.plan = {**context.operation.plan, POST_CREATE_VALUES_APPLIED_PLAN_KEY: dict(values)}
        context.session.commit()
        return values

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Restore, then create a cluster's writer; both are listed among the created resources.

        The restored resource is listed before the writer is asked for, so history attributes a cluster
        left without a writer to this operation.

        Raises:
            RestoreWriterNotCreatedError: The cluster was restored but AWS refused its writer; the operation
                is marked `applied_without_record` and the fix suggestion says how to recover.
        """
        submitted = super().apply_direct(context, entries)
        if self.writer_identifier is None:
            return submitted
        try:
            writer_arn = lifecycle_api.create_cluster_member(
                context, self.writer_arguments(context), "apply_direct"
            )
        except (ClientError, BotoCoreError) as error:
            context.operation.plan = {**context.operation.plan, "applied_without_record": True}
            context.session.commit()
            fix_suggestion = (
                f"the cluster {self.identifier} exists without a writer; run Refresh inventory, then Add "
                "Aurora reader on it to create its first member, or Decommission it"
            )
            logger.error(
                "restored_cluster_writer_not_created",
                operation_id=str(getattr(context.operation, "id", "")),
                cluster_identifier=self.identifier,
                writer_identifier=self.writer_identifier,
                error=str(error),
                fix_suggestion=fix_suggestion,
            )
            raise RestoreWriterNotCreatedError(
                f"{self.identifier} was restored, but creating its writer {self.writer_identifier} failed: "
                f"{error}",
                fix_suggestion,
            ) from error
        record_created_resource(context, writer_arn, link_operation_to_created=False)
        logger.info(
            "restored_cluster_writer_submitted",
            operation_id=str(getattr(context.operation, "id", "")),
            cluster_identifier=self.identifier,
            writer_arn=writer_arn,
        )
        return {**submitted, "writer_arn": writer_arn}

    def _placement_review_entries(self) -> list[tuple[str, Any]]:
        """Subnet group, parameter group, security groups, deletion protection and the master password.

        The master password closes the list: every restore asks AWS for a new one in Secrets Manager, so the
        approver learns that the source's password will not work on the restored copy.
        """
        return [
            ("subnet_group", self.options.subnet_group_name),
            ("parameter_group", self.options.parameter_group_name or "the AWS default group"),
            ("vpc_security_group_ids", list(self.options.vpc_security_group_ids)),
            ("deletion_protection", self.options.deletion_protection),
            ("manage_master_user_password", MANAGED_MASTER_PASSWORD_REVIEW),
        ]


class SnapshotRestoreCreate(RestoreCreate):
    """A new instance from an instance snapshot, or a new Aurora cluster and writer from a cluster snapshot.

    Attributes:
        snapshot: The record of the snapshot restored.

    Example:
        >>> SnapshotRestoreCreate(TARGET_KEY, snapshot_record, options).summary  # doctest: +SKIP
        'restore demo-orders-before-change to demo-orders-restored'
    """

    def __init__(self, target_key: str, snapshot: ResourceRecord, options: SnapshotRestoreOptions) -> None:
        """Describe the restore; nothing is created until a strategy runs the change."""
        scope: RestoreScope = "cluster" if snapshot.facts.get("snapshot_scope") == "cluster" else "instance"
        super().__init__(
            target_key,
            snapshot,
            scope,
            options,
            f"restore {snapshot.identifier} to {options.new_identifier}",
        )
        self.snapshot = snapshot
        self.restore_options = options

    def _engine(self) -> str:
        default_engine = "aurora-postgresql" if self.scope == "cluster" else "postgres"
        return str(self.snapshot.raw.get("Engine") or default_engine)

    def _engine_version(self) -> str:
        return str(self.snapshot.facts.get("engine_version") or self.snapshot.raw.get("EngineVersion") or "")

    def _port(self) -> int | None:
        port = self.snapshot.raw.get("Port")
        return port if isinstance(port, int) and port > 0 else None

    def create_only_fields(self) -> dict[str, Any]:
        """The snapshot the resource was restored from, and the managed password, as the record keeps them."""
        return {
            "snapshot_identifier": self.snapshot.identifier,
            "manage_master_user_password": MANAGE_RESTORED_MASTER_USER_PASSWORD,
        }

    def rendered_instance(self, context: StepContext) -> RenderedInstance:
        """The snapshot's engine, version, storage, users and encryption, with the chosen placement and class.

        Storage type, IOPS and throughput are left to the snapshot (sent, they could be refused, as a gp3
        snapshot under 400 GiB reports its baseline IOPS). Backups, Performance Insights, automatic minor
        upgrades and copying tags to snapshots are on, as on every instance the console creates.
        """
        options = self.restore_options
        return RenderedInstance(
            terraform_name=terraform_resource_name(self.identifier),
            identifier=self.identifier,
            replicate_source_reference=None,
            engine=self._engine(),
            engine_version=self._engine_version(),
            instance_class=options.instance_class,
            allocated_storage=self.snapshot.facts.get("allocated_storage_gb"),
            max_allocated_storage=None,
            storage_type=None,
            iops=None,
            storage_throughput=None,
            master_username=self.snapshot.raw.get("MasterUsername"),
            db_name=None,
            port=self._port(),
            db_subnet_group_reference=self.subnet_group_reference(context),
            vpc_security_group_ids=list(options.vpc_security_group_ids),
            parameter_group_reference=self.parameter_group_reference(context),
            multi_az=options.multi_az,
            publicly_accessible=options.publicly_accessible,
            backup_retention_period=RESTORED_BACKUP_RETENTION_DAYS,
            preferred_backup_window=None,
            preferred_maintenance_window=None,
            deletion_protection=options.deletion_protection,
            storage_encrypted=bool(self.snapshot.raw.get("Encrypted")),
            kms_key_id=_encryption_key_of(self.snapshot.raw),
            performance_insights_enabled=True,
            performance_insights_retention_period=None,
            monitoring_interval=0,
            monitoring_role_arn=None,
            auto_minor_version_upgrade=True,
            copy_tags_to_snapshot=True,
            ca_cert_identifier=None,
            tags={},
            snapshot_identifier=self.snapshot.identifier,
            manage_master_user_password=MANAGE_RESTORED_MASTER_USER_PASSWORD,
        )

    def rendered_cluster(self, context: StepContext) -> RenderedCluster:
        """The cluster snapshot's engine, version, users and encryption, with the chosen placement."""
        options = self.restore_options
        return RenderedCluster(
            terraform_name=terraform_resource_name(self.identifier),
            identifier=self.identifier,
            engine=self._engine(),
            engine_version=self._engine_version(),
            engine_mode=self.snapshot.raw.get("EngineMode"),
            master_username=str(self.snapshot.raw.get("MasterUsername") or ""),
            database_name=None,
            port=self._port(),
            db_subnet_group_reference=self.subnet_group_reference(context),
            vpc_security_group_ids=list(options.vpc_security_group_ids),
            cluster_parameter_group_reference=self.parameter_group_reference(context),
            backup_retention_period=RESTORED_BACKUP_RETENTION_DAYS,
            preferred_backup_window=None,
            preferred_maintenance_window=None,
            storage_encrypted=bool(self.snapshot.raw.get("StorageEncrypted")),
            kms_key_id=_encryption_key_of(self.snapshot.raw),
            deletion_protection=options.deletion_protection,
            copy_tags_to_snapshot=True,
            auto_minor_version_upgrade=True,
            serverless_v2_min_capacity=None,
            serverless_v2_max_capacity=None,
            tags={},
            snapshot_identifier=self.snapshot.identifier,
            manage_master_user_password=MANAGE_RESTORED_MASTER_USER_PASSWORD,
        )

    def create_arguments(self, context: StepContext) -> dict[str, Any]:
        """`RestoreDBInstanceFromDBSnapshot` or `RestoreDBClusterFromSnapshot`, from the rendered model.

        Everything the rendered model carries that the call takes is sent; what it does not take (the
        maintenance window, Performance Insights and monitoring of an instance; the users, encryption and
        automatic minor upgrades the snapshot decides) is not.
        """
        rendered = self._rendered_model(context)
        options = self.restore_options
        if isinstance(rendered, RenderedInstance):
            arguments: dict[str, Any] = {
                "DBInstanceIdentifier": rendered.identifier,
                "DBSnapshotIdentifier": rendered.snapshot_identifier,
                "Engine": rendered.engine,
                "DBInstanceClass": rendered.instance_class,
                "DBSubnetGroupName": options.subnet_group_name,
                "VpcSecurityGroupIds": list(rendered.vpc_security_group_ids),
                "MultiAZ": rendered.multi_az,
                "PubliclyAccessible": rendered.publicly_accessible,
                "DeletionProtection": rendered.deletion_protection,
                "AutoMinorVersionUpgrade": rendered.auto_minor_version_upgrade,
                "CopyTagsToSnapshot": rendered.copy_tags_to_snapshot,
                "BackupRetentionPeriod": rendered.backup_retention_period,
                "ManageMasterUserPassword": rendered.manage_master_user_password,
                "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
            }
            optional: dict[str, Any] = {
                "DBParameterGroupName": options.parameter_group_name,
                "AllocatedStorage": rendered.allocated_storage,
                "Port": rendered.port,
                "StorageType": rendered.storage_type,
                "Iops": rendered.iops,
                "StorageThroughput": rendered.storage_throughput,
                "PreferredBackupWindow": rendered.preferred_backup_window,
                "CACertificateIdentifier": rendered.ca_cert_identifier,
            }
        else:
            assert isinstance(rendered, RenderedCluster)
            arguments = {
                "DBClusterIdentifier": rendered.identifier,
                "SnapshotIdentifier": rendered.snapshot_identifier,
                "Engine": rendered.engine,
                "EngineVersion": rendered.engine_version,
                "DBSubnetGroupName": options.subnet_group_name,
                "VpcSecurityGroupIds": list(rendered.vpc_security_group_ids),
                "DeletionProtection": rendered.deletion_protection,
                "CopyTagsToSnapshot": rendered.copy_tags_to_snapshot,
                "BackupRetentionPeriod": rendered.backup_retention_period,
                "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
            }
            optional = {
                "DBClusterParameterGroupName": options.parameter_group_name,
                "EngineMode": rendered.engine_mode,
                "Port": rendered.port,
                "DatabaseName": rendered.database_name,
                "KmsKeyId": rendered.kms_key_id,
                "PreferredBackupWindow": rendered.preferred_backup_window,
            }
            if rendered.serverless_v2_min_capacity is not None:
                optional["ServerlessV2ScalingConfiguration"] = {
                    "MinCapacity": rendered.serverless_v2_min_capacity,
                    "MaxCapacity": rendered.serverless_v2_max_capacity,
                }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        return arguments

    def create(self, context: StepContext) -> str:
        """Restore the snapshot; a cluster's writer is created next by `apply_direct`."""
        if self.scope == "instance":
            return lifecycle_api.restore_instance_from_snapshot(
                context, self.create_arguments(context), "apply_direct"
            )
        return lifecycle_api.restore_cluster_from_snapshot(
            context, self.create_arguments(context), "apply_direct"
        )

    def review_entries(self, context: StepContext) -> list[tuple[str, Any]]:
        """Snapshot, version, class (an instance's), placement, Multi-AZ, public access and protection."""
        entries: list[tuple[str, Any]] = [
            ("restore_source", self.snapshot.identifier),
            ("engine_version", self._engine_version()),
        ]
        if self.scope == "instance":
            entries += [
                ("instance_class", self.restore_options.instance_class),
                ("allocated_storage", self.snapshot.facts.get("allocated_storage_gb")),
                ("multi_az", self.restore_options.multi_az),
            ]
        entries.append(("publicly_accessible", self.restore_options.publicly_accessible))
        return entries + self._placement_review_entries()


class SnapshotRestoreExecutor(ResourceCreateExecutor):
    """Restore the operation's snapshot to a new instance, or a new cluster and writer, in both modes."""

    operation_kind = "snapshot_restore"

    def build_change(self, context: StepContext) -> SnapshotRestoreCreate:
        """The restore from `plan["options"]` and the snapshot's record.

        Raises:
            KeyError: The snapshot is not in the latest inventory.
            ValueError: The options are not valid.
        """
        options = SnapshotRestoreOptions.model_validate(context.operation.plan.get("options", {}))
        return SnapshotRestoreCreate(context.target.target_key, _launching_record(context), options)


class PointInTimeRestoreCreate(RestoreCreate):
    """A new instance, or a new Aurora cluster and writer, restored from a source to a point in time.

    Attributes:
        source: The record of the instance or cluster restored.
        restore_point: The source and the time (or the latest restorable time) restored to.

    Example:
        >>> PointInTimeRestoreCreate(TARGET_KEY, orders_record, options).summary  # doctest: +SKIP
        'restore demo-orders at the latest restorable time to demo-orders-pitr'
    """

    def __init__(self, target_key: str, source: ResourceRecord, options: PointInTimeRestoreOptions) -> None:
        """Describe the restore; nothing is created until a strategy runs the change."""
        scope: RestoreScope = "cluster" if source.kind == ResourceKind.CLUSTER else "instance"
        moment = options.restore_time_utc or LATEST_RESTORABLE_TIME
        super().__init__(
            target_key,
            source,
            scope,
            options,
            f"restore {source.identifier} at {moment} to {options.new_identifier}",
        )
        self.source = source
        self.restore_options = options
        self.restore_point = RestorePointInTime(
            source_identifier=source.identifier,
            restore_time_utc=options.restore_time_utc,
            use_latest_restorable_time=options.use_latest_restorable_time,
        )

    def create_only_fields(self) -> dict[str, Any]:
        """The source and time restored from, and the managed password, as the record keeps them."""
        return {
            "restore_to_point_in_time": self.restore_point.model_copy(),
            "manage_master_user_password": MANAGE_RESTORED_MASTER_USER_PASSWORD,
        }

    def rendered_instance(self, context: StepContext) -> RenderedInstance:
        """The source instance's rendered model as a new standalone instance at the chosen placement.

        The engine, version, storage size and ceiling, users, port, Multi-AZ, backups, encryption, automatic
        minor upgrades, tag copying and certificate are the source's. Storage type, IOPS and throughput are
        left to the restore (a gp3 source under 400 GiB reports baseline IOPS a restore refuses). The
        maintenance window, monitoring and Performance Insights retention are the source's too, and
        Performance Insights is on, as on every instance the console creates; no restore call takes those,
        so direct mode sends them in a modify once the instance is available.
        """
        options = self.restore_options
        source_model = creates.rendered_instance_of(context, self.source.identifier)
        return source_model.model_copy(
            deep=True,
            update={
                "terraform_name": terraform_resource_name(self.identifier),
                "identifier": self.identifier,
                "replicate_source_reference": None,
                "instance_class": options.instance_class,
                "storage_type": None,
                "iops": None,
                "storage_throughput": None,
                "db_name": None,
                "db_subnet_group_reference": self.subnet_group_reference(context),
                "vpc_security_group_ids": list(options.vpc_security_group_ids),
                "parameter_group_reference": self.parameter_group_reference(context),
                "publicly_accessible": False,
                "deletion_protection": options.deletion_protection,
                "performance_insights_enabled": True,
                "tags": {},
                "final_snapshot_identifier": None,
                "snapshot_identifier": None,
                "restore_to_point_in_time": self.restore_point.model_copy(),
                "manage_master_user_password": MANAGE_RESTORED_MASTER_USER_PASSWORD,
            },
        )

    def rendered_cluster(self, context: StepContext) -> RenderedCluster:
        """The source cluster's engine, version, users, backups, windows and encryption, placed as chosen.

        No cluster restore call takes the maintenance window; direct mode sends it in a modify once the
        cluster is available.
        """
        options = self.restore_options
        facts, raw = self.source.facts, self.source.raw
        port = facts.get("port")
        return RenderedCluster(
            terraform_name=terraform_resource_name(self.identifier),
            identifier=self.identifier,
            engine=str(facts.get("engine") or "aurora-postgresql"),
            engine_version=str(facts.get("engine_version") or ""),
            engine_mode=facts.get("engine_mode"),
            master_username=str(raw.get("MasterUsername") or ""),
            database_name=None,
            port=port if isinstance(port, int) and port > 0 else None,
            db_subnet_group_reference=self.subnet_group_reference(context),
            vpc_security_group_ids=list(options.vpc_security_group_ids),
            cluster_parameter_group_reference=self.parameter_group_reference(context),
            backup_retention_period=facts.get("backup_retention_period"),
            preferred_backup_window=facts.get("preferred_backup_window"),
            preferred_maintenance_window=facts.get("preferred_maintenance_window"),
            storage_encrypted=bool(facts.get("storage_encrypted")),
            kms_key_id=_encryption_key_of(raw),
            deletion_protection=options.deletion_protection,
            copy_tags_to_snapshot=bool(raw.get("CopyTagsToSnapshot", True)),
            auto_minor_version_upgrade=bool(raw.get("AutoMinorVersionUpgrade", True)),
            serverless_v2_min_capacity=facts.get("serverless_v2_min_capacity"),
            serverless_v2_max_capacity=facts.get("serverless_v2_max_capacity"),
            tags={},
            restore_to_point_in_time=self.restore_point.model_copy(),
            manage_master_user_password=MANAGE_RESTORED_MASTER_USER_PASSWORD,
        )

    def _restore_moment(self, time_key: str) -> dict[str, Any]:
        """`{time_key: <time>}` or `{"UseLatestRestorableTime": True}`, from the rendered restore point."""
        if self.restore_point.use_latest_restorable_time:
            return {"UseLatestRestorableTime": True}
        return {time_key: self.restore_point.restore_time_utc}

    def create_arguments(self, context: StepContext) -> dict[str, Any]:
        """`RestoreDBInstanceToPointInTime` or `RestoreDBClusterToPointInTime`, from the rendered model.

        Everything the rendered model carries that the call takes is sent; what it does not take (the
        version and users of the source, an instance's maintenance window, Performance Insights and
        monitoring) is not.
        """
        rendered = self._rendered_model(context)
        options = self.restore_options
        if isinstance(rendered, RenderedInstance):
            arguments: dict[str, Any] = {
                "SourceDBInstanceIdentifier": self.restore_point.source_identifier,
                "TargetDBInstanceIdentifier": rendered.identifier,
                **self._restore_moment("RestoreTime"),
                "Engine": rendered.engine,
                "DBInstanceClass": rendered.instance_class,
                "DBSubnetGroupName": options.subnet_group_name,
                "VpcSecurityGroupIds": list(rendered.vpc_security_group_ids),
                "MultiAZ": rendered.multi_az,
                "PubliclyAccessible": rendered.publicly_accessible,
                "DeletionProtection": rendered.deletion_protection,
                "AutoMinorVersionUpgrade": rendered.auto_minor_version_upgrade,
                "CopyTagsToSnapshot": rendered.copy_tags_to_snapshot,
                "ManageMasterUserPassword": rendered.manage_master_user_password,
                "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
            }
            optional: dict[str, Any] = {
                "DBParameterGroupName": options.parameter_group_name,
                "AllocatedStorage": rendered.allocated_storage,
                "MaxAllocatedStorage": rendered.max_allocated_storage,
                "Port": rendered.port,
                "StorageType": rendered.storage_type,
                "Iops": rendered.iops,
                "StorageThroughput": rendered.storage_throughput,
                "BackupRetentionPeriod": rendered.backup_retention_period,
                "PreferredBackupWindow": rendered.preferred_backup_window,
                "CACertificateIdentifier": rendered.ca_cert_identifier,
            }
        else:
            assert isinstance(rendered, RenderedCluster)
            arguments = {
                "DBClusterIdentifier": rendered.identifier,
                "SourceDBClusterIdentifier": self.restore_point.source_identifier,
                **self._restore_moment("RestoreToTime"),
                "DBSubnetGroupName": options.subnet_group_name,
                "VpcSecurityGroupIds": list(rendered.vpc_security_group_ids),
                "DeletionProtection": rendered.deletion_protection,
                "CopyTagsToSnapshot": rendered.copy_tags_to_snapshot,
                "Tags": lifecycle_api.tag_list(CONSOLE_MANAGED_TAGS),
            }
            optional = {
                "DBClusterParameterGroupName": options.parameter_group_name,
                "EngineMode": rendered.engine_mode,
                "Port": rendered.port,
                "KmsKeyId": rendered.kms_key_id,
                "BackupRetentionPeriod": rendered.backup_retention_period,
                "PreferredBackupWindow": rendered.preferred_backup_window,
            }
            if rendered.serverless_v2_min_capacity is not None:
                optional["ServerlessV2ScalingConfiguration"] = {
                    "MinCapacity": rendered.serverless_v2_min_capacity,
                    "MaxCapacity": rendered.serverless_v2_max_capacity,
                }
        arguments.update({key: value for key, value in optional.items() if value is not None})
        return arguments

    def create(self, context: StepContext) -> str:
        """Restore the source to the point in time; a cluster's writer is created next by `apply_direct`."""
        if self.scope == "instance":
            return lifecycle_api.restore_instance_to_point_in_time(
                context, self.create_arguments(context), "apply_direct"
            )
        return lifecycle_api.restore_cluster_to_point_in_time(
            context, self.create_arguments(context), "apply_direct"
        )

    def review_entries(self, context: StepContext) -> list[tuple[str, Any]]:
        """Source, time, version, class and Multi-AZ (an instance's), and the placement."""
        entries: list[tuple[str, Any]] = [
            ("restore_source", self.source.identifier),
            ("restore_time", self.restore_point.restore_time_utc or LATEST_RESTORABLE_TIME),
            ("engine_version", self.source.facts.get("engine_version")),
        ]
        if self.scope == "instance":
            rendered = self._rendered_model(context)
            entries += [
                ("instance_class", self.restore_options.instance_class),
                ("multi_az", getattr(rendered, "multi_az", False)),
            ]
        return entries + self._placement_review_entries()


class PointInTimeRestoreExecutor(ResourceCreateExecutor):
    """Restore the operation's instance or cluster to a point in time as a new resource, in both modes.

    When the operation was created without a readable restore window, the first step records a
    `restore_window_unknown` warning on it, so the approver knows the time was not checked.
    """

    operation_kind = "pitr_restore"

    def build_change(self, context: StepContext) -> PointInTimeRestoreCreate:
        """The restore from `plan["options"]` and the source's record.

        Raises:
            KeyError: The source is not in the latest inventory.
            ValueError: The options are not valid.
        """
        options = PointInTimeRestoreOptions.model_validate(context.operation.plan.get("options", {}))
        return PointInTimeRestoreCreate(context.target.target_key, _launching_record(context), options)

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Warn when the window was unknown, then compute the change set."""
        self._warn_when_the_restore_window_was_unknown(context, "compute_change_set")
        return super()._execute_compute_change_set(context)

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Warn when the window was unknown, then render."""
        self._warn_when_the_restore_window_was_unknown(context, "render_desired")
        return super()._execute_render_desired(context)

    def _warn_when_the_restore_window_was_unknown(self, context: StepContext, step_name: str) -> None:
        """Record the `restore_window_unknown` warning once, when the preparer could not read the window."""
        reason = context.operation.plan.get(RESTORE_WINDOW_UNKNOWN_PLAN_KEY)
        if not reason:
            return
        already_recorded = context.session.execute(
            select(DiagnosticReport.id).where(
                DiagnosticReport.operation_id == context.operation.id,
                DiagnosticReport.diagnostic_kind == RESTORE_WINDOW_UNKNOWN_DIAGNOSTIC,
            )
        ).first()
        if already_recorded is not None:
            return
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name=step_name,
            severity="warning",
            diagnostic_kind=RESTORE_WINDOW_UNKNOWN_DIAGNOSTIC,
            title="The restore time was not checked against the restore window",
            detail=f"{reason}, so the window the restore time must fall in is unknown here.",
            fix_suggestion=(
                "check the source's automated backups in AWS before approving; AWS refuses a time outside "
                "its window when the restore is submitted"
            ),
        )
        context.session.commit()
