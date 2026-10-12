"""The needs-attention inbox.

Every row names the action that resolves it (`action_kind` and `action_options`), so the frontend can open
the task panel straight from the inbox.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from rdscommander.catalogue.catalogue import decommissionable, has_pending_reboot
from rdscommander.config.models import ConsoleConfig
from rdscommander.control_db.models import (
    BlueGreenDeployment,
    DriftFinding,
    InventoryPendingMaintenance,
    Operation,
    ResourceChangeEvent,
)
from rdscommander.history.change_events import EXPLANATION_OUTSIDE_CONSOLE
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.operations.enums import OperationState
from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import list_resources
from rdscommander.upgrades.blue_green_api import BLUE_SIDE_TAG_KEY, deployments_left_in_place

router = APIRouter(prefix="/api/v1", tags=["attention"])

FAILED_OPERATIONS_WINDOW = timedelta(days=7)
OUTSIDE_CHANGES_LIMIT = 200
PENDING_REBOOT_STATUS = "pending-reboot"
PENDING_REBOOT_DETAIL = "parameter changes are pending a reboot"
ADOPTABLE_KINDS = (ResourceKind.INSTANCE, ResourceKind.CLUSTER, ResourceKind.PARAMETER_GROUP)
FOREIGN_OWNED_DETAIL = "read-only unless the environment allows direct changes on foreign-owned resources"
BLUE_SIDE_DETAIL = (
    "kept after a major upgrade switched production to the green side; decommission it once the upgrade is "
    "proven"
)
BLUE_GREEN_CLEANUP_KIND = "blue_green_cleanup"
BLUE_SIDE_IDENTIFIER_PATTERN = re.compile(r"-old\d+$")
"""What AWS appends to the blue side's identifiers at a switchover: `-old` and the next free number."""
CANCELLED_ROW_KIND = "cancelled_recent"
"""Kind of the row of a rejected operation that left something to clean up (a blue/green deployment)."""


class AttentionItem(BaseModel):
    """One inbox row with the action that resolves it.

    Attributes:
        kind: The inbox section the row belongs to, e.g. `drift`.
        title: One line saying what needs attention.
        detail: A second line with the particulars.
        environment_slug: Environment the row belongs to; empty for a failed operation that belongs to no
            environment (an environment import), which every environment's inbox lists.
        resource_id: URL-safe id of the resource, when the row is about one.
        resource_identifier: The resource's AWS identifier, when the row is about one.
        operation_id: The operation the row is about, when it is about one.
        action_kind: Catalogue operation kind that resolves the row, when one does.
        action_options: Options to prefill for `action_kind`.
    """

    kind: str
    title: str
    detail: str
    environment_slug: str
    resource_id: str | None = None
    resource_identifier: str | None = None
    operation_id: uuid.UUID | None = None
    action_kind: str | None = None
    action_options: dict[str, Any] = Field(default_factory=dict)


class AttentionInbox(BaseModel):
    """All sections of the inbox, each a list of rows (empty when nothing needs attention)."""

    awaiting_approval: list[AttentionItem]
    pending_reboot: list[AttentionItem]
    pending_maintenance: list[AttentionItem]
    drift: list[AttentionItem]
    outside_changes: list[AttentionItem]
    failed_recent: list[AttentionItem]
    foreign_owned: list[AttentionItem]
    unmanaged: list[AttentionItem]
    blue_side: list[AttentionItem]


def _identifier_of(arn: str) -> str:
    """The AWS identifier at the end of an ARN (or the whole text when it is not an ARN)."""
    return arn.rsplit(":", 1)[-1]


def _resource_fields(arn: str | None) -> dict[str, str | None]:
    """`resource_id` and `resource_identifier` for a row about `arn`; both None without an ARN.

    A value that is not an ARN (a drift finding for a resource that has left the inventory is keyed by its
    Terraform address) has no page to link to.
    """
    if not arn or not arn.startswith("arn:"):
        return {"resource_id": None, "resource_identifier": _identifier_of(arn) if arn else None}
    return {"resource_id": encode_resource_id(arn), "resource_identifier": _identifier_of(arn)}


