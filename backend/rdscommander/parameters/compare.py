"""Parameter group comparison: against the family defaults or against another group.

The comparison reads only the latest inventory snapshot and the family metadata (the cached AWS data, or
the bundle), so it makes no call to AWS and works for real and simulated accounts alike.

Example:
    >>> comparison = compare_parameter_groups(  # doctest: +SKIP
    ...     session, console_config, tuned_arn, None, against_defaults=True
    ... )
    >>> [row.name for row in comparison.rows if row.differs]  # doctest: +SKIP
    ['log_min_duration_statement', 'work_mem']
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from sqlalchemy.orm import Session

from rdscommander.config.models import ConsoleConfig
from rdscommander.parameters.metadata import load_family_metadata
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import load_resource


class ParameterRow(BaseModel):
    """One parameter across the two sides.

    Attributes:
        name: Parameter name.
        left: The value set on the left group; None when the group leaves it at the default.
        right: The value on the right side (another group's, or the default when comparing with defaults);
            None when that group leaves it at the default.
        default: The family's default value, which may be an RDS formula.
        differs: Whether the two sides end up with different values once defaults are filled in.
        apply_type: `dynamic` or `static` (a static parameter needs a reboot).
        data_type: `integer`, `float`, `boolean`, `string` or `list`.
        description: AWS's one-line description.
    """

    name: str
    left: str | None
    right: str | None
    default: str | None
    differs: bool
    apply_type: str
    data_type: str
    description: str


class ParameterComparison(BaseModel):
    """The result of comparing two parameter groups, or a group with its family's defaults.

    Attributes:
        left_name: Name of the left group.
        right_name: Name of the right group, or `<family> defaults`.
        family_left: Family of the left group.
        family_right: Family of the right side.
        rows: One row per parameter overridden on either side (every family parameter when comparing with
            defaults), sorted by name.
    """

    left_name: str
    right_name: str
    family_left: str
    family_right: str
    rows: list[ParameterRow]


def _parameter_group(session: Session, console_config: ConsoleConfig, arn: str) -> ResourceRecord:
    """The parameter group record of `arn`.

    Raises:
        KeyError: The ARN is not in the latest inventory.
        ValueError: The resource is not a parameter group.
    """
    record = load_resource(session, console_config, arn)
    if record is None:
        raise KeyError(f"{arn} is not in the latest inventory")
    if record.kind != ResourceKind.PARAMETER_GROUP:
        raise ValueError(f"{record.identifier} is not a parameter group")
    return record


def _overrides(record: ResourceRecord) -> dict[str, Any]:
    """`{name: value}` of the user-set parameters of a group record."""
    return {name: entry["value"] for name, entry in record.facts.get("parameters", {}).items()}


def compare_parameter_groups(
    session: Session,
    console_config: ConsoleConfig,
    left_arn: str,
    right_arn: str | None,
    *,
    against_defaults: bool,
) -> ParameterComparison:
    """Rows for every parameter overridden on either side (or every family parameter when against defaults).

    Args:
        session: Control database session.
        console_config: Loaded console configuration.
        left_arn: ARN of the left parameter group.
        right_arn: ARN of the right parameter group; None compares with the family defaults.
        against_defaults: Compare with the family defaults even when `right_arn` is given.

    Returns:
        The comparison; the family metadata comes from the left group's family.

    Raises:
        KeyError: A group is not in the latest inventory, or its family is unknown.
        ValueError: A resource is not a parameter group.

    Example:
        >>> compare_parameter_groups(  # doctest: +SKIP
        ...     session, console_config, tuned_arn, other_arn, against_defaults=False
        ... ).right_name
        'other'
    """
    left = _parameter_group(session, console_config, left_arn)
    family_left = left.facts["parameter_group_family"]
    simulated = console_config.target(left.target_key).simulated
    metadata = load_family_metadata(
        session, family_left, None, scope=left.facts["group_scope"], simulated=simulated
    )
    left_values = _overrides(left)
    compare_with_defaults = against_defaults or right_arn is None
    if compare_with_defaults:
        right_values: dict[str, Any] = {}
        right_name, family_right = f"{family_left} defaults", family_left
        names = sorted(set(metadata.parameters) | set(left_values))
    else:
        right = _parameter_group(session, console_config, right_arn)
        right_values = _overrides(right)
        right_name, family_right = right.identifier, right.facts["parameter_group_family"]
        names = sorted(set(left_values) | set(right_values))
    rows = []
    for name in names:
        parameter = metadata.parameters.get(name)
        default = parameter.default_value if parameter else None
        left_value = left_values.get(name)
        right_value = default if compare_with_defaults else right_values.get(name)
        effective_left = left_value if left_value is not None else default
        effective_right = right_value if right_value is not None else default
        rows.append(
            ParameterRow(
                name=name,
                left=left_value,
                right=right_value,
                default=default,
                differs=effective_left != effective_right,
                apply_type=parameter.apply_type if parameter else "dynamic",
                data_type=parameter.data_type if parameter else "string",
                description=parameter.description if parameter else "",
            )
        )
    return ParameterComparison(
        left_name=left.identifier,
        right_name=right_name,
        family_left=family_left,
        family_right=family_right,
        rows=rows,
    )
