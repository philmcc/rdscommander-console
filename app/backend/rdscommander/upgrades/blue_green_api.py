"""The RDS blue/green deployment API and the console's record of each deployment.

Every AWS call goes through a recorded client, so each one is filed as an execution record under the
running operation and the step name the caller passes. The rows in `blue_green_deployments` follow a
deployment from creation to switchover or deletion.

Example:
    >>> deployment_id = create_deployment(  # doctest: +SKIP
    ...     context, "demo-orders-upgrade", orders_arn, "17.2", "demo-postgres17", None, "create"
    ... )
    >>> describe_deployment(context, deployment_id, "await_green_available").status  # doctest: +SKIP
    'AVAILABLE'
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.control_db.models import BlueGreenDeployment, Operation, utc_now
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import OperationState
from rdscommander.operations.executor import StepContext

logger = get_logger(__name__)

DEPLOYMENT_NOT_FOUND_ERROR_CODE = "BlueGreenDeploymentNotFoundFault"
"""Error code RDS answers a describe, switchover or delete of a deployment that does not exist with."""
SWITCHOVER_COMPLETED_STATUS = "SWITCHOVER_COMPLETED"
"""Status of a deployment whose switchover has finished."""
SWITCHOVER_IN_PROGRESS_STATUS = "SWITCHOVER_IN_PROGRESS"
"""Status of a deployment AWS is switching over."""
SWITCHOVER_FAILED_STATUS = "SWITCHOVER_FAILED"
"""Status of a deployment whose switchover AWS could not finish (it rolled back)."""
DELETING_STATUS = "DELETING"
"""Status RDS gives a deployment it is deleting."""
BLUE_SIDE_TAG_KEY = "rdscommander:blue-side"
"""Tag the console puts on the blue side after a switchover; its value is the major upgrade's id."""
BLUE_SIDE_SUFFIX = "-old1"
"""What AWS appends to the blue side's identifiers when it switches production to the green side."""
FINISHED_OPERATION_STATES = (
    OperationState.FAILED.value,
    OperationState.CANCELLED.value,
    OperationState.SUCCEEDED.value,
)
"""States of an operation that no longer drives its deployment, so the deployment may be cleaned up."""


@dataclass(frozen=True)
class BlueGreenStatus:
    """What AWS reports about one blue/green deployment.

    Attributes:
        status: AWS status, such as `PROVISIONING`, `AVAILABLE` or `SWITCHOVER_COMPLETED`.
        green_endpoint: Address of the green side, or None while AWS reports none.
        green_arn: ARN of the green instance or cluster, or None before it exists.
        switchover_details: AWS `SwitchoverDetails`, one entry per member pair.
        tasks: AWS `Tasks`, the deployment's progress steps with their statuses.
        status_details: AWS `StatusDetails`, the reason behind a status such as `INVALID_CONFIGURATION`.

    Example:
        >>> BlueGreenStatus("AVAILABLE", None, None).status
        'AVAILABLE'
    """

    status: str
    green_endpoint: str | None
    green_arn: str | None
    switchover_details: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    status_details: str | None = None


def _recorded_rds_client(context: StepContext, step_name: str) -> Any:
    """A recorded RDS client for the running operation's target, filed under `step_name`."""
    return context.services.aws_sessions.client(
        context.target, "rds", operation_id=context.operation.id, step_name=step_name
    )


@contextmanager
def _logged_call(event_prefix: str, **log_fields: Any) -> Iterator[None]:
    """Log the start and completion of a call, and the AWS error code if it fails (then re-raise)."""
    logger.info(f"{event_prefix}_started", **log_fields)
    try:
        yield
    except ClientError as error:
        logger.error(
            f"{event_prefix}_failed",
            aws_error_code=error.response.get("Error", {}).get("Code"),
            aws_error_message=error.response.get("Error", {}).get("Message"),
            fix_suggestion=(
                "Read the AWS reason in aws_error_message; the deployment is left as it is. "
                "Fix the cause and retry, or clean the deployment up from the resource page."
            ),
            **log_fields,
        )
        raise
    logger.info(f"{event_prefix}_completed", **log_fields)


