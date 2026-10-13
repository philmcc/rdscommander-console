"""Desired changes for parameter groups, expressed once for both change strategies.

Each class is a `DesiredChange` (see `operations/change_models.py`): it says which Terraform addresses it
touches, how the render input looks with the change applied (Terraform mode), what would change in AWS
(the change set), how to make the change through the RDS API and how to read it back (direct mode).
Terraform addresses are always derived from the loaded inventory record or from the names in the request,
never from a value left behind by an earlier call, so a mode that never computes a change set works too.

Example:
    >>> change = ParameterGroupEdit(group_arn, {"work_mem": "65536"}, reset=[])  # doctest: +SKIP
    >>> [entry.attribute for entry in change.change_set(context)]  # doctest: +SKIP
    ['parameters.work_mem']
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from rdscommander.aws.rds_reader import RdsReader, SimulatorUnsupportedAction
from rdscommander.config.models import ConsoleConfig
from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import (
    ChangeDiagnosticError,
    ChangeImpact,
    ChangeSetEntry,
    VerificationResult,
)
from rdscommander.operations.executor import StepContext
from rdscommander.parameters import direct_api
from rdscommander.parameters.metadata import (
    FamilyMetadata,
    ParameterScope,
    ValidationProblem,
    load_family_metadata,
    validate_parameters,
)
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import find_parameter_group, load_resource
from rdscommander.terraform.hcl import to_hcl
from rdscommander.terraform.names import terraform_address_for, terraform_resource_name
from rdscommander.terraform.render_input import (
    RenderedParameter,
    RenderedParameterGroup,
    RenderWorkspaceInput,
)

logger = get_logger(__name__)

INSTANCE_GROUP_ARN_TYPE = "pg"
CLUSTER_GROUP_ARN_TYPE = "cluster-pg"
PENDING_REBOOT = "pending-reboot"
ATTACHED_GROUP_APPLIED_STATUSES = frozenset({PENDING_REBOOT, "in-sync"})
"""Parameter apply statuses of an instance whose newly attached group AWS has finished applying."""
ATTACHED_GROUP_FAILED_STATUS = "failed-to-apply"
"""Parameter apply status of an instance whose attached group AWS could not apply; waiting never ends it."""
CONSOLE_MANAGED_TAGS = {"rdscommander:managed": "true"}
"""Tags a group created by the console carries in AWS; Terraform's provider adds them on its own."""


class ParameterValidationError(ChangeDiagnosticError):
    """Parameter values or names that cannot be set; one problem per parameter.

    Attributes:
        problems: The individual problems, each naming its parameter.
    """

    diagnostic_kind = "parameter_validation_failed"
    title = "Some parameter values cannot be set"
    fix_suggestion = (
        "correct the parameters named in the detail (the family's allowed values are on the group's page), "
        "then create the operation again"
    )

    def __init__(self, problems: list[ValidationProblem]) -> None:
        """Keep the problems and word the message as one sentence per parameter."""
        self.problems = problems
        super().__init__("; ".join(problem.message for problem in problems))


class ParameterGroupInUse(ChangeDiagnosticError):  # noqa: N818 - the name states the condition, not a fault
    """A delete was refused because instances or clusters still use the group."""

    diagnostic_kind = "parameter_group_in_use"
    title = "The parameter group is still in use"
    fix_suggestion = (
        "attach the instances and clusters named in the detail to another group first, then delete"
    )


