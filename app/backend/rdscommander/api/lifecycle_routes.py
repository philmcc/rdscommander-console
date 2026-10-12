"""Lifecycle helper routes: what AWS offers for the choices an operation's form shows."""

from __future__ import annotations

from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from rdscommander.aws.errors import diagnose_exception
from rdscommander.aws.rds_reader import SimulatorUnsupportedAction
from rdscommander.config.models import ResolvedTarget
from rdscommander.lifecycle.direct_api import orderable_instance_classes
from rdscommander.lifecycle.restore_window import format_utc, restore_window, restore_window_unknown_reason
from rdscommander.logging_setup import get_logger
from rdscommander.resources.identity import decode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import load_resource

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/lifecycle", tags=["lifecycle"])


class InstanceClassList(BaseModel):
    """Instance classes AWS offers for one engine version.

    Attributes:
        classes: Sorted instance classes such as `db.m6g.large`; empty when the account cannot say.
    """

    classes: list[str]


class RestoreWindowResponse(BaseModel):
    """The times a point-in-time restore of an instance or cluster can choose.

    Attributes:
        earliest_utc: The earliest restorable time, ISO 8601 UTC with a `Z`; None when unknown.
        latest_restorable_utc: The latest restorable time AWS reports; None when unknown.
        unknown_reason: Why the window is unknown (automated backups off, or AWS did not report it); None
            when both times are given.
    """

    earliest_utc: str | None
    latest_restorable_utc: str | None
    unknown_reason: str | None = None


def _record_in_environment(
    request: Request, resource_id: str, environment_slug: str | None
) -> ResourceRecord:
    """The resource's record from the latest inventory, in `environment_slug` when one is given.

    Raises:
        HTTPException: 404 for an id that is not a resource id, or a resource missing from the latest
            inventory (of the environment, when given).
    """
    try:
        resource_arn = decode_resource_id(resource_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    console_config = request.app.state.config_store.current
    with request.app.state.session_factory() as session:
        record = load_resource(session, console_config, resource_arn)
    if record is None or (environment_slug is not None and record.environment_slug != environment_slug):
        where = (
            f"the latest inventory of {environment_slug!r}" if environment_slug else "the latest inventory"
        )
        raise HTTPException(status_code=404, detail=f"{resource_arn} is not in {where}")
    return record


@router.get("/instance-classes", response_model=InstanceClassList)
def read_instance_classes(
    request: Request,
    environment_slug: str,
    resource_id: str | None = None,
    engine: Literal["postgres", "aurora-postgresql"] | None = None,
    engine_version: str | None = None,
    target_key: str | None = None,
) -> InstanceClassList:
    """The instance classes AWS offers for an engine version: of a resource, or one a create chose.

    With `resource_id`, the engine and version are the instance's, cluster's or snapshot's (a snapshot for the
    restore form). Without one (an instance or cluster create), `engine` and `engine_version` say what to
    ask about, in `target_key` or else the environment's first target.

    Args:
        request: The incoming request.
        environment_slug: Environment the resource or the create belongs to.
        resource_id: Resource id of the instance, cluster or snapshot; None for a create.
        engine: `postgres` or `aurora-postgresql`, when no resource is given.
        engine_version: The version, such as `16.4`, when no resource is given.
        target_key: The target a create puts its resource in; the environment's first target when None.

    Returns:
        The orderable classes, sorted; an empty list when the simulated account cannot answer.

    Raises:
        HTTPException: 404 for an id that is not a resource id, a resource missing from the latest inventory
            of the environment, or an unknown environment or target; 422 when the resource is not an
            instance, cluster or snapshot, or neither a resource nor an engine and version is given; 502,
            with the diagnosis, when AWS fails.

    Example:
        GET /api/v1/lifecycle/instance-classes?environment_slug=sandbox&resource_id=... returns
        `{"classes": ["db.m6g.large", "db.m6g.xlarge"]}`; so does
        `?environment_slug=sandbox&engine=postgres&engine_version=16.4`.
    """
    console_config = request.app.state.config_store.current
    if resource_id is not None:
        record = _record_in_environment(request, resource_id, environment_slug)
        if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER, ResourceKind.SNAPSHOT):
            raise HTTPException(
                status_code=422, detail="instance classes are offered for instances, clusters and snapshots"
            )
        target = console_config.target(record.target_key)
        # A snapshot's facts carry its engine version only; its engine is in the describe entry.
        asked_engine = str(record.facts.get("engine") or record.raw.get("Engine") or "")
        asked_version = str(record.facts.get("engine_version") or "")
        subject = record.arn
    else:
        if not engine or not engine_version:
            raise HTTPException(
                status_code=422,
                detail="give a resource_id, or an engine and engine_version for a resource to be created",
            )
        target = _target_in_environment(console_config, environment_slug, target_key)
        asked_engine, asked_version, subject = engine, engine_version, target.target_key
    logger.info(
        "instance_classes_requested",
        subject=subject,
        engine=asked_engine,
        engine_version=asked_version,
    )
    try:
        rds = request.app.state.services.aws_sessions.client(
            target, "rds", operation_id=None, step_name="instance_classes"
        )
        classes = orderable_instance_classes(rds, asked_engine, asked_version)
    except SimulatorUnsupportedAction:
        logger.info("instance_classes_unsupported_by_simulator", subject=subject)
        return InstanceClassList(classes=[])
    except (ClientError, BotoCoreError) as error:
        kind, detail, fix_suggestion = diagnose_exception(error, target.aws_profile_name, "")
        logger.error(
            "instance_classes_request_failed",
            subject=subject,
            diagnostic_kind=kind,
            detail=detail,
            fix_suggestion=fix_suggestion,
        )
        raise HTTPException(
            status_code=502, detail={"kind": kind, "detail": detail, "fix_suggestion": fix_suggestion}
        ) from error
    return InstanceClassList(classes=classes)


