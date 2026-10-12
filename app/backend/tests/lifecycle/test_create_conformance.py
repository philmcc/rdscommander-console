"""Both modes create the same resource: the direct create call carries what the rendered model carries.

Every field of the rendered model a create renders is classified here: sent under a request key with the same
value, sent as a reference resolved to an identifier, or listed with the reason it has no request key. A new
field, or a new create kind (restores, instance and cluster creates), extends these tables; the classification
test fails until it does.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from rdscommander.lifecycle import creates
from rdscommander.lifecycle.creates import (
    ClusterCreate,
    ClusterReaderCreate,
    InstanceCreate,
    ReadReplicaCreate,
)
from rdscommander.lifecycle.options import (
    ClusterCreateOptions,
    ClusterReaderCreateOptions,
    InstanceCreateOptions,
    PointInTimeRestoreOptions,
    ReadReplicaCreateOptions,
    SnapshotRestoreOptions,
)
from rdscommander.lifecycle.restores import PointInTimeRestoreCreate, SnapshotRestoreCreate
from rdscommander.parameters.direct_api import resource_modify_request
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import load_resource
from rdscommander.terraform.hcl import to_hcl
from rdscommander.terraform.names import terraform_resource_name
from rdscommander.terraform.render_input import RenderedCluster, RenderedClusterInstance, RenderedInstance
from tests.terraform.conftest import TARGET_KEY, arn_for

CONSOLE_TAG_LIST = [{"Key": "rdscommander:managed", "Value": "true"}]

REPLICA_FIELDS_SENT_AS = {
    "identifier": "DBInstanceIdentifier",
    "instance_class": "DBInstanceClass",
    "multi_az": "MultiAZ",
    "storage_type": "StorageType",
    "iops": "Iops",
    "storage_throughput": "StorageThroughput",
    "vpc_security_group_ids": "VpcSecurityGroupIds",
    "publicly_accessible": "PubliclyAccessible",
    "deletion_protection": "DeletionProtection",
    "performance_insights_enabled": "EnablePerformanceInsights",
    "performance_insights_retention_period": "PerformanceInsightsRetentionPeriod",
    "monitoring_interval": "MonitoringInterval",
    "monitoring_role_arn": "MonitoringRoleArn",
    "auto_minor_version_upgrade": "AutoMinorVersionUpgrade",
    "copy_tags_to_snapshot": "CopyTagsToSnapshot",
    "ca_cert_identifier": "CACertificateIdentifier",
}
"""Rendered replica field -> `CreateDBInstanceReadReplica` key, sent with the same value when set."""
REPLICA_REFERENCES_SENT_AS = {
    "replicate_source_reference": ("SourceDBInstanceIdentifier", "aws_db_instance", "identifier"),
    "parameter_group_reference": ("DBParameterGroupName", "aws_db_parameter_group", "name"),
}
"""Rendered reference -> (request key, resource type, attribute): the key holds the referenced identifier."""
REPLICA_FIELDS_WITHOUT_A_REQUEST_KEY = {
    "terraform_name": "Terraform's own name",
    "tags": "the console's tag only (provider default_tags in Terraform mode)",
    "preferred_maintenance_window": "CreateDBInstanceReadReplica takes no maintenance window",
    "apply_immediately": "Terraform-only behaviour",
    "final_snapshot_identifier": "Terraform-only behaviour",
    "snapshot_identifier": "not a restore",
    "restore_to_point_in_time": "not a restore",
    "manage_master_user_password": "a replica has its source's users",
    "engine": "not rendered for a replica: inherited from the source",
    "engine_version": "not rendered for a replica: inherited from the source",
    "allocated_storage": "not rendered for a replica: inherited from the source",
    "max_allocated_storage": "not rendered for a replica: inherited from the source",
    "master_username": "not rendered for a replica: inherited from the source",
    "db_name": "not rendered for a replica: inherited from the source",
    "port": "not rendered for a replica: inherited from the source",
    "db_subnet_group_reference": "not rendered for a replica: inherited from the source",
    "backup_retention_period": "rendered for a replica, but none for a new one: AWS sets a new replica's "
    "backups (off for PostgreSQL), as the create call takes none; Backup settings change them later",
    "preferred_backup_window": "rendered for a replica, but none for a new one: AWS chooses the window",
    "storage_encrypted": "not rendered for a replica: inherited from the source",
    "kms_key_id": "not rendered for a replica: a same-region replica uses the source's key",
}
READER_FIELDS_SENT_AS = {
    "identifier": "DBInstanceIdentifier",
    "instance_class": "DBInstanceClass",
    "engine": "Engine",
    "publicly_accessible": "PubliclyAccessible",
    "auto_minor_version_upgrade": "AutoMinorVersionUpgrade",
    "performance_insights_enabled": "EnablePerformanceInsights",
    "monitoring_interval": "MonitoringInterval",
    "promotion_tier": "PromotionTier",
    "preferred_maintenance_window": "PreferredMaintenanceWindow",
    "performance_insights_retention_period": "PerformanceInsightsRetentionPeriod",
    "monitoring_role_arn": "MonitoringRoleArn",
}
READER_REFERENCES_SENT_AS = {
    "cluster_reference": ("DBClusterIdentifier", "aws_rds_cluster", "id"),
    "parameter_group_reference": ("DBParameterGroupName", "aws_db_parameter_group", "name"),
}
READER_FIELDS_WITHOUT_A_REQUEST_KEY = {
    "terraform_name": "Terraform's own name",
    "tags": "the console's tag only (provider default_tags in Terraform mode)",
    "engine_version": "the cluster manages its members' version",
    "apply_immediately": "Terraform-only behaviour",
    "depends_on_member": "Terraform-only ordering: direct mode creates the writer first by calling in order",
}

RESTORE_COMMON_FIELDS_WITHOUT_A_REQUEST_KEY = {
    "terraform_name": "Terraform's own name",
    "tags": "the console's tag only (provider default_tags in Terraform mode)",
    "apply_immediately": "Terraform-only behaviour",
    "final_snapshot_identifier": "Terraform-only behaviour",
    "master_username": "the restored resource keeps the source's master user",
    "storage_encrypted": "encryption follows the snapshot or source restored",
}
"""Rendered fields no restore call takes, for instances and clusters alike."""
INSTANCE_RESTORE_FIELDS_SENT_BY_THE_FOLLOW_UP_MODIFY = {
    "performance_insights_enabled": "EnablePerformanceInsights",
    "performance_insights_retention_period": "PerformanceInsightsRetentionPeriod",
    "monitoring_interval": "MonitoringInterval",
    "monitoring_role_arn": "MonitoringRoleArn",
    "preferred_maintenance_window": "PreferredMaintenanceWindow",
}
"""Rendered field -> `ModifyDBInstance` key: the instance restore calls take no parameter for these, so direct
mode sends them once the restored instance is available (monitoring only when its interval is above 0)."""
CLUSTER_RESTORE_FIELDS_SENT_BY_THE_FOLLOW_UP_MODIFY = {
    "preferred_maintenance_window": "PreferredMaintenanceWindow",
    "manage_master_user_password": "ManageMasterUserPassword",
}
"""Rendered field -> `ModifyDBCluster` key, sent once the restored cluster is available: no cluster restore
call takes the maintenance window or `ManageMasterUserPassword` (the restored password is handed to Secrets
Manager by the modify; Terraform's provider makes the same modify after the restore)."""
SNAPSHOT_RESTORE_INSTANCE_FIELDS_SENT_AS = {
    "identifier": "DBInstanceIdentifier",
    "snapshot_identifier": "DBSnapshotIdentifier",
    "engine": "Engine",
    "instance_class": "DBInstanceClass",
    "allocated_storage": "AllocatedStorage",
    "storage_type": "StorageType",
    "iops": "Iops",
    "storage_throughput": "StorageThroughput",
    "port": "Port",
    "vpc_security_group_ids": "VpcSecurityGroupIds",
    "multi_az": "MultiAZ",
    "publicly_accessible": "PubliclyAccessible",
    "backup_retention_period": "BackupRetentionPeriod",
    "preferred_backup_window": "PreferredBackupWindow",
    "deletion_protection": "DeletionProtection",
    "auto_minor_version_upgrade": "AutoMinorVersionUpgrade",
    "copy_tags_to_snapshot": "CopyTagsToSnapshot",
    "ca_cert_identifier": "CACertificateIdentifier",
    "manage_master_user_password": "ManageMasterUserPassword",
}
"""Rendered restored-instance field -> `RestoreDBInstanceFromDBSnapshot` key."""
INSTANCE_RESTORE_REFERENCES_SENT_AS = {
    "db_subnet_group_reference": ("DBSubnetGroupName", "aws_db_subnet_group", "name"),
    "parameter_group_reference": ("DBParameterGroupName", "aws_db_parameter_group", "name"),
}
INSTANCE_RESTORE_FIELDS_WITHOUT_A_REQUEST_KEY = {
    **RESTORE_COMMON_FIELDS_WITHOUT_A_REQUEST_KEY,
    "replicate_source_reference": "not a replica",
    "kms_key_id": "the restore uses the key of the snapshot or source; the instance restore calls take none",
    "db_name": "the databases come with the restore; DBName applies to Oracle and SQL Server only",
}
SNAPSHOT_RESTORE_INSTANCE_FIELDS_WITHOUT_A_REQUEST_KEY = {
    **INSTANCE_RESTORE_FIELDS_WITHOUT_A_REQUEST_KEY,
    "engine_version": "RestoreDBInstanceFromDBSnapshot takes no version: the snapshot's",
    "max_allocated_storage": "RestoreDBInstanceFromDBSnapshot takes no storage ceiling (rendered none)",
    "restore_to_point_in_time": "not a point-in-time restore",
}
SNAPSHOT_RESTORE_CLUSTER_FIELDS_SENT_AS = {
    "identifier": "DBClusterIdentifier",
    "snapshot_identifier": "SnapshotIdentifier",
    "engine": "Engine",
    "engine_version": "EngineVersion",
    "engine_mode": "EngineMode",
    "port": "Port",
    "database_name": "DatabaseName",
    "vpc_security_group_ids": "VpcSecurityGroupIds",
    "backup_retention_period": "BackupRetentionPeriod",
    "preferred_backup_window": "PreferredBackupWindow",
    "kms_key_id": "KmsKeyId",
    "deletion_protection": "DeletionProtection",
    "copy_tags_to_snapshot": "CopyTagsToSnapshot",
}
"""Rendered restored-cluster field -> `RestoreDBClusterFromSnapshot` key."""
CLUSTER_RESTORE_REFERENCES_SENT_AS = {
    "db_subnet_group_reference": ("DBSubnetGroupName", "aws_db_subnet_group", "name"),
    "cluster_parameter_group_reference": (
        "DBClusterParameterGroupName",
        "aws_rds_cluster_parameter_group",
        "name",
    ),
}
CLUSTER_RESTORE_FIELDS_SENT_NESTED = {
    "serverless_v2_min_capacity": ("ServerlessV2ScalingConfiguration", "MinCapacity"),
    "serverless_v2_max_capacity": ("ServerlessV2ScalingConfiguration", "MaxCapacity"),
}
"""Rendered field -> (request key, nested key), sent when the capacity is set."""
CLUSTER_RESTORE_FIELDS_WITHOUT_A_REQUEST_KEY = {
    **RESTORE_COMMON_FIELDS_WITHOUT_A_REQUEST_KEY,
    "auto_minor_version_upgrade": "Aurora members carry it: the writer's CreateDBInstance sends it",
}
SNAPSHOT_RESTORE_CLUSTER_FIELDS_WITHOUT_A_REQUEST_KEY = {
    **CLUSTER_RESTORE_FIELDS_WITHOUT_A_REQUEST_KEY,
    "restore_to_point_in_time": "not a point-in-time restore",
}

PITR_INSTANCE_FIELDS_SENT_AS = {
    "identifier": "TargetDBInstanceIdentifier",
    "engine": "Engine",
    "instance_class": "DBInstanceClass",
    "allocated_storage": "AllocatedStorage",
    "max_allocated_storage": "MaxAllocatedStorage",
    "storage_type": "StorageType",
    "iops": "Iops",
    "storage_throughput": "StorageThroughput",
    "port": "Port",
    "vpc_security_group_ids": "VpcSecurityGroupIds",
    "multi_az": "MultiAZ",
    "publicly_accessible": "PubliclyAccessible",
    "backup_retention_period": "BackupRetentionPeriod",
    "preferred_backup_window": "PreferredBackupWindow",
    "deletion_protection": "DeletionProtection",
    "auto_minor_version_upgrade": "AutoMinorVersionUpgrade",
    "copy_tags_to_snapshot": "CopyTagsToSnapshot",
    "ca_cert_identifier": "CACertificateIdentifier",
    "manage_master_user_password": "ManageMasterUserPassword",
}
"""Rendered restored-instance field -> `RestoreDBInstanceToPointInTime` key."""
PITR_RESTORE_POINT_SENT_AS = {
    "instance": ("SourceDBInstanceIdentifier", "RestoreTime"),
    "cluster": ("SourceDBClusterIdentifier", "RestoreToTime"),
}
"""`restore_to_point_in_time` -> (source key, time key), or `UseLatestRestorableTime` for the latest."""
PITR_INSTANCE_FIELDS_WITHOUT_A_REQUEST_KEY = {
    **INSTANCE_RESTORE_FIELDS_WITHOUT_A_REQUEST_KEY,
    "engine_version": "RestoreDBInstanceToPointInTime takes no version: the source's",
    "snapshot_identifier": "not a snapshot restore",
}
PITR_CLUSTER_FIELDS_SENT_AS = {
    "identifier": "DBClusterIdentifier",
    "engine_mode": "EngineMode",
    "port": "Port",
    "vpc_security_group_ids": "VpcSecurityGroupIds",
    "backup_retention_period": "BackupRetentionPeriod",
    "preferred_backup_window": "PreferredBackupWindow",
    "kms_key_id": "KmsKeyId",
    "deletion_protection": "DeletionProtection",
    "copy_tags_to_snapshot": "CopyTagsToSnapshot",
}
"""Rendered restored-cluster field -> `RestoreDBClusterToPointInTime` key."""
PITR_CLUSTER_FIELDS_WITHOUT_A_REQUEST_KEY = {
    **CLUSTER_RESTORE_FIELDS_WITHOUT_A_REQUEST_KEY,
    "engine": "RestoreDBClusterToPointInTime takes no engine: the source's",
    "engine_version": "RestoreDBClusterToPointInTime takes no version: the source's",
    "database_name": "the databases come with the restore; the call takes no database name",
    "snapshot_identifier": "not a snapshot restore",
}


def _absent_value(value: Any) -> bool:
    """Values the request leaves out instead of sending: nothing set, no list, or monitoring off."""
    return value is None or value == [] or value == 0


def _assert_conforms(
    rendered: Any,
    request: dict[str, Any],
    sent_as: dict[str, str],
    references: dict[str, tuple[str, str, str]],
) -> None:
    for field_name, request_key in sent_as.items():
        value = getattr(rendered, field_name)
        if _absent_value(value) and not isinstance(value, bool):
            assert request.get(request_key) in (None, value), (field_name, request_key)
        else:
            assert request[request_key] == value, (field_name, request_key)
    for field_name, (request_key, resource_type, attribute) in references.items():
        reference = getattr(rendered, field_name)
        if reference is None:
            assert request_key not in request, field_name
            continue
        identifier = request[request_key]
        assert reference in (
            f"{resource_type}.{terraform_resource_name(identifier)}.{attribute}",
            to_hcl(identifier),
        ), (field_name, reference, identifier)
    assert request["Tags"] == CONSOLE_TAG_LIST


def test_every_rendered_replica_field_is_classified() -> None:
    classified = (
        set(REPLICA_FIELDS_SENT_AS)
        | set(REPLICA_REFERENCES_SENT_AS)
        | set(REPLICA_FIELDS_WITHOUT_A_REQUEST_KEY)
    )
    assert classified == set(RenderedInstance.model_fields)


def test_every_rendered_restored_instance_and_cluster_field_is_classified() -> None:
    modify = INSTANCE_RESTORE_FIELDS_SENT_BY_THE_FOLLOW_UP_MODIFY
    for sent_as, references, without in (
        (
            SNAPSHOT_RESTORE_INSTANCE_FIELDS_SENT_AS,
            INSTANCE_RESTORE_REFERENCES_SENT_AS,
            SNAPSHOT_RESTORE_INSTANCE_FIELDS_WITHOUT_A_REQUEST_KEY,
        ),
        (
            PITR_INSTANCE_FIELDS_SENT_AS | {"restore_to_point_in_time": "(PITR_RESTORE_POINT_SENT_AS)"},
            INSTANCE_RESTORE_REFERENCES_SENT_AS,
            PITR_INSTANCE_FIELDS_WITHOUT_A_REQUEST_KEY,
        ),
    ):
        assert set(sent_as) | set(references) | set(modify) | set(without) == set(
            RenderedInstance.model_fields
        )
        assert not set(sent_as) & set(without) and not set(references) & set(without)
        assert not set(modify) & (set(sent_as) | set(without))
    for sent_as, without in (
        (SNAPSHOT_RESTORE_CLUSTER_FIELDS_SENT_AS, SNAPSHOT_RESTORE_CLUSTER_FIELDS_WITHOUT_A_REQUEST_KEY),
        (
            PITR_CLUSTER_FIELDS_SENT_AS | {"restore_to_point_in_time": "(PITR_RESTORE_POINT_SENT_AS)"},
            PITR_CLUSTER_FIELDS_WITHOUT_A_REQUEST_KEY,
        ),
    ):
        classified = (
            set(sent_as)
            | set(CLUSTER_RESTORE_REFERENCES_SENT_AS)
            | set(CLUSTER_RESTORE_FIELDS_SENT_NESTED)
            | set(CLUSTER_RESTORE_FIELDS_SENT_BY_THE_FOLLOW_UP_MODIFY)
            | set(without)
        )
        assert classified == set(RenderedCluster.model_fields)
        assert not set(sent_as) & set(without)
        assert not set(CLUSTER_RESTORE_FIELDS_SENT_BY_THE_FOLLOW_UP_MODIFY) & (set(sent_as) | set(without))


def test_every_rendered_reader_field_is_classified() -> None:
    classified = (
        set(READER_FIELDS_SENT_AS) | set(READER_REFERENCES_SENT_AS) | set(READER_FIELDS_WITHOUT_A_REQUEST_KEY)
    )
    assert classified == set(RenderedClusterInstance.model_fields)


@pytest.fixture
def rendered_in_workspace(monkeypatch):  # noqa: ANN201
    """Treat every referenced resource as rendered in the workspace, as an adopted estate has it."""
    monkeypatch.setattr(creates, "_rendered_in_workspace", lambda context, resource_type, identifier: True)


def _source_with_every_setting(sandbox_render_input) -> RenderedInstance:
    orders = next(i for i in sandbox_render_input.instances if i.identifier == "demo-orders")
    return orders.model_copy(
        update={
            "storage_type": "gp3",
            "iops": 3000,
            "storage_throughput": 125,
            "vpc_security_group_ids": ["sg-0aaa", "sg-0bbb"],
            "deletion_protection": True,
            "performance_insights_enabled": True,
            "performance_insights_retention_period": 7,
            "monitoring_interval": 60,
            "monitoring_role_arn": "arn:aws:iam::000000000000:role/rds-monitoring",
            "auto_minor_version_upgrade": False,
            "copy_tags_to_snapshot": True,
            "ca_cert_identifier": "rds-ca-rsa2048-g1",
            "preferred_maintenance_window": "sun:03:00-sun:03:30",
        }
    )


def _orders_record(session_factory, console_config, sandbox_snapshot):  # noqa: ANN202
    with session_factory() as session:
        return load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-orders")
        )


