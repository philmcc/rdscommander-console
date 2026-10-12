"""One desired change for every operation that sets attributes on an instance, member or cluster.

An `AttributeChange` knows the attribute values an operation wants on one rendered resource. Terraform mode
writes them into the rendered model; direct mode turns them into a change set from the live values, writes
them through the RDS API and reads them back. Tags are attributes named `tags.<key>`.

Example:
    >>> change = AttributeChange(arn, "aws_db_instance.demo_orders", {"multi_az": True}, "enable Multi-AZ")
    >>> change.touched_addresses(context)
    {'aws_db_instance.demo_orders'}
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from rdscommander.diagnostics.service import record_diagnostic
from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import (
    ChangeDiagnosticError,
    ChangeImpact,
    ChangeSetEntry,
    VerificationResult,
)
from rdscommander.operations.change_strategy import ResourceChangeExecutor
from rdscommander.operations.executor import StepContext
from rdscommander.parameters.direct_api import (
    ATTRIBUTE_APPLY_PROFILES,
    MODIFIABLE_ATTRIBUTES_BY_TYPE,
    NO_DOWNTIME,
    PENDING_WINDOW_DOWNTIME,
    TAG_APPLY_PROFILE,
    TERMINAL_RESOURCE_STATUSES,
    AttributeApplyProfile,
    apply_attribute_values,
    apply_profile_for_values,
    downtime_states_no_interruption,
    queued_attribute_values,
    read_attribute_values,
    read_pending_modifications,
    read_resource_status,
)
from rdscommander.resources.model import ResourceRecord, terraform_resource_type_for
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.names import terraform_address_for
from rdscommander.terraform.record import RESOURCE_SECTIONS
from rdscommander.terraform.render_input import RenderWorkspaceInput

logger = get_logger(__name__)

TAG_PREFIX = "tags."
RENDER_ONLY_ATTRIBUTES: frozenset[str] = frozenset({"final_snapshot_identifier"})
"""Attributes of the rendered model the API cannot write: an `AttributeChange` may carry them, the rendered
model takes them, and direct mode neither compares, submits nor verifies them. `final_snapshot_identifier`
is read by Terraform from its state when it deletes the resource; direct mode passes the name to the
delete call instead."""
ABSENT_MEANS_ZERO_ATTRIBUTES: frozenset[str] = frozenset({"max_allocated_storage"})
"""Attributes AWS stops reporting once they are switched off with 0: a live None equals a desired 0."""
COMPANION_ATTRIBUTE_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"serverless_v2_min_capacity", "serverless_v2_max_capacity"}),
    frozenset({"monitoring_interval", "monitoring_role_arn"}),
)
"""Attributes AWS takes only together: when one of a group differs from live, every one of the group the
change carries stays in the change set, so the request is complete (`ServerlessV2ScalingConfiguration`
refuses a MinCapacity without its MaxCapacity and the other way round; a non-zero `MonitoringInterval`
goes with its `MonitoringRoleArn`, which the review then shows)."""
PROTECTED_TAG_PREFIXES = ("rdscommander:", "aws:")
APPLY_NOW = "now"
APPLY_AT_WINDOW = "maintenance_window"
APPLY_AT_VALUES = (APPLY_NOW, APPLY_AT_WINDOW)


class AttributeNotChangeableError(ChangeDiagnosticError):
    """The operation asked for an attribute the console cannot set on this resource type."""

    diagnostic_kind = "attribute_not_changeable"
    title = "That attribute cannot be changed here"
    fix_suggestion = "change it in AWS or in the tooling that owns the resource, then refresh inventory"


class WindowApplyNotAvailableInTerraformMode(ChangeDiagnosticError):  # noqa: N818 - the name is part of the contract
    """The operation asked to apply at the maintenance window, which only direct mode can do."""

    diagnostic_kind = "apply_at_window_not_available"
    title = "The maintenance window cannot be chosen in Terraform mode"
    fix_suggestion = (
        "choose Apply: Now; Terraform mode always applies immediately, "
        "or switch the environment to direct mode"
    )


def _comparable_live_value(attribute: str, value: Any) -> Any:
    """The live value as compared with a desired one: an absent switched-off attribute reads as 0."""
    return 0 if value is None and attribute in ABSENT_MEANS_ZERO_ATTRIBUTES else value


def rendered_field_for(address: str, attribute: str) -> str:
    """The rendered model field an attribute lives in: itself, or `tags` for a `tags.<key>` attribute.

    Args:
        address: Terraform address of the resource (every rendered type keeps its tags in `tags`).
        attribute: Attribute name, or `tags.<key>`.

    Returns:
        The field name on the rendered resource.

    Example:
        >>> rendered_field_for("aws_db_instance.x", "tags.team")
        'tags'
    """
    return "tags" if attribute.startswith(TAG_PREFIX) else attribute


def _resource_type(address: str) -> str:
    return address.partition(".")[0]


class AttributeChange:
    """Set attribute values on one rendered resource.

    Attributes:
        resource_arn: ARN of the resource.
        address: Its Terraform address; the type picks the writable attributes and the rendered list.
        values: `{attribute: value}`; `tags.<key>` entries set a tag, and a value of None removes it.
            Tags are allowed on every rendered resource type, other attributes only where the API writes
            them (`MODIFIABLE_ATTRIBUTES_BY_TYPE`). An attribute named in `RENDER_ONLY_ATTRIBUTES` is also
            accepted: the rendered model takes it, but direct mode never compares, writes or verifies it.
        summary: What the change does, for titles.
        apply_at: `now` (ApplyImmediately) or `maintenance_window` (direct mode only; entries are
            `pending-window` and verification accepts them as queued).
        known_current_values: `{attribute: value}` the latest inventory holds, for the Terraform-mode
            review (`expected_impact`), which reads nothing from AWS; an attribute left out is unknown.
            Direct mode reads the live values instead.

    Raises:
        ValueError: `apply_at` is neither `now` nor `maintenance_window`, or a tag is empty or has a
            protected prefix.
        AttributeNotChangeableError: An attribute cannot be written on this resource type.

    Example:
        >>> AttributeChange(arn, "aws_db_instance.x", {"tags.team": "orders"}, "tag").values
        {'tags.team': 'orders'}
    """

    def __init__(
        self,
        resource_arn: str,
        address: str,
        values: dict[str, Any],
        summary: str,
        apply_at: str = APPLY_NOW,
        known_current_values: dict[str, Any] | None = None,
    ) -> None:
        if apply_at not in APPLY_AT_VALUES:
            raise ValueError(f"apply_at must be '{APPLY_NOW}' or '{APPLY_AT_WINDOW}', not '{apply_at}'")
        resource_type = _resource_type(address)
        writable = MODIFIABLE_ATTRIBUTES_BY_TYPE.get(resource_type, {})
        for attribute in values:
            if attribute.startswith(TAG_PREFIX):
                key = attribute[len(TAG_PREFIX) :]
                if not key or key.startswith(PROTECTED_TAG_PREFIXES):
                    self._refuse(address, attribute, "a protected or empty tag key")
                    raise ValueError(
                        f"the tag {key or '(empty)'} is managed by the console or AWS and cannot be changed"
                        if key
                        else "a tag needs a key"
                    )
                if resource_type not in RESOURCE_SECTIONS:
                    self._refuse(address, attribute, "an unknown resource type")
                    raise AttributeNotChangeableError(
                        f"{attribute} cannot be changed on {resource_type} resources"
                    )
            elif attribute not in writable and attribute not in RENDER_ONLY_ATTRIBUTES:
                self._refuse(address, attribute, "not writable on this resource type")
                raise AttributeNotChangeableError(
                    f"{attribute} cannot be changed on {resource_type} resources"
                )
        self.resource_arn = resource_arn
        self.address = address
        self.values = dict(values)
        self.summary = summary
        self.apply_at = apply_at
        self.known_current_values = dict(known_current_values or {})

    @staticmethod
    def _refuse(address: str, attribute: str, reason: str) -> None:
        logger.error(
            "attribute_change_refused",
            address=address,
            attribute=attribute,
            reason=reason,
            fix_suggestion="change the attribute in AWS or in the tooling that owns the resource",
        )

    @property
    def _api_values(self) -> dict[str, Any]:
        """The values the API can write and read: everything except the render-only attributes."""
        return {a: v for a, v in self.values.items() if a not in RENDER_ONLY_ATTRIBUTES}

    def touched_addresses(self, context: StepContext) -> set[str]:
        """Only this resource."""
        return {self.address}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """Terraform mode: the rendered resource with the new values (a copy of the input).

        Raises:
            KeyError: The resource is not in the rendered input.
        """
        updated = render_input.model_copy(deep=True)
        section = RESOURCE_SECTIONS[_resource_type(self.address)]
        terraform_name = self.address.partition(".")[2]
        resource = next((r for r in getattr(updated, section) if r.terraform_name == terraform_name), None)
        if resource is None:
            logger.error(
                "attribute_change_resource_not_rendered",
                address=self.address,
                fix_suggestion="refresh inventory so the resource is in the recorded input",
            )
            raise KeyError(f"{self.address} is not in the rendered input")
        for attribute, value in self.values.items():
            field = rendered_field_for(self.address, attribute)
            if field == "tags":
                key = attribute[len(TAG_PREFIX) :]
                if value is None:
                    resource.tags.pop(key, None)
                else:
                    resource.tags[key] = str(value)
            else:
                setattr(resource, field, value)
        return updated

    def _profile(self, attribute: str, current: Any = None) -> AttributeApplyProfile:
        """How AWS applies this change to `attribute`; `current` refines it where timing depends on values."""
        if attribute.startswith(TAG_PREFIX):
            return TAG_APPLY_PROFILE
        return apply_profile_for_values(
            attribute, ATTRIBUTE_APPLY_PROFILES[attribute], current, self.values.get(attribute)
        )

    def expected_impact(self, context: StepContext) -> ChangeImpact:
        """Terraform mode: reboot and downtime from the attribute profiles (no AWS call).

        The current values come from `known_current_values` (the latest inventory), so a change whose timing
        depends on where it starts (backup retention from 0) is classified as it will happen.
        """
        requires_reboot = False
        downtimes: list[str] = []
        for attribute in self._api_values:
            profile = self._profile(
                attribute, _comparable_live_value(attribute, self.known_current_values.get(attribute))
            )
            requires_reboot = requires_reboot or profile.requires_reboot
            if not downtime_states_no_interruption(profile.downtime) and profile.downtime not in downtimes:
                downtimes.append(profile.downtime)
        return ChangeImpact(requires_reboot=requires_reboot, downtime="; ".join(downtimes) or NO_DOWNTIME)

    def change_set(self, context: StepContext) -> list[ChangeSetEntry]:
        """Direct mode: one entry per attribute whose live value differs; empty when nothing differs.

        An attribute equal to live stays when a companion of it differs (`COMPANION_ATTRIBUTE_GROUPS`).
        """
        values = self._api_values
        live = read_attribute_values(context, self.address, list(values), step_name="compute_change_set")
        differing = {
            attribute
            for attribute, desired in values.items()
            if _comparable_live_value(attribute, live.get(attribute)) != desired
        }
        for group in COMPANION_ATTRIBUTE_GROUPS:
            if differing & group:
                differing |= group & values.keys()
        entries: list[ChangeSetEntry] = []
        for attribute, desired in values.items():
            if attribute not in differing:
                continue
            profile = self._profile(attribute, _comparable_live_value(attribute, live.get(attribute)))
            queued = (
                self.apply_at == APPLY_AT_WINDOW
                and profile.apply_type == "immediate"
                and profile.honours_apply_immediately
            )
            entries.append(
                ChangeSetEntry(
                    address=self.address,
                    attribute=attribute,
                    current=_comparable_live_value(attribute, live.get(attribute)),
                    desired=desired,
                    apply_type="pending-window" if queued else profile.apply_type,
                    requires_reboot=profile.requires_reboot,
                    downtime=PENDING_WINDOW_DOWNTIME if queued else profile.downtime,
                )
            )
        if entries and self.apply_at == APPLY_NOW:
            self._warn_about_queued_modifications(context)
        return entries

    def _warn_about_queued_modifications(self, context: StepContext) -> None:
        queued = read_pending_modifications(context, self.address, step_name="compute_change_set")
        if not queued:
            return
        record_diagnostic(
            context.session,
            operation_id=context.operation.id,
            step_name="compute_change_set",
            severity="warning",
            diagnostic_kind="pending_modifications_will_apply",
            title="Queued modifications will be applied together with this change",
            detail=(
                f"{self.resource_arn} has modifications AWS has not applied yet: "
                f"{json.dumps(queued, sort_keys=True, default=str)}"
            ),
            fix_suggestion=(
                "applying now also applies the queued modifications; "
                "reject and cancel them in AWS if they should wait"
            ),
        )
        context.session.commit()

    def apply_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> dict[str, Any]:
        """Write the values through the RDS API, now or queued for the maintenance window."""
        return apply_attribute_values(
            context,
            self.address,
            {
                entry.attribute: entry.desired
                for entry in entries
                if entry.attribute not in RENDER_ONLY_ATTRIBUTES
            },
            apply_immediately=self.apply_at == APPLY_NOW,
        )

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """Read the values back; queued entries count as held once AWS lists them as pending.

        AWS lists a queued modification under its own field name (`BackupRetentionPeriod`);
        `queued_attribute_values` names it by attribute, the same reading drift detection uses, and an entry
        is queued when that attribute holds the desired value. When another attribute has not reached its
        value the result lists nothing as pending: the strategy keeps polling instead of accepting a
        partial change.
        """
        checked = [entry for entry in entries if entry.attribute not in RENDER_ONLY_ATTRIBUTES]
        live = read_attribute_values(context, self.address, [entry.attribute for entry in checked])
        needs_pending = any(entry.apply_type == "pending-window" for entry in checked)
        queued_now = (
            queued_attribute_values(
                _resource_type(self.address), read_pending_modifications(context, self.address)
            )
            if needs_pending
            else {}
        )
        mismatched: list[str] = []
        pending: list[str] = []
        for entry in checked:
            if _comparable_live_value(entry.attribute, live.get(entry.attribute)) == entry.desired:
                continue
            if (
                entry.apply_type == "pending-window"
                and entry.attribute in queued_now
                and queued_now[entry.attribute] == entry.desired
            ):
                pending.append(entry.attribute)
                continue
            mismatched.append(entry.attribute)
        if not mismatched:
            return VerificationResult(ok=True, live=live, pending=pending)
        pending = []
        status = read_resource_status(context, self.address)
        terminal = status in TERMINAL_RESOURCE_STATUSES
        detail = f"not yet at the requested value: {', '.join(mismatched)}"
        if terminal:
            detail = (
                f"{detail}; AWS reports the resource as {status}, which it will not recover from by waiting"
            )
        return VerificationResult(ok=False, live=live, pending=pending, detail=detail, terminal=terminal)


class AttributeChangeExecutor(ResourceChangeExecutor):
    """Base for every operation that is an `AttributeChange`; subclasses map options to values.

    Subclasses set `operation_kind` and implement `attribute_values` and `summary`. The record is loaded from
    the latest inventory so a subclass can validate against the current state (for example that storage
    only grows). `options.apply_at`, when the options model has it, picks `now` or `maintenance_window`.
    """

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """`{attribute: value}` the operation wants, from its validated options and the current record."""
        raise NotImplementedError

    def summary(self, options: BaseModel) -> str:
        """What the change does, for titles."""
        raise NotImplementedError

    def options_model(self) -> type[BaseModel]:
        """The options model, taken from the catalogue so the executor validates the same shape."""
        from rdscommander.catalogue.catalogue import definition_for

        return definition_for(self.operation_kind).options_model

    def desired_change(self, context: StepContext) -> AttributeChange:
        """The change for this operation: options validated, record loaded, values computed.

        In direct mode a record has no Terraform address unless the resource was adopted, so the address
        the resource would have is derived from its type and identifier.

        Raises:
            KeyError: The resource is not in the latest inventory.
            ValueError: The options do not validate, or the values are refused.
            WindowApplyNotAvailableInTerraformMode: `apply_at` asks for the maintenance window in Terraform
                mode, which always applies at once.
        """
        options = self.options_model().model_validate(context.operation.plan["options"])
        if not context.target.is_direct and getattr(options, "apply_at", APPLY_NOW) == APPLY_AT_WINDOW:
            logger.error(
                "apply_at_window_refused_in_terraform_mode",
                operation_id=str(context.operation.id),
                fix_suggestion=WindowApplyNotAvailableInTerraformMode.fix_suggestion,
            )
            raise WindowApplyNotAvailableInTerraformMode(
                "the maintenance window cannot be chosen in Terraform mode: "
                "terraform apply changes the resource now"
            )
        resource_arn = context.operation.resource_arn or ""
        record = load_resource(context.session, context.console_config, resource_arn)
        if record is None:
            logger.error(
                "attribute_change_resource_missing",
                operation_id=str(context.operation.id),
                resource_arn=resource_arn,
                fix_suggestion="refresh the inventory and start the operation again",
            )
            raise KeyError(f"{resource_arn} is not in the latest inventory")
        address = record.terraform_address or terraform_address_for(
            terraform_resource_type_for(record), record.identifier
        )
        values = self.attribute_values(options, record)
        return AttributeChange(
            resource_arn=record.arn,
            address=address,
            values=values,
            summary=self.summary(options),
            apply_at=getattr(options, "apply_at", APPLY_NOW),
            known_current_values={
                attribute: record.facts[attribute] for attribute in values if attribute in record.facts
            },
        )
