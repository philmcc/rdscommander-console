"""Creates the operations the console should run on its own: inventory refresh and drift checks.

Example:
    >>> scheduler = OperationScheduler(  # doctest: +SKIP
    ...     session_factory, config_store, operator_name="scheduler", interval_seconds=60
    ... )
    >>> scheduler.start()  # doctest: +SKIP
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.catalogue.catalogue import definition_for
from rdscommander.config.models import EnvironmentConfig
from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import InventorySnapshot, Operation
from rdscommander.control_db.session import ControlDbSessionFactory
from rdscommander.logging_setup import get_logger
from rdscommander.operations.enums import OperationState
from rdscommander.operations.service import OperationService

logger = get_logger(__name__)

ACTIVE_OPERATION_STATES = (
    OperationState.QUEUED.value,
    OperationState.RUNNING.value,
    OperationState.AWAITING_APPROVAL.value,
)
INVENTORY_REFRESH_KIND = "inventory_refresh"
DRIFT_DETECT_KIND = "drift_detect"
# Recorded as `created_by_operator_name` on everything the scheduler creates, so scheduled work is
# never attributed to the human operator running the console.
SCHEDULER_OPERATOR_NAME = "scheduler"
BACK_OFF_OPERATION_STATES = (OperationState.FAILED.value, OperationState.CANCELLED.value)
STOP_JOIN_TIMEOUT_SECONDS = 10.0


def _active_operation_exists(session: Session, operation_kind: str, environment_slug: str) -> bool:
    """True when an operation of `operation_kind` is queued, running or awaiting approval there."""
    active_operation_id = session.execute(
        select(Operation.id)
        .where(
            Operation.operation_kind == operation_kind,
            Operation.environment_slug == environment_slug,
            Operation.state.in_(ACTIVE_OPERATION_STATES),
        )
        .limit(1)
    ).first()
    return active_operation_id is not None


def _cadence_for(operation_kind: str, environment: EnvironmentConfig) -> timedelta:
    """How often the environment wants `operation_kind` to run."""
    if operation_kind == INVENTORY_REFRESH_KIND:
        return timedelta(minutes=environment.inventory_refresh_minutes)
    if operation_kind == DRIFT_DETECT_KIND:
        return timedelta(hours=environment.drift_check_hours)
    raise ValueError(f"the scheduler has no cadence for operation kind {operation_kind!r}")


def back_off_remaining(
    session: Session, operation_kind: str, environment: EnvironmentConfig, now: datetime
) -> timedelta | None:
    """How much longer to wait before retrying `operation_kind`, or None when it may be scheduled now.

    The scheduler backs off when the latest operation of the kind for the environment ended FAILED or
    CANCELLED less than one cadence ago (`inventory_refresh_minutes` for refreshes, `drift_check_hours`
    for drift detections). Without it a persistent failure, such as expired AWS credentials, would queue a
    new failing operation on every tick.

    Args:
        session: Control database session.
        operation_kind: `inventory_refresh` or `drift_detect`.
        environment: The environment, whose cadence sets the back-off.
        now: The current time (timezone-aware).

    Returns:
        The time left until the back-off ends, or None when the latest operation did not fail recently.

    Raises:
        ValueError: The scheduler has no cadence for `operation_kind`.

    Example:
        >>> sandbox = config.environment("sandbox")
        >>> back_off_remaining(session, "inventory_refresh", sandbox, now)  # doctest: +SKIP
        datetime.timedelta(seconds=780)
    """
    cadence = _cadence_for(operation_kind, environment)
    latest_operation = session.execute(
        select(Operation.state, Operation.finished_at_utc, Operation.created_at_utc)
        .where(Operation.operation_kind == operation_kind, Operation.environment_slug == environment.slug)
        .order_by(Operation.created_at_utc.desc())
        .limit(1)
    ).first()
    if latest_operation is None or latest_operation.state not in BACK_OFF_OPERATION_STATES:
        return None
    ended_at_utc = latest_operation.finished_at_utc or latest_operation.created_at_utc
    remaining = ended_at_utc + cadence - now
    return remaining if remaining > timedelta(0) else None


def recently_failed(
    session: Session, operation_kind: str, environment: EnvironmentConfig, now: datetime
) -> bool:
    """True when the latest `operation_kind` for the environment failed or was cancelled within one cadence.

    Args:
        session: Control database session.
        operation_kind: `inventory_refresh` or `drift_detect`.
        environment: The environment, whose cadence sets the back-off.
        now: The current time (timezone-aware).

    Returns:
        Whether the scheduler should hold off creating another operation of this kind.

    Raises:
        ValueError: The scheduler has no cadence for `operation_kind`.

    Example:
        >>> sandbox = config.environment("sandbox")
        >>> recently_failed(session, "inventory_refresh", sandbox, now)  # doctest: +SKIP
        True
    """
    return back_off_remaining(session, operation_kind, environment, now) is not None


def _definition_exists(operation_kind: str) -> bool:
    """True when the operation catalogue defines `operation_kind`."""
    try:
        definition_for(operation_kind)
    except KeyError:
        return False
    return True


def refresh_is_due(session: Session, environment: EnvironmentConfig, now: datetime) -> bool:
    """True when any target of the environment has no snapshot or a newest snapshot past its refresh interval.

    An environment that has never been refreshed is due, so a freshly imported environment gets its first
    refresh from the scheduler too.

    Args:
        session: Control database session.
        environment: The environment, whose `inventory_refresh_minutes` is the allowed snapshot age.
        now: The current time (timezone-aware).

    Returns:
        Whether an inventory refresh should be created for the environment.

    Example:
        >>> refresh_is_due(session, config.environment("sandbox"), datetime.now(UTC))  # doctest: +SKIP
        True
    """
    maximum_snapshot_age = timedelta(minutes=environment.inventory_refresh_minutes)
    for target in environment.targets:
        target_key = f"{environment.slug}/{target.aws_account_id}/{target.aws_region}"
        newest_snapshot_captured_at_utc = session.execute(
            select(InventorySnapshot.captured_at_utc)
            .where(InventorySnapshot.target_key == target_key)
            .order_by(InventorySnapshot.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if (
            newest_snapshot_captured_at_utc is None
            or now - newest_snapshot_captured_at_utc > maximum_snapshot_age
        ):
            return True
    return False


def drift_check_is_due(session: Session, environment: EnvironmentConfig, now: datetime) -> bool:
    """True when no drift detection has succeeded in the environment within `drift_check_hours`.

    Args:
        session: Control database session.
        environment: The environment, whose `drift_check_hours` is the allowed age of the last detection.
        now: The current time (timezone-aware).

    Returns:
        Whether a drift detection should be created for the environment.

    Example:
        >>> drift_check_is_due(session, config.environment("sandbox"), datetime.now(UTC))  # doctest: +SKIP
        True
    """
    last_detection_finished_at_utc = session.execute(
        select(Operation.finished_at_utc)
        .where(
            Operation.operation_kind == DRIFT_DETECT_KIND,
            Operation.environment_slug == environment.slug,
            Operation.state == OperationState.SUCCEEDED.value,
        )
        .order_by(Operation.finished_at_utc.desc())
        .limit(1)
    ).scalar_one_or_none()
    if last_detection_finished_at_utc is None:
        return True
    return now - last_detection_finished_at_utc > timedelta(hours=environment.drift_check_hours)


class OperationScheduler:
    """Runs `tick` on an interval in a daemon thread.

    Each tick creates, per environment, at most one inventory refresh and one drift detection. It creates
    none while one of that kind is queued, running or awaiting approval in the environment, nor for one
    cadence after the latest one failed or was cancelled.
    """

    def __init__(
        self,
        session_factory: ControlDbSessionFactory,
        config_store: ConfigStoreProtocol,
        operator_name: str,
        interval_seconds: float,
    ) -> None:
        """Prepare the scheduler; nothing runs until `start`.

        Args:
            session_factory: Control database sessions.
            config_store: Source of the live configuration, read once per tick.
            operator_name: Recorded as `created_by_operator_name` on every operation the scheduler creates.
            interval_seconds: Seconds between ticks.
        """
        self.session_factory = session_factory
        self.config_store = config_store
        self.operator_name = operator_name
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        """Whether the scheduler thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def tick(self) -> list[uuid.UUID]:
        """Create the due operations, at most one of each kind per environment.

        A drift detection is created only when the catalogue defines `drift_detect`, and never in the same
        tick as a refresh of that environment, so drift is judged against fresh inventory. A failure while
        scheduling one environment is logged and does not stop the others. A kind is skipped for an
        environment while one is queued, running or awaiting approval, and while the latest one failed
        or was cancelled less than one cadence ago.

        Returns:
            The ids of the operations created by this tick.

        Example:
            >>> scheduler.tick()  # doctest: +SKIP
            [UUID('0b6f7d0e-...')]
        """
        now = datetime.now(UTC)
        idempotency_bucket = int(now.timestamp() // max(self.interval_seconds, 1))
        drift_detection_is_defined = _definition_exists(DRIFT_DETECT_KIND)
        operation_service = OperationService(self.session_factory)
        created_operation_ids: list[uuid.UUID] = []
        for environment in self.config_store.current.environments:
            try:
                with self.session_factory() as session:
                    refresh_should_be_created = refresh_is_due(
                        session, environment, now
                    ) and self._may_schedule(session, INVENTORY_REFRESH_KIND, environment, now)
                    drift_check_should_be_created = (
                        drift_detection_is_defined
                        and not refresh_should_be_created
                        and drift_check_is_due(session, environment, now)
                        and self._may_schedule(session, DRIFT_DETECT_KIND, environment, now)
                    )
                if refresh_should_be_created:
                    refresh_operation = operation_service.create(
                        INVENTORY_REFRESH_KIND,
                        environment.slug,
                        None,
                        {"options": {"target_keys": []}},
                        f"scheduled:{INVENTORY_REFRESH_KIND}:{environment.slug}:{idempotency_bucket}",
                        self.operator_name,
                        title=f"Refresh inventory: {environment.slug}",
                    )
                    created_operation_ids.append(refresh_operation.id)
                    logger.info(
                        "scheduled_refresh_created",
                        environment_slug=environment.slug,
                        operation_id=str(refresh_operation.id),
                    )
                if drift_check_should_be_created:
                    drift_operation = operation_service.create(
                        DRIFT_DETECT_KIND,
                        environment.slug,
                        None,
                        {"options": {}},
                        f"scheduled:{DRIFT_DETECT_KIND}:{environment.slug}:{idempotency_bucket}",
                        self.operator_name,
                        title=f"Detect drift: {environment.slug}",
                    )
                    created_operation_ids.append(drift_operation.id)
                    logger.info(
                        "scheduled_drift_check_created",
                        environment_slug=environment.slug,
                        operation_id=str(drift_operation.id),
                    )
            except Exception as error:  # noqa: BLE001 - one environment must not starve the others
                logger.error(
                    "scheduler_environment_failed",
                    environment_slug=environment.slug,
                    error=str(error),
                    fix_suggestion="check control database connectivity; the next tick retries it",
                )
        return created_operation_ids

    def _may_schedule(
        self, session: Session, operation_kind: str, environment: EnvironmentConfig, now: datetime
    ) -> bool:
        """False while an operation of the kind is in flight or the latest one failed within a cadence."""
        if _active_operation_exists(session, operation_kind, environment.slug):
            return False
        remaining_back_off = back_off_remaining(session, operation_kind, environment, now)
        if remaining_back_off is not None:
            logger.info(
                "scheduled_operation_backoff",
                environment_slug=environment.slug,
                operation_kind=operation_kind,
                seconds_until_next_attempt=int(remaining_back_off.total_seconds()),
                next_attempt_at_utc=(now + remaining_back_off).isoformat(),
            )
            return False
        return True

    def _loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.tick()
            except Exception as error:  # noqa: BLE001 - the loop must survive
                logger.error(
                    "scheduler_tick_failed",
                    error=str(error),
                    fix_suggestion="check control database connectivity",
                )

    def start(self) -> None:
        """Start ticking in a daemon thread; does nothing when already running."""
        if self.is_running:
            return
        self._thread = threading.Thread(target=self._loop, name="operation-scheduler", daemon=True)
        self._thread.start()
        logger.info("operation_scheduler_started", interval_seconds=self.interval_seconds)

    def stop(self) -> None:
        """Stop ticking and wait for the thread; safe when the scheduler was never started."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=STOP_JOIN_TIMEOUT_SECONDS)
            logger.info("operation_scheduler_stopped")
