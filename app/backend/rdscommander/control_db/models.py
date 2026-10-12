"""SQLAlchemy models. Every table and column carries a comment that the migration emits."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Timezone-aware now; every timestamp column uses it."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base with JSON mapped to JSONB."""

    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB}


class Environment(Base):
    """Mirror of config environments so other rows can reference them."""

    __tablename__ = "environments"
    __table_args__ = {"comment": "Environments from config/environments.yaml, synced at startup"}

    environment_slug: Mapped[str] = mapped_column(String(40), primary_key=True, comment="Slug from config")
    display_name: Mapped[str] = mapped_column(String(200), comment="Human name shown in the UI")
    requires_approval: Mapped[bool] = mapped_column(Boolean, comment="Whether operations pause for approval")
    change_management: Mapped[str] = mapped_column(
        String(16), default="terraform", server_default="terraform", comment="terraform or direct"
    )
    allow_direct_changes_to_foreign_owned: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
        comment="Direct changes on foreign-owned resources allowed",
    )
    inventory_refresh_minutes: Mapped[int] = mapped_column(
        Integer, default=15, server_default="15", comment="Scheduled refresh interval"
    )
    drift_check_hours: Mapped[int] = mapped_column(
        Integer, default=24, server_default="24", comment="Scheduled drift check interval"
    )
    synced_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Last config sync"
    )


class EnvironmentTargetRecord(Base):
    """Mirror of a target: one account in one region."""

    __tablename__ = "environment_targets"
    __table_args__ = {"comment": "Targets from config, one AWS account and region each"}

    target_key: Mapped[str] = mapped_column(
        String(80), primary_key=True, comment="environment/account/region"
    )
    environment_slug: Mapped[str] = mapped_column(
        ForeignKey("environments.environment_slug"), comment="Owning environment"
    )
    aws_account_id: Mapped[str] = mapped_column(String(12), comment="12-digit AWS account id")
    aws_region: Mapped[str] = mapped_column(String(32), comment="AWS region")
    aws_profile_name: Mapped[str | None] = mapped_column(
        String(200), comment="AWS CLI profile; null when simulated"
    )
    is_simulated: Mapped[bool] = mapped_column(Boolean, comment="True when served by the local simulated AWS")


class Operation(Base):
    """A durable unit of work walked by the runner."""

    __tablename__ = "operations"
    __table_args__ = {
        "comment": "Durable operations; handlers persist intent here and the runner executes it"
    }

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, comment="Operation id"
    )
    operation_kind: Mapped[str] = mapped_column(
        String(64), index=True, comment="Executor key, e.g. inventory_refresh"
    )
    state: Mapped[str] = mapped_column(
        String(32),
        index=True,
        comment="DRAFT QUEUED RUNNING AWAITING_APPROVAL SUCCEEDED FAILED CANCELLED",
    )
    environment_slug: Mapped[str | None] = mapped_column(
        ForeignKey("environments.environment_slug"), comment="Environment scope; null for environment import"
    )
    target_key: Mapped[str | None] = mapped_column(
        String(80), comment="Target scope when the operation is per target"
    )
    resource_arn: Mapped[str | None] = mapped_column(
        String(300), index=True, comment="Resource the operation acts on, if any"
    )
    title: Mapped[str] = mapped_column(
        String(300), default="", server_default="", comment="Human title shown in lists"
    )
    approval_required_text: Mapped[str | None] = mapped_column(
        String(255), comment="Identifier the approver must type, when destructive or disruptive"
    )
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Immutable input of the operation")
    idempotency_key: Mapped[str] = mapped_column(
        String(200), unique=True, comment="Client-supplied key; repeats return the same operation"
    )
    lease_owner: Mapped[str | None] = mapped_column(String(200), comment="Runner identity holding the lease")
    lease_expires_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When the lease lapses"
    )
    created_by_operator_name: Mapped[str] = mapped_column(String(200), comment="Operator who created it")
    approved_by_operator_name: Mapped[str | None] = mapped_column(
        String(200), comment="Operator who approved it"
    )
    approved_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="Approval time")
    failure_summary: Mapped[str | None] = mapped_column(
        Text, comment="One-line reason when FAILED or CANCELLED"
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Creation time"
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, comment="Last change"
    )
    started_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="First time it ran"
    )
    finished_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Terminal state time"
    )

    step_attempts: Mapped[list[OperationStepAttempt]] = relationship(
        back_populates="operation", order_by="OperationStepAttempt.id"
    )
    diagnostic_reports: Mapped[list[DiagnosticReport]] = relationship(
        back_populates="operation", order_by="DiagnosticReport.id"
    )


