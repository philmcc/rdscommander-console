"""Discover a target's estate and persist it as one snapshot."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from rdscommander.aws.rds_reader import RdsReader, tags_to_mapping
from rdscommander.config.models import ResolvedTarget
from rdscommander.control_db.models import (
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryDbSnapshot,
    InventoryParameterGroup,
    InventoryParameterValue,
    InventoryPendingMaintenance,
    InventorySnapshot,
    InventorySubnetGroup,
)
from rdscommander.inventory.ownership import derive_ownership
from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass
class DiscoveredEstate:
    """Raw describe output for one target."""

    instances: list[dict[str, Any]] = field(default_factory=list)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    instance_parameter_groups: list[dict[str, Any]] = field(default_factory=list)
    cluster_parameter_groups: list[dict[str, Any]] = field(default_factory=list)
    parameters_by_group: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    subnet_groups: list[dict[str, Any]] = field(default_factory=list)
    db_snapshots: list[dict[str, Any]] = field(default_factory=list)
    cluster_snapshots: list[dict[str, Any]] = field(default_factory=list)
    pending_maintenance: list[dict[str, Any]] = field(default_factory=list)
    tags_by_arn: dict[str, dict[str, str]] = field(default_factory=dict)


def _tags_of_described_resource(
    reader: RdsReader, described_item: dict[str, Any], resource_arn: str
) -> dict[str, str]:
    """Tags of a resource, taken from its describe entry when that carries a `TagList`.

    An empty `TagList` means the resource has no tags and costs no call; only an entry without a `TagList`
    key falls back to `list_tags_for_resource`, so untagged resources (snapshots especially) do not add
    one lookup each per refresh.

    Args:
        reader: Reader used for the fallback lookup.
        described_item: The resource's describe entry.
        resource_arn: ARN looked up when the entry has no `TagList`.

    Returns:
        The tags as a plain mapping.
    """
    if "TagList" in described_item:
        return tags_to_mapping(described_item["TagList"])
    return reader.list_tags(resource_arn)


def discover_estate(reader: RdsReader) -> DiscoveredEstate:
    """Run every describe call for one target."""
    estate = DiscoveredEstate()
    estate.instances = reader.list_postgres_instances()
    estate.clusters = reader.list_postgres_clusters()
    estate.instance_parameter_groups = reader.list_instance_parameter_groups()
    estate.cluster_parameter_groups = reader.list_cluster_parameter_groups()
    estate.subnet_groups = reader.list_subnet_groups()
    estate.db_snapshots = reader.list_db_snapshots()
    estate.cluster_snapshots = reader.list_cluster_snapshots()
    estate.pending_maintenance = reader.list_pending_maintenance_actions()
    for group in estate.instance_parameter_groups:
        name = group["DBParameterGroupName"]
        estate.parameters_by_group[f"instance:{name}"] = reader.list_parameters(name)
    for group in estate.cluster_parameter_groups:
        name = group["DBClusterParameterGroupName"]
        estate.parameters_by_group[f"cluster:{name}"] = reader.list_cluster_parameters(name)
    for item in estate.instances:
        arn = item["DBInstanceArn"]
        estate.tags_by_arn[arn] = _tags_of_described_resource(reader, item, arn)
    for item in estate.clusters:
        arn = item["DBClusterArn"]
        estate.tags_by_arn[arn] = _tags_of_described_resource(reader, item, arn)
    for group in estate.instance_parameter_groups:
        estate.tags_by_arn[group["DBParameterGroupArn"]] = reader.list_tags(group["DBParameterGroupArn"])
    for group in estate.cluster_parameter_groups:
        arn = group["DBClusterParameterGroupArn"]
        estate.tags_by_arn[arn] = reader.list_tags(arn)
    for subnet_group in estate.subnet_groups:
        arn = subnet_group["DBSubnetGroupArn"]
        estate.tags_by_arn[arn] = reader.list_tags(arn)
    for snapshot_item in estate.db_snapshots:
        arn = snapshot_item["DBSnapshotArn"]
        estate.tags_by_arn[arn] = _tags_of_described_resource(reader, snapshot_item, arn)
    for snapshot_item in estate.cluster_snapshots:
        arn = snapshot_item["DBClusterSnapshotArn"]
        estate.tags_by_arn[arn] = _tags_of_described_resource(reader, snapshot_item, arn)
    return estate


def _is_default_group(name: str) -> bool:
    return name.startswith("default.") or name.startswith("default:")


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def write_snapshot(
    session: Session, target: ResolvedTarget, operation_id: uuid.UUID | None, discovered: DiscoveredEstate
) -> InventorySnapshot:
    """Persist `discovered` as a new snapshot for `target`; caller commits."""
    snapshot = InventorySnapshot(
        target_key=target.target_key,
        operation_id=operation_id,
        instance_count=len(discovered.instances),
        cluster_count=len(discovered.clusters),
        parameter_group_count=len(discovered.instance_parameter_groups)
        + len(discovered.cluster_parameter_groups),
        subnet_group_count=len(discovered.subnet_groups),
    )
    session.add(snapshot)
    session.flush()
    for item in discovered.instances:
        tags = discovered.tags_by_arn.get(item["DBInstanceArn"], {})
        session.add(
            InventoryDbInstance(
                snapshot_id=snapshot.id,
                db_instance_identifier=item["DBInstanceIdentifier"],
                db_instance_arn=item["DBInstanceArn"],
                engine=item["Engine"],
                engine_version=item.get("EngineVersion", ""),
                db_instance_class=item.get("DBInstanceClass", ""),
                db_instance_status=item.get("DBInstanceStatus", ""),
                db_cluster_identifier=item.get("DBClusterIdentifier"),
                read_replica_source_identifier=item.get("ReadReplicaSourceDBInstanceIdentifier"),
                parameter_group_name=(item.get("DBParameterGroups") or [{}])[0].get("DBParameterGroupName"),
                parameter_apply_status=(item.get("DBParameterGroups") or [{}])[0].get("ParameterApplyStatus"),
                db_subnet_group_name=(item.get("DBSubnetGroup") or {}).get("DBSubnetGroupName"),
                ownership=derive_ownership(tags),
                tags=tags,
                raw_description=_jsonable(item),
            )
        )
    for item in discovered.clusters:
        tags = discovered.tags_by_arn.get(item["DBClusterArn"], {})
        session.add(
            InventoryDbCluster(
                snapshot_id=snapshot.id,
                db_cluster_identifier=item["DBClusterIdentifier"],
                db_cluster_arn=item["DBClusterArn"],
                engine=item["Engine"],
                engine_version=item.get("EngineVersion", ""),
                status=item.get("Status", ""),
                cluster_parameter_group_name=item.get("DBClusterParameterGroup"),
                db_subnet_group_name=item.get("DBSubnetGroup"),
                member_instance_identifiers=[
                    m.get("DBInstanceIdentifier") for m in item.get("DBClusterMembers", [])
                ],
                ownership=derive_ownership(tags),
                tags=tags,
                raw_description=_jsonable(item),
            )
        )
    scopes = (
        ("instance", discovered.instance_parameter_groups, "DBParameterGroupName", "DBParameterGroupArn"),
        (
            "cluster",
            discovered.cluster_parameter_groups,
            "DBClusterParameterGroupName",
            "DBClusterParameterGroupArn",
        ),
    )
    for scope, groups, name_key, arn_key in scopes:
        for group in groups:
            name = group[name_key]
            tags = discovered.tags_by_arn.get(group[arn_key], {})
            parameters = discovered.parameters_by_group.get(f"{scope}:{name}", [])
            is_default = _is_default_group(name)
            row = InventoryParameterGroup(
                snapshot_id=snapshot.id,
                parameter_group_name=name,
                parameter_group_arn=group[arn_key],
                parameter_group_family=group["DBParameterGroupFamily"],
                group_scope=scope,
                description=group.get("Description", ""),
                is_default=is_default,
                user_set_parameter_count=0,
                ownership="unmanaged" if is_default else derive_ownership(tags),
                tags=tags,
                raw_description=_jsonable(group),
            )
            session.add(row)
            session.flush()
            user_set = 0
            for parameter in parameters:
                source = parameter.get("Source") or ("user" if not is_default else "engine-default")
                user_set += source == "user"
                session.add(
                    InventoryParameterValue(
                        parameter_group_id=row.id,
                        parameter_name=parameter["ParameterName"],
                        parameter_value=parameter.get("ParameterValue"),
                        source=source,
                        apply_type=parameter.get("ApplyType", "dynamic"),
                        apply_method=parameter.get("ApplyMethod"),
                        data_type=parameter.get("DataType"),
                        is_modifiable=bool(parameter.get("IsModifiable", True)),
                        description=parameter.get("Description"),
                    )
                )
            row.user_set_parameter_count = user_set
    for item in discovered.subnet_groups:
        tags = discovered.tags_by_arn.get(item["DBSubnetGroupArn"], {})
        session.add(
            InventorySubnetGroup(
                snapshot_id=snapshot.id,
                db_subnet_group_name=item["DBSubnetGroupName"],
                db_subnet_group_arn=item["DBSubnetGroupArn"],
                vpc_id=item.get("VpcId", ""),
                subnet_ids=[s.get("SubnetIdentifier") for s in item.get("Subnets", [])],
                description=item.get("DBSubnetGroupDescription", ""),
                ownership=derive_ownership(tags),
                tags=tags,
                raw_description=_jsonable(item),
            )
        )
    snapshot_sources = (
        (
            "instance",
            discovered.db_snapshots,
            "DBSnapshotIdentifier",
            "DBSnapshotArn",
            "DBInstanceIdentifier",
        ),
        (
            "cluster",
            discovered.cluster_snapshots,
            "DBClusterSnapshotIdentifier",
            "DBClusterSnapshotArn",
            "DBClusterIdentifier",
        ),
    )
    for snapshot_scope, snapshot_items, identifier_key, arn_key, source_identifier_key in snapshot_sources:
        for item in snapshot_items:
            session.add(
                InventoryDbSnapshot(
                    snapshot_id=snapshot.id,
                    snapshot_scope=snapshot_scope,
                    db_snapshot_identifier=item[identifier_key],
                    db_snapshot_arn=item[arn_key],
                    source_identifier=item.get(source_identifier_key, ""),
                    snapshot_type=item.get("SnapshotType", "manual"),
                    status=item.get("Status", ""),
                    engine_version=item.get("EngineVersion", ""),
                    allocated_storage_gb=item.get("AllocatedStorage"),
                    created_at_source_utc=item.get("SnapshotCreateTime"),
                    tags=discovered.tags_by_arn.get(item[arn_key], {}),
                    raw_description=_jsonable(item),
                )
            )
    for action_set in discovered.pending_maintenance:
        for detail in action_set.get("PendingMaintenanceActionDetails", []):
            session.add(
                InventoryPendingMaintenance(
                    snapshot_id=snapshot.id,
                    resource_arn=action_set.get("ResourceIdentifier", ""),
                    action=detail.get("Action", ""),
                    description=detail.get("Description", ""),
                    auto_applied_after_utc=detail.get("AutoAppliedAfterDate"),
                    current_apply_utc=detail.get("CurrentApplyDate"),
                    opt_in_status=detail.get("OptInStatus"),
                )
            )
    session.flush()
    logger.info(
        "inventory_snapshot_written",
        target_key=target.target_key,
        snapshot_id=snapshot.id,
        instance_count=snapshot.instance_count,
        cluster_count=snapshot.cluster_count,
        db_snapshot_count=len(discovered.db_snapshots),
        cluster_snapshot_count=len(discovered.cluster_snapshots),
        pending_maintenance_action_count=sum(
            len(action_set.get("PendingMaintenanceActionDetails", []))
            for action_set in discovered.pending_maintenance
        ),
    )
    return snapshot
