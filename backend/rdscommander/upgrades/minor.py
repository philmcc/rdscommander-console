"""Minor engine upgrades and the auto minor upgrade setting, as attribute changes.

A minor upgrade sets `engine_version` on a standalone instance or a cluster to a version the engine-versions
table lists as a minor upgrade target of the current one (`upgrades.targets`). AWS restarts the instance (a
Multi-AZ instance fails over), so the operation is disruptive and the approver types the identifier.

Direct mode sends `EngineVersion` with `ApplyImmediately` (or queued for the window) and never
`AllowMajorVersionUpgrade`; it is verified once `engine_version` reads back the target and AWS reports the
resource `available` again. Terraform mode renders the new version; when auto minor upgrade is on, the
rendered files leave `engine_version` to AWS (`ignore_changes`), so the operation also turns auto minor
upgrade off, says so in the review, and leaves it off (`auto_minor_upgrade_change` turns it back on).

An Aurora cluster's members run the cluster's version: a cluster upgrade renders and records the new
version on its members too, and change history explains their new version by the operation.

Example:
    >>> MinorUpgradeExecutor(config_store).operation_kind  # doctest: +SKIP
    'minor_upgrade'
"""

from __future__ import annotations

import threading
from typing import Any

from pydantic import BaseModel

from rdscommander.config.store import ConfigStoreProtocol
from rdscommander.lifecycle.settings_changes import add_to_review_impact
from rdscommander.logging_setup import get_logger
from rdscommander.operations.attribute_change import (
    APPLY_NOW,
    AttributeChange,
    AttributeChangeExecutor,
)
from rdscommander.operations.change_models import ChangeDiagnosticError, ChangeSetEntry, VerificationResult
from rdscommander.operations.enums import StepOutcomeStatus
from rdscommander.operations.executor import StepContext, StepOutcome
from rdscommander.parameters.direct_api import TERMINAL_RESOURCE_STATUSES, read_resource_status
from rdscommander.parameters.metadata import EngineVersionInfo
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.names import terraform_address_for
from rdscommander.terraform.render_input import RenderWorkspaceInput
from rdscommander.upgrades.options import AutoMinorUpgradeOptions, MinorUpgradeOptions
from rdscommander.upgrades.targets import engine_versions_for, ensure_valid_upgrade_target

logger = get_logger(__name__)

AUTO_MINOR_TURNED_OFF_IMPACT = "auto minor upgrade is turned off so the version can be applied"
"""Added to the review of a Terraform-mode minor upgrade of a resource whose auto minor upgrade is on."""
UPGRADED_STATUS = "available"
"""The status AWS reports once the upgrade's restart is over."""
MEMBER_RESOURCE_TYPE = "aws_rds_cluster_instance"


class EngineVersionLivesOnTheClusterError(ChangeDiagnosticError):
    """An Aurora member runs its cluster's engine version; the console refuses to upgrade the member."""

    diagnostic_kind = "engine_version_lives_on_the_cluster"
    title = "Upgrade the cluster"
    fix_suggestion = "open the cluster and run the minor upgrade there; its members follow"


