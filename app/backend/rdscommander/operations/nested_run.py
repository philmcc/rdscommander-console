"""Drive another operation to its end from inside a step, recording it as the runner would.

The runner is one polling thread, and a step cannot wait for another runner pass, so a step that needs a
second operation (the parameter groups of a major upgrade, for instance) creates that operation already
approved and RUNNING, its lease held by the parent's runner, so that no runner claims it, and walks its
steps inline through `operations.step_walk`. The nested operation ends SUCCEEDED or FAILED on its own row,
with its own step attempts and diagnostics; the parent step reads the result.

The nested operation is approved by the parent's approval: it carries the parent's approver and approval time
(the parent's creator and now, for a parent that has none) and the plan notes `approved_with_operation_id`.
It may not pause: a step that asks for approval fails it, because no one can approve it inline.

Example:
    >>> child_id = create_nested_operation(  # doctest: +SKIP
    ...     context, operation_kind="parameter_group_build_for_upgrade", plan=plan,
    ...     title="Build upgrade parameter groups: demo-orders",
    ...     idempotency_key=f"{context.operation.id}:groups",
    ... )
    >>> run_nested_operation(context, child_id, builder).state  # doctest: +SKIP
    'SUCCEEDED'
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from rdscommander.control_db.models import Operation, utc_now
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import OperationState
from rdscommander.operations.executor import StepContext, StepExecutor
from rdscommander.operations.step_walk import (
    StepWalkEnd,
    fail_operation,
    succeed_operation,
    walk_operation_steps,
)

logger = get_logger(__name__)

APPROVED_WITH_OPERATION_PLAN_KEY = "approved_with_operation_id"
"""Plan key of a nested operation naming the operation whose approval covers it."""
NESTED_PAUSE_DIAGNOSTIC_KIND = "nested_operation_paused"
"""Diagnostic of a nested operation that asked for approval, which no one can give inline."""
FINISHED_STATES = frozenset(
    {OperationState.SUCCEEDED.value, OperationState.FAILED.value, OperationState.CANCELLED.value}
)


@dataclass(frozen=True)
class NestedRunResult:
    """How a nested operation ended.

    Attributes:
        operation_id: The nested operation.
        state: Its state: SUCCEEDED, FAILED or CANCELLED; RUNNING when another runner holds its lease.
        failure_summary: Why it did not succeed, in one line; None when it succeeded.
        fix_suggestion: The fix suggestion of its latest error diagnostic, if any.

    Example:
        >>> NestedRunResult(uuid.uuid4(), "SUCCEEDED").failure_summary is None
        True
    """

    operation_id: uuid.UUID
    state: str
    failure_summary: str | None = None
    fix_suggestion: str | None = None


def create_nested_operation(
    parent_context: StepContext,
    *,
    operation_kind: str,
    plan: dict[str, Any],
    title: str,
    idempotency_key: str,
) -> uuid.UUID:
    """Add the nested operation, approved with the parent, RUNNING and leased to the parent's runner.

    `approved_by_operator_name` and `approved_at_utc` are the parent's: whoever approved the parent approved
    this work. A parent without an approval (one that never pauses) lends its creator and the current time.

    It is never QUEUED, so no runner picks it up. A second call with the same `idempotency_key` returns the
    operation the first call created.

    Args:
        parent_context: The running step of the parent operation.
        operation_kind: Kind of the nested operation; its executor walks it.
        plan: The nested operation's plan; `approved_with_operation_id` is added.
        title: Human title of the nested operation.
        idempotency_key: Key unique to this nested operation of this parent.

    Returns:
        The nested operation's id.

    Example:
        >>> create_nested_operation(  # doctest: +SKIP
        ...     context, operation_kind="parameter_group_build_for_upgrade", plan={...}, title="Build",
        ...     idempotency_key="major-1:groups",
        ... )
        UUID('...')
    """
    session = parent_context.session
    existing_id = session.execute(
        select(Operation.id).where(Operation.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing_id is not None:
        return existing_id
    parent = parent_context.operation
    now_utc = utc_now()
    approved_by_operator_name = parent.approved_by_operator_name or parent.created_by_operator_name
    approved_at_utc = parent.approved_at_utc or now_utc
    child = Operation(
        operation_kind=operation_kind,
        state=OperationState.RUNNING.value,
        environment_slug=parent.environment_slug,
        target_key=parent.target_key,
        resource_arn=parent.resource_arn,
        title=title,
        plan={**plan, APPROVED_WITH_OPERATION_PLAN_KEY: str(parent.id)},
        idempotency_key=idempotency_key,
        created_by_operator_name=parent.created_by_operator_name,
        approved_by_operator_name=approved_by_operator_name,
        approved_at_utc=approved_at_utc,
        started_at_utc=now_utc,
        lease_owner=parent_context.runner_identity,
        lease_expires_at_utc=now_utc + timedelta(seconds=parent_context.settings.lease_seconds),
    )
    session.add(child)
    session.commit()
    logger.info(
        "nested_operation_created",
        operation_id=str(child.id),
        operation_kind=operation_kind,
        parent_operation_id=str(parent.id),
        approved_by_operator_name=approved_by_operator_name,
    )
    return child.id


def _latest_error_fix_suggestion(child: Operation) -> str | None:
    """The fix suggestion of the child's latest error diagnostic."""
    errors = [report for report in child.diagnostic_reports if report.severity == "error"]
    return errors[-1].fix_suggestion if errors else None


