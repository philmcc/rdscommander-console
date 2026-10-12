"""The runner: poll, lease, observe, execute, record. Safe to run several at once."""

from __future__ import annotations

import threading
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig
from rdscommander.config.settings import OperatorSettings
from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import Operation, utc_now
from rdscommander.control_db.session import ControlDbSessionFactory
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import OperationState
from rdscommander.operations.executor import StepContext, StepExecutor
from rdscommander.operations.lease import acquire_next_operation, release_lease
from rdscommander.operations.service import CLEANUP_DONE_PLAN_KEY, CLEANUP_PENDING_PLAN_KEY
from rdscommander.operations.step_walk import (
    StepWalkEnd,
    fail_operation,
    succeed_operation,
    walk_operation_steps,
)
from rdscommander.services import ServiceBundle

logger = get_logger(__name__)


class OperationRunner:
    """Walks queued operations through their executors."""

    def __init__(
        self,
        session_factory: ControlDbSessionFactory,
        executors: dict[str, StepExecutor],
        runner_identity: str,
        settings: OperatorSettings,
        config_store: ConfigStoreProtocol,
        services: ServiceBundle,
        lease_seconds: int = 30,
    ) -> None:
        self.session_factory = session_factory
        self.executors = executors
        self.runner_identity = runner_identity
        self.settings = settings
        self.config_store = config_store
        self.services = services
        self.lease_seconds = lease_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start polling in a daemon thread."""
        self._thread = threading.Thread(target=self._poll_forever, name="operation-runner", daemon=True)
        self._thread.start()
        logger.info("operation_runner_started", runner_identity=self.runner_identity)

    def stop(self) -> None:
        """Stop polling; the current step finishes first."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self.lease_seconds)

    def _poll_forever(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.run_once() is None:
                    self._stop_event.wait(self.settings.runner_poll_interval_seconds)
            except Exception as error:  # noqa: BLE001 - the loop must survive
                logger.error(
                    "operation_runner_poll_failed",
                    error=str(error),
                    fix_suggestion="check control database connectivity",
                )
                self._stop_event.wait(self.settings.runner_poll_interval_seconds)

    def run_once(self) -> uuid.UUID | None:
        """Claim and process one operation; returns its id, or None when nothing was claimable.

        The configuration is read once per call, so a reload between operations takes effect on the next
        operation while one operation always sees a single consistent configuration.

        Before claiming work, the runner lets the executors clean up after operations that were rejected
        while they waited for approval (see `_clean_up_cancelled_operations`).
        """
        console_config = self.config_store.current
        self._clean_up_cancelled_operations(console_config)
        with self.session_factory() as session:
            operation = acquire_next_operation(session, self.runner_identity, self.lease_seconds)
            if operation is None:
                return None
            executor = self.executors.get(operation.operation_kind)
            if executor is None:
                fail_operation(
                    session,
                    operation,
                    None,
                    "unknown_operation_kind",
                    f"no executor for {operation.operation_kind}",
                    "upgrade the console or fix the operation kind",
                )
                return operation.id
            operation.state = OperationState.RUNNING.value
            operation.started_at_utc = operation.started_at_utc or utc_now()
            session.commit()
            logger.info(
                "operation_started",
                operation_id=str(operation.id),
                operation_kind=operation.operation_kind,
                runner_identity=self.runner_identity,
            )

            walk = walk_operation_steps(
                session,
                operation,
                executor,
                session_factory=self.session_factory,
                settings=self.settings,
                console_config=console_config,
                services=self.services,
                runner_identity=self.runner_identity,
                lease_seconds=self.lease_seconds,
            )
            if walk.end == StepWalkEnd.AWAITING_APPROVAL:
                operation.state = OperationState.AWAITING_APPROVAL.value
                session.commit()
                release_lease(session, operation.id)
                logger.info(
                    "operation_awaiting_approval", operation_id=str(operation.id), step_name=walk.step_name
                )
            elif walk.end == StepWalkEnd.COMPLETED:
                succeed_operation(session, operation)
            return operation.id

    def _clean_up_cancelled_operations(self, console_config: ConsoleConfig) -> None:
        """Run `executor.on_cancelled` once for each rejected operation that still has cleanup pending.

        Rejecting an operation sets `cleanup_pending` in its plan. The executor's hook undoes what the
        operation prepared before approval (rendered Terraform files, for instance); when it reports that
        the cleanup is done, the flag is replaced by `cleanup_done_at_utc`. A hook that returns False leaves
        the flag for the console that holds the files. Executors without a hook have nothing to clean, so
        their flag is cleared. A hook that raises is not retried: the failure is logged and recorded as a
        warning diagnostic on the operation. A row lock keeps several consoles from cleaning the same
        operation twice.
        """
        with self.session_factory() as session:
            pending_ids = list(
                session.execute(
                    select(Operation.id)
                    .where(
                        Operation.state == OperationState.CANCELLED.value,
                        Operation.plan[CLEANUP_PENDING_PLAN_KEY].astext == "true",
                    )
                    .order_by(Operation.created_at_utc)
                ).scalars()
            )
        for operation_id in pending_ids:
            with self.session_factory() as lock_session:
                self._clean_up_cancelled_operation(lock_session, operation_id, console_config)

    def _clean_up_cancelled_operation(
        self, lock_session: Session, operation_id: uuid.UUID, console_config: ConsoleConfig
    ) -> None:
        """Clean up one rejected operation while holding its row lock; commits when it is done."""
        operation = lock_session.execute(
            select(Operation)
            .where(
                Operation.id == operation_id,
                Operation.state == OperationState.CANCELLED.value,
                Operation.plan[CLEANUP_PENDING_PLAN_KEY].astext == "true",
            )
            .with_for_update(skip_locked=True, key_share=True)
        ).scalar_one_or_none()
        executor = self.executors.get(operation.operation_kind) if operation is not None else None
        if operation is None or executor is None:
            lock_session.rollback()
            return
        hook = getattr(executor, "on_cancelled", None)
        cleanup_done = True
        if hook is not None:
            with self.session_factory() as work_session:
                work_operation = work_session.get(Operation, operation.id)
                context = StepContext(
                    session=work_session,
                    operation=work_operation,
                    settings=self.settings,
                    console_config=console_config,
                    services=self.services,
                    runner_identity=self.runner_identity,
                )
                try:
                    cleanup_done = hook(context) is not False
                except Exception as error:  # noqa: BLE001 - one failed cleanup must not stop the runner
                    work_session.rollback()
                    logger.error(
                        "operation_cleanup_failed",
                        operation_id=str(operation.id),
                        error=str(error),
                        fix_suggestion="check the repository state with `git status -- terraform` and "
                        "discard the leftover files by hand",
                    )
                    record_diagnostic(
                        lock_session,
                        operation_id=operation.id,
                        step_name="on_cancelled",
                        severity="warning",
                        diagnostic_kind="cleanup_failed",
                        title="Cleaning up after the rejected operation failed",
                        detail=str(error),
                        fix_suggestion="check the repository state with `git status -- terraform` and "
                        "discard the leftover files by hand",
                    )
        if not cleanup_done:
            lock_session.rollback()
            return
        remaining_plan = {k: v for k, v in operation.plan.items() if k != CLEANUP_PENDING_PLAN_KEY}
        operation.plan = {**remaining_plan, CLEANUP_DONE_PLAN_KEY: utc_now().isoformat()}
        lock_session.commit()
        logger.info("operation_cleanup_done", operation_id=str(operation.id), had_hook=hook is not None)
