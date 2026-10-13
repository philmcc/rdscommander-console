"""`blue_green_cleanup`: delete a blue/green deployment that a major upgrade left in place (spec 8.7.1).

A major upgrade that fails or is rejected before its switchover finishes leaves its deployment, and the
green copy it made, running in AWS. This operation is the only one that deletes a deployment. Its review
names the deployment and its status as AWS reports them now; once approved (typed by the production
identifier) it calls `DeleteBlueGreenDeployment`, deleting the green side too unless the operator keeps
it, and is settled when AWS no longer knows the deployment. Right before the delete it describes the
deployment again and deletes nothing if a switchover started after the review. Production (the blue side) is
never touched.

Example:
    >>> BlueGreenCleanupExecutor(config_store).operation_kind  # doctest: +SKIP
    'blue_green_cleanup'
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from rdscommander.control_db.models import BlueGreenDeployment
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import VerificationResult
from rdscommander.operations.direct_action import ActionReview, DirectActionExecutor
from rdscommander.operations.executor import StepContext, StepOutcome
from rdscommander.upgrades.blue_green_api import (
    SWITCHOVER_COMPLETED_STATUS,
    SWITCHOVER_IN_PROGRESS_STATUS,
    cluster_member_arns,
    delete_deployment,
    describe_deployment,
    is_deployment_not_found,
    mark_deployment_deleted,
    update_deployment_status,
)
from rdscommander.upgrades.options import BlueGreenCleanupOptions
from rdscommander.upgrades.transient import call_riding_out_transient_errors

logger = get_logger(__name__)

CLEANUP_DOWNTIME = "none; production is not touched"


class BlueGreenCleanupExecutor(DirectActionExecutor):
    """Delete the deployment named in `plan["deployment_id"]`, with its green side unless kept."""

    operation_kind = "blue_green_cleanup"
    not_settled_diagnostic_kind = "blue_green_deployment_still_present"
    not_settled_title = "The blue/green deployment is still there"

    def _options(self, context: StepContext) -> BlueGreenCleanupOptions:
        return BlueGreenCleanupOptions.model_validate(context.operation.plan.get("options", {}))

    def _deployment_id(self, context: StepContext) -> str:
        return str(context.operation.plan["deployment_id"])

    def _record_deleted(self, context: StepContext, deployment_id: str) -> None:
        """Record the deployment as deleted, once."""
        deployment = context.session.get(BlueGreenDeployment, deployment_id)
        if deployment is not None and deployment.deleted_at_utc is None:
            mark_deployment_deleted(context.session, deployment_id)
            context.session.commit()

    def review(self, context: StepContext) -> ActionReview:
        """Name the deployment, its status as AWS reports it now, and what is deleted with it.

        The green side's ARNs are kept as `plan["affected_resource_arns"]`, so change history credits
        their removal to this operation.

        Raises:
            ValueError: The deployment is already deleted, AWS no longer knows it (the console then records
                it as deleted), or it is switching over or switched over.
        """
        deployment_id = self._deployment_id(context)
        deployment = context.session.get(BlueGreenDeployment, deployment_id)
        if deployment is None or deployment.deleted_at_utc is not None:
            raise ValueError(f"blue/green deployment {deployment_id} is already deleted")
        try:
            status = describe_deployment(context, deployment_id, "await_approval")
        except ClientError as error:
            if not is_deployment_not_found(error):
                raise
            self._record_deleted(context, deployment_id)
            raise ValueError(
                f"AWS no longer knows blue/green deployment {deployment_id}; the console now records it as "
                "deleted"
            ) from error
        update_deployment_status(context.session, deployment_id, status.status, status.green_endpoint)
        context.session.commit()
        if status.status in (SWITCHOVER_IN_PROGRESS_STATUS, SWITCHOVER_COMPLETED_STATUS):
            raise ValueError(
                f"blue/green deployment {deployment_id} is {status.status}; production already moved to the "
                "green side, and the kept blue side is listed in the inbox for Decommission"
            )
        green_arns = [status.green_arn] if status.green_arn else []
        if status.green_arn and ":cluster:" in status.green_arn:
            green_arns += cluster_member_arns(context, status.green_arn, "await_approval")
        green_identifiers = ", ".join(arn.rsplit(":", 1)[-1] for arn in green_arns) or "none listed"
        identifier = (context.operation.resource_arn or "").rsplit(":", 1)[-1]
        sentence = f"delete blue/green deployment {deployment_id} ({status.status}) of {identifier}"
        if self._options(context).delete_green:
            sentence += f" and its green side {green_identifiers}; this cannot be undone"
        else:
            sentence += f"; its green side {green_identifiers} is kept as standalone databases"
        context.operation.plan = {**context.operation.plan, "affected_resource_arns": sorted(green_arns)}
        context.session.commit()
        return ActionReview(
            what=[sentence],
            identifiers=[identifier],
            requires_reboot=False,
            downtime=CLEANUP_DOWNTIME,
            reconcile=False,
        )

    def _execute_perform(self, context: StepContext) -> StepOutcome:
        """Describe the deployment again and refuse, deleting nothing, once it is switching over or done.

        The review read the status when the operation paused; the approval may come hours later, and a
        switchover started meanwhile (in the AWS console, say) makes production run on the green side that
        this cleanup would delete. A deployment AWS no longer knows goes on to `perform`, which counts it as
        deleted.
        """
        deployment_id = self._deployment_id(context)
        try:
            status = describe_deployment(context, deployment_id, "perform")
        except ClientError as error:
            if not is_deployment_not_found(error):
                raise
            return super()._execute_perform(context)
        update_deployment_status(context.session, deployment_id, status.status, status.green_endpoint)
        context.session.commit()
        if status.status in (SWITCHOVER_IN_PROGRESS_STATUS, SWITCHOVER_COMPLETED_STATUS):
            return self._fail(
                context,
                "perform",
                "blue_green_deployment_switching_over",
                "The deployment started its switchover after the review",
                f"blue/green deployment {deployment_id} is {status.status} now; production is moving (or has "
                "moved) to its green side, so nothing was deleted",
                "read the deployment in AWS; once its switchover completes, the kept blue side is listed in "
                "the inbox for Decommission, and the deployment needs no cleanup",
            )
        return super()._execute_perform(context)

    def perform(self, context: StepContext) -> dict[str, Any]:
        """`DeleteBlueGreenDeployment`; a deployment AWS no longer knows counts as deleted."""
        deployment_id = self._deployment_id(context)
        delete_green = self._options(context).delete_green
        try:
            delete_deployment(context, deployment_id, delete_green, "perform")
        except ClientError as error:
            if not is_deployment_not_found(error):
                raise
            logger.info(
                "blue_green_cleanup_found_nothing_to_delete",
                operation_id=str(context.operation.id),
                deployment_id=deployment_id,
            )
        return {"deleted_deployment_id": deployment_id, "delete_green": delete_green}

    def settled(self, context: StepContext) -> VerificationResult:
        """AWS no longer knows the deployment; the row then records it as deleted.

        A describe failing for a passing reason (throttling, an expired login, no connection) is asked again
        with backoff for up to `direct_verify_timeout_seconds` before the error ends the step.
        """
        deployment_id = self._deployment_id(context)
        try:
            status, _ = call_riding_out_transient_errors(
                context,
                lambda: describe_deployment(context, deployment_id, "await_settled"),
                step_name="await_settled",
                waited_seconds=0.0,
                limit_seconds=context.settings.direct_verify_timeout_seconds,
                poll_seconds=context.settings.direct_verify_poll_seconds,
            )
        except ClientError as error:
            if not is_deployment_not_found(error):
                raise
            self._record_deleted(context, deployment_id)
            return VerificationResult(ok=True, live={"status": None}, detail="")
        update_deployment_status(context.session, deployment_id, status.status)
        context.session.commit()
        return VerificationResult(
            ok=False,
            live={"status": status.status},
            detail=f"blue/green deployment {deployment_id} is {status.status}",
        )