class OperationStepAttempt(Base):
    """One pass over one step."""

    __tablename__ = "operation_step_attempts"
    __table_args__ = {"comment": "Every observe/execute pass over a step, never deleted"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Attempt id")
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id"), index=True, comment="Owning operation"
    )
    step_name: Mapped[str] = mapped_column(String(200), comment="Executor step name")
    attempt_number: Mapped[int] = mapped_column(Integer, comment="1-based attempt counter per step")
    observation_state: Mapped[str] = mapped_column(
        String(16), comment="ABSENT PENDING SATISFIED CONFLICT BLOCKED"
    )
    outcome_status: Mapped[str | None] = mapped_column(
        String(32),
        comment="completed awaiting_approval failed blocked; null when observation satisfied it",
    )
    detail: Mapped[str] = mapped_column(Text, default="", comment="Human-readable detail")
    output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, comment="Structured step output")
    started_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Start"
    )
    finished_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="End")

    operation: Mapped[Operation] = relationship(back_populates="step_attempts")


class ExecutionRecord(Base):
    """Complete record of one external invocation."""

    __tablename__ = "execution_records"
    __table_args__ = {"comment": "Every Terraform, AWS API, and git invocation with full input and output"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Record id")
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id"), index=True, comment="Operation, if any"
    )
    step_name: Mapped[str | None] = mapped_column(String(200), comment="Step, if any")
    record_kind: Mapped[str] = mapped_column(String(16), index=True, comment="terraform aws_api git")
    target_key: Mapped[str | None] = mapped_column(String(80), index=True, comment="Target, if any")
    command_line: Mapped[str | None] = mapped_column(Text, comment="Subprocess command line")
    working_directory: Mapped[str | None] = mapped_column(Text, comment="Subprocess working directory")
    api_name: Mapped[str | None] = mapped_column(String(200), comment="AWS service:operation")
    request_parameters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, comment="AWS request parameters")
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="AWS response (metadata only for reads)"
    )
    standard_output: Mapped[str | None] = mapped_column(Text, comment="Full stdout")
    standard_error: Mapped[str | None] = mapped_column(Text, comment="Full stderr")
    exit_code: Mapped[int | None] = mapped_column(Integer, comment="Subprocess exit code")
    succeeded: Mapped[bool] = mapped_column(Boolean, comment="Whether the invocation succeeded")
    error_message: Mapped[str | None] = mapped_column(Text, comment="Exception text when it failed")
    started_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True, comment="Start"
    )
    duration_milliseconds: Mapped[int] = mapped_column(Integer, comment="Wall time")


class DiagnosticReport(Base):
    """A problem or notable finding, with a fix suggestion."""

    __tablename__ = "diagnostic_reports"
    __table_args__ = {"comment": "Diagnostics survive retries; new ones are appended"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Report id")
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id"), index=True, comment="Operation, if any"
    )
    step_name: Mapped[str | None] = mapped_column(String(200), comment="Step, if any")
    severity: Mapped[str] = mapped_column(String(16), comment="info warning error")
    diagnostic_kind: Mapped[str] = mapped_column(
        String(64), comment="Machine-readable kind, e.g. policy_refusal"
    )
    title: Mapped[str] = mapped_column(String(300), comment="Short title")
    detail: Mapped[str] = mapped_column(Text, comment="Full detail")
    fix_suggestion: Mapped[str] = mapped_column(Text, comment="What to do about it")
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Creation time"
    )

    operation: Mapped[Operation | None] = relationship(back_populates="diagnostic_reports")


class InventorySnapshot(Base):
    """One capture of one target's RDS estate."""

    __tablename__ = "inventory_snapshots"
    __table_args__ = {"comment": "Point-in-time inventory per target"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Snapshot id")
    target_key: Mapped[str] = mapped_column(
        ForeignKey("environment_targets.target_key"), index=True, comment="Target"
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id"), comment="Refresh operation"
    )
    captured_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Capture time"
    )
    instance_count: Mapped[int] = mapped_column(Integer, comment="Instances captured")
    cluster_count: Mapped[int] = mapped_column(Integer, comment="Clusters captured")
    parameter_group_count: Mapped[int] = mapped_column(
        Integer, comment="Parameter groups captured (both scopes)"
    )
    subnet_group_count: Mapped[int] = mapped_column(Integer, comment="Subnet groups captured")


