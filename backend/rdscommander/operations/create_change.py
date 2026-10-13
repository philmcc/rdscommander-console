"""A desired change that creates one new rendered resource: a replica, a reader, a restore or a create.

`ResourceCreateChange` is the `DesiredChange` every create builds on. Terraform mode adds the new resource's
rendered model to the workspace (the plan is a plain create of one address, which the executor's policy lets
through); direct mode makes the AWS create call and waits until AWS reports the resource `available`. Either
way the new resource's ARN is listed in `plan["created_resource_arns"]`, so change history attributes its
appearance to the operation. A create launched from another resource (a replica from its source, a reader
from its cluster) keeps the operation on that resource; a create with no source links the operation to the
new resource (`link_operation_to_created`).

Example:
    >>> class ReadReplicaCreateExecutor(ResourceCreateExecutor):  # doctest: +SKIP
    ...     operation_kind = "replica_create"
    ...
    ...     def build_change(self, context):
    ...         return ReadReplicaCreate(...)
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from rdscommander.config.models import ResolvedTarget
from rdscommander.control_db.models import Operation
from rdscommander.logging_setup import get_logger
from rdscommander.operations.attribute_change import _comparable_live_value
from rdscommander.operations.change_models import (
    ChangeDiagnosticError,
    ChangeImpact,
    ChangeSetEntry,
    VerificationResult,
)
from rdscommander.operations.change_strategy import (
    CHANGE_ERRORS,
    PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN,
    ResourceChangeExecutor,
)
from rdscommander.operations.enums import StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepOutcome
from rdscommander.parameters.direct_api import (
    TERMINAL_RESOURCE_STATUSES,
    apply_attribute_values,
    read_attribute_values,
)
from rdscommander.terraform.names import terraform_address_for
from rdscommander.terraform.plan_parser import PlanSummary, summarise_plan_json
from rdscommander.terraform.record import RESOURCE_SECTIONS, resource_identifiers
from rdscommander.terraform.render_input import (
    RenderedCluster,
    RenderedClusterInstance,
    RenderedInstance,
    RenderWorkspaceInput,
)
from rdscommander.terraform.state_addresses import ARN_RESOURCE_KINDS
from rdscommander.terraform.workspace_revert import revert_workspace

logger = get_logger(__name__)

CREATED_RESOURCE_ARNS_PLAN_KEY = "created_resource_arns"
"""Plan key listing the resources an operation created; change history attributes their appearance to it."""
CONSOLE_MANAGED_TAGS = {"rdscommander:managed": "true"}
"""The tag every resource the console creates carries (Terraform mode sets it through `default_tags`)."""
NEW_RESOURCE_DOWNTIME = "none (a new resource; nothing existing is touched)"
"""What the approver of a create should expect."""
AVAILABLE_STATUS = "available"
RECORD_AS_BUILT_STEP = "record_as_built"
"""Terraform mode's closing step for a create: record the new resource as the closing refresh read it."""
RECORD_AS_BUILT_COMMIT_SUFFIX = " (record as built)"
"""Added to the operation's commit message for the second commit `record_as_built` makes."""
POST_CREATE_VALUES_APPLIED_PLAN_KEY = "post_create_values_applied"
"""Plan key holding `{attribute: value}` the modify after a create sent (`post_create_attribute_values`)."""
RenderedResource = RenderedInstance | RenderedClusterInstance | RenderedCluster
"""The rendered model of an instance, a cluster member or a cluster."""
__all__ = [
    "CONSOLE_MANAGED_TAGS",
    "CREATED_RESOURCE_ARNS_PLAN_KEY",
    "NEW_RESOURCE_DOWNTIME",
    "PostCreateSettingsNotAppliedError",
    "POST_CREATE_VALUES_APPLIED_PLAN_KEY",
    "RECORD_AS_BUILT_STEP",
    "RenderedResource",
    "ResourceAlreadyExistsError",
    "ResourceCreateChange",
    "ResourceCreateExecutor",
    "expected_arn_of",
    "record_created_resource",
    "sibling_arn_of",
]


