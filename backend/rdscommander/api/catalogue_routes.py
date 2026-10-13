"""Catalogue listing and generic operation creation."""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from rdscommander.api.schemas import OperationCreatedResponse
from rdscommander.api.validation_problems import option_problems
from rdscommander.catalogue.catalogue import (
    OPERATION_DEFINITIONS,
    OPERATION_KINDS_WITHOUT_ENVIRONMENT,
    definition_for,
    refuse_a_change_to_a_resource_with_open_record_drift,
)
from rdscommander.catalogue.definitions import OPERATION_FIELDS_PLAN_KEY, PlanPreparationError
from rdscommander.control_db.models import Operation
from rdscommander.logging_setup import get_logger
from rdscommander.operations.service import OperationService
from rdscommander.resources.identity import decode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.adoption_plan import AdoptionPlanError

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["catalogue"])

IDEMPOTENCY_KEY_COLUMN_LENGTH: int = Operation.__table__.c.idempotency_key.type.length
"""The longest idempotency key the operations table stores."""
NO_SCOPE = "-"


def scoped_idempotency_key(
    operation_kind: str, environment_slug: str | None, target_key: str | None, client_key: str
) -> str:
    """The stored idempotency key: the client's key namespaced by kind, environment and target.

    The same client key used for another environment or target is another operation. A composed key longer
    than the column is replaced by `<kind>:sha256:<digest of the composed key>`, which is as unique and
    always fits, so no key the request accepts can fail to be stored.

    Args:
        operation_kind: Catalogue kind of the operation.
        environment_slug: Environment of the operation; None for operations of no environment.
        target_key: Target of the operation, when it has one.
        client_key: The key the client sent.

    Returns:
        A key of at most `IDEMPOTENCY_KEY_COLUMN_LENGTH` characters.

    Example:
        >>> scoped_idempotency_key("inventory_refresh", "sandbox", None, "k1")
        'inventory_refresh:sandbox:-:k1'
        >>> len(scoped_idempotency_key("inventory_refresh", "sandbox", None, "k" * 200)) <= 200
        True
    """
    composed = f"{operation_kind}:{environment_slug or NO_SCOPE}:{target_key or NO_SCOPE}:{client_key}"
    if len(composed) <= IDEMPOTENCY_KEY_COLUMN_LENGTH:
        return composed
    digest = hashlib.sha256(composed.encode("utf-8")).hexdigest()
    return f"{operation_kind}:sha256:{digest}"[:IDEMPOTENCY_KEY_COLUMN_LENGTH]


class DefinitionSummary(BaseModel):
    """One catalogue entry for the frontend.

    Attributes:
        pauses_for_approval: Whether a run stops at an approval step before it changes anything, in every
            mode the definition is available in. When `pauses_for_approval_when` is set this is the general
            answer and the chosen option value gives the exact one.
        pauses_for_approval_when: Whether a run pauses, per value of the option that decides it (for
            `drift_resolve`, the `choice` option: `{"accept": false, "restore": true}`); None when no
            option changes the answer.
    """

    kind: str
    title: str
    group: str
    summary: str
    applies_to: list[str]
    modes: list[str]
    destructive: bool
    disruptive: bool
    needs_target: bool
    pauses_for_approval: bool
    pauses_for_approval_when: dict[str, bool] | None
    options_schema: dict[str, Any]


class CatalogueResponse(BaseModel):
    """All definitions."""

    definitions: list[DefinitionSummary]


class OperationCreateRequest(BaseModel):
    """Body of `POST /operations`."""

    kind: str
    environment_slug: str | None = None
    resource_id: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=200)


class OperationRequestError(Exception):
    """A request that cannot become an operation.

    Attributes:
        status_code: HTTP status the request deserves.
        detail: One-line explanation.
        problems: Individual problems, when there is more than one.
    """

    def __init__(self, status_code: int, detail: str, problems: list[str] | None = None) -> None:
        """Record the status, the explanation and the individual problems."""
        self.status_code = status_code
        self.detail = detail
        self.problems = problems or []
        super().__init__(detail)