class EngineVersionChange(AttributeChange):
    """Sets `engine_version`, waits for `available`, and carries a cluster's members.

    Attributes:
        member_addresses: Terraform addresses of a cluster's members; they run the cluster's version, so the
            rendered files and the record give them the new version too. Empty for a standalone instance.

    Example:
        >>> change = EngineVersionChange(arn, "aws_db_instance.x", {"engine_version": "16.6"}, "upgrade")
        >>> change.member_addresses
        ()
    """

    def __init__(
        self,
        resource_arn: str,
        address: str,
        values: dict[str, Any],
        summary: str,
        apply_at: str = APPLY_NOW,
        known_current_values: dict[str, Any] | None = None,
        member_addresses: tuple[str, ...] = (),
    ) -> None:
        super().__init__(resource_arn, address, values, summary, apply_at, known_current_values)
        self.member_addresses = tuple(member_addresses)

    def touched_addresses(self, context: StepContext) -> set[str]:
        """The resource and, for a cluster, its members (their recorded version changes with it)."""
        return {self.address, *self.member_addresses}

    def apply_to_render_input(self, render_input: RenderWorkspaceInput) -> RenderWorkspaceInput:
        """The rendered resource with the new values, and every rendered member with the new version."""
        updated = super().apply_to_render_input(render_input)
        target_engine_version = self.values.get("engine_version")
        for member in updated.cluster_instances:
            if f"{MEMBER_RESOURCE_TYPE}.{member.terraform_name}" in self.member_addresses:
                member.engine_version = str(target_engine_version)
        return updated

    def verify_direct(self, context: StepContext, entries: list[ChangeSetEntry]) -> VerificationResult:
        """The values read back, and, for an upgrade applied now, AWS reporting the resource `available`.

        AWS reports the new version before the upgrade's restart has always finished; until the status is
        `available` the result is not ok and lists nothing pending, so the strategy keeps polling. A status
        AWS never recovers from is terminal.
        """
        result = super().verify_direct(context, entries)
        upgraded_now = any(
            entry.attribute == "engine_version" and entry.apply_type != "pending-window" for entry in entries
        )
        if not result.ok or not upgraded_now:
            return result
        status = read_resource_status(context, self.address)
        if status == UPGRADED_STATUS:
            return result
        terminal = status in TERMINAL_RESOURCE_STATUSES
        logger.info(
            "engine_upgrade_awaiting_available",
            operation_id=str(context.operation.id),
            address=self.address,
            status=status,
            terminal=terminal,
        )
        detail = f"the engine version reads back the target, but AWS reports the resource as {status}"
        if terminal:
            detail = f"{detail}, which it will not recover from by waiting"
        return VerificationResult(ok=False, live=result.live, pending=[], detail=detail, terminal=terminal)


def _auto_minor_upgrade_is_on(record: ResourceRecord) -> bool:
    """Whether the resource's auto minor upgrade is on, read the way the renderer reads it (default on)."""
    if "auto_minor_version_upgrade" in record.facts:
        return bool(record.facts["auto_minor_version_upgrade"])
    return bool(record.raw.get("AutoMinorVersionUpgrade", True))


def _member_arns_and_addresses(record: ResourceRecord) -> tuple[list[str], tuple[str, ...]]:
    """A cluster's member ARNs and Terraform addresses; empty for an instance."""
    if record.kind != ResourceKind.CLUSTER:
        return [], ()
    arn_prefix = record.arn.rsplit(":", 2)[0]
    members = sorted(record.relationships.get("member_identifiers") or [])
    return (
        [f"{arn_prefix}:db:{member}" for member in members],
        tuple(terraform_address_for(MEMBER_RESOURCE_TYPE, member) for member in members),
    )


