"""Match resources between two environments and list attribute differences.

Resources are matched by AWS identifier: exactly first, then after stripping a configured affix (for
instance the `-prod` of `orders-prod`) from both sides. Parameter groups are matched by name the same way.
Matched pairs list the attributes whose values differ; resources without a counterpart are listed apart.

Example:
    >>> comparison = compare_environments(  # doctest: +SKIP
    ...     session, console_config, "staging", "production", strip_suffixes=["-prod"]
    ... )
    >>> [pair.left_identifier for pair in comparison.pairs]  # doctest: +SKIP
    ['orders']
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig
from rdscommander.history.change_events import resources_in_snapshot
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.logging_setup import get_logger
from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import list_resources

logger = get_logger(__name__)

COMPARED_ATTRIBUTES: dict[ResourceKind, list[str]] = {
    ResourceKind.INSTANCE: [
        "engine_version",
        "db_instance_class",
        "allocated_storage_gb",
        "max_allocated_storage_gb",
        "storage_type",
        "multi_az",
        "backup_retention_period",
        "preferred_backup_window",
        "preferred_maintenance_window",
        "deletion_protection",
        "performance_insights_enabled",
        "parameter_group_name",
    ],
    ResourceKind.CLUSTER: [
        "engine_version",
        "backup_retention_period",
        "preferred_backup_window",
        "preferred_maintenance_window",
        "deletion_protection",
        "cluster_parameter_group_name",
        "member_identifiers",
    ],
}
"""Attributes compared for instances and clusters; a parameter group compares its `parameter.*` values."""
PARAMETER_ATTRIBUTE_PREFIX = "parameter."


class AttributeDifference(BaseModel):
    """One attribute whose value differs between the two environments.

    Attributes:
        attribute: Attribute name, e.g. `db_instance_class` or `parameter.work_mem`.
        left: Value in the left environment; None when absent there.
        right: Value in the right environment; None when absent there.
    """

    attribute: str
    left: Any
    right: Any


class ResourcePair(BaseModel):
    """A resource of the left environment and its counterpart in the right one.

    Attributes:
        kind: Resource kind, e.g. `instance`.
        left_identifier: AWS identifier in the left environment.
        right_identifier: AWS identifier in the right environment.
        left_resource_id: URL-safe id of the left resource, for its page.
        right_resource_id: URL-safe id of the right resource, for its page.
        differences: The attributes that differ; empty when the two are alike.
    """

    kind: str
    left_identifier: str
    right_identifier: str
    left_resource_id: str
    right_resource_id: str
    differences: list[AttributeDifference] = Field(default_factory=list)


class UnmatchedResource(BaseModel):
    """A resource with no counterpart in the other environment.

    Attributes:
        kind: Resource kind, e.g. `instance`.
        identifier: AWS identifier.
        resource_id: URL-safe id of the resource, for its page.
    """

    kind: str
    identifier: str
    resource_id: str


class EnvironmentComparison(BaseModel):
    """Result of comparing two environments.

    Attributes:
        left_slug: The left environment.
        right_slug: The right environment.
        pairs: Matched resources with their differences.
        unmatched_left: Left resources with no counterpart on the right.
        unmatched_right: Right resources with no counterpart on the left.
    """

    left_slug: str
    right_slug: str
    pairs: list[ResourcePair]
    unmatched_left: list[UnmatchedResource]
    unmatched_right: list[UnmatchedResource]


def normalise_identifier(identifier: str, strip_suffixes: list[str]) -> str:
    """Strip the first matching suffix or prefix (`-prod`, `prod-`).

    Args:
        identifier: An AWS identifier.
        strip_suffixes: Affixes to try in order; an empty one is ignored.

    Returns:
        The identifier without the first affix it ends or starts with, or unchanged when none fits.

    Example:
        >>> normalise_identifier("orders-prod", ["-prod"])
        'orders'
        >>> normalise_identifier("prod-orders", ["prod-"])
        'orders'
    """
    for affix in strip_suffixes:
        if affix and identifier.endswith(affix):
            return identifier[: -len(affix)]
        if affix and identifier.startswith(affix):
            return identifier[len(affix) :]
    return identifier


def _attributes_of_environment(
    session: Session, console_config: ConsoleConfig, environment_slug: str
) -> dict[str, dict[str, Any]]:
    """ARN to flat attributes for the latest inventory snapshot of each target of the environment."""
    attributes_by_arn: dict[str, dict[str, Any]] = {}
    for target in console_config.targets_for_environment(environment_slug):
        snapshot = latest_snapshot_for_target(session, target.target_key)
        if snapshot is not None:
            attributes_by_arn.update(resources_in_snapshot(session, snapshot.id))
    return attributes_by_arn


def _differences(
    record: ResourceRecord, left_attributes: dict[str, Any], right_attributes: dict[str, Any]
) -> list[AttributeDifference]:
    """The compared attributes of `record`'s kind whose values differ."""
    attribute_names = COMPARED_ATTRIBUTES.get(record.kind) or sorted(
        name
        for name in set(left_attributes) | set(right_attributes)
        if name.startswith(PARAMETER_ATTRIBUTE_PREFIX)
    )
    return [
        AttributeDifference(
            attribute=attribute_name,
            left=left_attributes.get(attribute_name),
            right=right_attributes.get(attribute_name),
        )
        for attribute_name in attribute_names
        if left_attributes.get(attribute_name) != right_attributes.get(attribute_name)
    ]