@pytest.mark.parametrize(
    "options",
    [
        {"replica_identifier": "demo-orders-replica-2", "instance_class": "db.m6g.large"},
        {
            "replica_identifier": "demo-orders-replica-2",
            "instance_class": "db.m6g.xlarge",
            "multi_az": True,
            "storage_type": "io1",
            "iops": 5000,
        },
    ],
)
def test_the_direct_replica_create_sends_what_the_rendered_replica_carries(
    session_factory,
    console_config,
    sandbox_snapshot,
    sandbox_render_input,
    rendered_in_workspace,
    monkeypatch,
    options,
) -> None:
    source_model = _source_with_every_setting(sandbox_render_input)
    monkeypatch.setattr(creates, "rendered_instance_of", lambda context, identifier: source_model)
    change = ReadReplicaCreate(
        TARGET_KEY,
        _orders_record(session_factory, console_config, sandbox_snapshot),
        ReadReplicaCreateOptions.model_validate(options),
    )
    context = SimpleNamespace()
    change.prepare(context)
    request = change.create_arguments(context)
    _assert_conforms(change.rendered_cached, request, REPLICA_FIELDS_SENT_AS, REPLICA_REFERENCES_SENT_AS)
    assert request["SourceDBInstanceIdentifier"] == "demo-orders"
    assert request["VpcSecurityGroupIds"] == ["sg-0aaa", "sg-0bbb"] and request["DeletionProtection"] is True
    assert request["MonitoringRoleArn"].endswith("rds-monitoring")