class InventoryDbInstance(Base):
    """An RDS or Aurora instance as described."""

    __tablename__ = "inventory_db_instances"
    __table_args__ = {"comment": "DB instances in a snapshot"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_snapshots.id"), index=True, comment="Snapshot"
    )
    db_instance_identifier: Mapped[str] = mapped_column(String(63), comment="DBInstanceIdentifier")
    db_instance_arn: Mapped[str] = mapped_column(String(300), comment="ARN")
    engine: Mapped[str] = mapped_column(String(32), comment="postgres or aurora-postgresql")
    engine_version: Mapped[str] = mapped_column(String(32), comment="EngineVersion")
    db_instance_class: Mapped[str] = mapped_column(String(64), comment="Instance class")
    db_instance_status: Mapped[str] = mapped_column(String(64), comment="Status string")
    db_cluster_identifier: Mapped[str | None] = mapped_column(
        String(63), comment="Aurora cluster, if a member"
    )
    read_replica_source_identifier: Mapped[str | None] = mapped_column(
        String(63), comment="Source when a replica"
    )
    parameter_group_name: Mapped[str | None] = mapped_column(String(255), comment="Instance parameter group")
    parameter_apply_status: Mapped[str | None] = mapped_column(
        String(32), comment="in-sync, pending-reboot, applying"
    )
    db_subnet_group_name: Mapped[str | None] = mapped_column(String(255), comment="Subnet group")
    ownership: Mapped[str] = mapped_column(
        String(32), comment="unmanaged foreign_terraform foreign_cloudformation adopted"
    )
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Tags as a map")
    raw_description: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Full DescribeDBInstances entry")


class InventoryDbCluster(Base):
    """An Aurora cluster as described."""

    __tablename__ = "inventory_db_clusters"
    __table_args__ = {"comment": "DB clusters in a snapshot"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_snapshots.id"), index=True, comment="Snapshot"
    )
    db_cluster_identifier: Mapped[str] = mapped_column(String(63), comment="DBClusterIdentifier")
    db_cluster_arn: Mapped[str] = mapped_column(String(300), comment="ARN")
    engine: Mapped[str] = mapped_column(String(32), comment="Engine")
    engine_version: Mapped[str] = mapped_column(String(32), comment="EngineVersion")
    status: Mapped[str] = mapped_column(String(64), comment="Status string")
    cluster_parameter_group_name: Mapped[str | None] = mapped_column(
        String(255), comment="Cluster parameter group"
    )
    db_subnet_group_name: Mapped[str | None] = mapped_column(String(255), comment="Subnet group")
    member_instance_identifiers: Mapped[list[Any]] = mapped_column(
        JSONB, comment="Member instance identifiers"
    )
    ownership: Mapped[str] = mapped_column(String(32), comment="Ownership classification")
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Tags as a map")
    raw_description: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Full DescribeDBClusters entry")


class InventoryParameterGroup(Base):
    """An instance or cluster parameter group."""

    __tablename__ = "inventory_parameter_groups"
    __table_args__ = {"comment": "Parameter groups in a snapshot, both scopes"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_snapshots.id"), index=True, comment="Snapshot"
    )
    parameter_group_name: Mapped[str] = mapped_column(String(255), comment="Name")
    parameter_group_arn: Mapped[str] = mapped_column(String(300), comment="ARN")
    parameter_group_family: Mapped[str] = mapped_column(String(64), comment="Family, e.g. postgres16")
    group_scope: Mapped[str] = mapped_column(String(16), comment="instance or cluster")
    description: Mapped[str] = mapped_column(Text, comment="Description")
    is_default: Mapped[bool] = mapped_column(
        Boolean, comment="True for AWS default groups, which cannot be adopted"
    )
    user_set_parameter_count: Mapped[int] = mapped_column(Integer, comment="Parameters with source user")
    ownership: Mapped[str] = mapped_column(String(32), comment="Ownership classification")
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Tags as a map")
    raw_description: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Full describe entry")

    parameter_values: Mapped[list[InventoryParameterValue]] = relationship(
        back_populates="parameter_group", order_by="InventoryParameterValue.parameter_name"
    )