def is_deployment_not_found(error: ClientError) -> bool:
    """Whether an AWS error says the deployment does not exist.

    Args:
        error: The error a describe, switchover or delete raised.

    Returns:
        True for `BlueGreenDeploymentNotFoundFault`.

    Example:
        >>> is_deployment_not_found(error)  # doctest: +SKIP
        True
    """
    return error.response.get("Error", {}).get("Code") == DEPLOYMENT_NOT_FOUND_ERROR_CODE


def create_deployment(
    context: StepContext,
    name: str,
    source_arn: str,
    target_engine_version: str,
    target_parameter_group_name: str,
    target_cluster_parameter_group_name: str | None,
    step_name: str,
) -> str:
    """`CreateBlueGreenDeployment` for an instance or cluster, upgrading the green side's engine version.

    Args:
        context: The running step's context.
        name: Name of the deployment, unique in the account and region.
        source_arn: ARN of the blue instance or cluster.
        target_engine_version: Engine version the green side runs.
        target_parameter_group_name: Instance parameter group of the green side's target family.
        target_cluster_parameter_group_name: Cluster parameter group for the green side of a cluster,
            or None for an instance (the argument is then not sent).
        step_name: Step the AWS call is recorded under.

    Returns:
        The deployment identifier AWS assigned, such as `bgd-0123456789abcdef`.

    Raises:
        botocore.exceptions.ClientError: AWS refused the deployment; the reason is in the error.

    Example:
        >>> create_deployment(  # doctest: +SKIP
        ...     context, "demo-orders-upgrade", orders_arn, "17.2", "demo-postgres17", None, "create"
        ... )
        'bgd-0123456789abcdef'
    """
    arguments: dict[str, str] = {
        "BlueGreenDeploymentName": name,
        "Source": source_arn,
        "TargetEngineVersion": target_engine_version,
        "TargetDBParameterGroupName": target_parameter_group_name,
    }
    if target_cluster_parameter_group_name:
        arguments["TargetDBClusterParameterGroupName"] = target_cluster_parameter_group_name
    with _logged_call(
        "blue_green_deployment_creation",
        operation_id=str(context.operation.id),
        deployment_name=name,
        source_arn=source_arn,
        target_engine_version=target_engine_version,
    ):
        response = _recorded_rds_client(context, step_name).create_blue_green_deployment(**arguments)
    return response["BlueGreenDeployment"]["BlueGreenDeploymentIdentifier"]


def _green_endpoint(rds: Any, green_arn: str | None) -> str | None:
    """The green instance's or cluster's endpoint address, or None when it has none yet or is gone."""
    if green_arn is None:
        return None
    green_identifier = green_arn.rsplit(":", 1)[-1]
    try:
        if ":cluster:" in green_arn:
            green = rds.describe_db_clusters(DBClusterIdentifier=green_identifier)["DBClusters"][0]
            return green.get("Endpoint") or None
        green = rds.describe_db_instances(DBInstanceIdentifier=green_identifier)["DBInstances"][0]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {
            "DBInstanceNotFound",
            "DBClusterNotFoundFault",
            "DBClusterNotFound",
        }:
            return None
        raise
    return (green.get("Endpoint") or {}).get("Address") or None


def describe_deployment(context: StepContext, deployment_id: str, step_name: str) -> BlueGreenStatus:
    """`DescribeBlueGreenDeployments` for one deployment, plus the green side's endpoint.

    Args:
        context: The running step's context.
        deployment_id: Deployment identifier.
        step_name: Step the AWS calls are recorded under.

    Returns:
        The deployment's status, green ARN and endpoint, switchover details and tasks.

    Raises:
        botocore.exceptions.ClientError: The deployment does not exist (see `is_deployment_not_found`)
            or AWS refused the call.

    Example:
        >>> describe_deployment(context, "bgd-0123456789abcdef", "await").status  # doctest: +SKIP
        'AVAILABLE'
    """
    rds = _recorded_rds_client(context, step_name)
    deployment = rds.describe_blue_green_deployments(BlueGreenDeploymentIdentifier=deployment_id)[
        "BlueGreenDeployments"
    ][0]
    green_arn = deployment.get("Target") or None
    return BlueGreenStatus(
        status=deployment["Status"],
        green_endpoint=_green_endpoint(rds, green_arn),
        green_arn=green_arn,
        switchover_details=list(deployment.get("SwitchoverDetails", [])),
        tasks=list(deployment.get("Tasks", [])),
        status_details=deployment.get("StatusDetails") or None,
    )


