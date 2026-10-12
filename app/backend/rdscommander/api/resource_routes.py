"""Resource pages and lists."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from rdscommander.api.history_routes import ChangeEventSummary, summarize_change_event
from rdscommander.api.operation_routes import OperationSummary
from rdscommander.catalogue.catalogue import actions_for_resource, definition_for
from rdscommander.catalogue.definitions import ActionAvailability
from rdscommander.history.change_events import recent_change_events_for
from rdscommander.resources.identity import decode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import (
    list_resources,
    load_resource,
    open_drift_findings_for,
    pending_maintenance_for,
    provenance_of_parameter_group,
    recent_operations_for,
)
from rdscommander.terraform.record import DriftAttribute

router = APIRouter(prefix="/api/v1", tags=["resources"])

CHANGE_EVENTS_ON_RESOURCE_PAGE = 50


class DriftFindingView(BaseModel):
    """An open drift finding for a resource.

    Attributes:
        finding_kind: `record` when AWS differs from the committed record, `state` when the Terraform state
            differs from the files.
        attributes: What differs.
        detected_at_utc: When the console first noticed the difference.

    Example:
        >>> DriftFindingView(
        ...     finding_kind="record",
        ...     attributes=[
        ...         DriftAttribute(
        ...             address="aws_db_instance.demo_orders",
        ...             attribute="instance_class",
        ...             recorded="db.m6g.large",
        ...             live="db.r6g.large",
        ...         )
        ...     ],
        ...     detected_at_utc=datetime(2026, 10, 10, 10, 0),
        ... ).finding_kind
        'record'
    """

    finding_kind: str
    attributes: list[DriftAttribute]
    detected_at_utc: datetime


class PendingMaintenanceView(BaseModel):
    """A maintenance action AWS has queued for an instance or cluster.

    Attributes:
        action: The action, e.g. `system-update` or `db-upgrade`.
        description: AWS's description of the action.
        auto_applied_after_utc: AWS applies the action on its own after this time; None when it does not say.
        current_apply_utc: When the action is scheduled to be applied now; None when it is not scheduled.
        opt_in_status: `immediate`, `next-maintenance` or `undo-opt-in` once the action has been opted into;
            None when no opt-in is recorded.

    Example:
        >>> PendingMaintenanceView(action="system-update", description="New OS patch").opt_in_status is None
        True
    """

    action: str
    description: str
    auto_applied_after_utc: datetime | None = None
    current_apply_utc: datetime | None = None
    opt_in_status: str | None = None


class ResourcePage(BaseModel):
    """Everything the resource page needs.

    Attributes:
        record: The resource as of the latest inventory snapshot of its target.
        actions: Every catalogue operation that applies to the resource, with applicability.
        recent_operations: Most recent operations that acted on the resource, newest first.
        change_events: The latest recorded changes to the resource, newest first.
        drift: Open drift findings for the resource; `finding_kind` is `record` or `state` and `attributes`
            lists what differs.
        pending_maintenance: Maintenance AWS has queued for the resource as of the inventory snapshot of
            `record`, ordered by action; empty for resources AWS does not schedule maintenance for.
    """

    record: ResourceRecord
    actions: list[ActionAvailability]
    recent_operations: list[OperationSummary]
    change_events: list[ChangeEventSummary] = Field(default_factory=list)
    drift: list[DriftFindingView] = Field(default_factory=list)
    pending_maintenance: list[PendingMaintenanceView] = Field(default_factory=list)


class ResourceList(BaseModel):
    """List response."""

    resources: list[ResourceRecord]


@router.get("/resources/{resource_id}", response_model=ResourcePage)
def read_resource(request: Request, resource_id: str) -> ResourcePage:
    """Record, applicable actions, recent operations, change events and open drift findings.

    A parameter group the console built for an upgrade carries `record.facts["provenance"]`: the groups its
    overrides came from, the family it was built for and what became of each override.
    """
    try:
        arn = decode_resource_id(resource_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    console_config = request.app.state.config_store.current
    with request.app.state.session_factory() as session:
        record = load_resource(session, console_config, arn)
        if record is None:
            raise HTTPException(status_code=404, detail=f"resource {arn} is not in the latest inventory")
        operations = recent_operations_for(session, arn)
        change_events = recent_change_events_for(session, arn, limit=CHANGE_EVENTS_ON_RESOURCE_PAGE)
        drift_findings = open_drift_findings_for(session, arn)
        pending_maintenance = pending_maintenance_for(session, arn, record.snapshot_id)
        if record.kind == ResourceKind.PARAMETER_GROUP:
            provenance = provenance_of_parameter_group(session, record)
            if provenance is not None:
                record.facts["provenance"] = provenance
        return ResourcePage(
            record=record,
            actions=actions_for_resource(record, console_config.environment(record.environment_slug)),
            recent_operations=[OperationSummary.model_validate(o, from_attributes=True) for o in operations],
            change_events=[summarize_change_event(event) for event in change_events],
            drift=[
                DriftFindingView(
                    finding_kind=finding.finding_kind,
                    attributes=[DriftAttribute.model_validate(a) for a in finding.attributes],
                    detected_at_utc=finding.detected_at_utc,
                )
                for finding in drift_findings
            ],
            pending_maintenance=[
                PendingMaintenanceView.model_validate(row, from_attributes=True)
                for row in pending_maintenance
            ],
        )


@router.get("/environments/{slug}/resources", response_model=ResourceList)
def list_environment_resources(
    request: Request, slug: str, kind: ResourceKind | None = None, applicable_to: str | None = None
) -> ResourceList:
    """Resources of an environment, optionally one kind and optionally only those a task applies to.

    Args:
        request: The request; configuration and sessions come from `app.state`.
        slug: Environment to list.
        kind: Restrict to one resource kind.
        applicable_to: A catalogue operation kind, e.g. `terraform_adoption`. Only resources for which the
            catalogue reports that operation as applicable are returned, so a launcher can offer exactly
            the resources the task will accept: an adoption never lists a default or already adopted
            group, a drift resolution only what has an open finding.

    Returns:
        The matching records, in inventory order.

    Raises:
        HTTPException: 404 when `slug` names no environment or `applicable_to` names no operation.

    Example:
        >>> client.get(
        ...     "/api/v1/environments/sandbox/resources",
        ...     params={"kind": "parameter_group", "applicable_to": "drift_resolve"},
        ... ).json()["resources"]
        []
    """
    console_config = request.app.state.config_store.current
    try:
        environment = console_config.environment(slug)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if applicable_to is not None:
        try:
            definition_for(applicable_to)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
    with request.app.state.session_factory() as session:
        records = list_resources(session, console_config, slug, kind)
    if applicable_to is not None:
        records = [
            record
            for record in records
            if any(
                action.kind == applicable_to and action.applicable
                for action in actions_for_resource(record, environment)
            )
        ]
    return ResourceList(resources=records)