class InventoryParameterValue(Base):
    """One parameter inside a parameter group."""

    __tablename__ = "inventory_parameter_values"
    __table_args__ = {"comment": "Every parameter of every captured parameter group"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    parameter_group_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_parameter_groups.id"), index=True, comment="Group"
    )
    parameter_name: Mapped[str] = mapped_column(String(255), comment="ParameterName")
    parameter_value: Mapped[str | None] = mapped_column(Text, comment="ParameterValue")
    source: Mapped[str] = mapped_column(String(32), comment="user, system, engine-default")
    apply_type: Mapped[str] = mapped_column(String(32), comment="static or dynamic")
    apply_method: Mapped[str | None] = mapped_column(String(32), comment="immediate or pending-reboot")
    data_type: Mapped[str | None] = mapped_column(String(32), comment="DataType")
    is_modifiable: Mapped[bool] = mapped_column(Boolean, comment="IsModifiable")
    description: Mapped[str | None] = mapped_column(Text, comment="Description")

    parameter_group: Mapped[InventoryParameterGroup] = relationship(back_populates="parameter_values")


class InventorySubnetGroup(Base):
    """A DB subnet group."""

    __tablename__ = "inventory_subnet_groups"
    __table_args__ = {"comment": "DB subnet groups in a snapshot"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_snapshots.id"), index=True, comment="Snapshot"
    )
    db_subnet_group_name: Mapped[str] = mapped_column(String(255), comment="Name")
    db_subnet_group_arn: Mapped[str] = mapped_column(String(300), comment="ARN")
    vpc_id: Mapped[str] = mapped_column(String(64), comment="VPC id")
    subnet_ids: Mapped[list[Any]] = mapped_column(JSONB, comment="Subnet ids")
    description: Mapped[str] = mapped_column(Text, comment="Description")
    ownership: Mapped[str] = mapped_column(String(32), comment="Ownership classification")
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Tags as a map")
    raw_description: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Full describe entry")


class TerraformWorkspace(Base):
    """One Terraform root module per target."""

    __tablename__ = "terraform_workspaces"
    __table_args__ = {"comment": "Terraform workspaces, one per target"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Workspace id")
    target_key: Mapped[str] = mapped_column(
        ForeignKey("environment_targets.target_key"), unique=True, comment="Target"
    )
    workspace_relative_dir: Mapped[str] = mapped_column(
        String(300), comment="Directory relative to repository root"
    )
    state_bucket_name: Mapped[str] = mapped_column(String(63), comment="S3 state bucket")
    state_key: Mapped[str] = mapped_column(String(300), comment="S3 state key")
    last_applied_commit_sha: Mapped[str | None] = mapped_column(
        String(40), comment="Repository commit of the last applied files"
    )
    last_plan_summary: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="Summary of the last plan"
    )
    last_applied_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Last apply time"
    )


class TerraformRun(Base):
    """One init, plan, apply, or verify plan."""

    __tablename__ = "terraform_runs"
    __table_args__ = {"comment": "Terraform invocations with plan JSON (sensitive values redacted)"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Run id")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("terraform_workspaces.id"), index=True, comment="Workspace"
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id"), index=True, comment="Operation"
    )
    run_kind: Mapped[str] = mapped_column(String(16), comment="init plan apply verify_plan")
    plan_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, comment="Redacted `show -json` output")
    plan_summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB, comment="Parsed summary")
    exit_code: Mapped[int] = mapped_column(Integer, comment="Binary exit code")
    execution_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_records.id"), comment="Full output record"
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Creation time"
    )


class ResourceAdoption(Base):
    """Mapping from an AWS resource to its Terraform address."""

    __tablename__ = "resource_adoptions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "terraform_address", name="uq_resource_adoptions_workspace_address"),
        {"comment": "Resources imported into Terraform state"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("terraform_workspaces.id"), index=True, comment="Workspace"
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id"), comment="Adoption operation"
    )
    resource_arn: Mapped[str] = mapped_column(String(300), index=True, comment="AWS ARN")
    resource_identifier: Mapped[str] = mapped_column(String(255), comment="AWS identifier used as import id")
    resource_type: Mapped[str] = mapped_column(String(64), comment="Terraform resource type")
    terraform_address: Mapped[str] = mapped_column(String(300), comment="Terraform address")
    adoption_status: Mapped[str] = mapped_column(String(16), comment="planned adopted failed")
    adopted_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="When adopted")


