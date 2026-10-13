"""The vocabulary of a resource change: what changes, how it is verified, and what describes a change.

These models are shared by the change strategy (`change_strategy.py`), the operations that describe their
changes (`DesiredChange`) and the API that shows a change set for review, so they live apart from the
executor that runs them.

Example:
    >>> entry = ChangeSetEntry(  # doctest: +SKIP
    ...     address="aws_db_parameter_group.demo", attribute="parameters.work_mem", current="16384",
    ...     desired="40000", apply_type="immediate", requires_reboot=False, downtime="none",
    ... )
"""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from rdscommander.operations.executor import StepContext
from rdscommander.terraform.render_input import RenderWorkspaceInput

ApplyType = Literal["immediate", "pending-reboot", "pending-window", "recreate"]


class ChangeSetEntry(BaseModel):
    """One attribute the operation will change.

    Attributes:
        address: Terraform address of the resource.
        attribute: Attribute path, e.g. `parameters.work_mem` or `instance_class`.
        current: The value AWS has now.
        desired: The value the operation will set.
        apply_type: How AWS applies it: `immediate`, `pending-reboot` (at the next reboot) or `recreate`.
        requires_reboot: Whether the change only takes effect after a reboot.
        downtime: What the approver should expect, `none` when nothing is interrupted.
    """

    address: str
    attribute: str
    current: Any
    desired: Any
    apply_type: ApplyType
    requires_reboot: bool
    downtime: str


class VerificationResult(BaseModel):
    """What `verify_direct` read back.

    Attributes:
        ok: True when every attribute of the change set holds its desired value.
        live: The values read back, kept as the review evidence.
        pending: Attributes AWS has accepted but is still applying. The change counts as verified when every
            pending attribute's apply type is `pending-reboot`, because AWS applies those at the next reboot.
            An attribute that is not at its desired value must be listed here, or `ok` stays False and the
            base keeps polling.
        detail: Why verification failed, for the diagnostic.
        terminal: True when the resource reached a status that will never converge to the desired values
            (for example a failed modification). The base stops polling at once and fails with `detail`.
    """

    ok: bool
    live: dict[str, Any] = Field(default_factory=dict)
    pending: list[str] = Field(default_factory=list)
    detail: str = ""
    terminal: bool = False


class ChangeImpact(BaseModel):
    """What a change will cost the approver, known before anything is planned or called.

    Attributes:
        requires_reboot: Whether the change only takes effect after a reboot.
        downtime: What the approver should expect, `none` when nothing is interrupted.
    """

    requires_reboot: bool
    downtime: str


class DesiredChange(Protocol):
    """What one operation wants, expressed for both strategies."""

    resource_arn: str
    summary: str

    def touched_addresses(self, context: StepContext) -> set[str]:
        """Terraform addresses of every resource the change creates, edits or deletes."""
        ...

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: `render_input` with the change applied (a copy; the argument is not modified)."""
        ...

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """Direct mode: what would change, from a fresh read of the resource; empty when nothing would."""
        ...

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Direct mode: call the AWS API; returns what was submitted."""
        ...

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """Direct mode: read the resource back and say whether the change holds."""
        ...

    # A change may also provide `expected_impact(self, context) -> ChangeImpact`. Terraform mode has no
    # change set to read the reboot and downtime from, so the base asks the change for it (when it has the
    # method) while rendering. It must not call AWS: the inventory record and the cached family metadata
    # are all it may use.


class ChangeDiagnosticError(Exception):
    """A change that cannot be made, with the diagnostic the operator should see.

    Raised while working out what a change is; the change strategy turns it into a failed step with
    `diagnostic_kind`, `title`, the exception message as detail and `fix_suggestion`.

    Attributes:
        diagnostic_kind: Machine-readable kind, e.g. `restore_not_possible`.
        title: Short headline for the diagnostic.
        fix_suggestion: What the operator can do instead.
    """

    diagnostic_kind = "change_not_possible"
    title = "This change is not possible"
    fix_suggestion = "correct the request and create the operation again"

    def __init__(self, message: str, fix_suggestion: str | None = None) -> None:
        """Keep `message` as the detail; `fix_suggestion` overrides the class default when given."""
        super().__init__(message)
        if fix_suggestion is not None:
            self.fix_suggestion = fix_suggestion
