"""Family metadata from AWS (cached seven days) or the bundle, and parameter value validation.

Metadata is kept per parameter group family and scope: instance (DB) parameter groups and cluster parameter
groups of the same family list different parameters (an Aurora family has both kinds of group). It is cached
in the control database together with its source. A real account reads it from AWS
(`DescribeEngineDefaultParameters` for instance groups, `DescribeEngineDefaultClusterParameters` for cluster
groups); the simulated account, which cannot answer those calls, reads the bundled files in
`rdscommander/parameters/families`, where each parameter lists the scopes it belongs to under `Scopes`. Both
use AWS's field names so one loader reads both. The bundle never stands in for AWS data in the cache: a real
account ignores bundled rows.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from rdscommander.aws.rds_reader import RdsReader, SimulatorUnsupportedAction
from rdscommander.control_db.models import EngineParameterFamily, EngineVersion, utc_now
from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)
FAMILY_PACKAGE = "rdscommander.parameters.families"
BUNDLED_FAMILIES: tuple[str, ...] = (
    "postgres15",
    "postgres16",
    "postgres17",
    "aurora-postgresql15",
    "aurora-postgresql16",
    "aurora-postgresql17",
)
DEFAULT_CACHE_MAX_AGE = timedelta(days=7)
ParameterScope = Literal["instance", "cluster"]
"""`instance` for instance (DB) parameter groups, `cluster` for cluster parameter groups."""
BUNDLED_SCOPES_KEY = "Scopes"
"""Bundle-only field of a parameter: the scopes whose groups have it. Absent means instance groups only."""
DEFAULT_BUNDLED_SCOPES: tuple[ParameterScope, ...] = ("instance",)
DEFAULTS_API_BY_SCOPE: dict[str, str] = {
    "instance": "DescribeEngineDefaultParameters",
    "cluster": "DescribeEngineDefaultClusterParameters",
}
"""The AWS call that lists the defaults of a family for groups of each scope."""
FAMILY_NAME_PATTERN = re.compile(r"(?:postgres|aurora-postgresql)\d+(?:\.\d+)?")
# RDS formulas such as `{DBInstanceClassMemory/32768}` or `LEAST({DBInstanceClassMemory/9531392},5000)`.
FORMULA_PATTERN = re.compile(r"^(GREATEST|LEAST)?\(?\{.*\}.*$")
INTEGER_PATTERN = re.compile(r"[+-]?\d+")
NUMBER_TEXT = r"(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
NUMBER_PATTERN = re.compile(rf"[+-]?{NUMBER_TEXT}")
# AllowedValues such as `64-2147483647`, `-1-86400` or `0-1.79769e+308`.
NUMERIC_RANGE_PATTERN = re.compile(rf"(-?{NUMBER_TEXT})-(-?{NUMBER_TEXT})")
NUMERIC_DATA_TYPES = ("integer", "float")


class UnknownParameterFamilyError(KeyError):
    """The family is not a PostgreSQL parameter group family that AWS or the bundle knows."""

    def __init__(self, family: str, scope: str | None = None) -> None:
        """Remember the family and word the message.

        Args:
            family: The family that could not be found, e.g. `postgres9`.
            scope: The scope it was asked for, when the family is known but has no groups of that scope.
        """
        message = f"unknown parameter group family {family!r}"
        if scope is not None:
            message = f"parameter group family {family!r} has no {scope} parameter groups"
        super().__init__(message)
        self.family = family
        self.scope = scope

    def __str__(self) -> str:
        """The plain message (a `KeyError` would show it quoted)."""
        return str(self.args[0])


class ParameterMetadata(BaseModel):
    """One parameter of a family.

    Attributes:
        name: Parameter name, e.g. `work_mem`.
        default_value: The engine default, which may be an RDS formula; None when the family sets none.
        description: AWS's one-line description.
        data_type: `integer`, `float`, `boolean`, `string` or `list`.
        allowed_values: AWS's `AllowedValues`: a range such as `64-2147483647` or choices such as `0,1`.
        is_modifiable: False for parameters only RDS may change.
        apply_type: `dynamic` parameters apply without a reboot, `static` ones need a reboot.
        apply_method_default: The apply method the console proposes: `immediate` or `pending-reboot`.
        minimum_engine_version: First engine version that has the parameter, when AWS says.
    """

    name: str
    default_value: str | None
    description: str
    data_type: str
    allowed_values: str | None
    is_modifiable: bool
    apply_type: Literal["dynamic", "static"]
    apply_method_default: Literal["immediate", "pending-reboot"]
    minimum_engine_version: str | None = None


class FamilyMetadata(BaseModel):
    """All parameters that groups of one family and scope have.

    Attributes:
        family: Family name, e.g. `postgres16`.
        scope: `instance` or `cluster`: which kind of parameter group the parameters are for (`instance`
            unless said otherwise, which is the only scope an RDS for PostgreSQL family has). It words
            messages and is left out of API answers, whose request names the scope already.
        fetched_at_utc: When the metadata was read from its source.
        parameters: The family's parameters keyed by name.
        source: `aws` when AWS answered, `bundled` when the bundled file was used.
    """

    family: str
    scope: ParameterScope = Field(default="instance", exclude=True)
    fetched_at_utc: datetime
    parameters: dict[str, ParameterMetadata]
    source: Literal["aws", "bundled"]

    @property
    def family_and_scope_label(self) -> str:
        """How messages name the groups the metadata is for.

        A family that has groups of both scopes (Aurora), and any cluster scope, is named with its scope;
        an RDS for PostgreSQL family has instance groups only and is named alone.

        Example:
            >>> aurora_instance_metadata.family_and_scope_label  # doctest: +SKIP
            'aurora-postgresql16 instance groups'
        """
        if self.scope == "instance" and not self.family.startswith("aurora-"):
            return self.family
        return f"{self.family} {self.scope} groups"


class ValidationProblem(BaseModel):
    """A value that cannot be set.

    Attributes:
        name: The parameter the problem is about.
        message: What is wrong, naming the parameter.
    """

    name: str
    message: str


class UpgradeTarget(BaseModel):
    """One valid upgrade target.

    Attributes:
        engine_version: The target version, e.g. `17.2`.
        is_major: True when upgrading to it changes the major version.
    """

    engine_version: str
    is_major: bool


class EngineVersionInfo(BaseModel):
    """One engine version.

    Attributes:
        engine: `postgres` or `aurora-postgresql`.
        engine_version: The version, e.g. `16.4`.
        parameter_group_family: The family its parameter groups use, e.g. `postgres16`.
        valid_upgrade_targets: Versions it can be upgraded to.
    """

    engine: str
    engine_version: str
    parameter_group_family: str
    valid_upgrade_targets: list[UpgradeTarget] = Field(default_factory=list)


def _from_aws_entry(entry: dict[str, Any]) -> ParameterMetadata:
    """One AWS-shaped parameter description (as returned by AWS or stored in the bundle) as metadata."""
    apply_type: Literal["dynamic", "static"] = (
        "static" if str(entry.get("ApplyType", "dynamic")).lower() == "static" else "dynamic"
    )
    return ParameterMetadata(
        name=entry["ParameterName"],
        default_value=entry.get("ParameterValue"),
        description=entry.get("Description") or "",
        data_type=str(entry.get("DataType") or "string").lower(),
        allowed_values=entry.get("AllowedValues") or None,
        is_modifiable=bool(entry.get("IsModifiable", False)),
        apply_type=apply_type,
        apply_method_default="pending-reboot" if apply_type == "static" else "immediate",
        minimum_engine_version=entry.get("MinimumEngineVersion"),
    )


def _bundled(family: str) -> dict[str, Any] | None:
    """The bundled file of `family`, or None when the bundle has no such family."""
    if family not in BUNDLED_FAMILIES:
        return None
    return json.loads(resources.files(FAMILY_PACKAGE).joinpath(f"{family}.json").read_text(encoding="utf-8"))


def _bundled_entries(family: str, scope: ParameterScope) -> list[dict[str, Any]]:
    """The parameter descriptions the bundled file of `family` lists for groups of `scope`.

    Each entry comes back AWS-shaped, without the bundle's own `Scopes` field.

    Raises:
        UnknownParameterFamilyError: The bundle lacks the family, or the family has no groups of `scope`.
    """
    bundled = _bundled(family)
    if bundled is None:
        logger.warning(
            "family_metadata_family_not_bundled",
            family=family,
            scope=scope,
            bundled_families=list(BUNDLED_FAMILIES),
            fix_suggestion="use one of the bundled families, or read the family from a real account",
        )
        raise UnknownParameterFamilyError(family)
    entries = [
        {key: value for key, value in entry.items() if key != BUNDLED_SCOPES_KEY}
        for entry in bundled["parameters"]
        if scope in entry.get(BUNDLED_SCOPES_KEY, DEFAULT_BUNDLED_SCOPES)
    ]
    if not entries:
        logger.warning(
            "family_metadata_scope_not_bundled",
            family=family,
            scope=scope,
            fix_suggestion=f"{family} has no {scope} parameter groups; use a family that has them",
        )
        raise UnknownParameterFamilyError(family, scope)
    return entries


def _store(
    session: Session,
    family: str,
    scope: ParameterScope,
    entries: list[dict[str, Any]],
    source: Literal["aws", "bundled"],
) -> EngineParameterFamily:
    """Insert or replace the cached row of `family` and `scope`, recording the source (caller commits)."""
    row = session.get(EngineParameterFamily, (family, scope))
    if row is None:
        row = EngineParameterFamily(family=family, scope=scope, parameters=entries, source=source)
        session.add(row)
    else:
        row.parameters = entries
        row.source = source
        row.fetched_at_utc = utc_now()
    session.flush()
    return row


def _is_stale(row: EngineParameterFamily, max_age: timedelta) -> bool:
    """True when the cached row is older than `max_age`."""
    return datetime.now(UTC) - row.fetched_at_utc > max_age


def _family_metadata(
    family: str,
    scope: ParameterScope,
    entries: list[dict[str, Any]],
    fetched_at_utc: datetime,
    source: Literal["aws", "bundled"],
) -> FamilyMetadata:
    """AWS-shaped parameter descriptions as the metadata of `family` for groups of `scope`."""
    return FamilyMetadata(
        family=family,
        scope=scope,
        fetched_at_utc=fetched_at_utc,
        parameters={entry["ParameterName"]: _from_aws_entry(entry) for entry in entries},
        source=source,
    )


def _bundled_family_metadata(
    session: Session,
    family: str,
    scope: ParameterScope,
    row: EngineParameterFamily | None,
    max_age: timedelta,
) -> FamilyMetadata:
    """The family's `scope` from the bundled file, also recorded in the cache unless AWS data is there.

    The row is written when it is absent and refreshed when it is a stale bundled row; a row that holds
    AWS data is never touched.
    """
    entries = _bundled_entries(family, scope)
    if row is None or (row.source == "bundled" and _is_stale(row, max_age)):
        row = _store(session, family, scope, entries, "bundled")
    fetched_at_utc = row.fetched_at_utc if row.source == "bundled" else utc_now()
    return _family_metadata(family, scope, entries, fetched_at_utc, "bundled")


def load_family_metadata(
    session: Session,
    family: str,
    reader: RdsReader | None,
    *,
    scope: ParameterScope,
    simulated: bool,
    max_age: timedelta = DEFAULT_CACHE_MAX_AGE,
) -> FamilyMetadata:
    """Metadata of a family for one scope of group: AWS (cached) for real accounts, the bundle if simulated.

    The control database keeps one row per family and scope, with the `source` that filled it. A simulated
    account always answers from the bundled file, so its answer never depends on what a real account cached;
    it records the bundle in the row only when the row is absent or already bundled, never over AWS data. A
    real account uses the row only when it holds fresh AWS data, otherwise it reads AWS and replaces the row.
    The bundle answers a real account solely when the account cannot answer (`SimulatorUnsupportedAction`),
    and is then recorded only if no AWS row exists. A real account asked without a reader (a read-only path
    that makes no AWS call, such as validating a request or comparing groups) gets the fresh AWS row when
    there is one and otherwise the bundle, which is then not recorded. The caller commits the session.

    Args:
        session: Control database session.
        family: Parameter group family, e.g. `postgres16` or `aurora-postgresql16`.
        reader: Reader over the target's RDS client; a real account without one is answered from the cache
            or the bundle and never calls AWS. Unused when simulated.
        scope: The scope of the group being worked on: `instance` reads the instance (DB) parameter group
            defaults, `cluster` the cluster parameter group defaults.
        simulated: True for the simulated account, whose metadata always comes from the bundle.
        max_age: How long cached AWS data stays fresh.

    Returns:
        The metadata of the family's `scope`, with `source` saying whether AWS or the bundle supplied it.

    Raises:
        UnknownParameterFamilyError: Neither AWS nor the bundle knows the family for groups of that scope
            (a `KeyError`).
        Exception: Whatever AWS raised for a real account; it is logged, never replaced by the bundle.

    Example:
        >>> family = load_family_metadata(session, "postgres16", None, scope="instance", simulated=True)
        >>> family.parameters["work_mem"].apply_type
        'dynamic'
    """
    if not FAMILY_NAME_PATTERN.fullmatch(family):
        logger.warning(
            "family_metadata_family_name_rejected",
            family=family,
            fix_suggestion="family names look like postgres16 or aurora-postgresql16",
        )
        raise UnknownParameterFamilyError(family)
    row = session.get(EngineParameterFamily, (family, scope))
    if simulated:
        return _bundled_family_metadata(session, family, scope, row, max_age)
    if row is not None and row.source == "aws" and not _is_stale(row, max_age):
        return _family_metadata(family, scope, row.parameters, row.fetched_at_utc, "aws")
    if reader is None:
        logger.info(
            "family_metadata_answered_from_bundle_without_reader",
            family=family,
            scope=scope,
            reason="no reader was given and the cache holds no fresh AWS data",
        )
        return _family_metadata(family, scope, _bundled_entries(family, scope), utc_now(), "bundled")
    defaults_api = DEFAULTS_API_BY_SCOPE[scope]
    logger.info("family_metadata_refresh_started", family=family, scope=scope, defaults_api=defaults_api)
    try:
        entries = reader.describe_engine_default_parameters(family, scope)
    except SimulatorUnsupportedAction:
        logger.info(
            "family_metadata_bundle_fallback",
            family=family,
            scope=scope,
            reason=f"the account cannot answer {defaults_api}",
        )
        return _bundled_family_metadata(session, family, scope, row, max_age)
    except Exception as error:
        logger.error(
            "family_metadata_aws_read_failed",
            family=family,
            scope=scope,
            error_type=type(error).__name__,
            error=str(error),
            fix_suggestion=(
                f"check that the identity behind this target may call rds:{defaults_api}, then retry"
            ),
        )
        raise
    if not entries:
        logger.warning(
            "family_metadata_family_unknown_to_aws",
            family=family,
            scope=scope,
            fix_suggestion=(
                f"use a family listed by the engine versions of this environment that has {scope} groups"
            ),
        )
        raise UnknownParameterFamilyError(family, scope if scope == "cluster" else None)
    row = _store(session, family, scope, entries, "aws")
    logger.info(
        "family_metadata_loaded", family=family, scope=scope, source="aws", parameter_count=len(entries)
    )
    return _family_metadata(family, scope, entries, row.fetched_at_utc, "aws")


def _number_bounds(allowed_values: str) -> tuple[str, str] | None:
    """The two ends of a numeric range such as `64-2147483647`, as written; None for anything else."""
    if "," in allowed_values:
        return None
    match = NUMERIC_RANGE_PATTERN.fullmatch(allowed_values)
    return (match.group(1), match.group(2)) if match else None


def _validate_number(metadata: ParameterMetadata, value: str, bounds: tuple[str, str]) -> str | None:
    """Message when `value` is not a number of the parameter's type within `bounds`."""
    is_integer = metadata.data_type == "integer"
    if not (INTEGER_PATTERN if is_integer else NUMBER_PATTERN).fullmatch(value):
        return f"{metadata.name} must be {'an integer' if is_integer else 'a number'}"
    low_text, high_text = bounds
    if not float(low_text) <= float(value) <= float(high_text):
        return f"{metadata.name} must be between {low_text} and {high_text}"
    return None