def create_operation_from_request(app_state: Any, body: OperationCreateRequest) -> Operation:
    """Validate kind, environment, mode, resource, applicability and options, then persist the operation.

    Args:
        app_state: `app.state` of the running application (configuration store, session factory, settings).
        body: The creation request.

    Returns:
        The queued operation, or the existing one when `body.idempotency_key` was used before.

    Raises:
        OperationRequestError: 404 for an unknown kind, environment, target or resource; 422 when the
            operation is unavailable in the environment's mode or on the resource, or its options are invalid.

    Example:
        >>> create_operation_from_request(
        ...     app.state,
        ...     OperationCreateRequest(
        ...         kind="inventory_refresh", environment_slug="sandbox", idempotency_key="k1"
        ...     ),
        ... ).title
        'Refresh inventory: sandbox'
    """
    try:
        definition = definition_for(body.kind)
    except KeyError as error:
        raise OperationRequestError(404, str(error)) from error
    console_config = app_state.config_store.current
    environment = None
    if body.environment_slug:
        try:
            environment = console_config.environment(body.environment_slug)
        except KeyError as error:
            raise OperationRequestError(404, str(error)) from error
    record: ResourceRecord | None = None
    with app_state.session_factory() as session:
        if body.resource_id:
            try:
                arn = decode_resource_id(body.resource_id)
            except ValueError as error:
                raise OperationRequestError(404, str(error)) from error
            record = load_resource(session, console_config, arn)
            if record is None:
                raise OperationRequestError(404, f"resource {arn} is not in the latest inventory")
            if environment is None:
                environment = console_config.environment(record.environment_slug)
            elif record.environment_slug != environment.slug:
                raise OperationRequestError(
                    422,
                    f"resource {record.identifier} belongs to {record.environment_slug}, "
                    f"not {environment.slug}",
                )
        if record is None and ResourceKind.ENVIRONMENT not in definition.applies_to:
            raise OperationRequestError(
                422,
                f"{definition.title} acts on a resource; resource_id is required",
                ["resource_id: required"],
            )
        if record is not None and record.kind not in definition.applies_to:
            applicable_kinds = ", ".join(sorted(kind.value for kind in definition.applies_to))
            raise OperationRequestError(
                422,
                f"{definition.title} does not apply to {record.kind.value} resources",
                [f"resource_id: {record.kind.value} is not supported; supported kinds: {applicable_kinds}"],
            )
        if environment is None and definition.kind not in OPERATION_KINDS_WITHOUT_ENVIRONMENT:
            raise OperationRequestError(422, "environment_slug is required", ["environment_slug: required"])
        if environment is not None:
            if environment.change_management not in definition.modes:
                raise OperationRequestError(
                    422, f"{definition.title} is not available in {environment.change_management} mode"
                )
            verdict = definition.applicability(record, environment)
            if not verdict.ok:
                raise OperationRequestError(
                    422, f"{definition.title} cannot run here: {verdict.reason}", [verdict.reason]
                )
        try:
            options = definition.options_model.model_validate(body.options)
        except ValidationError as error:
            raise OperationRequestError(
                422, "invalid options", option_problems(error.errors(), definition.options_model)
            ) from error
        if definition.needs_target:
            requested_target_key = getattr(options, "target_key", "")
            try:
                requested_target = console_config.target(requested_target_key)
            except KeyError as error:
                raise OperationRequestError(404, str(error)) from error
            if environment is not None and requested_target.environment_slug != environment.slug:
                raise OperationRequestError(
                    422,
                    f"target {requested_target.target_key} belongs to {requested_target.environment_slug}, "
                    f"not {environment.slug}",
                )
            if record is not None and requested_target.target_key != record.target_key:
                raise OperationRequestError(
                    422,
                    f"{definition.title} targets {requested_target.target_key} but {record.identifier} "
                    f"is in {record.target_key}",
                    [
                        f"options -> target_key: {requested_target.target_key} differs from the "
                        f"resource's target {record.target_key}"
                    ],
                )
        try:
            refuse_a_change_to_a_resource_with_open_record_drift(session, definition, record)
            plan = (
                definition.prepare_plan(session, console_config, options, record)
                if definition.prepare_plan
                else options.model_dump(mode="json")
            )
        except AdoptionPlanError as error:
            raise OperationRequestError(422, "selection cannot be adopted", error.problems) from error
        except PlanPreparationError as error:
            raise OperationRequestError(422, error.detail, error.problems) from error
        operation_fields = plan.pop(OPERATION_FIELDS_PLAN_KEY, {})
        prepared_target_key = operation_fields.get("target_key")
        if prepared_target_key is not None and environment is not None:
            try:
                prepared_target = console_config.target(prepared_target_key)
            except KeyError as error:
                raise OperationRequestError(404, str(error)) from error
            if prepared_target.environment_slug != environment.slug:
                raise OperationRequestError(
                    422,
                    f"{definition.title} targets {prepared_target.target_key}, which belongs to "
                    f"{prepared_target.environment_slug}, not {environment.slug}",
                )
    plan = {
        **plan,
        "options": options.model_dump(mode="json"),
        "resource_arn": record.arn if record else operation_fields.get("resource_arn"),
        "resource_kind": record.kind.value if record else None,
    }
    target_key = (
        record.target_key
        if record
        else operation_fields.get("target_key", getattr(options, "target_key", None))
    )
    # A preparer may name the subject even for a resource-launched operation (a create titles it with the
    # new resource's name while the operation stays on the resource it was launched from).
    subject = operation_fields.get("subject") or (
        record.identifier if record else (environment.slug if environment else getattr(options, "slug", ""))
    )
    title = f"{definition.title}: {subject}" if subject else definition.title
    return OperationService(app_state.session_factory).create(
        definition.kind,
        environment.slug if environment else None,
        target_key,
        plan,
        scoped_idempotency_key(
            definition.kind, environment.slug if environment else None, target_key, body.idempotency_key
        ),
        app_state.settings.operator_name,
        resource_arn=plan["resource_arn"],
        title=title,
        approval_required_text=(
            record.identifier if record and (definition.destructive or definition.disruptive) else None
        ),
    )