def find_deployment_by_name(context: StepContext, name: str, step_name: str) -> str | None:
    """The identifier of the deployment named `name`, or None when AWS has none of that name.

    A step that creates a deployment looks first, so that a crash between AWS accepting the creation and
    the console storing the identifier does not create a second deployment.

    Args:
        context: The running step's context.
        name: The deployment name the step creates.
        step_name: Step the AWS call is recorded under.

    Returns:
        The deployment identifier, such as `bgd-0123456789abcdef`, or None.

    Example:
        >>> find_deployment_by_name(context, "demo-orders-pg17-1a2b3c4d", "create")  # doctest: +SKIP
        'bgd-0123456789abcdef'
    """
    deployments = _recorded_rds_client(context, step_name).describe_blue_green_deployments(
        Filters=[{"Name": "blue-green-deployment-name", "Values": [name]}]
    )["BlueGreenDeployments"]
    return deployments[0]["BlueGreenDeploymentIdentifier"] if deployments else None


def switchover(context: StepContext, deployment_id: str, timeout_seconds: int, step_name: str) -> None:
    """`SwitchoverBlueGreenDeployment`: promote the green side, renaming the blue side `<name>-old1`.

    Args:
        context: The running step's context.
        deployment_id: Deployment identifier.
        timeout_seconds: Longest AWS may take; it rolls back past this.
        step_name: Step the AWS call is recorded under.

    Raises:
        botocore.exceptions.ClientError: AWS refused the switchover (for example the deployment is not
            `AVAILABLE`); the reason is in the error.

    Example:
        >>> switchover(context, "bgd-0123456789abcdef", 600, "switchover")  # doctest: +SKIP
    """
    with _logged_call(
        "blue_green_switchover",
        operation_id=str(context.operation.id),
        deployment_id=deployment_id,
        timeout_seconds=timeout_seconds,
    ):
        _recorded_rds_client(context, step_name).switchover_blue_green_deployment(
            BlueGreenDeploymentIdentifier=deployment_id, SwitchoverTimeout=timeout_seconds
        )


def delete_deployment(context: StepContext, deployment_id: str, delete_target: bool, step_name: str) -> None:
    """`DeleteBlueGreenDeployment`, deleting the green side too when asked.

    Args:
        context: The running step's context.
        deployment_id: Deployment identifier.
        delete_target: Whether to delete the green instances or cluster as well.
        step_name: Step the AWS call is recorded under.

    Raises:
        botocore.exceptions.ClientError: AWS refused the deletion; the reason is in the error.

    Example:
        >>> delete_deployment(context, "bgd-0123456789abcdef", True, "perform")  # doctest: +SKIP
    """
    with _logged_call(
        "blue_green_deletion",
        operation_id=str(context.operation.id),
        deployment_id=deployment_id,
        delete_target=delete_target,
    ):
        _recorded_rds_client(context, step_name).delete_blue_green_deployment(
            BlueGreenDeploymentIdentifier=deployment_id, DeleteTarget=delete_target
        )


def record_deployment(
    session: Session,
    deployment_id: str,
    operation_id: uuid.UUID,
    resource_arn: str,
    environment_slug: str,
    target_key: str,
    status: str,
    target_engine_version: str,
    green_endpoint: str | None = None,
) -> BlueGreenDeployment:
    """Add the row that follows a deployment the console just created (the caller commits).

    Args:
        session: Control database session.
        deployment_id: Deployment identifier AWS assigned.
        operation_id: The major upgrade that created it.
        resource_arn: ARN of the blue instance or cluster.
        environment_slug: Environment of the blue resource.
        target_key: Target (account and region) it lives in.
        status: Status AWS reported at creation.
        target_engine_version: Engine version the green side runs.
        green_endpoint: Green endpoint, if already known.

    Returns:
        The new row.

    Example:
        >>> record_deployment(  # doctest: +SKIP
        ...     session, "bgd-0123", operation.id, orders_arn, "prod", "prod/1/eu-west-2", "NEW", "17.2"
        ... ).status
        'NEW'
    """
    recorded_at_utc = utc_now()
    deployment = BlueGreenDeployment(
        deployment_id=deployment_id,
        operation_id=operation_id,
        resource_arn=resource_arn,
        environment_slug=environment_slug,
        target_key=target_key,
        status=status,
        green_endpoint=green_endpoint,
        target_engine_version=target_engine_version,
        created_at_utc=recorded_at_utc,
        updated_at_utc=recorded_at_utc,
    )
    session.add(deployment)
    logger.info(
        "blue_green_deployment_recorded",
        deployment_id=deployment_id,
        resource_arn=resource_arn,
        status=status,
    )
    return deployment