def compare_environments(
    session: Session,
    console_config: ConsoleConfig,
    left_slug: str,
    right_slug: str,
    strip_suffixes: list[str],
) -> EnvironmentComparison:
    """Match by exact identifier first, then by normalised identifier; parameter groups by name.

    Args:
        session: Control database session.
        console_config: Loaded console configuration, used to find each environment's targets.
        left_slug: The left environment.
        right_slug: The right environment.
        strip_suffixes: Suffixes or prefixes that differ between the environments' naming, e.g. `-prod`.

    Returns:
        The matched pairs with their differences, and the resources that have no counterpart. Snapshots
        are not compared. Each right resource is matched at most once.

    Raises:
        KeyError: Either slug names no environment.

    Example:
        >>> compare_environments(  # doctest: +SKIP
        ...     session, console_config, "sandbox", "production", strip_suffixes=["-prod"]
        ... ).pairs[0].differences[0].attribute
        'db_instance_class'
    """
    console_config.environment(left_slug)
    console_config.environment(right_slug)
    logger.info(
        "environment_comparison_started",
        left_slug=left_slug,
        right_slug=right_slug,
        strip_suffixes=strip_suffixes,
    )
    left_records = [
        record
        for record in list_resources(session, console_config, left_slug)
        if record.kind != ResourceKind.SNAPSHOT
    ]
    right_records = [
        record
        for record in list_resources(session, console_config, right_slug)
        if record.kind != ResourceKind.SNAPSHOT
    ]
    left_attributes_by_arn = _attributes_of_environment(session, console_config, left_slug)
    right_attributes_by_arn = _attributes_of_environment(session, console_config, right_slug)
    right_by_exact_identifier = {(record.kind, record.identifier): record for record in right_records}
    right_by_normalised_identifier: dict[tuple[ResourceKind, str], list[ResourceRecord]] = defaultdict(list)
    for record in right_records:
        right_by_normalised_identifier[
            (record.kind, normalise_identifier(record.identifier, strip_suffixes))
        ].append(record)
    matched_right_arns: set[str] = set()
    pairs: list[ResourcePair] = []
    unmatched_left: list[UnmatchedResource] = []
    for record in left_records:
        counterpart = right_by_exact_identifier.get((record.kind, record.identifier))
        if counterpart is None or counterpart.arn in matched_right_arns:
            counterpart = next(
                (
                    candidate
                    for candidate in right_by_normalised_identifier[
                        (record.kind, normalise_identifier(record.identifier, strip_suffixes))
                    ]
                    if candidate.arn not in matched_right_arns
                ),
                None,
            )
        if counterpart is None:
            unmatched_left.append(
                UnmatchedResource(
                    kind=record.kind.value,
                    identifier=record.identifier,
                    resource_id=encode_resource_id(record.arn),
                )
            )
            continue
        matched_right_arns.add(counterpart.arn)
        pairs.append(
            ResourcePair(
                kind=record.kind.value,
                left_identifier=record.identifier,
                right_identifier=counterpart.identifier,
                left_resource_id=encode_resource_id(record.arn),
                right_resource_id=encode_resource_id(counterpart.arn),
                differences=_differences(
                    record,
                    left_attributes_by_arn.get(record.arn, {}),
                    right_attributes_by_arn.get(counterpart.arn, {}),
                ),
            )
        )
    unmatched_right = [
        UnmatchedResource(
            kind=record.kind.value, identifier=record.identifier, resource_id=encode_resource_id(record.arn)
        )
        for record in right_records
        if record.arn not in matched_right_arns
    ]
    logger.info(
        "environment_comparison_completed",
        left_slug=left_slug,
        right_slug=right_slug,
        pair_count=len(pairs),
        unmatched_left_count=len(unmatched_left),
        unmatched_right_count=len(unmatched_right),
    )
    return EnvironmentComparison(
        left_slug=left_slug,
        right_slug=right_slug,
        pairs=pairs,
        unmatched_left=unmatched_left,
        unmatched_right=unmatched_right,
    )