class ChangeSet(Base):
    """Direct-mode review artefact: what will change, attribute by attribute."""

    __tablename__ = "change_sets"
    __table_args__ = {"comment": "Direct-mode change sets per operation"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id"), index=True, comment="Operation"
    )
    resource_arn: Mapped[str] = mapped_column(String(300), comment="Resource")
    mode: Mapped[str] = mapped_column(String(16), comment="direct or terraform")
    entries: Mapped[list[Any]] = mapped_column(
        JSONB, comment="[{address, attribute, current, desired, apply_type, requires_reboot, downtime}]"
    )
    applied_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="When applied")
    verification: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="Live values read back after apply"
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Creation time"
    )


class InventoryDbSnapshot(Base):
    """A manual or automated snapshot of an instance or cluster."""

    __tablename__ = "inventory_db_snapshots"
    __table_args__ = {"comment": "DB and cluster snapshots in an inventory snapshot"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_snapshots.id"), index=True, comment="Inventory snapshot"
    )
    snapshot_scope: Mapped[str] = mapped_column(String(16), comment="instance or cluster")
    db_snapshot_identifier: Mapped[str] = mapped_column(String(255), comment="Identifier")
    db_snapshot_arn: Mapped[str] = mapped_column(String(300), comment="ARN")
    source_identifier: Mapped[str] = mapped_column(
        String(63), comment="Source instance or cluster identifier"
    )
    snapshot_type: Mapped[str] = mapped_column(String(32), comment="manual or automated")
    status: Mapped[str] = mapped_column(String(64), comment="Status")
    engine_version: Mapped[str] = mapped_column(String(32), comment="Engine version at snapshot time")
    allocated_storage_gb: Mapped[int | None] = mapped_column(Integer, comment="Size")
    created_at_source_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="SnapshotCreateTime"
    )
    tags: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Tags as a map")
    raw_description: Mapped[dict[str, Any]] = mapped_column(JSONB, comment="Full describe entry")


class InventoryPendingMaintenance(Base):
    """A pending maintenance action on a resource."""

    __tablename__ = "inventory_pending_maintenance"
    __table_args__ = {"comment": "Pending maintenance actions per inventory snapshot"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_snapshots.id"), index=True, comment="Inventory snapshot"
    )
    resource_arn: Mapped[str] = mapped_column(String(300), index=True, comment="Resource")
    action: Mapped[str] = mapped_column(String(64), comment="Action, e.g. system-update")
    description: Mapped[str] = mapped_column(Text, comment="Description")
    auto_applied_after_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="AutoAppliedAfterDate"
    )
    current_apply_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="CurrentApplyDate"
    )
    opt_in_status: Mapped[str | None] = mapped_column(String(32), comment="OptInStatus")


class EngineParameterFamily(Base):
    """Cached engine defaults and metadata for a parameter group family, per scope of parameter group."""

    __tablename__ = "engine_parameter_families"
    __table_args__ = (
        CheckConstraint("scope IN ('instance', 'cluster')", name="ck_engine_parameter_families_scope"),
        {"comment": "Engine default parameters cache per family and scope (instance or cluster groups)"},
    )

    family: Mapped[str] = mapped_column(String(64), primary_key=True, comment="Family, e.g. postgres16")
    scope: Mapped[str] = mapped_column(
        String(16),
        primary_key=True,
        comment=(
            "instance (DescribeEngineDefaultParameters) or cluster (DescribeEngineDefaultClusterParameters)"
        ),
    )
    fetched_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Fetch time"
    )
    parameters: Mapped[list[Any]] = mapped_column(JSONB, comment="Parameter metadata list")
    source: Mapped[str] = mapped_column(
        String(16), default="aws", server_default="aws", comment="aws or bundled"
    )


class EngineVersion(Base):
    """Cached engine version metadata."""

    __tablename__ = "engine_versions"
    __table_args__ = {"comment": "DescribeDBEngineVersions cache"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    engine: Mapped[str] = mapped_column(String(32), comment="postgres or aurora-postgresql")
    engine_version: Mapped[str] = mapped_column(String(32), comment="Version")
    parameter_group_family: Mapped[str] = mapped_column(String(64), comment="Family")
    valid_upgrade_targets: Mapped[list[Any]] = mapped_column(JSONB, comment="[{engine_version, is_major}]")
    fetched_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Fetch time"
    )


