"""The step contract every executor implements.

Example:
    class InventoryRefreshExecutor:
        operation_kind = "inventory_refresh"

        def step_names(self, operation): ...
        def observe(self, step_name, context): ...
        def execute(self, step_name, context): ...
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig, ResolvedTarget
from rdscommander.config.settings import OperatorSettings
from rdscommander.control_db.models import Operation, OperationStepAttempt
from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
from rdscommander.services import ServiceBundle


class StepObservation(BaseModel):
    """What `observe` found."""

    state: ObservationState
    detail: str = ""
    output: dict[str, Any] = Field(default_factory=dict)


class StepOutcome(BaseModel):
    """What `execute` did."""

    status: StepOutcomeStatus
    detail: str = ""
    output: dict[str, Any] = Field(default_factory=dict)
    fix_suggestion: str | None = None
    diagnostic_recorded: bool = False
    """True when the executor already wrote a specific diagnostic; the runner then adds no generic one."""


@dataclass
class StepContext:
    """Everything a step may touch."""

    session: Session
    operation: Operation
    settings: OperatorSettings
    console_config: ConsoleConfig
    services: ServiceBundle
    runner_identity: str = "unknown-runner"

    def completed_step_output(self, step_name: str) -> dict[str, Any] | None:
        """Output of the latest completed attempt of `step_name`, or None when it never completed."""
        statement = (
            select(OperationStepAttempt.output)
            .where(
                OperationStepAttempt.operation_id == self.operation.id,
                OperationStepAttempt.step_name == step_name,
                OperationStepAttempt.outcome_status == StepOutcomeStatus.COMPLETED.value,
            )
            .order_by(OperationStepAttempt.id.desc())
            .limit(1)
        )
        return self.session.execute(statement).scalar_one_or_none()

    def step_already_completed(self, step_name: str) -> bool:
        """True when an earlier attempt of `step_name` on this operation completed."""
        statement = select(OperationStepAttempt.id).where(
            OperationStepAttempt.operation_id == self.operation.id,
            OperationStepAttempt.step_name == step_name,
            OperationStepAttempt.outcome_status == StepOutcomeStatus.COMPLETED.value,
        )
        return self.session.execute(statement).first() is not None

    @property
    def target(self) -> ResolvedTarget:
        """The operation's target; raises when the operation has none."""
        if not self.operation.target_key:
            raise ValueError(f"operation {self.operation.id} has no target_key")
        return self.console_config.target(self.operation.target_key)


class StepExecutor(Protocol):
    """Executor for one operation kind."""

    operation_kind: str

    def step_names(self, operation: Operation) -> list[str]:
        """Ordered step names for this operation."""
        ...

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """Look at real state without changing anything."""
        ...

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Make the step's goal true; must be safe to call again."""
        ...