def test_a_new_replica_renders_no_backup_settings_because_its_create_call_takes_none(
    session_factory,
    console_config,
    sandbox_snapshot,
    sandbox_render_input,
    rendered_in_workspace,
    monkeypatch,
) -> None:
    """Terraform's provider would modify a new replica to the source's backups; direct mode sends none."""
    source_model = _source_with_every_setting(sandbox_render_input).model_copy(
        update={"backup_retention_period": 7, "preferred_backup_window": "01:00-02:00"}
    )
    monkeypatch.setattr(creates, "rendered_instance_of", lambda context, identifier: source_model)
    change = ReadReplicaCreate(
        TARGET_KEY,
        _orders_record(session_factory, console_config, sandbox_snapshot),
        ReadReplicaCreateOptions(replica_identifier="demo-orders-replica-2", instance_class="db.m6g.large"),
    )
    change.prepare(SimpleNamespace())
    rendered = change.rendered_cached
    assert (rendered.backup_retention_period, rendered.preferred_backup_window) == (None, None)
    request = change.create_arguments(SimpleNamespace())
    assert not {"BackupRetentionPeriod", "PreferredBackupWindow"} & set(request)


def test_a_replica_takes_iops_and_throughput_only_from_its_options(
    session_factory,
    console_config,
    sandbox_snapshot,
    sandbox_render_input,
    rendered_in_workspace,
    monkeypatch,
) -> None:
    """A gp3 source under 400 GiB reports its baseline IOPS; sending them on a create is refused by AWS."""
    source_model = _source_with_every_setting(sandbox_render_input)
    monkeypatch.setattr(creates, "rendered_instance_of", lambda context, identifier: source_model)
    change = ReadReplicaCreate(
        TARGET_KEY,
        _orders_record(session_factory, console_config, sandbox_snapshot),
        ReadReplicaCreateOptions(replica_identifier="demo-orders-replica-2", instance_class="db.m6g.large"),
    )
    change.prepare(SimpleNamespace())
    rendered = change.rendered_cached
    assert (rendered.storage_type, rendered.iops, rendered.storage_throughput) == (None, None, None)
    request = change.create_arguments(SimpleNamespace())
    assert not {"StorageType", "Iops", "StorageThroughput"} & set(request)


