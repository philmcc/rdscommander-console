import json
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest

from rdscommander.aws.rds_reader import RdsReader
from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.inventory.snapshot_writer import discover_estate, write_snapshot
from rdscommander.parameters.metadata import FamilyMetadata, ParameterMetadata, _from_aws_entry
from rdscommander.parameters.upgrade_builder import (
    RENAMED_PARAMETERS,
    Classification,
    UpgradePlanError,
    build_upgrade_plan,
    classify_overrides,
    new_group_name_for,
    parameters_to_set,
    shared_instance_group_name,
)
from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord
from rdscommander.resources.queries import list_resources
from rdscommander.simulated.seeder import SIMULATED_MASTER_PASSWORD

FIXTURES = Path(__file__).parent / "fixtures"
REAL_TARGET_KEY = "production/123456789012/eu-west-2"


def _family(name: str) -> FamilyMetadata:
    data = json.loads((FIXTURES / name).read_text())
    return FamilyMetadata(
        family=data["family"],
        scope="instance",
        fetched_at_utc=datetime.now(UTC),
        parameters={e["ParameterName"]: _from_aws_entry(e) for e in data["parameters"]},
        source="bundled",
    )


def _instance_record(identifier: str, parameter_group_name: str | None) -> ResourceRecord:
    return ResourceRecord(
        kind=ResourceKind.INSTANCE,
        arn=f"arn:aws:rds:eu-west-2:000000000000:db:{identifier}",
        resource_id=encode_resource_id(f"arn:aws:rds:eu-west-2:000000000000:db:{identifier}"),
        identifier=identifier,
        environment_slug="sandbox",
        target_key="sandbox/000000000000/eu-west-2",
        ownership="unmanaged",
        status="available",
        snapshot_id=1,
        relationships={"parameter_group_name": parameter_group_name},
    )


def test_classification_covers_every_outcome(monkeypatch) -> None:
    monkeypatch.setitem(RENAMED_PARAMETERS, ("force_parallel_mode", "fixture16"), "debug_parallel_query")
    old, new = _family("family_old.json"), _family("family_new.json")
    overrides = {
        "work_mem": "65536",
        "log_statement": "mod",
        "old_param": "1",
        "force_parallel_mode": "on",
        "wal_compression": "off",
        "shared_buffers": "{DBInstanceClassMemory/16384}",
    }
    by_name = {c.name: c for c in classify_overrides(overrides, old, new)}
    assert by_name["work_mem"].outcome == "carried" and by_name["work_mem"].value == "65536"
    assert by_name["log_statement"].outcome == "needs_review" and "mod" in by_name["log_statement"].note
    assert by_name["old_param"].outcome == "removed"
    assert (
        by_name["force_parallel_mode"].outcome == "renamed"
        and by_name["force_parallel_mode"].new_name == "debug_parallel_query"
    )
    assert (
        by_name["wal_compression"].outcome == "default_changed"
        and by_name["wal_compression"].old_default == "off"
        and by_name["wal_compression"].new_default == "on"
    )
    assert by_name["shared_buffers"].outcome == "carried"


def test_classification_flags_a_parameter_whose_type_changed_between_families() -> None:
    def parameter(data_type: str) -> ParameterMetadata:
        return ParameterMetadata(
            name="idle_timeout",
            default_value="0",
            description="",
            data_type=data_type,
            allowed_values=None,
            is_modifiable=True,
            apply_type="dynamic",
            apply_method_default="immediate",
        )

    def family(name: str, data_type: str) -> FamilyMetadata:
        return FamilyMetadata(
            family=name,
            scope="instance",
            fetched_at_utc=datetime.now(UTC),
            parameters={"idle_timeout": parameter(data_type)},
            source="bundled",
        )

    [classification] = classify_overrides(
        {"idle_timeout": "5"}, family("fixture15", "integer"), family("fixture16", "string")
    )
    assert classification.outcome == "needs_review"
    assert "type changed from integer to string" in classification.note


def test_classification_drops_a_parameter_the_new_family_does_not_have_and_says_why() -> None:
    old, new = _family("family_old.json"), _family("family_new.json")
    [classification] = classify_overrides({"old_param": "1"}, old, new)
    assert classification.outcome == "removed"
    assert classification.note == "old_param does not exist in fixture16; dropped"
    assert classification.old_default == "0" and classification.new_default is None