class PostCreateSettingsNotAppliedError(ChangeDiagnosticError):
    """The resource was created, but AWS refused the modify that sets what its create call cannot."""

    diagnostic_kind = "post_create_settings_not_applied"
    title = "The new resource was created without some of its settings"


class ResourceAlreadyExistsError(ChangeDiagnosticError):
    """The identifier is already in use in this account and region."""

    diagnostic_kind = "resource_already_exists"
    title = "That identifier already exists"
    fix_suggestion = "choose another identifier, or refresh inventory and adopt the existing resource"


def expected_arn_of(target: ResolvedTarget, resource_type: str, identifier: str) -> str:
    """The ARN AWS gives a resource of `resource_type` named `identifier` in `target`.

    Terraform mode knows the new resource only by its address until the next inventory; its ARN follows
    from the region, the account and the identifier.

    Args:
        target: The target the resource is created in.
        resource_type: `aws_db_instance`, `aws_rds_cluster_instance` or `aws_rds_cluster`.
        identifier: The new resource's identifier.

    Returns:
        The ARN, such as `arn:aws:rds:eu-west-2:000000000000:db:demo-orders-replica-2`.

    Example:
        >>> expected_arn_of(target, "aws_rds_cluster", "demo-aurora")  # doctest: +SKIP
        'arn:aws:rds:eu-west-2:000000000000:cluster:demo-aurora'
    """
    arn_kind = ARN_RESOURCE_KINDS.get(resource_type, resource_type)
    return f"arn:aws:rds:{target.aws_region}:{target.aws_account_id}:{arn_kind}:{identifier}"


def sibling_arn_of(existing_arn: str, resource_type: str, identifier: str) -> str:
    """The ARN of a resource named `identifier` in the same partition, region and account as `existing_arn`.

    A create launched from a resource takes its ARN's account from that resource, which is what AWS (or the
    simulator, whose account may differ from the configured one) reports.

    Args:
        existing_arn: The ARN of a resource in the same place, such as the replica's source.
        resource_type: `aws_db_instance`, `aws_rds_cluster_instance` or `aws_rds_cluster`.
        identifier: The new resource's identifier.

    Returns:
        The new resource's ARN.

    Example:
        >>> sibling_arn_of("arn:aws:rds:eu-west-2:1:db:demo-orders", "aws_db_instance", "demo-orders-2")
        'arn:aws:rds:eu-west-2:1:db:demo-orders-2'
    """
    prefix = ":".join(existing_arn.split(":")[:5])
    return f"{prefix}:{ARN_RESOURCE_KINDS.get(resource_type, resource_type)}:{identifier}"


def record_created_resource(context: StepContext, arn: str, link_operation_to_created: bool) -> None:
    """List `arn` in `plan["created_resource_arns"]`, and point the operation at it when asked.

    Args:
        context: The running step's context; the plan is replaced as a whole and committed.
        arn: ARN of the created resource.
        link_operation_to_created: Set `operation.resource_arn` to `arn` as well (a create with no source).

    Example:
        >>> record_created_resource(context, replica_arn, link_operation_to_created=False)  # doctest: +SKIP
    """
    created = list(context.operation.plan.get(CREATED_RESOURCE_ARNS_PLAN_KEY, []))
    if arn not in created:
        created.append(arn)
    context.operation.plan = {**context.operation.plan, CREATED_RESOURCE_ARNS_PLAN_KEY: created}
    if link_operation_to_created:
        context.operation.resource_arn = arn
    context.session.commit()