def test_the_direct_reader_create_sends_what_the_rendered_reader_carries(
    session_factory, console_config, sandbox_snapshot, rendered_in_workspace
) -> None:
    with session_factory() as session:
        cluster = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-aurora")
        )
    for options in (
        {"reader_identifier": "demo-aurora-reader-2", "instance_class": "db.r6g.large"},
        {
            "reader_identifier": "demo-aurora-reader-2",
            "instance_class": "db.r6g.xlarge",
            "promotion_tier": 4,
            "parameter_group_name": "demo-aurora-members",
            "performance_insights_enabled": False,
        },
    ):
        change = ClusterReaderCreate(TARGET_KEY, cluster, ClusterReaderCreateOptions.model_validate(options))
        context = SimpleNamespace()
        change.prepare(context)
        request = change.create_arguments(context)
        _assert_conforms(change.rendered_cached, request, READER_FIELDS_SENT_AS, READER_REFERENCES_SENT_AS)
        assert request["DBClusterIdentifier"] == "demo-aurora"


KMS_KEY_ARN = "arn:aws:kms:eu-west-2:000000000000:key/0f1e2d3c-aaaa-bbbb-cccc-000000000001"


def _snapshot_record(scope: str, raw: dict[str, Any]) -> ResourceRecord:
    """A snapshot record as the inventory makes it, with a describe entry that sets everything it can."""
    arn_kind = "cluster-snapshot" if scope == "cluster" else "snapshot"
    return ResourceRecord(
        kind=ResourceKind.SNAPSHOT,
        arn=f"arn:aws:rds:eu-west-2:000000000000:{arn_kind}:before-change",
        resource_id="x",
        identifier="before-change",
        environment_slug="sandbox",
        target_key=TARGET_KEY,
        ownership="unmanaged",
        status="available",
        facts={
            "snapshot_scope": scope,
            "snapshot_type": "manual",
            "engine_version": "16.4",
            "allocated_storage_gb": 100,
        },
        relationships={"source_identifier": "demo-orders" if scope == "instance" else "demo-aurora"},
        raw=raw,
        snapshot_id=1,
    )


