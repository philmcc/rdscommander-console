"""Environments and inventory routes."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rdscommander.api.attention_routes import AttentionInbox, read_attention
from rdscommander.api.catalogue_routes import OperationCreateRequest, create_operation_response
from rdscommander.api.history_routes import ChangeEventSummary, summarize_change_event
from rdscommander.api.operation_routes import OperationSummary
from rdscommander.api.schemas import (
    ClusterSummary,
    EnvironmentInventory,
    InstanceSummary,
    InventorySnapshotSummary,
    OperationCreatedResponse,
    ParameterGroupParameters,
    ParameterGroupSummary,
    ParameterValueSummary,
    RefreshInventoryRequest,
    SubnetGroupSummary,
    TargetInventory,
)
from rdscommander.catalogue.catalogue import actions_for_environment
from rdscommander.catalogue.definitions import ActionAvailability
from rdscommander.config.aws_profiles import list_aws_profiles
from rdscommander.config.models import ConsoleConfig, EnvironmentConfig, ResolvedTarget
from rdscommander.control_db.models import (
    DriftFinding,
    InventoryDbCluster,
    InventoryDbInstance,
    InventoryParameterGroup,
    InventoryParameterValue,
    InventorySubnetGroup,
    Operation,
    ResourceChangeEvent,
    TerraformWorkspace,
)
from rdscommander.environments.compare import EnvironmentComparison, compare_environments
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.operations.enums import OperationState
from rdscommander.operations.service import OperationService
from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import adopted_addresses_of_target, list_resources, ownership_in_mode
from rdscommander.terraform.names import terraform_address_for

router = APIRouter(prefix="/api/v1", tags=["environments"])

RECENT_OPERATIONS_ON_ENVIRONMENT_PAGE = 20
CHANGE_EVENTS_ON_ENVIRONMENT_PAGE = 50
ACTIVE_OPERATION_STATES = (
    OperationState.QUEUED.value,
    OperationState.RUNNING.value,
    OperationState.AWAITING_APPROVAL.value,
)


class EnvironmentOverview(BaseModel):
    """One environment as listed on the Environments page and summarised on its own page.

    Attributes:
        slug: Short name of the environment.
        display_name: Name shown in the interface.
        is_production: Whether the environment is production.
        change_management: `terraform` or `direct`.
        allow_direct_changes_to_foreign_owned: Whether resources owned by other tooling may be changed.
        inventory_refresh_minutes: How often the inventory refreshes by itself.
        drift_check_hours: How often drift is checked by itself.
        counts: Resources by kind (`instance`, `cluster`, `parameter_group`, `snapshot`).
        ownership_counts: Instances, clusters and parameter groups by ownership.
        engine_versions: Instances and clusters by engine version.
        last_refresh_at_utc: When the newest inventory snapshot of any target was taken; null if never.
        running_operation_count: Operations of the environment that are queued, running or awaiting approval.
        attention_count: Items the needs-attention inbox holds for the environment.
    """

    slug: str
    display_name: str
    is_production: bool
    change_management: str
    allow_direct_changes_to_foreign_owned: bool
    inventory_refresh_minutes: int
    drift_check_hours: int
    counts: dict[str, int]
    ownership_counts: dict[str, int]
    engine_versions: dict[str, int]
    last_refresh_at_utc: datetime | None
    running_operation_count: int
    attention_count: int


class EnvironmentList(BaseModel):
    """Every configured environment, in configuration order."""

    environments: list[EnvironmentOverview]


class EnvironmentTargetView(BaseModel):
    """One AWS account and region of an environment.

    Attributes:
        target_key: `<environment_slug>/<aws_account_id>/<aws_region>`.
        aws_account_id: The 12-digit account id.
        aws_region: The region.
        aws_profile_name: Profile used for the target; null for the simulated account.
        simulated: Whether the target is the simulated account.
        last_snapshot_at_utc: When the target was last inventoried; null if never.
        workspace_relative_dir: Terraform workspace directory, relative to the repository root.
        last_applied_commit_sha: Repository commit of the last applied Terraform files; null if none.
    """

    target_key: str
    aws_account_id: str
    aws_region: str
    aws_profile_name: str | None
    simulated: bool
    last_snapshot_at_utc: datetime | None
    workspace_relative_dir: str
    last_applied_commit_sha: str | None


class EnvironmentPage(BaseModel):
    """Everything the environment page needs.

    Attributes:
        overview: The environment's summary.
        targets: The environment's accounts and regions.
        actions: Every environment-level catalogue operation with its applicability.
        recent_operations: The latest operations of the environment, newest first.
        change_events: The latest recorded changes in the environment, newest first.
        drift: Open drift findings, each `{resource_arn, resource_id, finding_kind, attributes,
            detected_at_utc}`; `resource_id` is null for a finding keyed by a Terraform address.
        first_steps: What has been done so far: `imported`, `refreshed`, `adopted_or_direct`, `mode_set`.
    """

    overview: EnvironmentOverview
    targets: list[EnvironmentTargetView]
    actions: list[ActionAvailability]
    recent_operations: list[OperationSummary]
    change_events: list[ChangeEventSummary]
    drift: list[dict[str, Any]]
    first_steps: dict[str, bool]


class ProfileList(BaseModel):
    """AWS profiles available on this machine."""

    profiles: list[str]


class ImportEnvironmentRequest(BaseModel):
    """Body of `POST /environments/import`.

    Attributes:
        idempotency_key: Client-chosen key; repeating it returns the first operation created with it.
        options: The `environment_import` options (see the catalogue for the schema).
    """

    idempotency_key: str = Field(min_length=1, max_length=200)
    options: dict[str, Any] = Field(default_factory=dict)


def _environment_or_404(request: Request, slug: str) -> EnvironmentConfig:
    try:
        return request.app.state.config_store.current.environment(slug)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def _engine_version_of(record: ResourceRecord) -> str | None:
    """The engine version of an instance or cluster record; None for other kinds."""
    if record.kind in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        return record.facts.get("engine_version") or None
    return None


def _build_overview(
    request: Request,
    session: Session,
    console_config: ConsoleConfig,
    environment: EnvironmentConfig,
    records: list[ResourceRecord],
) -> EnvironmentOverview:
    """Summarise one environment from its resource records, snapshots, operations and inbox."""
    counts = {kind.value: 0 for kind in (ResourceKind.INSTANCE, ResourceKind.CLUSTER)}
    counts.update({ResourceKind.PARAMETER_GROUP.value: 0, ResourceKind.SNAPSHOT.value: 0})
    counts.update(Counter(record.kind.value for record in records))
    ownership_counts = Counter(record.ownership for record in records if record.kind != ResourceKind.SNAPSHOT)
    engine_versions = Counter(
        version for version in (_engine_version_of(record) for record in records) if version
    )
    snapshot_times = [
        snapshot.captured_at_utc
        for snapshot in (
            latest_snapshot_for_target(session, target.target_key)
            for target in console_config.targets_for_environment(environment.slug)
        )
        if snapshot is not None
    ]
    running_operation_count = session.execute(
        select(func.count(Operation.id)).where(
            Operation.environment_slug == environment.slug, Operation.state.in_(ACTIVE_OPERATION_STATES)
        )
    ).scalar_one()
    inbox = read_attention(request, environment.slug)
    return EnvironmentOverview(
        slug=environment.slug,
        display_name=environment.display_name,
        is_production=environment.is_production,
        change_management=environment.change_management,
        allow_direct_changes_to_foreign_owned=environment.allow_direct_changes_to_foreign_owned,
        inventory_refresh_minutes=environment.inventory_refresh_minutes,
        drift_check_hours=environment.drift_check_hours,
        counts=dict(counts),
        ownership_counts=dict(ownership_counts),
        engine_versions=dict(engine_versions),
        last_refresh_at_utc=max(snapshot_times) if snapshot_times else None,
        running_operation_count=running_operation_count,
        attention_count=sum(len(getattr(inbox, section)) for section in AttentionInbox.model_fields),
    )


@router.get("/environments/compare", response_model=EnvironmentComparison)
def compare_two_environments(
    request: Request, left: str, right: str, strip: str = ""
) -> EnvironmentComparison:
    """Match the resources of two environments and list the attributes that differ.

    Args:
        request: The request; configuration and sessions come from `app.state`.
        left: Slug of the left environment.
        right: Slug of the right environment.
        strip: Comma-separated suffixes or prefixes that differ between the environments' naming,
            for example `-prod,-prd`; empty to match by exact identifier only.

    Returns:
        Matched pairs with their differences, and the resources that have no counterpart.

    Raises:
        HTTPException: 404 when either slug names no environment.

    Example:
        >>> client.get(  # doctest: +SKIP
        ...     "/api/v1/environments/compare",
        ...     params={"left": "staging", "right": "production", "strip": "-prod"},
        ... ).json()["pairs"][0]["left_identifier"]
        'orders'
    """
    _environment_or_404(request, left)
    _environment_or_404(request, right)
    strip_suffixes = [affix.strip() for affix in strip.split(",") if affix.strip()]
    with request.app.state.session_factory() as session:
        return compare_environments(
            session, request.app.state.config_store.current, left, right, strip_suffixes
        )


@router.get("/environments/import/profiles", response_model=ProfileList)
def list_importable_profiles(request: Request) -> ProfileList:
    """The AWS profiles on this machine, from the mounted AWS CLI config file."""
    return ProfileList(profiles=list_aws_profiles(request.app.state.settings.aws_config_path))


@router.post("/environments/import", response_model=OperationCreatedResponse, status_code=202)
def import_environment(
    request: Request, body: ImportEnvironmentRequest
) -> OperationCreatedResponse | JSONResponse:
    """Create an `environment_import` operation through the operation catalogue.

    The operation confirms the account, scans the regions, adds the environment to
    `config/environments.yaml`, commits that file, reloads the configuration and queues an inventory
    refresh. Invalid options are answered 422 with `{detail, problems}`.
    """
    return create_operation_response(
        request.app.state,
        OperationCreateRequest(
            kind="environment_import", options=body.options, idempotency_key=body.idempotency_key
        ),
    )


@router.get("/environments", response_model=EnvironmentList)
def list_environments(request: Request) -> EnvironmentList:
    """Every environment with its resource counts, last refresh, running operations and attention items."""
    console_config = request.app.state.config_store.current
    with request.app.state.session_factory() as session:
        return EnvironmentList(
            environments=[
                _build_overview(
                    request,
                    session,
                    console_config,
                    environment,
                    list_resources(session, console_config, environment.slug),
                )
                for environment in console_config.environments
            ]
        )


@router.get("/environments/{slug}", response_model=EnvironmentPage)
def read_environment_page(request: Request, slug: str) -> EnvironmentPage:
    """One environment: overview, targets, actions, recent operations, change events, drift, first steps."""
    environment = _environment_or_404(request, slug)
    console_config = request.app.state.config_store.current
    with request.app.state.session_factory() as session:
        records = list_resources(session, console_config, slug)
        overview = _build_overview(request, session, console_config, environment, records)
        targets: list[EnvironmentTargetView] = []
        for target in console_config.targets_for_environment(slug):
            snapshot = latest_snapshot_for_target(session, target.target_key)
            workspace = session.execute(
                select(TerraformWorkspace).where(TerraformWorkspace.target_key == target.target_key)
            ).scalar_one_or_none()
            targets.append(
                EnvironmentTargetView(
                    target_key=target.target_key,
                    aws_account_id=target.aws_account_id,
                    aws_region=target.aws_region,
                    aws_profile_name=target.aws_profile_name,
                    simulated=target.simulated,
                    last_snapshot_at_utc=snapshot.captured_at_utc if snapshot else None,
                    workspace_relative_dir=target.workspace_relative_dir.as_posix(),
                    last_applied_commit_sha=workspace.last_applied_commit_sha if workspace else None,
                )
            )
        change_events = session.execute(
            select(ResourceChangeEvent)
            .where(ResourceChangeEvent.environment_slug == slug)
            .order_by(ResourceChangeEvent.id.desc())
            .limit(CHANGE_EVENTS_ON_ENVIRONMENT_PAGE)
        ).scalars()
        drift_findings = session.execute(
            select(DriftFinding)
            .where(DriftFinding.environment_slug == slug, DriftFinding.resolved_at_utc.is_(None))
            .order_by(DriftFinding.detected_at_utc.desc())
        ).scalars()
        return EnvironmentPage(
            overview=overview,
            targets=targets,
            actions=actions_for_environment(environment),
            recent_operations=[
                OperationSummary.model_validate(operation, from_attributes=True)
                for operation in OperationService(request.app.state.session_factory).list(
                    limit=RECENT_OPERATIONS_ON_ENVIRONMENT_PAGE, environment_slug=slug
                )
            ],
            change_events=[summarize_change_event(event) for event in change_events],
            drift=[
                {
                    "resource_arn": finding.resource_arn,
                    "resource_id": (
                        encode_resource_id(finding.resource_arn)
                        if finding.resource_arn.startswith("arn:")
                        else None
                    ),
                    "finding_kind": finding.finding_kind,
                    "attributes": finding.attributes,
                    "detected_at_utc": finding.detected_at_utc.isoformat(),
                }
                for finding in drift_findings
            ],
            first_steps={
                "imported": True,
                "refreshed": overview.last_refresh_at_utc is not None,
                "adopted_or_direct": environment.change_management == "direct"
                or any(record.ownership == "adopted" or record.terraform_address for record in records),
                "mode_set": True,
            },
        )


def _subnet_group_summary(
    row: InventorySubnetGroup, adopted_addresses: set[str], target: ResolvedTarget
) -> SubnetGroupSummary:
    """A subnet group with the ownership resource pages show: in Terraform mode, adopted only under Terraform.

    The adoption picker lists the unmanaged subnet groups from here, and the adoption prerequisite check
    reads the same adopted addresses, so a group carrying the console's tags without being adopted is both
    offered and demanded.
    """
    summary = SubnetGroupSummary.model_validate(row, from_attributes=True)
    address = terraform_address_for("aws_db_subnet_group", row.db_subnet_group_name)
    return summary.model_copy(
        update={"ownership": ownership_in_mode(row.ownership, address, adopted_addresses, target)}
    )


@router.get("/environments/{slug}/inventory", response_model=EnvironmentInventory)
def read_environment_inventory(request: Request, slug: str) -> EnvironmentInventory:
    """Latest snapshot per target of `slug`."""
    environment = _environment_or_404(request, slug)
    console_config = request.app.state.config_store.current
    targets: list[TargetInventory] = []
    with request.app.state.session_factory() as session:
        for target in console_config.targets_for_environment(slug):
            snapshot = latest_snapshot_for_target(session, target.target_key)
            base = {
                "target_key": target.target_key,
                "aws_account_id": target.aws_account_id,
                "aws_region": target.aws_region,
                "aws_profile_name": target.aws_profile_name,
                "simulated": target.simulated,
            }
            if snapshot is None:
                targets.append(
                    TargetInventory(
                        **base,
                        snapshot=None,
                        instances=[],
                        clusters=[],
                        parameter_groups=[],
                        subnet_groups=[],
                    )
                )
                continue
            instances = session.execute(
                select(InventoryDbInstance)
                .where(InventoryDbInstance.snapshot_id == snapshot.id)
                .order_by(InventoryDbInstance.db_instance_identifier)
            ).scalars()
            clusters = session.execute(
                select(InventoryDbCluster)
                .where(InventoryDbCluster.snapshot_id == snapshot.id)
                .order_by(InventoryDbCluster.db_cluster_identifier)
            ).scalars()
            groups = session.execute(
                select(InventoryParameterGroup)
                .where(InventoryParameterGroup.snapshot_id == snapshot.id)
                .order_by(InventoryParameterGroup.group_scope, InventoryParameterGroup.parameter_group_name)
            ).scalars()
            adopted_addresses = adopted_addresses_of_target(session, target.target_key)
            subnet_groups = session.execute(
                select(InventorySubnetGroup)
                .where(InventorySubnetGroup.snapshot_id == snapshot.id)
                .order_by(InventorySubnetGroup.db_subnet_group_name)
            ).scalars()
            targets.append(
                TargetInventory(
                    **base,
                    snapshot=InventorySnapshotSummary.model_validate(snapshot, from_attributes=True),
                    instances=[InstanceSummary.model_validate(i, from_attributes=True) for i in instances],
                    clusters=[ClusterSummary.model_validate(c, from_attributes=True) for c in clusters],
                    parameter_groups=[
                        ParameterGroupSummary.model_validate(g, from_attributes=True) for g in groups
                    ],
                    subnet_groups=[
                        _subnet_group_summary(s, adopted_addresses, target) for s in subnet_groups
                    ],
                )
            )
    return EnvironmentInventory(
        environment_slug=slug,
        display_name=environment.display_name,
        requires_approval=environment.requires_approval,
        targets=targets,
    )


@router.post(
    "/environments/{slug}/inventory/refresh", response_model=OperationCreatedResponse, status_code=202
)
def refresh_environment_inventory(
    request: Request, slug: str, body: RefreshInventoryRequest
) -> OperationCreatedResponse | JSONResponse:
    """Create an `inventory_refresh` operation for `slug` through the operation catalogue."""
    return create_operation_response(
        request.app.state,
        OperationCreateRequest(
            kind="inventory_refresh",
            environment_slug=slug,
            options={},
            idempotency_key=body.idempotency_key,
        ),
    )


@router.get(
    "/inventory/parameter-groups/{parameter_group_id}/parameters", response_model=ParameterGroupParameters
)
def read_parameter_group_parameters(request: Request, parameter_group_id: int) -> ParameterGroupParameters:
    """Every parameter of one captured group."""
    with request.app.state.session_factory() as session:
        group = session.get(InventoryParameterGroup, parameter_group_id)
        if group is None:
            raise HTTPException(status_code=404, detail=f"parameter group {parameter_group_id} not found")
        values = session.execute(
            select(InventoryParameterValue)
            .where(InventoryParameterValue.parameter_group_id == group.id)
            .order_by(InventoryParameterValue.parameter_name)
        ).scalars()
        return ParameterGroupParameters(
            parameter_group_id=group.id,
            parameter_group_name=group.parameter_group_name,
            parameters=[ParameterValueSummary.model_validate(v, from_attributes=True) for v in values],
        )
