"""Valid upgrade targets of a resource's engine version, as the engine-versions table lists them.

The engine-versions table (the bundle for the simulated account, `DescribeDBEngineVersions` for a real one,
see `parameters.metadata.load_engine_versions`) alone decides which versions a resource may move to. The
parameter group family of a target is never consulted here: a version AWS lists before the console knows its
family's parameter metadata is still a valid target.

Example:
    >>> versions = load_engine_versions(session, "postgres", None, simulated=True)  # doctest: +SKIP
    >>> valid_upgrade_targets(versions, "postgres", "16.4", "minor")  # doctest: +SKIP
    ['16.6']
    >>> valid_upgrade_targets(versions, "postgres", "16.4", "major")  # doctest: +SKIP
    ['17.2']
"""

from __future__ import annotations

from typing import Literal

from rdscommander.logging_setup import get_logger
from rdscommander.operations.change_models import ChangeDiagnosticError
from rdscommander.operations.executor import StepContext
from rdscommander.parameters.changes import reader_for
from rdscommander.parameters.metadata import EngineVersionInfo, load_engine_versions

logger = get_logger(__name__)

UpgradeKind = Literal["minor", "major"]
"""`minor` stays on the current major version; `major` moves to another one."""


class UpgradeTargetNotValid(ChangeDiagnosticError):  # noqa: N818 - the name is part of the contract
    """The requested version is not one the engine-versions table lists for the current version."""

    diagnostic_kind = "upgrade_target_not_valid"
    title = "That version is not a valid upgrade target"
    fix_suggestion = "choose one of the versions AWS lists as an upgrade target of the current version"


def major_version_of(engine_version: str) -> str:
    """The major version of a PostgreSQL engine version (every major from 10 on is the first part).

    Example:
        >>> major_version_of("16.4")
        '16'
    """
    return engine_version.split(".", 1)[0]


def valid_upgrade_targets(
    versions: list[EngineVersionInfo], engine: str, current_engine_version: str, upgrade_kind: UpgradeKind
) -> list[str]:
    """The listed upgrade targets of `current_engine_version` of the given kind, in the table's order.

    A minor target is one the table does not mark as a major upgrade and that keeps the major version; a
    major target is every other listed target.

    Args:
        versions: The engine's versions, from `load_engine_versions`.
        engine: `postgres` or `aurora-postgresql`.
        current_engine_version: The version the resource runs now, e.g. `16.4`.
        upgrade_kind: `minor` or `major`.

    Returns:
        The target versions; empty when the current version is not listed or lists no target of that kind.

    Example:
        >>> valid_upgrade_targets(versions, "postgres", "16.4", "minor")  # doctest: +SKIP
        ['16.6']
    """
    current = next(
        (v for v in versions if v.engine == engine and v.engine_version == current_engine_version), None
    )
    if current is None:
        return []
    current_major = major_version_of(current_engine_version)
    targets: list[str] = []
    for target in current.valid_upgrade_targets:
        is_minor = not target.is_major and major_version_of(target.engine_version) == current_major
        if is_minor == (upgrade_kind == "minor"):
            targets.append(target.engine_version)
    return targets


def ensure_valid_upgrade_target(
    versions: list[EngineVersionInfo],
    engine: str,
    current_engine_version: str,
    target_engine_version: str,
    upgrade_kind: UpgradeKind,
    resource_identifier: str,
) -> None:
    """Refuse a target the table does not list for the current version as an upgrade of `upgrade_kind`.

    Args:
        versions: The engine's versions, from `load_engine_versions`.
        engine: `postgres` or `aurora-postgresql`.
        current_engine_version: The version the resource runs now.
        target_engine_version: The version asked for.
        upgrade_kind: `minor` or `major`.
        resource_identifier: The resource's identifier, for the message.

    Raises:
        UpgradeTargetNotValid: The target is not listed; the fix suggestion names the listed ones.

    Example:
        >>> ensure_valid_upgrade_target(versions, "postgres", "16.4", "16.6", "minor", "demo-orders")
    """
    listed = valid_upgrade_targets(versions, engine, current_engine_version, upgrade_kind)
    if target_engine_version in listed:
        return
    fix_suggestion = (
        f"choose one of the listed {upgrade_kind} targets of {current_engine_version}: {', '.join(listed)}"
        if listed
        else f"{current_engine_version} has no {upgrade_kind} upgrade target listed; "
        "refresh the engine versions or choose another kind of upgrade"
    )
    logger.error(
        "upgrade_target_not_valid",
        resource_identifier=resource_identifier,
        engine=engine,
        current_engine_version=current_engine_version,
        target_engine_version=target_engine_version,
        upgrade_kind=upgrade_kind,
        listed_targets=listed,
        fix_suggestion=fix_suggestion,
    )
    raise UpgradeTargetNotValid(
        f"{target_engine_version} is not a {upgrade_kind} upgrade target of {resource_identifier}'s "
        f"{engine} {current_engine_version}",
        fix_suggestion=fix_suggestion,
    )


def engine_versions_for(context: StepContext, engine: str, step_name: str) -> list[EngineVersionInfo]:
    """The engine's versions through the operation's target: the bundle if simulated, else AWS (cached).

    Args:
        context: The running step's context; its target decides where the versions come from.
        engine: `postgres` or `aurora-postgresql`.
        step_name: Step a `DescribeDBEngineVersions` call is recorded under.

    Returns:
        The engine's versions with their valid upgrade targets.

    Example:
        >>> engine_versions_for(context, "postgres", "compute_change_set")[0].engine_version  # doctest: +SKIP
        '16.4'
    """
    versions = load_engine_versions(
        context.session, engine, reader_for(context, step_name), simulated=context.target.simulated
    )
    context.session.commit()
    return versions
