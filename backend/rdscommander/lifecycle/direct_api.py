"""Direct RDS calls the lifecycle operations make, each recorded under the running step."""

from __future__ import annotations

from typing import Any, Literal

from botocore.exceptions import ClientError

from rdscommander.aws.rds_reader import UNSUPPORTED_MARKER, SimulatorUnsupportedAction
from rdscommander.operations.executor import StepContext

SnapshotScope = Literal["instance", "cluster"]
"""Whether a snapshot is of an instance or of a cluster."""
INSTANCE_NOT_FOUND_ERROR_CODE = "DBInstanceNotFound"
"""Error code RDS answers a describe of an instance that does not exist with."""
CLUSTER_NOT_FOUND_ERROR_CODES = frozenset({"DBClusterNotFoundFault", "DBClusterNotFound"})
"""Error codes RDS answers a describe of a cluster that does not exist with."""
SNAPSHOT_NOT_FOUND_ERROR_CODES = frozenset({"DBSnapshotNotFound", "DBClusterSnapshotNotFoundFault"})
"""Error codes RDS answers a describe of a snapshot that does not exist with."""


def _rds(context: StepContext, step_name: str) -> Any:
    """A recorded RDS client for the running operation's target, filed under `step_name`."""
    return context.services.aws_sessions.client(
        context.target, "rds", operation_id=context.operation.id, step_name=step_name
    )


def orderable_instance_classes(rds_or_context: Any, engine: str, engine_version: str) -> list[str]:
    """Instance classes AWS offers for an engine version, sorted and de-duplicated.

    Args:
        rds_or_context: A boto3 RDS client, or a `StepContext` (a recorded client is opened under the
            step `orderable_instance_classes`).
        engine: RDS engine name, such as `postgres` or `aurora-postgresql`.
        engine_version: Engine version, such as `16.4`.

    Returns:
        The offered instance classes, each starting with `db.`.

    Raises:
        SimulatorUnsupportedAction: The simulated account does not implement the call.

    Example:
        >>> orderable_instance_classes(rds, "postgres", "16.4")[:1]  # doctest: +SKIP
        ['db.m6g.large']
    """
    rds = (
        _rds(rds_or_context, "orderable_instance_classes")
        if isinstance(rds_or_context, StepContext)
        else rds_or_context
    )
    paginator = rds.get_paginator("describe_orderable_db_instance_options")
    classes: set[str] = set()
    try:
        for page in paginator.paginate(Engine=engine, EngineVersion=engine_version):
            classes.update(option["DBInstanceClass"] for option in page["OrderableDBInstanceOptions"])
    except Exception as error:  # noqa: BLE001 - only the simulator's "not implemented" is translated
        if UNSUPPORTED_MARKER in str(error):
            raise SimulatorUnsupportedAction("describe_orderable_db_instance_options") from error
        raise
    return sorted(classes)


def cluster_status(
    context: StepContext, identifier: str, step_name: str = "await_settled"
) -> tuple[str, str | None]:
    """The cluster's status and its current writer's identifier.

    Args:
        context: The running step's context.
        identifier: Cluster identifier.
        step_name: Step the AWS call is recorded under.

    Returns:
        `(status, writer_identifier)`; the writer is None when no member is the writer.

    Example:
        >>> cluster_status(context, "demo-aurora")  # doctest: +SKIP
        ('available', 'demo-aurora-writer')
    """
    cluster = _rds(context, step_name).describe_db_clusters(DBClusterIdentifier=identifier)["DBClusters"][0]
    writer_identifier = next(
        (
            member["DBInstanceIdentifier"]
            for member in cluster.get("DBClusterMembers", [])
            if member.get("IsClusterWriter")
        ),
        None,
    )
    return cluster["Status"], writer_identifier


