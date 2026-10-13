from datetime import UTC, datetime

from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.control_db.models import (
    InventoryDbInstance,
    InventoryDbSnapshot,
    InventoryPendingMaintenance,
)
from rdscommander.inventory.snapshot_writer import DiscoveredEstate, discover_estate, write_snapshot

INSTANCE_ARN = "arn:aws:rds:eu-west-2:000000000000:db:pg-one"
CLUSTER_SNAPSHOT_ARN = "arn:aws:rds:eu-west-2:000000000000:cluster-snapshot:aurora-one-manual"


def test_write_snapshot_stores_cluster_snapshots_pending_maintenance_and_apply_status(
    session_factory, console_config
) -> None:
    sync_configuration_to_control_db(session_factory, console_config)
    target = console_config.target("sandbox/000000000000/eu-west-2")
    auto_applied_after = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    discovered = DiscoveredEstate(
        instances=[
            {
                "DBInstanceIdentifier": "pg-one",
                "DBInstanceArn": INSTANCE_ARN,
                "Engine": "postgres",
                "DBParameterGroups": [
                    {"DBParameterGroupName": "custom-pg16", "ParameterApplyStatus": "pending-reboot"}
                ],
            }
        ],
        cluster_snapshots=[
            {
                "DBClusterSnapshotIdentifier": "aurora-one-manual",
                "DBClusterSnapshotArn": CLUSTER_SNAPSHOT_ARN,
                "DBClusterIdentifier": "aurora-one",
                "Engine": "aurora-postgresql",
                "EngineVersion": "16.4",
                "Status": "available",
                "AllocatedStorage": 1,
                "SnapshotType": "manual",
                "SnapshotCreateTime": datetime(2026, 10, 9, 12, 0, tzinfo=UTC),
            }
        ],
        pending_maintenance=[
            {
                "ResourceIdentifier": INSTANCE_ARN,
                "PendingMaintenanceActionDetails": [
                    {
                        "Action": "system-update",
                        "Description": "New OS patch",
                        "AutoAppliedAfterDate": auto_applied_after,
                        "OptInStatus": "next-maintenance",
                    },
                    {"Action": "db-upgrade", "Description": "Minor version upgrade"},
                ],
            }
        ],
        tags_by_arn={CLUSTER_SNAPSHOT_ARN: {"keep": "yes"}},
    )
    with session_factory() as session:
        snapshot = write_snapshot(session, target, None, discovered)
        session.commit()
        instance = session.query(InventoryDbInstance).filter_by(snapshot_id=snapshot.id).one()
        assert instance.parameter_apply_status == "pending-reboot"
        cluster_snapshot = session.query(InventoryDbSnapshot).filter_by(snapshot_id=snapshot.id).one()
        assert cluster_snapshot.snapshot_scope == "cluster"
        assert cluster_snapshot.db_snapshot_identifier == "aurora-one-manual"
        assert cluster_snapshot.source_identifier == "aurora-one"
        assert cluster_snapshot.tags == {"keep": "yes"} and cluster_snapshot.allocated_storage_gb == 1
        actions = {
            row.action: row
            for row in session.query(InventoryPendingMaintenance).filter_by(snapshot_id=snapshot.id)
        }
        assert set(actions) == {"system-update", "db-upgrade"}
        assert actions["system-update"].resource_arn == INSTANCE_ARN
        assert actions["system-update"].auto_applied_after_utc == auto_applied_after
        assert actions["system-update"].opt_in_status == "next-maintenance"
        assert actions["db-upgrade"].auto_applied_after_utc is None


class _FakeReaderCountingTagLookups:
    """Serves one described resource of each tagged kind and records every `list_tags` call."""

    def __init__(self, described_items: dict[str, list[dict]]) -> None:
        self.described_items = described_items
        self.list_tags_calls: list[str] = []

    def list_postgres_instances(self) -> list[dict]:
        return self.described_items.get("instances", [])

    def list_postgres_clusters(self) -> list[dict]:
        return self.described_items.get("clusters", [])

    def list_db_snapshots(self) -> list[dict]:
        return self.described_items.get("db_snapshots", [])

    def list_cluster_snapshots(self) -> list[dict]:
        return self.described_items.get("cluster_snapshots", [])

    def list_instance_parameter_groups(self) -> list[dict]:
        return []

    def list_cluster_parameter_groups(self) -> list[dict]:
        return []

    def list_subnet_groups(self) -> list[dict]:
        return []

    def list_pending_maintenance_actions(self) -> list[dict]:
        return []

    def list_tags(self, resource_arn: str) -> dict[str, str]:
        self.list_tags_calls.append(resource_arn)
        return {"looked-up": "yes"}


def test_discover_estate_looks_tags_up_only_when_the_description_has_no_tag_list() -> None:
    reader = _FakeReaderCountingTagLookups(
        {
            "instances": [
                {"DBInstanceArn": "arn:instance:empty", "TagList": []},
                {"DBInstanceArn": "arn:instance:tagged", "TagList": [{"Key": "team", "Value": "data"}]},
                {"DBInstanceArn": "arn:instance:absent"},
            ],
            "clusters": [
                {"DBClusterArn": "arn:cluster:empty", "TagList": []},
                {"DBClusterArn": "arn:cluster:absent"},
            ],
            "db_snapshots": [
                {"DBSnapshotArn": "arn:snapshot:empty", "TagList": []},
                {"DBSnapshotArn": "arn:snapshot:tagged", "TagList": [{"Key": "keep", "Value": "yes"}]},
                {"DBSnapshotArn": "arn:snapshot:absent"},
            ],
            "cluster_snapshots": [
                {"DBClusterSnapshotArn": "arn:cluster-snapshot:empty", "TagList": []},
                {"DBClusterSnapshotArn": "arn:cluster-snapshot:absent"},
            ],
        }
    )
    estate = discover_estate(reader)  # type: ignore[arg-type]
    assert sorted(reader.list_tags_calls) == [
        "arn:cluster-snapshot:absent",
        "arn:cluster:absent",
        "arn:instance:absent",
        "arn:snapshot:absent",
    ]
    assert estate.tags_by_arn["arn:instance:empty"] == {}
    assert estate.tags_by_arn["arn:instance:tagged"] == {"team": "data"}
    assert estate.tags_by_arn["arn:snapshot:empty"] == {}
    assert estate.tags_by_arn["arn:snapshot:tagged"] == {"keep": "yes"}
    assert estate.tags_by_arn["arn:cluster:empty"] == {}
    assert estate.tags_by_arn["arn:cluster-snapshot:empty"] == {}
    for absent_arn in reader.list_tags_calls:
        assert estate.tags_by_arn[absent_arn] == {"looked-up": "yes"}
