import json

import boto3
import pytest
import structlog
from botocore.exceptions import ClientError

from rdscommander.aws.rds_reader import RdsReader, SimulatorUnsupportedAction, tags_to_mapping
from rdscommander.logging_setup import configure_structured_logging


def _seed_two_engines(rds) -> None:
    rds.create_db_instance(
        DBInstanceIdentifier="pg-one",
        Engine="postgres",
        EngineVersion="16.3",
        DBInstanceClass="db.t4g.micro",
        MasterUsername="u",
        MasterUserPassword="password1",
        AllocatedStorage=20,
        Tags=[{"Key": "team", "Value": "data"}],
    )
    rds.create_db_instance(
        DBInstanceIdentifier="my-one",
        Engine="mysql",
        DBInstanceClass="db.t4g.micro",
        MasterUsername="u",
        MasterUserPassword="password1",
        AllocatedStorage=20,
    )
    rds.create_db_parameter_group(
        DBParameterGroupName="custom-pg16", DBParameterGroupFamily="postgres16", Description="custom"
    )
    rds.modify_db_parameter_group(
        DBParameterGroupName="custom-pg16",
        Parameters=[
            {
                "ParameterName": "log_min_duration_statement",
                "ParameterValue": "500",
                "ApplyMethod": "immediate",
            }
        ],
    )
    rds.create_db_parameter_group(
        DBParameterGroupName="custom-mysql", DBParameterGroupFamily="mysql8.0", Description="mysql"
    )


def test_reader_filters_to_postgres_engines_and_families(moto_aws) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    _seed_two_engines(rds)
    reader = RdsReader(rds)
    assert [i["DBInstanceIdentifier"] for i in reader.list_postgres_instances()] == ["pg-one"]
    group_names = [g["DBParameterGroupName"] for g in reader.list_instance_parameter_groups()]
    assert "custom-pg16" in group_names and "custom-mysql" not in group_names
    assert all(not name.startswith("default.mysql") for name in group_names)
    assert reader.list_postgres_clusters() == []


def test_reader_lists_parameters_and_tags(moto_aws) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    _seed_two_engines(rds)
    reader = RdsReader(rds)
    parameters = reader.list_parameters("custom-pg16")
    assert any(
        p["ParameterName"] == "log_min_duration_statement" and p["ParameterValue"] == "500"
        for p in parameters
    )
    [instance] = reader.list_postgres_instances()
    assert reader.list_tags(instance["DBInstanceArn"]) == {"team": "data"}


def test_tags_to_mapping_handles_missing_list() -> None:
    assert tags_to_mapping(None) == {}
    assert tags_to_mapping([{"Key": "a", "Value": "1"}]) == {"a": "1"}


def test_reader_lists_snapshots_pending_maintenance_and_engine_metadata(moto_aws) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    _seed_two_engines(rds)
    rds.create_db_snapshot(
        DBSnapshotIdentifier="pg-one-manual",
        DBInstanceIdentifier="pg-one",
        Tags=[{"Key": "keep", "Value": "yes"}],
    )
    reader = RdsReader(rds)
    snapshots = reader.list_db_snapshots()
    # the simulator takes an automated snapshot of every new instance; manual ones sit beside them
    assert {s["SnapshotType"] for s in snapshots} == {"automated", "manual"}
    assert {s["DBInstanceIdentifier"] for s in snapshots} == {"pg-one"}
    [snapshot] = [s for s in snapshots if s["SnapshotType"] == "manual"]
    assert snapshot["DBSnapshotIdentifier"] == "pg-one-manual" and snapshot["Engine"] == "postgres"
    assert reader.list_cluster_snapshots() == []
    # the simulator cannot answer this call; the reader treats that as "none"
    assert reader.list_pending_maintenance_actions() == []


def test_reader_raises_on_engine_metadata_calls_the_simulator_cannot_answer(moto_aws) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    reader = RdsReader(rds)
    with pytest.raises(SimulatorUnsupportedAction):
        reader.describe_engine_default_parameters("postgres16", "instance")
    with pytest.raises(SimulatorUnsupportedAction):
        reader.describe_engine_default_parameters("aurora-postgresql16", "cluster")
    with pytest.raises(SimulatorUnsupportedAction):
        reader.list_engine_versions("postgres")


class _FakePaginator:
    def __init__(self, pages):
        self.pages = pages

    def paginate(self, **kwargs):
        return iter(self.pages)


