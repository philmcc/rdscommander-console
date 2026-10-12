"""Two desired changes run one after the other under one approval.

Some operations cannot be one change: decommissioning a protected database first turns deletion protection
off (and, in Terraform mode, names the final snapshot in the state) and only then deletes it, because
Terraform reads the final snapshot's name from the state it is deleting. `ChainedChangeExecutor` runs the
first change through the environment's strategy, then the second, as one operation:

* The step list is the strategy's steps for the first change, then the second change's steps prefixed
  `then_`, without a second `check_checkout`, `ensure_state_bucket` or `await_approval`: the one approval
  covers both changes, and the review shows what the second will do (`plan["second_phase"]`).
* Each phase commits its own result; the second commit's message ends with ` (phase 2)`.
* The second phase keeps its review artefacts under `_phase_2` plan keys (`change_set_phase_2`,
  `touched_addresses_phase_2`, `plan_summary_phase_2`, ...) so the first phase's stay as they were approved.
* AWS calls, Terraform runs, git commands and diagnostics of the second phase are filed under the prefixed
  step name (`then_apply_direct`), so the execution records say which phase made them.
* When a second-phase step fails, or raises, the diagnostic says that the first change is already applied
  and committed (named from `plan["first_phase"]["summary"]`, with its commit), and how to finish or undo it:
  the operator must not read the failure as "nothing changed".

Example:
    >>> class DecommissionExecutor(ChainedChangeExecutor):  # doctest: +SKIP
    ...     def first_change(self, context):
    ...         return AttributeChange(arn, address, {"deletion_protection": False}, "protection off")
    ...
    ...     def second_change(self, context):
    ...         return ResourceDeleteChange(...)
"""

from __future__ import annotations

import dataclasses
import threading
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from rdscommander.aws.errors import diagnose_exception
from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import Operation
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeSetEntry, DesiredChange
from rdscommander.operations.change_strategy import (
    CHANGE_ERRORS,
    STEPS_SATISFIED_BY_PRIOR_COMPLETION,
    ResourceChangeExecutor,
)
from rdscommander.operations.enums import SECOND_PHASE_STEP_PREFIX, ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome

logger = get_logger(__name__)

FIRST_PHASE = 1
SECOND_PHASE = 2
FIRST_PHASE_ONLY_STEPS = ("check_checkout", "ensure_state_bucket", "await_approval")
"""Steps that run once per operation: the guard, the state bucket and the one approval."""
SECOND_PHASE_PLAN_KEY_SUFFIX = "_phase_2"
SECOND_PHASE_SCOPED_PLAN_KEYS = frozenset(
    {
        "change_set",
        "touched_addresses",
        "requires_reboot",
        "downtime",
        "plan_summary",
        "render_snapshot_id",
        "pending_reboot_attributes",
    }
)
"""Plan entries each phase writes for itself; the second phase's carry `SECOND_PHASE_PLAN_KEY_SUFFIX`."""
SECOND_PHASE_PLAN_KEY = "second_phase"
"""Plan entry the review shows: `{"summary", "what"}` of the change that follows the approved one, and its
`change_set` when it can be computed before approval."""
FIRST_PHASE_PLAN_KEY = "first_phase"
"""Plan entry with the `summary` of the approved first change, so a failed second phase can name it."""
FIRST_PHASE_COMMIT_STEPS = ("commit_to_checkout", "record_to_checkout")
"""The first phase's committing step (Terraform mode's, direct mode's); its output has the `commit_sha`."""
SECOND_PHASE_COMMIT_SUFFIX = " (phase 2)"
NO_DOWNTIME = "none"
__all__ = [
    "FIRST_PHASE_PLAN_KEY",
    "SECOND_PHASE_COMMIT_SUFFIX",
    "SECOND_PHASE_PLAN_KEY",
    "SECOND_PHASE_PLAN_KEY_SUFFIX",
    "ChainedChangeExecutor",
    "second_phase_plan_key",
]


def second_phase_plan_key(name: str) -> str:
    """The plan key the second phase of a chained operation keeps `name` under.

    Example:
        >>> second_phase_plan_key("change_set")
        'change_set_phase_2'
    """
    return f"{name}{SECOND_PHASE_PLAN_KEY_SUFFIX}"


def _prefixed(step_name: str) -> str:
    if step_name.startswith(SECOND_PHASE_STEP_PREFIX):
        return step_name
    return SECOND_PHASE_STEP_PREFIX + step_name