def failover_cluster(
    context: StepContext, identifier: str, target_member: str | None, step_name: str = "perform"
) -> None:
    """`FailoverDBCluster`, to a chosen member when given.

    Args:
        context: The running step's context.
        identifier: Cluster identifier.
        target_member: The member to promote, or None to let AWS choose.
        step_name: Step the AWS call is recorded under.

    Example:
        >>> failover_cluster(context, "demo-aurora", "demo-aurora-reader")  # doctest: +SKIP
    """
    failover_arguments: dict[str, str] = {"DBClusterIdentifier": identifier}
    if target_member:
        failover_arguments["TargetDBInstanceIdentifier"] = target_member
    _rds(context, step_name).failover_db_cluster(**failover_arguments)


def apply_pending_maintenance(
    context: StepContext, arn: str, action: str, opt_in_type: str, step_name: str = "perform"
) -> None:
    """`ApplyPendingMaintenanceAction`; raises SimulatorUnsupportedAction on the simulator.

    Args:
        context: The running step's context.
        arn: ARN of the instance or cluster.
        action: The pending action's name, such as `system-update`.
        opt_in_type: `immediate`, `next-maintenance` or `undo-opt-in`.
        step_name: Step the AWS call is recorded under.

    Raises:
        SimulatorUnsupportedAction: The simulated account does not implement the call.

    Example:
        >>> apply_pending_maintenance(  # doctest: +SKIP
        ...     context, orders_arn, "system-update", "next-maintenance"
        ... )
    """
    try:
        _rds(context, step_name).apply_pending_maintenance_action(
            ResourceIdentifier=arn, ApplyAction=action, OptInType=opt_in_type
        )
    except Exception as error:  # noqa: BLE001 - the simulator answers with a reworded 500 page
        if UNSUPPORTED_MARKER in str(error):
            raise SimulatorUnsupportedAction("ApplyPendingMaintenanceAction") from error
        raise


def create_snapshot(
    context: StepContext,
    scope: SnapshotScope,
    source_identifier: str,
    snapshot_identifier: str,
    tags: dict[str, str],
    step_name: str = "perform",
) -> str:
    """`CreateDBSnapshot` or `CreateDBClusterSnapshot`, with tags.

    Args:
        context: The running step's context.
        scope: `instance` or `cluster`.
        source_identifier: The instance or cluster to snapshot.
        snapshot_identifier: The new snapshot's name.
        tags: Tags to put on the snapshot.
        step_name: Step the AWS call is recorded under.

    Returns:
        The new snapshot's ARN.

    Example:
        >>> create_snapshot(  # doctest: +SKIP
        ...     context, "instance", "demo-orders", "demo-orders-before-change", {}
        ... )
        'arn:aws:rds:eu-west-2:000000000000:snapshot:demo-orders-before-change'
    """
    tag_list = [{"Key": key, "Value": value} for key, value in sorted(tags.items())]
    rds = _rds(context, step_name)
    if scope == "cluster":
        response = rds.create_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=snapshot_identifier,
            DBClusterIdentifier=source_identifier,
            Tags=tag_list,
        )
        return response["DBClusterSnapshot"]["DBClusterSnapshotArn"]
    response = rds.create_db_snapshot(
        DBSnapshotIdentifier=snapshot_identifier, DBInstanceIdentifier=source_identifier, Tags=tag_list
    )
    return response["DBSnapshot"]["DBSnapshotArn"]


def snapshot_status(
    context: StepContext, scope: SnapshotScope, snapshot_identifier: str, step_name: str = "await_settled"
) -> str | None:
    """The snapshot's status (`creating`, `available`, ...), or None when it does not exist.

    Args:
        context: The running step's context.
        scope: `instance` or `cluster`.
        snapshot_identifier: The snapshot's name.
        step_name: Step the AWS call is recorded under.

    Returns:
        The status, or None once the snapshot is gone.

    Example:
        >>> snapshot_status(context, "instance", "demo-orders-before-change")  # doctest: +SKIP
        'available'
    """
    rds = _rds(context, step_name)
    try:
        if scope == "cluster":
            described = rds.describe_db_cluster_snapshots(DBClusterSnapshotIdentifier=snapshot_identifier)
            return described["DBClusterSnapshots"][0]["Status"]
        described = rds.describe_db_snapshots(DBSnapshotIdentifier=snapshot_identifier)
        return described["DBSnapshots"][0]["Status"]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in SNAPSHOT_NOT_FOUND_ERROR_CODES:
            return None
        raise


