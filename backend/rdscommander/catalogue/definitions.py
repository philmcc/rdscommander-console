"""Definition types."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig, EnvironmentConfig
from rdscommander.resources.model import ResourceKind, ResourceRecord


class Applicability(BaseModel):
    """Whether an operation can run on a resource right now, and why not.

    Attributes:
        ok: True when the operation can run.
        reason: Why it cannot run; empty when `ok` is True.
    """

    ok: bool
    reason: str = ""


def ui_field(
    widget: str, help: str, group: str = "", order: int = 0, *, label: str, **extra: Any
) -> dict[str, Any]:
    """`json_schema_extra` for an options field: how the frontend renders it.

    Args:
        widget: Name of the frontend widget, e.g. `target_picker`.
        help: Help text shown beside the field: what it does and what a value looks like. For a field
            with a `pattern`, the first sentence describes the values the pattern accepts; a refused value
            is explained with it.
        group: Optional form section the field belongs to.
        order: Position of the field within its form.
        label: The field's name as a person reads it, e.g. `Resources to adopt`, never the field name.
        **extra: Further widget settings, passed through to the frontend untouched.

    Returns:
        A dict to pass as `Field(json_schema_extra=...)`; the hints appear under the `ui` key of the
        field's JSON schema.

    Example:
        >>> ui_field("target_picker", "The account and region.", order=1, label="Target")["ui"]["label"]
        'Target'
    """
    return {"ui": {"widget": widget, "label": label, "help": help, "group": group, "order": order, **extra}}


OPERATION_FIELDS_PLAN_KEY = "operation_fields"
"""Key under which a plan preparer may return operation columns that the generic creation route would
otherwise take from the resource or the options: `target_key`, `resource_arn` and `subject` (the name shown
in the operation title). The route removes the key from the plan before storing it."""


class PlanPreparationError(Exception):
    """The options cannot become a plan; the creation request is answered 422 with this explanation.

    Attributes:
        detail: One-line explanation.
        problems: Individual problems, when there is more than one.
    """

    def __init__(self, detail: str, problems: list[str] | None = None) -> None:
        """Record the explanation and the individual problems."""
        self.detail = detail
        self.problems = problems or []
        super().__init__(detail)


ApplicabilityRule = Callable[[ResourceRecord | None, EnvironmentConfig], Applicability]
PlanPreparer = Callable[[Session, ConsoleConfig, BaseModel, ResourceRecord | None], dict[str, Any]]


class OperationDefinition(BaseModel):
    """One catalogue entry.

    Attributes:
        kind: Operation kind; also the key of the executor that runs it.
        title: Human title of the operation.
        group: Menu group the operation is listed under.
        summary: One sentence describing what the operation does.
        applies_to: Resource kinds the operation can be launched from.
        modes: Change-management modes in which the operation is available.
        destructive: Whether the operation destroys data or resources.
        disruptive: Whether the operation interrupts running databases.
        options_model: Pydantic model validating the operation's options.
        applicability: Rule deciding whether the operation can run on a resource right now.
        prepare_plan: Builds the operation's plan from validated options; the options themselves
            when None. It may raise `PlanPreparationError` to refuse the request, and may return
            `OPERATION_FIELDS_PLAN_KEY` to set the operation's target, resource and title subject.
        needs_target: Environment-level operation that still needs a `target_key` option.
        changes_the_resource: The operation changes the resource it is launched from, so it is refused
            while that resource has open record drift (the change would record the drifted value).
        pauses_for_approval: Whether a run stops at an approval step before it changes anything; the
            general answer when an option decides it (see `pauses_for_approval_when`).
        pauses_for_approval_when: For a definition where one option decides whether the run pauses, the
            answer per value of that option, e.g. `{"accept": False, "restore": True}`; None otherwise.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: str
    title: str
    group: str
    summary: str
    applies_to: frozenset[ResourceKind]
    modes: frozenset[str] = frozenset({"terraform", "direct"})
    destructive: bool = False
    disruptive: bool = False
    options_model: type[BaseModel]
    applicability: ApplicabilityRule
    prepare_plan: PlanPreparer | None = None
    needs_target: bool = Field(
        default=False, description="Environment-level operation that still needs a target_key option"
    )
    changes_the_resource: bool = Field(
        default=False, description="Refused while the resource it is launched from has open record drift"
    )
    pauses_for_approval: bool = Field(
        default=True, description="A run stops at an approval step before it changes anything"
    )
    pauses_for_approval_when: dict[str, bool] | None = Field(
        default=None, description="Per value of the option that decides it, whether a run pauses for approval"
    )


class ActionAvailability(BaseModel):
    """What the Actions menu and launcher show.

    Attributes:
        kind: Operation kind.
        title: Human title of the operation.
        group: Menu group the operation is listed under.
        summary: One sentence describing what the operation does.
        destructive: Whether the operation destroys data or resources.
        disruptive: Whether the operation interrupts running databases.
        applicable: Whether the operation can run now.
        reason: Why it cannot run; empty when `applicable` is True.
    """

    kind: str
    title: str
    group: str
    summary: str
    destructive: bool
    disruptive: bool
    applicable: bool
    reason: str = ""