def create_operation_response(
    app_state: Any, body: OperationCreateRequest
) -> OperationCreatedResponse | JSONResponse:
    """Create the operation and answer 202 with its id, or answer the request error as `{detail, problems}`.

    Args:
        app_state: `app.state` of the running application.
        body: The creation request.

    Returns:
        The creation response, or a JSON response carrying the error status, `detail` and `problems`.

    Example:
        >>> create_operation_response(app.state, OperationCreateRequest(kind="nope", idempotency_key="k"))
        <JSONResponse status_code=404>
    """
    try:
        operation = create_operation_from_request(app_state, body)
    except OperationRequestError as error:
        logger.warning(
            "operation_request_rejected",
            operation_kind=body.kind,
            environment_slug=body.environment_slug,
            status_code=error.status_code,
            detail=error.detail,
            problems=error.problems,
            fix_suggestion="correct the request as described in detail and problems, then submit it again",
        )
        return JSONResponse(
            status_code=error.status_code, content={"detail": error.detail, "problems": error.problems}
        )
    return OperationCreatedResponse(operation_id=operation.id, state=operation.state)


@router.get("/catalogue", response_model=CatalogueResponse)
def read_catalogue() -> CatalogueResponse:
    """Every operation definition with its options schema."""
    return CatalogueResponse(
        definitions=[
            DefinitionSummary(
                kind=d.kind,
                title=d.title,
                group=d.group,
                summary=d.summary,
                applies_to=sorted(k.value for k in d.applies_to),
                modes=sorted(d.modes),
                destructive=d.destructive,
                disruptive=d.disruptive,
                needs_target=d.needs_target,
                pauses_for_approval=d.pauses_for_approval,
                pauses_for_approval_when=d.pauses_for_approval_when,
                options_schema=d.options_model.model_json_schema(),
            )
            for d in OPERATION_DEFINITIONS
        ]
    )


@router.post("/operations", response_model=OperationCreatedResponse, status_code=202)
def create_operation(
    request: Request, body: OperationCreateRequest
) -> OperationCreatedResponse | JSONResponse:
    """Create any catalogue operation."""
    return create_operation_response(request.app.state, body)