def _cleanup_action(deployment: BlueGreenDeployment) -> dict[str, Any]:
    """The fields of a row that offers `blue_green_cleanup` for a deployment left in place."""
    return {
        "action_kind": BLUE_GREEN_CLEANUP_KIND,
        "action_options": {"deployment_id": deployment.deployment_id, "delete_green": True},
    }


def _operation_items(session: Session, slugs: list[str], inbox: AttentionInbox) -> None:
    """Operations awaiting approval, and operations that failed in the last seven days.

    A failed operation that belongs to no environment (an environment import) is in every environment's
    inbox, with an empty `environment_slug`: nothing else would lead back to it once its panel is closed.

    A major upgrade that failed or was rejected before its switchover finished left its blue/green
    deployment in place: its row (kind `failed_recent`, or `cancelled_recent` for a rejection) offers
    `blue_green_cleanup` with the deployment's id, for as long as the deployment is left, however old.
    """
    for operation in session.execute(
        select(Operation)
        .where(
            Operation.environment_slug.in_(slugs),
            Operation.state == OperationState.AWAITING_APPROVAL.value,
        )
        .order_by(Operation.created_at_utc)
    ).scalars():
        inbox.awaiting_approval.append(
            AttentionItem(
                kind="awaiting_approval",
                title=operation.title or operation.operation_kind,
                detail=f"created by {operation.created_by_operator_name}",
                environment_slug=operation.environment_slug or "",
                operation_id=operation.id,
                **_resource_fields(operation.resource_arn),
            )
        )
    left_by_operation: dict[uuid.UUID, BlueGreenDeployment] = {}
    for deployment in deployments_left_in_place(session):
        if deployment.environment_slug in slugs:
            left_by_operation.setdefault(deployment.operation_id, deployment)
    failed_since = datetime.now(UTC) - FAILED_OPERATIONS_WINDOW
    listed: set[uuid.UUID] = set()
    for operation in session.execute(
        select(Operation)
        .where(
            or_(Operation.environment_slug.in_(slugs), Operation.environment_slug.is_(None)),
            Operation.state == OperationState.FAILED.value,
            Operation.finished_at_utc >= failed_since,
        )
        .order_by(Operation.finished_at_utc.desc())
    ).scalars():
        listed.add(operation.id)
        deployment = left_by_operation.get(operation.id)
        inbox.failed_recent.append(
            AttentionItem(
                kind="failed_recent",
                title=operation.title or operation.operation_kind,
                detail=operation.failure_summary or "",
                environment_slug=operation.environment_slug or "",
                operation_id=operation.id,
                **(_cleanup_action(deployment) if deployment is not None else {}),
                **_resource_fields(operation.resource_arn),
            )
        )
    for operation_id, deployment in left_by_operation.items():
        operation = session.get(Operation, operation_id)
        if operation is None or operation.id in listed:
            continue
        cancelled = operation.state == OperationState.CANCELLED.value
        summary = operation.failure_summary or ("rejected" if cancelled else "failed")
        inbox.failed_recent.append(
            AttentionItem(
                kind=CANCELLED_ROW_KIND if cancelled else "failed_recent",
                title=operation.title or operation.operation_kind,
                detail=f"{summary}; blue/green deployment {deployment.deployment_id} is left in place",
                environment_slug=operation.environment_slug or "",
                operation_id=operation.id,
                **_cleanup_action(deployment),
                **_resource_fields(operation.resource_arn),
            )
        )