def _restore_options(**overrides: Any) -> dict[str, Any]:
    return {
        "new_identifier": "demo-restored",
        "instance_class": "db.m6g.large",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": ["sg-0123456789abcdef0", "sg-0fedcba9876543210"],
        **overrides,
    }


def _assert_nested(rendered: Any, request: dict[str, Any]) -> None:
    for field_name, (request_key, nested_key) in CLUSTER_RESTORE_FIELDS_SENT_NESTED.items():
        value = getattr(rendered, field_name)
        if value is None:
            assert request_key not in request, field_name
        else:
            assert request[request_key][nested_key] == value, field_name


@pytest.mark.parametrize(
    "options",
    [
        _restore_options(),
        _restore_options(
            parameter_group_name="demo-postgres16-tuned",
            multi_az=True,
            publicly_accessible=True,
            deletion_protection=False,
        ),
        _restore_options(parameter_group_name="default.postgres16"),
    ],
)
def test_the_direct_snapshot_restore_of_an_instance_sends_what_the_rendered_instance_carries(
    rendered_in_workspace, options
) -> None:
    snapshot = _snapshot_record(
        "instance",
        {
            "Engine": "postgres",
            "MasterUsername": "demo_admin",
            "Port": 5432,
            "Encrypted": True,
            "KmsKeyId": KMS_KEY_ARN,
        },
    )
    change = SnapshotRestoreCreate(TARGET_KEY, snapshot, SnapshotRestoreOptions.model_validate(options))
    context = SimpleNamespace()
    change.prepare(context)
    request = change.create_arguments(context)
    rendered = change.rendered_cached
    assert isinstance(rendered, RenderedInstance)
    _assert_conforms(
        rendered, request, SNAPSHOT_RESTORE_INSTANCE_FIELDS_SENT_AS, INSTANCE_RESTORE_REFERENCES_SENT_AS
    )
    assert (rendered.storage_encrypted, rendered.kms_key_id, rendered.master_username) == (
        True,
        KMS_KEY_ARN,
        "demo_admin",
    )
    assert request["DBSnapshotIdentifier"] == "before-change"
    assert request["DBSubnetGroupName"] == "demo-private-subnets"
    _assert_no_password_key(request)
    assert rendered.manage_master_user_password is True
    modify = _assert_follow_up_modify(change, context, "aws_db_instance")
    assert modify == {"EnablePerformanceInsights": True}