def validate_parameter_value(metadata: ParameterMetadata, value: str) -> str | None:
    """Why `value` cannot be set on the parameter, or None when it can.

    A parameter RDS does not let anyone modify is refused. RDS formulas such as
    `{DBInstanceClassMemory/32768}` are accepted because RDS evaluates them. Otherwise the value is checked
    against the parameter's type and `AllowedValues`: numeric ranges, choices, or (for `list` parameters
    such as `shared_preload_libraries`) every comma-separated element against the choices.

    Args:
        metadata: The parameter's metadata.
        value: The proposed value, as RDS takes it (always text).

    Returns:
        A message that names the parameter, or None when the value is acceptable.

    Example:
        >>> validate_parameter_value(work_mem_metadata, "10")
        'work_mem must be between 64 and 2147483647'
    """
    if not metadata.is_modifiable:
        return f"{metadata.name} cannot be modified"
    if FORMULA_PATTERN.match(value):
        return None  # RDS formulas such as {DBInstanceClassMemory/32768} are evaluated by RDS
    allowed_values = (metadata.allowed_values or "").strip()
    bounds = _number_bounds(allowed_values)
    if metadata.data_type in NUMERIC_DATA_TYPES and bounds is not None:
        return _validate_number(metadata, value, bounds)
    choices = [choice.strip() for choice in allowed_values.split(",") if choice.strip()]
    if metadata.data_type == "list" and choices:
        rejected = [
            item for item in (part.strip() for part in value.split(",")) if item and item not in choices
        ]
        if rejected:
            verb = "is" if len(rejected) == 1 else "are"
            return (
                f"{metadata.name} must be a comma-separated list of {', '.join(choices)} "
                f"({', '.join(rejected)} {verb} not allowed)"
            )
        return None
    if metadata.data_type == "boolean":
        choices = choices or ["0", "1"]
    if len(choices) > 1 and value not in choices:
        return f"{metadata.name} must be one of {', '.join(choices)}"
    return None