class MinorUpgradeExecutor(AttributeChangeExecutor):
    """Upgrade a standalone instance or a cluster to a listed minor version of its major version."""

    operation_kind = "minor_upgrade"

    def __init__(self, config_store: ConfigStoreProtocol) -> None:
        """Read the live configuration from `config_store`; the step's engine versions are kept per thread."""
        super().__init__(config_store)
        self._step_state = threading.local()

    def desired_change(self, context: StepContext) -> EngineVersionChange:
        """The upgrade: the target validated against the engine-versions table, with a cluster's members.

        For a cluster, its members are kept as `plan["affected_resource_arns"]` so change history explains
        their new version by this operation.

        Raises:
            KeyError: The resource is not in the latest inventory.
            UpgradeTargetNotValid: The target is not a listed minor upgrade of the current version.
            EngineVersionLivesOnTheClusterError: The resource is an Aurora member.
        """
        resource_arn = context.operation.resource_arn or ""
        record = load_resource(context.session, context.console_config, resource_arn)
        engine = str((record.facts.get("engine") if record is not None else None) or "postgres")
        self._step_state.engine_versions = engine_versions_for(context, engine, step_name="engine_versions")
        self._step_state.is_terraform_mode = not context.target.is_direct
        try:
            change = super().desired_change(context)
        finally:
            self._step_state.engine_versions = None
        assert record is not None  # super() raised KeyError otherwise
        member_arns, member_addresses = _member_arns_and_addresses(record)
        if member_arns and context.operation.plan.get("affected_resource_arns") != member_arns:
            self._store_plan_values(context, affected_resource_arns=member_arns)
        return EngineVersionChange(
            resource_arn=change.resource_arn,
            address=change.address,
            values=change.values,
            summary=change.summary,
            apply_at=change.apply_at,
            known_current_values=change.known_current_values,
            member_addresses=member_addresses,
        )

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """The target version; in Terraform mode also auto minor upgrade off when it is on.

        A target equal to the current version passes unchecked: there is nothing to change, which the
        strategy reports as such.

        Raises:
            EngineVersionLivesOnTheClusterError: The record is an Aurora cluster member.
            UpgradeTargetNotValid: The target is not a listed minor upgrade of the current version.

        Example:
            >>> executor.attribute_values(MinorUpgradeOptions(target_engine_version="16.6"), record)
            {'engine_version': '16.6'}
        """
        assert isinstance(options, MinorUpgradeOptions)
        if record.kind == ResourceKind.INSTANCE and record.relationships.get("cluster_identifier"):
            logger.error(
                "minor_upgrade_of_aurora_member_refused",
                resource_identifier=record.identifier,
                cluster_identifier=record.relationships["cluster_identifier"],
                fix_suggestion=EngineVersionLivesOnTheClusterError.fix_suggestion,
            )
            raise EngineVersionLivesOnTheClusterError(
                f"{record.identifier} is a member of {record.relationships['cluster_identifier']}; "
                "Aurora members run the version of their cluster"
            )
        engine = str(record.facts.get("engine") or "postgres")
        current_engine_version = str(record.facts.get("engine_version") or "")
        if options.target_engine_version != current_engine_version:
            versions: list[EngineVersionInfo] = getattr(self._step_state, "engine_versions", None) or []
            ensure_valid_upgrade_target(
                versions,
                engine,
                current_engine_version,
                options.target_engine_version,
                "minor",
                record.identifier,
            )
        values: dict[str, Any] = {"engine_version": options.target_engine_version}
        if getattr(self._step_state, "is_terraform_mode", False) and _auto_minor_upgrade_is_on(record):
            values["auto_minor_version_upgrade"] = False
        logger.info(
            "minor_upgrade_values_computed",
            resource_identifier=record.identifier,
            current_engine_version=current_engine_version,
            target_engine_version=options.target_engine_version,
            turns_auto_minor_upgrade_off="auto_minor_version_upgrade" in values,
        )
        return values

    def _execute_render_desired(self, context: StepContext) -> StepOutcome:
        """Terraform mode: the rendered change; the review says when auto minor upgrade is turned off."""
        outcome = super()._execute_render_desired(context)
        if outcome.status == StepOutcomeStatus.COMPLETED and (
            "auto_minor_version_upgrade" in self.desired_change(context).values
        ):
            add_to_review_impact(self, context, AUTO_MINOR_TURNED_OFF_IMPACT)
        return outcome

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, MinorUpgradeOptions)
        return f"upgrade to {options.target_engine_version}"


class AutoMinorUpgradeChangeExecutor(AttributeChangeExecutor):
    """Turn auto minor upgrade on or off on an instance, cluster member or cluster."""

    operation_kind = "auto_minor_upgrade_change"

    def attribute_values(self, options: BaseModel, record: ResourceRecord) -> dict[str, Any]:
        """The flag.

        Example:
            >>> executor.attribute_values(AutoMinorUpgradeOptions(auto_minor_version_upgrade=False), record)
            {'auto_minor_version_upgrade': False}
        """
        assert isinstance(options, AutoMinorUpgradeOptions)
        return {"auto_minor_version_upgrade": options.auto_minor_version_upgrade}

    def summary(self, options: BaseModel) -> str:
        """What the change does."""
        assert isinstance(options, AutoMinorUpgradeOptions)
        return (
            "turn auto minor upgrade on"
            if options.auto_minor_version_upgrade
            else "turn auto minor upgrade off"
        )