class ResourceCreateChange:
    """Base for changes that create a new instance, cluster member or cluster.

    Subclasses implement `rendered`, `create`, `exists` and `status`, and may add `review_entries`. The
    executor calls `prepare` once when it builds the change, so the Terraform overlay
    (`apply_to_render_input`) needs neither the context nor AWS.

    Attributes:
        target_key: The target the resource is created in.
        identifier: The new resource's identifier.
        resource_type: `aws_db_instance`, `aws_rds_cluster_instance` or `aws_rds_cluster`.
        address: Its Terraform address.
        resource_arn: Empty until created.
        summary: What the change does, for titles.
        link_operation_to_created: Whether creating the resource points the operation's `resource_arn` at
            it. A create launched from another resource (a replica from its source) stays on that resource.
        rendered_cached: The rendered model `prepare` computed; None until then.

    Example:
        >>> change = ReadReplicaCreate(...)  # doctest: +SKIP
        >>> change.prepare(context)  # doctest: +SKIP
        >>> change.apply_to_render_input(render_input).instances[-1].identifier  # doctest: +SKIP
        'demo-orders-replica-2'
    """

    def __init__(
        self,
        target_key: str,
        identifier: str,
        resource_type: str,
        summary: str,
        *,
        link_operation_to_created: bool = False,
    ) -> None:
        """Describe the new resource; nothing is created until a strategy runs the change."""
        if resource_type not in RESOURCE_SECTIONS:
            raise ValueError(f"{resource_type} is not a resource type the console renders")
        self.target_key = target_key
        self.identifier = identifier
        self.resource_type = resource_type
        self.address = terraform_address_for(resource_type, identifier)
        self.resource_arn = ""
        self.summary = summary
        self.link_operation_to_created = link_operation_to_created
        self.rendered_cached: RenderedResource | None = None

    # --- what subclasses provide --------------------------------------------------------------------

    def rendered(self, context: StepContext) -> RenderedResource:
        """The rendered model Terraform mode adds to the workspace."""
        raise NotImplementedError

    def create(self, context: StepContext) -> str:
        """Make the AWS create call; return the new resource's ARN."""
        raise NotImplementedError

    def exists(self, context: StepContext, step_name: str = "compute_change_set") -> bool:
        """Whether AWS already has a resource of this type and identifier."""
        raise NotImplementedError

    def status(self, context: StepContext, step_name: str = "verify_direct") -> str | None:
        """The new resource's status as AWS reports it, or None while AWS does not list it."""
        raise NotImplementedError

    def review_entries(self, context: StepContext) -> list[tuple[str, Any]]:
        """`(attribute, value)` pairs shown on the review besides presence (class, storage, version)."""
        return []

    def create_only_fields(self) -> dict[str, Any]:
        """Rendered fields that only say how the resource was created, kept when it is recorded as built.

        AWS does not report them (a restore's `snapshot_identifier`, `restore_to_point_in_time`,
        `manage_master_user_password`), so the record as built takes them from the change instead. A replica
        or a reader has none.
        """
        return {}

    def post_create_attribute_values(self, context: StepContext) -> dict[str, Any]:
        """Rendered settings the create call does not take, sent by a modify once the resource is available.

        Direct mode only (Terraform's provider makes the same modify itself), so both modes build the same
        resource: `{attribute: value}` for `apply_attribute_values` on the new address. A replica or a reader
        has none; a restored instance has, for example, Performance Insights and its maintenance window.
        """
        return {}

    def created_arn(self, context: StepContext) -> str:
        """The new resource's ARN as the create listed it in the plan, else the expected one."""
        created = context.operation.plan.get(CREATED_RESOURCE_ARNS_PLAN_KEY, [])
        return next(
            (arn for arn in created if arn.rsplit(":", 1)[-1] == self.identifier), self.expected_arn(context)
        )

    def expected_arn(self, context: StepContext) -> str:
        """The ARN the new resource will have; Terraform mode lists it before AWS reports it.

        A create launched from another resource overrides this with `sibling_arn_of` that resource.
        """
        return expected_arn_of(context.target, self.resource_type, self.identifier)

    def expected_arns(self, context: StepContext) -> list[str]:
        """Every ARN the change creates: the new resource's, then any created with it (a cluster's writer)."""
        return [self.expected_arn(context)]

    def taken_identifier(self, context: StepContext, step_name: str = "compute_change_set") -> str | None:
        """The identifier of a resource this change would create that AWS already has, or None.

        A change that creates more than one resource (a restored cluster and its writer) names the one taken.
        """
        return self.identifier if self.exists(context, step_name) else None

    # --- the DesiredChange protocol -----------------------------------------------------------------

    def prepare(self, context: StepContext) -> None:
        """Work out the rendered model once, while the context is at hand (see `apply_to_render_input`)."""
        self.rendered_cached = self.rendered(context)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """Only the new resource."""
        return {self.address}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the render input with the new resource added (a copy), its section sorted.

        Raises:
            ValueError: `prepare` was not called first.
        """
        if self.rendered_cached is None:
            raise ValueError(f"the create of {self.identifier} was not prepared; call prepare(context) first")
        updated = render_input.model_copy(deep=True)
        section = getattr(updated, RESOURCE_SECTIONS[self.resource_type])
        section[:] = [resource for resource in section if resource.identifier != self.identifier]
        section.append(self.rendered_cached.model_copy(deep=True))
        section.sort(key=lambda resource: resource.identifier)
        return updated

    def expected_impact(self, context: StepContext) -> ChangeImpact:
        """Nothing existing restarts or stops."""
        return ChangeImpact(requires_reboot=False, downtime=NEW_RESOURCE_DOWNTIME)

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """The new resource's presence, then one entry per notable setting for the review.

        Raises:
            ResourceAlreadyExistsError: AWS already has a resource with this identifier.
        """
        taken = self.taken_identifier(context, "compute_change_set")
        if taken is not None:
            raise ResourceAlreadyExistsError(f"{taken} already exists in this account and region")
        entries = [
            ChangeSetEntry(
                address=self.address,
                attribute="presence",
                current=None,
                desired=f"{self.resource_type} {self.identifier}",
                apply_type="immediate",
                requires_reboot=False,
                downtime=NEW_RESOURCE_DOWNTIME,
            )
        ]
        for attribute, value in self.review_entries(context):
            entries.append(
                ChangeSetEntry(
                    address=self.address,
                    attribute=attribute,
                    current=None,
                    desired=value,
                    apply_type="immediate",
                    requires_reboot=False,
                    downtime="none",
                )
            )
        return entries

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Create the resource and list it among the operation's created resources."""
        self.resource_arn = self.create(context)
        record_created_resource(context, self.resource_arn, self.link_operation_to_created)
        logger.info(
            "resource_create_submitted",
            operation_id=str(getattr(context.operation, "id", "")),
            resource_arn=self.resource_arn,
            resource_type=self.resource_type,
        )
        return {"arn": self.resource_arn}

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """`available` holds; a status AWS never recovers from stops the wait at once.

        The first time the new resource is available, the settings the create call does not take
        (`post_create_attribute_values`) are sent in one modify with `ApplyImmediately`, recorded under
        `verify_direct`. From then on it holds only while the resource is available and every value sent reads
        back as sent (AWS can still report `available` just after the modify); until then the wait goes on.

        Raises:
            PostCreateSettingsNotAppliedError: AWS refused the modify.
        """
        resource_status = self.status(context, "verify_direct")
        if resource_status == AVAILABLE_STATUS:
            sent = self._apply_post_create_values(context)
            if not sent:
                return VerificationResult(ok=True, live={"status": resource_status})
            live = read_attribute_values(
                context, self.address, sorted(sent), "verify_direct", resource_arn=self.created_arn(context)
            )
            mismatched = sorted(
                attribute
                for attribute, value in sent.items()
                if _comparable_live_value(attribute, live.get(attribute)) != value
            )
            if not mismatched:
                return VerificationResult(ok=True, live={"status": resource_status, **live})
            return VerificationResult(
                ok=False,
                live={"status": resource_status, **live},
                detail=(
                    f"{self.identifier} is not yet at the settings sent after its create: "
                    f"{', '.join(mismatched)}"
                ),
            )
        shown_status = resource_status or "not listed by AWS yet"
        return VerificationResult(
            ok=False,
            live={"status": resource_status},
            detail=f"{self.identifier} is {shown_status}",
            terminal=resource_status in TERMINAL_RESOURCE_STATUSES,
        )

    def _apply_post_create_values(self, context: StepContext) -> dict[str, Any]:
        """Send `post_create_attribute_values` once and keep them in the plan; returns what was sent.

        Raises:
            PostCreateSettingsNotAppliedError: AWS refused the modify; the operation is marked
                `applied_without_record`, since the resource exists without the settings.
        """
        if POST_CREATE_VALUES_APPLIED_PLAN_KEY in context.operation.plan:
            return dict(context.operation.plan[POST_CREATE_VALUES_APPLIED_PLAN_KEY])
        values = self.post_create_attribute_values(context)
        if values:
            created_arn = self.created_arn(context)
            try:
                apply_attribute_values(
                    context, self.address, values, step_name="verify_direct", resource_arn=created_arn
                )
            except (ClientError, BotoCoreError) as error:
                context.operation.plan = {**context.operation.plan, "applied_without_record": True}
                context.session.commit()
                fix_suggestion = (
                    f"{self.identifier} was created without {', '.join(sorted(values))}; run Refresh "
                    "inventory, then the Monitoring or Maintenance window change on it"
                )
                logger.error(
                    "post_create_settings_not_applied",
                    operation_id=str(getattr(context.operation, "id", "")),
                    address=self.address,
                    attributes=sorted(values),
                    error=str(error),
                    fix_suggestion=fix_suggestion,
                )
                raise PostCreateSettingsNotAppliedError(
                    f"{self.identifier} was created, but AWS refused the modify that sets "
                    f"{', '.join(sorted(values))}: {error}",
                    fix_suggestion,
                ) from error
            logger.info(
                "post_create_values_applied",
                operation_id=str(getattr(context.operation, "id", "")),
                address=self.address,
                attributes=sorted(values),
            )
        context.operation.plan = {**context.operation.plan, POST_CREATE_VALUES_APPLIED_PLAN_KEY: dict(values)}
        context.session.commit()
        return values


