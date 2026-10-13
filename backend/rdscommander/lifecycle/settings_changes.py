"""The instance and cluster setting operations: each maps its options to attribute values."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from rdscommander.lifecycle.options import (
    BackupSettingsOptions,
    DeletionProtectionOptions,
    InstanceClassChangeOptions,
    MaintenanceWindowOptions,
    MonitoringOptions,
    MultiAzChangeOptions,
    ServerlessCapacityOptions,
    StorageChangeOptions,
    TagsOptions,
)
from rdscommander.logging_setup import get_logger
from rdscommander.operations.attribute_change import AttributeChange, AttributeChangeExecutor
from rdscommander.operations.change_models import ChangeDiagnosticError
from rdscommander.operations.enums import StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepOutcome
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import load_resource

logger = get_logger(__name__)

BACKUPS_TURNED_OFF_IMPACT = "existing automated backups are deleted and point-in-time restore is lost"
"""Added to the review of a change that turns automated backups off (retention to 0 from non-zero)."""
STORAGE_CHANGE_INTERVAL_IMPACT = (
    "AWS allows the next storage change only after storage optimisation completes and at least six hours "
    "after this one"
)
"""Added to the review of every storage change; AWS exposes no field to check the interval against."""


def add_to_review_impact(executor: AttributeChangeExecutor, context: StepContext, sentence: str) -> None:
    """Add `sentence` to what the review says to expect (`plan["downtime"]`), once.

    Example:
        >>> add_to_review_impact(executor, context, STORAGE_CHANGE_INTERVAL_IMPACT)  # doctest: +SKIP
    """
    downtime = str(executor._plan_value(context, "downtime") or "none")
    if sentence in downtime:
        return
    executor._store_plan_values(
        context, downtime=sentence if downtime == "none" else f"{downtime}; {sentence}"
    )


class StorageCannotShrinkError(ChangeDiagnosticError):
    """AWS does not reduce allocated storage; the console refuses before asking for approval."""

    diagnostic_kind = "storage_cannot_shrink"
    title = "Storage cannot be made smaller"
    fix_suggestion = (
        "choose a size at or above the current one, or restore a snapshot into a smaller instance"
    )


class BackupsRequiredForReplicasError(ChangeDiagnosticError):
    """Automated backups cannot be turned off while the instance is the source of read replicas."""

    diagnostic_kind = "backups_required_for_replicas"
    title = "Backups are required while replicas exist"
    fix_suggestion = "keep a retention of at least 1 day, or promote or delete the replicas first"


class MonitoringRoleRequiredError(ChangeDiagnosticError):
    """Enhanced monitoring was asked for without a role, and the resource has none to keep."""

    diagnostic_kind = "monitoring_role_required"
    title = "Enhanced monitoring needs a monitoring role"
    fix_suggestion = (
        "give the ARN of an IAM role that RDS enhanced monitoring can assume, or set enhanced monitoring off"
    )


class BackupsLiveOnTheClusterError(ChangeDiagnosticError):
    """Aurora backups are a cluster setting; the console refuses to set them on a member."""

    diagnostic_kind = "backups_live_on_the_cluster"
    title = "Set backups on the cluster"
    fix_suggestion = "open the cluster and change its backup settings there"


class InstanceClassChangeExecutor(AttributeChangeExecutor):
    """Change the class of an instance or cluster member."""

    operation_kind = "instance_class_change"

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """The new class.

        Example:
            >>> executor.attribute_values(InstanceClassChangeOptions(instance_class="db.m6g.xlarge"), record)
            {'instance_class': 'db.m6g.xlarge'}
        """
        assert isinstance(options, InstanceClassChangeOptions)
        return {"instance_class": options.instance_class}

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, InstanceClassChangeOptions)
        return f"change class to {options.instance_class}"


class StorageChangeExecutor(AttributeChangeExecutor):
    """Change the storage of a standalone instance; it only grows."""

    operation_kind = "storage_change"

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: the rendered change; the review says how long AWS waits between storage changes."""
        outcome = super()._execute_render_desired(context)
        if outcome.status == StepOutcomeStatus.COMPLETED:
            add_to_review_impact(self, context, STORAGE_CHANGE_INTERVAL_IMPACT)
        return outcome

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Direct mode: the change set; the review says how long AWS waits between storage changes."""
        outcome = super()._execute_compute_change_set(context)
        if outcome.status == StepOutcomeStatus.COMPLETED:
            add_to_review_impact(self, context, STORAGE_CHANGE_INTERVAL_IMPACT)
        return outcome

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """The storage attributes the options ask for.

        Raises:
            StorageCannotShrinkError: The requested size is below the current allocation.

        Example:
            >>> executor.attribute_values(StorageChangeOptions(allocated_storage_gb=200), record)
            {'allocated_storage': 200}

        A ceiling of 0 stays 0: that is how both the API and the provider turn autoscaling off.
        """
        assert isinstance(options, StorageChangeOptions)
        current = int(record.facts.get("allocated_storage_gb") or 0)
        if options.allocated_storage_gb is not None and options.allocated_storage_gb < current:
            raise StorageCannotShrinkError(
                f"{record.identifier} has {current} GiB allocated; "
                f"{options.allocated_storage_gb} GiB is smaller"
            )
        values: dict[str, Any] = {}
        if options.allocated_storage_gb is not None:
            values["allocated_storage"] = options.allocated_storage_gb
        if options.max_allocated_storage_gb is not None:
            values["max_allocated_storage"] = options.max_allocated_storage_gb
        if options.storage_type is not None:
            values["storage_type"] = options.storage_type
        if options.iops is not None:
            values["iops"] = options.iops
        if options.storage_throughput is not None:
            values["storage_throughput"] = options.storage_throughput
        return values

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        return "change storage"


class MultiAzChangeExecutor(AttributeChangeExecutor):
    """Turn Multi-AZ on or off."""

    operation_kind = "multi_az_change"

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """The Multi-AZ flag.

        Example:
            >>> executor.attribute_values(MultiAzChangeOptions(multi_az=True), record)
            {'multi_az': True}
        """
        assert isinstance(options, MultiAzChangeOptions)
        return {"multi_az": options.multi_az}

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, MultiAzChangeOptions)
        return "enable Multi-AZ" if options.multi_az else "disable Multi-AZ"


class BackupSettingsChangeExecutor(AttributeChangeExecutor):
    """Change backup retention, window and tag copying of an instance or cluster.

    Turning automated backups off deletes the existing automated backups and the point-in-time restore
    history, so that change asks for the identifier to be typed and says so in the review, in both modes.
    """

    operation_kind = "backup_settings_change"

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: the rendered change; turning backups off (inventory retention) is gated."""
        outcome = super()._execute_render_desired(context)
        if outcome.status == StepOutcomeStatus.COMPLETED:
            change = self.desired_change(context)
            current = change.known_current_values.get("backup_retention_period")
            if change.values.get("backup_retention_period") == 0 and current != 0:
                self._require_identifier_for_turning_backups_off(context)
        return outcome

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Direct mode: the change set; turning backups off (from the live retention) is gated."""
        outcome = super()._execute_compute_change_set(context)
        if outcome.status == StepOutcomeStatus.COMPLETED and any(
            entry.attribute == "backup_retention_period" and entry.desired == 0 and entry.current != 0
            for entry in self._entries(context)
        ):
            self._require_identifier_for_turning_backups_off(context)
        return outcome

    def _require_identifier_for_turning_backups_off(self, context: StepContext) -> None:
        """Make the approver type the identifier and add the lost backups to the review's impact."""
        resource_arn = context.operation.resource_arn or ""
        record = load_resource(context.session, context.console_config, resource_arn)
        identifier = record.identifier if record is not None else resource_arn.rsplit(":", 1)[-1]
        context.operation.approval_required_text = identifier
        add_to_review_impact(self, context, BACKUPS_TURNED_OFF_IMPACT)
        logger.info(
            "backups_turned_off_requires_typed_identifier",
            operation_id=str(context.operation.id),
            identifier=identifier,
        )

    def desired_change(self, context: StepContext) -> AttributeChange:
        """The change; for a cluster, its members are kept as `plan["affected_resource_arns"]`.

        AWS reports a cluster's backup retention and window on each member as well, so change history must
        explain the members' copies of the change by this operation, not as changes outside the console.

        Example:
            >>> executor.desired_change(context).values  # doctest: +SKIP
            {'backup_retention_period': 14}
        """
        change = super().desired_change(context)
        record = load_resource(context.session, context.console_config, change.resource_arn)
        if record is not None and record.kind == ResourceKind.CLUSTER:
            arn_prefix = record.arn.rsplit(":", 2)[0]
            members = sorted(record.relationships.get("member_identifiers") or [])
            member_arns = [f"{arn_prefix}:db:{member}" for member in members]
            if context.operation.plan.get("affected_resource_arns") != member_arns:
                self._store_plan_values(context, affected_resource_arns=member_arns)
        return change

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """The backup attributes the options ask for.

        Raises:
            BackupsLiveOnTheClusterError: The record is an Aurora cluster member.
            BackupsRequiredForReplicasError: Retention 0 was asked for a source of read replicas.

        Example:
            >>> executor.attribute_values(BackupSettingsOptions(backup_retention_period_days=14), record)
            {'backup_retention_period': 14}
        """
        assert isinstance(options, BackupSettingsOptions)
        if record.kind == ResourceKind.INSTANCE and record.relationships.get("cluster_identifier"):
            raise BackupsLiveOnTheClusterError(
                f"{record.identifier} is a member of {record.relationships['cluster_identifier']}; "
                "set this on the cluster"
            )
        replicas = record.relationships.get("replica_identifiers") or []
        if options.backup_retention_period_days == 0 and replicas:
            raise BackupsRequiredForReplicasError(
                f"{record.identifier} is the source of {', '.join(replicas)}; "
                "AWS needs automated backups for read replicas"
            )
        values: dict[str, Any] = {"backup_retention_period": options.backup_retention_period_days}
        if options.preferred_backup_window is not None:
            values["preferred_backup_window"] = options.preferred_backup_window
        if options.copy_tags_to_snapshot is not None:
            values["copy_tags_to_snapshot"] = options.copy_tags_to_snapshot
        return values

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, BackupSettingsOptions)
        if options.backup_retention_period_days == 0:
            return "turn automated backups off"
        return f"keep automated backups {options.backup_retention_period_days} days"


