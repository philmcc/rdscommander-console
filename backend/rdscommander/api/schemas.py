"""Response and request models shared by routers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class OperationCreatedResponse(BaseModel):
    """Returned by every route that creates an operation."""

    operation_id: uuid.UUID
    state: str


class RefreshInventoryRequest(BaseModel):
    """Body of the refresh route."""

    idempotency_key: str = Field(min_length=1, max_length=200)


class InventorySnapshotSummary(BaseModel):
    """Snapshot header."""

    id: int
    operation_id: uuid.UUID | None
    captured_at_utc: datetime
    instance_count: int
    cluster_count: int
    parameter_group_count: int
    subnet_group_count: int


class InstanceSummary(BaseModel):
    """Instance row for the estate page."""

    id: int
    db_instance_identifier: str
    db_instance_arn: str
    engine: str
    engine_version: str
    db_instance_class: str
    db_instance_status: str
    db_cluster_identifier: str | None
    read_replica_source_identifier: str | None
    parameter_group_name: str | None
    db_subnet_group_name: str | None
    ownership: str
    tags: dict[str, str]


class ClusterSummary(BaseModel):
    """Cluster row."""

    id: int
    db_cluster_identifier: str
    db_cluster_arn: str
    engine: str
    engine_version: str
    status: str
    cluster_parameter_group_name: str | None
    db_subnet_group_name: str | None
    member_instance_identifiers: list[str]
    ownership: str
    tags: dict[str, str]


class ParameterGroupSummary(BaseModel):
    """Parameter group row."""

    id: int
    parameter_group_name: str
    parameter_group_arn: str
    parameter_group_family: str
    group_scope: str
    description: str
    is_default: bool
    user_set_parameter_count: int
    ownership: str
    tags: dict[str, str]


class SubnetGroupSummary(BaseModel):
    """Subnet group row."""

    id: int
    db_subnet_group_name: str
    db_subnet_group_arn: str
    vpc_id: str
    subnet_ids: list[str]
    description: str
    ownership: str
    tags: dict[str, str]


class TargetInventory(BaseModel):
    """Everything known about one target."""

    target_key: str
    aws_account_id: str
    aws_region: str
    aws_profile_name: str | None
    simulated: bool
    snapshot: InventorySnapshotSummary | None
    instances: list[InstanceSummary]
    clusters: list[ClusterSummary]
    parameter_groups: list[ParameterGroupSummary]
    subnet_groups: list[SubnetGroupSummary]


class EnvironmentInventory(BaseModel):
    """Inventory of every target in one environment."""

    environment_slug: str
    display_name: str
    requires_approval: bool
    targets: list[TargetInventory]


class ParameterValueSummary(BaseModel):
    """One parameter."""

    parameter_name: str
    parameter_value: str | None
    source: str
    apply_type: str
    apply_method: str | None
    data_type: str | None
    is_modifiable: bool
    description: str | None


class ParameterGroupParameters(BaseModel):
    """Parameters of one group."""

    parameter_group_id: int
    parameter_group_name: str
    parameters: list[ParameterValueSummary]


class ApiError(BaseModel):
    """Error body."""

    detail: str
    fix_suggestion: str | None = None
    extra: dict[str, Any] | None = None