class ResourceCreateExecutor(ResourceChangeExecutor):
    """Executor base for creates: nothing to guard before the resource exists, plain creates allowed.

    Subclasses implement `build_change`; `desired_change` prepares what it builds. Terraform mode refuses an
    identifier AWS already has before anything is rendered (the plan would only fail at apply), and lists
    the new resource's ARN in the plan once the files are rendered.
    """

    allows_create = True
    refuses_open_record_drift = False

    def build_change(self, context: StepContext) -> ResourceCreateChange:
        """The create from `plan["options"]` and the launching record (when any); subclasses override.

        Raises:
            KeyError: The plan names something that does not exist.
            ValueError: The resource cannot be created; the message is shown to the operator.
        """
        raise NotImplementedError

    def desired_change(self, context: StepContext) -> ResourceCreateChange:
        """The prepared create (see `ResourceCreateChange.prepare`)."""
        change = self.build_change(context)
        change.prepare(context)
        return change

    def _guard_resource(self, context: StepContext, step_name: str) -> StepOutcome | None:
        """There is no resource yet, so there is nothing to guard."""
        return None

    def step_names(self, operation: Operation) -> list[str]:
        """Terraform mode adds `record_as_built` after the closing refresh; direct mode needs nothing more.

        Direct mode already records after its closing refresh (`refresh_resource`, then `record_to_checkout`),
        so its record is the resource as AWS built it.
        """
        steps = super().step_names(operation)
        if "commit_to_checkout" in steps:
            steps.insert(steps.index("refresh_resource") + 1, RECORD_AS_BUILT_STEP)
        return steps

    def _execute_record_as_built(self, context: StepContext) -> StepOutcome:
        """Record the created resources as the closing refresh read them, prove it plans clean, commit again.

        The first commit recorded the desired render, which leaves to AWS what the files do not set (a
        replica's backup retention and port, a reader's default parameter group); recorded that way, the next
        Detect drift would report them. This step renders the created addresses from the latest snapshot,
        keeps the change's `create_only_fields`, writes the files and the record, and requires a clean plan
        before committing with the suffix ` (record as built)`. Nothing here changes AWS and the first commit
        stands: a plan that is not clean puts the workspace back as `HEAD` has it and fails the step. When the
        closing refresh did not run, or does not show the new resource yet, the step completes without
        recording and says why.
        """
        step_name = RECORD_AS_BUILT_STEP
        refresh_output = context.completed_step_output("refresh_resource") or {}
        if not refresh_output.get("refreshed"):
            return self._skip_record_as_built(context, "the closing refresh did not read AWS")
        try:
            change = self.desired_change(context)
            snapshot = self._latest_snapshot(context)
            created = set(self._plan_value(context, "touched_addresses", []))
            built = self._build_input(context, snapshot, include_all=False)
            missing = sorted(created - set(resource_identifiers(built)))
            if missing:
                return self._skip_record_as_built(
                    context, f"the latest inventory does not show {', '.join(missing)} yet"
                )
            built = self._with_create_only_fields(built, change)
            record_input = self._with_touched_replaced(self._recorded_input(context), created, built)
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, step_name, error)
        workspace = self._workspace(context)
        try:
            self._write_files_and_record(context, record_input, snapshot.id)
            run = context.services.terraform_runner.plan(
                self._workspace_dir(context),
                context.target,
                context.settings,
                operation_id=context.operation.id,
                step_name=step_name,
            )
        except Exception:
            revert_workspace(context, workspace, step_name)
            raise
        summary = summarise_plan_json(run.plan_json) if run.plan_json else PlanSummary()
        self._record_terraform_run(
            context, "verify_plan", run.result.exit_code, run.result.record_id, run.plan_json or None, summary
        )
        if run.result.exit_code not in PLAN_EXIT_CODES_THAT_PRODUCED_A_PLAN:
            return self._fail_and_revert(
                context,
                step_name,
                "terraform_plan_failed",
                "Planning the record as built failed",
                run.result.standard_error[-2000:],
                "the created resource and the first commit stand; open the execution record, then run Detect "
                "drift and resolve it",
            )
        if run.result.exit_code != 0 or run.has_changes:
            residual_changes = [
                f"{resource_change.address}: {', '.join(resource_change.actions)} on "
                f"{', '.join(resource_change.changed_attribute_names) or 'unknown attributes'}"
                for resource_change in summary.resource_changes
                if resource_change.actions != ["no-op"]
            ]
            return self._fail_and_revert(
                context,
                step_name,
                "record_as_built_not_clean",
                "The record as built does not plan clean",
                "\n".join(residual_changes) or "the plan proposes changes",
                "the created resource and the first commit stand; run Detect drift and resolve it",
            )
        return self._commit(context, step_name, message_suffix=RECORD_AS_BUILT_COMMIT_SUFFIX)

    def _with_create_only_fields(
        self, built: RenderWorkspaceInput, change: ResourceCreateChange
    ) -> RenderWorkspaceInput:
        """`built` with the change's create-only fields set on the created resource (a copy)."""
        fields = change.create_only_fields()
        if not fields:
            return built
        updated = built.model_copy(deep=True)
        section = getattr(updated, RESOURCE_SECTIONS[change.resource_type])
        section[:] = [
            resource.model_copy(update=fields) if resource.identifier == change.identifier else resource
            for resource in section
        ]
        return updated

    def _skip_record_as_built(self, context: StepContext, reason: str) -> StepOutcome:
        """Complete without a second commit: the first commit's record stands until Detect drift runs."""
        fix_suggestion = (
            "run Refresh inventory, then Detect drift, and accept what AWS reports for the new resource"
        )
        logger.warning(
            "record_as_built_skipped",
            operation_id=str(context.operation.id),
            reason=reason,
            fix_suggestion=fix_suggestion,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            detail=f"not recorded as built: {reason}; {fix_suggestion}",
            output={"recorded_as_built": False, "reason": reason},
        )

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Refuse an identifier AWS already has, render, then list the new resources' ARNs in the plan."""
        try:
            change = self.build_change(context)
            taken = change.taken_identifier(context, "render_desired")
            if taken is not None:
                raise ResourceAlreadyExistsError(f"{taken} already exists in this account and region")
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, "render_desired", error)
        outcome = super()._execute_render_desired(context)
        if outcome.status == StepOutcomeStatus.COMPLETED:
            for position, arn in enumerate(change.expected_arns(context)):
                record_created_resource(context, arn, change.link_operation_to_created and position == 0)
        return outcome

    def _overlay_recorded_input(
        self, context: StepContext, fresh: RenderWorkspaceInput
    ) -> RenderWorkspaceInput:
        """Direct mode records the created resource from the inventory, keeping its create-only fields.

        AWS does not report how a resource was created (the snapshot a restore read, its point in time), so
        the record takes those fields from the change, as Terraform mode's `record_as_built` does.
        """
        return self._with_create_only_fields(fresh, self.build_change(context))
