"""Parameter metadata, comparison and upgrade preview."""

from __future__ import annotations

from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from rdscommander.aws.errors import diagnose_exception
from rdscommander.aws.rds_reader import RdsReader
from rdscommander.config.models import ResolvedTarget
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeDiagnosticError
from rdscommander.parameters.compare import ParameterComparison, compare_parameter_groups
from rdscommander.parameters.metadata import (
    EngineVersionInfo,
    FamilyMetadata,
    ParameterScope,
    UnknownParameterFamilyError,
    load_engine_versions,
    load_family_metadata,
)
from rdscommander.parameters.upgrade_builder import UpgradePreview, build_upgrade_plan
from rdscommander.resources.identity import decode_resource_id
from rdscommander.resources.queries import load_resource

logger = get_logger(__name__)
router = APIRouter(prefix="/api/v1/parameters", tags=["parameters"])


class EngineVersionList(BaseModel):
    """Versions of one engine.

    Attributes:
        versions: Each version with its parameter group family and valid upgrade targets.
    """

    versions: list[EngineVersionInfo]


def _first_target(request: Request, environment_slug: str) -> ResolvedTarget:
    """The environment's first target, through which metadata is read; 404 when there is none."""
    console_config = request.app.state.config_store.current
    targets = console_config.targets_for_environment(environment_slug)
    if not targets:
        raise HTTPException(status_code=404, detail=f"unknown environment {environment_slug!r}")
    return targets[0]


def _reader_for(request: Request, target: ResolvedTarget, step_name: str) -> RdsReader | None:
    """A reader over a recorded RDS client for a real target; None for a simulated one (the bundle)."""
    if target.simulated:
        return None
    client = request.app.state.services.aws_sessions.client(
        target, "rds", operation_id=None, step_name=step_name
    )
    return RdsReader(client)


def _bad_gateway(error: ClientError | BotoCoreError, target: ResolvedTarget, step_name: str) -> HTTPException:
    """A 502 that carries the diagnosis of an AWS failure: what failed and what to do about it."""
    kind, detail, fix_suggestion = diagnose_exception(error, target.aws_profile_name, "")
    logger.error(
        "parameter_metadata_request_failed",
        step_name=step_name,
        target_key=target.target_key,
        diagnostic_kind=kind,
        detail=detail,
        fix_suggestion=fix_suggestion,
    )
    return HTTPException(
        status_code=502, detail={"kind": kind, "detail": detail, "fix_suggestion": fix_suggestion}
    )


@router.get("/families/{family}", response_model=FamilyMetadata)
def read_family(
    request: Request, family: str, environment_slug: str, scope: ParameterScope = "instance"
) -> FamilyMetadata:
    """Metadata for a family's instance or cluster parameter groups, through the environment's first target.

    Args:
        request: The incoming request.
        family: Parameter group family, e.g. `postgres16`.
        environment_slug: Environment whose first target is asked (a simulated one answers from the bundle).
        scope: `instance` for instance parameter groups (the default), `cluster` for cluster parameter
            groups; an Aurora family lists other parameters for each.

    Returns:
        The parameters that groups of that family and scope have, with allowed values and apply types, and
        whether AWS or the bundle supplied them.

    Raises:
        HTTPException: 404 for an unknown environment or family; 502, with the diagnosis, when AWS fails.
    """
    target = _first_target(request, environment_slug)
    try:
        reader = _reader_for(request, target, "read_family")
        with request.app.state.session_factory() as session:
            loaded = load_family_metadata(session, family, reader, scope=scope, simulated=target.simulated)
            session.commit()
    except UnknownParameterFamilyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except (ClientError, BotoCoreError) as error:
        raise _bad_gateway(error, target, "read_family") from error
    return loaded


@router.get("/engine-versions", response_model=EngineVersionList)
def read_engine_versions(
    request: Request, environment_slug: str, engine: Literal["postgres", "aurora-postgresql"]
) -> EngineVersionList:
    """Engine versions with families and upgrade targets.

    Args:
        request: The incoming request.
        environment_slug: Environment whose first target is asked (a simulated one answers from the bundle).
        engine: `postgres` or `aurora-postgresql`.

    Returns:
        The engine's versions, each with its parameter group family and valid upgrade targets.

    Raises:
        HTTPException: 404 for an unknown environment; 502, with the diagnosis, when AWS fails.
    """
    target = _first_target(request, environment_slug)
    try:
        reader = _reader_for(request, target, "read_engine_versions")
        with request.app.state.session_factory() as session:
            versions = load_engine_versions(session, engine, reader, simulated=target.simulated)
            session.commit()
    except (ClientError, BotoCoreError) as error:
        raise _bad_gateway(error, target, "read_engine_versions") from error
    return EngineVersionList(versions=versions)


