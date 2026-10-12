"""Deterministic refusal rules evaluated on every plan before approval is even offered."""

from __future__ import annotations

from collections.abc import Collection

from pydantic import BaseModel, Field

from rdscommander.terraform.plan_parser import PlanSummary, ResourceChange

PROTECTED_RESOURCE_TYPES = ("aws_db_instance", "aws_rds_cluster", "aws_rds_cluster_instance")
# Attributes an adoption may change on a protected resource: ownership tags and attributes that exist only
# in Terraform configuration (they never modify the running database).
PROTECTED_UPDATE_ALLOWLIST = frozenset(
    {
        "tags",
        "tags_all",
        "skip_final_snapshot",
        "final_snapshot_identifier",
        "apply_immediately",
        "allow_major_version_upgrade",
        "force_destroy",
        "delete_automated_backups",
        "enable_global_write_forwarding",
        "enable_local_write_forwarding",
        "scaling_configuration",
        "skip_destroy",
        "snapshot_identifier",
        "restore_to_point_in_time",
        "s3_import",
        "timeouts",
    }
)
# Attributes a change operation may update on an address it does not touch: lines the templates render since
# 0.3.0 that exist only in Terraform configuration and never modify the running database. The first operation
# on a workspace rendered by 0.2.0 adds `apply_immediately` to every instance, cluster and member;
# `final_snapshot_identifier` and `skip_final_snapshot` are configuration-only too. The replica lines
# (`storage_type`, `iops`, `multi_az`) and `manage_master_user_password` are not here: they are real
# settings, rendered from the record, and plan nothing while the record matches AWS.
UNTOUCHED_ADDRESS_UPDATE_ALLOWLIST = frozenset(
    {"apply_immediately", "final_snapshot_identifier", "skip_final_snapshot", "timeouts"}
)


class PolicyDecision(BaseModel):
    """Allowed, or the list of reasons it is not."""

    allowed: bool
    refusals: list[str] = Field(default_factory=list)
    also_brought_up_to_date: list[str] = Field(default_factory=list)
    """Untouched addresses whose update only changes configuration-only lines; the review names them."""


def _change_to_an_earlier_adoption(change: ResourceChange) -> str | None:
    """The refusal for a planned change to a resource adopted earlier, or None when nothing real changes.

    An adoption renders the resources it adopted earlier from the record, so a plan that changes one of
    them means AWS has drifted from the record; applying it would restore the resource without anyone
    choosing to. Tags and configuration-only attributes are not such a change.
    """
    if change.actions == ["no-op"]:
        return None
    if change.actions == ["update"]:
        changed = [name for name in change.changed_attribute_names if name not in PROTECTED_UPDATE_ALLOWLIST]
        if not changed:
            return None
        what = ", ".join(changed)
    else:
        what = "/".join(change.actions)
    return (
        f"{change.address}: the plan would change {what} on a resource adopted earlier, which this adoption "
        f"does not select; AWS differs from the record. Resolve drift on {change.address} first"
    )