@pytest.mark.parametrize(
    "options",
    [
        _restore_options(instance_class="db.r6g.large"),
        _restore_options(
            instance_class="db.r6g.xlarge",
            parameter_group_name="demo-aurora-pg16-tuned",
            publicly_accessible=True,
            deletion_protection=False,
        ),
    ],
)
def test_the_direct_snapshot_restore_of_a_cluster_sends_what_the_rendered_cluster_and_writer_carry(
    rendered_in_workspace, options
) -> None:
    snapshot = _snapshot_record(
        "cluster",
        {
            "Engine": "aurora-postgresql",
            "EngineMode": "provisioned",
            "MasterUsername": "demo_admin",
            "Port": 5432,
            "StorageEncrypted": True,
            "KmsKeyId": KMS_KEY_ARN,
        },
    )
    change = SnapshotRestoreCreate(TARGET_KEY, snapshot, SnapshotRestoreOptions.model_validate(options))
    context = SimpleNamespace()
    change.prepare(context)
    request = change.create_arguments(context)
    rendered = change.rendered_cached
    assert isinstance(rendered, RenderedCluster)
    _assert_conforms(
        rendered, request, SNAPSHOT_RESTORE_CLUSTER_FIELDS_SENT_AS, CLUSTER_RESTORE_REFERENCES_SENT_AS
    )
    _assert_nested(rendered, request)
    _assert_managed_password_by_the_follow_up_modify(change, context, request)
    assert request["KmsKeyId"] == KMS_KEY_ARN and request["SnapshotIdentifier"] == "before-change"
    writer_request = change.writer_arguments(context)
    _assert_conforms(
        change.writer_rendered_cached, writer_request, READER_FIELDS_SENT_AS, READER_REFERENCES_SENT_AS
    )
    assert writer_request["DBClusterIdentifier"] == "demo-restored"
    assert writer_request["DBInstanceIdentifier"] == "demo-restored-1"


def _assert_follow_up_modify(change: Any, context: Any, resource_type: str) -> dict[str, Any]:
    """The modify after the restore carries every follow-up field the rendered model sets; returns it."""
    rendered = change.rendered_cached
    sent_by_modify = (
        INSTANCE_RESTORE_FIELDS_SENT_BY_THE_FOLLOW_UP_MODIFY
        if resource_type == "aws_db_instance"
        else CLUSTER_RESTORE_FIELDS_SENT_BY_THE_FOLLOW_UP_MODIFY
    )
    modify = change.post_create_modify_request(context)
    attribute_values = change.post_create_attribute_values(context)
    assert resource_modify_request(resource_type, attribute_values).items() <= modify.items()
    monitoring_on = bool(getattr(rendered, "monitoring_interval", 0))
    for field_name, request_key in sent_by_modify.items():
        value = getattr(rendered, field_name)
        if value is None or (field_name.startswith("monitoring_") and not monitoring_on):
            assert request_key not in modify, field_name
        else:
            assert modify[request_key] == value, field_name
    return modify


def _assert_managed_password_by_the_follow_up_modify(
    change: Any, context: Any, request: dict[str, Any]
) -> None:
    """A cluster restore asks AWS to manage the password in the follow-up modify; no request carries one."""
    assert change.rendered_cached.manage_master_user_password is True
    modify = _assert_follow_up_modify(change, context, "aws_rds_cluster")
    assert modify["ManageMasterUserPassword"] is True
    assert "ManageMasterUserPassword" not in request, "no cluster restore call takes it"
    assert "MasterUserPassword" not in request and "MasterUserPassword" not in modify


def _assert_restore_point(rendered: Any, request: dict[str, Any], scope: str) -> None:
    """The request restores from the rendered source, at its time or the latest restorable time."""
    source_key, time_key = PITR_RESTORE_POINT_SENT_AS[scope]
    point = rendered.restore_to_point_in_time
    assert request[source_key] == point.source_identifier
    if point.use_latest_restorable_time:
        assert request["UseLatestRestorableTime"] is True and time_key not in request
    else:
        assert request[time_key] == point.restore_time_utc and "UseLatestRestorableTime" not in request


def _pitr_options(**overrides: Any) -> dict[str, Any]:
    return {
        "new_identifier": "demo-restored",
        "instance_class": "db.m6g.large",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": ["sg-0123456789abcdef0"],
        **overrides,
    }


@pytest.mark.parametrize(
    "options",
    [
        _pitr_options(),
        _pitr_options(
            restore_time_utc="2026-10-11T10:00:00Z",
            parameter_group_name="demo-postgres16-tuned",
            deletion_protection=False,
        ),
    ],
)
def test_the_direct_point_in_time_restore_of_an_instance_sends_what_the_rendered_instance_carries(
    session_factory,
    console_config,
    sandbox_snapshot,
    sandbox_render_input,
    rendered_in_workspace,
    monkeypatch,
    options,
) -> None:
    source_model = _source_with_every_setting(sandbox_render_input)
    monkeypatch.setattr(creates, "rendered_instance_of", lambda context, identifier: source_model)
    change = PointInTimeRestoreCreate(
        TARGET_KEY,
        _orders_record(session_factory, console_config, sandbox_snapshot),
        PointInTimeRestoreOptions.model_validate(options),
    )
    context = SimpleNamespace()
    change.prepare(context)
    request = change.create_arguments(context)
    rendered = change.rendered_cached
    assert isinstance(rendered, RenderedInstance)
    _assert_conforms(rendered, request, PITR_INSTANCE_FIELDS_SENT_AS, INSTANCE_RESTORE_REFERENCES_SENT_AS)
    _assert_restore_point(rendered, request, "instance")
    assert request["SourceDBInstanceIdentifier"] == "demo-orders"
    assert (rendered.storage_type, rendered.iops) == (None, None)
    assert request["CACertificateIdentifier"] == "rds-ca-rsa2048-g1"
    _assert_no_password_key(request)
    modify = _assert_follow_up_modify(change, context, "aws_db_instance")
    assert modify == {
        "EnablePerformanceInsights": True,
        "PerformanceInsightsRetentionPeriod": 7,
        "MonitoringInterval": 60,
        "MonitoringRoleArn": "arn:aws:iam::000000000000:role/rds-monitoring",
        "PreferredMaintenanceWindow": "sun:03:00-sun:03:30",
    }


