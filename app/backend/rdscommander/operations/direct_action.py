"""Operations that are one AWS action in both modes: review, approve, perform, settle, refresh, reconcile.

A reboot, a failover, a snapshot, a promotion or a maintenance action changes nothing the Terraform files
describe, or changes it only as a side effect (a promoted replica becomes a standalone instance). The
executor pauses with a plain-language review, performs the call once approved, polls until the resource
settles, takes an inventory snapshot, and, when the review said so, reconciles the rendered files.

Steps: `await_approval` (computes the review, stored at `plan["action_review"]`, and pauses), `perform`
(the AWS call; its output is stored), `await_settled` (polls `settled` with the direct-verification timeout
and poll interval), `refresh_resource` (a new snapshot, `snapshot_id` in its output, which change history
uses as the attribution window) and, when the review says rendered attributes change, `reconcile`.

Once `perform` has run, every failure marks `plan["applied_without_record"]` so change history still
attributes what AWS did to the operation.

Example:
    >>> class Failover(DirectActionExecutor):  # doctest: +SKIP
    ...     operation_kind = "cluster_failover"
    ...
    ...     def review(self, context):
    ...         return ActionReview(what=["fail over demo-aurora"], identifiers=["demo-aurora"])
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from rdscommander.aws.errors import diagnose_exception
from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import Operation
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.inventory.refresh_executor import refresh_target_now
from rdscommander.lifecycle.reconcile import (
    RECONCILE_STEP_NAME,
    checkout_guard_fix_suggestion,
    reconcile_checkout_report,
    reconcile_touched_addresses,
)
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeDiagnosticError, VerificationResult
from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome

logger = get_logger(__name__)

ACTION_REVIEW_PLAN_KEY = "action_review"
"""Plan key holding the `ActionReview` the approver read; fixed when approval is asked for."""
BASE_STEP_NAMES = ("await_approval", "perform", "await_settled", "refresh_resource")
"""Every action's steps; `reconcile` follows when the review says rendered attributes change."""
__all__ = [
    "ACTION_REVIEW_PLAN_KEY",
    "BASE_STEP_NAMES",
    "RECONCILE_STEP_NAME",
    "ActionReview",
    "DirectActionExecutor",
]


class ActionReview(BaseModel):
    """What the approver reads before an action runs.

    Attributes:
        what: Sentences describing exactly what will happen.
        identifiers: The resources acted on.
        requires_reboot: Whether the action restarts something.
        downtime: What the approver should expect, in words; `none` when nothing is interrupted.
        reconcile: Whether rendered attributes change, so the files are re-rendered and committed after.

    Example:
        >>> ActionReview(what=["reboot demo-orders"], identifiers=["demo-orders"]).downtime
        'none'
    """

    what: list[str]
    identifiers: list[str] = Field(default_factory=list)
    requires_reboot: bool = False
    downtime: str = "none"
    reconcile: bool = False