def validate_parameters(family: FamilyMetadata, values: dict[str, str]) -> list[ValidationProblem]:
    """Every problem in `values` against `family`.

    Args:
        family: The family and scope of the parameter group the values are for.
        values: Proposed values keyed by parameter name.

    Returns:
        One problem per unknown parameter or unacceptable value; empty when everything can be set. An
        unknown name is worded with the family and, for Aurora and cluster groups, the scope
        (`max_connections is not a parameter of aurora-postgresql16 cluster groups`).

    Example:
        >>> [p.message for p in validate_parameters(family, {"work_mem": "abc"})]
        ['work_mem must be an integer']
    """
    problems: list[ValidationProblem] = []
    for name, value in values.items():
        metadata = family.parameters.get(name)
        if metadata is None:
            problems.append(
                ValidationProblem(
                    name=name, message=f"{name} is not a parameter of {family.family_and_scope_label}"
                )
            )
            continue
        message = validate_parameter_value(metadata, value)
        if message:
            problems.append(ValidationProblem(name=name, message=message))
    return problems


def _bundled_engine_versions(engine: str) -> list[EngineVersionInfo]:
    """Every bundled engine version of `engine`, in the order of `BUNDLED_FAMILIES`."""
    infos: list[EngineVersionInfo] = []
    for family in BUNDLED_FAMILIES:
        bundled = _bundled(family)
        if bundled is None or bundled["engine"] != engine:
            continue
        infos.extend(
            EngineVersionInfo(
                engine=engine,
                engine_version=version["engine_version"],
                parameter_group_family=family,
                valid_upgrade_targets=[
                    UpgradeTarget(**target) for target in version["valid_upgrade_targets"]
                ],
            )
            for version in bundled["engine_versions"]
        )
    return infos