class ParameterGroupNotAttachable(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """The group cannot be attached to the instance or cluster; one problem per reason.

    Attributes:
        problems: The individual reasons, each naming the group.
    """

    diagnostic_kind = "parameter_group_not_attachable"
    title = "This parameter group cannot be attached"
    fix_suggestion = (
        "choose a group of the same family and scope that is in the inventory (refresh inventory if it was "
        "just created), then create the operation again"
    )

    def __init__(self, problems: list[str]) -> None:
        """Keep the problems and word the message as one sentence per reason."""
        self.problems = problems
        super().__init__("; ".join(problems))


class ParameterGroupAlreadyExists(ChangeDiagnosticError):  # noqa: N818 - the name states the condition
    """A create was refused because a group of that name and scope exists already."""

    diagnostic_kind = "parameter_group_exists"
    title = "A parameter group with this name exists already"
    fix_suggestion = "choose another name, or edit the existing group instead of creating it"


def _record(context: StepContext, arn: str) -> ResourceRecord:
    """The record of `arn` from the latest inventory snapshot.

    Raises:
        KeyError: The ARN is not in the latest snapshot.
    """
    record = load_resource(context.session, context.console_config, arn)
    if record is None:
        raise KeyError(f"{arn} is not in the latest inventory")
    return record


def _group_type(scope: str) -> str:
    """Terraform resource type of a parameter group of `scope`."""
    return "aws_rds_cluster_parameter_group" if scope == "cluster" else "aws_db_parameter_group"


def group_from_arn(arn: str) -> tuple[str, str]:
    """The name and scope of a parameter group from its ARN.

    Args:
        arn: `arn:aws:rds:<region>:<account>:pg:<name>` or `...:cluster-pg:<name>`.

    Returns:
        `(name, scope)` where scope is `instance` or `cluster`.

    Raises:
        ValueError: The ARN is not the ARN of a parameter group.

    Example:
        >>> group_from_arn("arn:aws:rds:eu-west-2:000000000000:cluster-pg:demo-aurora-pg16-tuned")
        ('demo-aurora-pg16-tuned', 'cluster')
    """
    parts = arn.split(":")
    if len(parts) >= 7 and parts[5] in (INSTANCE_GROUP_ARN_TYPE, CLUSTER_GROUP_ARN_TYPE):
        return parts[6], "cluster" if parts[5] == CLUSTER_GROUP_ARN_TYPE else "instance"
    raise ValueError(f"{arn} is not the ARN of a parameter group")


def reader_for(context: StepContext, step_name: str) -> RdsReader | None:
    """A reader over a recorded RDS client of the operation's target; None for the simulated account.

    Args:
        context: The running step's context; its target decides which account is read.
        step_name: Step the AWS calls are recorded under.

    Returns:
        The reader for a real account, whose metadata is read from AWS; None for the simulated account,
        whose metadata always comes from the bundle.

    Example:
        >>> reader_for(context, "build_plan")  # doctest: +SKIP
        <RdsReader object>
    """
    if context.target.simulated:
        return None
    return RdsReader(
        context.services.aws_sessions.client(
            context.target, "rds", operation_id=context.operation.id, step_name=step_name
        )
    )


def family_metadata_for(context: StepContext, family: str, scope: ParameterScope) -> FamilyMetadata:
    """The family's metadata through the operation's target: the bundle if simulated, else AWS (cached).

    Args:
        context: The running step's context; its target decides where the metadata comes from.
        family: Parameter group family, e.g. `postgres16`.
        scope: Scope of the group being worked on, `instance` or `cluster`; an Aurora family lists other
            parameters for instance groups than for cluster groups.

    Returns:
        The metadata of the family's `scope`.

    Raises:
        UnknownParameterFamilyError: Neither AWS nor the bundle knows the family (a `KeyError`).

    Example:
        >>> metadata = family_metadata_for(context, "postgres16", "instance")  # doctest: +SKIP
        >>> metadata.parameters["work_mem"].apply_type  # doctest: +SKIP
        'dynamic'
    """
    reader = reader_for(context, "family_metadata")
    metadata = load_family_metadata(
        context.session, family, reader, scope=scope, simulated=context.target.simulated
    )
    context.session.commit()
    return metadata


def parameter_problems(
    metadata: FamilyMetadata,
    parameters: dict[str, str],
    reset: Iterable[str] = (),
    *,
    check_unknown_names: bool = True,
) -> list[ValidationProblem]:
    """Everything wrong with a request to set `parameters` and reset `reset`.

    Args:
        metadata: The group's family.
        parameters: Values to set, by parameter name.
        reset: Parameters to return to the family default.
        check_unknown_names: Whether a name the metadata does not list is a problem. Turn it off when the
            metadata is only the bundle for a real account: the bundle lists a subset of what AWS has, so
            such a name is left for the executor to check against AWS. Values of known names are always
            checked.

    Returns:
        One problem per unknown or unmodifiable parameter, per unacceptable value and per parameter that is
        both set and reset; empty when the request is fine.

    Example:
        >>> [p.message for p in parameter_problems(metadata, {"work_mem": "abc"}, ["work_mem"])]
        ['work_mem must be an integer', 'work_mem cannot be both set and reset']
    """
    checked = (
        parameters
        if check_unknown_names
        else {k: v for k, v in parameters.items() if k in metadata.parameters}
    )
    problems = validate_parameters(metadata, checked)
    for name in reset:
        known = metadata.parameters.get(name)
        if known is None:
            if check_unknown_names:
                problems.append(
                    ValidationProblem(
                        name=name, message=f"{name} is not a parameter of {metadata.family_and_scope_label}"
                    )
                )
        elif not known.is_modifiable:
            problems.append(ValidationProblem(name=name, message=f"{name} cannot be modified"))
    for name in sorted(set(parameters) & set(reset)):
        problems.append(ValidationProblem(name=name, message=f"{name} cannot be both set and reset"))
    return problems


class ClonedParameters(BaseModel):
    """What cloning a group gives a new group.

    Attributes:
        source_name: Name of the group that was cloned.
        parameters: The overrides the new group starts with.
        dropped: Parameters of the source that the new group's family does not have, so they were left out.
        cross_family: Whether the source is of another family than the new group.
    """

    source_name: str
    parameters: dict[str, str]
    dropped: list[str]
    cross_family: bool


def clone_parameters(
    session: Session,
    console_config: ConsoleConfig,
    source_arn: str,
    scope: str,
    family: str,
    metadata: FamilyMetadata | None,
) -> ClonedParameters:
    """The user-set parameters of the group at `source_arn`, as a new group of `family` can take them.

    A clone of a group of the same family carries every override. Across families the overrides are filtered
    through the new family's metadata: parameters it does not have are dropped (and listed), the rest are
    carried; whether their values are valid in the new family is for `parameter_problems` to say.

    Args:
        session: Control database session.
        console_config: Loaded console configuration.
        source_arn: ARN of the group to clone.
        scope: Scope the new group will have; a clone keeps the scope of its source.
        family: Family the new group will have.
        metadata: The new family's metadata; None when it cannot say which parameters exist (the bundle for
            a real account), in which case nothing is dropped.

    Returns:
        The overrides to start from and the parameters that were dropped.

    Raises:
        KeyError: The source group is not in the latest inventory.
        ValueError: The source is not a parameter group, or is of the other scope.

    Example:
        >>> cloned = clone_parameters(  # doctest: +SKIP
        ...     session, console_config, postgres15_group_arn, "instance", "postgres16", family_metadata
        ... )
        >>> cloned.dropped  # doctest: +SKIP
        ['force_parallel_mode']
    """
    source = load_resource(session, console_config, source_arn)
    if source is None:
        raise KeyError(f"{source_arn} is not in the latest inventory")
    if source.kind != ResourceKind.PARAMETER_GROUP:
        raise ValueError(f"{source.identifier} is not a parameter group, so it cannot be cloned")
    source_scope = source.facts["group_scope"]
    if source_scope != scope:
        raise ValueError(
            f"{source.identifier} is a {source_scope} group; the new group is a {scope} group, "
            "and a clone keeps the scope of its source"
        )
    overrides = {name: str(entry["value"]) for name, entry in source.facts.get("parameters", {}).items()}
    cross_family = source.facts["parameter_group_family"] != family
    if not cross_family or metadata is None:
        return ClonedParameters(
            source_name=source.identifier, parameters=overrides, dropped=[], cross_family=cross_family
        )
    carried = {name: value for name, value in overrides.items() if name in metadata.parameters}
    return ClonedParameters(
        source_name=source.identifier,
        parameters=carried,
        dropped=sorted(set(overrides) - set(carried)),
        cross_family=True,
    )


def create_problems(
    metadata: FamilyMetadata,
    explicit_parameters: dict[str, str],
    cloned: ClonedParameters | None,
    *,
    check_unknown_names: bool = True,
) -> list[ValidationProblem]:
    """Everything wrong with the overrides of a new group.

    The operator's own values are always checked. A clone's values are checked only across families: within
    a family they are already in AWS, so they are valid there by definition.

    Args:
        metadata: The new group's family.
        explicit_parameters: The values the operator typed.
        cloned: The result of cloning, or None when the group does not start from a clone.
        check_unknown_names: See `parameter_problems`.

    Returns:
        One problem per parameter that cannot be set; empty when the overrides are fine.

    Example:
        >>> create_problems(metadata, {"work_mem": "abc"}, None)[0].message  # doctest: +SKIP
        'work_mem must be an integer'
    """
    carried = cloned.parameters if cloned is not None and cloned.cross_family else {}
    return parameter_problems(
        metadata, {**carried, **explicit_parameters}, check_unknown_names=check_unknown_names
    )


def group_name_is_taken(
    session: Session, console_config: ConsoleConfig, target_key: str, scope: str, name: str
) -> bool:
    """Whether the latest inventory of `target_key` already has a group of this scope and name.

    Args:
        session: Control database session.
        console_config: Loaded console configuration.
        target_key: The target the group would be created in.
        scope: `instance` or `cluster`.
        name: The proposed name.

    Returns:
        True when a group of that name and scope exists.

    Example:
        >>> group_name_is_taken(  # doctest: +SKIP
        ...     session, console_config, "sandbox/000000000000/eu-west-2", "instance", "demo-postgres16-tuned"
        ... )
        True
    """
    return find_parameter_group(session, console_config, target_key, scope, name) is not None


def attach_problems(
    session: Session, console_config: ConsoleConfig, resource: ResourceRecord, group_arn: str
) -> list[str]:
    """Why the group at `group_arn` cannot be attached to `resource`; empty when it can.

    The group must be in the inventory of the resource's own account and region, be of the scope the
    resource takes (instances take instance groups, clusters cluster groups) and be of the family of the
    group attached now, because RDS refuses a group of another family.

    Args:
        session: Control database session.
        console_config: Loaded console configuration.
        resource: The instance or cluster.
        group_arn: ARN of the group to attach.

    Returns:
        One sentence per reason, each naming the group.

    Example:
        >>> attach_problems(session, console_config, orders_record, postgres15_group_arn)  # doctest: +SKIP
        ['orders-pg15 is of family postgres15; demo-orders uses family postgres16 (demo-postgres16-tuned)...']
    """
    if resource.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        return [f"{resource.identifier} is a {resource.kind.value}; only instances and clusters have a group"]
    try:
        group_name, group_scope = group_from_arn(group_arn)
    except ValueError as error:
        return [str(error)]
    is_cluster = resource.kind == ResourceKind.CLUSTER
    needed_scope = "cluster" if is_cluster else "instance"
    if group_scope != needed_scope:
        needed_group = "a cluster group" if is_cluster else "an instance group"
        return [f"{group_name} is a {group_scope} group; {resource.identifier} needs {needed_group}"]
    group = load_resource(session, console_config, group_arn)
    if group is None:
        return [f"{group_name} is not in the latest inventory; refresh inventory first"]
    if group.target_key != resource.target_key:
        return [f"{group_name} is in {group.target_key}, not in {resource.target_key}"]
    current_name = resource.relationships.get(
        "cluster_parameter_group_name" if is_cluster else "parameter_group_name"
    )
    current = (
        find_parameter_group(session, console_config, resource.target_key, needed_scope, current_name)
        if current_name
        else None
    )
    if (
        current is not None
        and current.facts["parameter_group_family"] != group.facts["parameter_group_family"]
    ):
        return [
            f"{group_name} is of family {group.facts['parameter_group_family']}; {resource.identifier} uses "
            f"family {current.facts['parameter_group_family']} ({current_name}), and an attached group must "
            "be of the same family"
        ]
    return []


def _static_downtime(used_by: list[str]) -> str:
    """What a static parameter costs: a reboot of the instances using the group."""
    return f"a reboot of {', '.join(used_by)}" if used_by else "a reboot of every instance using the group"


def _is_static(metadata: FamilyMetadata, name: str) -> bool:
    """Whether the family says `name` needs a reboot to apply."""
    parameter = metadata.parameters.get(name)
    return parameter is not None and parameter.apply_type == "static"


def _entry(
    address: str, name: str, current: Any, desired: Any, metadata: FamilyMetadata, used_by: list[str]
) -> ChangeSetEntry:
    """A change-set entry for one parameter; static parameters are flagged as needing a reboot."""
    static = _is_static(metadata, name)
    return ChangeSetEntry(
        address=address,
        attribute=f"parameters.{name}",
        current=current,
        desired=desired,
        apply_type=PENDING_REBOOT if static else "immediate",
        requires_reboot=static,
        downtime=_static_downtime(used_by) if static else "none",
    )


def _attach_downtime(reboot: str) -> str:
    """What the approver should expect from attaching a group with the chosen reboot option."""
    if reboot == "now":
        return "a reboot now"
    return "none now; static parameters stay pending until the next reboot (reboot from the instance page)"


def _groups_of(render_input: RenderWorkspaceInput, scope: str) -> list[RenderedParameterGroup]:
    """The rendered parameter groups of `scope`."""
    return (
        render_input.cluster_parameter_groups
        if scope == "cluster"
        else render_input.instance_parameter_groups
    )


class ParameterGroupEdit:
    """Set and reset parameters on an existing group.

    Attributes:
        resource_arn: ARN of the group.
        parameters: Values to set, by parameter name.
        reset: Parameters to return to the family default.
        apply_methods: The apply method for parameters the group does not hold yet (Terraform mode);
            `pending-reboot` for any parameter missing here.
        summary: What the change does, for titles.
    """

    def __init__(
        self,
        resource_arn: str,
        parameters: dict[str, str],
        reset: list[str],
        apply_methods: dict[str, str] | None = None,
    ) -> None:
        """Describe the edit; nothing is read or changed until a strategy runs it."""
        self.resource_arn = resource_arn
        self.parameters = parameters
        self.reset = reset
        self.apply_methods = apply_methods or {}
        self.summary = f"set {len(parameters)} and reset {len(reset)} parameters"

    def _group(self, context: StepContext) -> tuple[ResourceRecord, str, str]:
        """The group's record, its scope and its Terraform address, from the loaded record."""
        record = _record(context, self.resource_arn)
        scope = record.facts["group_scope"]
        return record, scope, terraform_address_for(_group_type(scope), record.identifier)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """Only the group itself."""
        return {self._group(context)[2]}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the group with the parameters set and reset (a copy of `render_input`)."""
        updated = render_input.model_copy(deep=True)
        name, scope = group_from_arn(self.resource_arn)
        for group in _groups_of(updated, scope):
            if group.name != name:
                continue
            kept = [p for p in group.parameters if p.name not in self.reset and p.name not in self.parameters]
            for parameter_name, value in self.parameters.items():
                existing = next((p for p in group.parameters if p.name == parameter_name), None)
                method = (
                    existing.apply_method
                    if existing
                    else self.apply_methods.get(parameter_name, PENDING_REBOOT)
                )
                kept.append(RenderedParameter(name=parameter_name, value=value, apply_method=method))
            group.parameters = sorted(kept, key=lambda parameter: parameter.name)
        return updated

    def expected_impact(self, context: StepContext) -> ChangeImpact:
        """Whether the edit needs a reboot, from the recorded parameters and the family (no AWS call).

        A parameter counts when its value would change from what the latest inventory holds (or, for a
        reset, when it is user-set now) and the family says it is static.
        """
        record, scope, _ = self._group(context)
        metadata = family_metadata_for(context, record.facts["parameter_group_family"], scope)
        recorded = {name: entry["value"] for name, entry in record.facts.get("parameters", {}).items()}
        changed = [name for name, value in self.parameters.items() if recorded.get(name) != value]
        changed += [name for name in self.reset if name in recorded]
        if not any(_is_static(metadata, name) for name in changed):
            return ChangeImpact(requires_reboot=False, downtime="none")
        return ChangeImpact(requires_reboot=True, downtime=_static_downtime(record.facts.get("used_by", [])))

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """What differs from the group as AWS has it now; static parameters need a reboot."""
        record, scope, address = self._group(context)
        metadata = family_metadata_for(context, record.facts["parameter_group_family"], scope)
        current = direct_api.group_parameters(context, record.identifier, scope, "compute_change_set")
        used_by = record.facts.get("used_by", [])
        entries = [
            _entry(address, name, (current.get(name) or {}).get("value"), value, metadata, used_by)
            for name, value in self.parameters.items()
            if (current.get(name) or {}).get("value") != value
        ]
        entries += [
            _entry(address, name, current[name]["value"], None, metadata, used_by)
            for name in self.reset
            if name in current
        ]
        return entries

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Set the values and reset the rest.

        The simulated account cannot reset a parameter; there the reset parameters are set to the family
        default instead and a `simulator_unsupported` warning says so. A real account always resets.
        """
        record, scope, _ = self._group(context)
        metadata = family_metadata_for(context, record.facts["parameter_group_family"], scope)
        to_set = {
            e.attribute.removeprefix("parameters."): e.desired for e in entries if e.desired is not None
        }
        to_reset = [e.attribute.removeprefix("parameters.") for e in entries if e.desired is None]
        if to_set:
            direct_api.modify_group(context, record.identifier, scope, to_set, metadata)
        if to_reset:
            try:
                direct_api.reset_group(context, record.identifier, scope, to_reset, metadata)
            except SimulatorUnsupportedAction:
                if not context.target.simulated:
                    raise
                self._reset_by_setting_defaults(context, record.identifier, scope, to_reset, metadata)
        return {"set": to_set, "reset": to_reset}

    def _reset_by_setting_defaults(
        self, context: StepContext, group_name: str, scope: str, names: list[str], metadata: FamilyMetadata
    ) -> None:
        """Stand in for the reset call the simulator lacks: write each family default as the value."""
        defaults = {
            name: metadata.parameters[name].default_value
            for name in names
            if name in metadata.parameters and metadata.parameters[name].default_value is not None
        }
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name="apply_direct",
            severity="warning",
            diagnostic_kind="simulator_unsupported",
            title="The simulated account cannot reset parameters; family defaults were set instead",
            detail=(
                f"reset_db_parameter_group is not implemented by the simulator, so {', '.join(names)} "
                f"on {group_name} were set to their family defaults and stay user-set values there"
            ),
            fix_suggestion="nothing to do in the simulator; a real account resets the parameters for real",
        )
        context.session.commit()
        if defaults:
            direct_api.modify_group(context, group_name, scope, defaults, metadata)

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """Read the group back; a reset holds when the parameter is unset or at its default."""
        record, scope, _ = self._group(context)
        metadata = family_metadata_for(context, record.facts["parameter_group_family"], scope)
        live = direct_api.group_parameters(context, record.identifier, scope, "verify_direct")
        live_values: dict[str, Any] = {}
        mismatched: list[str] = []
        for entry in entries:
            name = entry.attribute.removeprefix("parameters.")
            value = (live.get(name) or {}).get("value")
            live_values[entry.attribute] = value
            if entry.desired is not None:
                held = value == entry.desired
            else:
                default = metadata.parameters[name].default_value if name in metadata.parameters else None
                held = value is None or value == default
            if not held:
                mismatched.append(entry.attribute)
        return VerificationResult(
            ok=not mismatched, live=live_values, pending=[], detail=", ".join(mismatched)
        )


