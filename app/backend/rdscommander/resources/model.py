"""The record every page, action menu and executor reads."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ResourceKind(StrEnum):
    """What a resource is."""

    ENVIRONMENT = "environment"
    INSTANCE = "instance"
    CLUSTER = "cluster"
    PARAMETER_GROUP = "parameter_group"
    SNAPSHOT = "snapshot"


class ResourceRecord(BaseModel):
    """A resource as of the latest inventory snapshot of its target.

    Attributes:
        kind: What the resource is.
        arn: AWS ARN; the resource's identity everywhere in the console.
        resource_id: URL-safe id of the ARN (`encode_resource_id`), the value pages put in `/resources/{id}`.
        identifier: The AWS identifier (instance id, cluster id, group name, snapshot id).
        environment_slug: Environment the owning target belongs to.
        target_key: `<environment_slug>/<aws_account_id>/<aws_region>` of the owning target.
        ownership: Ownership classification recorded by the inventory refresh.
        status: AWS status string.
        facts: Scalar attributes about the resource.
        relationships: Names and identifiers of related resources.
        raw: The full AWS describe entry.
        snapshot_id: Inventory snapshot the record was read from.
        terraform_address: Terraform address when the resource is adopted, else None.
        parameter_apply_status: Parameter group apply status for instances, else None.
    """

    kind: ResourceKind
    arn: str
    resource_id: str
    identifier: str
    environment_slug: str
    target_key: str
    ownership: str
    status: str
    facts: dict[str, Any] = Field(default_factory=dict)
    relationships: dict[str, Any] = Field(default_factory=dict)
    raw: dict[str, Any] = Field(default_factory=dict)
    snapshot_id: int
    terraform_address: str | None = None
    parameter_apply_status: str | None = None


def terraform_resource_type_for(record: ResourceRecord) -> str:
    """Terraform resource type for a record.

    Args:
        record: The resource.

    Returns:
        The Terraform resource type, e.g. `aws_db_instance`.

    Raises:
        ValueError: If the record's kind has no Terraform type.

    Example:
        >>> terraform_resource_type_for(cluster_record)
        'aws_rds_cluster'
    """
    if record.kind == ResourceKind.INSTANCE:
        return (
            "aws_rds_cluster_instance"
            if record.relationships.get("cluster_identifier")
            else "aws_db_instance"
        )
    if record.kind == ResourceKind.CLUSTER:
        return "aws_rds_cluster"
    if record.kind == ResourceKind.PARAMETER_GROUP:
        if record.facts.get("group_scope") == "instance":
            return "aws_db_parameter_group"
        return "aws_rds_cluster_parameter_group"
    if record.kind == ResourceKind.SNAPSHOT:
        if record.facts.get("snapshot_scope") == "instance":
            return "aws_db_snapshot"
        return "aws_db_cluster_snapshot"
    raise ValueError(f"no Terraform type for {record.kind}")