def test_parameters_to_set_excludes_removed_and_needs_review_and_applies_renames(monkeypatch) -> None:
    monkeypatch.setitem(RENAMED_PARAMETERS, ("force_parallel_mode", "fixture16"), "debug_parallel_query")
    old, new = _family("family_old.json"), _family("family_new.json")
    classifications = classify_overrides(
        {
            "work_mem": "65536",
            "log_statement": "mod",
            "old_param": "1",
            "force_parallel_mode": "on",
            "wal_compression": "off",
        },
        old,
        new,
    )
    assert parameters_to_set(classifications) == {
        "work_mem": "65536",
        "debug_parallel_query": "on",
        "wal_compression": "off",
    }


def test_known_renames_include_the_postgres_16_parallel_mode_change() -> None:
    assert RENAMED_PARAMETERS[("force_parallel_mode", "postgres16")] == "debug_parallel_query"
    assert RENAMED_PARAMETERS[("force_parallel_mode", "aurora-postgresql16")] == "debug_parallel_query"


def test_a_tuned_source_group_gives_the_new_group_its_name_plus_the_target_major_and_suffix() -> None:
    assert (
        new_group_name_for("demo-postgres16-tuned", False, "demo-orders", "postgres17", "")
        == "demo-postgres16-tuned-pg17"
    )
    assert (
        new_group_name_for("demo-aurora-pg16-tuned", False, "demo-aurora", "aurora-postgresql17", "-v2")
        == "demo-aurora-pg16-tuned-pg17-v2"
    )


def test_an_aws_default_source_group_gives_the_new_group_the_resource_identifier_as_its_name() -> None:
    assert (
        new_group_name_for("default.aurora-postgresql16", True, "demo-aurora", "aurora-postgresql17", "")
        == "demo-aurora-pg17"
    )
    assert (
        new_group_name_for("default.postgres16", True, "demo-orders", "postgres17", "-rehearsal")
        == "demo-orders-pg17-rehearsal"
    )


def test_a_new_group_name_never_exceeds_the_255_characters_aws_allows() -> None:
    name = new_group_name_for("a" * 300, False, "demo", "postgres17", "")
    assert len(name) == 255


def test_members_that_share_one_instance_group_give_that_group() -> None:
    members = [_instance_record("writer", "shared-pg16"), _instance_record("reader", "shared-pg16")]
    assert shared_instance_group_name("demo-aurora", members) == "shared-pg16"


def test_members_without_any_instance_group_give_none() -> None:
    assert shared_instance_group_name("demo-aurora", []) is None
    assert shared_instance_group_name("demo-aurora", [_instance_record("writer", None)]) is None


def test_members_that_use_different_instance_groups_are_refused_with_the_groups_named() -> None:
    members = [_instance_record("writer", "writer-pg16"), _instance_record("reader", "reader-pg16")]
    with pytest.raises(UpgradePlanError) as refusal:
        shared_instance_group_name("demo-aurora", members)
    assert "reader-pg16" in str(refusal.value) and "writer-pg16" in str(refusal.value)
    assert "each instance" in refusal.value.fix_suggestion


def test_a_classification_that_is_carried_has_no_note_and_keeps_both_defaults() -> None:
    old, new = _family("family_old.json"), _family("family_new.json")
    [classification] = classify_overrides({"work_mem": "8192"}, old, new)
    assert classification == Classification(
        name="work_mem", value="8192", outcome="carried", old_default="4096", new_default="4096"
    )


INSTANCE_LEVEL_DEFAULTS = [
    {
        "ParameterName": "max_connections",
        "ParameterValue": "LEAST({DBInstanceClassMemory/9531392},5000)",
        "DataType": "integer",
        "AllowedValues": "6-262143",
        "IsModifiable": True,
        "ApplyType": "static",
    },
    {
        "ParameterName": "work_mem",
        "ParameterValue": "4096",
        "DataType": "integer",
        "AllowedValues": "64-2147483647",
        "IsModifiable": True,
        "ApplyType": "dynamic",
    },
]
CLUSTER_LEVEL_DEFAULTS = [
    {
        "ParameterName": "rds.force_ssl",
        "ParameterValue": "0",
        "DataType": "boolean",
        "AllowedValues": "0,1",
        "IsModifiable": True,
        "ApplyType": "dynamic",
    },
    {
        "ParameterName": "work_mem",
        "ParameterValue": "4096",
        "DataType": "integer",
        "AllowedValues": "64-2147483647",
        "IsModifiable": True,
        "ApplyType": "dynamic",
    },
]