class ParameterGroupCreate:
    """A new group with its parameter overrides.

    The overrides are complete: a clone's parameters are merged in by the executor before the change is
    built (see `clone_parameters`), so every strategy renders and creates the same group.

    Attributes:
        resource_arn: ARN of the new group; empty until the group has been created.
        address: Terraform address the group will have.
        dropped_parameters: Parameters of a cloned group's other family that the new family lacks, so they
            were left out (the executor warns about them).
        clone_source: Name of the cloned group, if any.
        link_operation_to_group: Whether creating the group points the operation's `resource_arn` at it. A
            create operation is about its group, so it does. A change that creates groups on behalf of
            another resource (the upgrade build) leaves the operation linked to that resource.
        summary: What the change does, for titles.
    """

    def __init__(
        self,
        target_key: str,
        name: str,
        scope: str,
        family: str,
        description: str,
        parameters: dict[str, str],
        apply_methods: dict[str, str] | None = None,
        dropped_parameters: list[str] | None = None,
        clone_source: str | None = None,
        link_operation_to_group: bool = True,
    ) -> None:
        """Describe the new group; nothing is created until a strategy runs the change."""
        self.target_key = target_key
        self.name = name
        self.scope = scope
        self.family = family
        self.description = description
        self.parameters = parameters
        self.apply_methods = apply_methods or {}
        self.dropped_parameters = dropped_parameters or []
        self.clone_source = clone_source
        self.link_operation_to_group = link_operation_to_group
        self.resource_arn = ""
        self.summary = f"create {name}"
        self.address = terraform_address_for(_group_type(scope), name)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """Only the new group."""
        return {self.address}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the render input with the new group added (a copy)."""
        updated = render_input.model_copy(deep=True)
        groups = _groups_of(updated, self.scope)
        groups[:] = [group for group in groups if group.name != self.name]
        parameters = [
            RenderedParameter(
                name=name, value=value, apply_method=self.apply_methods.get(name, PENDING_REBOOT)
            )
            for name, value in sorted(self.parameters.items())
        ]
        groups.append(
            RenderedParameterGroup(
                terraform_name=terraform_resource_name(self.name),
                name=self.name,
                family=self.family,
                description=self.description,
                parameters=parameters,
                tags={},
            )
        )
        groups.sort(key=lambda group: group.name)
        return updated

    def expected_impact(self, context: StepContext) -> ChangeImpact:
        """Whether the overrides include a static parameter (family metadata only, no AWS call)."""
        metadata = family_metadata_for(context, self.family, self.scope)
        if not any(_is_static(metadata, name) for name in self.parameters):
            return ChangeImpact(requires_reboot=False, downtime="none")
        return ChangeImpact(requires_reboot=True, downtime=_static_downtime([]))

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """Every override as an entry (one `presence` entry when there are none).

        Raises:
            KeyError: A group of that name already exists in AWS.
        """
        metadata = family_metadata_for(context, self.family, self.scope)
        if direct_api.group_exists(context, self.name, self.scope, "compute_change_set"):
            raise KeyError(f"parameter group {self.name} already exists")
        if not self.parameters:
            return [
                ChangeSetEntry(
                    address=self.address,
                    attribute="presence",
                    current=None,
                    desired=f"{self.family} group",
                    apply_type="immediate",
                    requires_reboot=False,
                    downtime="none",
                )
            ]
        return [
            _entry(self.address, name, None, value, metadata, [])
            for name, value in sorted(self.parameters.items())
        ]

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Create the group and set its overrides; the operation is linked to it unless told otherwise."""
        metadata = family_metadata_for(context, self.family, self.scope)
        self.resource_arn = direct_api.create_group(
            context, self.name, self.scope, self.family, self.description, CONSOLE_MANAGED_TAGS
        )
        if self.link_operation_to_group:
            context.operation.resource_arn = self.resource_arn
            context.session.commit()
        if self.parameters:
            direct_api.modify_group(context, self.name, self.scope, self.parameters, metadata)
        return {"arn": self.resource_arn, "overrides": self.parameters}

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """The group exists and holds every override."""
        live = direct_api.group_parameters(context, self.name, self.scope, "verify_direct")
        mismatched = [
            name for name, value in self.parameters.items() if (live.get(name) or {}).get("value") != value
        ]
        exists = direct_api.group_exists(context, self.name, self.scope, "verify_direct")
        detail = ", ".join(mismatched) if exists else f"{self.name} does not exist"
        return VerificationResult(
            ok=not mismatched and exists,
            live={f"parameters.{name}": (live.get(name) or {}).get("value") for name in self.parameters},
            pending=[],
            detail=detail,
        )


