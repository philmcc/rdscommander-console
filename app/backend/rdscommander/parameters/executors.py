"""Executors for parameter group operations, and the reboot rule every reboot the console runs follows.

The four group operations (`parameter_group_create`, `parameter_group_edit`, `parameter_group_attach`,
`parameter_group_delete`) and the upgrade build (`parameter_group_build_for_upgrade`) are
`ResourceChangeExecutor`s: they say what they want as a `DesiredChange` and the base runs it through
Terraform or the RDS API, whichever the environment uses. `reboot_instance` is a direct action and lives in
`rdscommander.lifecycle.actions`; `refuse_foreign_owned_reboot` stays here because the attach's reboot uses
it too.

Example:
    >>> executors = {  # doctest: +SKIP
    ...     ParameterGroupEditExecutor.operation_kind: ParameterGroupEditExecutor(config_store),
    ... }
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from rdscommander.catalogue.options import (
    ParameterGroupAttachOptions,
    ParameterGroupCreateOptions,
    ParameterGroupEditOptions,
    UpgradeGroupBuildOptions,
)
from rdscommander.control_db.models import Operation, ParameterGroupProvenance
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeDiagnosticError, DesiredChange
from rdscommander.operations.change_strategy import (
    CHANGE_ERRORS,
    FOREIGN_OWNERSHIP_PREFIX,
    ResourceChangeExecutor,
)
from rdscommander.operations.enums import ObservationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.parameters import direct_api
from rdscommander.parameters.changes import (
    ParameterGroupAlreadyExists,
    ParameterGroupAttach,
    ParameterGroupCreate,
    ParameterGroupDelete,
    ParameterGroupEdit,
    ParameterValidationError,
    clone_parameters,
    create_problems,
    family_metadata_for,
    group_name_is_taken,
    parameter_problems,
    reader_for,
)
from rdscommander.parameters.direct_api import TERMINAL_RESOURCE_STATUSES
from rdscommander.parameters.metadata import FamilyMetadata, ValidationProblem
from rdscommander.parameters.upgrade_builder import (
    PlannedGroup,
    UpgradeGroupPlan,
    UpgradeParameterGroups,
    UpgradePlanError,
    build_upgrade_plan,
    final_parameters,
    record_group_provenance,
    was_built_from_same_source,
)
from rdscommander.resources.model import ResourceKind
from rdscommander.resources.queries import load_resource

logger = get_logger(__name__)

UPGRADE_PLANS_PLAN_KEY = "upgrade_plans"
"""Plan key holding the groups an upgrade build creates, worked out by its `build_plan` step."""
TERMINAL_STATUS_NOTE = " (a status the instance will not recover from by waiting)"


def _apply_methods(metadata: FamilyMetadata, names: list[str]) -> dict[str, str]:
    """The apply method the family gives each of `names` that it knows."""
    return {
        name: metadata.parameters[name].apply_method_default for name in names if name in metadata.parameters
    }


class _GroupCreatingExecutor(ResourceChangeExecutor):
    """Base of the executors that create parameter groups: there is no resource to guard, and a name is free.

    A new group is not in the inventory yet, so the resource guard has nothing to look at. What can go wrong
    instead is the name: a group of that scope and name may exist already, in the inventory or (Terraform
    mode) in the committed record that the inventory no longer shows.
    """

    allows_create = True

    def _guard_resource(self, context: StepContext, step_name: str) -> StepOutcome | None:
        """There is no resource yet, so there is nothing to guard."""
        return None

    def _where_group_name_is_taken(
        self, context: StepContext, target_key: str, scope: str, name: str
    ) -> str | None:
        """Where a group of this scope and name exists already, or None when the name is free.

        Args:
            context: The running step's context.
            target_key: The target the group would be created in.
            scope: `instance` or `cluster`.
            name: The proposed name.

        Returns:
            `the inventory of <target>`, or `the record of <target>` (the committed Terraform record, which
            can hold a group the inventory no longer shows), or None.
        """
        if group_name_is_taken(context.session, context.console_config, target_key, scope, name):
            return f"the inventory of {target_key}"
        recorded = self._recorded_input(context)
        if recorded is None:
            return None
        groups = (
            recorded.cluster_parameter_groups if scope == "cluster" else recorded.instance_parameter_groups
        )
        return f"the record of {target_key}" if any(group.name == name for group in groups) else None

    def _ensure_group_name_is_free(
        self, context: StepContext, target_key: str, scope: str, name: str
    ) -> None:
        """Refuse a name that the inventory or the record already has, before anything is written.

        Raises:
            ParameterGroupAlreadyExists: A group of that scope and name is in the inventory or the record.
        """
        where = self._where_group_name_is_taken(context, target_key, scope, name)
        if where is not None:
            raise ParameterGroupAlreadyExists(f"the {scope} parameter group {name} is already in {where}")


class ParameterGroupCreateExecutor(_GroupCreatingExecutor):
    """Create a parameter group from the family defaults or by cloning another group's overrides."""

    operation_kind = "parameter_group_create"

    def desired_change(self, context: StepContext) -> ParameterGroupCreate:
        """The new group with every override, a clone's included.

        A clone of a group of another family keeps the parameters the new family has and drops the rest
        (the executor warns about them); the values that are carried across families are validated like the
        ones the operator typed.

        Raises:
            ParameterValidationError: A value or name cannot be set.
            KeyError: The group to clone is not in the latest inventory.
            ValueError: The group to clone is of the other scope, or none was named.
        """
        options = ParameterGroupCreateOptions.model_validate(context.operation.plan["options"])
        metadata = family_metadata_for(context, options.family, options.scope)
        cloned = None
        if options.source == "clone":
            if not options.clone_from_arn:
                raise ValueError("cloning needs the group to clone (clone_from_arn)")
            cloned = clone_parameters(
                context.session,
                context.console_config,
                options.clone_from_arn,
                options.scope,
                options.family,
                metadata,
            )
        problems = create_problems(metadata, options.parameters, cloned)
        if problems:
            raise ParameterValidationError(problems)
        overrides = {**(cloned.parameters if cloned else {}), **options.parameters}
        return ParameterGroupCreate(
            options.target_key,
            options.name,
            options.scope,
            options.family,
            options.description,
            overrides,
            apply_methods=_apply_methods(metadata, list(overrides)),
            dropped_parameters=cloned.dropped if cloned else [],
            clone_source=cloned.source_name if cloned else None,
        )

    def _ensure_name_is_free(self, context: StepContext) -> None:
        """Refuse the operation's name when the inventory or the record already has it.

        Raises:
            ParameterGroupAlreadyExists: A group of that scope and name is in the inventory or the record.
        """
        options = ParameterGroupCreateOptions.model_validate(context.operation.plan["options"])
        self._ensure_group_name_is_free(context, options.target_key, options.scope, options.name)

    def _warn_about_dropped_parameters(self, context: StepContext, step_name: str) -> None:
        """Record a warning naming the parameters a cross-family clone could not carry."""
        change = self.desired_change(context)
        if not change.dropped_parameters:
            return
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name=step_name,
            severity="warning",
            diagnostic_kind="clone_parameters_dropped",
            title="Some parameters of the cloned group do not exist in the new family",
            detail=(
                f"{change.clone_source} sets {', '.join(change.dropped_parameters)}, which {change.family} "
                f"does not have, so {change.name} starts without them"
            ),
            fix_suggestion=(
                "review the dropped parameters; if the new family replaced one with another parameter, set "
                "that one on the new group after it exists"
            ),
        )
        context.session.commit()

    def _guarded(
        self, context: StepContext, step_name: str, run: Callable[[StepContext], StepOutcome]
    ) -> StepOutcome:
        """`run` after the name check; a completed step warns about dropped parameters."""
        try:
            self._ensure_name_is_free(context)
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, step_name, error)
        outcome = run(context)
        if outcome.status == StepOutcomeStatus.COMPLETED:
            self._warn_about_dropped_parameters(context, step_name)
        return outcome

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: refuse an existing name before any file is rendered."""
        return self._guarded(context, "render_desired", super()._execute_render_desired)

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Direct mode: refuse an existing name before the change set is shown."""
        return self._guarded(context, "compute_change_set", super()._execute_compute_change_set)