def evaluate_adoption_policy(
    summary: PlanSummary, selected_addresses: set[str], adopted_earlier: Collection[str] = frozenset()
) -> PolicyDecision:
    """Refuse deletes or replaces on protected types, creates of selected resources, and missing imports.

    Args:
        summary: The summarised adoption plan.
        selected_addresses: The addresses this adoption imports.
        adopted_earlier: Addresses adopted by earlier adoptions; any real change to one that is not selected
            is refused, because the plan would restore drift nobody chose to restore.

    Returns:
        The decision; `refusals` names every address that is not allowed and why.

    Example:
        >>> evaluate_adoption_policy(summary, {"aws_db_subnet_group.private"}).allowed  # doctest: +SKIP
        True
    """
    refusals: list[str] = []
    seen: set[str] = set()
    for change in summary.resource_changes:
        seen.add(change.address)
        if change.address in adopted_earlier and change.address not in selected_addresses:
            refusal = _change_to_an_earlier_adoption(change)
            if refusal is not None:
                refusals.append(refusal)
            continue
        if change.resource_type in PROTECTED_RESOURCE_TYPES and "delete" in change.actions:
            verb = "replace" if change.is_replace else "delete"
            refusals.append(
                f"{change.address}: plan would {verb} a {change.resource_type}; "
                "adoption never destroys instances or clusters"
            )
        elif change.resource_type in PROTECTED_RESOURCE_TYPES and change.actions == ["update"]:
            disallowed = [a for a in change.changed_attribute_names if a not in PROTECTED_UPDATE_ALLOWLIST]
            if disallowed:
                refusals.append(
                    f"{change.address}: adoption would change {', '.join(disallowed)} on a "
                    f"{change.resource_type}; adoption only adds ownership tags and configuration-only "
                    "attributes. The inventory snapshot may be stale (refresh it) or the rendering "
                    "differs from AWS"
                )
        if change.address in selected_addresses:
            if change.actions == ["create"]:
                refusals.append(
                    f"{change.address}: plan would create a new resource instead of importing "
                    "the existing one; the identifier or rendering is wrong"
                )
            elif not change.is_import:
                refusals.append(f"{change.address}: selected but not imported by this plan")
    for address in sorted(selected_addresses - seen):
        refusals.append(f"{address}: selected but absent from the plan")
    return PolicyDecision(allowed=not refusals, refusals=refusals)


def evaluate_change_policy(
    summary: PlanSummary,
    allowed_addresses: set[str],
    allow_delete_of: frozenset[str] = frozenset(),
    allow_create_of: frozenset[str] = frozenset(),
) -> PolicyDecision:
    """Only in-place updates of the addresses the operation touched are allowed.

    A change operation edits the resources it names and nothing else. Anything that would create, delete
    or replace a resource is refused, unless the operation is of a kind that creates or deletes the very
    address it names: `allow_create_of` lets a plain create of those addresses through and
    `allow_delete_of` a plain delete. A replace (delete and create together) is never allowed.

    The one exception for other addresses: an in-place update that only changes attributes in
    `UNTOUCHED_ADDRESS_UPDATE_ALLOWLIST` (configuration-only lines, such as the `apply_immediately` a
    workspace rendered by 0.2.0 lacks) is let through and listed in `also_brought_up_to_date`.

    Args:
        summary: The summarised plan.
        allowed_addresses: Terraform addresses the operation touches; an update of any other address is
            refused.
        allow_delete_of: Addresses whose plain delete is part of the operation.
        allow_create_of: Addresses whose plain create is part of the operation.

    Returns:
        The decision; `refusals` names every address that is not allowed and why, and
        `also_brought_up_to_date` every untouched address whose configuration-only lines the apply updates.

    Example:
        >>> decision = evaluate_change_policy(  # doctest: +SKIP
        ...     summary, {"aws_db_parameter_group.demo"}, allow_delete_of=frozenset()
        ... )
        >>> decision.allowed  # doctest: +SKIP
        True
    """
    refusals: list[str] = []
    also_brought_up_to_date: list[str] = []
    for change in summary.resource_changes:
        if change.actions == ["no-op"]:
            continue
        if change.actions == ["delete"] and change.address in allow_delete_of:
            continue
        if change.actions == ["create"] and change.address in allow_create_of:
            continue
        if change.actions != ["update"]:
            refusals.append(
                f"{change.address}: {'/'.join(change.actions)} is not allowed by a change operation"
            )
        elif change.address not in allowed_addresses:
            changed_attribute_names = change.changed_attribute_names
            if changed_attribute_names and set(changed_attribute_names) <= UNTOUCHED_ADDRESS_UPDATE_ALLOWLIST:
                also_brought_up_to_date.append(change.address)
            else:
                refusals.append(f"{change.address}: not part of this operation")
    return PolicyDecision(
        allowed=not refusals, refusals=refusals, also_brought_up_to_date=sorted(also_brought_up_to_date)
    )