def _recorded_deployment(session: Session, deployment_id: str) -> BlueGreenDeployment:
    """The row for `deployment_id`, or a `LookupError` naming it."""
    deployment = session.get(BlueGreenDeployment, deployment_id)
    if deployment is None:
        logger.error(
            "blue_green_deployment_not_recorded",
            deployment_id=deployment_id,
            fix_suggestion="Record the deployment with record_deployment right after creating it",
        )
        raise LookupError(f"no blue/green deployment {deployment_id} is recorded")
    return deployment


def update_deployment_status(
    session: Session, deployment_id: str, status: str, green_endpoint: str | None = None
) -> BlueGreenDeployment:
    """Store a status read from AWS (the caller commits).

    Sets `updated_at_utc`; stores `green_endpoint` when one is given (a call without one keeps the earlier
    value); and, the first time the status is `SWITCHOVER_COMPLETED`, sets `switched_over_at_utc`.

    Args:
        session: Control database session.
        deployment_id: Deployment identifier.
        status: Status AWS reported.
        green_endpoint: Green endpoint, when AWS reported one.

    Returns:
        The updated row.

    Raises:
        LookupError: The deployment was never recorded.

    Example:
        >>> update_deployment_status(session, "bgd-0123", "AVAILABLE", "g.example").status  # doctest: +SKIP
        'AVAILABLE'
    """
    deployment = _recorded_deployment(session, deployment_id)
    now_utc: datetime = utc_now()
    deployment.status = status
    deployment.updated_at_utc = now_utc
    if green_endpoint is not None:
        deployment.green_endpoint = green_endpoint
    if status == SWITCHOVER_COMPLETED_STATUS and deployment.switched_over_at_utc is None:
        deployment.switched_over_at_utc = now_utc
    logger.info("blue_green_deployment_status_recorded", deployment_id=deployment_id, status=status)
    return deployment


def mark_deployment_deleted(session: Session, deployment_id: str) -> BlueGreenDeployment:
    """Record that the deployment was deleted in AWS (the caller commits).

    Sets the status to `DELETING` and `deleted_at_utc`.

    Args:
        session: Control database session.
        deployment_id: Deployment identifier.

    Returns:
        The updated row.

    Raises:
        LookupError: The deployment was never recorded.

    Example:
        >>> mark_deployment_deleted(session, "bgd-0123").deleted_at_utc is not None  # doctest: +SKIP
        True
    """
    deployment = _recorded_deployment(session, deployment_id)
    now_utc = utc_now()
    deployment.status = DELETING_STATUS
    deployment.updated_at_utc = now_utc
    deployment.deleted_at_utc = now_utc
    logger.info("blue_green_deployment_marked_deleted", deployment_id=deployment_id)
    return deployment


def _arn_with_identifier(arn: str, resource_type: str, identifier: str) -> str:
    """`arn` (any RDS ARN of the same account and region) naming `resource_type` `identifier` instead.

    Example:
        >>> _arn_with_identifier("arn:aws:rds:eu-west-2:1:cluster:demo-aurora", "db", "demo-aurora-writer")
        'arn:aws:rds:eu-west-2:1:db:demo-aurora-writer'
    """
    return f"{arn.rsplit(':', 2)[0]}:{resource_type}:{identifier}"


def cluster_member_arns(context: StepContext, cluster_arn: str, step_name: str) -> list[str]:
    """The ARNs of a cluster's member instances, as `DescribeDBClusters` lists them; empty when it is gone.

    Args:
        context: The running step's context.
        cluster_arn: ARN of the cluster.
        step_name: Step the AWS call is recorded under.

    Returns:
        The members' ARNs, sorted.

    Example:
        >>> cluster_member_arns(context, aurora_arn, "record_blue_side")  # doctest: +SKIP
        ['arn:aws:rds:eu-west-2:123456789012:db:demo-aurora-reader', '...:db:demo-aurora-writer']
    """
    try:
        cluster = _recorded_rds_client(context, step_name).describe_db_clusters(
            DBClusterIdentifier=cluster_arn.rsplit(":", 1)[-1]
        )["DBClusters"][0]
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"DBClusterNotFoundFault", "DBClusterNotFound"}:
            return []
        raise
    return sorted(
        _arn_with_identifier(cluster_arn, "db", member["DBInstanceIdentifier"])
        for member in cluster.get("DBClusterMembers", [])
    )


