"""The catalogue itself. Later tasks register their definitions by appending to `OPERATION_DEFINITIONS`."""

from __future__ import annotations

from typing import Any, NamedTuple

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from rdscommander.catalogue.definitions import (
    OPERATION_FIELDS_PLAN_KEY,
    ActionAvailability,
    Applicability,
    OperationDefinition,
    PlanPreparationError,
)
from rdscommander.catalogue.options import (
    DriftResolveOptions,
    EnvironmentImportOptions,
    InventoryRefreshOptions,
    NoOptions,
    ParameterGroupAttachOptions,
    ParameterGroupCreateOptions,
    ParameterGroupEditOptions,
    TerraformAdoptionOptions,
    UpgradeGroupBuildOptions,
)
from rdscommander.config.models import ConsoleConfig, EnvironmentConfig, ResolvedTarget
from rdscommander.control_db.models import DriftFinding, InventorySubnetGroup
from rdscommander.drift.open_findings import (
    describe_open_drift,
    open_record_drift_findings,
    resolve_drift_first,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.lifecycle.options import (
    BackupSettingsOptions,
    ClusterCreateOptions,
    ClusterReaderCreateOptions,
    DecommissionOptions,
    DeletionProtectionOptions,
    FailoverOptions,
    InstanceClassChangeOptions,
    InstanceCreateOptions,
    MaintenanceWindowOptions,
    MonitoringOptions,
    MultiAzChangeOptions,
    PendingMaintenanceOptions,
    PointInTimeRestoreOptions,
    PromoteReplicaOptions,
    ReadReplicaCreateOptions,
    RebootInstanceOptions,
    ServerlessCapacityOptions,
    SnapshotRestoreOptions,
    StorageChangeOptions,
    TagsOptions,
    TakeSnapshotOptions,
)
from rdscommander.lifecycle.restore_window import (
    format_utc,
    parse_utc,
    restore_window,
    restore_window_unknown_reason,
)
from rdscommander.logging_setup import get_logger
from rdscommander.operations.executor import StepExecutor
from rdscommander.parameters.changes import (
    attach_problems,
    clone_parameters,
    create_problems,
    group_name_is_taken,
    parameter_problems,
)
from rdscommander.parameters.metadata import (
    FamilyMetadata,
    ParameterScope,
    UnknownParameterFamilyError,
    family_for_version,
    load_family_metadata,
)
from rdscommander.parameters.upgrade_builder import (
    UpgradePlanError,
    new_group_name_for,
    upgrade_sources,
    was_built_from_same_source,
)
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import (
    BLUE_GREEN_DEPLOYMENT_LEFT_FACT,
    find_parameter_group,
    list_resources,
)
from rdscommander.terraform.adoption_plan import build_adoption_plan
from rdscommander.upgrades.blue_green_api import deployments_left_in_place
from rdscommander.upgrades.options import (
    AutoMinorUpgradeOptions,
    BlueGreenCleanupOptions,
    MajorUpgradeOptions,
    MinorUpgradeOptions,
)

logger = get_logger(__name__)

GROUP_DISCOVERY = "Discovery and adoption"
GROUP_PARAMETER_GROUPS = "Parameter groups"
GROUP_ENVIRONMENTS = "Environments"
GROUP_INSTANCES_AND_CLUSTERS = "Instances and clusters"
GROUP_REPLICAS_AND_SCALING = "Replicas and scaling"
GROUP_SNAPSHOTS = "Snapshots"
GROUP_LIFECYCLE = "Create and decommission"
GROUP_UPGRADES = "Upgrades"
FOREIGN_OWNERSHIP_PREFIX = "foreign_"
FOREIGN_OWNED_REFUSAL = "owned by other tooling; read-only here"
OPERATION_KINDS_WITHOUT_ENVIRONMENT = frozenset({"environment_import"})
"""Kinds whose operations belong to no environment: an import creates the environment it would belong to."""


def always(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Always applicable."""
    return Applicability(ok=True)


def requires_terraform_mode(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Only in Terraform-mode environments."""
    if environment.change_management != "terraform":
        return Applicability(
            ok=False, reason="this environment manages changes directly, not through Terraform"
        )
    return Applicability(ok=True)


def adoptable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Unmanaged, non-default resources in Terraform-mode environments."""
    gate = requires_terraform_mode(record, environment)
    if not gate.ok:
        return gate
    if record is None:
        return Applicability(ok=True)
    if record.facts.get("is_default"):
        return Applicability(ok=False, reason="AWS default parameter groups cannot be adopted")
    if record.ownership == "adopted":
        return Applicability(ok=False, reason="already adopted")
    if record.ownership.startswith("foreign_"):
        return Applicability(ok=False, reason="owned by other tooling")
    return Applicability(ok=True)


def has_open_drift(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """A resource with an open drift finding; at environment level, always (the plan names the resource)."""
    if record is None:
        return Applicability(ok=True)
    if not record.facts.get("has_open_drift"):
        return Applicability(ok=False, reason="no open drift on this resource")
    return Applicability(ok=True)


def _foreign_ownership_refusal(
    record: ResourceRecord | None, environment: EnvironmentConfig
) -> Applicability | None:
    """The refusal for a resource owned by other tooling, unless the environment allows changing it."""
    if environment.allow_direct_changes_to_foreign_owned or record is None:
        return None
    if record.ownership.startswith(FOREIGN_OWNERSHIP_PREFIX):
        return Applicability(ok=False, reason=FOREIGN_OWNED_REFUSAL)
    return None


def _change_gate(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Resources the console may change: not foreign-owned (unless allowed), adopted in Terraform mode."""
    if record is None:
        return Applicability(ok=True)
    refusal = _foreign_ownership_refusal(record, environment)
    if refusal is not None:
        return refusal
    if environment.change_management == "terraform" and record.ownership != "adopted":
        return Applicability(
            ok=False, reason="adopt it first: Terraform-mode environments change adopted resources only"
        )
    return Applicability(ok=True)


def _default_group_refusal(record: ResourceRecord | None) -> Applicability | None:
    """The refusal for an AWS default parameter group, or None for any other record."""
    if record is not None and record.facts.get("is_default"):
        return Applicability(
            ok=False, reason="AWS default groups cannot be edited; create a group from defaults instead"
        )
    return None


def parameter_group_editable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Non-default groups that pass the change gate."""
    return _default_group_refusal(record) or _change_gate(record, environment)


def parameter_group_deletable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Editable and unused.

    A group in use is refused as in use before ownership is looked at: it can never be deleted, whoever
    owns it.
    """
    refusal = _default_group_refusal(record)
    if refusal is not None:
        return refusal
    if record is not None and record.facts.get("used_by"):
        return Applicability(ok=False, reason=f"in use by {', '.join(record.facts['used_by'])}")
    return _change_gate(record, environment)


def attachable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Instances and clusters that pass the change gate."""
    return _change_gate(record, environment)


STORAGE_OPTIMIZATION_STATUS = "storage-optimization"
"""The status AWS reports for hours after a storage change; every other modification is allowed meanwhile."""
STATUSES_THAT_ALLOW_CHANGES = frozenset({"available", STORAGE_OPTIMIZATION_STATUS})
"""Instance and cluster statuses in which the console offers a change, an action or a create from it."""
STORAGE_CHANGE_WAITS_REASON = (
    "AWS is still optimising the storage after the last storage change; the next storage change is possible "
    "once that completes, and no sooner than six hours after the last one"
)


def status_allows_changes(status: str | None) -> bool:
    """Whether an instance or cluster in `status` may be changed: `available`, or optimising its storage.

    `storage-optimization` follows a storage change, often for hours, and AWS accepts every other
    modification meanwhile; only a further storage change waits (see `storage_changeable`).

    Example:
        >>> status_allows_changes("storage-optimization"), status_allows_changes("modifying")
        (True, False)
    """
    return status in STATUSES_THAT_ALLOW_CHANGES


def has_pending_reboot(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Instances: when available. Groups: only when an instance using the group is pending a reboot.

    Either way an instance the reboot would touch must not be owned by other tooling, unless the
    environment allows direct changes to foreign-owned resources: foreign-owned resources are read-only.
    """
    if record is not None and record.kind == ResourceKind.INSTANCE:
        if not status_allows_changes(record.status):
            return Applicability(ok=False, reason=f"instance is {record.status}")
        return _foreign_ownership_refusal(record, environment) or Applicability(ok=True)
    if record is not None and record.kind == ResourceKind.PARAMETER_GROUP:
        if not record.facts.get("pending_reboot_instances"):
            return Applicability(ok=False, reason="no instance using this group is pending a reboot")
        foreign_owned = sorted(
            identifier
            for identifier, ownership in record.facts.get("pending_reboot_instance_ownership", {}).items()
            if ownership.startswith(FOREIGN_OWNERSHIP_PREFIX)
        )
        if foreign_owned and not environment.allow_direct_changes_to_foreign_owned:
            verb = "is" if len(foreign_owned) == 1 else "are"
            return Applicability(
                ok=False, reason=f"{', '.join(foreign_owned)} {verb} {FOREIGN_OWNED_REFUSAL}"
            )
    return Applicability(ok=True)


def rebootable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Any available instance, or a parameter group with instances pending a reboot.

    An instance owned by other tooling is refused unless the environment allows direct changes to
    foreign-owned resources; a group follows `has_pending_reboot`, which refuses the same way for each
    instance it would reboot.

    Example:
        >>> rebootable(None, environment).reason  # doctest: +SKIP
        'pick an instance or a parameter group'
    """
    if record is None:
        return Applicability(ok=False, reason="pick an instance or a parameter group")
    if record.kind == ResourceKind.INSTANCE:
        if not status_allows_changes(record.status):
            return Applicability(ok=False, reason=f"the instance is {record.status}")
        refusal = _foreign_ownership_refusal(record, environment)
        return refusal or Applicability(ok=True)
    return has_pending_reboot(record, environment)


def instance_or_member_changeable(
    record: ResourceRecord | None, environment: EnvironmentConfig
) -> Applicability:
    """An instance (standalone or cluster member) that is available and may be changed here."""
    if record is None:
        return Applicability(ok=False, reason="pick an instance")
    if record.kind != ResourceKind.INSTANCE:
        return Applicability(ok=False, reason="applies to instances")
    if not status_allows_changes(record.status):
        return Applicability(ok=False, reason=f"the instance is {record.status}; wait until it is available")
    return _change_gate(record, environment)


def standalone_instance_changeable(
    record: ResourceRecord | None, environment: EnvironmentConfig
) -> Applicability:
    """An available instance that is not an Aurora member (storage and Multi-AZ live on the cluster there)."""
    availability = instance_or_member_changeable(record, environment)
    if not availability.ok:
        return availability
    assert record is not None
    if record.relationships.get("cluster_identifier"):
        return Applicability(ok=False, reason="set this on the cluster; Aurora members share its storage")
    return availability


def storage_changeable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """A standalone instance that may be changed, and is not still optimising the last storage change.

    Example:
        >>> storage_changeable(optimising_record, environment).reason  # doctest: +SKIP
        'AWS is still optimising the storage after the last storage change; ...'
    """
    availability = standalone_instance_changeable(record, environment)
    if availability.ok and record is not None and record.status == STORAGE_OPTIMIZATION_STATUS:
        return Applicability(ok=False, reason=STORAGE_CHANGE_WAITS_REASON)
    return availability


def instance_or_cluster_changeable(
    record: ResourceRecord | None, environment: EnvironmentConfig
) -> Applicability:
    """An available instance or cluster that may be changed here."""
    if record is None:
        return Applicability(ok=False, reason="pick an instance or cluster")
    if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        return Applicability(ok=False, reason="applies to instances and clusters")
    if not status_allows_changes(record.status):
        return Applicability(ok=False, reason=f"it is {record.status}; wait until it is available")
    return _change_gate(record, environment)


def instance_or_cluster_but_not_member_changeable(
    record: ResourceRecord | None, environment: EnvironmentConfig
) -> Applicability:
    """An available standalone instance or cluster; an Aurora member keeps these settings on its cluster.

    Aurora applies automated backups and deletion protection to the cluster, not to its members.

    Example:
        >>> instance_or_cluster_but_not_member_changeable(writer_record, environment).reason  # doctest: +SKIP
        'set this on the cluster; Aurora keeps backups and deletion protection there'
    """
    availability = instance_or_cluster_changeable(record, environment)
    if not availability.ok:
        return availability
    assert record is not None
    if record.kind == ResourceKind.INSTANCE and record.relationships.get("cluster_identifier"):
        return Applicability(
            ok=False, reason="set this on the cluster; Aurora keeps backups and deletion protection there"
        )
    return availability


def version_changeable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available standalone instance or cluster; an Aurora member runs the version of its cluster.

    Example:
        >>> version_changeable(writer_record, environment).reason  # doctest: +SKIP
        'upgrade the cluster; Aurora members run the engine version of their cluster'
    """
    availability = instance_or_cluster_changeable(record, environment)
    if not availability.ok:
        return availability
    assert record is not None
    if record.kind == ResourceKind.INSTANCE and record.relationships.get("cluster_identifier"):
        return Applicability(
            ok=False, reason="upgrade the cluster; Aurora members run the engine version of their cluster"
        )
    return availability


def blue_green_cleanable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An instance or cluster whose finished major upgrade left a blue/green deployment in place.

    Example:
        >>> blue_green_cleanable(orders_record, environment).reason  # doctest: +SKIP
        'no blue/green deployment of this resource is left in place'
    """
    if record is None:
        return Applicability(ok=False, reason="pick an instance or cluster")
    if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        return Applicability(ok=False, reason="applies to instances and clusters")
    refusal = _foreign_ownership_refusal(record, environment)
    if refusal is not None:
        return refusal
    if not record.facts.get(BLUE_GREEN_DEPLOYMENT_LEFT_FACT):
        return Applicability(ok=False, reason="no blue/green deployment of this resource is left in place")
    return Applicability(ok=True)


def cluster_changeable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available cluster that may be changed here."""
    availability = instance_or_cluster_changeable(record, environment)
    if availability.ok and record is not None and record.kind != ResourceKind.CLUSTER:
        return Applicability(ok=False, reason="applies to clusters")
    return availability


def serverless_cluster_changeable(
    record: ResourceRecord | None, environment: EnvironmentConfig
) -> Applicability:
    """An available cluster with a serverless v2 scaling configuration that may be changed here."""
    availability = cluster_changeable(record, environment)
    if availability.ok and record is not None and record.facts.get("serverless_v2_min_capacity") is None:
        return Applicability(
            ok=False,
            reason="the cluster has no serverless v2 members; capacity applies to serverless clusters",
        )
    return availability


def taggable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """Instances, clusters and parameter groups that may be changed here."""
    if record is None:
        return Applicability(ok=False, reason="pick an instance, cluster or parameter group")
    if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER, ResourceKind.PARAMETER_GROUP):
        return Applicability(ok=False, reason="applies to instances, clusters and parameter groups")
    if record.kind == ResourceKind.PARAMETER_GROUP and record.facts.get("is_default"):
        return Applicability(ok=False, reason="AWS default groups cannot be tagged")
    return _change_gate(record, environment)


def failover_possible(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available cluster with at least two members, not owned by other tooling.

    A failover changes nothing Terraform renders, so adoption is not required.

    Example:
        >>> failover_possible(None, environment).reason  # doctest: +SKIP
        'pick a cluster'
    """
    if record is None:
        return Applicability(ok=False, reason="pick a cluster")
    if record.kind != ResourceKind.CLUSTER:
        return Applicability(ok=False, reason="applies to clusters")
    if not status_allows_changes(record.status):
        return Applicability(ok=False, reason=f"the cluster is {record.status}; wait until it is available")
    member_count = len(record.relationships.get("member_identifiers", []))
    if member_count < 2:
        return Applicability(
            ok=False, reason=f"a failover needs at least two members; this cluster has {member_count}"
        )
    return _foreign_ownership_refusal(record, environment) or Applicability(ok=True)


def has_pending_maintenance(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An instance or cluster with a pending maintenance action, not owned by other tooling.

    Example:
        >>> has_pending_maintenance(None, environment).reason  # doctest: +SKIP
        'pick an instance or cluster'
    """
    if record is None:
        return Applicability(ok=False, reason="pick an instance or cluster")
    if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        return Applicability(ok=False, reason="applies to instances and clusters")
    if not record.facts.get("pending_maintenance_actions"):
        return Applicability(ok=False, reason="AWS has no maintenance pending on this resource")
    return _foreign_ownership_refusal(record, environment) or Applicability(ok=True)


def snapshot_source(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available instance that is not a cluster member, or an available cluster; not foreign-owned.

    A snapshot is a write on the resource, so a resource owned by other tooling is refused unless the
    environment allows direct changes to foreign-owned resources. Adoption is not required.

    Example:
        >>> snapshot_source(None, environment).reason  # doctest: +SKIP
        'pick an instance or cluster'
    """
    if record is None:
        return Applicability(ok=False, reason="pick an instance or cluster")
    if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        return Applicability(ok=False, reason="applies to instances and clusters")
    cluster_identifier = record.relationships.get("cluster_identifier")
    if record.kind == ResourceKind.INSTANCE and cluster_identifier:
        return Applicability(
            ok=False, reason=f"this instance is a member of {cluster_identifier}; snapshot the cluster"
        )
    if not status_allows_changes(record.status):
        return Applicability(ok=False, reason=f"it is {record.status}; wait until it is available")
    return _foreign_ownership_refusal(record, environment) or Applicability(ok=True)


def manual_snapshot(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available snapshot somebody took; AWS manages the automated ones.

    Snapshot records are never adopted or foreign-owned, so the change gate does not apply.

    Example:
        >>> manual_snapshot(None, environment).reason  # doctest: +SKIP
        'pick a snapshot'
    """
    if record is None:
        return Applicability(ok=False, reason="pick a snapshot")
    if record.kind != ResourceKind.SNAPSHOT:
        return Applicability(ok=False, reason="applies to snapshots")
    snapshot_type = record.facts.get("snapshot_type")
    if snapshot_type != "manual":
        return Applicability(
            ok=False,
            reason=f"{snapshot_type} snapshots are managed by AWS; only manual snapshots can be deleted here",
        )
    if record.status != "available":
        return Applicability(ok=False, reason=f"the snapshot is {record.status}; wait until it is available")
    return Applicability(ok=True)


RESTORABLE_SNAPSHOT_TYPES = ("manual", "automated")
"""Snapshot types a restore takes: one somebody took, or one of AWS's automated backups."""


def restorable_snapshot(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available manual or automated snapshot, of an instance or a cluster.

    A restore reads the snapshot and creates a new resource; the snapshot is not changed, so neither adoption
    nor ownership applies.

    Example:
        >>> restorable_snapshot(None, environment).reason  # doctest: +SKIP
        'pick a snapshot'
    """
    if record is None:
        return Applicability(ok=False, reason="pick a snapshot")
    if record.kind != ResourceKind.SNAPSHOT:
        return Applicability(ok=False, reason="applies to snapshots")
    snapshot_type = record.facts.get("snapshot_type")
    if snapshot_type not in RESTORABLE_SNAPSHOT_TYPES:
        return Applicability(
            ok=False,
            reason=f"{snapshot_type} snapshots cannot be restored here; copy it into this account first",
        )
    if record.status != "available":
        return Applicability(ok=False, reason=f"the snapshot is {record.status}; wait until it is available")
    return Applicability(ok=True)


def pitr_source(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available standalone instance or cluster with automated backups on.

    A point-in-time restore reads the source's backups and creates a new resource; the source is not changed,
    so neither adoption nor ownership applies. An Aurora member is refused: its cluster is restored instead.

    Example:
        >>> pitr_source(None, environment).reason  # doctest: +SKIP
        'pick an instance or cluster'
    """
    if record is None:
        return Applicability(ok=False, reason="pick an instance or cluster")
    if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        return Applicability(ok=False, reason="applies to instances and clusters")
    cluster_identifier = record.relationships.get("cluster_identifier")
    if record.kind == ResourceKind.INSTANCE and cluster_identifier:
        return Applicability(
            ok=False,
            reason=f"this instance is a member of {cluster_identifier}; restore the cluster instead",
        )
    if not status_allows_changes(record.status):
        return Applicability(ok=False, reason=f"it is {record.status}; wait until it is available")
    if not (record.facts.get("backup_retention_period") or 0) > 0:
        return Applicability(
            ok=False,
            reason=(
                "automated backups are off; turn them on (Backup settings) before a point-in-time restore, "
                "which can then reach back to when they were turned on"
            ),
        )
    return Applicability(ok=True)


def promotable_replica(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available read replica that may be changed here (adopted in Terraform mode, not foreign-owned).

    Promotion rewrites the replica's rendered block, so Terraform mode needs the replica adopted.

    Example:
        >>> promotable_replica(None, environment).reason  # doctest: +SKIP
        'pick a read replica'
    """
    if record is None:
        return Applicability(ok=False, reason="pick a read replica")
    if record.kind != ResourceKind.INSTANCE:
        return Applicability(ok=False, reason="applies to instances")
    if not record.relationships.get("read_replica_source_identifier"):
        return Applicability(ok=False, reason="this instance is not a read replica")
    if not status_allows_changes(record.status):
        return Applicability(ok=False, reason=f"the instance is {record.status}; wait until it is available")
    return _change_gate(record, environment)


def removable_reader(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available Aurora cluster member that is not the writer, and may be changed here.

    The writer comes from `facts["is_cluster_writer"]`, which the inventory sets from the cluster's
    `DBClusterMembers`; the executor checks it again against AWS before asking for approval.

    Example:
        >>> removable_reader(None, environment).reason  # doctest: +SKIP
        'pick an Aurora reader'
    """
    if record is None:
        return Applicability(ok=False, reason="pick an Aurora reader")
    if record.kind != ResourceKind.INSTANCE:
        return Applicability(ok=False, reason="applies to instances")
    cluster_identifier = record.relationships.get("cluster_identifier")
    if not cluster_identifier:
        return Applicability(ok=False, reason="only a reader of an Aurora cluster can be removed this way")
    if record.facts.get("is_cluster_writer"):
        return Applicability(
            ok=False,
            reason=f"this instance is the writer of {cluster_identifier}; fail over to another member first",
        )
    if record.status != "available":
        return Applicability(ok=False, reason=f"the instance is {record.status}; wait until it is available")
    return _change_gate(record, environment)


def decommissionable(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available instance without read replicas (a read replica included), or an available cluster.

    A cluster member is removed as a reader or with its cluster; a read replica is deleted without a final
    snapshot, since its data lives on its source; an instance whose replicas replicate from it keeps them
    alive, so they are decommissioned (or promoted) first. The executor checks the replicas and the
    cluster's members again against AWS before asking for approval.

    Example:
        >>> decommissionable(None, environment).reason  # doctest: +SKIP
        'pick an instance or cluster'
    """
    if record is None:
        return Applicability(ok=False, reason="pick an instance or cluster")
    if record.kind not in (ResourceKind.INSTANCE, ResourceKind.CLUSTER):
        return Applicability(ok=False, reason="applies to instances and clusters")
    if record.kind == ResourceKind.INSTANCE:
        cluster_identifier = record.relationships.get("cluster_identifier")
        if cluster_identifier:
            return Applicability(
                ok=False,
                reason=(
                    f"this instance is a member of {cluster_identifier}; decommission the cluster, or remove "
                    "this reader"
                ),
            )
        replicas = sorted(record.relationships.get("replica_identifiers") or [])
        if replicas:
            pronoun = "it" if len(replicas) == 1 else "them"
            noun = "read replica" if len(replicas) == 1 else "read replicas"
            verb = "depends" if len(replicas) == 1 else "depend"
            return Applicability(
                ok=False,
                reason=(
                    f"its {noun} {', '.join(replicas)} {verb} on it; promote or decommission {pronoun} first"
                ),
            )
    if not status_allows_changes(record.status):
        return Applicability(ok=False, reason=f"it is {record.status}; wait until it is available")
    return _change_gate(record, environment)


def replica_source(record: ResourceRecord | None, environment: EnvironmentConfig) -> Applicability:
    """An available standalone instance with automated backups on, that may be changed here.

    RDS replicates from the source's backups, so a source without automated backups cannot have a replica.
    An Aurora member is refused: a cluster scales by adding readers. AWS creates no read replica of an
    instance whose master password Secrets Manager manages (every instance created from the template).

    Example:
        >>> replica_source(None, environment).reason  # doctest: +SKIP
        'pick an instance'
    """
    if record is None:
        return Applicability(ok=False, reason="pick an instance")
    if record.kind != ResourceKind.INSTANCE:
        return Applicability(ok=False, reason="applies to instances")
    cluster_identifier = record.relationships.get("cluster_identifier")
    if cluster_identifier:
        return Applicability(
            ok=False,
            reason=f"this instance is a member of {cluster_identifier}; add a reader to the cluster instead",
        )
    if not status_allows_changes(record.status):
        return Applicability(ok=False, reason=f"the instance is {record.status}; wait until it is available")
    if not (record.facts.get("backup_retention_period") or 0) > 0:
        return Applicability(
            ok=False,
            reason=(
                "automated backups are off on this instance; turn them on (Backup settings) before creating "
                "a read replica"
            ),
        )
    if record.raw.get("MasterUserSecret"):
        return Applicability(
            ok=False,
            reason=(
                "its master password is managed by Secrets Manager; AWS cannot create a read replica of it"
            ),
        )
    return _change_gate(record, environment)


def refuse_a_change_to_a_resource_with_open_record_drift(
    session: Session, definition: OperationDefinition, record: ResourceRecord | None
) -> None:
    """Refuse a change to a resource whose open record drift the change would fold into the record.

    The change renders or records the resource from the latest inventory, which holds the outside value;
    committing it would claim an approval for a value nobody reviewed and the next Detect drift would close
    the finding as resolved. The operator resolves the drift first. The change strategy checks again when
    it works the change out, for every address the change touches.

    Raises:
        PlanPreparationError: The definition changes the resource and the resource has open record drift;
            the problem names the resource, its drifted attributes and "Resolve drift on <resource> first".

    Example:
        >>> refuse_a_change_to_a_resource_with_open_record_drift(  # doctest: +SKIP
        ...     session, definition_for("parameter_group_edit"), drifted_group_record
        ... )
        Traceback (most recent call last):
        PlanPreparationError: demo-postgres16-tuned has drift that is not resolved
    """
    if not definition.changes_the_resource or record is None:
        return
    findings = open_record_drift_findings(
        session,
        record.target_key,
        addresses={record.terraform_address} if record.terraform_address else set(),
        resource_arns={record.arn},
    )
    if not findings:
        return
    logger.info(
        "change_refused_for_open_drift",
        operation_kind=definition.kind,
        resource_arn=record.arn,
        finding_ids=[finding.id for finding in findings],
    )
    raise PlanPreparationError(
        f"{record.identifier} has drift that is not resolved",
        [f"resource_id: {describe_open_drift(findings)}; {resolve_drift_first(findings)}"],
    )


def prepare_adoption_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Validate the selection and build the adoption plan JSON."""
    assert isinstance(options, TerraformAdoptionOptions)
    return build_adoption_plan(
        session, console_config, options.target_key, options.resource_arns
    ).model_dump()


def prepare_drift_resolve_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check that `options.resource_arn` has open drift and build the plan.

    The key is the resource's ARN, or the Terraform address for a resource that has left the inventory
    (its finding is keyed by address and its page no longer exists). Without a `record`, the operation's
    target, resource and title subject come from the finding.

    Raises:
        PlanPreparationError: The options name a different resource than `record`, or the resource has no
            open drift.
    """
    assert isinstance(options, DriftResolveOptions)
    if record is not None and options.resource_arn != record.arn:
        raise PlanPreparationError(
            f"the options name {options.resource_arn} but the selected resource is {record.arn}",
            [f"options -> resource_arn: must be {record.arn}"],
        )
    findings = list(
        session.execute(
            select(DriftFinding)
            .where(DriftFinding.resource_arn == options.resource_arn, DriftFinding.resolved_at_utc.is_(None))
            .order_by(DriftFinding.finding_kind)
        ).scalars()
    )
    if not findings:
        raise PlanPreparationError(
            f"{options.resource_arn} has no open drift",
            ["options -> resource_arn: no open drift finding with this key; run Detect drift first"],
        )
    plan = options.model_dump(mode="json")
    addresses = {
        attribute["address"]
        for finding in findings
        for attribute in finding.attributes
        if attribute.get("address")
    }
    if len(addresses) == 1:
        plan["address"] = next(iter(addresses))
    if record is None:
        plan[OPERATION_FIELDS_PLAN_KEY] = {
            "target_key": findings[0].target_key,
            "resource_arn": options.resource_arn,
            "subject": options.resource_arn.rsplit(":", 1)[-1],
        }
    return plan


class RequestMetadata(NamedTuple):
    """What a request can be checked against without a call to AWS.

    Attributes:
        metadata: The family's metadata, or None when it cannot be had offline.
        unknown_names_are_errors: Whether a name the metadata lacks is surely wrong. It is for the simulated
            account (the bundle is its authority) and for AWS data; it is not for the bundle standing in
            for a real account, which lists only a subset of AWS's parameters.
    """

    metadata: FamilyMetadata | None
    unknown_names_are_errors: bool


def _request_time_metadata(
    session: Session, console_config: ConsoleConfig, target_key: str, family: str, scope: ParameterScope
) -> RequestMetadata:
    """The metadata of the family's `scope` for checking a request, without a call to AWS.

    The scope is the scope of the group the request is about: an Aurora family lists other parameters for
    instance groups than for cluster groups.

    A real account answers from fresh cached AWS data or else from the bundle; the executor reads AWS and
    validates again, so a name only the bundle lacks is not a problem yet. A family the bundle does not know
    cannot be checked offline for a real account (the executor can), so its request is let through with no
    check; for the simulated account the bundle is the authority, so an unknown family is refused.

    Raises:
        PlanPreparationError: The simulated account's bundle does not know the family.
    """
    target = console_config.target(target_key)
    try:
        metadata = load_family_metadata(session, family, None, scope=scope, simulated=target.simulated)
    except UnknownParameterFamilyError as error:
        if target.simulated:
            raise PlanPreparationError(str(error), [f"options -> family: {error}"]) from error
        logger.info(
            "request_validation_skipped",
            target_key=target_key,
            family=family,
            scope=scope,
            reason="the family is neither cached from AWS nor bundled; the executor validates it",
        )
        return RequestMetadata(None, False)
    return RequestMetadata(metadata, target.simulated or metadata.source == "aws")


def prepare_parameter_edit_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check the values of a parameter group edit against the family before anything is created.

    Raises:
        PlanPreparationError: Nothing is set or reset, or a value or name cannot be set; `problems` has one
            message per parameter.
    """
    assert isinstance(options, ParameterGroupEditOptions)
    if record is None:
        raise PlanPreparationError("an edit needs a parameter group", ["resource_id: required"])
    if not options.parameters and not options.reset:
        raise PlanPreparationError("nothing to change", ["options: set or reset at least one parameter"])
    known = _request_time_metadata(
        session,
        console_config,
        record.target_key,
        record.facts["parameter_group_family"],
        record.facts["group_scope"],
    )
    if known.metadata is not None:
        problems = parameter_problems(
            known.metadata,
            options.parameters,
            options.reset,
            check_unknown_names=known.unknown_names_are_errors,
        )
        if problems:
            raise PlanPreparationError(
                "some parameter values cannot be set", [problem.message for problem in problems]
            )
    return options.model_dump(mode="json")


def prepare_parameter_create_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check a new parameter group before anything is created, and name the group in the title.

    The name must be free in the target's inventory. A clone is resolved here the way the executor will
    resolve it (across families the parameters the new family lacks are dropped), and the merged overrides,
    the clone's carried values and the operator's own, are validated against the family.

    Raises:
        PlanPreparationError: The name is taken, a clone names no usable source group, or a value or name
            cannot be set; `problems` has one message per parameter.
    """
    assert isinstance(options, ParameterGroupCreateOptions)
    if options.source == "clone" and not options.clone_from_arn:
        raise PlanPreparationError(
            "cloning needs the group to clone",
            ["options -> clone_from_arn: required when source is clone"],
        )
    if group_name_is_taken(session, console_config, options.target_key, options.scope, options.name):
        raise PlanPreparationError(
            f"{options.name} exists already",
            [
                f"options -> name: the {options.scope} parameter group {options.name} already exists in "
                f"{options.target_key}"
            ],
        )
    known = _request_time_metadata(session, console_config, options.target_key, options.family, options.scope)
    cloned = None
    if options.source == "clone" and options.clone_from_arn:
        try:
            cloned = clone_parameters(
                session,
                console_config,
                options.clone_from_arn,
                options.scope,
                options.family,
                known.metadata if known.unknown_names_are_errors else None,
            )
        except (KeyError, ValueError) as error:
            message = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
            raise PlanPreparationError(
                "the group to clone cannot be used", [f"options -> clone_from_arn: {message}"]
            ) from error
    if known.metadata is not None:
        problems = create_problems(
            known.metadata,
            options.parameters,
            cloned,
            check_unknown_names=known.unknown_names_are_errors,
        )
        if problems:
            raise PlanPreparationError(
                "some parameter values cannot be set", [problem.message for problem in problems]
            )
    return {
        **options.model_dump(mode="json"),
        OPERATION_FIELDS_PLAN_KEY: {"target_key": options.target_key, "subject": options.name},
    }


def prepare_parameter_attach_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check that the group can be attached to the instance or cluster before anyone is asked to approve.

    Raises:
        PlanPreparationError: The group is not in the inventory, is of the other scope or another family;
            `problems` names each reason.
    """
    assert isinstance(options, ParameterGroupAttachOptions)
    if record is None:
        raise PlanPreparationError("an attachment needs an instance or cluster", ["resource_id: required"])
    problems = attach_problems(session, console_config, record, options.parameter_group_arn)
    if problems:
        raise PlanPreparationError("the parameter group cannot be attached", problems)
    return options.model_dump(mode="json")


def prepare_upgrade_group_build_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check an upgrade build before anything is created: the groups are known and their names are free.

    The names of the new groups follow from the current groups and the target version's major version, so
    no metadata (and no AWS call) is needed to refuse a name that is taken. A name that is taken is accepted
    when an earlier build created that group from the same source group for the same family (typically a
    build that failed part way): the new build then creates only the groups that are missing. The executor
    classifies the overrides and validates the values itself.

    Raises:
        PlanPreparationError: The resource has no group to carry, its members use different instance groups,
            the edits name a group the operation would not build, a new group's name is taken by another
            group, or every group exists already from an earlier build.
    """
    assert isinstance(options, UpgradeGroupBuildOptions)
    if record is None:
        raise PlanPreparationError("an upgrade build needs an instance or cluster", ["resource_id: required"])
    try:
        sources = upgrade_sources(session, console_config, record)
    except UpgradePlanError as error:
        raise PlanPreparationError(str(error), [f"resource_id: {error}"]) from error
    engine = str(record.facts.get("engine", "postgres"))
    target_family = family_for_version(engine, options.target_engine_version, [])
    stray = sorted(set(options.overrides) - {source.scope for source in sources})
    if stray:
        raise PlanPreparationError(
            "the edits name a parameter group this operation does not build",
            [f"options -> overrides: {record.identifier} has no {scope} group to build" for scope in stray],
        )
    taken: list[str] = []
    earlier_builds = 0
    for source in sources:
        name = new_group_name_for(
            source.group_name, source.is_default, record.identifier, target_family, options.name_suffix
        )
        if not group_name_is_taken(session, console_config, record.target_key, source.scope, name):
            continue
        if was_built_from_same_source(
            session, record.target_key, source.scope, name, target_family, source.group_name
        ):
            earlier_builds += 1  # an earlier build created it from this group; the build keeps it
        else:
            taken.append(
                f"options -> name_suffix: the {source.scope} parameter group {name} already exists in "
                f"{record.target_key}; choose another suffix"
            )
    if taken:
        raise PlanPreparationError("the new parameter groups exist already", taken)
    if earlier_builds == len(sources):
        raise PlanPreparationError(
            "every parameter group of this build exists already from an earlier build",
            ["options: nothing is left to create; choose another suffix to build a second set"],
        )
    return options.model_dump(mode="json")


def prepare_blue_green_cleanup_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Name the deployment to delete: the one the options name, or the resource's one left in place.

    Raises:
        PlanPreparationError: The resource has no deployment left in place, or not the one named.
    """
    assert isinstance(options, BlueGreenCleanupOptions)
    if record is None:
        raise PlanPreparationError("a cleanup needs an instance or cluster", ["resource_id: required"])
    left = [
        row for row in deployments_left_in_place(session, record.target_key) if row.resource_arn == record.arn
    ]
    if options.deployment_id:
        left = [row for row in left if row.deployment_id == options.deployment_id]
    if not left:
        named = f" named {options.deployment_id}" if options.deployment_id else ""
        raise PlanPreparationError(
            f"{record.identifier} has no blue/green deployment{named} left in place",
            [f"options -> deployment_id: no deployment{named} of {record.identifier} is left to clean up"],
        )
    return {"deployment_id": left[0].deployment_id}


def _refuse_a_taken_instance_identifier(
    session: Session,
    console_config: ConsoleConfig,
    record: ResourceRecord,
    identifier: str,
    field_name: str,
    kind: ResourceKind = ResourceKind.INSTANCE,
) -> None:
    """Refuse an identifier an instance (or cluster) of the record's target has in the latest inventory.

    The executor asks AWS again before approval; this answers the request at once for the common case.

    Raises:
        PlanPreparationError: The identifier is taken; the problem names the option.
    """
    _refuse_a_taken_identifier_in_target(
        session, console_config, record.target_key, record.environment_slug, identifier, field_name, kind
    )


def _refuse_a_taken_identifier_in_target(
    session: Session,
    console_config: ConsoleConfig,
    target_key: str,
    environment_slug: str,
    identifier: str,
    field_name: str,
    kind: ResourceKind = ResourceKind.INSTANCE,
) -> None:
    """Refuse an identifier an instance (or cluster) of `target_key` has in the latest inventory.

    Raises:
        PlanPreparationError: The identifier is taken; the problem names the option.
    """
    taken = any(
        other.target_key == target_key and other.identifier == identifier
        for other in list_resources(session, console_config, environment_slug, kind)
    )
    if taken:
        what = "a cluster" if kind == ResourceKind.CLUSTER else "an instance"
        raise PlanPreparationError(
            f"{identifier} exists already",
            [f"options -> {field_name}: {what} named {identifier} already exists in {target_key}"],
        )


def prepare_replica_create_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check the replica's name is free and title the operation with it; the operation stays on the source.

    Raises:
        PlanPreparationError: No source instance, or the name is taken in the source's target.
    """
    assert isinstance(options, ReadReplicaCreateOptions)
    if record is None:
        raise PlanPreparationError("a read replica needs a source instance", ["resource_id: required"])
    _refuse_a_taken_instance_identifier(
        session, console_config, record, options.replica_identifier, "replica_identifier"
    )
    return {
        **options.model_dump(mode="json"),
        OPERATION_FIELDS_PLAN_KEY: {"subject": options.replica_identifier},
    }


def prepare_cluster_reader_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check the reader's name is free and its parameter group fits the cluster; title it with the name.

    A chosen group must be an instance parameter group of the target, of the family the cluster's engine
    version takes (`aurora-postgresql16` for 16.x).

    Raises:
        PlanPreparationError: No cluster, the name is taken, or the group is missing or of another family.
    """
    assert isinstance(options, ClusterReaderCreateOptions)
    if record is None:
        raise PlanPreparationError("a reader needs a cluster", ["resource_id: required"])
    _refuse_a_taken_instance_identifier(
        session, console_config, record, options.reader_identifier, "reader_identifier"
    )
    group_name = options.parameter_group_name
    if group_name is not None:
        group = find_parameter_group(session, console_config, record.target_key, "instance", group_name)
        if group is None:
            raise PlanPreparationError(
                "the parameter group cannot be used",
                [
                    f"options -> parameter_group_name: no instance parameter group named {group_name} in "
                    f"{record.target_key}"
                ],
            )
        cluster_engine = str(record.facts.get("engine") or "aurora-postgresql")
        cluster_family = family_for_version(cluster_engine, str(record.facts.get("engine_version")), [])
        group_family = group.facts.get("parameter_group_family")
        if group_family != cluster_family:
            raise PlanPreparationError(
                "the parameter group cannot be used",
                [
                    f"options -> parameter_group_name: {group_name} is a {group_family} group; the "
                    f"members of {record.identifier} take {cluster_family}"
                ],
            )
    return {
        **options.model_dump(mode="json"),
        OPERATION_FIELDS_PLAN_KEY: {"subject": options.reader_identifier},
    }


def _refuse_restore_placement_problems(
    session: Session,
    console_config: ConsoleConfig,
    record: ResourceRecord,
    options: Any,
    scope: str,
    engine: str,
    engine_version: str,
) -> None:
    """Refuse a restore whose name is taken, or whose subnet or parameter group cannot be used.

    The new name must be free among the target's instances (instance scope) or clusters, and a cluster's
    writer `<name>-1` among its instances. The subnet group must be in the target's latest inventory; a chosen
    parameter group must be an instance group (instance scope) or cluster group of the restored engine
    version's family (`postgres16`, `aurora-postgresql16`).

    Raises:
        PlanPreparationError: One of the above; the problem names the option.
    """
    if scope == "cluster":
        _refuse_a_taken_instance_identifier(
            session, console_config, record, options.new_identifier, "new_identifier", ResourceKind.CLUSTER
        )
        writer_identifier = f"{options.new_identifier}-1"
        _refuse_a_taken_instance_identifier(
            session, console_config, record, writer_identifier, "new_identifier"
        )
    else:
        _refuse_a_taken_instance_identifier(
            session, console_config, record, options.new_identifier, "new_identifier"
        )
    subnet_group_known = session.execute(
        select(InventorySubnetGroup.id).where(
            InventorySubnetGroup.snapshot_id == record.snapshot_id,
            InventorySubnetGroup.db_subnet_group_name == options.subnet_group_name,
        )
    ).first()
    if subnet_group_known is None:
        raise PlanPreparationError(
            "the subnet group cannot be used",
            [
                f"options -> subnet_group_name: no DB subnet group named {options.subnet_group_name} in "
                f"{record.target_key}"
            ],
        )
    group_name = options.parameter_group_name
    if group_name is None:
        return
    group = find_parameter_group(session, console_config, record.target_key, scope, group_name)
    group_kind = f"{scope} parameter group"
    if group is None:
        raise PlanPreparationError(
            "the parameter group cannot be used",
            [f"options -> parameter_group_name: no {group_kind} named {group_name} in {record.target_key}"],
        )
    family = family_for_version(engine, engine_version, [])
    group_family = group.facts.get("parameter_group_family")
    if group_family != family:
        raise PlanPreparationError(
            "the parameter group cannot be used",
            [
                f"options -> parameter_group_name: {group_name} is a {group_family} group; the restored "
                f"{engine} {engine_version} takes a {group_kind} of family {family}"
            ],
        )


def prepare_snapshot_restore_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check the restore can be placed as asked and title the operation with the new name.

    The operation stays on the snapshot; an Aurora cluster is not Multi-AZ by a setting (it adds readers), so
    Multi-AZ is refused for a cluster snapshot.

    Raises:
        PlanPreparationError: No snapshot, Multi-AZ asked of a cluster, the name is taken, or the subnet or
            parameter group cannot be used.
    """
    assert isinstance(options, SnapshotRestoreOptions)
    if record is None:
        raise PlanPreparationError("a restore needs a snapshot", ["resource_id: required"])
    scope = "cluster" if record.facts.get("snapshot_scope") == "cluster" else "instance"
    if scope == "cluster" and options.multi_az:
        raise PlanPreparationError(
            "an Aurora cluster is not made Multi-AZ by a setting",
            ["options -> multi_az: leave it off for a cluster snapshot and add a reader after the restore"],
        )
    engine = str(record.raw.get("Engine") or ("aurora-postgresql" if scope == "cluster" else "postgres"))
    _refuse_restore_placement_problems(
        session, console_config, record, options, scope, engine, str(record.facts.get("engine_version") or "")
    )
    return {
        **options.model_dump(mode="json"),
        OPERATION_FIELDS_PLAN_KEY: {"subject": options.new_identifier},
    }


def prepare_pitr_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check the placement and the restore time against the source's window; title it with the new name.

    The window is read from the inventory (`restore_window`). A chosen time outside it is refused, naming the
    window; when the inventory cannot say what the window is, the time is not checked and the plan carries
    `restore_window_unknown` with the reason, which the executor records as a warning on the operation.

    Raises:
        PlanPreparationError: No source, the name is taken, the subnet or parameter group cannot be used, or
            the time is outside the window.
    """
    assert isinstance(options, PointInTimeRestoreOptions)
    if record is None:
        raise PlanPreparationError(
            "a point-in-time restore needs an instance or cluster", ["resource_id: required"]
        )
    scope = "cluster" if record.kind == ResourceKind.CLUSTER else "instance"
    engine = str(record.facts.get("engine") or ("aurora-postgresql" if scope == "cluster" else "postgres"))
    _refuse_restore_placement_problems(
        session, console_config, record, options, scope, engine, str(record.facts.get("engine_version") or "")
    )
    plan: dict[str, Any] = {
        **options.model_dump(mode="json"),
        OPERATION_FIELDS_PLAN_KEY: {"subject": options.new_identifier},
    }
    window = restore_window(record)
    if window is None:
        reason = restore_window_unknown_reason(record)
        logger.warning(
            "restore_window_unknown",
            resource_arn=record.arn,
            reason=reason,
            fix_suggestion="check the source's automated backups in AWS; the time is not checked here",
        )
        return {**plan, "restore_window_unknown": reason}
    plan["restore_window"] = {
        "earliest_utc": format_utc(window.earliest_utc),
        "latest_restorable_utc": format_utc(window.latest_restorable_utc),
    }
    restore_time = parse_utc(options.restore_time_utc) if options.restore_time_utc else None
    if restore_time is not None and not window.contains(restore_time):
        raise PlanPreparationError(
            f"the restore time is outside the restore window of {record.identifier}",
            [
                f"options -> restore_time_utc: {options.restore_time_utc} is outside the restore window, "
                f"{window.describe()}; choose a time inside it, or the latest restorable time"
            ],
        )
    return plan


def _refuse_created_resource_placement_problems(
    session: Session,
    console_config: ConsoleConfig,
    target_key: str,
    scope: str,
    engine: str,
    engine_version: str,
    subnet_group_name: str,
    group_name: str,
    group_field_name: str,
) -> None:
    """Refuse a create whose subnet group or parameter group cannot be used.

    The subnet group must be in the target's latest inventory. The parameter group must be one of the
    target's own groups of `scope` (an AWS `default.` group cannot be changed later, so it is refused) and of
    the family the engine version takes (`postgres16`, `aurora-postgresql16`).

    Raises:
        PlanPreparationError: One of the above, or the target was never inventoried; the problem names the
            option.
    """
    snapshot = latest_snapshot_for_target(session, target_key)
    if snapshot is None:
        raise PlanPreparationError(
            f"{target_key} has never been inventoried",
            [f"options -> target_key: refresh the inventory of {target_key} first"],
        )
    subnet_group_known = session.execute(
        select(InventorySubnetGroup.id).where(
            InventorySubnetGroup.snapshot_id == snapshot.id,
            InventorySubnetGroup.db_subnet_group_name == subnet_group_name,
        )
    ).first()
    if subnet_group_known is None:
        raise PlanPreparationError(
            "the subnet group cannot be used",
            [f"options -> subnet_group_name: no DB subnet group named {subnet_group_name} in {target_key}"],
        )
    family = family_for_version(engine, engine_version, [])
    group_kind = f"{scope} parameter group"
    if group_name.startswith("default."):
        raise PlanPreparationError(
            "the parameter group cannot be used",
            [
                f"options -> {group_field_name}: {group_name} is an AWS default parameter group, and default "
                f"groups cannot be changed later; choose a {group_kind} of family {family} of your own, or "
                "make one with Create parameter group"
            ],
        )
    group = find_parameter_group(session, console_config, target_key, scope, group_name)
    if group is None:
        raise PlanPreparationError(
            "the parameter group cannot be used",
            [f"options -> {group_field_name}: no {group_kind} named {group_name} in {target_key}"],
        )
    group_family = group.facts.get("parameter_group_family")
    if group_family != family:
        raise PlanPreparationError(
            "the parameter group cannot be used",
            [
                f"options -> {group_field_name}: {group_name} is a {group_family} group; {engine} "
                f"{engine_version} takes a {group_kind} of family {family}"
            ],
        )


def prepare_instance_create_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check the new instance's name and placement, and put the operation on its target, titled with the name.

    Raises:
        PlanPreparationError: The target is unknown, the name is taken, or the subnet or parameter group
            cannot be used.
    """
    assert isinstance(options, InstanceCreateOptions)
    target = _known_target(console_config, options.target_key)
    _refuse_a_taken_identifier_in_target(
        session, console_config, options.target_key, target.environment_slug, options.identifier, "identifier"
    )
    _refuse_created_resource_placement_problems(
        session,
        console_config,
        options.target_key,
        "instance",
        "postgres",
        options.engine_version,
        options.subnet_group_name,
        options.parameter_group_name,
        "parameter_group_name",
    )
    return {
        **options.model_dump(mode="json"),
        OPERATION_FIELDS_PLAN_KEY: {"target_key": options.target_key, "subject": options.identifier},
    }


def prepare_cluster_create_plan(
    session: Session, console_config: ConsoleConfig, options: BaseModel, record: ResourceRecord | None
) -> dict[str, Any]:
    """Check the new cluster's and its members' names and the placement; title the operation with the name.

    Raises:
        PlanPreparationError: The target is unknown, the cluster's or a member's name is taken, or the subnet
            or cluster parameter group cannot be used.
    """
    assert isinstance(options, ClusterCreateOptions)
    target = _known_target(console_config, options.target_key)
    _refuse_a_taken_identifier_in_target(
        session,
        console_config,
        options.target_key,
        target.environment_slug,
        options.identifier,
        "identifier",
        ResourceKind.CLUSTER,
    )
    for member_identifier in options.member_identifiers():
        _refuse_a_taken_identifier_in_target(
            session,
            console_config,
            options.target_key,
            target.environment_slug,
            member_identifier,
            "identifier",
        )
    _refuse_created_resource_placement_problems(
        session,
        console_config,
        options.target_key,
        "cluster",
        "aurora-postgresql",
        options.engine_version,
        options.subnet_group_name,
        options.cluster_parameter_group_name,
        "cluster_parameter_group_name",
    )
    return {
        **options.model_dump(mode="json"),
        OPERATION_FIELDS_PLAN_KEY: {"target_key": options.target_key, "subject": options.identifier},
    }


def _known_target(console_config: ConsoleConfig, target_key: str) -> ResolvedTarget:
    """The configured target named `target_key`.

    Raises:
        PlanPreparationError: No target has that key.
    """
    try:
        return console_config.target(target_key)
    except KeyError as error:
        raise PlanPreparationError(
            f"unknown target {target_key}", [f"options -> target_key: {error.args[0]}"]
        ) from error


OPERATION_DEFINITIONS: tuple[OperationDefinition, ...] = (
    OperationDefinition(
        kind="inventory_refresh",
        title="Refresh inventory",
        group=GROUP_DISCOVERY,
        summary="Read every instance, cluster, parameter group, subnet group and snapshot from AWS.",
        applies_to=frozenset({ResourceKind.ENVIRONMENT}),
        options_model=InventoryRefreshOptions,
        applicability=always,
        pauses_for_approval=False,
    ),
    OperationDefinition(
        kind="terraform_adoption",
        title="Adopt resources",
        group=GROUP_DISCOVERY,
        summary="Bring existing resources under Terraform with a reviewed import.",
        applies_to=frozenset(
            {
                ResourceKind.ENVIRONMENT,
                ResourceKind.INSTANCE,
                ResourceKind.CLUSTER,
                ResourceKind.PARAMETER_GROUP,
            }
        ),
        modes=frozenset({"terraform"}),
        options_model=TerraformAdoptionOptions,
        applicability=adoptable,
        prepare_plan=prepare_adoption_plan,
        needs_target=True,
    ),
    OperationDefinition(
        kind="drift_detect",
        title="Detect drift",
        group=GROUP_DISCOVERY,
        summary="Compare the recorded configuration with what is live, and with Terraform state.",
        applies_to=frozenset({ResourceKind.ENVIRONMENT}),
        options_model=NoOptions,
        applicability=always,
        pauses_for_approval=False,
    ),
    OperationDefinition(
        kind="drift_resolve",
        title="Resolve drift",
        group=GROUP_DISCOVERY,
        summary="Accept what AWS has into the record, or restore AWS to the record.",
        applies_to=frozenset(
            {
                ResourceKind.ENVIRONMENT,
                ResourceKind.INSTANCE,
                ResourceKind.CLUSTER,
                ResourceKind.PARAMETER_GROUP,
            }
        ),
        options_model=DriftResolveOptions,
        applicability=has_open_drift,
        prepare_plan=prepare_drift_resolve_plan,
        pauses_for_approval_when={"accept": False, "restore": True},
    ),
    OperationDefinition(
        kind="environment_import",
        title="Import environment",
        group=GROUP_ENVIRONMENTS,
        summary="Add an AWS account and its regions as an environment, then read its inventory.",
        applies_to=frozenset({ResourceKind.ENVIRONMENT}),
        options_model=EnvironmentImportOptions,
        applicability=always,
        needs_target=False,
        pauses_for_approval=False,
    ),
    OperationDefinition(
        kind="parameter_group_create",
        title="Create parameter group",
        group=GROUP_PARAMETER_GROUPS,
        summary="New group from family defaults or by cloning a group.",
        applies_to=frozenset({ResourceKind.ENVIRONMENT}),
        options_model=ParameterGroupCreateOptions,
        applicability=always,
        prepare_plan=prepare_parameter_create_plan,
        needs_target=True,
    ),
    OperationDefinition(
        kind="parameter_group_edit",
        title="Edit parameters",
        group=GROUP_PARAMETER_GROUPS,
        summary="Set or reset parameters with validation; reboot-needing parameters are flagged.",
        applies_to=frozenset({ResourceKind.PARAMETER_GROUP}),
        options_model=ParameterGroupEditOptions,
        applicability=parameter_group_editable,
        changes_the_resource=True,
        prepare_plan=prepare_parameter_edit_plan,
    ),
    OperationDefinition(
        kind="parameter_group_attach",
        title="Attach parameter group",
        group=GROUP_PARAMETER_GROUPS,
        summary="Point an instance or cluster at another group, optionally rebooting.",
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        disruptive=True,
        options_model=ParameterGroupAttachOptions,
        applicability=attachable,
        changes_the_resource=True,
        prepare_plan=prepare_parameter_attach_plan,
    ),
    OperationDefinition(
        kind="parameter_group_delete",
        title="Delete parameter group",
        group=GROUP_PARAMETER_GROUPS,
        summary="Remove a group nothing uses.",
        applies_to=frozenset({ResourceKind.PARAMETER_GROUP}),
        destructive=True,
        options_model=NoOptions,
        applicability=parameter_group_deletable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="parameter_group_build_for_upgrade",
        title="Build upgrade parameter groups",
        group=GROUP_PARAMETER_GROUPS,
        summary="New groups in the target family carrying the overrides from the current groups.",
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        options_model=UpgradeGroupBuildOptions,
        applicability=always,
        prepare_plan=prepare_upgrade_group_build_plan,
    ),
    OperationDefinition(
        kind="reboot_instance",
        title="Reboot",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary=(
            "Reboot an instance, or every instance using a parameter group that is pending a reboot, "
            "with or without failover."
        ),
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.PARAMETER_GROUP}),
        disruptive=True,
        options_model=RebootInstanceOptions,
        applicability=rebootable,
    ),
    OperationDefinition(
        kind="instance_class_change",
        title="Change instance class",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Change the instance class; the instance restarts (Multi-AZ fails over).",
        applies_to=frozenset({ResourceKind.INSTANCE}),
        disruptive=True,
        options_model=InstanceClassChangeOptions,
        applicability=instance_or_member_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="storage_change",
        title="Change storage",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Grow storage, change its type, IOPS or throughput, or set the autoscaling ceiling.",
        applies_to=frozenset({ResourceKind.INSTANCE}),
        options_model=StorageChangeOptions,
        applicability=storage_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="multi_az_change",
        title="Change Multi-AZ",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Add or remove the standby in another availability zone.",
        applies_to=frozenset({ResourceKind.INSTANCE}),
        options_model=MultiAzChangeOptions,
        applicability=standalone_instance_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="backup_settings_change",
        title="Change backup settings",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Change automated backup retention, the backup window or whether snapshots copy tags.",
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        options_model=BackupSettingsOptions,
        applicability=instance_or_cluster_but_not_member_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="maintenance_window_change",
        title="Change maintenance window",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Change the weekly window in which AWS applies maintenance.",
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        options_model=MaintenanceWindowOptions,
        applicability=instance_or_cluster_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="monitoring_change",
        title="Change monitoring",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Turn Performance Insights and enhanced monitoring on or off, or change their settings.",
        applies_to=frozenset({ResourceKind.INSTANCE}),
        options_model=MonitoringOptions,
        applicability=instance_or_member_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="deletion_protection_change",
        title="Change deletion protection",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Turn deletion protection on or off; while on, AWS refuses to delete the resource.",
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        options_model=DeletionProtectionOptions,
        applicability=instance_or_cluster_but_not_member_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="serverless_capacity_change",
        title="Change serverless capacity",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Change the minimum and maximum Aurora Serverless v2 capacity of a cluster.",
        applies_to=frozenset({ResourceKind.CLUSTER}),
        options_model=ServerlessCapacityOptions,
        applicability=serverless_cluster_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="tags_change",
        title="Change tags",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Set and remove tags; tags starting rdscommander: or aws: are never touched.",
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER, ResourceKind.PARAMETER_GROUP}),
        options_model=TagsOptions,
        applicability=taggable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="cluster_failover",
        title="Fail over",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Promote another member of an Aurora cluster to writer; connections drop briefly.",
        applies_to=frozenset({ResourceKind.CLUSTER}),
        disruptive=True,
        options_model=FailoverOptions,
        applicability=failover_possible,
    ),
    OperationDefinition(
        kind="apply_pending_maintenance",
        title="Apply pending maintenance",
        group=GROUP_INSTANCES_AND_CLUSTERS,
        summary="Apply a pending maintenance action now, in the next maintenance window, or undo the opt-in.",
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        options_model=PendingMaintenanceOptions,
        applicability=has_pending_maintenance,
    ),
    OperationDefinition(
        kind="replica_promote",
        title="Promote replica",
        group=GROUP_REPLICAS_AND_SCALING,
        summary=(
            "Promote a read replica to a standalone instance; replication stops for good and the replica "
            "restarts."
        ),
        applies_to=frozenset({ResourceKind.INSTANCE}),
        disruptive=True,
        options_model=PromoteReplicaOptions,
        applicability=promotable_replica,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="cluster_reader_remove",
        title="Remove reader",
        group=GROUP_REPLICAS_AND_SCALING,
        summary="Delete an Aurora reader instance; connections to it drop and the cluster keeps running.",
        applies_to=frozenset({ResourceKind.INSTANCE}),
        destructive=True,
        options_model=NoOptions,
        applicability=removable_reader,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="replica_create",
        title="Create read replica",
        group=GROUP_REPLICAS_AND_SCALING,
        summary="Create a read replica of this instance; the source keeps running and is not changed.",
        applies_to=frozenset({ResourceKind.INSTANCE}),
        options_model=ReadReplicaCreateOptions,
        applicability=replica_source,
        prepare_plan=prepare_replica_create_plan,
    ),
    OperationDefinition(
        kind="cluster_reader_add",
        title="Add reader",
        group=GROUP_REPLICAS_AND_SCALING,
        summary="Add a reader instance to an Aurora cluster; the cluster keeps running.",
        applies_to=frozenset({ResourceKind.CLUSTER}),
        options_model=ClusterReaderCreateOptions,
        applicability=cluster_changeable,
        prepare_plan=prepare_cluster_reader_plan,
    ),
    OperationDefinition(
        kind="snapshot_take",
        title="Take snapshot",
        group=GROUP_SNAPSHOTS,
        summary="Take a manual snapshot of an instance or cluster, with tags.",
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        options_model=TakeSnapshotOptions,
        applicability=snapshot_source,
    ),
    OperationDefinition(
        kind="snapshot_restore",
        title="Restore to new instance or cluster",
        group=GROUP_SNAPSHOTS,
        summary=(
            "Restore this snapshot to a new instance, or a cluster snapshot to a new Aurora cluster with one "
            "writer; the snapshot is not changed."
        ),
        applies_to=frozenset({ResourceKind.SNAPSHOT}),
        options_model=SnapshotRestoreOptions,
        applicability=restorable_snapshot,
        prepare_plan=prepare_snapshot_restore_plan,
    ),
    OperationDefinition(
        kind="pitr_restore",
        title="Point-in-time restore",
        group=GROUP_SNAPSHOTS,
        summary=(
            "Restore this instance or cluster, as it was at a time inside its backup window, to a new "
            "instance or cluster; the source keeps running and is not changed."
        ),
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        options_model=PointInTimeRestoreOptions,
        applicability=pitr_source,
        prepare_plan=prepare_pitr_plan,
    ),
    OperationDefinition(
        kind="instance_create",
        title="Create instance",
        group=GROUP_LIFECYCLE,
        summary=(
            "Create a PostgreSQL instance from the template: encrypted, backed up for 7 days, protected from "
            "deletion, with its master password kept in AWS Secrets Manager."
        ),
        applies_to=frozenset({ResourceKind.ENVIRONMENT}),
        options_model=InstanceCreateOptions,
        applicability=always,
        prepare_plan=prepare_instance_create_plan,
        needs_target=True,
    ),
    OperationDefinition(
        kind="cluster_create",
        title="Create cluster",
        group=GROUP_LIFECYCLE,
        summary=(
            "Create an Aurora PostgreSQL cluster with a writer and readers from the template: encrypted, "
            "backed up for 7 days, protected from deletion, with its master password kept in AWS Secrets "
            "Manager."
        ),
        applies_to=frozenset({ResourceKind.ENVIRONMENT}),
        options_model=ClusterCreateOptions,
        applicability=always,
        prepare_plan=prepare_cluster_create_plan,
        needs_target=True,
    ),
    OperationDefinition(
        kind="decommission",
        title="Decommission",
        group=GROUP_LIFECYCLE,
        summary=(
            "Delete an instance, or a cluster with its members, after AWS takes a final snapshot; deletion "
            "protection is turned off first. A read replica is deleted without a snapshot: its data lives on "
            "its source. This cannot be undone."
        ),
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        destructive=True,
        options_model=DecommissionOptions,
        applicability=decommissionable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="snapshot_delete",
        title="Delete snapshot",
        group=GROUP_SNAPSHOTS,
        summary="Delete a manual snapshot; this cannot be undone.",
        applies_to=frozenset({ResourceKind.SNAPSHOT}),
        destructive=True,
        options_model=NoOptions,
        applicability=manual_snapshot,
    ),
    OperationDefinition(
        kind="auto_minor_upgrade_change",
        title="Change auto minor upgrade",
        group=GROUP_UPGRADES,
        summary="Turn on or off whether AWS applies new minor versions on its own in the maintenance window.",
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        options_model=AutoMinorUpgradeOptions,
        applicability=instance_or_cluster_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="minor_upgrade",
        title="Minor version upgrade",
        group=GROUP_UPGRADES,
        summary=(
            "Upgrade to a newer minor version of the same major version; the instance restarts (Multi-AZ "
            "fails over). In Terraform mode auto minor upgrade is turned off so the version can be applied."
        ),
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        disruptive=True,
        options_model=MinorUpgradeOptions,
        applicability=version_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="major_upgrade",
        title="Major version upgrade",
        group=GROUP_UPGRADES,
        summary=(
            "Upgrade to a new major version through a blue/green deployment: a green copy runs the new "
            "version with parameter groups of its family, and production switches over only after a second "
            "approval. The old instances are kept."
        ),
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        disruptive=True,
        options_model=MajorUpgradeOptions,
        applicability=version_changeable,
        changes_the_resource=True,
    ),
    OperationDefinition(
        kind="blue_green_cleanup",
        title="Clean up blue/green deployment",
        group=GROUP_UPGRADES,
        summary=(
            "Delete the blue/green deployment a failed or rejected major upgrade left in place, with its "
            "green copy unless you keep it. Production is not touched."
        ),
        applies_to=frozenset({ResourceKind.INSTANCE, ResourceKind.CLUSTER}),
        destructive=True,
        options_model=BlueGreenCleanupOptions,
        applicability=blue_green_cleanable,
        prepare_plan=prepare_blue_green_cleanup_plan,
    ),
)


def definition_for(kind: str) -> OperationDefinition:
    """Lookup by kind; `KeyError` names the known kinds.

    Args:
        kind: Operation kind, e.g. `inventory_refresh`.

    Returns:
        The catalogue entry.

    Raises:
        KeyError: If no definition has that kind.

    Example:
        >>> definition_for("inventory_refresh").title
        'Refresh inventory'
    """
    for definition in OPERATION_DEFINITIONS:
        if definition.kind == kind:
            return definition
    raise KeyError(
        f"unknown operation kind {kind!r}; known: {', '.join(d.kind for d in OPERATION_DEFINITIONS)}"
    )


def _availability(
    definition: OperationDefinition, record: ResourceRecord | None, environment: EnvironmentConfig
) -> ActionAvailability:
    """Applicability of one definition, honouring the environment's change-management mode first."""
    if environment.change_management not in definition.modes:
        verdict = Applicability(ok=False, reason=f"not available in {environment.change_management} mode")
    else:
        verdict = definition.applicability(record, environment)
    return ActionAvailability(
        kind=definition.kind,
        title=definition.title,
        group=definition.group,
        summary=definition.summary,
        destructive=definition.destructive,
        disruptive=definition.disruptive,
        applicable=verdict.ok,
        reason=verdict.reason,
    )


def actions_for_resource(record: ResourceRecord, environment: EnvironmentConfig) -> list[ActionAvailability]:
    """Every definition that applies to the record's kind, with applicability.

    Args:
        record: The resource the Actions menu is for.
        environment: The environment owning the resource.

    Returns:
        One entry per matching definition, applicable or not; non-applicable entries carry a reason.

    Example:
        >>> [a.kind for a in actions_for_resource(orders_record, sandbox_environment)]
        ['terraform_adoption']
    """
    return [
        _availability(d, record, environment) for d in OPERATION_DEFINITIONS if record.kind in d.applies_to
    ]


def actions_for_environment(environment: EnvironmentConfig) -> list[ActionAvailability]:
    """Environment-level definitions.

    Args:
        environment: The environment the launcher is for.

    Returns:
        One entry per environment-level definition, applicable or not.

    Example:
        >>> [a.kind for a in actions_for_environment(sandbox_environment)]
        ['inventory_refresh', 'terraform_adoption']
    """
    return [
        _availability(d, None, environment)
        for d in OPERATION_DEFINITIONS
        if ResourceKind.ENVIRONMENT in d.applies_to
    ]


def validate_catalogue(executors: dict[str, StepExecutor]) -> None:
    """Every definition has an executor and every executor has a definition.

    Args:
        executors: Executors keyed by operation kind, as built for the runner.

    Raises:
        RuntimeError: If a definition has no executor or an executor has no definition.

    Example:
        >>> validate_catalogue(build_executors(services, config_store))
    """
    kinds = {d.kind for d in OPERATION_DEFINITIONS}
    missing_executors = sorted(kinds - set(executors))
    missing_definitions = sorted(set(executors) - kinds)
    if missing_executors or missing_definitions:
        logger.error(
            "catalogue_validation_failed",
            definitions_without_executors=missing_executors,
            executors_without_definitions=missing_definitions,
            fix_suggestion=(
                "add the missing definitions to OPERATION_DEFINITIONS and the missing executors to "
                "build_executors so both list the same kinds"
            ),
        )
        raise RuntimeError(
            f"catalogue mismatch: definitions without executors {missing_executors}; "
            f"executors without definitions {missing_definitions}"
        )