class ParameterGroupAttach:
    """Point an instance or a cluster at another parameter group.

    The group must be in the inventory, of the scope the resource takes and of the family of the group
    attached now (`attach_problems`); both strategies check that before anyone is asked to approve.

    Attributes:
        resource_arn: ARN of the instance or cluster.
        parameter_group_arn: ARN of the group to attach.
        reboot: `none` leaves static parameters pending until the next reboot, `now` reboots after the
            attachment.
        summary: What the change does, for titles.
    """

    def __init__(self, resource_arn: str, parameter_group_arn: str, reboot: str) -> None:
        """Describe the attachment; nothing is read or changed until a strategy runs it."""
        self.resource_arn = resource_arn
        self.parameter_group_arn = parameter_group_arn
        self.reboot = reboot
        self.summary = "attach parameter group"

    def _resource(self, context: StepContext) -> tuple[ResourceRecord, bool, str]:
        """The instance or cluster record, whether it is a cluster and its Terraform address."""
        record = _record(context, self.resource_arn)
        if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
            raise ValueError(
                f"{record.identifier} is a {record.kind.value}; only instances and clusters have a group"
            )
        is_cluster = record.kind == ResourceKind.CLUSTER
        if is_cluster:
            resource_type = "aws_rds_cluster"
        elif record.relationships.get("cluster_identifier"):
            resource_type = "aws_rds_cluster_instance"
        else:
            resource_type = "aws_db_instance"
        return record, is_cluster, terraform_address_for(resource_type, record.identifier)

    def ensure_attachable(self, context: StepContext) -> None:
        """Refuse a group that is not in the inventory, of the other scope or of another family.

        Raises:
            KeyError: The instance or cluster is not in the latest inventory.
            ParameterGroupNotAttachable: The group cannot be attached; the message names every reason.
        """
        record = _record(context, self.resource_arn)
        problems = attach_problems(context.session, context.console_config, record, self.parameter_group_arn)
        if problems:
            raise ParameterGroupNotAttachable(problems)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """The instance or cluster, plus (direct mode) a group the record does not hold yet.

        A direct-mode change records the touched resources from a fresh rendering of the estate. The
        instance is then rendered with a reference to the group, so the group has to be recorded with it, or
        the committed files would point at a group they never declare.
        """
        addresses = {self._resource(context)[2]}
        if context.target.is_direct and self._record_lacks_group(context):
            group_name, group_scope = group_from_arn(self.parameter_group_arn)
            addresses.add(terraform_address_for(_group_type(group_scope), group_name))
        return addresses

    def _record_lacks_group(self, context: StepContext) -> bool:
        """Whether the committed record exists, has no such group, and the group is not an AWS default."""
        recorded = direct_api.recorded_render_input(context)
        group_name, group_scope = group_from_arn(self.parameter_group_arn)
        if recorded is None or any(group.name == group_name for group in _groups_of(recorded, group_scope)):
            return False
        group = load_resource(context.session, context.console_config, self.parameter_group_arn)
        return group is not None and not group.facts.get("is_default")

    def expected_impact(self, context: StepContext) -> ChangeImpact:
        """A reboot only when the attachment chains one; otherwise static parameters wait."""
        return ChangeImpact(requires_reboot=self.reboot == "now", downtime=_attach_downtime(self.reboot))

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the resource points at the group (a copy of `render_input`).

        A group rendered in the same workspace is referenced; any other is written as a quoted name.
        """
        updated = render_input.model_copy(deep=True)
        identifier = self.resource_arn.rsplit(":", 1)[-1]
        group_name, group_scope = group_from_arn(self.parameter_group_arn)
        rendered = any(group.name == group_name for group in _groups_of(updated, group_scope))
        reference = (
            f"{_group_type(group_scope)}.{terraform_resource_name(group_name)}.name"
            if rendered
            else to_hcl(group_name)
        )
        if group_scope == "cluster":
            for cluster in updated.clusters:
                if cluster.identifier == identifier:
                    cluster.cluster_parameter_group_reference = reference
        else:
            for instance in [*updated.instances, *updated.cluster_instances]:
                if instance.identifier == identifier:
                    instance.parameter_group_reference = reference
        return updated

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """The one attribute that points at the group; empty when the group is attached already.

        Raises:
            ParameterGroupNotAttachable: The group is not in the inventory, is of the other scope or of
                another family.
            KeyError: The group does not exist in AWS any more.
        """
        self.ensure_attachable(context)
        record, is_cluster, address = self._resource(context)
        group_name, group_scope = group_from_arn(self.parameter_group_arn)
        if not direct_api.group_exists(context, group_name, group_scope, "compute_change_set"):
            raise KeyError(f"parameter group {group_name} does not exist")
        current, _ = direct_api.attached_group(context, record.identifier, is_cluster, "compute_change_set")
        if current == group_name:
            return []
        attribute = "cluster_parameter_group_name" if is_cluster else "parameter_group_name"
        return [
            ChangeSetEntry(
                address=address,
                attribute=attribute,
                current=current,
                desired=group_name,
                apply_type=PENDING_REBOOT,
                requires_reboot=self.reboot == "now",
                downtime=_attach_downtime(self.reboot),
            )
        ]

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Point the instance or cluster at the group."""
        record, is_cluster, _ = self._resource(context)
        group_name, _ = group_from_arn(self.parameter_group_arn)
        direct_api.attach_group(context, record.identifier, is_cluster, group_name)
        return {"group": group_name}

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """AWS reports the group as the one attached, and has finished applying it.

        AWS names the new group at once, with `ParameterApplyStatus: applying`. Until the status is
        `pending-reboot` or `in-sync` the attachment is not verified (`ok` is False with nothing pending), so
        the base keeps reading; the instance's pending reboot is then known when the operation records it.
        `failed-to-apply` never becomes either by waiting, so it is terminal: the base stops reading at
        once and the detail names the status. A cluster reports no apply status.
        """
        record, is_cluster, _ = self._resource(context)
        group_name, _ = group_from_arn(self.parameter_group_arn)
        current, status = direct_api.attached_group(context, record.identifier, is_cluster)
        applied = status is None or status in ATTACHED_GROUP_APPLIED_STATUSES
        failed_to_apply = current == group_name and status == ATTACHED_GROUP_FAILED_STATUS
        detail = f"attached group is {current}"
        if failed_to_apply:
            detail = (
                f"{detail}, which AWS could not apply to {record.identifier} (parameter apply status "
                f"{status}); waiting will not change that"
            )
            logger.warning(
                "parameter_group_attach_failed_to_apply",
                resource_identifier=record.identifier,
                parameter_group_name=group_name,
                parameter_apply_status=status,
                fix_suggestion=(
                    "read the instance's events in AWS for why the group could not be applied (often a "
                    "parameter value the instance class or engine rejects), fix the group, then attach again"
                ),
            )
        elif current == group_name and not applied:
            detail = f"{detail}, which AWS is still applying (parameter apply status {status})"
        return VerificationResult(
            ok=current == group_name and applied,
            live={"group": current, "apply_status": status},
            pending=[],
            detail=detail,
            terminal=failed_to_apply,
        )