class _Pages:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages

    def paginate(self, **kwargs):  # noqa: ANN003, ANN201
        return iter(self.pages)


class _RealAuroraAccountClient:
    """Answers the metadata calls as a real account does: Aurora's instance and cluster defaults differ."""

    def get_paginator(self, name: str) -> _Pages:
        if name == "describe_db_engine_versions":
            return _Pages(
                [
                    {
                        "DBEngineVersions": [
                            {
                                "Engine": "aurora-postgresql",
                                "EngineVersion": "16.4",
                                "DBParameterGroupFamily": "aurora-postgresql16",
                                "ValidUpgradeTarget": [
                                    {"EngineVersion": "17.2", "IsMajorVersionUpgrade": True}
                                ],
                            },
                            {
                                "Engine": "aurora-postgresql",
                                "EngineVersion": "17.2",
                                "DBParameterGroupFamily": "aurora-postgresql17",
                            },
                        ]
                    }
                ]
            )
        if name == "describe_engine_default_parameters":
            return _Pages([{"EngineDefaults": {"Parameters": INSTANCE_LEVEL_DEFAULTS}}])
        raise AssertionError(f"unexpected paginator {name}")

    def describe_engine_default_cluster_parameters(self, **kwargs):  # noqa: ANN003, ANN201
        return {"EngineDefaults": {"Parameters": CLUSTER_LEVEL_DEFAULTS}}


def test_an_aurora_upgrade_keeps_an_instance_group_override_that_aws_lists_only_for_instance_groups(
    session_factory, console_config, moto_aws
) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_cluster_parameter_group(
        DBClusterParameterGroupName="orders-cluster-pg16",
        DBParameterGroupFamily="aurora-postgresql16",
        Description="orders cluster",
    )
    rds.modify_db_cluster_parameter_group(
        DBClusterParameterGroupName="orders-cluster-pg16",
        Parameters=[{"ParameterName": "rds.force_ssl", "ParameterValue": "1", "ApplyMethod": "immediate"}],
    )
    rds.create_db_parameter_group(
        DBParameterGroupName="orders-members-pg16",
        DBParameterGroupFamily="aurora-postgresql16",
        Description="orders members",
    )
    rds.modify_db_parameter_group(
        DBParameterGroupName="orders-members-pg16",
        Parameters=[
            {"ParameterName": "max_connections", "ParameterValue": "500", "ApplyMethod": "pending-reboot"}
        ],
    )
    rds.create_db_cluster(
        DBClusterIdentifier="orders-aurora",
        Engine="aurora-postgresql",
        EngineVersion="16.4",
        MasterUsername="orders_admin",
        MasterUserPassword=SIMULATED_MASTER_PASSWORD,
        DBClusterParameterGroupName="orders-cluster-pg16",
    )
    rds.create_db_instance(
        DBInstanceIdentifier="orders-aurora-writer",
        DBClusterIdentifier="orders-aurora",
        Engine="aurora-postgresql",
        DBInstanceClass="db.r6g.large",
        DBParameterGroupName="orders-members-pg16",
    )
    sync_configuration_to_control_db(session_factory, console_config)
    real_target = console_config.target(REAL_TARGET_KEY)
    with session_factory() as session:
        write_snapshot(session, real_target, None, discover_estate(RdsReader(rds)))
        session.commit()
        [cluster] = [
            record
            for record in list_resources(session, console_config, "production", ResourceKind.CLUSTER)
            if record.identifier == "orders-aurora"
        ]
        plans = build_upgrade_plan(
            session,
            console_config,
            cluster,
            "17.2",
            reader=RdsReader(_RealAuroraAccountClient()),  # type: ignore[arg-type]
        )
    by_scope = {plan.scope: plan for plan in plans}
    assert [(c.name, c.outcome) for c in by_scope["instance"].classifications] == [
        ("max_connections", "carried")
    ]
    assert by_scope["instance"].parameters_to_set == {"max_connections": "500"}
    assert [(c.name, c.outcome) for c in by_scope["cluster"].classifications] == [
        ("rds.force_ssl", "carried")
    ]