class UpgradeGroupBuilderExecutor(_GroupCreatingExecutor):
    """Build the parameter groups of an upgrade: the instance group and, for a cluster, the cluster group.

    `build_plan` works out the groups from the current ones (see `upgrade_builder`), settles which of them
    exist already from an earlier build, and validates the whole request before anything is touched. The
    groups to create then go through the environment's strategy as one change (`UpgradeParameterGroups`): one
    change set or Terraform plan for the approver to review, one approval, one commit. `record_provenance`
    closes the operation (direct mode wrote each row as its group was created).
    """

    operation_kind = "parameter_group_build_for_upgrade"

    def _options(self, operation: Operation) -> UpgradeGroupBuildOptions:
        return UpgradeGroupBuildOptions.model_validate(operation.plan["options"])

    def _plans(self, operation: Operation) -> list[UpgradeGroupPlan]:
        """The groups `build_plan` worked out; empty before it ran."""
        return [
            UpgradeGroupPlan.model_validate(plan) for plan in operation.plan.get(UPGRADE_PLANS_PLAN_KEY, [])
        ]

    def _problems_in(
        self, metadata: FamilyMetadata, plan: UpgradeGroupPlan, options: UpgradeGroupBuildOptions
    ) -> list[ValidationProblem]:
        """Everything that stops the new group from holding its parameters in the target family."""
        return [
            ValidationProblem(name=problem.name, message=f"{plan.new_group_name}: {problem.message}")
            for problem in parameter_problems(metadata, final_parameters(plan, options.overrides))
        ]

    # --- steps -------------------------------------------------------------------------------------

    def step_names(self, operation: Operation) -> list[str]:
        """The strategy's steps, with `build_plan` after the checkout guard and `record_provenance` last."""
        strategy = super().step_names(operation)
        return [strategy[0], "build_plan", *strategy[1:], "record_provenance"]

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """`build_plan` and `record_provenance` stay complete once done; the rest is the strategy's."""
        if step_name in ("build_plan", "record_provenance"):
            completed = context.step_already_completed(step_name)
            return StepObservation(state=ObservationState.SATISFIED if completed else ObservationState.ABSENT)
        return super().observe(step_name, context)

    def desired_change(self, context: StepContext) -> UpgradeParameterGroups:
        """The creation of every group of the build; groups that exist from an earlier build are left out.

        Raises:
            ParameterValidationError: A value or name cannot be set in the target family.
            KeyError: The operation has no groups yet (`build_plan` has not run).
        """
        options = self._options(context.operation)
        plans = self._plans(context.operation)
        if not plans:
            raise KeyError("the groups of the build have not been worked out yet")
        groups = []
        problems: list[ValidationProblem] = []
        for plan in plans:
            metadata = family_metadata_for(context, plan.target_family, plan.scope)
            if not plan.already_built:
                problems += self._problems_in(metadata, plan, options)
            parameters = final_parameters(plan, options.overrides)
            groups.append(
                PlannedGroup(
                    plan,
                    ParameterGroupCreate(
                        context.target.target_key,
                        plan.new_group_name,
                        plan.scope,
                        plan.target_family,
                        f"{plan.source_group_name} carried to {plan.target_family}",
                        parameters,
                        apply_methods=_apply_methods(metadata, list(parameters)),
                        link_operation_to_group=False,
                    ),
                )
            )
        if problems:
            raise ParameterValidationError(problems)
        return UpgradeParameterGroups(context.operation.resource_arn or "", groups)

    def _refuse_taken_names_then(
        self, context: StepContext, step_name: str, run: Callable[[StepContext], StepOutcome]
    ) -> StepOutcome:
        """`run` after checking that the name of every group to create is still free."""
        try:
            for plan in self._plans(context.operation):
                if not plan.already_built:
                    self._ensure_group_name_is_free(
                        context, context.target.target_key, plan.scope, plan.new_group_name
                    )
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, step_name, error)
        return run(context)

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: refuse an existing name before any file is rendered."""
        return self._refuse_taken_names_then(context, "render_desired", super()._execute_render_desired)

    def _execute_compute_change_set(self, context: StepContext) -> StepOutcome:
        """Direct mode: refuse an existing name before the change set is shown."""
        return self._refuse_taken_names_then(
            context, "compute_change_set", super()._execute_compute_change_set
        )

    def _execute_apply_direct(self, context: StepContext) -> StepOutcome:
        """Create the groups; when some exist and the step failed, say the record does not describe them."""
        try:
            outcome = super()._execute_apply_direct(context)
        except Exception:
            self._note_groups_without_record(context)
            raise
        if outcome.status != StepOutcomeStatus.COMPLETED:
            self._note_groups_without_record(context)
        return outcome

    def _note_groups_without_record(self, context: StepContext) -> None:
        """Mark the operation as applied without a record when this operation created any group."""
        created = context.session.execute(
            select(ParameterGroupProvenance.id).where(
                ParameterGroupProvenance.operation_id == context.operation.id
            )
        ).first()
        if created is not None:
            self._mark_applied_without_record(context)

    # --- build_plan ------------------------------------------------------------------------------

    def _settle_existing_groups(
        self, context: StepContext, plans: list[UpgradeGroupPlan]
    ) -> list[UpgradeGroupPlan]:
        """Mark the groups that exist from an earlier build of the same groups; refuse any other that exists.

        Existence is looked up in the inventory, in the committed record and, in direct mode, in AWS itself
        (a build that failed part way did not refresh the inventory).

        Raises:
            ParameterGroupAlreadyExists: A group exists that no earlier build created from this source group
                for this family.
        """
        settled: list[UpgradeGroupPlan] = []
        target_key = context.target.target_key
        for plan in plans:
            where = self._where_group_name_is_taken(context, target_key, plan.scope, plan.new_group_name)
            if where is None and context.target.is_direct:
                live = direct_api.group_exists(context, plan.new_group_name, plan.scope, "build_plan")
                where = f"AWS ({target_key})" if live else None
            if where is None:
                settled.append(plan.model_copy(update={"already_built": False}))
            elif was_built_from_same_source(
                context.session,
                target_key,
                plan.scope,
                plan.new_group_name,
                plan.target_family,
                plan.source_group_name,
            ):
                settled.append(plan.model_copy(update={"already_built": True}))
            else:
                raise ParameterGroupAlreadyExists(
                    f"the {plan.scope} parameter group {plan.new_group_name} is already in {where}"
                )
        return settled

    def _ensure_request_can_be_built(
        self, context: StepContext, plans: list[UpgradeGroupPlan], options: UpgradeGroupBuildOptions
    ) -> None:
        """Everything that can be known before the first group is created, so that no group is built in vain.

        Edits for a group that exists already are not applied (it is left as the earlier build made it).

        Raises:
            ValueError: The operator's edits name a group this operation does not build.
            UpgradePlanError: Every group exists already.
            ParameterValidationError: A value or name cannot be set in the target family.
        """
        stray = sorted(set(options.overrides) - {plan.scope for plan in plans})
        if stray:
            raise ValueError(
                f"the edits name the {', '.join(stray)} parameter group, which this operation does not build"
            )
        to_create = [plan for plan in plans if not plan.already_built]
        if not to_create:
            raise UpgradePlanError(
                "every parameter group of this build exists already: "
                + ", ".join(plan.new_group_name for plan in plans),
                "nothing to do; attach the groups to the instance or cluster when it is upgraded",
            )
        problems = [
            problem
            for plan in to_create
            for problem in self._problems_in(
                family_metadata_for(context, plan.target_family, plan.scope), plan, options
            )
        ]
        if problems:
            raise ParameterValidationError(problems)

    def _execute_build_plan(self, context: StepContext) -> StepOutcome:
        """Work out the groups from the current ones and keep them in the plan for review."""
        options = self._options(context.operation)
        try:
            record = load_resource(
                context.session, context.console_config, context.operation.resource_arn or ""
            )
            if record is None:
                raise KeyError(f"{context.operation.resource_arn} is not in the latest inventory")
            plans = build_upgrade_plan(
                context.session,
                context.console_config,
                record,
                options.target_engine_version,
                name_suffix=options.name_suffix,
                reader=reader_for(context, "build_plan"),
            )
            context.session.commit()  # the family metadata may have been cached
            plans = self._settle_existing_groups(context, plans)
            self._ensure_request_can_be_built(context, plans, options)
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, "build_plan", error)
        context.operation.plan = {
            **context.operation.plan,
            UPGRADE_PLANS_PLAN_KEY: [plan.model_dump(mode="json") for plan in plans],
        }
        context.session.commit()
        to_create = [plan.new_group_name for plan in plans if not plan.already_built]
        existing = [plan.new_group_name for plan in plans if plan.already_built]
        logger.info(
            "upgrade_groups_planned",
            operation_id=str(context.operation.id),
            groups_to_create=to_create,
            groups_from_an_earlier_build=existing,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"groups_to_create": to_create, "groups_from_an_earlier_build": existing},
        )

    def _execute_record_provenance(self, context: StepContext) -> StepOutcome:
        """Record the provenance of each group this operation created that has no row yet.

        Direct mode wrote each row as its group was created, so nothing is left to add; Terraform mode writes
        them all here, after the commit. A group from an earlier build keeps that build's row.
        """
        options = self._options(context.operation)
        added: list[str] = []
        for plan in self._plans(context.operation):
            if plan.already_built:
                continue
            if record_group_provenance(
                context.session,
                context.operation.id,
                context.target.target_key,
                plan,
                final_parameters(plan, options.overrides),
            ):
                added.append(plan.new_group_name)
        context.session.commit()
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"recorded": added})


class ParameterGroupEditExecutor(ResourceChangeExecutor):
    """Set and reset parameters of an existing group, validated before anyone is asked to approve."""

    operation_kind = "parameter_group_edit"

    def desired_change(self, context: StepContext) -> DesiredChange:
        """The edit, once every value has been validated against the group's family.

        Raises:
            ParameterValidationError: A value or name cannot be set or reset.
            KeyError: The group is not in the latest inventory.
            ValueError: The resource is not a parameter group.
        """
        options = ParameterGroupEditOptions.model_validate(context.operation.plan["options"])
        record = load_resource(context.session, context.console_config, context.operation.resource_arn or "")
        if record is None:
            raise KeyError(f"{context.operation.resource_arn} is not in the latest inventory")
        if record.kind != ResourceKind.PARAMETER_GROUP:
            raise ValueError(f"{record.identifier} is not a parameter group")
        metadata = family_metadata_for(
            context, record.facts["parameter_group_family"], record.facts["group_scope"]
        )
        problems = parameter_problems(metadata, options.parameters, options.reset)
        if problems:
            raise ParameterValidationError(problems)
        return ParameterGroupEdit(
            record.arn,
            options.parameters,
            options.reset,
            apply_methods=_apply_methods(metadata, list(options.parameters)),
        )


class ParameterGroupDeleteExecutor(ResourceChangeExecutor):
    """Delete a parameter group nothing uses; a group in use is refused in both modes."""

    operation_kind = "parameter_group_delete"
    allows_delete = True

    def desired_change(self, context: StepContext) -> ParameterGroupDelete:
        """The delete of the operation's group."""
        return ParameterGroupDelete(context.operation.resource_arn or "")

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Refuse a group in use before any file is rendered.

        Direct mode finds out in its change set; Terraform mode never computes one, and a plan that deletes
        a group in use would only fail at the apply, so the check runs here first.
        """
        try:
            self.desired_change(context).ensure_unused(context)
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, "render_desired", error)
        return super()._execute_render_desired(context)


def _wait_until_available(context: StepContext, identifiers: list[str], step_name: str) -> dict[str, str]:
    """Poll until every instance reports `available`, one reaches a terminal status, or the wait is used up.

    A terminal status (`incompatible-parameters`, `failed`, `storage-full`, ...; see
    `TERMINAL_RESOURCE_STATUSES`) never turns into `available` by waiting, so the wait stops there instead
    of using up `direct_verify_timeout_seconds`.

    Args:
        context: The running step's context.
        identifiers: The instances to wait for.
        step_name: Step the AWS calls are recorded under.

    Returns:
        `{identifier: status}` of the instances that are still not available; empty when all are.
    """
    deadline = datetime.now(UTC) + timedelta(seconds=context.settings.direct_verify_timeout_seconds)
    statuses = {i: direct_api.instance_status(context, i, step_name) for i in identifiers}
    while (
        any(status != "available" for status in statuses.values())
        and not any(status in TERMINAL_RESOURCE_STATUSES for status in statuses.values())
        and datetime.now(UTC) < deadline
    ):
        context.services.sleep(context.settings.direct_verify_poll_seconds)
        statuses = {i: direct_api.instance_status(context, i, step_name) for i in identifiers}
    return {i: status for i, status in statuses.items() if status != "available"}


def describe_instance_statuses(statuses: dict[str, str]) -> str:
    """`<identifier> is <status>` per instance; a terminal status says that waiting will not clear it.

    Args:
        statuses: `{identifier: status}` of the instances to describe.

    Returns:
        One comma-separated sentence; empty when `statuses` is empty.

    Example:
        >>> describe_instance_statuses({"demo-orders": "rebooting"})
        'demo-orders is rebooting'
    """
    return ", ".join(
        f"{identifier} is {status}" + (TERMINAL_STATUS_NOTE if status in TERMINAL_RESOURCE_STATUSES else "")
        for identifier, status in statuses.items()
    )


class ParameterGroupAttachExecutor(ResourceChangeExecutor):
    """Point an instance or cluster at another group, optionally rebooting to apply static parameters."""

    operation_kind = "parameter_group_attach"

    def _options(self, operation: Operation) -> ParameterGroupAttachOptions:
        return ParameterGroupAttachOptions.model_validate(operation.plan["options"])

    def desired_change(self, context: StepContext) -> ParameterGroupAttach:
        """The attachment named by the operation's options."""
        options = self._options(context.operation)
        return ParameterGroupAttach(
            context.operation.resource_arn or "", options.parameter_group_arn, options.reboot
        )

    def step_names(self, operation: Operation) -> list[str]:
        """The strategy's steps; `reboot == "now"` adds the reboot and the wait.

        Terraform mode commits the files first and reboots afterwards, so a reboot that goes wrong cannot
        lose the commit; its closing refresh comes after the reboot, so the inventory sees the instances
        rebooted. Direct mode reboots before the record step, because the record is the last thing it
        writes.
        """
        base = super().step_names(operation)
        if self._options(operation).reboot != "now":
            return base
        if "commit_to_checkout" in base:
            index = base.index("commit_to_checkout") + 1
            return [*base[:index], "reboot_instances", "await_in_sync", *base[index:]]
        index = base.index("record_to_checkout")
        return [*base[:index], "reboot_instances", "await_in_sync", *base[index:]]

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """The reboot steps are complete once they completed; everything else is the strategy's."""
        if step_name in ("reboot_instances", "await_in_sync"):
            completed = context.step_already_completed(step_name)
            return StepObservation(state=ObservationState.SATISFIED if completed else ObservationState.ABSENT)
        return super().observe(step_name, context)

    def _guard_resource(self, context: StepContext, step_name: str) -> StepOutcome | None:
        """The change gate for the instance or cluster, then, when it reboots, the reboot's rule per member.

        A cluster attach with `reboot: now` reboots every member instance, so each member gets the
        foreign-ownership refusal a reboot gets (lifted by `allow_direct_changes_to_foreign_owned`). It runs
        in the first step, long before approval.
        """
        refusal = super()._guard_resource(context, step_name)
        if refusal is not None or self._options(context.operation).reboot != "now":
            return refusal
        record = load_resource(context.session, context.console_config, context.operation.resource_arn or "")
        if record is None or record.kind != ResourceKind.CLUSTER:
            return None
        try:
            refuse_foreign_owned_reboot(context, record.facts.get("member_instance_ownership", {}))
        except ForeignOwnedRebootRefused as error:
            return self._fail(
                context,
                step_name,
                error.diagnostic_kind,
                error.title,
                f"Attaching with a reboot reboots every member of {record.identifier}: {error}",
                "attach without a reboot and reboot those members in the tooling that owns them, or allow "
                "direct changes to foreign-owned resources in this environment's configuration",
            )
        return None

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: refuse a group that cannot be attached before any file is rendered."""
        try:
            self.desired_change(context).ensure_attachable(context)
        except CHANGE_ERRORS as error:
            return self._fail_for_change_error(context, "render_desired", error)
        return super()._execute_render_desired(context)

    def _members(self, context: StepContext) -> list[str]:
        """The instance itself, or every member instance of the cluster."""
        record = load_resource(context.session, context.console_config, context.operation.resource_arn or "")
        if record is None:
            return []
        return list(record.relationships.get("member_identifiers", [])) or [record.identifier]

    def _recovery_advice(self, context: StepContext) -> str:
        """What to do when the reboot did not happen or did not finish, by mode."""
        if context.target.is_direct:
            return (
                "the group is attached but the record was not committed; refresh inventory, then reboot "
                "from the instance page"
            )
        return (
            "the group is attached and the files are committed; reboot from the instance page once the "
            "instances are available"
        )

    def _execute_reboot_instances(self, context: StepContext) -> StepOutcome:
        """Wait for each instance to be `available`, then reboot the instance or every cluster member.

        RDS refuses to reboot an instance that is still modifying, which is what the attachment has just
        made it, so the reboot waits for the instance to settle first.
        """
        members = self._members(context)
        not_ready = _wait_until_available(context, members, "reboot_instances")
        if not_ready:
            if context.target.is_direct:
                self._mark_applied_without_record(context)
            return self._fail(
                context,
                "reboot_instances",
                "instance_not_ready_for_reboot",
                "Instances did not become available before the reboot",
                describe_instance_statuses(not_ready),
                self._recovery_advice(context),
            )
        try:
            for identifier in members:
                direct_api.reboot_instance(context, identifier, False, "reboot_instances")
        except Exception:
            if context.target.is_direct:
                self._mark_applied_without_record(context)
            raise
        return StepOutcome(status=StepOutcomeStatus.COMPLETED, output={"rebooted": members})

    def _execute_await_in_sync(self, context: StepContext) -> StepOutcome:
        """Wait until every rebooted instance is available and reports its parameters in sync.

        The wait stops as soon as an instance reaches a terminal status, which waiting never clears.
        """
        deadline = datetime.now(UTC) + timedelta(seconds=context.settings.direct_verify_timeout_seconds)
        pending = self._members(context)
        terminal: dict[str, str] = {}
        while pending and not terminal and datetime.now(UTC) < deadline:
            still_pending: list[str] = []
            for identifier in pending:
                status = direct_api.instance_status(context, identifier, "await_in_sync")
                if status in TERMINAL_RESOURCE_STATUSES:
                    terminal[identifier] = status
                elif status != "available" or direct_api.attached_group(
                    context, identifier, False, "await_in_sync"
                )[1] not in (None, "in-sync"):
                    still_pending.append(identifier)
            pending = still_pending
            if pending and not terminal:
                context.services.sleep(context.settings.direct_verify_poll_seconds)
        if not pending and not terminal:
            return StepOutcome(status=StepOutcomeStatus.COMPLETED)
        if context.target.is_direct:
            self._mark_applied_without_record(context)
        if terminal:
            return self._fail(
                context,
                "await_in_sync",
                "reboot_not_in_sync",
                "An instance reached a status it will not recover from after the reboot",
                ", ".join([describe_instance_statuses(terminal), *pending]),
                "check the instance's status and events in AWS and fix the cause; "
                + self._recovery_advice(context),
            )
        return self._fail(
            context,
            "await_in_sync",
            "reboot_not_in_sync",
            "Instances did not report in-sync after the reboot",
            ", ".join(pending),
            self._recovery_advice(context),
        )


class ForeignOwnedRebootRefused(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """An instance the reboot would touch is owned by other tooling, which leaves it read-only here."""

    diagnostic_kind = "foreign_owned_resource"
    title = "Instances owned by other tooling cannot be rebooted here"
    fix_suggestion = (
        "reboot them in the tooling that owns them, or allow direct changes to foreign-owned resources in "
        "this environment's configuration"
    )


def refuse_foreign_owned_reboot(context: StepContext, ownership_by_identifier: dict[str, str]) -> None:
    """Refuse a reboot that would touch an instance owned by other tooling, unless the environment allows it.

    The one rule for every reboot the console runs: a reboot of an instance, of the instances pending a
    group's reboot, and of a cluster's members after an attach. `allow_direct_changes_to_foreign_owned`
    lifts it, as it lifts the change gate's refusal.

    Args:
        context: The running step's context; its target carries the environment's setting.
        ownership_by_identifier: The ownership of each instance the reboot would touch.

    Raises:
        ForeignOwnedRebootRefused: Names each foreign-owned instance and its owner.

    Example:
        >>> refuse_foreign_owned_reboot(context, {"demo-aurora-reader": "foreign_cloudformation"})
        Traceback (most recent call last):
        ForeignOwnedRebootRefused: demo-aurora-reader is foreign cloudformation; the console leaves ...
    """
    if context.target.allow_direct_changes_to_foreign_owned:
        return
    foreign_owned = {
        identifier: ownership
        for identifier, ownership in sorted(ownership_by_identifier.items())
        if ownership.startswith(FOREIGN_OWNERSHIP_PREFIX)
    }
    if foreign_owned:
        raise ForeignOwnedRebootRefused(
            "; ".join(
                f"{identifier} is {ownership.replace('_', ' ')}"
                for identifier, ownership in foreign_owned.items()
            )
            + "; the console leaves resources owned by other tooling read-only"
        )