def _drift_and_change_items(
    session: Session, slugs: list[str], known_arns: set[str], inbox: AttentionInbox
) -> None:
    """Open drift findings, and changes made outside the console that nobody has reviewed.

    Every drift row can be resolved. A resource with a page links to it; a finding for something without
    a page (a subnet group, or a resource that has left the inventory and is keyed by its Terraform
    address) carries no link and a ready-made acceptance, since accepting is the only resolution there.
    """
    for finding in session.execute(
        select(DriftFinding)
        .where(DriftFinding.environment_slug.in_(slugs), DriftFinding.resolved_at_utc.is_(None))
        .order_by(DriftFinding.detected_at_utc.desc())
    ).scalars():
        names = ", ".join(str(attribute.get("attribute")) for attribute in finding.attributes[:4])
        has_page = finding.resource_arn in known_arns
        inbox.drift.append(
            AttentionItem(
                kind="drift",
                title=f"Drift on {_identifier_of(finding.resource_arn)}",
                detail=f"{finding.finding_kind}: {names}",
                environment_slug=finding.environment_slug,
                action_kind="drift_resolve",
                action_options=(
                    {"resource_arn": finding.resource_arn}
                    if has_page
                    else {"resource_arn": finding.resource_arn, "choice": "accept"}
                ),
                **(
                    _resource_fields(finding.resource_arn)
                    if has_page
                    else {"resource_id": None, "resource_identifier": _identifier_of(finding.resource_arn)}
                ),
            )
        )
    for event in session.execute(
        select(ResourceChangeEvent)
        .where(
            ResourceChangeEvent.environment_slug.in_(slugs),
            ResourceChangeEvent.explanation == EXPLANATION_OUTSIDE_CONSOLE,
            ResourceChangeEvent.reviewed_at_utc.is_(None),
        )
        .order_by(ResourceChangeEvent.detected_at_utc.desc())
        .limit(OUTSIDE_CHANGES_LIMIT)
    ).scalars():
        inbox.outside_changes.append(
            AttentionItem(
                kind="outside_change",
                title=f"{_identifier_of(event.resource_arn)}: {event.attribute_path}",
                detail=f"{event.value_before!r} became {event.value_after!r}",
                environment_slug=event.environment_slug,
                **_resource_fields(event.resource_arn),
            )
        )


def _is_blue_side(record: ResourceRecord) -> bool:
    """A standalone instance or cluster that a major upgrade renamed `-old<n>` and tagged as its blue side.

    A blue-side cluster's members carry the tag too; they go with the cluster's Decommission, so they have
    no row of their own.
    """
    if record.kind == ResourceKind.INSTANCE and record.relationships.get("cluster_identifier"):
        return False
    tags = record.facts.get("tags") or {}
    return (
        record.kind in (ResourceKind.INSTANCE, ResourceKind.CLUSTER)
        and BLUE_SIDE_IDENTIFIER_PATTERN.search(record.identifier) is not None
        and bool(tags.get(BLUE_SIDE_TAG_KEY))
    )


def _upgrade_operation_id(record: ResourceRecord) -> uuid.UUID | None:
    """The major upgrade a blue-side resource's tag names, or None when the tag is not an operation id."""
    try:
        return uuid.UUID(str((record.facts.get("tags") or {}).get(BLUE_SIDE_TAG_KEY)))
    except ValueError:
        return None