class _StepNamePrefixingService:
    """A service whose calls file their work under the second phase's step names.

    Every method call that passes `step_name=` as a keyword gets it prefixed with `then_`; everything else is
    passed through unchanged to the wrapped service, which is read live (tests replace its methods).
    """

    def __init__(self, wrapped: Any) -> None:
        self._wrapped = wrapped

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._wrapped, name)
        if not callable(attribute):
            return attribute

        def call_with_prefixed_step_name(*args: Any, **kwargs: Any) -> Any:
            step_name = kwargs.get("step_name")
            if isinstance(step_name, str):
                kwargs["step_name"] = _prefixed(step_name)
            return attribute(*args, **kwargs)

        return call_with_prefixed_step_name


class ChainedChangeExecutor(ResourceChangeExecutor):
    """Runs `first_change` and then `second_change` through the environment's strategy, approved once.

    Subclasses implement `first_change` and `second_change`, and may override `_check_before_approval` to
    refuse before anything is rendered or computed. `first_phase_may_be_empty` lets the first change have
    nothing to do in direct mode (the second still does), instead of failing `nothing_to_change`.
    """

    first_phase_may_be_empty = False

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read the live configuration from `config_store`; the phase is kept per thread."""
        super().__init__(config_store)
        self._phase_state = threading.local()

    # --- what subclasses provide -------------------------------------------------------------------

    def first_change(self, context: StepContext) -> DesiredChange:
        """The change the approval is for, run first; subclasses override."""
        raise NotImplementedError

    def second_change(self, context: StepContext) -> DesiredChange:
        """The change that follows once the first is committed; subclasses override."""
        raise NotImplementedError

    def _check_before_approval(self, context: StepContext, step_name: str) -> StepOutcome | None:
        """The failed outcome when the operation must be refused before review, else None (the default)."""
        return None

    def _second_phase_review_entries(
        self, context: StepContext, second: DesiredChange
    ) -> list[ChangeSetEntry] | None:
        """The second change set to show at review, or None (the default) when it can only be computed later.

        A subclass whose second change set needs no AWS state the first change alters (a delete, whose
        entries name only what is deleted) returns it, so the approver sees both changes.
        """
        return None

    def _first_change_still_applied(self, context: StepContext, commit_phrase: str) -> tuple[str, str] | None:
        """What a failed second phase adds to its diagnostic: `(detail sentence, fix suggestion clause)`.

        The default names the first change from `plan["first_phase"]["summary"]`; a subclass says it in its
        own words (a decommission names the deletion protection it turned off), or returns None when no
        first change was made.

        Args:
            context: The running step's context.
            commit_phrase: `committed as <sha>`, or `committed` when the commit is not known.

        Example:
            >>> executor._first_change_still_applied(context, "committed as 1a2b3c")  # doctest: +SKIP
            ('The first change (set work_mem) is already applied and committed as 1a2b3c; ...', '...')
        """
        summary = self._first_phase_summary(context)
        return (
            f"The first change ({summary}) is already applied and {commit_phrase}; this failure does not "
            "undo it.",
            f"the first change ({summary}) stays applied ({commit_phrase}): run the operation again to "
            "finish it, or undo the first change with an operation of its own",
        )

    # --- phase routing -----------------------------------------------------------------------------

    @property
    def current_phase(self) -> int:
        """1 while the first change's steps run, 2 while the second's (`then_*`) do."""
        return int(getattr(self._phase_state, "phase", FIRST_PHASE))

    @contextmanager
    def _in_second_phase(self) -> Iterator[None]:
        self._phase_state.phase = SECOND_PHASE
        try:
            yield
        finally:
            self._phase_state.phase = FIRST_PHASE

    def desired_change(self, context: StepContext) -> DesiredChange:
        """The change of the phase that is running."""
        if self.current_phase == SECOND_PHASE:
            return self.second_change(context)
        return self.first_change(context)

    def step_names(self, operation: Operation) -> list[str]:
        """The strategy's steps, then the second change's prefixed `then_` without the once-only steps."""
        first_phase_steps = super().step_names(operation)
        return first_phase_steps + [
            SECOND_PHASE_STEP_PREFIX + step_name
            for step_name in first_phase_steps
            if step_name not in FIRST_PHASE_ONLY_STEPS
        ]

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """A second-phase step is satisfied only by its own earlier completion, never by the first's."""
        if not step_name.startswith(SECOND_PHASE_STEP_PREFIX):
            return super().observe(step_name, context)
        inner_step_name = step_name.removeprefix(SECOND_PHASE_STEP_PREFIX)
        satisfiable = inner_step_name in STEPS_SATISFIED_BY_PRIOR_COMPLETION
        if satisfiable and context.step_already_completed(step_name):
            return StepObservation(state=ObservationState.SATISFIED)
        return StepObservation(state=ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Run a first-phase step as the strategy does, a `then_` step as the second change's step."""
        if not step_name.startswith(SECOND_PHASE_STEP_PREFIX):
            return super().execute(step_name, context)
        inner_step_name = step_name.removeprefix(SECOND_PHASE_STEP_PREFIX)
        with self._in_second_phase():
            try:
                return super().execute(inner_step_name, self._second_phase_context(context))
            except Exception as error:  # noqa: BLE001 - the runner's diagnosis would not name the first change
                return self._fail_for_second_phase_exception(context, step_name, error)

    def _fail_for_second_phase_exception(
        self, context: StepContext, step_name: str, error: Exception
    ) -> StepOutcome:
        """Diagnose a second-phase step's exception as the runner would, naming the applied first change.

        The session is rolled back first (the runner does the same), the error is explained by
        `diagnose_exception`, and `_fail` adds what the first change left in place.
        """
        context.session.rollback()
        diagnostic_kind, detail, fix_suggestion = diagnose_exception(
            error, context.target.aws_profile_name, traceback.format_exc()
        )
        outcome = self._fail(
            context,
            step_name,
            diagnostic_kind,
            f"{context.operation.operation_kind} failed at {step_name}",
            detail,
            fix_suggestion,
        )
        return outcome.model_copy(update={"output": {"exception": repr(error)}})

    def _second_phase_context(self, context: StepContext) -> StepContext:
        """The context with services that file the second phase's work under its own step names."""
        services = context.services
        replacements = {
            name: _StepNamePrefixingService(getattr(services, name))
            for name in ("execution_recorder", "aws_sessions", "terraform_runner")
            if getattr(services, name) is not None
        }
        return dataclasses.replace(context, services=dataclasses.replace(services, **replacements))

    def _phase_step_name(self, step_name: str) -> str:
        return _prefixed(step_name) if self.current_phase == SECOND_PHASE else step_name

    # --- plan entries and commits per phase -------------------------------------------------------

    def _phase_plan_key(self, name: str) -> str:
        if self.current_phase == SECOND_PHASE and name in SECOND_PHASE_SCOPED_PLAN_KEYS:
            return second_phase_plan_key(name)
        return name

    def _plan_value(self, context: StepContext, name: str, default: Any = None) -> Any:
        """The plan entry of the running phase."""
        return super()._plan_value(context, self._phase_plan_key(name), default)

    def _store_plan_values(self, context: StepContext, **values: Any) -> None:
        """Write plan entries of the running phase; the second phase's carry the `_phase_2` suffix."""
        super()._store_plan_values(context, **{self._phase_plan_key(name): v for name, v in values.items()})

    def _fail(
        self,
        context: StepContext,
        step_name: str,
        diagnostic_kind: str,
        title: str,
        detail: str,
        fix_suggestion: str,
    ) -> StepOutcome:
        """Fail with the diagnostic filed under the running phase's step name.

        In the second phase the first change is already applied and committed, so the detail and the fix
        suggestion say so (`_first_change_still_applied`).
        """
        if self.current_phase == SECOND_PHASE:
            note = self._first_change_still_applied(context, self._first_phase_commit_phrase(context))
            if note is not None:
                detail_sentence, fix_clause = note
                detail = f"{detail}\n\n{detail_sentence}" if detail else detail_sentence
                fix_suggestion = f"{fix_suggestion}; {fix_clause}" if fix_suggestion else fix_clause
        return super()._fail(
            context, self._phase_step_name(step_name), diagnostic_kind, title, detail, fix_suggestion
        )

    def _first_phase_summary(self, context: StepContext) -> str:
        """The approved first change's summary, from the plan; worked out again for an older plan."""
        first_phase = context.operation.plan.get(FIRST_PHASE_PLAN_KEY) or {}
        if first_phase.get("summary"):
            return str(first_phase["summary"])
        try:
            return self.first_change(context).summary
        except CHANGE_ERRORS:
            return "the first change"

    def _first_phase_commit_phrase(self, context: StepContext) -> str:
        """`committed as <sha>` of the first phase's commit, or `committed` when no step output has it."""
        for commit_step_name in FIRST_PHASE_COMMIT_STEPS:
            output = context.completed_step_output(commit_step_name) or {}
            if output.get("commit_sha"):
                return f"committed as {output['commit_sha']}"
        return "committed"

    def _commit(self, context: StepContext, step_name: str, message_suffix: str = "") -> StepOutcome:
        """Commit as the strategy does; the second phase's message ends with ` (phase 2)`."""
        if self.current_phase == SECOND_PHASE:
            return super()._commit(context, _prefixed(step_name), message_suffix + SECOND_PHASE_COMMIT_SUFFIX)
        return super()._commit(context, step_name, message_suffix)

    def _allows_an_empty_change_set(self, context: StepContext) -> bool:
        """The first change may have nothing to do when `first_phase_may_be_empty`; the second never."""
        return self.first_phase_may_be_empty and self.current_phase == FIRST_PHASE

    # --- the review: refusals first, then what the second change will do --------------------------

    def _review_step(
        self, context: StepContext, step_name: str, run_step: Callable[[StepContext], StepOutcome]
    ) -> StepOutcome:
        """Run a first-phase review step with the refusals before it and the second phase described.

        The second change is described before the first is rendered or computed, so a second change that
        cannot be worked out, or touches a resource with open record drift, fails the review with nothing
        written or committed. The approver sees the downtime of both changes in `plan["downtime"]`, and the
        second change set when the subclass can compute it now (`plan["second_phase"]["change_set"]`).
        """
        refusal = self._check_before_approval(context, step_name)
        if refusal is not None:
            return refusal
        try:
            first_summary = self.first_change(context).summary
            second = self.second_change(context)
            second_touched = sorted(second.touched_addresses(context))
            expected_impact = getattr(second, "expected_impact", None)
            second_impact = expected_impact(context) if expected_impact is not None else None
            review_entries = self._second_phase_review_entries(context, second)
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, step_name, error)
        drift_refusal = self._refuse_open_record_drift(context, step_name, second_touched)
        if drift_refusal is not None:
            return drift_refusal
        second_phase: dict[str, Any] = {"summary": second.summary, "what": second_touched}
        if review_entries is not None:
            second_phase["change_set"] = [entry.model_dump(mode="json") for entry in review_entries]
        self._store_plan_values(
            context,
            **{SECOND_PHASE_PLAN_KEY: second_phase, FIRST_PHASE_PLAN_KEY: {"summary": first_summary}},
        )
        outcome = run_step(context)
        if outcome.status == StepOutcomeStatus.COMPLETED and second_impact is not None:
            first_downtime = str(self._plan_value(context, "downtime", NO_DOWNTIME))
            downtimes = [d for d in (first_downtime, second_impact.downtime) if d and d != NO_DOWNTIME]
            self._store_plan_values(
                context,
                downtime="; ".join(dict.fromkeys(downtimes)) or NO_DOWNTIME,
                requires_reboot=bool(self._plan_value(context, "requires_reboot", False))
                or second_impact.requires_reboot,
            )
        logger.info(
            "chained_change_reviewed",
            operation_id=str(context.operation.id),
            step_name=step_name,
            second_phase_summary=second.summary,
            second_phase_addresses=second_touched,
            outcome=outcome.status.value,
        )
        return outcome

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: the first change's render is the review; the second's is not reviewed again."""
        if self.current_phase == SECOND_PHASE:
            return super()._execute_render_desired(context)
        return self._review_step(context, "render_desired", super()._execute_render_desired)

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Direct mode: the first change set is the review; the second is computed after approval."""
        if self.current_phase == SECOND_PHASE:
            return super()._execute_compute_change_set(context)
        return self._review_step(context, "compute_change_set", super()._execute_compute_change_set)

    def _execute_apply_direct(self, context: StepContext) -> StepOutcome:
        """Nothing to call when the first change set is empty (`first_phase_may_be_empty`)."""
        if self.current_phase == FIRST_PHASE and not self._entries(context):
            return StepOutcome(
                status=StepOutcomeStatus.COMPLETED, output={"submitted": {}, "skipped": "nothing to change"}
            )
        return super()._execute_apply_direct(context)

    def _execute_verify_direct(self, context: StepContext) -> StepOutcome:
        """Nothing to read back when the first change set is empty."""
        if self.current_phase == FIRST_PHASE and not self._entries(context):
            self._store_plan_values(context, pending_reboot_attributes=[])
            return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"live": {}, "pending_reboot": []})
        return super()._execute_verify_direct(context)