def _target_in_environment(
    console_config: Any, environment_slug: str, target_key: str | None
) -> ResolvedTarget:
    """`target_key` when it belongs to the environment, else the environment's first target.

    Raises:
        HTTPException: 404 for an unknown environment or a target that is not one of its targets.
    """
    targets = console_config.targets_for_environment(environment_slug)
    if not targets:
        raise HTTPException(status_code=404, detail=f"unknown environment {environment_slug!r}")
    if target_key is None:
        return targets[0]
    for target in targets:
        if target.target_key == target_key:
            return target
    raise HTTPException(status_code=404, detail=f"{target_key} is not a target of {environment_slug}")


@router.get("/restore-window/{resource_id}", response_model=RestoreWindowResponse)
def read_restore_window(request: Request, resource_id: str) -> RestoreWindowResponse:
    """The window a point-in-time restore of an instance or cluster can choose a time from.

    Read from the latest inventory (`LatestRestorableTime`, and `EarliestRestorableTime` or the creation time
    and backup retention); nothing is asked of AWS. The point-in-time restore checks a chosen time against
    the same window.

    Args:
        request: The incoming request.
        resource_id: Resource id of the instance or cluster.

    Returns:
        Both times as ISO 8601 UTC, or nulls with the reason when the inventory cannot say.

    Raises:
        HTTPException: 404 for an id that is not a resource id or a resource missing from the latest
            inventory; 422 when the resource is not an instance or cluster.

    Example:
        GET /api/v1/lifecycle/restore-window/<id> returns
        `{"earliest_utc": "2026-10-04T09:55:00Z", "latest_restorable_utc": "2026-10-11T09:55:00Z",
        "unknown_reason": null}`.
    """
    record = _record_in_environment(request, resource_id, None)
    if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        raise HTTPException(status_code=422, detail="a restore window is read for instances and clusters")
    window = restore_window(record)
    if window is None:
        reason = restore_window_unknown_reason(record)
        logger.info("restore_window_unknown", resource_arn=record.arn, reason=reason)
        return RestoreWindowResponse(earliest_utc=None, latest_restorable_utc=None, unknown_reason=reason)
    logger.info("restore_window_read", resource_arn=record.arn, window=window.describe())
    return RestoreWindowResponse(
        earliest_utc=format_utc(window.earliest_utc),
        latest_restorable_utc=format_utc(window.latest_restorable_utc),
    )
