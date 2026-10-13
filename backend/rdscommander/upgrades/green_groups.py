"""Which existing parameter groups a major upgrade's green side can reuse (spec 8.7.1 step 2).

A major upgrade that builds its groups first looks for groups of the target family that an earlier upgrade
build made from this resource's current groups (matched by provenance); only when every current group has
one does it reuse them instead of building again.

Example:
    >>> reusable_groups(context, orders_record, options, "postgres17")  # doctest: +SKIP
    {'instance': 'demo-postgres16-tuned-pg17'}
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from rdscommander.control_db.models import Operation
from rdscommander.operations.executor import StepContext
from rdscommander.parameters.upgrade_builder import UpgradeSource, new_group_name_for, upgrade_sources
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import list_resources, provenance_of_parameter_group
from rdscommander.upgrades.options import MajorUpgradeOptions


def built_for_this_resource(session: Session, provenance: dict[str, Any], resource_arn: str) -> bool:
    """Whether the build that made a group acted on `resource_arn`.

    Example:
        >>> built_for_this_resource(session, {"operation_id": str(build.id)}, orders_arn)  # doctest: +SKIP
        True
    """
    operation_id = provenance.get("operation_id")
    if not operation_id:
        return False
    build = session.get(Operation, uuid.UUID(operation_id))
    return build is not None and build.resource_arn == resource_arn


def reusable_group_for(
    context: StepContext,
    record: ResourceRecord,
    source: UpgradeSource,
    candidates: list[ResourceRecord],
    target_family: str,
    name_suffix: str,
) -> str | None:
    """The group of the target family an earlier build made from `source`, or None.

    A group built from an AWS default group (shared by every database) counts only when it was built for
    this resource; with a name suffix, only the group of exactly the name a build would give counts.

    Example:
        >>> reusable_group_for(context, orders, source, groups, "postgres17", "")  # doctest: +SKIP
        'demo-postgres16-tuned-pg17'
    """
    expected_name = new_group_name_for(
        source.group_name, source.is_default, record.identifier, target_family, name_suffix
    )
    matches: list[str] = []
    for group in candidates:
        if group.facts.get("group_scope") != source.scope:
            continue
        if group.facts.get("parameter_group_family") != target_family:
            continue
        if name_suffix and group.identifier != expected_name:
            continue
        provenance = provenance_of_parameter_group(context.session, group)
        if provenance is None or provenance["target_family"] != target_family:
            continue
        if source.group_name not in provenance["source_group_names"]:
            continue
        if source.is_default and not built_for_this_resource(context.session, provenance, record.arn):
            continue
        matches.append(group.identifier)
    if not matches:
        return None
    return expected_name if expected_name in matches else sorted(matches)[0]


def reusable_groups(
    context: StepContext,
    record: ResourceRecord,
    options: MajorUpgradeOptions,
    target_family: str,
) -> dict[str, str | None] | None:
    """A group of the target family for every current group, each built earlier from it; None otherwise.

    Raises:
        UpgradePlanError: The resource has no group to carry, or a cluster's members use different groups.

    Example:
        >>> reusable_groups(context, orders_record, options, "postgres17")  # doctest: +SKIP
        {'instance': 'demo-postgres16-tuned-pg17'}
    """
    candidates = [
        group
        for group in list_resources(
            context.session, context.console_config, record.environment_slug, ResourceKind.PARAMETER_GROUP
        )
        if group.target_key == record.target_key
    ]
    chosen: dict[str, str | None] = {}
    for source in upgrade_sources(context.session, context.console_config, record):
        name = reusable_group_for(
            context, record, source, candidates, target_family, options.group_name_suffix
        )
        if name is None:
            return None
        chosen[source.scope] = name
    return chosen