def delete_snapshot(
    context: StepContext, scope: SnapshotScope, snapshot_identifier: str, step_name: str = "perform"
) -> None:
    """`DeleteDBSnapshot` or `DeleteDBClusterSnapshot`.

    Args:
        context: The running step's context.
        scope: `instance` or `cluster`.
        snapshot_identifier: The snapshot's name.
        step_name: Step the AWS call is recorded under.

    Example:
        >>> delete_snapshot(context, "instance", "demo-orders-before-change")  # doctest: +SKIP
    """
    rds = _rds(context, step_name)
    if scope == "cluster":
        rds.delete_db_cluster_snapshot(DBClusterSnapshotIdentifier=snapshot_identifier)
    else:
        rds.delete_db_snapshot(DBSnapshotIdentifier=snapshot_identifier)


def promote_read_replica(
    context: StepContext, identifier: str, backup_retention_days: int, step_name: str = "perform"
) -> None:
    """`PromoteReadReplica`: the replica stops replicating and becomes a standalone instance.

    Args:
        context: The running step's context.
        identifier: The replica's identifier.
        backup_retention_days: Days of automated backups the promoted instance keeps.
        step_name: Step the AWS call is recorded under.

    Example:
        >>> promote_read_replica(context, "demo-orders-replica", 7)  # doctest: +SKIP
    """
    _rds(context, step_name).promote_read_replica(
        DBInstanceIdentifier=identifier, BackupRetentionPeriod=backup_retention_days
    )


def instance_describe(context: StepContext, identifier: str, step_name: str = "await_settled") -> dict | None:
    """The instance as `DescribeDBInstances` reports it, or None when AWS does not know it.

    Args:
        context: The running step's context.
        identifier: The instance's identifier.
        step_name: Step the AWS call is recorded under.

    Returns:
        The `DBInstances` entry, or None once the instance is gone.

    Example:
        >>> instance_describe(context, "demo-orders-replica")["DBInstanceStatus"]  # doctest: +SKIP
        'available'
    """
    try:
        described = _rds(context, step_name).describe_db_instances(DBInstanceIdentifier=identifier)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == INSTANCE_NOT_FOUND_ERROR_CODE:
            return None
        raise
    return described["DBInstances"][0]


def delete_instance(
    context: StepContext, identifier: str, final_snapshot_identifier: str | None, step_name: str = "perform"
) -> None:
    """`DeleteDBInstance`, with a final snapshot when a name is given and without one otherwise.

    Args:
        context: The running step's context.
        identifier: The instance to delete.
        final_snapshot_identifier: Name of the snapshot AWS takes first, or None to skip it (a reader of an
            Aurora cluster holds no data of its own).
        step_name: Step the AWS call is recorded under.

    Example:
        >>> delete_instance(context, "demo-aurora-reader", None)  # doctest: +SKIP
    """
    rds = _rds(context, step_name)
    if final_snapshot_identifier:
        rds.delete_db_instance(
            DBInstanceIdentifier=identifier,
            SkipFinalSnapshot=False,
            FinalDBSnapshotIdentifier=final_snapshot_identifier,
        )
    else:
        rds.delete_db_instance(DBInstanceIdentifier=identifier, SkipFinalSnapshot=True)


