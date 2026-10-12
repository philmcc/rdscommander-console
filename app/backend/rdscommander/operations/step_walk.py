"""Walk an operation's steps: observe, execute, record each attempt, and fail with a diagnostic.

The runner walks every operation it claims through here, and a step that drives another operation inline
(`operations.nested_run`) walks that operation through here too, so both record step attempts, outcomes
and diagnostics the same way.

Example:
    >>> walk = walk_operation_steps(  # doctest: +SKIP
    ...     session, operation, executor, session_factory=factory, settings=settings,
    ...     console_config=config, services=services, runner_identity="r", lease_seconds=30,
    ... )
    >>> walk.end  # doctest: +SKIP
    <StepWalkEnd.COMPLETED: 'completed'>
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rdscommander.aws.errors import diagnose_exception
from rdscommander.config.models import ConsoleConfig
from rdscommander.config.settings import OperatorSettings
from rdscommander.control_db.models import DiagnosticReport, Operation, OperationStepAttempt, utc_now
from rdscommander.control_db.session import ControlDbSessionFactory
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import ObservationState, OperationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepExecutor
from rdscommander.operations.lease import LeaseHeartbeat, release_lease, renew_lease
from rdscommander.services import ServiceBundle

logger = get_logger(__name__)


class StepWalkEnd(StrEnum):
    """Where a walk over an operation's steps stopped."""

    COMPLETED = "completed"
    """Every step is done; the caller marks the operation succeeded."""
    AWAITING_APPROVAL = "awaiting_approval"
    """A step paused for approval; the caller decides what a pause means."""
    FAILED = "failed"
    """A step failed, was blocked or raised; the operation is already FAILED with a diagnostic."""
    LEASE_LOST = "lease_lost"
    """Another runner took the operation over; nothing more is recorded here."""


@dataclass(frozen=True)
class StepWalkResult:
    """The end of a walk and the step it stopped at.

    Attributes:
        end: Where the walk stopped.
        step_name: The step that paused, failed or lost the lease; None when every step completed.

    Example:
        >>> StepWalkResult(StepWalkEnd.COMPLETED).step_name is None
        True
    """

    end: StepWalkEnd
    step_name: str | None = None


def start_step_attempt(session: Session, operation: Operation, step_name: str) -> OperationStepAttempt:
    """Add and commit the next attempt of `step_name`, numbered after the earlier ones.

    Example:
        >>> start_step_attempt(session, operation, "plan").attempt_number  # doctest: +SKIP
        1
    """
    previous = session.execute(
        select(func.count(OperationStepAttempt.id)).where(
            OperationStepAttempt.operation_id == operation.id,
            OperationStepAttempt.step_name == step_name,
        )
    ).scalar_one()
    attempt = OperationStepAttempt(
        operation_id=operation.id,
        step_name=step_name,
        attempt_number=previous + 1,
        observation_state=ObservationState.ABSENT.value,
    )
    session.add(attempt)
    session.commit()
    return attempt


def finish_step_attempt(
    session: Session,
    attempt: OperationStepAttempt,
    status: StepOutcomeStatus,
    output: dict[str, Any],
    detail: str | None = None,
) -> None:
    """Store the attempt's outcome, output and (when given) detail, and commit.

    Example:
        >>> finish_step_attempt(session, attempt, StepOutcomeStatus.COMPLETED, {})  # doctest: +SKIP
    """
    attempt.outcome_status = status.value
    attempt.output = output
    if detail is not None:
        attempt.detail = detail
    attempt.finished_at_utc = utc_now()
    session.commit()


def fail_operation(
    session: Session,
    operation: Operation,
    step_name: str | None,
    kind: str,
    detail: str,
    fix_suggestion: str,
    add_diagnostic: bool = True,
) -> None:
    """Mark the operation FAILED (with a diagnostic unless the step wrote one), release its lease, log it.

    Example:
        >>> fail_operation(session, operation, "plan", "step_failed", "no", "retry")  # doctest: +SKIP
    """
    if add_diagnostic:
        session.add(
            DiagnosticReport(
                operation_id=operation.id,
                step_name=step_name,
                severity="error",
                diagnostic_kind=kind,
                title=f"{operation.operation_kind} failed at {step_name or 'start'}",
                detail=detail,
                fix_suggestion=fix_suggestion,
            )
        )
    operation.state = OperationState.FAILED.value
    operation.failure_summary = detail.splitlines()[0][:500] if detail else kind
    operation.finished_at_utc = utc_now()
    session.commit()
    release_lease(session, operation.id)
    logger.error(
        "operation_failed",
        operation_id=str(operation.id),
        step_name=step_name,
        diagnostic_kind=kind,
        fix_suggestion=fix_suggestion,
    )


def succeed_operation(session: Session, operation: Operation) -> None:
    """Mark the operation SUCCEEDED, release its lease, log it.

    Example:
        >>> succeed_operation(session, operation)  # doctest: +SKIP
    """
    operation.state = OperationState.SUCCEEDED.value
    operation.finished_at_utc = utc_now()
    session.commit()
    release_lease(session, operation.id)
    logger.info("operation_succeeded", operation_id=str(operation.id))