class MaintenanceWindowChangeExecutor(AttributeChangeExecutor):
    """Change the weekly maintenance window."""

    operation_kind = "maintenance_window_change"

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """The window.

        Example:
            >>> executor.attribute_values(
            ...     MaintenanceWindowOptions(preferred_maintenance_window="sun:03:00-sun:04:00"), record
            ... )
            {'preferred_maintenance_window': 'sun:03:00-sun:04:00'}
        """
        assert isinstance(options, MaintenanceWindowOptions)
        return {"preferred_maintenance_window": options.preferred_maintenance_window}

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, MaintenanceWindowOptions)
        return f"set the maintenance window to {options.preferred_maintenance_window}"


class MonitoringChangeExecutor(AttributeChangeExecutor):
    """Change Performance Insights and enhanced monitoring."""

    operation_kind = "monitoring_change"

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """The monitoring attributes; the role is sent only while enhanced monitoring is on.

        Without a role in the options, the role the resource has (`MonitoringRoleArn`) is kept and sent.

        Raises:
            MonitoringRoleRequiredError: Enhanced monitoring is on, no role was given and the resource has
                none.

        Example:
            >>> executor.attribute_values(
            ...     MonitoringOptions(performance_insights_enabled=True, monitoring_interval_seconds=0),
            ...     record,
            ... )
            {'performance_insights_enabled': True, 'monitoring_interval': 0}
        """
        assert isinstance(options, MonitoringOptions)
        values: dict[str, Any] = {"performance_insights_enabled": options.performance_insights_enabled}
        if options.performance_insights_retention_period_days is not None:
            values["performance_insights_retention_period"] = (
                options.performance_insights_retention_period_days
            )
        values["monitoring_interval"] = options.monitoring_interval_seconds
        if options.monitoring_interval_seconds != 0:
            role_arn = options.monitoring_role_arn or record.raw.get("MonitoringRoleArn")
            if not role_arn:
                logger.error(
                    "monitoring_role_missing",
                    resource_arn=record.arn,
                    monitoring_interval_seconds=options.monitoring_interval_seconds,
                    fix_suggestion=MonitoringRoleRequiredError.fix_suggestion,
                )
                raise MonitoringRoleRequiredError(
                    f"{record.identifier} has no enhanced monitoring role; "
                    f"a {options.monitoring_interval_seconds} s interval needs one"
                )
            values["monitoring_role_arn"] = role_arn
        return values

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, MonitoringOptions)
        insights = "on" if options.performance_insights_enabled else "off"
        interval = (
            f"{options.monitoring_interval_seconds} s" if options.monitoring_interval_seconds else "off"
        )
        return f"set Performance Insights {insights} and enhanced monitoring {interval}"