def delete_cluster(
    context: StepContext, identifier: str, final_snapshot_identifier: str | None, step_name: str = "perform"
) -> None:
    """`DeleteDBCluster`, with a final cluster snapshot when a name is given and without one otherwise.

    AWS refuses while the cluster still has members, so they are deleted first (`delete_instance`).

    Args:
        context: The running step's context.
        identifier: The cluster to delete.
        final_snapshot_identifier: Name of the cluster snapshot AWS takes first, or None to skip it.
        step_name: Step the AWS call is recorded under.

    Example:
        >>> delete_cluster(context, "demo-aurora", "demo-aurora-final")  # doctest: +SKIP
    """
    rds = _rds(context, step_name)
    if final_snapshot_identifier:
        rds.delete_db_cluster(
            DBClusterIdentifier=identifier,
            SkipFinalSnapshot=False,
            FinalDBSnapshotIdentifier=final_snapshot_identifier,
        )
    else:
        rds.delete_db_cluster(DBClusterIdentifier=identifier, SkipFinalSnapshot=True)


def tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    """`{key: value}` as the `[{"Key", "Value"}]` list RDS create calls take, sorted by key.

    Example:
        >>> tag_list({"rdscommander:managed": "true"})
        [{'Key': 'rdscommander:managed', 'Value': 'true'}]
    """
    return [{"Key": key, "Value": value} for key, value in sorted(tags.items())]


def create_read_replica(
    context: StepContext, arguments: dict[str, Any], step_name: str = "apply_direct"
) -> str:
    """`CreateDBInstanceReadReplica` with the request the change built from its rendered model.

    Args:
        context: The running step's context.
        arguments: The request (`ReadReplicaCreate.create_arguments`): identifier, source, class and every
            setting the rendered replica carries.
        step_name: Step the AWS call is recorded under.

    Returns:
        The new replica's ARN.

    Example:
        >>> create_read_replica(context, change.create_arguments(context))  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:db:demo-orders-replica-2'
    """
    response = _rds(context, step_name).create_db_instance_read_replica(**arguments)
    return response["DBInstance"]["DBInstanceArn"]


def create_cluster_member(
    context: StepContext, arguments: dict[str, Any], step_name: str = "apply_direct"
) -> str:
    """`CreateDBInstance` of an Aurora cluster member, with the request built from its rendered model.

    Args:
        context: The running step's context.
        arguments: The request (`ClusterReaderCreate.create_arguments`); the cluster holds storage, users and
            backups, so none of those are in it.
        step_name: Step the AWS call is recorded under.

    Returns:
        The new member's ARN.

    Example:
        >>> create_cluster_member(context, change.create_arguments(context))  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:db:demo-aurora-reader-2'
    """
    response = _rds(context, step_name).create_db_instance(**arguments)
    return response["DBInstance"]["DBInstanceArn"]


def cluster_describe(context: StepContext, identifier: str, step_name: str = "await_settled") -> dict | None:
    """The cluster as `DescribeDBClusters` reports it, or None when AWS does not know it.

    Args:
        context: The running step's context.
        identifier: The cluster's identifier.
        step_name: Step the AWS call is recorded under.

    Returns:
        The `DBClusters` entry, or None when there is no such cluster.

    Example:
        >>> cluster_describe(context, "demo-aurora")["Status"]  # doctest: +SKIP
        'available'
    """
    try:
        described = _rds(context, step_name).describe_db_clusters(DBClusterIdentifier=identifier)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in CLUSTER_NOT_FOUND_ERROR_CODES:
            return None
        raise
    return described["DBClusters"][0]


def restore_instance_from_snapshot(
    context: StepContext, arguments: dict[str, Any], step_name: str = "apply_direct"
) -> str:
    """`RestoreDBInstanceFromDBSnapshot` with the request the change built from its rendered model.

    Args:
        context: The running step's context.
        arguments: The request (`SnapshotRestoreCreate.create_arguments`): identifier, snapshot, class and
            every setting of the rendered instance the call takes.
        step_name: Step the AWS call is recorded under.

    Returns:
        The new instance's ARN.

    Example:
        >>> restore_instance_from_snapshot(context, change.create_arguments(context))  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:db:demo-orders-restored'
    """
    response = _rds(context, step_name).restore_db_instance_from_db_snapshot(**arguments)
    return response["DBInstance"]["DBInstanceArn"]