def _aws_engine_versions(engine: str, entries: list[dict[str, Any]]) -> list[EngineVersionInfo]:
    """`DescribeDBEngineVersions` entries as engine version infos."""
    return [
        EngineVersionInfo(
            engine=engine,
            engine_version=entry["EngineVersion"],
            parameter_group_family=entry["DBParameterGroupFamily"],
            valid_upgrade_targets=[
                UpgradeTarget(
                    engine_version=target["EngineVersion"], is_major=bool(target.get("IsMajorVersionUpgrade"))
                )
                for target in entry.get("ValidUpgradeTarget", [])
            ],
        )
        for entry in entries
    ]


def _to_engine_version_info(row: EngineVersion) -> EngineVersionInfo:
    """A cached row as an engine version info."""
    return EngineVersionInfo(
        engine=row.engine,
        engine_version=row.engine_version,
        parameter_group_family=row.parameter_group_family,
        valid_upgrade_targets=[UpgradeTarget.model_validate(target) for target in row.valid_upgrade_targets],
    )


def load_engine_versions(
    session: Session,
    engine: str,
    reader: RdsReader | None,
    *,
    simulated: bool,
    max_age: timedelta = DEFAULT_CACHE_MAX_AGE,
) -> list[EngineVersionInfo]:
    """Engine versions of an engine; AWS (cached) for real accounts, the bundled files for the simulator.

    The control database holds only what AWS answered. A simulated account reads the bundled files directly
    and neither reads nor writes the cache, so its answer never depends on what a real account cached; the
    bundle answers a real account solely when the account cannot answer (`SimulatorUnsupportedAction`) and
    is not recorded. Cached rows older than `max_age` are replaced. The caller commits the session.

    Args:
        session: Control database session.
        engine: `postgres` or `aurora-postgresql`.
        reader: Reader over the target's RDS client; required for a real account, unused when simulated.
        simulated: True for the simulated account, whose versions always come from the bundle.
        max_age: How long cached AWS data stays fresh.

    Returns:
        The engine's versions with their families and valid upgrade targets.

    Raises:
        ValueError: A real account was asked for without a reader.
        Exception: Whatever AWS raised for a real account; it is logged, never replaced by the bundle.

    Example:
        >>> [v.engine_version for v in load_engine_versions(session, "postgres", None, simulated=True)][:2]
        ['15.8', '16.4']
    """
    if simulated:
        return _bundled_engine_versions(engine)
    rows = (
        session.execute(
            select(EngineVersion).where(EngineVersion.engine == engine).order_by(EngineVersion.id)
        )
        .scalars()
        .all()
    )
    now = datetime.now(UTC)
    if rows and all(now - row.fetched_at_utc <= max_age for row in rows):
        return [_to_engine_version_info(row) for row in rows]
    if reader is None:
        logger.error(
            "engine_versions_reader_missing",
            engine=engine,
            fix_suggestion="pass a reader over the target's RDS client when the target is not simulated",
        )
        raise ValueError(f"a real account needs a reader to read the engine versions of {engine}")
    logger.info("engine_versions_refresh_started", engine=engine)
    try:
        entries = reader.list_engine_versions(engine)
    except SimulatorUnsupportedAction:
        logger.info(
            "engine_versions_bundle_fallback",
            engine=engine,
            reason="the account cannot answer DescribeDBEngineVersions",
        )
        return _bundled_engine_versions(engine)
    except Exception as error:
        logger.error(
            "engine_versions_aws_read_failed",
            engine=engine,
            error_type=type(error).__name__,
            error=str(error),
            fix_suggestion=(
                "check that the identity behind this target may call rds:DescribeDBEngineVersions, then retry"
            ),
        )
        raise
    infos = _aws_engine_versions(engine, entries)
    session.execute(delete(EngineVersion).where(EngineVersion.engine == engine))
    session.add_all(
        EngineVersion(
            engine=engine,
            engine_version=info.engine_version,
            parameter_group_family=info.parameter_group_family,
            valid_upgrade_targets=[target.model_dump() for target in info.valid_upgrade_targets],
        )
        for info in infos
    )
    session.flush()
    logger.info("engine_versions_loaded", engine=engine, version_count=len(infos))
    return infos


def family_for_version(engine: str, engine_version: str, versions: list[EngineVersionInfo]) -> str:
    """The parameter group family of an engine version.

    Falls back to `<engine><major>` (`postgres16`, `aurora-postgresql16`) when the version is not listed,
    which is how every PostgreSQL family is named from major 10 on.

    Args:
        engine: `postgres` or `aurora-postgresql`.
        engine_version: The version, e.g. `16.4`.
        versions: Known versions, from `load_engine_versions`.

    Returns:
        The family name.

    Example:
        >>> family_for_version("postgres", "17.4", [])
        'postgres17'
    """
    for info in versions:
        if info.engine == engine and info.engine_version == engine_version:
            return info.parameter_group_family
    return f"{engine}{engine_version.split('.')[0]}"
