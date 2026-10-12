"""What Terraform state holds, so that "adopted" means "under Terraform" and not "carries the console's tag".

A resource is adopted when the console imported it (a `resource_adoptions` row) or when it is in the
workspace's Terraform state some other way: created by a Terraform-mode change, or adopted by a console with
another control database that shares the repository and the state bucket. The ownership tag is no evidence:
a group the console created in direct mode carries it too, and is not under Terraform.

State lives in the S3 object the backend configures (`state_bucket_name`, `state_key`). Detection and the
adoption read it and give every console-rendered address found there a `resource_adoptions` row, so that the
rest of the console (resource pages, action gates, rendering) can go by the rows alone.

Example:
    >>> in_state = read_state_addresses(s3, bucket_name, state_key)  # doctest: +SKIP
    >>> sync_adoptions_with_state(session, workspace, in_state, arns)  # doctest: +SKIP
    {'aws_db_parameter_group.demo_postgres16_tuned'}
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.control_db.models import ResourceAdoption, TerraformWorkspace, utc_now
from rdscommander.logging_setup import get_logger
from rdscommander.terraform.record import RESOURCE_SECTIONS

logger = get_logger(__name__)

ADOPTED_STATUS = "adopted"
REMOVED_STATUS = "removed"
"""Status of a row whose resource a Terraform-mode change destroyed: it has left state."""
MISSING_STATE_ERROR_CODES = frozenset({"NoSuchKey", "NoSuchBucket", "404", "NotFound"})


class TerraformStateUnreadableError(RuntimeError):
    """The state object exists (or may exist) but could not be read or parsed."""

    diagnostic_kind = "terraform_state_unreadable"
    title = "Terraform state could not be read"
    fix_suggestion = (
        "check that the AWS profile may read the state bucket (s3:GetObject, s3:ListBucket) and that the "
        "state object is valid JSON, then run the operation again"
    )


def read_state_addresses(s3_client: Any, bucket_name: str, state_key: str) -> set[str]:
    """The addresses of the console-rendered resources in the state object; empty when there is no state yet.

    Args:
        s3_client: An S3 client for the target (recorded like every AWS call).
        bucket_name: The workspace's state bucket.
        state_key: The workspace's state key.

    Returns:
        `<type>.<name>` of every managed root-module resource of a type the renderer produces.

    Raises:
        TerraformStateUnreadableError: S3 refused the read for another reason than a missing bucket or object,
            or the object is not Terraform state JSON.

    Example:
        >>> read_state_addresses(s3, bucket, key)  # doctest: +SKIP
        {'aws_db_parameter_group.demo_postgres16_tuned'}
    """
    try:
        body = s3_client.get_object(Bucket=bucket_name, Key=state_key)["Body"].read()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in MISSING_STATE_ERROR_CODES:
            return set()
        raise TerraformStateUnreadableError(f"s3://{bucket_name}/{state_key}: {error}") from error
    except BotoCoreError as error:
        raise TerraformStateUnreadableError(f"s3://{bucket_name}/{state_key}: {error}") from error
    try:
        state = json.loads(body)
        resources = state.get("resources", [])
    except (ValueError, AttributeError) as error:
        raise TerraformStateUnreadableError(
            f"s3://{bucket_name}/{state_key} is not Terraform state JSON: {error}"
        ) from error
    return {
        f"{resource['type']}.{resource['name']}"
        for resource in resources
        if isinstance(resource, dict)
        and resource.get("mode") == "managed"
        and not resource.get("module")
        and resource.get("type") in RESOURCE_SECTIONS
        and resource.get("name")
    }


def sync_adoptions_with_state(
    session: Session, workspace: TerraformWorkspace, in_state: set[str], arns_by_address: dict[str, str]
) -> set[str]:
    """Give every address in state an `adopted` row; return every adopted address of the workspace.

    Rows are only ever added or brought back to `adopted` here, never removed: a state object nobody wrote
    yet (a fake Terraform in tests, a workspace whose state lives elsewhere) says nothing about what was
    adopted. An address in state whose resource is no longer in the inventory gets no row (the row needs
    the resource's ARN) but is still returned as adopted. The caller's session is committed.

    Args:
        session: Control database session.
        workspace: The workspace row.
        in_state: Addresses read from state (`read_state_addresses`).
        arns_by_address: `{address: ARN}` of the latest inventory snapshot.

    Returns:
        The adopted addresses: the rows' and the state's.

    Example:
        >>> in_state = {"aws_db_subnet_group.private"}
        >>> sync_adoptions_with_state(session, workspace, in_state, arns)  # doctest: +SKIP
        {'aws_db_subnet_group.private', 'aws_db_parameter_group.demo'}
    """
    rows = {
        row.terraform_address: row
        for row in session.execute(
            select(ResourceAdoption).where(ResourceAdoption.workspace_id == workspace.id)
        ).scalars()
    }
    learned: list[str] = []
    for address in sorted(in_state):
        row = rows.get(address)
        if row is not None and row.adoption_status == ADOPTED_STATUS:
            continue
        arn = arns_by_address.get(address)
        if row is None:
            if arn is None:
                continue
            row = ResourceAdoption(
                workspace_id=workspace.id,
                resource_arn=arn,
                resource_identifier=arn.rsplit(":", 1)[-1],
                resource_type=address.split(".", 1)[0],
                terraform_address=address,
                adoption_status=ADOPTED_STATUS,
            )
            session.add(row)
            rows[address] = row
        row.adoption_status = ADOPTED_STATUS
        row.adopted_at_utc = row.adopted_at_utc or utc_now()
        learned.append(address)
    session.commit()
    if learned:
        logger.info(
            "adoptions_learned_from_terraform_state",
            workspace_relative_dir=workspace.workspace_relative_dir,
            terraform_addresses=learned,
        )
    adopted = {address for address, row in rows.items() if row.adoption_status == ADOPTED_STATUS}
    return adopted | in_state


ARN_RESOURCE_KINDS: dict[str, str] = {
    "aws_db_parameter_group": "pg",
    "aws_rds_cluster_parameter_group": "cluster-pg",
    "aws_db_subnet_group": "subgrp",
    "aws_rds_cluster": "cluster",
    "aws_rds_cluster_instance": "db",
    "aws_db_instance": "db",
}


def record_terraform_changes(
    session: Session,
    workspace: TerraformWorkspace,
    *,
    created: dict[str, str],
    destroyed: set[str],
    aws_region: str,
    aws_account_id: str,
    operation_id: Any,
) -> None:
    """Keep the adoption rows in step with what a committed Terraform-mode change did to state.

    A resource the change created is under Terraform from now on (an `adopted` row, so that it can be changed
    and is not offered for adoption); one it destroyed has left state (`removed`). The caller's session is
    committed.

    Args:
        session: Control database session.
        workspace: The workspace the change applied in.
        created: `{address: AWS identifier}` of the resources the applied plan created.
        destroyed: Addresses of the resources the applied plan destroyed.
        aws_region: The target's region, for the ARN of a created resource.
        aws_account_id: The target's account, for the ARN of a created resource.
        operation_id: The change operation.

    Example:
        >>> record_terraform_changes(  # doctest: +SKIP
        ...     session, workspace, created={"aws_db_parameter_group.v2": "v2"}, destroyed=set(),
        ...     aws_region="eu-west-2", aws_account_id="123456789012", operation_id=operation.id)
    """
    rows = {
        row.terraform_address: row
        for row in session.execute(
            select(ResourceAdoption).where(
                ResourceAdoption.workspace_id == workspace.id,
                ResourceAdoption.terraform_address.in_([*created, *destroyed]),
            )
        ).scalars()
    }
    for address, identifier in sorted(created.items()):
        resource_type = address.split(".", 1)[0]
        row = rows.get(address)
        if row is None:
            row = ResourceAdoption(
                workspace_id=workspace.id,
                resource_arn=(
                    f"arn:aws:rds:{aws_region}:{aws_account_id}:"
                    f"{ARN_RESOURCE_KINDS.get(resource_type, resource_type)}:{identifier}"
                ),
                resource_identifier=identifier,
                resource_type=resource_type,
                terraform_address=address,
                adoption_status=ADOPTED_STATUS,
            )
            session.add(row)
        row.adoption_status = ADOPTED_STATUS
        row.operation_id = operation_id
        row.adopted_at_utc = utc_now()
    for address in sorted(destroyed):
        row = rows.get(address)
        if row is not None:
            row.adoption_status = REMOVED_STATUS
            row.operation_id = operation_id
    session.commit()
    if created or destroyed:
        logger.info(
            "terraform_state_membership_recorded",
            workspace_relative_dir=workspace.workspace_relative_dir,
            created_addresses=sorted(created),
            destroyed_addresses=sorted(destroyed),
        )
