"""Search and export execution records."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import Select, or_, select

from rdscommander.api.operation_routes import ExecutionRecordSummary
from rdscommander.control_db.models import ExecutionRecord, Operation, ResourceChangeEvent
from rdscommander.history.change_events import EXPLANATION_OUTSIDE_CONSOLE, review_change_event
from rdscommander.resources.identity import decode_resource_id, encode_resource_id

router = APIRouter(prefix="/api/v1/history", tags=["history"])


class HistoryPage(BaseModel):
    """Search response."""

    records: list[ExecutionRecordSummary]


def _query(
    environment_slug: str | None,
    record_kind: str | None,
    target_key: str | None,
    text: str | None,
    since: datetime | None,
    limit: int,
) -> Select:
    statement = select(ExecutionRecord).order_by(ExecutionRecord.id.desc()).limit(limit)
    if environment_slug:
        statement = statement.join(Operation, Operation.id == ExecutionRecord.operation_id).where(
            Operation.environment_slug == environment_slug
        )
    if record_kind:
        statement = statement.where(ExecutionRecord.record_kind == record_kind)
    if target_key:
        statement = statement.where(ExecutionRecord.target_key == target_key)
    if since:
        statement = statement.where(ExecutionRecord.started_at_utc >= since)
    if text:
        pattern = f"%{text}%"
        statement = statement.where(
            or_(
                ExecutionRecord.command_line.ilike(pattern),
                ExecutionRecord.api_name.ilike(pattern),
                ExecutionRecord.standard_output.ilike(pattern),
                ExecutionRecord.standard_error.ilike(pattern),
                ExecutionRecord.error_message.ilike(pattern),
            )
        )
    return statement


@router.get("", response_model=HistoryPage)
def search_history(
    request: Request,
    environment_slug: str | None = None,
    record_kind: str | None = None,
    target_key: str | None = None,
    text: str | None = None,
    since: datetime | None = None,
    limit: int = Query(100, ge=1, le=1000),
) -> HistoryPage:
    """Filter execution records."""
    with request.app.state.session_factory() as session:
        rows = session.execute(
            _query(environment_slug, record_kind, target_key, text, since, limit)
        ).scalars()
        return HistoryPage(
            records=[ExecutionRecordSummary.model_validate(r, from_attributes=True) for r in rows]
        )


@router.get("/export", response_model=list[ExecutionRecordSummary])
def export_history(
    request: Request,
    environment_slug: str | None = None,
    record_kind: str | None = None,
    target_key: str | None = None,
    since: datetime | None = None,
    limit: int = Query(10000, ge=1, le=100000),
) -> list[ExecutionRecordSummary]:
    """Same filters, larger page, plain JSON array for download."""
    with request.app.state.session_factory() as session:
        rows = session.execute(
            _query(environment_slug, record_kind, target_key, None, since, limit)
        ).scalars()
        return [ExecutionRecordSummary.model_validate(r, from_attributes=True) for r in rows]


class ChangeEventSummary(BaseModel):
    """One change event: an attribute of a resource that differs between two inventory snapshots.

    Attributes:
        id: Event id; newer events have larger ids.
        resource_arn: ARN of the resource that changed.
        resource_id: URL-safe id of the resource, as used by the resource routes.
        environment_slug: Environment of the resource.
        target_key: Target of the resource.
        attribute_path: Attribute that changed, e.g. `db_instance_class` or `parameter.work_mem`;
            `created` or `removed` when the resource itself appeared or vanished.
        value_before: Value in the earlier snapshot, null when absent.
        value_after: Value in the later snapshot, null when absent.
        explanation: `operation` when a console operation accounts for the change, else `outside_console`.
        operation_id: The explaining operation, when there is one.
        detected_at_utc: When the console noticed the change.
        reviewed_by_operator_name: Operator who reviewed the change, if anyone has.
        reviewed_at_utc: When it was reviewed.
    """

    id: int
    resource_arn: str
    resource_id: str
    environment_slug: str
    target_key: str
    attribute_path: str
    value_before: Any
    value_after: Any
    explanation: str
    operation_id: uuid.UUID | None
    detected_at_utc: datetime
    reviewed_by_operator_name: str | None
    reviewed_at_utc: datetime | None


class ChangeEventPage(BaseModel):
    """Changes view."""

    events: list[ChangeEventSummary]


def summarize_change_event(event: ResourceChangeEvent) -> ChangeEventSummary:
    """The API representation of a stored change event.

    Args:
        event: A change event row.

    Returns:
        The summary, with the resource's URL-safe id added.

    Example:
        >>> summarize_change_event(event).resource_id
        'YXJuOmF3czpyZHM6ZXUtd2VzdC0yOjAwMDAwMDAwMDAwMDpkYjpkZW1vLW9yZGVycw'
    """
    return ChangeEventSummary(
        id=event.id,
        resource_arn=event.resource_arn,
        resource_id=encode_resource_id(event.resource_arn),
        environment_slug=event.environment_slug,
        target_key=event.target_key,
        attribute_path=event.attribute_path,
        value_before=event.value_before,
        value_after=event.value_after,
        explanation=event.explanation,
        operation_id=event.operation_id,
        detected_at_utc=event.detected_at_utc,
        reviewed_by_operator_name=event.reviewed_by_operator_name,
        reviewed_at_utc=event.reviewed_at_utc,
    )


@router.get("/changes", response_model=ChangeEventPage)
def list_change_events(
    request: Request,
    environment_slug: str | None = None,
    resource_id: str | None = None,
    explanation: str | None = None,
    since: datetime | None = None,
    unreviewed_only: bool = False,
    limit: int = Query(200, ge=1, le=2000),
) -> ChangeEventPage:
    """Filter change events, newest first.

    `unreviewed_only` keeps outside-console changes nobody has reviewed yet.
    """
    statement = select(ResourceChangeEvent).order_by(ResourceChangeEvent.id.desc()).limit(limit)
    if environment_slug:
        statement = statement.where(ResourceChangeEvent.environment_slug == environment_slug)
    if resource_id:
        try:
            resource_arn = decode_resource_id(resource_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        statement = statement.where(ResourceChangeEvent.resource_arn == resource_arn)
    if explanation:
        statement = statement.where(ResourceChangeEvent.explanation == explanation)
    if since:
        statement = statement.where(ResourceChangeEvent.detected_at_utc >= since)
    if unreviewed_only:
        statement = statement.where(
            ResourceChangeEvent.explanation == EXPLANATION_OUTSIDE_CONSOLE,
            ResourceChangeEvent.reviewed_at_utc.is_(None),
        )
    with request.app.state.session_factory() as session:
        rows = session.execute(statement).scalars()
        return ChangeEventPage(events=[summarize_change_event(row) for row in rows])


@router.post("/changes/{event_id}/review", response_model=ChangeEventSummary)
def review_change(request: Request, event_id: int) -> ChangeEventSummary:
    """Mark a change as reviewed by the current operator."""
    with request.app.state.session_factory() as session:
        try:
            event = review_change_event(session, event_id, request.app.state.settings.operator_name)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        session.commit()
        return summarize_change_event(event)