def test_reader_passes_real_engine_metadata_through() -> None:
    class FakeClient:
        def get_paginator(self, name):
            if name == "describe_engine_default_parameters":
                return _FakePaginator(
                    [
                        {
                            "EngineDefaults": {
                                "Parameters": [
                                    {
                                        "ParameterName": "work_mem",
                                        "ParameterValue": "4096",
                                        "ApplyType": "dynamic",
                                        "DataType": "integer",
                                        "AllowedValues": "64-2147483647",
                                        "IsModifiable": True,
                                    }
                                ]
                            }
                        }
                    ]
                )
            if name == "describe_db_engine_versions":
                return _FakePaginator(
                    [
                        {
                            "DBEngineVersions": [
                                {
                                    "Engine": "postgres",
                                    "EngineVersion": "16.4",
                                    "DBParameterGroupFamily": "postgres16",
                                    "ValidUpgradeTarget": [
                                        {"EngineVersion": "17.2", "IsMajorVersionUpgrade": True}
                                    ],
                                }
                            ]
                        }
                    ]
                )
            raise AssertionError(name)

    reader = RdsReader(FakeClient())  # type: ignore[arg-type]
    assert (
        reader.describe_engine_default_parameters("postgres16", "instance")[0]["ParameterName"] == "work_mem"
    )
    assert (
        reader.list_engine_versions("postgres")[0]["ValidUpgradeTarget"][0]["IsMajorVersionUpgrade"] is True
    )


def test_reader_walks_the_marker_of_the_aurora_cluster_defaults_call() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.requested_markers: list[str | None] = []

        def describe_engine_default_cluster_parameters(self, **kwargs):
            assert kwargs["DBParameterGroupFamily"] == "aurora-postgresql16"
            self.requested_markers.append(kwargs.get("Marker"))
            if "Marker" not in kwargs:
                return {"EngineDefaults": {"Parameters": [{"ParameterName": "first"}], "Marker": "page-2"}}
            return {"EngineDefaults": {"Parameters": [{"ParameterName": "second"}]}}

    client = FakeClient()
    reader = RdsReader(client)  # type: ignore[arg-type]
    parameters = reader.describe_engine_default_parameters("aurora-postgresql16", "cluster")
    assert [p["ParameterName"] for p in parameters] == ["first", "second"]
    assert client.requested_markers == [None, "page-2"]


def _client_error(message: str) -> ClientError:
    return ClientError({"Error": {"Code": "InvalidAction", "Message": message}}, "DescribeSomething")


def test_reader_translates_the_server_flavour_not_implemented_error() -> None:
    class FakeClient:
        def get_paginator(self, name):
            raise _client_error("The describe_db_engine_versions action has not been implemented")

        def describe_engine_default_cluster_parameters(self, **kwargs):
            raise _client_error(
                "The describe_engine_default_cluster_parameters action has not been implemented"
            )

    reader = RdsReader(FakeClient())  # type: ignore[arg-type]
    with pytest.raises(SimulatorUnsupportedAction):
        reader.list_engine_versions("postgres")
    with pytest.raises(SimulatorUnsupportedAction):
        reader.describe_engine_default_parameters("aurora-postgresql16", "cluster")
    assert reader.list_pending_maintenance_actions() == []


def test_reader_re_raises_errors_that_are_not_the_simulator_limitation() -> None:
    class FakeClient:
        def get_paginator(self, name):
            raise _client_error("Access denied")

        def describe_engine_default_cluster_parameters(self, **kwargs):
            raise _client_error("Access denied")

    reader = RdsReader(FakeClient())  # type: ignore[arg-type]
    with pytest.raises(ClientError):
        reader.list_pending_maintenance_actions()
    with pytest.raises(ClientError):
        reader.list_engine_versions("postgres")
    with pytest.raises(ClientError):
        reader.describe_engine_default_parameters("aurora-postgresql16", "cluster")


def test_reader_logs_an_info_event_when_pending_maintenance_is_unsupported(capsys, moto_aws) -> None:
    configure_structured_logging()
    try:
        RdsReader(boto3.client("rds", region_name="eu-west-2")).list_pending_maintenance_actions()
        events = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines()]
    finally:
        structlog.reset_defaults()
    [event] = [e for e in events if e["event"] == "simulator_action_unsupported"]
    assert event["level"] == "info" and event["operation_name"] == "describe_pending_maintenance_actions"


def test_reader_reads_the_instance_defaults_of_an_aurora_family_with_the_instance_call() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.paginated_operations: list[str] = []

        def get_paginator(self, name):
            self.paginated_operations.append(name)
            return _FakePaginator(
                [{"EngineDefaults": {"Parameters": [{"ParameterName": "max_connections"}]}}]
            )

        def describe_engine_default_cluster_parameters(self, **kwargs):
            raise AssertionError("an instance group's defaults come from DescribeEngineDefaultParameters")

    client = FakeClient()
    parameters = RdsReader(client).describe_engine_default_parameters(  # type: ignore[arg-type]
        "aurora-postgresql16", "instance"
    )
    assert [p["ParameterName"] for p in parameters] == ["max_connections"]
    assert client.paginated_operations == ["describe_engine_default_parameters"]
