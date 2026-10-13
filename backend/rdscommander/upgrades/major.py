"""Major engine upgrades through an RDS blue/green deployment (spec 8.7.1).

A major upgrade never changes the production resource in place. It creates a green copy on the target
version with parameter groups of the target family, pauses so the operator can test the green endpoint, and
switches production over only after a second approval, typed by identifier. The blue side is kept.

Steps (`MAJOR_UPGRADE_STEP_NAMES`), each idempotent and resumable:

Nothing changes AWS or the repository before the first approval.

1. `choose_target`: the target must be a major upgrade target the engine-versions table lists for the
   current version; the target's parameter group family is kept as `plan["target_family"]`. It also plans the
   green groups, reading only the inventory: `existing` verifies the groups the operator named are of the
   target family; `build` reuses groups of the target family that an upgrade build made earlier from this
   resource's current groups (by provenance), or else names the groups the builder will make. The plan is
   `plan["parameter_groups_plan"]` (`{"source": "existing" | "reused" | "build", "instance": ..., "cluster":
   ...}`), and the names are `plan["target_parameter_group_name"]` and
   `plan["target_cluster_parameter_group_name"]`.
2. `preflight`: `upgrades.preflight`; every check is a diagnostic on the review's checklist and the list is
   `plan["preflight"]`; any failure fails the step.
3. `await_approval`: the action review (`plan["action_review"]`) names the groups and whether they will be
   built, are reused or exist; no identifier to type.
4. `prepare_parameter_groups`: for a `build` plan, creates a `parameter_group_build_for_upgrade` operation
   approved with this one (its approver and approval time) and drives it inline (`operations.nested_run`);
   its id is `plan["groups_operation_id"]`. Existing and reused groups need nothing.
5. `create_deployment`: `CreateBlueGreenDeployment`, at most once (`plan["deployment_id"]`, the recorded
   row, and a lookup by `plan["deployment_name"]` for a crash between AWS and the console).
6. `await_green_available`: polls until AWS reports `AVAILABLE` and keeps `plan["green_endpoint"]`.
7. `await_switchover_approval`: pauses with `plan["approval_stage"] = "switchover"` and the identifier to
   type, until `plan["switchover_approved_at_utc"]` is set.
8. `switchover`: re-checks the deployment, keeps the blue side's resource ids
   (`plan["blue_resource_ids"]`), switches over once and polls until `SWITCHOVER_COMPLETED`.
9. `record_blue_side`: right after the switchover, finds the blue side (`<identifier>-old1`, checked against
   the kept resource ids), tags it `rdscommander:blue-side=<operation id>` and refreshes inventory, so the
   inbox lists it even when a later step fails.
10. and 11. `verify` (production reports the target version and groups) and `reconcile`.

A failure at any blue/green step leaves the deployment in place with a diagnostic naming the AWS reason;
only `blue_green_cleanup` deletes a deployment.

Example:
    >>> MajorUpgradeExecutor(config_store).step_names(operation)[:2]  # doctest: +SKIP
    ['choose_target', 'preflight']
"""

from __future__ import annotations

import uuid
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from rdscommander.aws.errors import diagnose_exception
from rdscommander.catalogue.catalogue import definition_for
from rdscommander.catalogue.options import UpgradeGroupBuildOptions
from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.control_db.models import BlueGreenDeployment, Operation
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.history.change_events import CHANGED_AWS_BEFORE_PAUSE_PLAN_KEY
from rdscommander.inventory.refresh_executor import refresh_target_now
from rdscommander.lifecycle.direct_api import cluster_describe, instance_describe
from rdscommander.lifecycle.reconcile import (
    RECONCILE_STEP_NAME,
    StateMove,
    reconcile_touched_addresses,
    switchover_finish_by_hand,
)
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeDiagnosticError
from rdscommander.operations.direct_action import ACTION_REVIEW_PLAN_KEY, ActionReview
from rdscommander.operations.enums import ObservationState, OperationState, StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepObservation, StepOutcome
from rdscommander.operations.nested_run import (
    APPROVED_WITH_OPERATION_PLAN_KEY,
    create_nested_operation,
    run_nested_operation,
)
from rdscommander.operations.service import (
    APPROVAL_STAGE_PLAN_KEY,
    SWITCHOVER_APPROVAL_STAGE,
    SWITCHOVER_APPROVED_BY_PLAN_KEY,
    SWITCHOVER_APPROVED_PLAN_KEY,
)
from rdscommander.parameters.executors import UPGRADE_PLANS_PLAN_KEY, UpgradeGroupBuilderExecutor
from rdscommander.parameters.metadata import family_for_version
from rdscommander.parameters.upgrade_builder import new_group_name_for, upgrade_sources
from rdscommander.resources.model import ResourceKind, ResourceRecord, terraform_resource_type_for
from rdscommander.resources.queries import find_parameter_group, load_resource
from rdscommander.terraform.names import terraform_address_for
from rdscommander.upgrades.blue_green_api import (
    BLUE_SIDE_SUFFIX,
    SWITCHOVER_COMPLETED_STATUS,
    SWITCHOVER_FAILED_STATUS,
    SWITCHOVER_IN_PROGRESS_STATUS,
    BlueGreenStatus,
    blue_pending_modifications,
    cluster_member_arns,
    create_deployment,
    describe_deployment,
    find_deployment_by_name,
    is_deployment_not_found,
    record_deployment,
    switchover,
    tag_blue_side,
    update_deployment_status,
)
from rdscommander.upgrades.green_groups import reusable_groups
from rdscommander.upgrades.options import MajorUpgradeOptions
from rdscommander.upgrades.preflight import (
    CHECK_TITLES,
    PREFLIGHT_STEP_NAME,
    describe_operations,
    member_records,
    operations_in_progress_on,
    record_preflight_checks,
    run_preflight,
)
from rdscommander.upgrades.targets import (
    UpgradeTargetNotValid,
    engine_versions_for,
    ensure_valid_upgrade_target,
    major_version_of,
)
from rdscommander.upgrades.transient import call_riding_out_transient_errors

logger = get_logger(__name__)

MAJOR_UPGRADE_STEP_NAMES = (
    "choose_target",
    "preflight",
    "await_approval",
    "prepare_parameter_groups",
    "create_deployment",
    "await_green_available",
    "await_switchover_approval",
    "switchover",
    "record_blue_side",
    "verify",
    "reconcile",
)
GROUPS_OPERATION_PLAN_KEY = "groups_operation_id"
"""Plan key: the `parameter_group_build_for_upgrade` operation run inline for this upgrade."""
PARAMETER_GROUPS_PLAN_KEY = "parameter_groups_plan"
"""Plan key: `{"source": "existing" | "reused" | "build", "instance": name, "cluster": name or None}`, the
green groups as `choose_target` planned them before the first approval."""
BLUE_SIDE_IDENTIFIERS_PLAN_KEY = "blue_side_identifiers"
"""Plan key: the identifiers of the blue side kept after the switchover (the resource, then its members)."""
BLUE_SIDE_ARNS_PLAN_KEY = "blue_side_arns"
"""Plan key: the ARNs of `blue_side_identifiers`, the resource first."""
BLUE_RESOURCE_IDS_PLAN_KEY = "blue_resource_ids"
"""Plan key: `{identifier: resource id}` of the blue instance or cluster (and members) before the switchover;
the resource id (`DbiResourceId`, `DbClusterResourceId`) survives the rename, the name does not."""
BLUE_MEMBER_ARNS_PLAN_KEY = "blue_member_arns"
"""Plan key: a cluster's member ARNs as they were before the switchover."""
FIRST_PAUSE_DOWNTIME = "none until switchover"
PROVISIONING_STATUS = "PROVISIONING"
"""The status AWS gives a deployment it has just accepted."""
AVAILABLE_STATUS = "AVAILABLE"
"""The status of a deployment whose green side is ready for testing and switchover."""
GREEN_TERMINAL_STATUSES = frozenset({"INVALID_CONFIGURATION", "DELETING"})
"""Statuses a deployment never becomes `AVAILABLE` from by waiting."""
MAXIMUM_DEPLOYMENT_NAME_PREFIX_LENGTH = 40
CLEAN_UP_SUGGESTION = (
    "the deployment is left in place: read the AWS reason, fix the cause, then remove it with Clean up "
    "blue/green deployment on the resource page and create the upgrade again"
)
SWITCHOVER_TIMEOUT_SECONDS = 300
"""`SwitchoverTimeout`: AWS rolls the switchover back when it takes longer."""
SWITCHOVER_WAIT_LIMIT_SECONDS = SWITCHOVER_TIMEOUT_SECONDS * 3
"""How long the switchover step polls; AWS has finished or rolled back well before."""
SWITCHOVER_DOWNTIME = "connections drop while AWS switches over, usually for under a minute"
SWITCHOVER_TERMINAL_STATUSES = frozenset({SWITCHOVER_FAILED_STATUS, "INVALID_CONFIGURATION", "DELETING"})
"""Statuses a deployment never reaches `SWITCHOVER_COMPLETED` from by waiting."""
SWITCHOVER_STARTED_STATUSES = frozenset({SWITCHOVER_IN_PROGRESS_STATUS, SWITCHOVER_COMPLETED_STATUS})
"""Statuses saying the switchover was already asked for (a resumed step must not ask again)."""


