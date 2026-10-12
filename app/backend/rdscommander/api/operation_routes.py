"""Operations: list, detail, approve, reject."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, computed_field
from sqlalchemy import select

from rdscommander.catalogue.catalogue import OPERATION_KINDS_WITHOUT_ENVIRONMENT
from rdscommander.control_db.models import (
    ChangeSet,
    ExecutionRecord,
    Operation,
    OperationStepAttempt,
    TerraformRun,
)
from rdscommander.logging_setup import get_logger
from rdscommander.operations.chained_change import SECOND_PHASE_PLAN_KEY, second_phase_plan_key
from rdscommander.operations.change_models import ChangeSetEntry
from rdscommander.operations.enums import OperationState, StepOutcomeStatus
from rdscommander.operations.service import (
    ApprovalMismatchError,
    OperationNotFoundError,
    OperationService,
    OperationStateError,
)
from rdscommander.resources.identity import decode_resource_id, encode_resource_id

router = APIRouter(prefix="/api/v1", tags=["operations"])
logger = get_logger(__name__)

PROGRESS_STATES = frozenset(
    {OperationState.QUEUED.value, OperationState.RUNNING.value, OperationState.AWAITING_APPROVAL.value}
)
"""States in which an operation row in the list carries `progress`."""


class OperationProgress(BaseModel):
    """How far an operation that has not finished has got.

    Attributes:
        completed_steps: Steps with a completed attempt (a step the live state already satisfied counts).
        total_steps: Steps the operation's executor will walk for this operation.
        current_step: The step of the latest attempt; None before the first attempt.
    """

    completed_steps: int
    total_steps: int
    current_step: str | None


class OperationSummary(BaseModel):
    """List row.

    Attributes:
        resource_arn: The resource the operation acts on; None for an operation on a whole environment.
        resource_id: `resource_arn` as the URL-safe id the resource pages and filters use; None without one.
        title: What the operation does, in the words the operator reads in the review.
        approval_required_text: The text the operator must type to approve; None when a plain click suffices.
        progress: Step progress; filled in the list for queued, running and awaiting-approval operations
            whose kind has an executor, None everywhere else.
    """

    id: uuid.UUID
    operation_kind: str
    state: str
    environment_slug: str | None
    target_key: str | None
    resource_arn: str | None
    title: str
    approval_required_text: str | None
    created_by_operator_name: str
    approved_by_operator_name: str | None
    failure_summary: str | None
    created_at_utc: datetime
    updated_at_utc: datetime
    finished_at_utc: datetime | None
    progress: OperationProgress | None = None

    @computed_field
    @property
    def resource_id(self) -> str | None:
        """The URL-safe id of `resource_arn`, or None when the operation has no resource."""
        return encode_resource_id(self.resource_arn) if self.resource_arn else None


class StepAttemptSummary(BaseModel):
    """Step attempt row."""

    id: int
    step_name: str
    attempt_number: int
    observation_state: str
    outcome_status: str | None
    detail: str
    output: dict[str, Any]
    started_at_utc: datetime
    finished_at_utc: datetime | None


class DiagnosticSummary(BaseModel):
    """Diagnostic row."""

    id: int
    step_name: str | None
    severity: str
    diagnostic_kind: str
    title: str
    detail: str
    fix_suggestion: str
    created_at_utc: datetime


class ExecutionRecordSummary(BaseModel):
    """Execution record with full output."""

    id: int
    step_name: str | None
    record_kind: str
    target_key: str | None
    command_line: str | None
    working_directory: str | None
    api_name: str | None
    request_parameters: dict[str, Any] | None
    response_payload: dict[str, Any] | None
    standard_output: str | None
    standard_error: str | None
    exit_code: int | None
    succeeded: bool
    error_message: str | None
    started_at_utc: datetime
    duration_milliseconds: int


class TerraformRunSummary(BaseModel):
    """Terraform run row."""

    id: int
    run_kind: str
    exit_code: int
    plan_summary: dict[str, Any] | None
    execution_record_id: int | None
    created_at_utc: datetime


class OperationDetail(OperationSummary):
    """Everything about one operation.

    Attributes:
        change_set: Direct mode: the attributes the operation will change, from the plan; None before it
            has been computed and in Terraform mode. A chained operation's second change set follows the
            first: the one shown at review, then the one computed after the approval.
        change_set_verification: The live values read back after the change was applied; None until then.
        requires_reboot: Whether any change in the change set only takes effect after a reboot.
        downtime: What the approver should expect; `none` when nothing is interrupted.
        pending_reboot_attributes: Attributes AWS holds until the next reboot, after verification.
        foreign_owner_banner: The tooling that owns the resource when the environment allows changing it
            anyway; the review shows it as a warning.
        touched_addresses: Terraform addresses of the resources the operation changes.
    """

    plan: dict[str, Any]
    step_attempts: list[StepAttemptSummary]
    diagnostic_reports: list[DiagnosticSummary]
    execution_records: list[ExecutionRecordSummary]
    terraform_runs: list[TerraformRunSummary]
    change_set: list[ChangeSetEntry] | None = None
    change_set_verification: dict[str, Any] | None = None
    requires_reboot: bool = False
    downtime: str = "none"
    pending_reboot_attributes: list[str] = Field(default_factory=list)
    foreign_owner_banner: str | None = None
    touched_addresses: list[str] = Field(default_factory=list)


class OperationList(BaseModel):
    """List response."""

    operations: list[OperationSummary]


def _progress_by_operation_id(request: Request, rows: list[Operation]) -> dict[uuid.UUID, OperationProgress]:
    """Progress of each row that has not finished, from its executor's steps and its step attempts.

    One query reads the attempts of every such row. A row is left out when its kind has no executor or the
    executor cannot list its steps (the target it names is no longer configured, or its plan holds options
    the executor rejects), so one such row never fails the whole list.
    """
    unfinished = [row for row in rows if row.state in PROGRESS_STATES]
    if not unfinished:
        return {}
    with request.app.state.session_factory() as session:
        attempts = session.execute(
            select(
                OperationStepAttempt.operation_id,
                OperationStepAttempt.step_name,
                OperationStepAttempt.outcome_status,
            )
            .where(OperationStepAttempt.operation_id.in_([row.id for row in unfinished]))
            .order_by(OperationStepAttempt.id)
        ).all()
    completed_steps: dict[uuid.UUID, set[str]] = {row.id: set() for row in unfinished}
    current_step: dict[uuid.UUID, str] = {}
    for operation_id, step_name, outcome_status in attempts:
        current_step[operation_id] = step_name
        if outcome_status == StepOutcomeStatus.COMPLETED.value:
            completed_steps[operation_id].add(step_name)
    progress: dict[uuid.UUID, OperationProgress] = {}
    for row in unfinished:
        executor = request.app.state.runner.executors.get(row.operation_kind)
        if executor is None:
            continue
        try:
            step_names = executor.step_names(row)
        except (KeyError, ValueError) as error:
            logger.warning(
                "operation_progress_unavailable",
                operation_id=str(row.id),
                operation_kind=row.operation_kind,
                reason=str(error),
                fix_suggestion=(
                    "restore the operation's target in the configuration, or reject the operation "
                    "if its plan is malformed"
                ),
            )
            continue
        progress[row.id] = OperationProgress(
            completed_steps=len(completed_steps[row.id] & set(step_names)),
            total_steps=len(step_names),
            current_step=current_step.get(row.id),
        )
    return progress


@router.get("/operations", response_model=OperationList)
def list_operations(
    request: Request,
    environment_slug: str | None = None,
    kind: str | None = Query(None, description="Keep operations of this kind, e.g. environment_import"),
    operation_kind: str | None = Query(None, description="The same filter as kind, by its older name"),
    resource_id: str | None = None,
    state: str | None = None,
    limit: int = Query(50, ge=1, le=500),
) -> OperationList:
    """Most recent first.

    Without `environment_slug` the list covers every environment and the operations that belong to none
    (environment imports). `kind` keeps one operation kind (`operation_kind` is the same filter by its older
    name); `kind=environment_import` finds imports even with an `environment_slug`, since an import belongs
    to no environment. `resource_id` keeps the operations on one resource (a value from a resource page or
    an operation row); `state` keeps the given states, one or several separated by commas (`RUNNING`, or
    `QUEUED,RUNNING,AWAITING_APPROVAL` for everything in progress). A queued, running or awaiting-approval
    row carries `progress` (completed and total steps, the current step) when its kind has an executor.

    Raises:
        HTTPException: 422 when `resource_id` is malformed, or `kind` and `operation_kind` differ.
    """
    if kind and operation_kind and kind != operation_kind:
        logger.warning(
            "operation_list_kind_conflict",
            kind=kind,
            operation_kind=operation_kind,
            fix_suggestion="send the kind filter once, as kind",
        )
        raise HTTPException(
            status_code=422, detail=f"kind {kind} and operation_kind {operation_kind} name different kinds"
        )
    selected_kind = kind or operation_kind
    resource_arn: str | None = None
    if resource_id:
        try:
            resource_arn = decode_resource_id(resource_id)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    states = [name.strip() for name in state.split(",") if name.strip()] if state else None
    rows = OperationService(request.app.state.session_factory).list(
        limit=limit,
        environment_slug=environment_slug,
        operation_kind=selected_kind,
        resource_arn=resource_arn,
        states=states,
        include_operations_without_environment=selected_kind in OPERATION_KINDS_WITHOUT_ENVIRONMENT,
    )
    progress = _progress_by_operation_id(request, rows)
    summaries = []
    for row in rows:
        summary = OperationSummary.model_validate(row, from_attributes=True)
        summary.progress = progress.get(row.id)
        summaries.append(summary)
    return OperationList(operations=summaries)


def _change_set_of_every_phase(plan: dict[str, Any]) -> list[Any] | None:
    """The change set of the plan, followed by a chained operation's second change set.

    The second change set is the one computed after the approval once it exists, and before that the one
    shown at review (`plan["second_phase"]["change_set"]`), so it is never listed twice.

    Example:
        >>> _change_set_of_every_phase({"change_set": [1], "change_set_phase_2": [2]})
        [1, 2]
        >>> _change_set_of_every_phase({"change_set": [], "second_phase": {"change_set": [2]}})
        [2]
    """
    first_phase_entries = plan.get("change_set")
    second_phase_entries = plan.get(second_phase_plan_key("change_set"))
    if second_phase_entries is None:
        second_phase_entries = (plan.get(SECOND_PHASE_PLAN_KEY) or {}).get("change_set")
    if second_phase_entries is None:
        return first_phase_entries
    return [*(first_phase_entries or []), *second_phase_entries]


@router.get("/operations/{operation_id}", response_model=OperationDetail)
def read_operation(request: Request, operation_id: uuid.UUID) -> OperationDetail:
    """Detail with attempts, diagnostics, records, and runs."""
    factory = request.app.state.session_factory
    try:
        operation = OperationService(factory).get(operation_id)
    except OperationNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"operation {operation_id} not found") from error
    with factory() as session:
        records = session.execute(
            select(ExecutionRecord)
            .where(ExecutionRecord.operation_id == operation_id)
            .order_by(ExecutionRecord.id)
        ).scalars()
        runs = session.execute(
            select(TerraformRun).where(TerraformRun.operation_id == operation_id).order_by(TerraformRun.id)
        ).scalars()
        latest_change_set = session.execute(
            select(ChangeSet)
            .where(ChangeSet.operation_id == operation_id)
            .order_by(ChangeSet.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        plan = operation.plan
        return OperationDetail(
            **OperationSummary.model_validate(operation, from_attributes=True).model_dump(),
            plan=plan,
            change_set=_change_set_of_every_phase(plan),
            change_set_verification=latest_change_set.verification if latest_change_set else None,
            requires_reboot=bool(plan.get("requires_reboot", False)),
            downtime=str(plan.get("downtime", "none")),
            pending_reboot_attributes=list(plan.get("pending_reboot_attributes", [])),
            foreign_owner_banner=plan.get("foreign_owner_banner"),
            touched_addresses=list(plan.get("touched_addresses", [])),
            step_attempts=[
                StepAttemptSummary.model_validate(a, from_attributes=True) for a in operation.step_attempts
            ],
            diagnostic_reports=[
                DiagnosticSummary.model_validate(d, from_attributes=True)
                for d in operation.diagnostic_reports
            ],
            execution_records=[
                ExecutionRecordSummary.model_validate(r, from_attributes=True) for r in records
            ],
            terraform_runs=[TerraformRunSummary.model_validate(r, from_attributes=True) for r in runs],
        )


class ApprovalRequest(BaseModel):
    """What the approver saw and typed; the server checks both against what the operation waits for.

    Attributes:
        approval_stage: The stage of the review the approver read (`plan["approval_stage"]`, for example a
            major upgrade's `create_green` or `switchover`); required when the operation's plan names one.
        confirmation_text: What the approver typed; required, exactly, when the operation has an
            `approval_required_text`.

    Example:
        >>> ApprovalRequest(approval_stage="switchover", confirmation_text="demo-orders").approval_stage
        'switchover'
    """

    approval_stage: str | None = None
    confirmation_text: str | None = None


def _transition(
    request: Request, operation_id: uuid.UUID, action: str, approval: ApprovalRequest | None = None
) -> OperationSummary:
    service = OperationService(request.app.state.session_factory)
    operator_name = request.app.state.settings.operator_name
    try:
        if action == "approve":
            approval = approval or ApprovalRequest()
            row = service.approve(
                operation_id,
                operator_name,
                approval_stage=approval.approval_stage,
                confirmation_text=approval.confirmation_text,
            )
        else:
            row = service.reject(operation_id, operator_name)
    except OperationNotFoundError as error:
        raise HTTPException(status_code=404, detail=f"operation {operation_id} not found") from error
    except OperationStateError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ApprovalMismatchError as error:
        raise HTTPException(
            status_code=409,
            detail={
                "diagnostic_kind": error.diagnostic_kind,
                "detail": str(error),
                "fix_suggestion": error.fix_suggestion,
            },
        ) from error
    return OperationSummary.model_validate(row, from_attributes=True)


@router.post("/operations/{operation_id}/approve", response_model=OperationSummary)
def approve_operation(
    request: Request, operation_id: uuid.UUID, approval: ApprovalRequest | None = None
) -> OperationSummary:
    """Approve an operation awaiting approval, for the stage reviewed and with the typed identifier.

    A 409 with `diagnostic_kind` `approval_stage_mismatch` or `confirmation_text_mismatch` leaves the
    operation waiting.
    """
    return _transition(request, operation_id, "approve", approval)


@router.post("/operations/{operation_id}/reject", response_model=OperationSummary)
def reject_operation(request: Request, operation_id: uuid.UUID) -> OperationSummary:
    """Reject an operation awaiting approval."""
    return _transition(request, operation_id, "reject")