@pytest.mark.parametrize(
    "options",
    [
        _pitr_options(instance_class="db.r6g.large"),
        _pitr_options(
            instance_class="db.r6g.xlarge",
            restore_time_utc="2026-10-11T10:00:00Z",
            parameter_group_name="demo-aurora-pg16-tuned",
        ),
    ],
)
def test_the_direct_point_in_time_restore_of_a_cluster_sends_what_the_rendered_cluster_and_writer_carry(
    session_factory, console_config, sandbox_snapshot, rendered_in_workspace, options
) -> None:
    with session_factory() as session:
        aurora = load_resource(
            session, console_config, arn_for(session_factory, sandbox_snapshot, "demo-aurora")
        )
    source = aurora.model_copy(
        update={
            "facts": {
                **aurora.facts,
                "storage_encrypted": True,
                "serverless_v2_min_capacity": 0.5,
                "serverless_v2_max_capacity": 4.0,
            },
            "raw": {**aurora.raw, "StorageEncrypted": True, "KmsKeyId": KMS_KEY_ARN},
        }
    )
    change = PointInTimeRestoreCreate(TARGET_KEY, source, PointInTimeRestoreOptions.model_validate(options))
    context = SimpleNamespace()
    change.prepare(context)
    request = change.create_arguments(context)
    rendered = change.rendered_cached
    assert isinstance(rendered, RenderedCluster)
    _assert_conforms(rendered, request, PITR_CLUSTER_FIELDS_SENT_AS, CLUSTER_RESTORE_REFERENCES_SENT_AS)
    _assert_nested(rendered, request)
    _assert_managed_password_by_the_follow_up_modify(change, context, request)
    _assert_restore_point(rendered, request, "cluster")
    assert request["ServerlessV2ScalingConfiguration"] == {"MinCapacity": 0.5, "MaxCapacity": 4.0}
    writer_request = change.writer_arguments(context)
    _assert_conforms(
        change.writer_rendered_cached, writer_request, READER_FIELDS_SENT_AS, READER_REFERENCES_SENT_AS
    )
    assert writer_request["DBClusterIdentifier"] == "demo-restored"


INSTANCE_CREATE_FIELDS_SENT_AS = {
    "identifier": "DBInstanceIdentifier",
    "engine": "Engine",
    "engine_version": "EngineVersion",
    "instance_class": "DBInstanceClass",
    "allocated_storage": "AllocatedStorage",
    "max_allocated_storage": "MaxAllocatedStorage",
    "storage_type": "StorageType",
    "iops": "Iops",
    "storage_throughput": "StorageThroughput",
    "master_username": "MasterUsername",
    "manage_master_user_password": "ManageMasterUserPassword",
    "db_name": "DBName",
    "port": "Port",
    "vpc_security_group_ids": "VpcSecurityGroupIds",
    "multi_az": "MultiAZ",
    "publicly_accessible": "PubliclyAccessible",
    "backup_retention_period": "BackupRetentionPeriod",
    "preferred_backup_window": "PreferredBackupWindow",
    "preferred_maintenance_window": "PreferredMaintenanceWindow",
    "deletion_protection": "DeletionProtection",
    "storage_encrypted": "StorageEncrypted",
    "kms_key_id": "KmsKeyId",
    "performance_insights_enabled": "EnablePerformanceInsights",
    "performance_insights_retention_period": "PerformanceInsightsRetentionPeriod",
    "monitoring_interval": "MonitoringInterval",
    "monitoring_role_arn": "MonitoringRoleArn",
    "auto_minor_version_upgrade": "AutoMinorVersionUpgrade",
    "copy_tags_to_snapshot": "CopyTagsToSnapshot",
    "ca_cert_identifier": "CACertificateIdentifier",
}
"""Rendered created-instance field -> `CreateDBInstance` key: the call takes every setting, so nothing waits
for a follow-up modify."""
INSTANCE_CREATE_REFERENCES_SENT_AS = {
    "db_subnet_group_reference": ("DBSubnetGroupName", "aws_db_subnet_group", "name"),
    "parameter_group_reference": ("DBParameterGroupName", "aws_db_parameter_group", "name"),
}
INSTANCE_CREATE_FIELDS_WITHOUT_A_REQUEST_KEY = {
    "terraform_name": "Terraform's own name",
    "tags": "the console's tag only (provider default_tags in Terraform mode)",
    "apply_immediately": "Terraform-only behaviour",
    "final_snapshot_identifier": "Terraform-only behaviour",
    "replicate_source_reference": "not a replica",
    "snapshot_identifier": "not a restore",
    "restore_to_point_in_time": "not a restore",
}
CLUSTER_CREATE_FIELDS_SENT_AS = {
    "identifier": "DBClusterIdentifier",
    "engine": "Engine",
    "engine_version": "EngineVersion",
    "engine_mode": "EngineMode",
    "master_username": "MasterUsername",
    "manage_master_user_password": "ManageMasterUserPassword",
    "database_name": "DatabaseName",
    "port": "Port",
    "vpc_security_group_ids": "VpcSecurityGroupIds",
    "backup_retention_period": "BackupRetentionPeriod",
    "preferred_backup_window": "PreferredBackupWindow",
    "preferred_maintenance_window": "PreferredMaintenanceWindow",
    "storage_encrypted": "StorageEncrypted",
    "kms_key_id": "KmsKeyId",
    "deletion_protection": "DeletionProtection",
    "copy_tags_to_snapshot": "CopyTagsToSnapshot",
}
"""Rendered created-cluster field -> `CreateDBCluster` key."""
CLUSTER_CREATE_FIELDS_WITHOUT_A_REQUEST_KEY = {
    "terraform_name": "Terraform's own name",
    "tags": "the console's tag only (provider default_tags in Terraform mode)",
    "apply_immediately": "Terraform-only behaviour",
    "final_snapshot_identifier": "Terraform-only behaviour",
    "snapshot_identifier": "not a restore",
    "restore_to_point_in_time": "not a restore",
    "auto_minor_version_upgrade": "Aurora members carry it: each member's CreateDBInstance sends it",
}


