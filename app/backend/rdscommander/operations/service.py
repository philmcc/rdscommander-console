"""Create, read, approve, and reject operations. Never touches AWS, Terraform, or git."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from rdscommander.control_db.models import Operation, utc_now
from rdscommander.control_db.session import ControlDbSessionFactory
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import OperationState

logger = get_logger(__name__)

CLEANUP_PENDING_PLAN_KEY = "cleanup_pending"
CLEANUP_DONE_PLAN_KEY = "cleanup_done_at_utc"
APPROVAL_STAGE_PLAN_KEY = "approval_stage"
"""Plan key naming the pause of an operation that asks for approval twice (a major upgrade)."""
SWITCHOVER_APPROVAL_STAGE = "switchover"
"""The second pause of a major upgrade: the switchover of production to the green side."""
SWITCHOVER_APPROVED_PLAN_KEY = "switchover_approved_at_utc"
"""Plan key the switchover approval sets; until then the second pause holds."""
SWITCHOVER_APPROVED_BY_PLAN_KEY = "switchover_approved_by"
"""Plan key naming the operator who approved the switchover."""


class OperationStateError(Exception):
    """The operation is not in a state that allows the requested transition."""


class OperationNotFoundError(Exception):
    """No operation with that id."""


APPROVAL_STAGE_MISMATCH_KIND = "approval_stage_mismatch"
"""The approval was given for another pause than the one the operation waits at."""
CONFIRMATION_TEXT_MISMATCH_KIND = "confirmation_text_mismatch"
"""The approval did not carry the identifier the review asks the approver to type."""


class ApprovalMismatchError(Exception):
    """The approval does not match what the operation waits for: another stage, or not the typed identifier.

    Attributes:
        diagnostic_kind: `approval_stage_mismatch` or `confirmation_text_mismatch`.
        fix_suggestion: What the approver should do.

    Example:
        >>> ApprovalMismatchError("approval_stage_mismatch", "stale", "reload").diagnostic_kind
        'approval_stage_mismatch'
    """

    def __init__(self, diagnostic_kind: str, message: str, fix_suggestion: str) -> None:
        """Keep the kind and the fix suggestion beside the message."""
        super().__init__(message)
        self.diagnostic_kind = diagnostic_kind
        self.fix_suggestion = fix_suggestion


def _ensure_approval_matches(
    operation: Operation, approval_stage: str | None, confirmation_text: str | None
) -> None:
    """Refuse an approval for another stage, or without the identifier the review asks to type.

    Raises:
        ApprovalMismatchError: Either does not match; the expected identifier is not repeated in it.
    """
    waiting_stage = operation.plan.get(APPROVAL_STAGE_PLAN_KEY)
    if waiting_stage is not None and approval_stage != waiting_stage:
        error = ApprovalMismatchError(
            APPROVAL_STAGE_MISMATCH_KIND,
            f"the operation waits for approval of the {waiting_stage} stage, but this approval was given "
            f"for {approval_stage or 'no stage'}",
            "reload the operation, read the review it shows now, and approve that",
        )
    elif operation.approval_required_text and confirmation_text != operation.approval_required_text:
        error = ApprovalMismatchError(
            CONFIRMATION_TEXT_MISMATCH_KIND,
            "the typed text is not the identifier shown on the review",
            "type the identifier shown on the review exactly, then approve again",
        )
    else:
        return
    logger.error(
        "operation_approval_refused",
        operation_id=str(operation.id),
        diagnostic_kind=error.diagnostic_kind,
        waiting_approval_stage=waiting_stage,
        given_approval_stage=approval_stage,
        fix_suggestion=error.fix_suggestion,
    )
    raise error


class OperationService:
    """Persistence-only operations API."""

    def __init__(self, session_factory: ControlDbSessionFactory) -> None:
        self.session_factory = session_factory

    def create(
        self,
        operation_kind: str,
        environment_slug: str | None,
        target_key: str | None,
        plan: dict[str, Any],
        idempotency_key: str,
        created_by_operator_name: str,
        *,
        resource_arn: str | None = None,
        title: str = "",
        approval_required_text: str | None = None,
    ) -> Operation:
        """Persist a new QUEUED operation, or return the existing one for `idempotency_key`.

        Args:
            operation_kind: Executor key, e.g. `inventory_refresh`.
            environment_slug: Environment scope; None for operations that belong to no environment.
            target_key: Target scope when the operation is per target.
            plan: Immutable input of the operation.
            idempotency_key: Client-supplied key; repeats return the first operation created with it.
            created_by_operator_name: Operator creating the operation.
            resource_arn: ARN of the resource the operation acts on, if any.
            title: Human title shown in lists.
            approval_required_text: Identifier the approver must type, for destructive or disruptive work.

        Returns:
            The new operation, or the existing one for `idempotency_key`.

        Example:
            >>> OperationService(session_factory).create(
            ...     "inventory_refresh", "sandbox", None, {}, "key-1", "phil",
            ...     title="Refresh inventory: sandbox",
            ... )
            <Operation ...>
        """
        with self.session_factory() as session:
            existing = session.execute(
                select(Operation).where(Operation.idempotency_key == idempotency_key)
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            operation = Operation(
                operation_kind=operation_kind,
                state=OperationState.QUEUED.value,
                environment_slug=environment_slug,
                target_key=target_key,
                resource_arn=resource_arn,
                title=title,
                approval_required_text=approval_required_text,
                plan=plan,
                idempotency_key=idempotency_key,
                created_by_operator_name=created_by_operator_name,
            )
            session.add(operation)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return session.execute(
                    select(Operation).where(Operation.idempotency_key == idempotency_key)
                ).scalar_one()
            logger.info(
                "operation_created",
                operation_id=str(operation.id),
                operation_kind=operation_kind,
                environment_slug=environment_slug,
                resource_arn=resource_arn,
            )
            return operation

    def get(self, operation_id: uuid.UUID) -> Operation:
        """Fetch one operation with its attempts and diagnostics loaded."""
        with self.session_factory() as session:
            operation = session.get(Operation, operation_id)
            if operation is None:
                raise OperationNotFoundError(str(operation_id))
            _ = operation.step_attempts, operation.diagnostic_reports
            return operation

    def list(
        self,
        limit: int = 50,
        environment_slug: str | None = None,
        operation_kind: str | None = None,
        resource_arn: str | None = None,
        states: list[str] | None = None,
        include_operations_without_environment: bool = False,
    ) -> list[Operation]:
        """Most recent operations first, optionally narrowed by every filter that is given.

        Args:
            limit: The most operations to return.
            environment_slug: Keep operations of this environment; operations of every environment, and of
                none, when None.
            operation_kind: Keep operations of this kind.
            resource_arn: Keep operations acting on this resource.
            states: Keep operations in any of these states, for example `["QUEUED", "RUNNING"]`; no
                filter when None or empty.
            include_operations_without_environment: With `environment_slug`, also keep operations that
                belong to no environment (environment imports).

        Returns:
            Matching operations, newest first.

        Example:
            >>> OperationService(session_factory).list(states=["RUNNING"], limit=5)  # doctest: +SKIP
            [<Operation ...>]
        """
        statement = select(Operation).order_by(Operation.created_at_utc.desc()).limit(limit)
        if environment_slug and include_operations_without_environment:
            statement = statement.where(
                or_(Operation.environment_slug == environment_slug, Operation.environment_slug.is_(None))
            )
        elif environment_slug:
            statement = statement.where(Operation.environment_slug == environment_slug)
        if operation_kind:
            statement = statement.where(Operation.operation_kind == operation_kind)
        if resource_arn:
            statement = statement.where(Operation.resource_arn == resource_arn)
        if states:
            statement = statement.where(Operation.state.in_(states))
        with self.session_factory() as session:
            return list(session.execute(statement).scalars().all())

    def approve(
        self,
        operation_id: uuid.UUID,
        operator_name: str,
        *,
        approval_stage: str | None = None,
        confirmation_text: str | None = None,
    ) -> Operation:
        """Record approval and requeue so the runner resumes.

        The approval must be for the pause the operation waits at now: when the plan names a stage
        (`plan["approval_stage"]`, a major upgrade's `create_green` or `switchover`), `approval_stage` must
        be that stage, so an approval sent from a review page loaded at the first pause cannot approve the
        switchover. When the operation asks for an identifier to be typed (`approval_required_text`),
        `confirmation_text` must be exactly it.

        An operation paused at its second stage (a major upgrade waiting for the switchover, with
        `plan["approval_stage"] == "switchover"` and a first approval recorded) keeps the first approval's
        audit trail: the switchover approval goes to `plan["switchover_approved_by"]` and
        `plan["switchover_approved_at_utc"]` instead.

        Args:
            operation_id: The operation awaiting approval.
            operator_name: Who approves.
            approval_stage: The stage the approver reviewed; checked when the plan names one.
            confirmation_text: What the approver typed; checked when the operation asks for typing.

        Returns:
            The requeued operation.

        Raises:
            OperationNotFoundError: No operation has that id.
            OperationStateError: The operation is not awaiting approval.
            ApprovalMismatchError: The stage or the typed text is not what the operation waits for; the
                operation keeps waiting.

        Example:
            >>> OperationService(session_factory).approve(  # doctest: +SKIP
            ...     operation_id, "sam", approval_stage="switchover", confirmation_text="demo-orders"
            ... ).state
            'QUEUED'
        """
        with self.session_factory() as session:
            operation = session.get(Operation, operation_id)
            if operation is None:
                raise OperationNotFoundError(str(operation_id))
            if operation.state != OperationState.AWAITING_APPROVAL.value:
                raise OperationStateError(f"operation is {operation.state}, approval needs AWAITING_APPROVAL")
            _ensure_approval_matches(operation, approval_stage, confirmation_text)
            approved_at_utc = utc_now()
            is_switchover_approval = (
                operation.plan.get(APPROVAL_STAGE_PLAN_KEY) == SWITCHOVER_APPROVAL_STAGE
                and operation.approved_at_utc is not None
            )
            if is_switchover_approval:
                # Replaced as a whole: an in-place edit of the JSON column is not seen by the session.
                operation.plan = {
                    **operation.plan,
                    SWITCHOVER_APPROVED_BY_PLAN_KEY: operator_name,
                    SWITCHOVER_APPROVED_PLAN_KEY: approved_at_utc.isoformat(),
                }
            else:
                operation.approved_by_operator_name = operator_name
                operation.approved_at_utc = approved_at_utc
            operation.state = OperationState.QUEUED.value
            session.commit()
            logger.info(
                "operation_approved",
                operation_id=str(operation_id),
                operator_name=operator_name,
                approval_stage=SWITCHOVER_APPROVAL_STAGE if is_switchover_approval else "first",
            )
            return operation

    def reject(self, operation_id: uuid.UUID, operator_name: str) -> Operation:
        """Cancel an operation that is awaiting approval.

        The plan gets `cleanup_pending`: the runner lets the operation's executor clean up whatever it
        prepared (for instance rendered Terraform files) the next time it runs, and then replaces the flag
        with `cleanup_done_at_utc`.
        """
        with self.session_factory() as session:
            operation = session.get(Operation, operation_id)
            if operation is None:
                raise OperationNotFoundError(str(operation_id))
            if operation.state != OperationState.AWAITING_APPROVAL.value:
                raise OperationStateError(
                    f"operation is {operation.state}, rejection needs AWAITING_APPROVAL"
                )
            operation.state = OperationState.CANCELLED.value
            operation.plan = {**operation.plan, CLEANUP_PENDING_PLAN_KEY: True}
            operation.failure_summary = f"rejected by {operator_name}"
            operation.finished_at_utc = utc_now()
            session.commit()
            logger.info("operation_rejected", operation_id=str(operation_id), operator_name=operator_name)
            return operation
