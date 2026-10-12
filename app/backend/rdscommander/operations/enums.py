"""State vocabularies shared by the runner, executors, API, and frontend."""

from __future__ import annotations

from enum import StrEnum


class OperationState(StrEnum):
    """Lifecycle of an operation."""

    DRAFT = "DRAFT"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ObservationState(StrEnum):
    """What a step found when it looked at the real world."""

    ABSENT = "ABSENT"
    PENDING = "PENDING"
    SATISFIED = "SATISFIED"
    CONFLICT = "CONFLICT"
    BLOCKED = "BLOCKED"


class StepOutcomeStatus(StrEnum):
    """Result of executing a step."""

    COMPLETED = "completed"
    AWAITING_APPROVAL = "awaiting_approval"
    FAILED = "failed"
    BLOCKED = "blocked"


SECOND_PHASE_STEP_PREFIX = "then_"
"""Prefix of the steps of a chained operation's second change, such as `then_apply_direct`."""
