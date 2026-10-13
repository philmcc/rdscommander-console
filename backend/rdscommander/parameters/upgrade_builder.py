"""Build parameter groups for a target engine version from the overrides on the current groups.

An engine upgrade moves an instance (and, on Aurora, its cluster) to a parameter group family it cannot
reuse its groups in. This module works out what the new groups should hold. It loads the target family's
metadata, collects every `user`-sourced parameter on the current instance group and, for Aurora, the
current cluster group, and classifies each against the target family:

* `carried`: the parameter exists in the target family and the value is acceptable there.
* `default_changed`: as `carried`, but the family default moved between the two families; the value is
  kept, and the operator is told the default is no longer the same.
* `needs_review`: the value is not acceptable in the target family (or the parameter changed type), so the
  operator decides; nothing is set unless the operator supplies a value.
* `removed`: the parameter does not exist in the target family; it is dropped.
* `renamed`: the parameter has a new name in the target family; the value moves to it.

The classification is what the review step shows, with editable values, and what is recorded as provenance
on the groups the operation creates.

The groups are created as one change, `UpgradeParameterGroups`, which wraps one `ParameterGroupCreate` per
group: the approver reviews every group in one change set (or one Terraform plan) and one commit records
them. If AWS fails part way, the groups already created keep their provenance rows, and a later build of the
same groups recognises them (same source group, same target family) and creates only the missing ones.

Example:
    >>> plans = build_upgrade_plan(  # doctest: +SKIP
    ...     session, console_config, cluster_record, "17.2"
    ... )
    >>> [(plan.scope, plan.new_group_name) for plan in plans]  # doctest: +SKIP
    [('instance', 'demo-aurora-pg17'), ('cluster', 'demo-aurora-pg16-tuned-pg17')]
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.aws.rds_reader import RdsReader
from rdscommander.config.models import ConsoleConfig
from rdscommander.control_db.models import ParameterGroupProvenance
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import (
    ChangeDiagnosticError,
    ChangeImpact,
    ChangeSetEntry,
    VerificationResult,
)
from rdscommander.operations.executor import StepContext
from rdscommander.parameters.changes import ParameterGroupCreate, group_name_is_taken
from rdscommander.parameters.metadata import (
    FamilyMetadata,
    family_for_version,
    load_engine_versions,
    load_family_metadata,
    validate_parameter_value,
)
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import find_parameter_group, list_resources
from rdscommander.terraform.render_input import RenderWorkspaceInput

logger = get_logger(__name__)

RENAMED_PARAMETERS: dict[tuple[str, str], str] = {
    ("force_parallel_mode", "postgres16"): "debug_parallel_query",
    ("force_parallel_mode", "aurora-postgresql16"): "debug_parallel_query",
    ("force_parallel_mode", "postgres17"): "debug_parallel_query",
    ("force_parallel_mode", "aurora-postgresql17"): "debug_parallel_query",
}
"""Documented renames between PostgreSQL majors: `(old parameter name, new family)` to the new name."""

MAXIMUM_GROUP_NAME_LENGTH = 255
"""The longest parameter group name AWS accepts."""

DEFAULT_GROUP_NAME_PREFIX = "default."
"""Every AWS default parameter group is named `default.<family>`."""

Outcome = Literal["carried", "default_changed", "needs_review", "removed", "renamed"]
Scope = Literal["instance", "cluster"]


class UpgradePlanError(ChangeDiagnosticError):
    """The groups for an upgrade cannot be worked out for this resource."""

    diagnostic_kind = "upgrade_plan_not_possible"
    title = "Parameter groups cannot be built for this upgrade"
    fix_suggestion = "correct the request, or build the groups from the page of each instance"


class UpgradeGroupsPartiallyCreated(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """AWS failed after some of the groups of a build were created; the others were not."""

    diagnostic_kind = "upgrade_groups_partially_created"
    title = "Some of the upgrade parameter groups were created and some were not"
    fix_suggestion = (
        "build the groups again: the groups already created are kept and only the missing ones are created"
    )


class Classification(BaseModel):
    """One override's fate in the target family.

    Attributes:
        name: The parameter's name on the current group.
        value: The value on the current group.
        outcome: What happens to it in the target family.
        new_name: The name in the target family when it differs from `name`.
        note: Why the outcome is what it is; empty for a plain carry.
        old_default: The parameter's default in the current family, when it had one.
        new_default: The parameter's default in the target family, when it has one.
    """

    name: str
    value: str
    outcome: Outcome
    new_name: str | None = None
    note: str = ""
    old_default: str | None = None
    new_default: str | None = None


class UpgradeGroupPlan(BaseModel):
    """One group to build.

    Attributes:
        scope: `instance` for an instance group, `cluster` for an Aurora cluster group.
        source_group_name: The group the overrides were read from (an AWS default group has none to carry).
        source_family: Family of the source group.
        target_family: Family of the new group.
        new_group_name: Name of the new group.
        classifications: Every override of the source group, classified.
        parameters_to_set: What the new group will hold unless the operator edits it on the review step.
        already_built: Whether the group exists already because an earlier build created it from the same
            source group for the same target family; the build then leaves it as it is and creates only
            the groups that are missing.
    """

    scope: Scope
    source_group_name: str
    source_family: str
    target_family: str
    new_group_name: str
    classifications: list[Classification]
    parameters_to_set: dict[str, str]
    already_built: bool = False


class UpgradePreview(BaseModel):
    """What the review step shows: the plans and the families they are built for."""

    plans: list[UpgradeGroupPlan]
    target_family_instance: str
    target_family_cluster: str | None


class UpgradeSource(BaseModel):
    """A group whose overrides are carried to the target family.

    Attributes:
        scope: `instance` or `cluster`.
        group_name: Name of the current group.
        group_family: Family of the current group.
        is_default: Whether it is an AWS default group, which holds no overrides.
        overrides: The `user`-sourced parameters of the current group, `{name: value}`.
    """

    scope: Scope
    group_name: str
    group_family: str
    is_default: bool
    overrides: dict[str, str] = Field(default_factory=dict)


def classify_overrides(
    overrides: dict[str, str], old_family: FamilyMetadata, new_family: FamilyMetadata
) -> list[Classification]:
    """Classify every override against the target family.

    Args:
        overrides: The `user`-sourced parameters of the current group, `{name: value}`.
        old_family: Metadata of the current group's family.
        new_family: Metadata of the target family.

    Returns:
        One classification per override, sorted by parameter name.

    Example:
        >>> classifications = classify_overrides({"work_mem": "65536"}, old, new)  # doctest: +SKIP
        >>> [(c.name, c.outcome) for c in classifications]  # doctest: +SKIP
        [('work_mem', 'carried')]
    """
    result: list[Classification] = []
    for name, value in sorted(overrides.items()):
        old = old_family.parameters.get(name)
        renamed_to = RENAMED_PARAMETERS.get((name, new_family.family))
        new = new_family.parameters.get(renamed_to or name)
        old_default = old.default_value if old else None
        if new is None:
            result.append(
                Classification(
                    name=name,
                    value=value,
                    outcome="removed",
                    note=f"{name} does not exist in {new_family.family_and_scope_label}; dropped",
                    old_default=old_default,
                )
            )
            continue
        problem = validate_parameter_value(new, value)
        if problem:
            result.append(
                Classification(
                    name=name,
                    value=value,
                    outcome="needs_review",
                    new_name=renamed_to,
                    note=f"{value} is not accepted in {new_family.family}: {problem}",
                    old_default=old_default,
                    new_default=new.default_value,
                )
            )
        elif old is not None and old.data_type != new.data_type:
            result.append(
                Classification(
                    name=name,
                    value=value,
                    outcome="needs_review",
                    new_name=renamed_to,
                    note=f"type changed from {old.data_type} to {new.data_type}",
                    old_default=old_default,
                    new_default=new.default_value,
                )
            )
        elif renamed_to:
            result.append(
                Classification(
                    name=name,
                    value=value,
                    outcome="renamed",
                    new_name=renamed_to,
                    note=f"{name} is {renamed_to} in {new_family.family}",
                    old_default=old_default,
                    new_default=new.default_value,
                )
            )
        elif old is not None and old.default_value != new.default_value:
            result.append(
                Classification(
                    name=name,
                    value=value,
                    outcome="default_changed",
                    note=(
                        f"default changed from {old.default_value} to {new.default_value}; your value is kept"
                    ),
                    old_default=old_default,
                    new_default=new.default_value,
                )
            )
        else:
            result.append(
                Classification(
                    name=name,
                    value=value,
                    outcome="carried",
                    old_default=old_default,
                    new_default=new.default_value,
                )
            )
    return result


def parameters_to_set(classifications: list[Classification]) -> dict[str, str]:
    """The overrides of the new group: `carried`, `default_changed` and `renamed` ones, under the new name.

    `needs_review` and `removed` parameters are left out; the operator may add a `needs_review` one back
    with a value that is valid in the target family.

    Args:
        classifications: The result of `classify_overrides`.

    Returns:
        `{parameter name in the target family: value}`.

    Example:
        >>> parameters_to_set([Classification(name="work_mem", value="8192", outcome="carried")])
        {'work_mem': '8192'}
    """
    parameters: dict[str, str] = {}
    for classification in classifications:
        if classification.outcome in ("carried", "default_changed"):
            parameters[classification.name] = classification.value
        elif classification.outcome == "renamed" and classification.new_name:
            parameters[classification.new_name] = classification.value
    return parameters


def new_group_name_for(
    source_group_name: str,
    source_is_default: bool,
    resource_identifier: str,
    target_family: str,
    suffix: str,
) -> str:
    """The name of the group built for `target_family`: `<base>-pg<major><suffix>`.

    The base is the source group's name. An AWS default group is shared by every database, so a group built
    from one is named after the resource instead.

    Args:
        source_group_name: Name of the group the overrides come from.
        source_is_default: Whether that group is an AWS default group.
        resource_identifier: Identifier of the instance or cluster the groups are built for.
        target_family: Family of the new group, e.g. `postgres17`.
        suffix: Optional text appended to the name.

    Returns:
        The name, at most 255 characters.

    Example:
        >>> new_group_name_for("default.aurora-postgresql16", True, "demo-aurora", "aurora-postgresql17", "")
        'demo-aurora-pg17'
    """
    major_version = re.search(r"(\d+)$", target_family)
    major_text = major_version.group(1) if major_version else ""
    base = resource_identifier if source_is_default else source_group_name
    return f"{base}-pg{major_text}{suffix}"[:MAXIMUM_GROUP_NAME_LENGTH]


def shared_instance_group_name(cluster_identifier: str, member_records: list[ResourceRecord]) -> str | None:
    """The one instance parameter group the members of a cluster use.

    Args:
        cluster_identifier: The cluster, for the message.
        member_records: The cluster's member instances.

    Returns:
        The group's name, or None when no member names one.

    Raises:
        UpgradePlanError: The members use different groups; one operation builds one instance group.

    Example:
        >>> shared_instance_group_name("demo-aurora", [])  # doctest: +SKIP
    """
    group_names = sorted(
        {
            group_name
            for member in member_records
            if (group_name := member.relationships.get("parameter_group_name"))
        }
    )
    if len(group_names) > 1:
        raise UpgradePlanError(
            f"the members of {cluster_identifier} use different instance parameter groups "
            f"({', '.join(group_names)}), and one operation builds one instance group",
            "build the groups from the page of each instance, so that each instance's group is carried "
            "on its own",
        )
    return group_names[0] if group_names else None


def _override_values(group: ResourceRecord | None) -> dict[str, str]:
    """The `user`-sourced parameters of a group record as `{name: value}`; empty for an AWS default group."""
    if group is None or group.facts.get("is_default"):
        return {}
    return {name: str(entry["value"]) for name, entry in group.facts.get("parameters", {}).items()}


def _source_for(
    session: Session,
    console_config: ConsoleConfig,
    record: ResourceRecord,
    scope: Scope,
    group_name: str,
) -> UpgradeSource:
    """The source group `group_name` of `scope`, read from the inventory.

    A group the inventory does not list (it should always list the group a resource points at) is treated as
    holding nothing, in the family the resource's engine version implies.
    """
    group = find_parameter_group(session, console_config, record.target_key, scope, group_name)
    engine = str(record.facts.get("engine", "postgres"))
    major_version = str(record.facts.get("engine_version", "")).split(".")[0]
    return UpgradeSource(
        scope=scope,
        group_name=group_name,
        group_family=group.facts["parameter_group_family"] if group else f"{engine}{major_version}",
        is_default=bool(group.facts.get("is_default"))
        if group
        else group_name.startswith(DEFAULT_GROUP_NAME_PREFIX),
        overrides=_override_values(group),
    )


def upgrade_sources(
    session: Session, console_config: ConsoleConfig, record: ResourceRecord
) -> list[UpgradeSource]:
    """The groups whose overrides an upgrade of `record` carries: instance group first, then cluster group.

    An instance has its instance group. A cluster has the cluster group, and the instance group its members
    share. Needs no metadata, so a request can be checked with it before any AWS call.

    Args:
        session: Control database session.
        console_config: Loaded console configuration.
        record: The instance or cluster the upgrade is for.

    Returns:
        One source per group to build, never empty.

    Raises:
        UpgradePlanError: The resource has no group, or a cluster's members use different instance groups.

    Example:
        >>> sources = upgrade_sources(session, console_config, cluster)  # doctest: +SKIP
        >>> [(source.scope, source.group_name) for source in sources]  # doctest: +SKIP
        [('instance', 'default.aurora-postgresql16'), ('cluster', 'demo-aurora-pg16-tuned')]
    """
    named_groups: list[tuple[Scope, str]] = []
    if record.kind == ResourceKind.CLUSTER:
        member_identifiers = set(record.relationships.get("member_identifiers", []))
        members = [
            member
            for member in list_resources(
                session, console_config, record.environment_slug, ResourceKind.INSTANCE
            )
            if member.target_key == record.target_key and member.identifier in member_identifiers
        ]
        instance_group_name = shared_instance_group_name(record.identifier, members)
        if instance_group_name:
            named_groups.append(("instance", instance_group_name))
        cluster_group_name = record.relationships.get("cluster_parameter_group_name")
        if cluster_group_name:
            named_groups.append(("cluster", cluster_group_name))
    else:
        instance_group_name = record.relationships.get("parameter_group_name")
        if instance_group_name:
            named_groups.append(("instance", instance_group_name))
    if not named_groups:
        raise UpgradePlanError(
            f"{record.identifier} uses no parameter group, so there is nothing to carry to a new family",
            "attach a parameter group first, or create the new groups from the family defaults",
        )
    return [
        _source_for(session, console_config, record, scope, group_name) for scope, group_name in named_groups
    ]


def build_upgrade_plan(
    session: Session,
    console_config: ConsoleConfig,
    record: ResourceRecord,
    target_engine_version: str,
    *,
    name_suffix: str = "",
    reader: RdsReader | None = None,
) -> list[UpgradeGroupPlan]:
    """Plans for the instance group and, for an Aurora cluster, the cluster group.

    Args:
        session: Control database session; the caller commits it (the family cache may have been written).
        console_config: Loaded console configuration.
        record: The instance or cluster the upgrade is for.
        target_engine_version: The version it will be upgraded to, e.g. `17.2`.
        name_suffix: Optional text appended to the names of the new groups.
        reader: Reader over the target's RDS client; required for a real account whose metadata is not
            cached, unused for the simulated account.

    Returns:
        One plan per group to build, instance group first.

    Raises:
        UpgradePlanError: The groups cannot be worked out (see `upgrade_sources`).
        UnknownParameterFamilyError: The current or the target family is unknown (a `KeyError`).

    Example:
        >>> plans = build_upgrade_plan(session, console_config, instance, "17.2")  # doctest: +SKIP
        >>> plans[0].target_family  # doctest: +SKIP
        'postgres17'
    """
    target = console_config.target(record.target_key)
    engine = str(record.facts.get("engine", "postgres"))
    logger.info(
        "upgrade_plan_build_started",
        resource_identifier=record.identifier,
        target_key=record.target_key,
        target_engine_version=target_engine_version,
    )
    versions = load_engine_versions(session, engine, reader, simulated=target.simulated)
    target_family = family_for_version(engine, target_engine_version, versions)
    plans: list[UpgradeGroupPlan] = []
    for source in upgrade_sources(session, console_config, record):
        # Each group is classified against the parameters of its own scope: an Aurora family lists other
        # parameters for instance groups than for cluster groups.
        new_family = load_family_metadata(
            session, target_family, reader, scope=source.scope, simulated=target.simulated
        )
        classifications = (
            classify_overrides(
                source.overrides,
                load_family_metadata(
                    session, source.group_family, reader, scope=source.scope, simulated=target.simulated
                ),
                new_family,
            )
            if source.overrides
            else []  # nothing to classify, so the current family need not be read
        )
        new_group_name = new_group_name_for(
            source.group_name, source.is_default, record.identifier, target_family, name_suffix
        )
        plans.append(
            UpgradeGroupPlan(
                scope=source.scope,
                source_group_name=source.group_name,
                source_family=source.group_family,
                target_family=target_family,
                new_group_name=new_group_name,
                classifications=classifications,
                parameters_to_set=parameters_to_set(classifications),
                already_built=group_name_is_taken(
                    session, console_config, record.target_key, source.scope, new_group_name
                )
                and was_built_from_same_source(
                    session, record.target_key, source.scope, new_group_name, target_family, source.group_name
                ),
            )
        )
    logger.info(
        "upgrade_plan_built",
        resource_identifier=record.identifier,
        target_family=target_family,
        groups=[plan.new_group_name for plan in plans],
        outcome_counts={
            outcome: sum(1 for plan in plans for c in plan.classifications if c.outcome == outcome)
            for outcome in ("carried", "default_changed", "needs_review", "removed", "renamed")
        },
    )
    return plans


def was_built_from_same_source(
    session: Session,
    target_key: str,
    scope: str,
    group_name: str,
    target_family: str,
    source_group_name: str,
) -> bool:
    """Whether the console built the group `group_name` from `source_group_name` for `target_family`.

    The newest provenance row of the group decides. A group like this that exists is the result of an
    earlier build of the same groups (typically one that failed part way), so a new build keeps it.

    Args:
        session: Control database session.
        target_key: The target the group is in.
        scope: `instance` or `cluster`.
        group_name: Name of the existing group.
        target_family: Family the new build would give the group.
        source_group_name: Group the new build would carry the overrides from.

    Returns:
        True when the newest provenance row names exactly this source and family.

    Example:
        >>> was_built_from_same_source(  # doctest: +SKIP
        ...     session, target_key, "cluster", "demo-aurora-pg16-tuned-pg17",
        ...     "aurora-postgresql17", "demo-aurora-pg16-tuned",
        ... )
        True
    """
    row = session.execute(
        select(ParameterGroupProvenance)
        .where(
            ParameterGroupProvenance.target_key == target_key,
            ParameterGroupProvenance.group_scope == scope,
            ParameterGroupProvenance.group_name == group_name,
        )
        .order_by(ParameterGroupProvenance.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (
        row is not None
        and row.target_family == target_family
        and list(row.source_group_names) == [source_group_name]
    )


def final_parameters(plan: UpgradeGroupPlan, edits: dict[str, dict[str, str]]) -> dict[str, str]:
    """What the new group holds: the classification's parameters, then the operator's edits on top.

    Args:
        plan: The group's plan.
        edits: The operator's edits from the review step, `{scope: {parameter name: value}}`.

    Returns:
        `{parameter name in the target family: value}`.

    Example:
        >>> final_parameters(plan_with_work_mem_8192, {"instance": {"work_mem": "4096"}})  # doctest: +SKIP
        {'work_mem': '4096'}
    """
    return {**plan.parameters_to_set, **edits.get(plan.scope, {})}


def record_group_provenance(
    session: Session,
    operation_id: uuid.UUID,
    target_key: str,
    plan: UpgradeGroupPlan,
    parameters: dict[str, str],
) -> bool:
    """Add the provenance row of a group this operation built, unless the operation recorded it already.

    The row names the source group, the target family and the classification of every override with the
    value the group finally holds (`final_value`). The caller commits.

    Args:
        session: Control database session.
        operation_id: The build operation.
        target_key: The target the group is in.
        plan: The group's plan.
        parameters: What the group holds, from `final_parameters`.

    Returns:
        True when a row was added, False when the operation had recorded the group already.

    Example:
        >>> parameters = {"work_mem": "8192"}
        >>> record_group_provenance(session, operation.id, target_key, plan, parameters)  # doctest: +SKIP
        True
    """
    recorded = session.execute(
        select(ParameterGroupProvenance.id).where(
            ParameterGroupProvenance.operation_id == operation_id,
            ParameterGroupProvenance.group_scope == plan.scope,
            ParameterGroupProvenance.group_name == plan.new_group_name,
        )
    ).first()
    if recorded is not None:
        return False
    session.add(
        ParameterGroupProvenance(
            operation_id=operation_id,
            target_key=target_key,
            group_name=plan.new_group_name,
            group_scope=plan.scope,
            target_family=plan.target_family,
            source_group_names=[plan.source_group_name],
            classification=[
                {
                    **classification.model_dump(),
                    "final_value": parameters.get(classification.new_name or classification.name),
                }
                for classification in plan.classifications
            ],
        )
    )
    logger.info(
        "upgrade_group_provenance_recorded",
        operation_id=str(operation_id),
        group_name=plan.new_group_name,
        group_scope=plan.scope,
        source_group_name=plan.source_group_name,
    )
    return True


@dataclass(frozen=True)
class PlannedGroup:
    """A group of a build: its plan and the creation that makes it.

    Attributes:
        plan: What the group is for and whether it exists already.
        change: The creation of the group; not run when `plan.already_built`.
    """

    plan: UpgradeGroupPlan
    change: ParameterGroupCreate


class UpgradeParameterGroups:
    """The creation of every group of an upgrade build, as one change for the change strategy.

    Wraps one `ParameterGroupCreate` per group to create, so that the approver reviews all of them together
    (one change set in direct mode, one Terraform plan in Terraform mode) and one commit records them. A
    group that already exists from an earlier build of the same groups (`plan.already_built`) is left alone;
    in direct mode it is still part of what the record is rebuilt for, because the build that created it
    may have failed before the record was written.

    Attributes:
        resource_arn: The instance or cluster the groups are built for; the change set belongs to it and so
            does the operation.
        summary: What the change does, for titles.
        groups: Every group of the build, in the order they are created.
    """

    def __init__(self, source_resource_arn: str, groups: list[PlannedGroup]) -> None:
        """Describe the groups; nothing is created until a strategy runs the change."""
        self.resource_arn = source_resource_arn
        self.groups = groups
        self.groups_to_create = [group for group in groups if not group.plan.already_built]
        self.summary = "create " + " and ".join(g.plan.new_group_name for g in self.groups_to_create)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """The groups to create; in direct mode also the ones that exist, which the record must describe.

        Terraform mode renders only the groups to create, and the record keeps what it has for the rest.
        """
        chosen = self.groups if context.target.is_direct else self.groups_to_create
        return {group.change.address for group in chosen}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the render input with every group to create added, one after the other."""
        for group in self.groups_to_create:
            render_input = group.change.apply_to_render_input(render_input)
        return render_input

    def expected_impact(self, context: StepContext) -> ChangeImpact:
        """A reboot is expected when any group sets a static parameter; the downtimes are joined."""
        impacts = [group.change.expected_impact(context) for group in self.groups_to_create]
        downtimes = dict.fromkeys(impact.downtime for impact in impacts if impact.downtime != "none")
        return ChangeImpact(
            requires_reboot=any(impact.requires_reboot for impact in impacts),
            downtime="; ".join(downtimes) or "none",
        )

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """Every group's entries, in the order the groups are created; the entries' addresses tell them apart.

        Raises:
            KeyError: A group to create exists in AWS already.
        """
        return [entry for group in self.groups_to_create for entry in group.change.change_set(context)]

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Create the groups in order, writing each group's provenance row as soon as the group exists.

        A failure after the first group leaves that group and its row in place, so that building the same
        groups again creates only the rest.

        Raises:
            UpgradeGroupsPartiallyCreated: AWS failed after at least one group was created.
        """
        submitted: dict[str, Any] = {}
        created: list[str] = []
        for group in self.groups_to_create:
            name = group.plan.new_group_name
            try:
                submitted[name] = group.change.apply_direct(
                    context, [entry for entry in entries if entry.address == group.change.address]
                )
                created.append(name)
                record_group_provenance(
                    context.session,
                    context.operation.id,
                    context.target.target_key,
                    group.plan,
                    group.change.parameters,
                )
                context.session.commit()
            except Exception as error:
                context.session.rollback()
                if not created:
                    raise
                missing = [
                    g.plan.new_group_name
                    for g in self.groups_to_create
                    if g.plan.new_group_name not in created
                ]
                logger.error(
                    "upgrade_groups_partially_created",
                    operation_id=str(context.operation.id),
                    created=created,
                    missing=missing,
                    error=str(error),
                    fix_suggestion=UpgradeGroupsPartiallyCreated.fix_suggestion,
                )
                raise UpgradeGroupsPartiallyCreated(
                    f"created {', '.join(created)}; creating {name} failed ({error}); not created: "
                    f"{', '.join(missing)}",
                    f"build the groups again: {', '.join(created)} is kept and only {', '.join(missing)} is "
                    f"created; if {name} exists in AWS with some parameters missing, delete it first",
                ) from error
        return submitted

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """Every created group exists and holds its parameters; the evidence is kept per group."""
        results = {
            group.plan.new_group_name: group.change.verify_direct(context, entries)
            for group in self.groups_to_create
        }
        return VerificationResult(
            ok=all(result.ok for result in results.values()),
            live={name: result.live for name, result in results.items()},
            pending=[attribute for result in results.values() for attribute in result.pending],
            detail="; ".join(f"{name}: {result.detail}" for name, result in results.items() if result.detail),
            terminal=any(result.terminal for result in results.values()),
        )