def test_every_rendered_created_instance_and_cluster_field_is_classified() -> None:
    instance_classes = (
        set(INSTANCE_CREATE_FIELDS_SENT_AS),
        set(INSTANCE_CREATE_REFERENCES_SENT_AS),
        set(INSTANCE_CREATE_FIELDS_WITHOUT_A_REQUEST_KEY),
    )
    assert set().union(*instance_classes) == set(RenderedInstance.model_fields)
    assert sum(len(fields) for fields in instance_classes) == len(RenderedInstance.model_fields)
    cluster_classes = (
        set(CLUSTER_CREATE_FIELDS_SENT_AS),
        set(CLUSTER_RESTORE_REFERENCES_SENT_AS),
        set(CLUSTER_RESTORE_FIELDS_SENT_NESTED),
        set(CLUSTER_CREATE_FIELDS_WITHOUT_A_REQUEST_KEY),
    )
    assert set().union(*cluster_classes) == set(RenderedCluster.model_fields)
    assert sum(len(fields) for fields in cluster_classes) == len(RenderedCluster.model_fields)


def _assert_no_password_key(request: dict[str, Any]) -> None:
    """The request asks AWS to manage the password and never carries one."""
    assert request["ManageMasterUserPassword"] is True
    assert "MasterUserPassword" not in request


def _created_instance_options(**overrides: Any) -> dict[str, Any]:
    return {
        "target_key": TARGET_KEY,
        "identifier": "demo-reports",
        "engine_version": "16.4",
        "instance_class": "db.m6g.large",
        "subnet_group_name": "demo-private-subnets",
        "vpc_security_group_ids": ["sg-0123456789abcdef0"],
        "parameter_group_name": "demo-postgres16-tuned",
        **overrides,
    }


@pytest.mark.parametrize(
    "options",
    [
        _created_instance_options(),
        _created_instance_options(
            allocated_storage_gb=200,
            max_allocated_storage_gb=1000,
            storage_type="gp2",
            master_username="reports_admin",
            db_name="reports",
            multi_az=True,
            publicly_accessible=True,
            port=6543,
        ),
    ],
)
def test_the_direct_instance_create_sends_what_the_rendered_instance_carries(
    rendered_in_workspace, options
) -> None:
    change = InstanceCreate(TARGET_KEY, InstanceCreateOptions.model_validate(options), None)
    context = SimpleNamespace()
    change.prepare(context)
    request = change.create_arguments(context)
    rendered = change.rendered_cached
    assert isinstance(rendered, RenderedInstance)
    _assert_conforms(rendered, request, INSTANCE_CREATE_FIELDS_SENT_AS, INSTANCE_CREATE_REFERENCES_SENT_AS)
    _assert_no_password_key(request)
    assert rendered.manage_master_user_password is True
    assert change.post_create_attribute_values(context) == {}, "CreateDBInstance takes every setting"
    assert (request["StorageEncrypted"], request["BackupRetentionPeriod"], request["DeletionProtection"]) == (
        True,
        7,
        True,
    )


@pytest.mark.parametrize("reader_count", [0, 2])
def test_the_direct_cluster_create_sends_what_the_rendered_cluster_and_members_carry(
    rendered_in_workspace, reader_count
) -> None:
    options = ClusterCreateOptions(
        target_key=TARGET_KEY,
        identifier="demo-analytics",
        engine_version="16.4",
        subnet_group_name="demo-private-subnets",
        vpc_security_group_ids=["sg-0123456789abcdef0"],
        cluster_parameter_group_name="demo-aurora-pg16-tuned",
        database_name="analytics",
        writer_instance_class="db.r6g.large",
        reader_count=reader_count,
        reader_instance_class="db.r6g.xlarge",
    )
    change = ClusterCreate(TARGET_KEY, options, None)
    context = SimpleNamespace()
    change.prepare(context)
    request = change.create_arguments(context)
    rendered = change.rendered_cached
    assert isinstance(rendered, RenderedCluster)
    _assert_conforms(rendered, request, CLUSTER_CREATE_FIELDS_SENT_AS, CLUSTER_RESTORE_REFERENCES_SENT_AS)
    _assert_nested(rendered, request)
    _assert_no_password_key(request)
    assert change.post_create_attribute_values(context) == {}
    members = change.members_rendered_cached
    assert [member.identifier for member in members] == [
        f"demo-analytics-{position}" for position in range(1, reader_count + 2)
    ]
    for position, member in enumerate(members):
        member_request = change.member_arguments(context, member)
        _assert_conforms(member, member_request, READER_FIELDS_SENT_AS, READER_REFERENCES_SENT_AS)
        assert member_request["DBClusterIdentifier"] == "demo-analytics"
        assert member_request["DBInstanceClass"] == ("db.r6g.large" if position == 0 else "db.r6g.xlarge")
        assert member.auto_minor_version_upgrade is rendered.auto_minor_version_upgrade