class ParameterGroupProvenance(Base):
    """How a console-built parameter group was derived."""

    __tablename__ = "parameter_group_provenance"
    __table_args__ = {"comment": "Provenance of parameter groups built by the console"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id"), comment="Operation that built it"
    )
    target_key: Mapped[str] = mapped_column(String(80), comment="Target")
    group_name: Mapped[str] = mapped_column(String(255), comment="New group")
    group_scope: Mapped[str] = mapped_column(String(16), comment="instance or cluster")
    target_family: Mapped[str] = mapped_column(String(64), comment="Family of the new group")
    source_group_names: Mapped[list[Any]] = mapped_column(JSONB, comment="Groups the overrides came from")
    classification: Mapped[list[Any]] = mapped_column(JSONB, comment="[{name, value, outcome, note}]")
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Creation time"
    )


class ResourceChangeEvent(Base):
    """One attribute change observed between two inventory snapshots."""

    __tablename__ = "resource_change_events"
    __table_args__ = {"comment": "Derived change history per resource; rebuildable from snapshots"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    resource_arn: Mapped[str] = mapped_column(String(300), index=True, comment="Resource")
    environment_slug: Mapped[str] = mapped_column(String(40), index=True, comment="Environment")
    target_key: Mapped[str] = mapped_column(String(80), comment="Target")
    snapshot_before_id: Mapped[int | None] = mapped_column(
        ForeignKey("inventory_snapshots.id"), comment="Earlier snapshot"
    )
    snapshot_after_id: Mapped[int] = mapped_column(
        ForeignKey("inventory_snapshots.id"), comment="Later snapshot"
    )
    attribute_path: Mapped[str] = mapped_column(
        String(300), comment="Attribute, e.g. db_instance_class or parameter.work_mem"
    )
    value_before: Mapped[Any | None] = mapped_column(JSONB, comment="Earlier value")
    value_after: Mapped[Any | None] = mapped_column(JSONB, comment="Later value")
    explanation: Mapped[str] = mapped_column(String(32), comment="operation or outside_console")
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id"), comment="Explaining operation"
    )
    detected_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Detection time"
    )
    reviewed_by_operator_name: Mapped[str | None] = mapped_column(
        String(200), comment="Who reviewed an outside change"
    )
    reviewed_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="Review time")


class DriftFinding(Base):
    """Differences between the record and reality for one resource."""

    __tablename__ = "drift_findings"
    __table_args__ = {"comment": "Open and resolved drift findings"}

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment="Row id")
    resource_arn: Mapped[str] = mapped_column(String(300), index=True, comment="Resource")
    environment_slug: Mapped[str] = mapped_column(String(40), index=True, comment="Environment")
    target_key: Mapped[str] = mapped_column(String(80), comment="Target")
    finding_kind: Mapped[str] = mapped_column(String(16), comment="record or state")
    attributes: Mapped[list[Any]] = mapped_column(JSONB, comment="[{attribute, recorded, live}]")
    detected_by_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id"), comment="Detecting operation"
    )
    detected_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="Detection time"
    )
    resolved_by_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operations.id"), comment="Resolving operation"
    )
    resolved_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="Resolution time"
    )


class BlueGreenDeployment(Base):
    """An RDS blue/green deployment a major upgrade created, followed until it is switched over or deleted."""

    __tablename__ = "blue_green_deployments"
    __table_args__ = {
        "comment": "Blue/green deployments of major upgrades, from creation to switchover or deletion"
    }

    deployment_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        comment="AWS blue/green deployment identifier, e.g. bgd-0123456789abcdef",
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("operations.id"), index=True, comment="Operation that created the deployment"
    )
    resource_arn: Mapped[str] = mapped_column(
        String(300), index=True, comment="ARN of the blue (source) instance or cluster"
    )
    environment_slug: Mapped[str] = mapped_column(String(40), comment="Environment of the source resource")
    target_key: Mapped[str] = mapped_column(String(80), comment="Target (account and region) it lives in")
    status: Mapped[str] = mapped_column(
        String(40),
        comment=(
            "Last status read from AWS: PROVISIONING AVAILABLE SWITCHOVER_IN_PROGRESS "
            "SWITCHOVER_COMPLETED SWITCHOVER_FAILED INVALID_CONFIGURATION DELETING"
        ),
    )
    green_endpoint: Mapped[str | None] = mapped_column(
        String(300), comment="Endpoint address of the green side, once AWS reports one"
    )
    target_engine_version: Mapped[str] = mapped_column(
        String(32), comment="Engine version the green side runs"
    )
    created_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="When the console recorded the deployment"
    )
    updated_at_utc: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, comment="When the status was last updated"
    )
    switched_over_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When the switchover was observed complete"
    )
    deleted_at_utc: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="When the deployment was deleted in AWS"
    )