def profile_name_for(operation: Operation, console_config: ConsoleConfig) -> str | None:
    """The AWS profile of the operation's target, for diagnosing an exception; None without a target."""
    if not operation.target_key:
        return None
    try:
        return console_config.target(operation.target_key).aws_profile_name
    except KeyError:
        return None


def walk_operation_steps(
    session: Session,
    operation: Operation,
    executor: StepExecutor,
    *,
    session_factory: ControlDbSessionFactory,
    settings: OperatorSettings,
    console_config: ConsoleConfig,
    services: ServiceBundle,
    runner_identity: str,
    lease_seconds: int,
) -> StepWalkResult:
    """Observe and execute each step of a RUNNING operation that `runner_identity` holds the lease of.

    A step `observe` finds satisfied is recorded as completed and skipped; a blocked or conflicting one fails
    the operation. Every other step is executed while a heartbeat renews the lease; an exception becomes a
    failed attempt and a diagnostic. A failed or blocked outcome fails the operation. The lease is renewed
    before each step, and the walk stops as soon as another runner holds it.

    Args:
        session: Session the operation is loaded in; attempts and diagnostics are committed through it.
        operation: The operation, RUNNING and leased by `runner_identity`.
        executor: The operation kind's executor.
        session_factory: Sessions for the lease heartbeat.
        settings: Operator settings handed to each step.
        console_config: The configuration the whole walk sees.
        services: Services handed to each step.
        runner_identity: Identity holding the lease.
        lease_seconds: Length of each lease renewal.

    Returns:
        Where the walk stopped. A completed walk or a pause leaves the operation's state to the caller.

    Example:
        >>> walk_operation_steps(session, operation, executor, ...).end  # doctest: +SKIP
        <StepWalkEnd.COMPLETED: 'completed'>
    """
    for step_name in executor.step_names(operation):
        if not renew_lease(session, operation.id, runner_identity, lease_seconds):
            logger.warning(
                "operation_lease_lost",
                operation_id=str(operation.id),
                step_name=step_name,
                fix_suggestion="another runner took over; nothing to do",
            )
            return StepWalkResult(StepWalkEnd.LEASE_LOST, step_name)
        attempt = start_step_attempt(session, operation, step_name)
        context = StepContext(
            session=session,
            operation=operation,
            settings=settings,
            console_config=console_config,
            services=services,
            runner_identity=runner_identity,
        )
        try:
            observation = executor.observe(step_name, context)
            attempt.observation_state = observation.state.value
            attempt.detail = observation.detail
            if observation.state == ObservationState.SATISFIED:
                finish_step_attempt(session, attempt, StepOutcomeStatus.COMPLETED, observation.output)
                continue
            if observation.state in (ObservationState.BLOCKED, ObservationState.CONFLICT):
                finish_step_attempt(session, attempt, StepOutcomeStatus.BLOCKED, observation.output)
                fail_operation(
                    session,
                    operation,
                    step_name,
                    "step_blocked",
                    observation.detail,
                    "resolve the reported condition and create a new operation",
                )
                return StepWalkResult(StepWalkEnd.FAILED, step_name)
            with LeaseHeartbeat(session_factory, operation.id, runner_identity, lease_seconds) as heartbeat:
                outcome = executor.execute(step_name, context)
            if heartbeat.lease_lost:
                logger.warning(
                    "operation_lease_lost",
                    operation_id=str(operation.id),
                    step_name=step_name,
                    fix_suggestion="another runner took over during the step; its outcome is discarded here",
                )
                return StepWalkResult(StepWalkEnd.LEASE_LOST, step_name)
        except Exception as error:  # noqa: BLE001 - every exception becomes a diagnostic
            session.rollback()
            attempt = start_step_attempt(session, operation, step_name)
            finish_step_attempt(
                session, attempt, StepOutcomeStatus.FAILED, {"exception": repr(error)}, detail=str(error)
            )
            kind, title_detail, fix_suggestion = diagnose_exception(
                error, profile_name_for(operation, console_config), traceback.format_exc()
            )
            fail_operation(session, operation, step_name, kind, title_detail, fix_suggestion)
            return StepWalkResult(StepWalkEnd.FAILED, step_name)
        finish_step_attempt(session, attempt, outcome.status, outcome.output, detail=outcome.detail)
        if outcome.status == StepOutcomeStatus.COMPLETED:
            continue
        if outcome.status == StepOutcomeStatus.AWAITING_APPROVAL:
            return StepWalkResult(StepWalkEnd.AWAITING_APPROVAL, step_name)
        kind = "step_failed" if outcome.status == StepOutcomeStatus.FAILED else "step_blocked"
        fail_operation(
            session,
            operation,
            step_name,
            kind,
            outcome.detail,
            outcome.fix_suggestion or "inspect the diagnostics and create a new operation",
            add_diagnostic=not outcome.diagnostic_recorded,
        )
        return StepWalkResult(StepWalkEnd.FAILED, step_name)
    return StepWalkResult(StepWalkEnd.COMPLETED)