class ParameterGroupDelete:
    """Remove a group nothing uses.

    Attributes:
        resource_arn: ARN of the group.
        summary: What the change does, for titles.
    """

    def __init__(self, resource_arn: str) -> None:
        """Describe the delete; nothing is changed until a strategy runs it."""
        self.resource_arn = resource_arn
        self.summary = "delete parameter group"

    def _group(self, context: StepContext) -> tuple[ResourceRecord, str, str]:
        """The group's record, its scope and its Terraform address, from the loaded record."""
        record = _record(context, self.resource_arn)
        scope = record.facts["group_scope"]
        return record, scope, terraform_address_for(_group_type(scope), record.identifier)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """Only the group itself."""
        return {self._group(context)[2]}

    def ensure_unused(self, context: StepContext) -> ResourceRecord:
        """The group's record, provided no instance or cluster uses the group.

        Raises:
            KeyError: The group is not in the latest inventory.
            ParameterGroupInUse: Instances or clusters use the group.
        """
        record, _, _ = self._group(context)
        if record.facts.get("used_by"):
            raise ParameterGroupInUse(f"{record.identifier} is used by {', '.join(record.facts['used_by'])}")
        return record

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the render input without the group (a copy)."""
        updated = render_input.model_copy(deep=True)
        name, scope = group_from_arn(self.resource_arn)
        groups = _groups_of(updated, scope)
        groups[:] = [group for group in groups if group.name != name]
        return updated

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """The group's presence, from something to nothing.

        Raises:
            ParameterGroupInUse: Instances or clusters use the group.
        """
        record = self.ensure_unused(context)
        address = terraform_address_for(_group_type(record.facts["group_scope"]), record.identifier)
        return [
            ChangeSetEntry(
                address=address,
                attribute="presence",
                current=f"{record.facts['parameter_group_family']} group",
                desired=None,
                apply_type="immediate",
                requires_reboot=False,
                downtime="none",
            )
        ]

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Delete the group."""
        record, scope, _ = self._group(context)
        direct_api.delete_group(context, record.identifier, scope)
        return {"deleted": record.identifier}

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """The group is gone."""
        record, scope, _ = self._group(context)
        exists = direct_api.group_exists(context, record.identifier, scope, "verify_direct")
        return VerificationResult(
            ok=not exists, live={"exists": exists}, pending=[], detail="group still exists" if exists else ""
        )
