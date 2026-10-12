"""Read-only RDS discovery filtered to PostgreSQL engines."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from botocore.client import BaseClient

from rdscommander.logging_setup import get_logger

logger = get_logger(__name__)

POSTGRES_ENGINES = ("postgres", "aurora-postgresql")
POSTGRES_FAMILY_PREFIXES = ("postgres", "aurora-postgresql")
UNSUPPORTED_MARKER = "has not been implemented"
CLUSTER_SCOPE = "cluster"
"""The scope of cluster parameter groups; every other group is an instance (DB) parameter group."""


class SimulatorUnsupportedAction(RuntimeError):  # noqa: N818 - the name states the condition, not a fault
    """The simulated account cannot answer this API call; real accounts never raise it."""


def _is_simulator_unsupported(error: Exception) -> bool:
    """True when `error` carries the simulator's "action has not been implemented" wording.

    The in-process simulator raises `NotImplementedError`; the simulator container answers a bare HTTP 500
    page, which simulated clients reword the same way (`sessions.name_the_action_the_simulator_has_not_
    implemented`), so botocore's parse error carries the wording too.
    """
    return UNSUPPORTED_MARKER in str(error)


@contextmanager
def _translate_simulator_unsupported(operation_name: str) -> Iterator[None]:
    """Translate the simulator's "not implemented" failure into `SimulatorUnsupportedAction`.

    Any other exception passes through unchanged.
    """
    try:
        yield
    except Exception as error:  # noqa: BLE001 - only the simulator's "not implemented" is translated
        if _is_simulator_unsupported(error):
            raise SimulatorUnsupportedAction(operation_name) from error
        raise


def tags_to_mapping(tag_list: list[dict[str, str]] | None) -> dict[str, str]:
    """`[{Key, Value}]` to a plain mapping."""
    return {tag["Key"]: tag.get("Value", "") for tag in tag_list or []}


class RdsReader:
    """Paginated describe calls over one RDS client."""

    def __init__(self, rds_client: BaseClient) -> None:
        self.rds = rds_client

    def _paginate(self, operation_name: str, result_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in self.rds.get_paginator(operation_name).paginate(**kwargs):
            items.extend(page.get(result_key, []))
        return items

    def _paginate_or_unsupported(
        self, operation_name: str, result_key: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """`_paginate`, raising `SimulatorUnsupportedAction` when the simulator lacks the call."""
        with _translate_simulator_unsupported(operation_name):
            return self._paginate(operation_name, result_key, **kwargs)

    def list_postgres_instances(self) -> list[dict[str, Any]]:
        """Instances whose engine is postgres or aurora-postgresql."""
        return [
            i
            for i in self._paginate("describe_db_instances", "DBInstances")
            if i.get("Engine") in POSTGRES_ENGINES
        ]

    def list_postgres_clusters(self) -> list[dict[str, Any]]:
        """Aurora PostgreSQL clusters."""
        return [
            c
            for c in self._paginate("describe_db_clusters", "DBClusters")
            if c.get("Engine") in POSTGRES_ENGINES
        ]

    def list_instance_parameter_groups(self) -> list[dict[str, Any]]:
        """Instance parameter groups of PostgreSQL families."""
        return [
            g
            for g in self._paginate("describe_db_parameter_groups", "DBParameterGroups")
            if str(g.get("DBParameterGroupFamily", "")).startswith(POSTGRES_FAMILY_PREFIXES)
        ]

    def list_cluster_parameter_groups(self) -> list[dict[str, Any]]:
        """Cluster parameter groups of Aurora PostgreSQL families."""
        return [
            g
            for g in self._paginate("describe_db_cluster_parameter_groups", "DBClusterParameterGroups")
            if str(g.get("DBParameterGroupFamily", "")).startswith(POSTGRES_FAMILY_PREFIXES)
        ]

    def list_parameters(self, group_name: str) -> list[dict[str, Any]]:
        """Every parameter of an instance parameter group."""
        return self._paginate("describe_db_parameters", "Parameters", DBParameterGroupName=group_name)

    def list_cluster_parameters(self, group_name: str) -> list[dict[str, Any]]:
        """Every parameter of a cluster parameter group."""
        return self._paginate(
            "describe_db_cluster_parameters", "Parameters", DBClusterParameterGroupName=group_name
        )

    def list_subnet_groups(self) -> list[dict[str, Any]]:
        """All DB subnet groups."""
        return self._paginate("describe_db_subnet_groups", "DBSubnetGroups")

    def list_tags(self, resource_arn: str) -> dict[str, str]:
        """Tags of any RDS resource."""
        return tags_to_mapping(self.rds.list_tags_for_resource(ResourceName=resource_arn).get("TagList"))

    def list_db_snapshots(self) -> list[dict[str, Any]]:
        """Instance snapshots of PostgreSQL engines, manual and automated.

        Returns:
            The `describe_db_snapshots` entries whose engine is postgres or aurora-postgresql.
        """
        return [
            snapshot
            for snapshot in self._paginate("describe_db_snapshots", "DBSnapshots")
            if snapshot.get("Engine") in POSTGRES_ENGINES
        ]

    def list_cluster_snapshots(self) -> list[dict[str, Any]]:
        """Cluster snapshots of Aurora PostgreSQL.

        Returns:
            The `describe_db_cluster_snapshots` entries whose engine is postgres or aurora-postgresql.
        """
        return [
            snapshot
            for snapshot in self._paginate("describe_db_cluster_snapshots", "DBClusterSnapshots")
            if snapshot.get("Engine") in POSTGRES_ENGINES
        ]

    def list_pending_maintenance_actions(self) -> list[dict[str, Any]]:
        """Pending maintenance actions for every resource in the region.

        Returns:
            One entry per resource with `ResourceIdentifier` and `PendingMaintenanceActionDetails`; an
            empty list when the simulator cannot answer the call (logged as `simulator_action_unsupported`).
        """
        operation_name = "describe_pending_maintenance_actions"
        try:
            return self._paginate_or_unsupported(operation_name, "PendingMaintenanceActions")
        except SimulatorUnsupportedAction:
            logger.info(
                "simulator_action_unsupported",
                operation_name=operation_name,
                treated_as="no pending maintenance actions",
            )
            return []

    def describe_engine_default_parameters(self, family: str, scope: str) -> list[dict[str, Any]]:
        """Default parameters and metadata of a parameter group family, for groups of one scope.

        Instance (DB) parameter groups use `describe_engine_default_parameters`; cluster parameter groups use
        `describe_engine_default_cluster_parameters`, which has no paginator and is walked by `Marker`. An
        Aurora family has both, and they list different parameters.

        Args:
            family: Parameter group family, e.g. `postgres16` or `aurora-postgresql16`.
            scope: `instance` for instance parameter groups, `cluster` for cluster parameter groups.

        Returns:
            The family's parameter descriptions (name, default value, apply type, allowed values, ...).

        Raises:
            SimulatorUnsupportedAction: The simulated account cannot answer the call.

        Example:
            >>> RdsReader(rds_client).describe_engine_default_parameters("postgres16", "instance")[0][
            ...     "ParameterName"
            ... ]
            'work_mem'
        """
        items: list[dict[str, Any]] = []
        if scope == CLUSTER_SCOPE:
            operation_name = "describe_engine_default_cluster_parameters"
            with _translate_simulator_unsupported(operation_name):
                marker: str | None = None
                while True:
                    request: dict[str, Any] = {"DBParameterGroupFamily": family}
                    if marker:
                        request["Marker"] = marker
                    page = self.rds.describe_engine_default_cluster_parameters(**request)["EngineDefaults"]
                    items.extend(page.get("Parameters", []))
                    marker = page.get("Marker")
                    if not marker:
                        break
            return items
        operation_name = "describe_engine_default_parameters"
        with _translate_simulator_unsupported(operation_name):
            for page in self.rds.get_paginator(operation_name).paginate(DBParameterGroupFamily=family):
                items.extend(page.get("EngineDefaults", {}).get("Parameters", []))
        return items

    def list_engine_versions(self, engine: str) -> list[dict[str, Any]]:
        """Engine versions with their families and valid upgrade targets.

        Args:
            engine: `postgres` or `aurora-postgresql`.

        Returns:
            The `describe_db_engine_versions` entries for `engine`.

        Raises:
            SimulatorUnsupportedAction: The simulated account cannot answer the call.
        """
        return self._paginate_or_unsupported(
            "describe_db_engine_versions", "DBEngineVersions", Engine=engine, IncludeAll=False
        )
