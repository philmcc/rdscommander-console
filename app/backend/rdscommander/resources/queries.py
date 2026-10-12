"""Build `ResourceRecord`s from the latest snapshots."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig, ResolvedTarget
from rdscommander.control_db.models import (
    DriftFinding,
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryDbSnapshot,
    InventoryParameterGroup,
    InventoryParameterValue,
    InventoryPendingMaintenance,
    InventorySnapshot,
    Operation,
    ParameterGroupProvenance,
    ResourceAdoption,
    TerraformWorkspace,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.terraform.names import terraform_address_for
from rdscommander.upgrades.blue_green_api import deployments_left_in_place

PENDING_REBOOT_APPLY_STATUS = "pending-reboot"
"""`ParameterApplyStatus` of an instance whose parameter group changes wait for a reboot."""
ALL_LISTED_KINDS: frozenset[ResourceKind] = frozenset(
    {ResourceKind.INSTANCE, ResourceKind.CLUSTER, ResourceKind.PARAMETER_GROUP, ResourceKind.SNAPSHOT}
)


BLUE_GREEN_DEPLOYMENT_LEFT_FACT = "blue_green_deployment_left_in_place"
"""Fact of an instance or cluster: the id of a blue/green deployment a finished major upgrade left in place
(failed or rejected before the switchover finished), which `blue_green_cleanup` removes; None otherwise."""


def adopted_addresses_of_target(session: Session, target_key: str) -> set[str]:
    """Terraform addresses under Terraform in the workspace of `target_key`: the one "is adopted" answer.

    An adoption row with status `adopted` is written when an adoption applies and, from Terraform state, by
    detection and by the next adoption's render; tags are never evidence. The resource pages, the adoption
    picker's subnet groups and the adoption prerequisite check all read this, so they agree on what still
    needs adopting (a failed adoption apply can leave the console's tags on a resource it did not adopt).

    Example:
        >>> adopted_addresses_of_target(session, "sandbox/000000000000/eu-west-2")  # doctest: +SKIP
        {'aws_db_subnet_group.demo_private_subnets'}
    """
    workspace = session.execute(
        select(TerraformWorkspace).where(TerraformWorkspace.target_key == target_key)
    ).scalar_one_or_none()
    if workspace is None:
        return set()
    adopted_addresses = session.execute(
        select(ResourceAdoption.terraform_address).where(
            ResourceAdoption.workspace_id == workspace.id,
            ResourceAdoption.adoption_status == "adopted",
        )
    ).scalars()
    return set(adopted_addresses)


def ownership_in_mode(
    tag_ownership: str, terraform_address: str, adopted_addresses: set[str], target: ResolvedTarget
) -> str:
    """The ownership a page shows and the action gates read.

    In a Terraform-mode environment `adopted` means under Terraform: an adoption row (which detection also
    writes for what Terraform state holds), whatever the tags say. A resource the console created in direct
    mode carries the console's tag but is not under Terraform, so it is `unmanaged` and can be adopted; a
    resource adopted moments ago is `adopted` before the refresh that reads its new tag. Direct mode keeps
    the ownership the tags give.
    """
    if target.is_direct:
        return tag_ownership
    if terraform_address in adopted_addresses:
        return "adopted"
    if tag_ownership == "adopted":
        return "unmanaged"
    return tag_ownership


def _arns_with_open_drift(session: Session, target_key: str) -> set[str]:
    """ARNs of the resources of `target_key` that have an unresolved drift finding."""
    return set(
        session.execute(
            select(DriftFinding.resource_arn).where(
                DriftFinding.target_key == target_key, DriftFinding.resolved_at_utc.is_(None)
            )
        ).scalars()
    )


def _target_may_own_arn(target: ResolvedTarget, arn_region: str, arn_account_id: str) -> bool:
    """Whether an ARN naming `arn_region` and `arn_account_id` can belong to `target`.

    A simulated target is matched on region alone: the simulated AWS issues ARNs carrying its own
    account id, not the account id the target is configured with.
    """
    if target.aws_region != arn_region:
        return False
    return target.simulated or target.aws_account_id == arn_account_id


def _instance_record(
    instance_row: InventoryDbInstance,
    target: ResolvedTarget,
    snapshot: InventorySnapshot,
    all_instance_rows: list[InventoryDbInstance],
    adopted_addresses: set[str],
    cluster_writer_identifiers: frozenset[str] = frozenset(),
) -> ResourceRecord:
    """Record for one database instance row.

    `facts["is_cluster_writer"]` is True for the member its cluster's describe lists with `IsClusterWriter`
    (`cluster_writer_identifiers`, from the cluster rows of the same snapshot) and False for every other
    instance, so a rule that needs the writer does not have to load the cluster.
    """
    raw_description = instance_row.raw_description
    endpoint = raw_description.get("Endpoint") or {}
    resource_type = "aws_rds_cluster_instance" if instance_row.db_cluster_identifier else "aws_db_instance"
    terraform_address = terraform_address_for(resource_type, instance_row.db_instance_identifier)
    return ResourceRecord(
        kind=ResourceKind.INSTANCE,
        arn=instance_row.db_instance_arn,
        resource_id=encode_resource_id(instance_row.db_instance_arn),
        identifier=instance_row.db_instance_identifier,
        environment_slug=target.environment_slug,
        target_key=target.target_key,
        ownership=ownership_in_mode(instance_row.ownership, terraform_address, adopted_addresses, target),
        status=instance_row.db_instance_status,
        snapshot_id=snapshot.id,
        terraform_address=terraform_address if terraform_address in adopted_addresses else None,
        parameter_apply_status=instance_row.parameter_apply_status,
        facts={
            "engine": instance_row.engine,
            "engine_version": instance_row.engine_version,
            "db_instance_class": instance_row.db_instance_class,
            "db_instance_status": instance_row.db_instance_status,
            "allocated_storage_gb": raw_description.get("AllocatedStorage"),
            "max_allocated_storage_gb": raw_description.get("MaxAllocatedStorage"),
            "storage_type": raw_description.get("StorageType"),
            "iops": raw_description.get("Iops"),
            "multi_az": bool(raw_description.get("MultiAZ", False)),
            "publicly_accessible": bool(raw_description.get("PubliclyAccessible", False)),
            "backup_retention_period": raw_description.get("BackupRetentionPeriod"),
            "preferred_backup_window": raw_description.get("PreferredBackupWindow"),
            "preferred_maintenance_window": raw_description.get("PreferredMaintenanceWindow"),
            "storage_encrypted": bool(raw_description.get("StorageEncrypted", False)),
            "performance_insights_enabled": bool(raw_description.get("PerformanceInsightsEnabled", False)),
            "monitoring_interval": raw_description.get("MonitoringInterval", 0),
            "auto_minor_version_upgrade": bool(raw_description.get("AutoMinorVersionUpgrade", True)),
            "deletion_protection": bool(raw_description.get("DeletionProtection", False)),
            "endpoint_address": endpoint.get("Address"),
            "port": endpoint.get("Port"),
            "tags": instance_row.tags,
            "is_cluster_writer": instance_row.db_instance_identifier in cluster_writer_identifiers,
        },
        relationships={
            "parameter_group_name": instance_row.parameter_group_name,
            "db_subnet_group_name": instance_row.db_subnet_group_name,
            "cluster_identifier": instance_row.db_cluster_identifier,
            "read_replica_source_identifier": instance_row.read_replica_source_identifier,
            "replica_identifiers": [
                other_instance_row.db_instance_identifier
                for other_instance_row in all_instance_rows
                if other_instance_row.read_replica_source_identifier == instance_row.db_instance_identifier
            ],
        },
        raw=raw_description,
    )


def _cluster_record(
    cluster_row: InventoryDbCluster,
    target: ResolvedTarget,
    snapshot: InventorySnapshot,
    all_instance_rows: list[InventoryDbInstance],
    adopted_addresses: set[str],
) -> ResourceRecord:
    """Record for one database cluster row.

    Besides the scalar facts, `facts["member_instance_ownership"]` holds the ownership of each member
    instance found in the same snapshot, as that instance's own record shows it: a change that reboots the
    members must not touch one owned by other tooling.
    """
    raw_description = cluster_row.raw_description
    terraform_address = terraform_address_for("aws_rds_cluster", cluster_row.db_cluster_identifier)
    instance_rows_by_identifier = {row.db_instance_identifier: row for row in all_instance_rows}
    member_instance_ownership = {
        member_identifier: ownership_in_mode(
            instance_rows_by_identifier[member_identifier].ownership,
            terraform_address_for("aws_rds_cluster_instance", member_identifier),
            adopted_addresses,
            target,
        )
        for member_identifier in sorted(cluster_row.member_instance_identifiers)
        if member_identifier in instance_rows_by_identifier
    }
    serverless_scaling = raw_description.get("ServerlessV2ScalingConfiguration") or {}
    return ResourceRecord(
        kind=ResourceKind.CLUSTER,
        arn=cluster_row.db_cluster_arn,
        resource_id=encode_resource_id(cluster_row.db_cluster_arn),
        identifier=cluster_row.db_cluster_identifier,
        environment_slug=target.environment_slug,
        target_key=target.target_key,
        ownership=ownership_in_mode(cluster_row.ownership, terraform_address, adopted_addresses, target),
        status=cluster_row.status,
        snapshot_id=snapshot.id,
        terraform_address=terraform_address if terraform_address in adopted_addresses else None,
        facts={
            "engine": cluster_row.engine,
            "engine_version": cluster_row.engine_version,
            "status": cluster_row.status,
            "engine_mode": raw_description.get("EngineMode"),
            "backup_retention_period": raw_description.get("BackupRetentionPeriod"),
            "preferred_backup_window": raw_description.get("PreferredBackupWindow"),
            "preferred_maintenance_window": raw_description.get("PreferredMaintenanceWindow"),
            "storage_encrypted": bool(raw_description.get("StorageEncrypted", False)),
            "deletion_protection": bool(raw_description.get("DeletionProtection", False)),
            "serverless_v2_min_capacity": serverless_scaling.get("MinCapacity"),
            "serverless_v2_max_capacity": serverless_scaling.get("MaxCapacity"),
            "endpoint_address": raw_description.get("Endpoint"),
            "reader_endpoint_address": raw_description.get("ReaderEndpoint"),
            "port": raw_description.get("Port"),
            "tags": cluster_row.tags,
            "member_instance_ownership": member_instance_ownership,
        },
        relationships={
            "cluster_parameter_group_name": cluster_row.cluster_parameter_group_name,
            "db_subnet_group_name": cluster_row.db_subnet_group_name,
            "member_identifiers": list(cluster_row.member_instance_identifiers),
            "writer_identifier": next(
                (
                    cluster_member.get("DBInstanceIdentifier")
                    for cluster_member in raw_description.get("DBClusterMembers", [])
                    if cluster_member.get("IsClusterWriter")
                ),
                None,
            ),
        },
        raw=raw_description,
    )


def _user_parameters_by_group(
    session: Session, parameter_group_rows: list[InventoryParameterGroup]
) -> dict[int, dict[str, dict[str, Any]]]:
    """`{group row id: {name: {"value", "apply_method"}}}` for the user-set parameters of the groups."""
    if not parameter_group_rows:
        return {}
    value_rows = session.execute(
        select(InventoryParameterValue)
        .where(
            InventoryParameterValue.parameter_group_id.in_([row.id for row in parameter_group_rows]),
            InventoryParameterValue.source == "user",
        )
        .order_by(InventoryParameterValue.parameter_name)
    ).scalars()
    parameters: dict[int, dict[str, dict[str, Any]]] = {row.id: {} for row in parameter_group_rows}
    for value_row in value_rows:
        if value_row.parameter_value is None:
            continue
        parameters[value_row.parameter_group_id][value_row.parameter_name] = {
            "value": value_row.parameter_value,
            "apply_method": value_row.apply_method
            or ("pending-reboot" if value_row.apply_type == "static" else "immediate"),
        }
    return parameters


def _parameter_group_record(
    parameter_group_row: InventoryParameterGroup,
    target: ResolvedTarget,
    snapshot: InventorySnapshot,
    adopted_addresses: set[str],
    user_identifiers: list[str],
    pending_reboot_ownership: dict[str, str],
    user_set_parameters: dict[str, dict[str, Any]],
) -> ResourceRecord:
    """Record for one parameter group row, with who uses it and what is set on it.

    Besides the scalar facts, `facts["used_by"]` holds the sorted identifiers of the instances (or, for a
    cluster group, clusters) that use the group, `facts["pending_reboot_instances"]` those instances that
    wait for a reboot to apply the group, `facts["pending_reboot_instance_ownership"]` the ownership of each
    of them (a reboot of the group must not touch one owned by other tooling), and `facts["parameters"]`
    the user-set parameters as `{name: {"value", "apply_method"}}`.
    """
    resource_type = (
        "aws_db_parameter_group"
        if parameter_group_row.group_scope == "instance"
        else "aws_rds_cluster_parameter_group"
    )
    terraform_address = terraform_address_for(resource_type, parameter_group_row.parameter_group_name)
    return ResourceRecord(
        kind=ResourceKind.PARAMETER_GROUP,
        arn=parameter_group_row.parameter_group_arn,
        resource_id=encode_resource_id(parameter_group_row.parameter_group_arn),
        identifier=parameter_group_row.parameter_group_name,
        environment_slug=target.environment_slug,
        target_key=target.target_key,
        ownership=ownership_in_mode(
            parameter_group_row.ownership, terraform_address, adopted_addresses, target
        ),
        status="available",
        snapshot_id=snapshot.id,
        terraform_address=terraform_address if terraform_address in adopted_addresses else None,
        facts={
            "group_scope": parameter_group_row.group_scope,
            "parameter_group_family": parameter_group_row.parameter_group_family,
            "description": parameter_group_row.description,
            "is_default": parameter_group_row.is_default,
            "user_set_parameter_count": parameter_group_row.user_set_parameter_count,
            "tags": parameter_group_row.tags,
            "used_by": sorted(user_identifiers),
            "pending_reboot_instances": sorted(pending_reboot_ownership),
            "pending_reboot_instance_ownership": dict(sorted(pending_reboot_ownership.items())),
            "parameters": user_set_parameters,
        },
        relationships={"used_by_identifiers": user_identifiers, "inventory_row_id": parameter_group_row.id},
        raw=parameter_group_row.raw_description,
    )


def _snapshot_record(
    database_snapshot_row: InventoryDbSnapshot, target: ResolvedTarget, snapshot: InventorySnapshot
) -> ResourceRecord:
    """Record for one database or cluster snapshot row."""
    created_at_source_utc = database_snapshot_row.created_at_source_utc
    return ResourceRecord(
        kind=ResourceKind.SNAPSHOT,
        arn=database_snapshot_row.db_snapshot_arn,
        resource_id=encode_resource_id(database_snapshot_row.db_snapshot_arn),
        identifier=database_snapshot_row.db_snapshot_identifier,
        environment_slug=target.environment_slug,
        target_key=target.target_key,
        ownership="unmanaged",
        status=database_snapshot_row.status,
        snapshot_id=snapshot.id,
        facts={
            "snapshot_scope": database_snapshot_row.snapshot_scope,
            "snapshot_type": database_snapshot_row.snapshot_type,
            "engine_version": database_snapshot_row.engine_version,
            "allocated_storage_gb": database_snapshot_row.allocated_storage_gb,
            "created_at_source_utc": created_at_source_utc.isoformat() if created_at_source_utc else None,
            "tags": database_snapshot_row.tags,
        },
        relationships={"source_identifier": database_snapshot_row.source_identifier},
        raw=database_snapshot_row.raw_description,
    )


def _pending_maintenance_actions_by_arn(session: Session, snapshot_id: int) -> dict[str, list[str]]:
    """The names of the pending maintenance actions of each resource in one inventory snapshot."""
    actions_by_arn: dict[str, list[str]] = {}
    for row in session.execute(
        select(InventoryPendingMaintenance)
        .where(InventoryPendingMaintenance.snapshot_id == snapshot_id)
        .order_by(InventoryPendingMaintenance.action, InventoryPendingMaintenance.id)
    ).scalars():
        actions_by_arn.setdefault(row.resource_arn, []).append(row.action)
    return actions_by_arn


def _records_for_target(
    session: Session, target: ResolvedTarget, kinds: set[ResourceKind]
) -> list[ResourceRecord]:
    """Records of the requested kinds from the latest snapshot of `target`, sorted by kind then identifier."""
    snapshot = latest_snapshot_for_target(session, target.target_key)
    if snapshot is None:
        return []
    adopted_addresses = adopted_addresses_of_target(session, target.target_key)
    records: list[ResourceRecord] = []
    instance_rows = list(
        session.execute(
            select(InventoryDbInstance).where(InventoryDbInstance.snapshot_id == snapshot.id)
        ).scalars()
    )
    cluster_rows = list(
        session.execute(
            select(InventoryDbCluster).where(InventoryDbCluster.snapshot_id == snapshot.id)
        ).scalars()
    )
    if ResourceKind.INSTANCE in kinds:
        cluster_writer_identifiers = frozenset(
            cluster_member["DBInstanceIdentifier"]
            for cluster_row in cluster_rows
            for cluster_member in cluster_row.raw_description.get("DBClusterMembers", [])
            if cluster_member.get("IsClusterWriter") and cluster_member.get("DBInstanceIdentifier")
        )
        records += [
            _instance_record(
                instance_row, target, snapshot, instance_rows, adopted_addresses, cluster_writer_identifiers
            )
            for instance_row in instance_rows
        ]
    if ResourceKind.CLUSTER in kinds:
        records += [
            _cluster_record(cluster_row, target, snapshot, instance_rows, adopted_addresses)
            for cluster_row in cluster_rows
        ]
    if ResourceKind.PARAMETER_GROUP in kinds:
        parameter_group_rows = list(
            session.execute(
                select(InventoryParameterGroup).where(InventoryParameterGroup.snapshot_id == snapshot.id)
            ).scalars()
        )
        user_set_parameters = _user_parameters_by_group(session, parameter_group_rows)
        for parameter_group_row in parameter_group_rows:
            pending_reboot_ownership: dict[str, str] = {}
            if parameter_group_row.group_scope == "instance":
                using_instance_rows = [
                    instance_row
                    for instance_row in instance_rows
                    if instance_row.parameter_group_name == parameter_group_row.parameter_group_name
                ]
                user_identifiers = [row.db_instance_identifier for row in using_instance_rows]
                pending_reboot_ownership = {
                    row.db_instance_identifier: row.ownership
                    for row in using_instance_rows
                    if row.parameter_apply_status == PENDING_REBOOT_APPLY_STATUS
                }
            else:
                user_identifiers = [
                    cluster_row.db_cluster_identifier
                    for cluster_row in cluster_rows
                    if cluster_row.cluster_parameter_group_name == parameter_group_row.parameter_group_name
                ]
            records.append(
                _parameter_group_record(
                    parameter_group_row,
                    target,
                    snapshot,
                    adopted_addresses,
                    user_identifiers,
                    pending_reboot_ownership,
                    user_set_parameters[parameter_group_row.id],
                )
            )
    if ResourceKind.SNAPSHOT in kinds:
        database_snapshot_rows = session.execute(
            select(InventoryDbSnapshot).where(InventoryDbSnapshot.snapshot_id == snapshot.id)
        ).scalars()
        records += [
            _snapshot_record(database_snapshot_row, target, snapshot)
            for database_snapshot_row in database_snapshot_rows
        ]
    arns_with_open_drift = _arns_with_open_drift(session, target.target_key)
    pending_maintenance_actions = _pending_maintenance_actions_by_arn(session, snapshot.id)
    deployment_left_by_arn: dict[str, str] = {}
    for deployment in deployments_left_in_place(session, target.target_key):
        deployment_left_by_arn.setdefault(deployment.resource_arn, deployment.deployment_id)
    for record in records:
        record.facts["has_open_drift"] = record.arn in arns_with_open_drift
        if record.kind in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
            record.facts["pending_maintenance_actions"] = pending_maintenance_actions.get(record.arn, [])
            record.facts[BLUE_GREEN_DEPLOYMENT_LEFT_FACT] = deployment_left_by_arn.get(record.arn)
    return sorted(records, key=lambda record: (record.kind.value, record.identifier))


def list_resources(
    session: Session,
    console_config: ConsoleConfig,
    environment_slug: str,
    kind: ResourceKind | None = None,
) -> list[ResourceRecord]:
    """Every resource of an environment, optionally of one kind.

    Args:
        session: Control database session.
        console_config: Loaded console configuration, used to find the environment's targets.
        environment_slug: Environment to list.
        kind: Restrict to one kind; all listable kinds when None.

    Returns:
        Records from the latest inventory snapshot of each target, sorted by kind then identifier
        within each target. Targets that were never inventoried contribute nothing. Every record's
        `facts["has_open_drift"]` says whether an unresolved drift finding exists for it.

    Example:
        >>> list_resources(session, console_config, "sandbox", ResourceKind.CLUSTER)
        [ResourceRecord(kind=<ResourceKind.CLUSTER: 'cluster'>, identifier='demo-aurora', ...)]
    """
    kinds = {kind} if kind else set(ALL_LISTED_KINDS)
    records: list[ResourceRecord] = []
    for target in console_config.targets_for_environment(environment_slug):
        records += _records_for_target(session, target, kinds)
    return records


def load_resource(session: Session, console_config: ConsoleConfig, arn: str) -> ResourceRecord | None:
    """The record for `arn`, found in the target whose region and account the ARN names.

    Simulated targets are matched on region only, because the simulated AWS puts its own account
    id in ARNs rather than the configured one.

    Args:
        session: Control database session.
        console_config: Loaded console configuration, used to find candidate targets.
        arn: ARN of the resource.

    Returns:
        The record from the latest inventory snapshot, or None when the ARN is malformed, names no
        configured target, or is absent from that target's latest snapshot. `facts["has_open_drift"]`
        says whether an unresolved drift finding exists for it.

    Example:
        >>> load_resource(session, console_config, "arn:aws:rds:eu-west-2:000000000000:db:demo-orders")
        ResourceRecord(kind=<ResourceKind.INSTANCE: 'instance'>, identifier='demo-orders', ...)
    """
    arn_parts = arn.split(":")
    if len(arn_parts) < 6:
        return None
    arn_region, arn_account_id = arn_parts[3], arn_parts[4]
    for target in console_config.all_targets():
        if not _target_may_own_arn(target, arn_region, arn_account_id):
            continue
        for record in _records_for_target(session, target, set(ALL_LISTED_KINDS)):
            if record.arn == arn:
                return record
    return None


def find_parameter_group(
    session: Session, console_config: ConsoleConfig, target_key: str, scope: str, name: str
) -> ResourceRecord | None:
    """The parameter group named `name` of `scope` in the latest snapshot of one target.

    Instance and cluster groups have separate namespaces in AWS, so the scope is part of the identity.

    Args:
        session: Control database session.
        console_config: Loaded console configuration.
        target_key: The target whose latest snapshot is searched.
        scope: `instance` or `cluster`.
        name: Name of the group.

    Returns:
        The group's record, or None when the target was never inventoried or has no such group.

    Example:
        >>> find_parameter_group(  # doctest: +SKIP
        ...     session, console_config, "sandbox/000000000000/eu-west-2", "instance", "demo-postgres16-tuned"
        ... ).facts["parameter_group_family"]
        'postgres16'
    """
    return next(
        (
            record
            for record in _records_for_target(
                session, console_config.target(target_key), {ResourceKind.PARAMETER_GROUP}
            )
            if record.identifier == name and record.facts["group_scope"] == scope
        ),
        None,
    )


def recent_operations_for(session: Session, arn: str, limit: int = 10) -> list[Operation]:
    """Most recent operations whose `resource_arn` is `arn`.

    Args:
        session: Control database session.
        arn: ARN of the resource.
        limit: Maximum number of operations to return.

    Returns:
        Operations newest first.

    Example:
        >>> recent_operations_for(session, "arn:aws:rds:eu-west-2:000000000000:db:demo-orders", limit=5)
        [<Operation ...>]
    """
    return list(
        session.execute(
            select(Operation)
            .where(Operation.resource_arn == arn)
            .order_by(Operation.created_at_utc.desc())
            .limit(limit)
        ).scalars()
    )


def open_drift_findings_for(session: Session, arn: str) -> list[DriftFinding]:
    """Unresolved drift findings for the resource `arn`.

    Args:
        session: Control database session.
        arn: ARN of the resource.

    Returns:
        Open findings, record drift before state drift, newest first within each kind.

    Example:
        >>> demo_orders_arn = "arn:aws:rds:eu-west-2:000000000000:db:demo-orders"
        >>> [f.finding_kind for f in open_drift_findings_for(session, demo_orders_arn)]
        ['record', 'state']
    """
    return list(
        session.execute(
            select(DriftFinding)
            .where(DriftFinding.resource_arn == arn, DriftFinding.resolved_at_utc.is_(None))
            .order_by(DriftFinding.finding_kind, DriftFinding.detected_at_utc.desc())
        ).scalars()
    )


def pending_maintenance_for(
    session: Session, arn: str, snapshot_id: int
) -> list[InventoryPendingMaintenance]:
    """Maintenance actions AWS has queued for the resource `arn`, as of inventory snapshot `snapshot_id`.

    Args:
        session: Control database session.
        arn: ARN of the instance or cluster.
        snapshot_id: Inventory snapshot the resource's record was read from.

    Returns:
        The rows, ordered by action name.

    Example:
        >>> demo_orders_arn = "arn:aws:rds:eu-west-2:000000000000:db:demo-orders"
        >>> [row.action for row in pending_maintenance_for(session, demo_orders_arn, 3)]  # doctest: +SKIP
        ['system-update']
    """
    return list(
        session.execute(
            select(InventoryPendingMaintenance)
            .where(
                InventoryPendingMaintenance.resource_arn == arn,
                InventoryPendingMaintenance.snapshot_id == snapshot_id,
            )
            .order_by(InventoryPendingMaintenance.action, InventoryPendingMaintenance.id)
        ).scalars()
    )


def provenance_of_parameter_group(session: Session, record: ResourceRecord) -> dict[str, Any] | None:
    """How a parameter group was built, when the console built it for an upgrade.

    Args:
        session: Control database session.
        record: The parameter group's record.

    Returns:
        `{source_group_names, target_family, classification, operation_id}` of the newest build that
        created a group of this name and scope in this target; None for any other group.

    Example:
        >>> provenance = provenance_of_parameter_group(session, group_record)  # doctest: +SKIP
        >>> provenance["source_group_names"]  # doctest: +SKIP
        ['demo-aurora-pg16-tuned']
    """
    row = session.execute(
        select(ParameterGroupProvenance)
        .where(
            ParameterGroupProvenance.target_key == record.target_key,
            ParameterGroupProvenance.group_name == record.identifier,
            ParameterGroupProvenance.group_scope == record.facts.get("group_scope"),
        )
        .order_by(ParameterGroupProvenance.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "source_group_names": row.source_group_names,
        "target_family": row.target_family,
        "classification": row.classification,
        "operation_id": str(row.operation_id) if row.operation_id else None,
    }
