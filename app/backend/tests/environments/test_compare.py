import boto3
import pytest

from rdscommander.aws.rds_reader import RdsReader
from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.environments.compare import compare_environments, normalise_identifier
from rdscommander.inventory.snapshot_writer import discover_estate, write_snapshot


def _snapshot_for(session_factory, console_config, target_key: str, absent_instance_identifiers=()):
    """Snapshot the shared simulated account as `target_key`, minus the named instances."""
    sync_configuration_to_control_db(session_factory, console_config)  # the target row the snapshot refers to
    target = console_config.target(target_key)
    rds = boto3.client("rds", region_name=target.aws_region)
    estate = discover_estate(RdsReader(rds))
    estate.instances = [
        instance
        for instance in estate.instances
        if instance["DBInstanceIdentifier"] not in absent_instance_identifiers
    ]
    with session_factory() as session:
        snapshot = write_snapshot(session, target, None, estate)
        session.commit()
        return snapshot.id


def test_compare_matches_by_identifier_after_stripping_suffixes_and_lists_differences(
    session_factory, console_config, moto_aws, sandbox_snapshot
) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_instance(
        DBInstanceIdentifier="demo-orders-prod",
        Engine="postgres",
        EngineVersion="16.3",
        DBInstanceClass="db.m6g.xlarge",
        AllocatedStorage=100,
        MasterUsername="u",
        MasterUserPassword="passw0rd!",
        DBParameterGroupName="demo-postgres16-tuned",
        Tags=[{"Key": "env", "Value": "prod"}],
    )
    production_config = console_config.model_copy(deep=True)
    production_config.environment("production").targets[0].simulated = True
    production_config.environment("production").targets[0].aws_profile_name = None
    production_config.environment("production").targets[0].aws_account_id = "000000000000"
    # Both environments read the same simulated account, so production would also hold "demo-orders", and an
    # exact identifier always wins over a stripped one. Production proper has only "demo-orders-prod".
    _snapshot_for(
        session_factory,
        production_config,
        "production/000000000000/eu-west-2",
        absent_instance_identifiers={"demo-orders"},
    )
    with session_factory() as session:
        comparison = compare_environments(
            session, production_config, "sandbox", "production", strip_suffixes=["-prod"]
        )
    pair = next(p for p in comparison.pairs if p.left_identifier == "demo-orders")
    assert pair.right_identifier == "demo-orders-prod"
    assert {d.attribute: (d.left, d.right) for d in pair.differences}["db_instance_class"] == (
        "db.m6g.large",
        "db.m6g.xlarge",
    )
    assert {d.attribute for d in pair.differences} >= {"engine_version", "multi_az"}
    group_pair = next(
        p
        for p in comparison.pairs
        if p.kind == "parameter_group" and p.left_identifier == "demo-postgres16-tuned"
    )
    assert group_pair.differences == []


def test_an_exact_identifier_wins_over_a_stripped_one_and_unmatched_resources_are_listed(
    session_factory, console_config, moto_aws, sandbox_snapshot
) -> None:
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_instance(
        DBInstanceIdentifier="demo-orders-prod",
        Engine="postgres",
        EngineVersion="16.3",
        DBInstanceClass="db.m6g.xlarge",
        AllocatedStorage=100,
        MasterUsername="u",
        MasterUserPassword="passw0rd!",
    )
    production_config = console_config.model_copy(deep=True)
    production_config.environment("production").targets[0].simulated = True
    production_config.environment("production").targets[0].aws_profile_name = None
    production_config.environment("production").targets[0].aws_account_id = "000000000000"
    _snapshot_for(session_factory, production_config, "production/000000000000/eu-west-2")
    with session_factory() as session:
        comparison = compare_environments(
            session, production_config, "sandbox", "production", strip_suffixes=["-prod"]
        )
    orders = [p for p in comparison.pairs if p.left_identifier == "demo-orders"]
    assert [(p.right_identifier, p.differences) for p in orders] == [("demo-orders", [])]
    assert ("instance", "demo-orders-prod") in [(u.kind, u.identifier) for u in comparison.unmatched_right]
    assert comparison.unmatched_left == []
    assert len({p.right_resource_id for p in comparison.pairs}) == len(comparison.pairs)


def test_compare_refuses_an_unknown_environment(session_factory, console_config) -> None:
    with session_factory() as session, pytest.raises(KeyError):
        compare_environments(session, console_config, "sandbox", "nope", strip_suffixes=[])


@pytest.mark.parametrize(
    ("identifier", "affixes", "expected"),
    [
        ("orders-prod", ["-prod"], "orders"),
        ("prod-orders", ["prod-"], "orders"),
        ("orders-prod", ["-stg", "-prod"], "orders"),
        ("orders", ["-prod"], "orders"),
        ("orders-prod", ["", "-prod"], "orders"),
        ("orders-prod", [], "orders-prod"),
    ],
)
def test_normalise_identifier_strips_the_first_matching_affix(identifier, affixes, expected) -> None:
    assert normalise_identifier(identifier, affixes) == expected