def _estate_items(
    session: Session,
    console_config: ConsoleConfig,
    records_by_environment: dict[str, list[ResourceRecord]],
    inbox: AttentionInbox,
) -> None:
    """Pending reboots, foreign-owned, unmanaged and blue-side resources, and pending maintenance."""
    for slug, records in records_by_environment.items():
        environment = console_config.environment(slug)
        for record in records:
            fields = _resource_fields(record.arn)
            if (
                record.kind == ResourceKind.INSTANCE
                and record.parameter_apply_status == PENDING_REBOOT_STATUS
            ):
                # The row offers the reboot only where the reboot's own applicability allows it (a
                # foreign-owned instance is read-only), and otherwise says why not.
                reboot = has_pending_reboot(record, environment)
                inbox.pending_reboot.append(
                    AttentionItem(
                        kind="pending_reboot",
                        title=f"{record.identifier} needs a reboot",
                        detail=(
                            PENDING_REBOOT_DETAIL
                            if reboot.ok
                            else f"{PENDING_REBOOT_DETAIL}; {reboot.reason}"
                        ),
                        environment_slug=slug,
                        action_kind="reboot_instance" if reboot.ok else None,
                        **fields,
                    )
                )
            if record.ownership.startswith("foreign_") and record.kind != ResourceKind.SNAPSHOT:
                inbox.foreign_owned.append(
                    AttentionItem(
                        kind="foreign_owned",
                        title=f"{record.identifier} is {record.ownership.replace('_', ' ')}",
                        detail=FOREIGN_OWNED_DETAIL,
                        environment_slug=slug,
                        **fields,
                    )
                )
            if _is_blue_side(record):
                # Offered only where Decommission's own applicability allows it, and otherwise says why not.
                decommission = decommissionable(record, environment)
                inbox.blue_side.append(
                    AttentionItem(
                        kind="blue_side",
                        title=f"{record.identifier} is the blue side of a major upgrade",
                        detail=BLUE_SIDE_DETAIL
                        if decommission.ok
                        else f"{BLUE_SIDE_DETAIL}; {decommission.reason}",
                        environment_slug=slug,
                        operation_id=_upgrade_operation_id(record),
                        action_kind="decommission" if decommission.ok else None,
                        **fields,
                    )
                )
            if (
                record.ownership == "unmanaged"
                and record.terraform_address is None
                and environment.change_management == "terraform"
                and record.kind in ADOPTABLE_KINDS
                and not record.facts.get("is_default")
            ):
                inbox.unmanaged.append(
                    AttentionItem(
                        kind="unmanaged",
                        title=f"{record.identifier} is not under Terraform",
                        detail="adopt it to manage it from here",
                        environment_slug=slug,
                        action_kind="terraform_adoption",
                        action_options={"target_key": record.target_key, "resource_arns": [record.arn]},
                        **fields,
                    )
                )
        for target in console_config.targets_for_environment(slug):
            snapshot = latest_snapshot_for_target(session, target.target_key)
            if snapshot is None:
                continue
            for pending in session.execute(
                select(InventoryPendingMaintenance).where(
                    InventoryPendingMaintenance.snapshot_id == snapshot.id
                )
            ).scalars():
                inbox.pending_maintenance.append(
                    AttentionItem(
                        kind="pending_maintenance",
                        title=f"{_identifier_of(pending.resource_arn)}: {pending.action}",
                        detail=pending.description,
                        environment_slug=slug,
                        **_resource_fields(pending.resource_arn),
                    )
                )


@router.get("/attention", response_model=AttentionInbox)
def read_attention(request: Request, environment_slug: str | None = None) -> AttentionInbox:
    """Everything that needs a person, optionally for one environment.

    Args:
        request: The request; configuration and sessions come from `app.state`.
        environment_slug: Restrict the inbox to one environment; every environment when omitted.

    Returns:
        The inbox sections: operations awaiting approval, parameter changes pending a reboot, pending
        maintenance, open drift, unreviewed changes made outside the console, operations that failed in the
        last seven days (and failed or rejected major upgrades that left a blue/green deployment in place),
        foreign-owned resources, unmanaged resources in Terraform-mode environments, and the blue side major
        upgrades kept.

    Raises:
        HTTPException: 404 when `environment_slug` names no environment.

    Example:
        >>> client.get("/api/v1/attention", params={"environment_slug": "sandbox"}).json()["drift"]
        []
    """
    console_config = request.app.state.config_store.current
    if environment_slug:
        try:
            console_config.environment(environment_slug)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
    slugs = [environment_slug] if environment_slug else [e.slug for e in console_config.environments]
    inbox = AttentionInbox(
        awaiting_approval=[],
        pending_reboot=[],
        pending_maintenance=[],
        drift=[],
        outside_changes=[],
        failed_recent=[],
        foreign_owned=[],
        unmanaged=[],
        blue_side=[],
    )
    with request.app.state.session_factory() as session:
        records_by_environment = {slug: list_resources(session, console_config, slug) for slug in slugs}
        known_arns = {record.arn for records in records_by_environment.values() for record in records}
        _operation_items(session, slugs, inbox)
        _drift_and_change_items(session, slugs, known_arns, inbox)
        _estate_items(session, console_config, records_by_environment, inbox)
    return inbox
