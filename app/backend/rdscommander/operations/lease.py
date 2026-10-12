"""Leases make several backends on one control database safe."""

from __future__ import annotations

import threading
import uuid
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from rdscommander.control_db.models import Operation, utc_now
from rdscommander.control_db.session import ControlDbSessionFactory
from rdscommander.operations.enums import OperationState

CLAIMABLE_STATES = (OperationState.QUEUED.value, OperationState.RUNNING.value)


def acquire_next_operation(session: Session, runner_identity: str, lease_seconds: int) -> Operation | None:
    """Claim the oldest claimable operation whose lease is free or expired."""
    now = utc_now()
    candidate = session.execute(
        select(Operation.id)
        .where(Operation.state.in_(CLAIMABLE_STATES))
        .where((Operation.lease_owner.is_(None)) | (Operation.lease_expires_at_utc < now))
        .order_by(Operation.created_at_utc)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).scalar_one_or_none()
    if candidate is None:
        session.rollback()
        return None
    session.execute(
        update(Operation)
        .where(Operation.id == candidate)
        .values(lease_owner=runner_identity, lease_expires_at_utc=now + timedelta(seconds=lease_seconds))
    )
    session.commit()
    return session.get(Operation, candidate)


def renew_lease(session: Session, operation_id: uuid.UUID, runner_identity: str, lease_seconds: int) -> bool:
    """Extend the lease if this runner still holds it."""
    result = session.execute(
        update(Operation)
        .where(Operation.id == operation_id, Operation.lease_owner == runner_identity)
        .values(lease_expires_at_utc=utc_now() + timedelta(seconds=lease_seconds))
    )
    session.commit()
    return result.rowcount == 1


def release_lease(session: Session, operation_id: uuid.UUID) -> None:
    """Drop the lease."""
    session.execute(
        update(Operation)
        .where(Operation.id == operation_id)
        .values(lease_owner=None, lease_expires_at_utc=None)
    )
    session.commit()


class LeaseHeartbeat:
    """Renews an operation's lease on its own session while a long step runs."""

    def __init__(
        self,
        session_factory: ControlDbSessionFactory,
        operation_id: uuid.UUID,
        runner_identity: str,
        lease_seconds: int,
    ) -> None:
        self.session_factory = session_factory
        self.operation_id = operation_id
        self.runner_identity = runner_identity
        self.interval_seconds = max(lease_seconds / 3.0, 0.2)
        self.lease_lost = False
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lease-heartbeat", daemon=True)

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            with self.session_factory() as session:
                if not renew_lease(
                    session, self.operation_id, self.runner_identity, int(self.interval_seconds * 3) + 1
                ):
                    self.lease_lost = True
                    return

    def __enter__(self) -> LeaseHeartbeat:
        """Start renewing."""
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        """Stop renewing and wait for the thread."""
        self._stop_event.set()
        self._thread.join(timeout=self.interval_seconds * 2)