def _arn_of(resource_id: str) -> str:
    """The ARN behind a resource id; 404 when the id is not one."""
    try:
        return decode_resource_id(resource_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/compare", response_model=ParameterComparison)
def read_comparison(
    request: Request, left: str, right: str | None = None, against_defaults: bool = False
) -> ParameterComparison:
    """Compare a parameter group with its family's defaults or with another group.

    Args:
        request: The incoming request.
        left: Resource id of the left parameter group.
        right: Resource id of the right parameter group; the family defaults when omitted.
        against_defaults: Compare with the family defaults even when `right` is given.

    Returns:
        One row per parameter overridden on either side (every family parameter against defaults), with the
        two values, the family default, whether they differ and whether the parameter needs a reboot.

    Raises:
        HTTPException: 404 for an id that is not a resource id or a group missing from the latest inventory
            (or a family neither cached nor bundled); 422 when a resource is not a parameter group.
    """
    left_arn = _arn_of(left)
    right_arn = _arn_of(right) if right else None
    console_config = request.app.state.config_store.current
    with request.app.state.session_factory() as session:
        try:
            return compare_parameter_groups(
                session, console_config, left_arn, right_arn, against_defaults=against_defaults
            )
        except KeyError as error:
            raise HTTPException(
                status_code=404, detail=str(error.args[0] if error.args else error)
            ) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error


class UpgradePreviewRequest(BaseModel):
    """The resource to build upgrade groups for and the version it will be upgraded to.

    Attributes:
        resource_id: Resource id of the instance or cluster.
        target_engine_version: The version it will be upgraded to, e.g. `17.2`.
    """

    resource_id: str
    target_engine_version: str = Field(pattern=r"^\d+(\.\d+){0,2}$")


@router.post("/upgrade-preview", response_model=UpgradePreview)
def upgrade_preview(request: Request, body: UpgradePreviewRequest) -> UpgradePreview:
    """Classify the current overrides against the target family without changing anything.

    Args:
        request: The incoming request.
        body: The instance or cluster and the version it will be upgraded to.

    Returns:
        One plan per group an upgrade build would create (the instance group and, for a cluster, the
        cluster group): the source group, the new group's name and family, every override classified as
        carried, default changed, needing review, removed or renamed, and the parameters the new group would
        hold. Also the families the instance and cluster groups are built for.

    Raises:
        HTTPException: 404 for an id that is not a resource id or a resource missing from the latest
            inventory; 422 when the groups cannot be worked out (the members of a cluster use different
            instance groups, or a family is unknown); 502, with the diagnosis, when AWS fails.
    """
    arn = _arn_of(body.resource_id)
    console_config = request.app.state.config_store.current
    with request.app.state.session_factory() as session:
        record = load_resource(session, console_config, arn)
        if record is None:
            raise HTTPException(status_code=404, detail=f"resource {arn} is not in the latest inventory")
        target = console_config.target(record.target_key)
        try:
            reader = _reader_for(request, target, "upgrade_preview")
            plans = build_upgrade_plan(
                session, console_config, record, body.target_engine_version, reader=reader
            )
            session.commit()
        except UnknownParameterFamilyError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ChangeDiagnosticError as error:
            raise HTTPException(status_code=422, detail=f"{error}; {error.fix_suggestion}") from error
        except (ClientError, BotoCoreError) as error:
            raise _bad_gateway(error, target, "upgrade_preview") from error
    instance_family = next(
        (plan.target_family for plan in plans if plan.scope == "instance"), plans[0].target_family
    )
    cluster_family = next((plan.target_family for plan in plans if plan.scope == "cluster"), None)
    return UpgradePreview(
        plans=plans, target_family_instance=instance_family, target_family_cluster=cluster_family
    )