def deployment_name_for(resource_identifier: str, target_engine_version: str, operation_id: uuid.UUID) -> str:
    """A deployment name unique to the operation: `<identifier>-pg<major>-<8 hex of the operation id>`.

    AWS accepts up to 60 letters, digits and hyphens, starting with a letter and with no hyphen at the end
    or twice in a row; an RDS identifier already starts with a letter and has neither.

    Example:
        >>> deployment_name_for("demo-orders", "17.2", uuid.UUID("1a2b3c4d" + "0" * 24))
        'demo-orders-pg17-1a2b3c4d'
    """
    prefix = resource_identifier[:MAXIMUM_DEPLOYMENT_NAME_PREFIX_LENGTH].rstrip("-")
    return f"{prefix}-pg{major_version_of(target_engine_version)}-{operation_id.hex[:8]}"


class MajorUpgradeExecutor:
    """Upgrade a standalone instance or a cluster to a listed major version through blue/green.

    Attributes:
        config_store: The live configuration.
        group_builder: The executor of the parameter group build run inline by `prepare_parameter_groups`.
    """

    operation_kind = "major_upgrade"
    STEP_NAMES = MAJOR_UPGRADE_STEP_NAMES

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read the live configuration from `config_store`; the group builder reads the same store."""
        self.config_store = config_store
        self.group_builder = UpgradeGroupBuilderExecutor(config_store)

    # ----- runner contract ---------------------------------------------------------------------------

    def step_names(self, operation: Operation) -> list[str]:
        """Every step of the upgrade, in order."""
        return list(self.STEP_NAMES)

    def observe(self, step_name: str, context: StepContext) -> StepObservation:
        """The first pause is satisfied by approval, the second by switchover approval, others once done."""
        if step_name == "await_approval":
            satisfied = context.operation.approved_at_utc is not None
        elif step_name == "await_switchover_approval":
            satisfied = bool(context.operation.plan.get(SWITCHOVER_APPROVED_PLAN_KEY))
        else:
            satisfied = context.step_already_completed(step_name)
        return StepObservation(state=ObservationState.SATISFIED if satisfied else ObservationState.ABSENT)

    def execute(self, step_name: str, context: StepContext) -> StepOutcome:
        """Run `_execute_<step_name>`; once the deployment exists, an AWS error fails through `_fail`.

        The diagnostic then names the deployment and says it is left in place, and the operation is marked
        `applied_without_record`, so history keeps crediting the green side and the renames to it.
        """
        try:
            return getattr(self, f"_execute_{step_name}")(context)
        except (ClientError, BotoCoreError) as error:
            if not context.operation.plan.get("deployment_id"):
                raise
            context.session.rollback()
            return self._aws_error_with_deployment(context, step_name, error)

    def _aws_error_with_deployment(
        self, context: StepContext, step_name: str, error: ClientError | BotoCoreError
    ) -> StepOutcome:
        """Fail a step whose AWS call raised while the deployment exists, naming it and what is left.

        A `BotoCoreError` (no answer from AWS: credentials, connection) is named by its type and message.
        """
        deployment_id = str(context.operation.plan["deployment_id"])
        identifier = str(context.operation.plan.get("resource_identifier") or "the resource")
        if isinstance(error, ClientError):
            aws_error = error.response.get("Error", {})
        else:
            aws_error = {"Code": type(error).__name__, "Message": str(error)}
        deployment = context.session.get(BlueGreenDeployment, deployment_id)
        if deployment is not None and deployment.switched_over_at_utc is not None:
            fix_suggestion = (
                f"production {identifier} runs the green side and deployment {deployment_id} is left in "
                f"place with the blue side kept as {identifier}{BLUE_SIDE_SUFFIX}; fix the cause AWS names "
                "(permissions, throttling), then refresh inventory and run Detect drift"
                + self._finish_by_hand(context)
            )
        else:
            fix_suggestion = f"production {identifier} is unchanged; {CLEAN_UP_SUGGESTION}"
        return self._fail(
            context,
            step_name,
            "blue_green_step_aws_error",
            "AWS refused a call while the blue/green deployment exists",
            f"{step_name}: {aws_error.get('Code')}: {aws_error.get('Message')}; deployment {deployment_id} "
            "is left in place",
            fix_suggestion,
        )

    # ----- helpers -----------------------------------------------------------------------------------

    def _options(self, operation: Operation) -> MajorUpgradeOptions:
        return MajorUpgradeOptions.model_validate(operation.plan["options"])

    def _store_plan_values(self, context: StepContext, **values: Any) -> None:
        """Write plan entries and commit; the plan is replaced as a whole because in-place edits are lost."""
        context.operation.plan = {**context.operation.plan, **values}
        context.session.commit()

    def _fail(
        self, context: StepContext, step_name: str, kind: str, title: str, detail: str, fix_suggestion: str
    ) -> StepOutcome:
        """Record a diagnostic (which logs the fix suggestion) and fail the step.

        Once the deployment exists AWS has changed, so the operation is marked `applied_without_record`:
        change history then credits the green side (and, after a switchover, everything it renamed) to
        the upgrade.
        """
        if context.operation.plan.get("deployment_id"):
            self._store_plan_values(context, applied_without_record=True)
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

    def _record(self, context: StepContext) -> ResourceRecord | None:
        """The resource from the latest inventory."""
        return load_resource(context.session, context.console_config, context.operation.resource_arn or "")

    def _resource_missing(self, context: StepContext, step_name: str) -> StepOutcome:
        return self._fail(
            context,
            step_name,
            "resource_not_in_inventory",
            "The resource is not in the latest inventory",
            f"{context.operation.resource_arn} is not in the latest inventory",
            "refresh inventory and create the upgrade again",
        )

    def _group_names(self, plan: dict[str, Any]) -> list[str]:
        """The green side's parameter group names: the instance group, then a cluster's group."""
        return [
            name
            for name in (
                plan.get("target_parameter_group_name"),
                plan.get("target_cluster_parameter_group_name"),
            )
            if name
        ]

    def _groups_phrase(self, plan: dict[str, Any]) -> str:
        """What the first review says about the green groups: built after approval, reused, or existing.

        Example:
            ``"will build demo-postgres16-tuned-pg17 first"`` for groups the upgrade builds once approved.
        """
        groups_plan = plan.get(PARAMETER_GROUPS_PLAN_KEY) or {}
        names = ", ".join(self._group_names(plan))
        source = groups_plan.get("source")
        if source == "build":
            return f"will build {names} first"
        if source == "reused":
            return f"reuses {names} (built earlier for {plan['resource_identifier']})"
        return f"uses existing {names}"

    # ----- 1. choose_target --------------------------------------------------------------------------

    def _execute_choose_target(self, context: StepContext) -> StepOutcome:
        """Refuse a target that is not a listed major upgrade; keep the target's parameter group family."""
        options = self._options(context.operation)
        record = self._record(context)
        if record is None:
            return self._resource_missing(context, "choose_target")
        if record.kind == ResourceKind.INSTANCE and record.relationships.get("cluster_identifier"):
            return self._fail(
                context,
                "choose_target",
                "engine_version_lives_on_the_cluster",
                "Upgrade the cluster",
                f"{record.identifier} is a member of {record.relationships['cluster_identifier']}; Aurora "
                "members run the version of their cluster",
                "open the cluster and run the major upgrade there; its members follow",
            )
        engine = str(record.facts.get("engine") or "postgres")
        current_engine_version = str(record.facts.get("engine_version") or "")
        versions = engine_versions_for(context, engine, step_name="choose_target")
        try:
            ensure_valid_upgrade_target(
                versions,
                engine,
                current_engine_version,
                options.target_engine_version,
                "major",
                record.identifier,
            )
        except UpgradeTargetNotValid as error:
            return self._fail(
                context,
                "choose_target",
                error.diagnostic_kind,
                error.title,
                str(error),
                error.fix_suggestion,
            )
        target_family = family_for_version(engine, options.target_engine_version, versions)
        self._store_plan_values(
            context,
            target_family=target_family,
            source_engine_version=current_engine_version,
            resource_identifier=record.identifier,
        )
        groups_plan = self._plan_parameter_groups(context, record, options, target_family)
        if isinstance(groups_plan, StepOutcome):
            return groups_plan
        logger.info(
            "major_upgrade_target_chosen",
            operation_id=str(context.operation.id),
            resource_identifier=record.identifier,
            current_engine_version=current_engine_version,
            target_engine_version=options.target_engine_version,
            target_family=target_family,
            parameter_groups_plan=groups_plan,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={
                "target_family": target_family,
                "source_engine_version": current_engine_version,
                PARAMETER_GROUPS_PLAN_KEY: groups_plan,
            },
        )

    def _plan_parameter_groups(
        self,
        context: StepContext,
        record: ResourceRecord,
        options: MajorUpgradeOptions,
        target_family: str,
    ) -> dict[str, Any] | StepOutcome:
        """Plan the green groups from the inventory alone and keep the plan; nothing is created here.

        `existing` checks the named groups; `build` reuses groups built earlier from the current ones, or
        names the groups the builder will make (the same names the builder gives them).

        Returns:
            The plan (`source`, `instance`, `cluster`), or the failed outcome of `choose_target`.
        """
        if options.parameter_group_choice == "existing":
            wanted, problems = self._existing_group_problems(context, record, options, target_family)
            if problems:
                return self._fail(
                    context,
                    "choose_target",
                    "upgrade_parameter_group_not_usable",
                    "The named parameter groups cannot be used for the upgrade",
                    "; ".join(problems),
                    f"name {target_family} groups that exist (refresh inventory after creating one), or let "
                    "the upgrade build them",
                )
            return self._store_groups_plan(context, wanted, "existing")
        try:
            reusable = reusable_groups(context, record, options, target_family)
            if reusable is not None:
                return self._store_groups_plan(context, reusable, "reused")
            planned: dict[str, str | None] = {
                source.scope: new_group_name_for(
                    source.group_name,
                    source.is_default,
                    record.identifier,
                    target_family,
                    options.group_name_suffix,
                )
                for source in upgrade_sources(context.session, context.console_config, record)
            }
        except ChangeDiagnosticError as error:
            return self._fail(
                context, "choose_target", error.diagnostic_kind, error.title, str(error), error.fix_suggestion
            )
        return self._store_groups_plan(context, planned, "build")

    def _existing_group_problems(
        self,
        context: StepContext,
        record: ResourceRecord,
        options: MajorUpgradeOptions,
        target_family: str,
    ) -> tuple[dict[str, str | None], list[str]]:
        """The groups the operator named, and why they cannot be used (none exists or is of the family)."""
        wanted: dict[str, str | None] = {"instance": options.instance_parameter_group_name}
        problems: list[str] = []
        if record.kind == ResourceKind.CLUSTER:
            wanted["cluster"] = options.cluster_parameter_group_name
        elif options.cluster_parameter_group_name:
            problems.append(f"{record.identifier} is an instance; it takes no cluster parameter group")
        for scope, name in wanted.items():
            if not name:
                problems.append(f"name the {scope} parameter group of {target_family} to use")
                continue
            group = find_parameter_group(
                context.session, context.console_config, record.target_key, scope, name
            )
            if group is None:
                problems.append(f"there is no {scope} parameter group {name} in {record.target_key}")
            elif group.facts.get("parameter_group_family") != target_family:
                problems.append(
                    f"{name} is a {group.facts.get('parameter_group_family')} group, not {target_family}"
                )
        return wanted, problems

    def _store_groups_plan(
        self, context: StepContext, groups: dict[str, str | None], source: str
    ) -> dict[str, Any] | StepOutcome:
        """Keep the planned group names; an instance group is required (for a cluster, its members')."""
        instance_group_name = groups.get("instance")
        if not instance_group_name:
            return self._fail(
                context,
                "choose_target",
                "upgrade_parameter_group_not_usable",
                "The upgrade has no instance parameter group for the green side",
                f"the {source} groups name no instance parameter group",
                "attach an instance parameter group to the resource (or its members), then create the "
                "upgrade again",
            )
        groups_plan = {"source": source, "instance": instance_group_name, "cluster": groups.get("cluster")}
        self._store_plan_values(
            context,
            target_parameter_group_name=instance_group_name,
            target_cluster_parameter_group_name=groups.get("cluster"),
            **{PARAMETER_GROUPS_PLAN_KEY: groups_plan},
        )
        return groups_plan

    # ----- 4. prepare_parameter_groups (after the first approval) ---------------------------------------

    def _execute_prepare_parameter_groups(self, context: StepContext) -> StepOutcome:
        """Build the planned groups inline (a `build` plan); existing and reused groups need nothing."""
        groups_plan = dict(context.operation.plan[PARAMETER_GROUPS_PLAN_KEY])
        if groups_plan["source"] != "build":
            how = str(groups_plan["source"])
            return self._store_groups(
                context, {"instance": groups_plan["instance"], "cluster": groups_plan["cluster"]}, how
            )
        record = self._record(context)
        if record is None:
            return self._resource_missing(context, "prepare_parameter_groups")
        groups_operation_id = context.operation.plan.get(GROUPS_OPERATION_PLAN_KEY)
        if groups_operation_id is None:
            groups_operation_id = str(
                self._create_group_build(context, record, self._options(context.operation))
            )
            self._store_plan_values(context, **{GROUPS_OPERATION_PLAN_KEY: groups_operation_id})
        return self._run_group_build(context, uuid.UUID(groups_operation_id))

    def _create_group_build(
        self, context: StepContext, record: ResourceRecord, options: MajorUpgradeOptions
    ) -> uuid.UUID:
        """The build operation of this upgrade, approved with it (its approver) and leased to this runner."""
        build_options = UpgradeGroupBuildOptions(
            target_engine_version=options.target_engine_version, name_suffix=options.group_name_suffix
        )
        return create_nested_operation(
            context,
            operation_kind=self.group_builder.operation_kind,
            plan={
                "options": build_options.model_dump(mode="json"),
                "resource_arn": record.arn,
                "resource_kind": record.kind.value,
            },
            title=f"{definition_for(self.group_builder.operation_kind).title}: {record.identifier}",
            idempotency_key=f"{context.operation.id}:parameter_groups",
        )

    def _run_group_build(self, context: StepContext, groups_operation_id: uuid.UUID) -> StepOutcome:
        """Drive the build to its end (or read how it ended) and take the names of the groups it built."""
        result = run_nested_operation(context, groups_operation_id, self.group_builder)
        if result.state != OperationState.SUCCEEDED.value:
            return self._fail(
                context,
                "prepare_parameter_groups",
                "upgrade_parameter_groups_not_built",
                "The parameter groups for the upgrade could not be built",
                f"operation {groups_operation_id} ({self.group_builder.operation_kind}) is {result.state}: "
                f"{result.failure_summary}",
                result.fix_suggestion
                or f"open operation {groups_operation_id}, fix the cause, then create the upgrade again",
            )
        build = context.session.get(Operation, groups_operation_id)
        assert build is not None  # run_nested_operation raised LookupError otherwise
        context.session.refresh(build)
        built = {plan["scope"]: plan["new_group_name"] for plan in build.plan.get(UPGRADE_PLANS_PLAN_KEY, [])}
        return self._store_groups(
            context, {"instance": built.get("instance"), "cluster": built.get("cluster")}, "built"
        )

    def _store_groups(self, context: StepContext, groups: dict[str, str | None], how: str) -> StepOutcome:
        """Keep the green side's group names; an instance group is required (for a cluster, its members')."""
        instance_group_name = groups.get("instance")
        if not instance_group_name:
            return self._fail(
                context,
                "prepare_parameter_groups",
                "upgrade_parameter_group_not_usable",
                "The upgrade has no instance parameter group for the green side",
                f"the {how} groups name no instance parameter group",
                "attach an instance parameter group to the resource (or its members), then create the "
                "upgrade again",
            )
        self._store_plan_values(
            context,
            target_parameter_group_name=instance_group_name,
            target_cluster_parameter_group_name=groups.get("cluster"),
            parameter_groups_source=how,
        )
        logger.info(
            "major_upgrade_parameter_groups_prepared",
            operation_id=str(context.operation.id),
            parameter_groups_source=how,
            instance_parameter_group_name=instance_group_name,
            cluster_parameter_group_name=groups.get("cluster"),
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"parameter_groups_source": how, "groups": {k: v for k, v in groups.items() if v}},
        )

    # ----- 2. preflight --------------------------------------------------------------------------------

    def _execute_preflight(self, context: StepContext) -> StepOutcome:
        """Run the checks, file each on the review's checklist, keep the list, and fail on any failure."""
        options = self._options(context.operation)
        record = self._record(context)
        if record is None:
            return self._resource_missing(context, PREFLIGHT_STEP_NAME)
        checks = run_preflight(context, record, options.target_engine_version)
        record_preflight_checks(context.session, context.operation.id, PREFLIGHT_STEP_NAME, checks)
        self._store_plan_values(context, preflight=[check.model_dump(mode="json") for check in checks])
        failed = [check for check in checks if check.outcome == "fail"]
        if failed:
            return StepOutcome(
                status=StepOutcomeStatus.FAILED,
                detail="; ".join(f"{CHECK_TITLES[check.name]}: {check.detail}" for check in failed),
                fix_suggestion=failed[0].fix_suggestion,
                diagnostic_recorded=True,
            )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED, output={check.name: check.outcome for check in checks}
        )

    # ----- 3. await_approval ---------------------------------------------------------------------------

    def _execute_await_approval(self, context: StepContext) -> StepOutcome:
        """Pause with the review of the green copy and its groups; nothing is typed, as it interrupts nothing.

        Nothing exists yet: the groups are built (when the plan says so) only after this approval.
        """
        plan = context.operation.plan
        options = self._options(context.operation)
        identifier = str(plan["resource_identifier"])
        review = ActionReview(
            what=[
                f"create a green copy of {identifier} on {options.target_engine_version}: "
                f"{self._groups_phrase(plan)}; switchover needs a second approval"
            ],
            identifiers=[identifier],
            requires_reboot=False,
            downtime=FIRST_PAUSE_DOWNTIME,
            reconcile=False,
        )
        # The creation route asks disruptive operations for the identifier; here the switchover asks it.
        context.operation.approval_required_text = None
        self._store_plan_values(
            context,
            **{
                ACTION_REVIEW_PLAN_KEY: review.model_dump(mode="json"),
                "requires_reboot": review.requires_reboot,
                "downtime": review.downtime,
                APPROVAL_STAGE_PLAN_KEY: "create_green",
            },
        )
        logger.info(
            "major_upgrade_ready_for_approval",
            operation_id=str(context.operation.id),
            resource_identifier=identifier,
            target_engine_version=options.target_engine_version,
        )
        return StepOutcome(status=StepOutcomeStatus.AWAITING_APPROVAL, detail="; ".join(review.what))

    # ----- 5. create_deployment ------------------------------------------------------------------------

    def _execute_create_deployment(self, context: StepContext) -> StepOutcome:
        """Create the deployment once: a stored id, or one AWS has under this operation's name, is kept."""
        plan = context.operation.plan
        if plan.get("deployment_id"):
            return StepOutcome(
                status=StepOutcomeStatus.COMPLETED,
                output={"deployment_id": plan["deployment_id"], "created": False},
            )
        options = self._options(context.operation)
        resource_arn = context.operation.resource_arn or ""
        name = plan.get("deployment_name") or deployment_name_for(
            str(plan["resource_identifier"]), options.target_engine_version, context.operation.id
        )
        if plan.get("deployment_name") != name or not plan.get(CHANGED_AWS_BEFORE_PAUSE_PLAN_KEY):
            # Before AWS: a retry looks the name up, and history credits the green side to this upgrade
            # while it waits for the switchover approval (or after a rejection).
            self._store_plan_values(
                context, deployment_name=name, **{CHANGED_AWS_BEFORE_PAUSE_PLAN_KEY: True}
            )
        deployment_id = find_deployment_by_name(context, name, "create_deployment")
        created = deployment_id is None
        if deployment_id is None:
            try:
                deployment_id = create_deployment(
                    context,
                    name,
                    resource_arn,
                    options.target_engine_version,
                    str(plan["target_parameter_group_name"]),
                    plan.get("target_cluster_parameter_group_name"),
                    "create_deployment",
                )
            except ClientError as error:
                # A retried create whose first response was lost answers "already exists" while AWS has the
                # deployment, so look once more before calling the creation refused.
                deployment_id = find_deployment_by_name(context, name, "create_deployment")
                if deployment_id is None:
                    aws_error = error.response.get("Error", {})
                    return self._fail(
                        context,
                        "create_deployment",
                        "blue_green_create_refused",
                        "AWS refused to create the blue/green deployment",
                        f"deployment {name}: {aws_error.get('Code')}: {aws_error.get('Message')}",
                        f"fix the cause AWS names (no deployment named {name} exists), then create the "
                        "upgrade again",
                    )
                logger.warning(
                    "major_upgrade_deployment_found_after_create_error",
                    operation_id=str(context.operation.id),
                    deployment_name=name,
                    deployment_id=deployment_id,
                    aws_error_code=error.response.get("Error", {}).get("Code"),
                    fix_suggestion="nothing to do; the deployment AWS created is tracked",
                )
                created = False
        if context.session.get(BlueGreenDeployment, deployment_id) is None:
            record_deployment(
                context.session,
                deployment_id,
                context.operation.id,
                resource_arn,
                context.operation.environment_slug or "",
                context.operation.target_key or "",
                PROVISIONING_STATUS,
                options.target_engine_version,
            )
        # AWS has the green side from here on: history credits it to this upgrade however a later step ends.
        self._store_plan_values(context, deployment_id=deployment_id, applied_without_record=True)
        logger.info(
            "major_upgrade_deployment_created",
            operation_id=str(context.operation.id),
            deployment_id=deployment_id,
            deployment_name=name,
            created_now=created,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"deployment_id": deployment_id, "deployment_name": name, "created": created},
        )

    # ----- 6. await_green_available --------------------------------------------------------------------

    def _execute_await_green_available(self, context: StepContext) -> StepOutcome:
        """Poll the deployment until AWS reports it `AVAILABLE`; a terminal status or the timeout fails.

        The wait is `blue_green_create_timeout_seconds` (copying a large database takes hours); the runner's
        lease heartbeat keeps the operation while it polls. A describe that fails for a passing reason
        (throttling, an expired login, no connection) is asked again with backoff until that limit, and only
        then fails, naming the deployment left in place. Past the limit the deployment is still being
        created in AWS: it is left in place and the diagnostic says how to remove it once it is available.
        """
        deployment_id = str(context.operation.plan["deployment_id"])
        timeout_seconds = context.settings.blue_green_create_timeout_seconds
        poll_seconds = context.settings.direct_verify_poll_seconds
        waited_seconds = 0.0
        while True:
            try:
                # A throttled or unanswered describe is asked again after a backoff, within the same limit.
                status, waited_seconds = call_riding_out_transient_errors(
                    context,
                    lambda: describe_deployment(context, deployment_id, "await_green_available"),
                    step_name="await_green_available",
                    waited_seconds=waited_seconds,
                    limit_seconds=timeout_seconds,
                    poll_seconds=poll_seconds,
                )
            except ClientError as error:
                if not is_deployment_not_found(error):
                    raise
                return self._fail(
                    context,
                    "await_green_available",
                    "blue_green_deployment_missing",
                    "The blue/green deployment no longer exists",
                    f"AWS does not know deployment {deployment_id}",
                    "refresh inventory and create the upgrade again",
                )
            update_deployment_status(context.session, deployment_id, status.status, status.green_endpoint)
            context.session.commit()
            if status.status == AVAILABLE_STATUS:
                self._store_plan_values(
                    context, green_endpoint=status.green_endpoint, green_arn=status.green_arn
                )
                self._credit_resources(context, created=self._green_side_arns(context, status))
                logger.info(
                    "major_upgrade_green_available",
                    operation_id=str(context.operation.id),
                    deployment_id=deployment_id,
                    green_endpoint=status.green_endpoint,
                    waited_seconds=waited_seconds,
                )
                return StepOutcome(
                    status=StepOutcomeStatus.COMPLETED,
                    output={
                        "deployment_status": status.status,
                        "green_endpoint": status.green_endpoint,
                        "waited_seconds": waited_seconds,
                    },
                )
            if status.status in GREEN_TERMINAL_STATUSES:
                return self._fail(
                    context,
                    "await_green_available",
                    "blue_green_green_not_available",
                    "The green side did not become available",
                    f"deployment {deployment_id} is {status.status}: "
                    f"{status.status_details or 'AWS gave no reason'}",
                    CLEAN_UP_SUGGESTION,
                )
            if waited_seconds >= timeout_seconds:
                return self._fail(
                    context,
                    "await_green_available",
                    "blue_green_green_not_available",
                    "The green side was not available in time",
                    f"deployment {deployment_id} is still being created in AWS ({status.status} after "
                    f"{int(waited_seconds)} s, the limit set by blue_green_create_timeout_seconds); it was "
                    "left in place and nothing was switched",
                    f"wait for AWS to report deployment {deployment_id} AVAILABLE, then remove it with "
                    "Clean up blue/green deployment (blue_green_cleanup) on the resource page and create the "
                    "upgrade again",
                )
            logger.info(
                "major_upgrade_awaiting_green",
                operation_id=str(context.operation.id),
                deployment_id=deployment_id,
                deployment_status=status.status,
                waited_seconds=waited_seconds,
            )
            context.services.sleep(poll_seconds)
            waited_seconds += poll_seconds

    # ----- blue/green helpers --------------------------------------------------------------------------

    def _is_cluster(self, context: StepContext) -> bool:
        """Whether the upgrade is of a cluster (its ARN names a cluster) rather than a standalone instance."""
        return ":cluster:" in (context.operation.resource_arn or "")

    def _credit_resources(
        self, context: StepContext, created: list[str] | None = None, affected: list[str] | None = None
    ) -> None:
        """Name resources the upgrade creates or renames, so change history credits them to it.

        `plan["created_resource_arns"]` gets the green side and the renamed blue side, and
        `plan["affected_resource_arns"]` a cluster's members, whose ARNs change hands at the switchover.
        """
        plan = context.operation.plan
        created_arns = sorted(set(plan.get("created_resource_arns", [])) | set(created or []))
        affected_arns = sorted(set(plan.get("affected_resource_arns", [])) | set(affected or []))
        if created_arns != plan.get("created_resource_arns", []) or affected_arns != plan.get(
            "affected_resource_arns", []
        ):
            self._store_plan_values(
                context, created_resource_arns=created_arns, affected_resource_arns=affected_arns
            )

    def _green_side_arns(self, context: StepContext, status: BlueGreenStatus) -> list[str]:
        """The green instance or cluster, its members, and every green member the switchover details list.

        The blue members (`SourceMember`) are not green: they are credited at the switchover, when AWS
        renames them.
        """
        arns = {status.green_arn} if status.green_arn else set()
        for detail in status.switchover_details:
            if detail.get("TargetMember"):
                arns.add(detail["TargetMember"])
        if status.green_arn and ":cluster:" in status.green_arn:
            arns.update(cluster_member_arns(context, status.green_arn, "await_green_available"))
        arns.discard(context.operation.resource_arn)
        return sorted(arns)

    def _describe_or_fail(self, context: StepContext, step_name: str) -> BlueGreenStatus | StepOutcome:
        """The deployment as AWS reports it now (the row follows it), or the failure when AWS has none."""
        status, _ = self._poll_deployment(context, step_name, waited_seconds=0.0, limit_seconds=0.0)
        return status

    def _poll_deployment(
        self, context: StepContext, step_name: str, *, waited_seconds: float, limit_seconds: float
    ) -> tuple[BlueGreenStatus | StepOutcome, float]:
        """One poll of the deployment: its status (or the failure when AWS has none) and the waited seconds.

        A describe failing for a passing reason (throttling, an expired login, no connection) is asked again
        with backoff until `limit_seconds` of waiting; the backoff counts in the returned waited seconds.
        With a limit of 0 nothing is retried.
        """
        deployment_id = str(context.operation.plan["deployment_id"])
        try:
            status, waited_seconds = call_riding_out_transient_errors(
                context,
                lambda: describe_deployment(context, deployment_id, step_name),
                step_name=step_name,
                waited_seconds=waited_seconds,
                limit_seconds=limit_seconds,
                poll_seconds=context.settings.direct_verify_poll_seconds,
            )
        except ClientError as error:
            if not is_deployment_not_found(error):
                raise
            return (
                self._fail(
                    context,
                    step_name,
                    "blue_green_deployment_missing",
                    "The blue/green deployment no longer exists",
                    f"AWS does not know deployment {deployment_id}",
                    f"refresh inventory and check {context.operation.plan['resource_identifier']} in AWS "
                    "before creating the upgrade again",
                ),
                waited_seconds,
            )
        update_deployment_status(context.session, deployment_id, status.status, status.green_endpoint)
        context.session.commit()
        return status, waited_seconds

    def _blue_side_arns_now(self, context: StepContext) -> list[str]:
        """The production resource and, for a cluster, its members as the latest inventory lists them."""
        resource_arn = context.operation.resource_arn or ""
        record = self._record(context)
        members = member_records(context, record) if record is not None else []
        return [resource_arn, *(member.arn for member in members)]

    def _blue_side_changes_since_green_copy(self, context: StepContext) -> list[Operation]:
        """Console operations on the blue side that succeeded after the deployment was created.

        The green copy was made when the deployment was created; a change made to production since (a
        parameter edit, a class change) is not on the green side, and the switchover would drop it. Work
        approved with this upgrade (its group build) does not count.
        """
        deployment = context.session.get(BlueGreenDeployment, str(context.operation.plan["deployment_id"]))
        if deployment is None:
            return []
        changed = context.session.execute(
            select(Operation)
            .where(
                Operation.resource_arn.in_(self._blue_side_arns_now(context)),
                Operation.state == OperationState.SUCCEEDED.value,
                Operation.finished_at_utc > deployment.created_at_utc,
                Operation.id != context.operation.id,
            )
            .order_by(Operation.finished_at_utc)
        ).scalars()
        return [
            operation
            for operation in changed
            if operation.plan.get(APPROVED_WITH_OPERATION_PLAN_KEY) != str(context.operation.id)
        ]

    def _warn_about_blue_side_changes(self, context: StepContext, step_name: str) -> str | None:
        """Warn about the blue side's changes since the green copy; return the review's sentence, or None."""
        changed = self._blue_side_changes_since_green_copy(context)
        if not changed:
            return None
        titles = ", ".join(operation.title or operation.operation_kind for operation in changed)
        sentence = (
            f"changed on the blue side since the green copy was made: {titles}; the green side does not "
            "carry them"
        )
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name=step_name,
            severity="warning",
            diagnostic_kind="blue_side_changed_since_green_copy",
            title="Production changed after the green copy was made",
            detail=f"{sentence} ({describe_operations(changed)})",
            fix_suggestion="make the same changes on the green side before the switchover, or reject it, "
            "clean up the deployment and create the upgrade again so the green copy includes them",
        )
        context.session.commit()
        return sentence

    def _refuse_switchover_unless_ready(
        self, context: StepContext, status: BlueGreenStatus, step_name: str
    ) -> StepOutcome | None:
        """Fail unless the deployment is `AVAILABLE` and the blue side is quiet.

        Quiet means no pending modifications in AWS and no other console operation on it (or a member)
        queued, running or awaiting approval. Asked when the second pause starts and again right before the
        switchover, which may be hours later (Review Focus 1). The deployment is left in place either way.
        """
        deployment_id = str(context.operation.plan["deployment_id"])
        identifier = str(context.operation.plan["resource_identifier"])
        in_progress = operations_in_progress_on(
            context.session, self._blue_side_arns_now(context), context.operation.id
        )
        if in_progress:
            return self._fail(
                context,
                step_name,
                "blue_green_not_ready_for_switchover",
                "Another operation on the blue side is in progress",
                f"other operations on {identifier} are in progress: {describe_operations(in_progress)}; a "
                "switchover now could drop their changes, so nothing was switched",
                f"let them finish, or reject the ones awaiting approval; {CLEAN_UP_SUGGESTION}",
            )
        if status.status != AVAILABLE_STATUS:
            return self._fail(
                context,
                step_name,
                "blue_green_not_ready_for_switchover",
                "The deployment is not ready for the switchover",
                f"deployment {deployment_id} is {status.status}, not {AVAILABLE_STATUS}: "
                f"{status.status_details or 'AWS gave no reason'}; nothing was switched",
                CLEAN_UP_SUGGESTION,
            )
        pending = blue_pending_modifications(context, context.operation.resource_arn or "", step_name)
        if pending:
            return self._fail(
                context,
                step_name,
                "blue_green_not_ready_for_switchover",
                "The blue side has modifications pending",
                f"AWS has modifications pending on {'; '.join(pending)}; a switchover now would fail or "
                "switch a half-changed database, so nothing was switched",
                f"apply the pending modifications to {identifier} (or wait for the maintenance window); "
                f"{CLEAN_UP_SUGGESTION}",
            )
        return None

    # ----- 7. await_switchover_approval ----------------------------------------------------------------

    def _execute_await_switchover_approval(self, context: StepContext) -> StepOutcome:
        """Check the deployment is still ready, then pause for the switchover, typed by identifier.

        The review is replaced by the switchover's: what moves where, the green endpoint to test first, and
        the interruption; the preflight checklist stays in `plan["preflight"]` for the review page. Console
        operations that changed the blue side after the green copy was made are named in the review (the
        green side does not carry them) and in a warning.
        """
        status = self._describe_or_fail(context, "await_switchover_approval")
        if isinstance(status, StepOutcome):
            return status
        refusal = self._refuse_switchover_unless_ready(context, status, "await_switchover_approval")
        if refusal is not None:
            return refusal
        changes_sentence = self._warn_about_blue_side_changes(context, "await_switchover_approval")
        plan = context.operation.plan
        identifier = str(plan["resource_identifier"])
        target_engine_version = self._options(context.operation).target_engine_version
        green_endpoint = status.green_endpoint or plan.get("green_endpoint")
        review = ActionReview(
            what=[
                f"switch production traffic from {identifier} to green ({green_endpoint}, "
                f"{target_engine_version}); {identifier} becomes {identifier}{BLUE_SIDE_SUFFIX} and is kept",
                *([changes_sentence] if changes_sentence else []),
            ],
            identifiers=[identifier],
            requires_reboot=False,
            downtime=SWITCHOVER_DOWNTIME,
            reconcile=True,
        )
        context.operation.approval_required_text = identifier
        self._store_plan_values(
            context,
            green_endpoint=green_endpoint,
            **{
                ACTION_REVIEW_PLAN_KEY: review.model_dump(mode="json"),
                "requires_reboot": review.requires_reboot,
                "downtime": review.downtime,
                APPROVAL_STAGE_PLAN_KEY: SWITCHOVER_APPROVAL_STAGE,
            },
        )
        logger.info(
            "major_upgrade_ready_for_switchover_approval",
            operation_id=str(context.operation.id),
            resource_identifier=identifier,
            green_endpoint=green_endpoint,
        )
        return StepOutcome(
            status=StepOutcomeStatus.AWAITING_APPROVAL,
            detail=f"the green side is available at {green_endpoint}; type {identifier} to switch production "
            "over",
        )

    # ----- 8. switchover -------------------------------------------------------------------------------

    def _blue_side_candidates(self, context: StepContext) -> tuple[list[str], list[str]]:
        """What the switchover renames: `(created, affected)` ARNs to credit to the upgrade before it runs.

        AWS appends `-old1` to the blue instance or cluster and to each blue member; the members' own ARNs
        then belong to the green members. The blue members are read once and kept in the plan, since after
        the switchover the cluster lists the green ones.
        """
        resource_arn = context.operation.resource_arn or ""
        blue_member_arns: list[str] = list(context.operation.plan.get(BLUE_MEMBER_ARNS_PLAN_KEY) or [])
        if self._is_cluster(context) and BLUE_MEMBER_ARNS_PLAN_KEY not in context.operation.plan:
            blue_member_arns = cluster_member_arns(context, resource_arn, "switchover")
            self._store_plan_values(context, **{BLUE_MEMBER_ARNS_PLAN_KEY: blue_member_arns})
        created = [f"{arn}{BLUE_SIDE_SUFFIX}" for arn in (resource_arn, *blue_member_arns)]
        return created, blue_member_arns

    def _capture_blue_resource_ids(self, context: StepContext) -> None:
        """Keep the blue side's resource ids before the switchover, once (the switchover renames it)."""
        if BLUE_RESOURCE_IDS_PLAN_KEY in context.operation.plan:
            return
        identifier = str(context.operation.plan["resource_identifier"])
        resource_ids: dict[str, str] = {}
        if self._is_cluster(context):
            cluster = cluster_describe(context, identifier, "switchover") or {}
            resource_ids[identifier] = str(cluster.get("DbClusterResourceId") or "")
            for member in cluster.get("DBClusterMembers", []):
                member_identifier = member["DBInstanceIdentifier"]
                described = instance_describe(context, member_identifier, "switchover") or {}
                resource_ids[member_identifier] = str(described.get("DbiResourceId") or "")
        else:
            described = instance_describe(context, identifier, "switchover") or {}
            resource_ids[identifier] = str(described.get("DbiResourceId") or "")
        self._store_plan_values(context, **{BLUE_RESOURCE_IDS_PLAN_KEY: resource_ids})

    def _execute_switchover(self, context: StepContext) -> StepOutcome:
        """Switch production to the green side once, then poll until AWS reports `SWITCHOVER_COMPLETED`.

        Before asking AWS, the deployment must still be `AVAILABLE` and the blue side free of pending
        modifications. A resumed step whose switchover AWS already started only polls; a poll failing for a
        passing reason (throttling, an expired login, no connection) is asked again with backoff within the
        wait limit. `SWITCHOVER_FAILED` (AWS rolled back), another terminal status or the wait limit fails,
        leaving the deployment in place.
        """
        deployment_id = str(context.operation.plan["deployment_id"])
        status = self._describe_or_fail(context, "switchover")
        if isinstance(status, StepOutcome):
            return status
        created, affected = self._blue_side_candidates(context)
        self._credit_resources(context, created=created, affected=affected)
        if status.status not in SWITCHOVER_STARTED_STATUSES:
            refusal = self._refuse_switchover_unless_ready(context, status, "switchover")
            if refusal is not None:
                return refusal
            self._warn_about_blue_side_changes(context, "switchover")
            self._capture_blue_resource_ids(context)
            try:
                switchover(context, deployment_id, SWITCHOVER_TIMEOUT_SECONDS, "switchover")
            except ClientError as error:
                aws_error = error.response.get("Error", {})
                return self._fail(
                    context,
                    "switchover",
                    "blue_green_switchover_refused",
                    "AWS refused the switchover",
                    f"deployment {deployment_id}: {aws_error.get('Code')}: {aws_error.get('Message')}; "
                    "nothing was switched",
                    CLEAN_UP_SUGGESTION,
                )
            logger.info(
                "major_upgrade_switchover_requested",
                operation_id=str(context.operation.id),
                deployment_id=deployment_id,
                switchover_approved_by=context.operation.plan.get(SWITCHOVER_APPROVED_BY_PLAN_KEY),
            )
        poll_seconds = context.settings.direct_verify_poll_seconds
        waited_seconds = 0.0
        while True:
            status, waited_seconds = self._poll_deployment(
                context,
                "switchover",
                waited_seconds=waited_seconds,
                limit_seconds=SWITCHOVER_WAIT_LIMIT_SECONDS,
            )
            if isinstance(status, StepOutcome):
                return status
            if status.status == SWITCHOVER_COMPLETED_STATUS:
                break
            if status.status in SWITCHOVER_TERMINAL_STATUSES:
                return self._fail(
                    context,
                    "switchover",
                    "blue_green_switchover_failed",
                    "The switchover did not complete",
                    f"deployment {deployment_id} is {status.status}: "
                    f"{status.status_details or 'AWS gave no reason'}",
                    f"production stays on the blue side; {CLEAN_UP_SUGGESTION}",
                )
            if waited_seconds >= SWITCHOVER_WAIT_LIMIT_SECONDS:
                return self._fail(
                    context,
                    "switchover",
                    "blue_green_switchover_failed",
                    "The switchover did not complete in time",
                    f"deployment {deployment_id} is still {status.status} after {int(waited_seconds)} s",
                    f"read the deployment's status in AWS; it is left in place. Once AWS reports it "
                    f"{SWITCHOVER_COMPLETED_STATUS}, refresh inventory and run Detect drift; otherwise "
                    f"{CLEAN_UP_SUGGESTION}",
                )
            logger.info(
                "major_upgrade_awaiting_switchover",
                operation_id=str(context.operation.id),
                deployment_id=deployment_id,
                deployment_status=status.status,
                waited_seconds=waited_seconds,
            )
            context.services.sleep(poll_seconds)
            waited_seconds += poll_seconds
        logger.info(
            "major_upgrade_switched_over",
            operation_id=str(context.operation.id),
            deployment_id=deployment_id,
            waited_seconds=waited_seconds,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={"deployment_status": SWITCHOVER_COMPLETED_STATUS, "waited_seconds": waited_seconds},
        )

    # ----- 10. verify ----------------------------------------------------------------------------------

    def _verification_problems(self, context: StepContext) -> list[str]:
        """What the production identifier reports that is not the target version or the green groups."""
        plan = context.operation.plan
        identifier = str(plan["resource_identifier"])
        target_engine_version = self._options(context.operation).target_engine_version
        instance_group_name = plan.get("target_parameter_group_name")
        problems: list[str] = []

        def check_instance(instance_identifier: str, check_version: bool) -> None:
            described = instance_describe(context, instance_identifier, "verify")
            if described is None:
                problems.append(f"AWS does not list {instance_identifier}")
                return
            version = described.get("EngineVersion")
            if check_version and version != target_engine_version:
                problems.append(f"{instance_identifier} runs {version}, not {target_engine_version}")
            groups = [group.get("DBParameterGroupName") for group in described.get("DBParameterGroups", [])]
            if instance_group_name not in groups:
                problems.append(
                    f"{instance_identifier} uses parameter group {', '.join(map(str, groups)) or 'none'}, "
                    f"not {instance_group_name}"
                )

        if not self._is_cluster(context):
            check_instance(identifier, check_version=True)
            return problems
        cluster = cluster_describe(context, identifier, "verify")
        if cluster is None:
            return [f"AWS does not list {identifier}"]
        version = cluster.get("EngineVersion")
        if version != target_engine_version:
            problems.append(f"{identifier} runs {version}, not {target_engine_version}")
        cluster_group_name = plan.get("target_cluster_parameter_group_name")
        if cluster_group_name and cluster.get("DBClusterParameterGroup") != cluster_group_name:
            problems.append(
                f"{identifier} uses cluster parameter group {cluster.get('DBClusterParameterGroup')}, "
                f"not {cluster_group_name}"
            )
        for member in cluster.get("DBClusterMembers", []):
            check_instance(member["DBInstanceIdentifier"], check_version=False)
        return problems

    def _execute_verify(self, context: StepContext) -> StepOutcome:
        """The production identifier reports the target version and the green groups.

        The switchover is done, so a mismatch is a warning (`upgrade_not_verified`) and the step completes:
        the record must still catch up with AWS. Only production not being listed at all fails the step.
        `record_blue_side` refreshed inventory after the switchover; `reconcile` renders from that snapshot.
        """
        identifier = str(context.operation.plan["resource_identifier"])
        target_engine_version = self._options(context.operation).target_engine_version
        problems = self._verification_problems(context)
        if f"AWS does not list {identifier}" in problems:
            return self._fail(
                context,
                "verify",
                "upgrade_not_verified",
                "Production does not report the upgrade",
                f"after the switchover {'; '.join(problems)}",
                f"production now runs the green side and the blue side is kept as {identifier}"
                f"{BLUE_SIDE_SUFFIX}; check {identifier} in AWS and, once it reports "
                f"{target_engine_version}, refresh inventory and run Detect drift"
                + self._finish_by_hand(context),
            )
        if problems:
            # The switchover is done, so the record must catch up: reconcile runs, the operator reads this.
            record_diagnostic(
                context.session,
                operation_id=context.operation.id,
                step_name="verify",
                severity="warning",
                diagnostic_kind="upgrade_not_verified",
                title="Production does not report all of the upgrade",
                detail=f"after the switchover {'; '.join(problems)}",
                fix_suggestion=f"check {identifier} in AWS; the record is reconciled from what AWS reports, "
                "so once it reports the upgrade, refresh inventory and run Detect drift",
            )
            context.session.commit()
            return StepOutcome(
                status=StepOutcomeStatus.COMPLETED,
                output={"engine_version": target_engine_version, "verification_warnings": problems},
            )
        logger.info(
            "major_upgrade_verified",
            operation_id=str(context.operation.id),
            resource_identifier=identifier,
            target_engine_version=target_engine_version,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED, output={"engine_version": target_engine_version}
        )

    # ----- 11. reconcile ------------------------------------------------------------------------------

    def _execute_reconcile(self, context: StepContext) -> StepOutcome:
        """Re-render the resource (and a cluster's members) from the fresh snapshot and commit.

        In Terraform mode state still holds the blue instance: an `aws_db_instance` is tracked by its
        resource id, which now belongs to `<identifier>-old1`. Its state entry moves to the blue side's own
        address (rendered into the record and adopted, so the blue side stays console-managed and can be
        decommissioned through Terraform), and the green instance is imported into the production address
        before the clean plan. A cluster and its members are tracked by identifier and need nothing.
        """
        record = self._record(context)
        if record is None:
            return self._resource_missing(context, RECONCILE_STEP_NAME)
        address = record.terraform_address or terraform_address_for(
            terraform_resource_type_for(record), record.identifier
        )
        addresses = {address}
        if record.kind == ResourceKind.CLUSTER:
            addresses.update(
                terraform_address_for("aws_rds_cluster_instance", member)
                for member in record.relationships.get("member_identifiers") or []
            )
        reimport: dict[str, str] = {}
        state_moves: list[StateMove] = []
        blue_arns = list(context.operation.plan.get(BLUE_SIDE_ARNS_PLAN_KEY) or [])
        if record.kind == ResourceKind.INSTANCE:
            reimport = {address: record.identifier}
            if blue_arns and not context.target.is_direct:
                blue_identifier = blue_arns[0].rsplit(":", 1)[-1]
                blue_address = terraform_address_for("aws_db_instance", blue_identifier)
                state_moves.append(
                    StateMove(address, blue_address, blue_arns[0], blue_identifier, "aws_db_instance")
                )
                addresses.add(blue_address)
        self._store_plan_values(context, touched_addresses=sorted(addresses))
        return reconcile_touched_addresses(
            context, addresses, reimport_import_ids=reimport, state_moves=state_moves
        )

    # ----- 9. record_blue_side -------------------------------------------------------------------------

    def _execute_record_blue_side(self, context: StepContext) -> StepOutcome:
        """Tag the kept blue side `rdscommander:blue-side=<operation id>`, list it, and refresh inventory.

        Runs right after the switchover, so a later failure still leaves the blue side listed. The resource
        found under `<identifier>-old1` must carry the resource id the blue side had before the switchover
        (`plan["blue_resource_ids"]`); anything else is not this upgrade's blue side and is left untagged.
        Nothing is deleted: the inbox lists the blue side with Decommission once the upgrade is proven.
        """
        identifier = str(context.operation.plan["resource_identifier"])
        blue_identifier = f"{identifier}{BLUE_SIDE_SUFFIX}"
        if self._is_cluster(context):
            described = cluster_describe(context, blue_identifier, "record_blue_side")
            blue_arns = (
                [described["DBClusterArn"]]
                + cluster_member_arns(context, described["DBClusterArn"], "record_blue_side")
                if described
                else []
            )
        else:
            described = instance_describe(context, blue_identifier, "record_blue_side")
            blue_arns = [described["DBInstanceArn"]] if described else []
        if not blue_arns:
            return self._fail(
                context,
                "record_blue_side",
                "blue_side_not_found",
                "The blue side is not where AWS puts it",
                f"AWS does not list {blue_identifier} after the switchover",
                f"find the renamed blue side in AWS (it is kept, not deleted) and decommission it from its "
                f"resource page once the upgrade is proven; production {identifier} is upgraded"
                + self._finish_by_hand(context),
            )
        mismatch = self._blue_side_mismatch(context, described, blue_arns)
        if mismatch:
            return self._fail(
                context,
                "record_blue_side",
                "blue_side_not_found",
                "The resource under the blue side's name is not the blue side",
                f"{mismatch}; it is not this upgrade's blue side and was not tagged",
                f"find the blue side in AWS by its resource id (it is kept, not deleted) and decommission it "
                f"from its resource page once the upgrade is proven; production {identifier} is upgraded"
                + self._finish_by_hand(context),
            )
        for blue_arn in blue_arns:
            tag_blue_side(context, blue_arn, "record_blue_side")
        blue_identifiers = [arn.rsplit(":", 1)[-1] for arn in blue_arns]
        self._credit_resources(context, created=blue_arns)
        self._store_plan_values(
            context, **{BLUE_SIDE_IDENTIFIERS_PLAN_KEY: blue_identifiers, BLUE_SIDE_ARNS_PLAN_KEY: blue_arns}
        )
        try:
            snapshot = refresh_target_now(context, context.target, "record_blue_side")
        except Exception as error:  # noqa: BLE001 - the blue side is tagged; say how to see it
            context.session.rollback()
            _, cause, _ = diagnose_exception(error, context.target.aws_profile_name, "")
            return self._fail(
                context,
                "record_blue_side",
                "refresh_after_action_failed",
                "The blue side is tagged, but the inventory could not be refreshed",
                cause,
                f"once AWS can be read again, run Refresh inventory on the {context.target.environment_slug} "
                "environment; the inbox then lists the blue side" + self._finish_by_hand(context),
            )
        logger.info(
            "major_upgrade_blue_side_recorded",
            operation_id=str(context.operation.id),
            blue_side_identifiers=blue_identifiers,
            snapshot_id=snapshot.id,
        )
        return StepOutcome(
            status=StepOutcomeStatus.COMPLETED,
            output={BLUE_SIDE_IDENTIFIERS_PLAN_KEY: blue_identifiers, "snapshot_id": snapshot.id},
        )

    def _finish_by_hand(self, context: StepContext) -> str:
        """Terraform mode, an instance: `; ` and the commands finishing the reconcile by hand, else empty."""
        if context.target.is_direct or self._is_cluster(context):
            return ""
        identifier = str(context.operation.plan["resource_identifier"])
        record = self._record(context)
        address = (record.terraform_address if record else None) or terraform_address_for(
            "aws_db_instance", identifier
        )
        blue_address = terraform_address_for("aws_db_instance", f"{identifier}{BLUE_SIDE_SUFFIX}")
        return "; " + switchover_finish_by_hand(context, address, blue_address, identifier)

    def _blue_side_mismatch(
        self, context: StepContext, described: dict[str, Any] | None, blue_arns: list[str]
    ) -> str | None:
        """Why the resources under `-old1` are not the blue side kept before the switchover, or None."""
        captured: dict[str, str] = context.operation.plan.get(BLUE_RESOURCE_IDS_PLAN_KEY) or {}
        identifier = str(context.operation.plan["resource_identifier"])
        expected = captured.get(identifier)
        if not expected or described is None:
            return None
        resource_id_key = "DbClusterResourceId" if self._is_cluster(context) else "DbiResourceId"
        found = described.get(resource_id_key)
        blue_identifier = blue_arns[0].rsplit(":", 1)[-1]
        if found != expected:
            return (
                f"{blue_identifier} has resource id {found}, but {identifier} had {expected} before the "
                "switchover"
            )
        for member_arn in blue_arns[1:]:
            member_identifier = member_arn.rsplit(":", 1)[-1]
            member = instance_describe(context, member_identifier, "record_blue_side") or {}
            if member.get("DbiResourceId") not in captured.values():
                return (
                    f"{member_identifier} has resource id {member.get('DbiResourceId')}, which no blue "
                    "member had before the switchover"
                )
        return None

    # ----- rejection -----------------------------------------------------------------------------------

    def on_cancelled(self, context: StepContext) -> bool:
        """After a rejection, leave the deployment in place and say so on the operation (Review Focus 5).

        Only `blue_green_cleanup` deletes a deployment; the inbox offers it on the cancelled operation.

        Returns:
            True: nothing is left for another console to clean.
        """
        deployment_id = context.operation.plan.get("deployment_id")
        if not deployment_id:
            return True
        green_endpoint = context.operation.plan.get("green_endpoint") or "its green side"
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name="on_cancelled",
            severity="warning",
            diagnostic_kind="blue_green_deployment_left_in_place",
            title="The blue/green deployment is left in place",
            detail=f"the upgrade was rejected; deployment {deployment_id} and its green side "
            f"({green_endpoint}) still run in AWS, and production is unchanged",
            fix_suggestion="remove the deployment with Clean up blue/green deployment (blue_green_cleanup) "
            "from the inbox or the resource page when it is no longer needed",
        )
        context.session.commit()
        return True