def _result_of(child: Operation) -> NestedRunResult:
    """The result a finished (or foreign-held) child reports."""
    if child.state == OperationState.SUCCEEDED.value:
        return NestedRunResult(child.id, child.state)
    return NestedRunResult(
        child.id, child.state, child.failure_summary or child.state, _latest_error_fix_suggestion(child)
    )


def run_nested_operation(
    parent_context: StepContext, child_operation_id: uuid.UUID, executor: StepExecutor
) -> NestedRunResult:
    """Walk the nested operation's remaining steps inline and report how it ended.

    A child that already finished is only reported. A RUNNING child (a crash interrupted it) is resumed
    after its completed steps once its lease is free, expired or already the parent runner's; a lease
    another runner holds leaves it alone. The walk uses its own session, so the parent's session is not
    touched, and the child's lease is renewed before each step and during each execution.

    Args:
        parent_context: The running step of the parent operation.
        child_operation_id: The nested operation, from `create_nested_operation`.
        executor: The executor of the nested operation's kind.

    Returns:
        Its end state, with the failure summary and fix suggestion when it did not succeed.

    Example:
        >>> run_nested_operation(context, child_id, builder).state  # doctest: +SKIP
        'SUCCEEDED'
    """
    runner_identity = parent_context.runner_identity
    lease_seconds = parent_context.settings.lease_seconds
    session_factory = parent_context.services.session_factory
    with session_factory() as session:
        child = session.get(Operation, child_operation_id)
        if child is None:
            logger.error(
                "nested_operation_missing",
                operation_id=str(child_operation_id),
                parent_operation_id=str(parent_context.operation.id),
                fix_suggestion="create the operation again; its nested operation was removed",
            )
            raise LookupError(f"nested operation {child_operation_id} does not exist")
        if child.state in FINISHED_STATES:
            return _result_of(child)
        now_utc = utc_now()
        claimed = session.execute(
            update(Operation)
            .where(
                Operation.id == child.id,
                Operation.lease_owner.is_(None)
                | (Operation.lease_owner == runner_identity)
                | (Operation.lease_expires_at_utc < now_utc),
            )
            .values(
                state=OperationState.RUNNING.value,
                lease_owner=runner_identity,
                lease_expires_at_utc=now_utc + timedelta(seconds=lease_seconds),
            )
        ).rowcount
        session.commit()
        session.refresh(child)
        if claimed != 1:
            logger.warning(
                "nested_operation_held_by_another_runner",
                operation_id=str(child.id),
                lease_owner=child.lease_owner,
                fix_suggestion="wait for the other runner to finish it, then run the parent operation again",
            )
            return NestedRunResult(
                child.id,
                child.state,
                f"operation {child.id} is being run by {child.lease_owner}",
                "wait for that runner to finish it, then create the parent operation again",
            )
        logger.info(
            "nested_operation_started",
            operation_id=str(child.id),
            operation_kind=child.operation_kind,
            parent_operation_id=str(parent_context.operation.id),
            runner_identity=runner_identity,
        )
        walk = walk_operation_steps(
            session,
            child,
            executor,
            session_factory=session_factory,
            settings=parent_context.settings,
            console_config=parent_context.console_config,
            services=parent_context.services,
            runner_identity=runner_identity,
            lease_seconds=lease_seconds,
        )
        if walk.end == StepWalkEnd.COMPLETED:
            succeed_operation(session, child)
        elif walk.end == StepWalkEnd.AWAITING_APPROVAL:
            fail_operation(
                session,
                child,
                walk.step_name,
                NESTED_PAUSE_DIAGNOSTIC_KIND,
                f"{child.operation_kind} paused for approval at {walk.step_name}, but it runs inside "
                f"operation {parent_context.operation.id}, whose approval covers it",
                "run this change as an operation of its own, then create the parent operation again",
            )
        session.refresh(child)
        logger.info(
            "nested_operation_finished",
            operation_id=str(child.id),
            parent_operation_id=str(parent_context.operation.id),
            state=child.state,
            walk_end=walk.end.value,
        )
        return _result_of(child)