def blue_pending_modifications(context: StepContext, resource_arn: str, step_name: str) -> list[str]:
    """The modifications AWS still has pending on the blue instance, or on the blue cluster and its members.

    A switchover of a resource with pending modifications fails in AWS (or switches a half-changed
    resource), so the switchover step refuses to start while any is listed.

    Args:
        context: The running step's context.
        resource_arn: ARN of the blue instance or cluster.
        step_name: Step the AWS calls are recorded under.

    Returns:
        One entry per resource with pending modifications, as `<identifier>: <attribute>, ...`.

    Example:
        >>> blue_pending_modifications(context, orders_arn, "switchover")  # doctest: +SKIP
        ['demo-orders: DBInstanceClass']
    """
    rds = _recorded_rds_client(context, step_name)
    identifier = resource_arn.rsplit(":", 1)[-1]
    described: list[tuple[str, dict[str, Any]]] = []
    if ":cluster:" in resource_arn:
        cluster = rds.describe_db_clusters(DBClusterIdentifier=identifier)["DBClusters"][0]
        described.append((identifier, cluster.get("PendingModifiedValues") or {}))
        for member in cluster.get("DBClusterMembers", []):
            member_identifier = member["DBInstanceIdentifier"]
            instance = rds.describe_db_instances(DBInstanceIdentifier=member_identifier)["DBInstances"][0]
            described.append((member_identifier, instance.get("PendingModifiedValues") or {}))
    else:
        instance = rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
        described.append((identifier, instance.get("PendingModifiedValues") or {}))
    return [
        f"{described_identifier}: {', '.join(sorted(pending))}"
        for described_identifier, pending in described
        if pending
    ]


def tag_blue_side(context: StepContext, resource_arn: str, step_name: str) -> None:
    """Tag a blue-side instance or cluster `rdscommander:blue-side=<operation id>` (`AddTagsToResource`).

    The inbox lists resources carrying the tag, with Decommission, and nothing else in AWS changes.

    Args:
        context: The running major upgrade's context; its id is the tag's value.
        resource_arn: ARN of the blue-side resource.
        step_name: Step the AWS call is recorded under.

    Example:
        >>> tag_blue_side(context, old_orders_arn, "record_blue_side")  # doctest: +SKIP
    """
    with _logged_call("blue_side_tagging", operation_id=str(context.operation.id), resource_arn=resource_arn):
        _recorded_rds_client(context, step_name).add_tags_to_resource(
            ResourceName=resource_arn,
            Tags=[{"Key": BLUE_SIDE_TAG_KEY, "Value": str(context.operation.id)}],
        )


def deployments_left_in_place(session: Session, target_key: str | None = None) -> list[BlueGreenDeployment]:
    """Deployments that still exist in AWS and that no running operation drives any more.

    A deployment is left in place when its major upgrade failed or was rejected before the switchover
    finished: it is neither deleted nor `SWITCHOVER_COMPLETED`, and its operation has finished. These are
    what `blue_green_cleanup` removes.

    Args:
        session: Control database session.
        target_key: Only deployments of this target; every target when None.

    Returns:
        The rows, newest first.

    Example:
        >>> [row.deployment_id for row in deployments_left_in_place(session)]  # doctest: +SKIP
        ['bgd-0123456789abcdef']
    """
    statement = (
        select(BlueGreenDeployment)
        .join(Operation, Operation.id == BlueGreenDeployment.operation_id)
        .where(
            BlueGreenDeployment.deleted_at_utc.is_(None),
            BlueGreenDeployment.status != SWITCHOVER_COMPLETED_STATUS,
            Operation.state.in_(FINISHED_OPERATION_STATES),
        )
        .order_by(BlueGreenDeployment.created_at_utc.desc())
    )
    if target_key is not None:
        statement = statement.where(BlueGreenDeployment.target_key == target_key)
    return list(session.execute(statement).scalars())