def restore_cluster_from_snapshot(
    context: StepContext, arguments: dict[str, Any], step_name: str = "apply_direct"
) -> str:
    """`RestoreDBClusterFromSnapshot` with the request the change built from its rendered model.

    The cluster has no instance yet; the change adds its writer with `create_cluster_member`.

    Args:
        context: The running step's context.
        arguments: The request (`SnapshotRestoreCreate.create_arguments`).
        step_name: Step the AWS call is recorded under.

    Returns:
        The new cluster's ARN.

    Example:
        >>> restore_cluster_from_snapshot(context, change.create_arguments(context))  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:cluster:demo-aurora-restored'
    """
    response = _rds(context, step_name).restore_db_cluster_from_snapshot(**arguments)
    return response["DBCluster"]["DBClusterArn"]


def restore_instance_to_point_in_time(
    context: StepContext, arguments: dict[str, Any], step_name: str = "apply_direct"
) -> str:
    """`RestoreDBInstanceToPointInTime` with the request the change built from its rendered model.

    Args:
        context: The running step's context.
        arguments: The request (`PointInTimeRestoreCreate.create_arguments`): source, target, the time or
            `UseLatestRestorableTime`, and every setting of the rendered instance the call takes.
        step_name: Step the AWS call is recorded under.

    Returns:
        The new instance's ARN.

    Example:
        >>> restore_instance_to_point_in_time(context, change.create_arguments(context))  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:db:demo-orders-pitr'
    """
    response = _rds(context, step_name).restore_db_instance_to_point_in_time(**arguments)
    return response["DBInstance"]["DBInstanceArn"]


def restore_cluster_to_point_in_time(
    context: StepContext, arguments: dict[str, Any], step_name: str = "apply_direct"
) -> str:
    """`RestoreDBClusterToPointInTime` with the request the change built from its rendered model.

    The cluster has no instance yet; the change adds its writer with `create_cluster_member`.

    Args:
        context: The running step's context.
        arguments: The request (`PointInTimeRestoreCreate.create_arguments`).
        step_name: Step the AWS call is recorded under.

    Returns:
        The new cluster's ARN.

    Example:
        >>> restore_cluster_to_point_in_time(context, change.create_arguments(context))  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:cluster:demo-aurora-pitr'
    """
    response = _rds(context, step_name).restore_db_cluster_to_point_in_time(**arguments)
    return response["DBCluster"]["DBClusterArn"]


def create_instance(context: StepContext, arguments: dict[str, Any], step_name: str = "apply_direct") -> str:
    """`CreateDBInstance` of a standalone instance, with the request built from its rendered model.

    The request carries `ManageMasterUserPassword=True` and never a password: AWS creates the password and
    keeps it in Secrets Manager.

    Args:
        context: The running step's context.
        arguments: The request (`InstanceCreate.create_arguments`).
        step_name: Step the AWS call is recorded under.

    Returns:
        The new instance's ARN.

    Example:
        >>> create_instance(context, change.create_arguments(context))  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:db:demo-reports'
    """
    response = _rds(context, step_name).create_db_instance(**arguments)
    return response["DBInstance"]["DBInstanceArn"]


def create_cluster(context: StepContext, arguments: dict[str, Any], step_name: str = "apply_direct") -> str:
    """`CreateDBCluster` with the request built from its rendered model; its members are created next.

    The request carries `ManageMasterUserPassword=True` and never a password: AWS creates the password and
    keeps it in Secrets Manager.

    Args:
        context: The running step's context.
        arguments: The request (`ClusterCreate.create_arguments`).
        step_name: Step the AWS call is recorded under.

    Returns:
        The new cluster's ARN.

    Example:
        >>> create_cluster(context, change.create_arguments(context))  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:cluster:demo-analytics'
    """
    response = _rds(context, step_name).create_db_cluster(**arguments)
    return response["DBCluster"]["DBClusterArn"]