class DirectActionExecutor:
    """Base for the actions; subclasses implement `review`, `perform`, `settled` and `touched_addresses`.

    Subclasses may also set `not_settled_diagnostic_kind` and `not_settled_title` to name the failure of
    `await_settled` in their own terms.
    """

    operation_kind = ""
    STEP_NAMES = BASE_STEP_NAMES
    not_settled_diagnostic_kind = "action_not_settled"
    not_settled_title = "The action was submitted but the resource did not settle"

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read the live configuration from `config_store` whenever a step needs it."""
        self.config_store = config_store

    # ----- subclass contract -------------------------------------------------------------------------

    def review(self, context: StepContext) -> ActionReview:
        """Compute the review; raise KeyError, ValueError or a ChangeDiagnosticError to refuse."""
        raise NotImplementedError

    def perform(self, context: StepContext) -> dict[str, Any]:
        """Make the AWS call(s); return what was submitted (stored as the step's output)."""
        raise NotImplementedError

    def settled(self, context: StepContext) -> VerificationResult:
        """Whether the resource has settled after the call; polled until ok, terminal or timeout."""
        raise NotImplementedError

    def touched_addresses(self, context: StepContext) -> set[str]:
        """Rendered addresses the action changed; used by reconcile and by history attribution."""
        return set()

    def reconciles_by_default(self, operation: Operation) -> bool:
        """Whether this kind always reconciles (so the step list is stable before the review exists)."""
        return False

    # ----- runner contract ---------------------------------------------------------------------------

    def step_names(self, operation: Operation) -> list[str]:
        """The base steps, plus reconcile when the review (once computed) says rendered attributes change."""
        names = list(BASE_STEP_NAMES)
        review = (operation.plan or {}).get(ACTION_REVIEW_PLAN_KEY) or {}
        if review.get("reconcile") or self.reconciles_by_default(operation):
            names.append(RECONCILE_STEP_NAME)
        return names

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """Approval is satisfied once an operator approved; other steps once they completed."""
        if step_name == "await_approval":
            approved = context.operation.approved_at_utc is not None
            return StepObservation(state=ObservationState.SATISFIED if approved else ObservationState.ABSENT)
        completed = context.step_already_completed(step_name)
        return StepObservation(state=ObservationState.SATISFIED if completed else ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Run `_execute_<step_name>`."""
        return getattr(self, f"_execute_{step_name}")(context)

    # ----- helpers -----------------------------------------------------------------------------------

    def planned_review(self, context: StepContext) -> ActionReview:
        """The review fixed at approval time."""
        return ActionReview.model_validate(context.operation.plan[ACTION_REVIEW_PLAN_KEY])

    def _fail(
        self, context: StepContext, step_name: str, kind: str, title: str, detail: str, fix_suggestion: str
    ) -> StepOutcome:
        """Record a diagnostic (which logs the fix suggestion) and fail the step."""
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name=step_name,
            severity="error",
            diagnostic_kind=kind,
            title=title,
            detail=detail,
            fix_suggestion=fix_suggestion,
        )
        context.session.commit()
        return StepOutcome(
            status=StepOutcomeStatus.FAILED,
            detail=detail,
            fix_suggestion=fix_suggestion,
            diagnostic_recorded=True,
        )

    def _mark_applied_without_record(self, context: StepContext) -> None:
        """Say on the operation that AWS has the action's effect while the record may not describe it."""
        context.operation.plan = {**context.operation.plan, "applied_without_record": True}
        context.session.commit()

    # ----- steps -------------------------------------------------------------------------------------

    def _execute_await_approval(self, context: StepContext) -> StepOutcome:
        """Compute the review, store it for the approver and pause; a refusal fails before any AWS call."""
        try:
            review = self.review(context)
            touched = sorted(self.touched_addresses(context))
        except ChangeDiagnosticError as error:
            return self._fail(
                context,
                "await_approval",
                error.diagnostic_kind,
                error.title,
                str(error),
                error.fix_suggestion or "",
            )
        except (KeyError, ValueError) as error:
            message = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
            return self._fail(
                context,
                "await_approval",
                "action_not_possible",
                "This action cannot run",
                message,
                "refresh inventory, check the resource's state, then create the operation again",
            )
        if review.reconcile:
            # The reconcile step commits only what it wrote, so a workspace that is already dirty (or a
            # checkout behind its upstream) must stop the action now, before AWS is called.
            report = reconcile_checkout_report(context, "await_approval")
            if not report.ok:
                return self._fail(
                    context,
                    "await_approval",
                    "checkout_guard",
                    "Repository checkout is not ready for the reconciled files",
                    report.summary(),
                    checkout_guard_fix_suggestion(report) + "; then create the operation again",
                )
        context.operation.plan = {
            **context.operation.plan,
            ACTION_REVIEW_PLAN_KEY: review.model_dump(mode="json"),
            "requires_reboot": review.requires_reboot,
            "downtime": review.downtime,
            "touched_addresses": touched,
        }
        context.session.commit()
        logger.info(
            "action_ready_for_approval",
            operation_id=str(context.operation.id),
            operation_kind=self.operation_kind,
            identifiers=review.identifiers,
            reconcile=review.reconcile,
        )
        return StepOutcome(status=StepOutcomeStatus.AWAITING_APPROVAL, detail="; ".join(review.what))

    def _execute_perform(self, context: StepContext) -> StepOutcome:
        """Make the approved AWS call; any failure may come after a call AWS accepted, so it is marked."""
        try:
            submitted = self.perform(context)
        except ChangeDiagnosticError as error:
            self._mark_applied_without_record(context)
            return self._fail(
                context, "perform", error.diagnostic_kind, error.title, str(error), error.fix_suggestion or ""
            )
        except Exception:
            self._mark_applied_without_record(context)
            raise
        logger.info(
            "action_performed",
            operation_id=str(context.operation.id),
            operation_kind=self.operation_kind,
            submitted=sorted(submitted),
        )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output=submitted)

    def _execute_await_settled(self, context: StepContext) -> StepOutcome:
        """Poll `settled` until ok, a terminal state, or `direct_verify_timeout_seconds` of waiting."""
        timeout_seconds = context.settings.direct_verify_timeout_seconds
        poll_seconds = context.settings.direct_verify_poll_seconds
        waited_seconds = 0.0
        try:
            result = self.settled(context)
            while not result.ok and not result.terminal and waited_seconds < timeout_seconds:
                logger.info(
                    "action_awaiting_settle",
                    operation_id=str(context.operation.id),
                    waited_seconds=waited_seconds,
                    detail=result.detail,
                )
                context.services.sleep(poll_seconds)
                waited_seconds += poll_seconds
                result = self.settled(context)
        except Exception:
            self._mark_applied_without_record(context)
            raise
        if result.ok:
            return StepOutcome(
                status=StepOutcomeStatus.COMPLETED,
                output={"live": result.live, "waited_seconds": waited_seconds},
            )
        self._mark_applied_without_record(context)
        return self._fail(
            context,
            "await_settled",
            self.not_settled_diagnostic_kind,
            self.not_settled_title,
            result.detail or f"no settled state after {int(waited_seconds)} s",
            "check the resource's status and events in AWS; once it is available, run Refresh inventory and "
            "Detect drift",
        )

    def _execute_refresh_resource(self, context: StepContext) -> StepOutcome:
        """Take a new inventory snapshot so the console sees what the action did."""
        try:
            snapshot = refresh_target_now(context, context.target, "refresh_resource")
        except Exception as error:  # noqa: BLE001 - the action is done; say so and how to see it
            context.session.rollback()
            self._mark_applied_without_record(context)
            _, cause, _ = diagnose_exception(error, context.target.aws_profile_name, "")
            environment_slug = context.target.environment_slug
            return self._fail(
                context,
                "refresh_resource",
                "refresh_after_action_failed",
                "The action is done in AWS, but the inventory could not be refreshed",
                cause,
                f"once AWS can be read again (see the detail), run Refresh inventory on the "
                f"{environment_slug} environment, then run Detect drift",
            )
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"snapshot_id": snapshot.id})

    def _execute_reconcile(self, context: StepContext) -> StepOutcome:
        """Re-render and commit the touched addresses (see `reconcile_touched_addresses`)."""
        return reconcile_touched_addresses(context, set(context.operation.plan.get("touched_addresses", [])))