class DeletionProtectionChangeExecutor(AttributeChangeExecutor):
    """Turn deletion protection on or off."""

    operation_kind = "deletion_protection_change"

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """The flag.

        Example:
            >>> executor.attribute_values(DeletionProtectionOptions(deletion_protection=True), record)
            {'deletion_protection': True}
        """
        assert isinstance(options, DeletionProtectionOptions)
        return {"deletion_protection": options.deletion_protection}

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, DeletionProtectionOptions)
        return (
            "turn deletion protection on" if options.deletion_protection else "turn deletion protection off"
        )


class ServerlessCapacityChangeExecutor(AttributeChangeExecutor):
    """Change the Aurora Serverless v2 capacity range; both ends are always sent."""

    operation_kind = "serverless_capacity_change"

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """Both capacities, so the nested scaling configuration sent to AWS is complete.

        Example:
            >>> options = ServerlessCapacityOptions(min_capacity_acu=0.5, max_capacity_acu=4)
            >>> executor.attribute_values(options, record)
            {'serverless_v2_min_capacity': 0.5, 'serverless_v2_max_capacity': 4.0}
        """
        assert isinstance(options, ServerlessCapacityOptions)
        return {
            "serverless_v2_min_capacity": options.min_capacity_acu,
            "serverless_v2_max_capacity": options.max_capacity_acu,
        }

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, ServerlessCapacityOptions)
        return f"scale between {options.min_capacity_acu} and {options.max_capacity_acu} ACUs"


class TagsChangeExecutor(AttributeChangeExecutor):
    """Set and remove tags on an instance, cluster or parameter group."""

    operation_kind = "tags_change"

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """`tags.<key>` entries: the value to set, or None to remove the tag.

        Example:
            >>> options = TagsOptions(set_tags={"team": "orders"}, remove_tags=["old"])
            >>> executor.attribute_values(options, record)
            {'tags.team': 'orders', 'tags.old': None}
        """
        assert isinstance(options, TagsOptions)
        return {f"tags.{key}": value for key, value in options.set_tags.items()} | {
            f"tags.{key}": None for key in options.remove_tags
        }

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, TagsOptions)
        return f"set {len(options.set_tags)} tags, remove {len(options.remove_tags)}"
