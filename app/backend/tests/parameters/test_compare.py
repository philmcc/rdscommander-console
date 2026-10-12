import boto3
import pytest

from rdscommander.aws.rds_reader import RdsReader
from rdscommander.inventory.snapshot_writer import discover_estate, write_snapshot
from rdscommander.parameters.compare import compare_parameter_groups
from tests.terraform.conftest import arn_for


def test_compare_against_defaults_and_another_group(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    tuned = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        against_defaults = compare_parameter_groups(
            session, console_config, tuned, None, against_defaults=True
        )
    rows = {r.name: r for r in against_defaults.rows}
    assert rows["work_mem"].left == "16384" and rows["work_mem"].default == "4096"
    assert rows["work_mem"].differs
    assert rows["shared_buffers"].apply_type == "static"
    assert against_defaults.right_name == "postgres16 defaults"
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_parameter_group(
        DBParameterGroupName="other", DBParameterGroupFamily="postgres16", Description="o"
    )
    rds.modify_db_parameter_group(
        DBParameterGroupName="other",
        Parameters=[{"ParameterName": "work_mem", "ParameterValue": "8192", "ApplyMethod": "immediate"}],
    )
    other_arn = rds.describe_db_parameter_groups(DBParameterGroupName="other")["DBParameterGroups"][0][
        "DBParameterGroupArn"
    ]
    with session_factory() as session:
        write_snapshot(
            session,
            console_config.target("sandbox/000000000000/eu-west-2"),
            None,
            discover_estate(RdsReader(rds)),
        )
        session.commit()
        comparison = compare_parameter_groups(
            session, console_config, tuned, other_arn, against_defaults=False
        )
    rows = {r.name: r for r in comparison.rows}
    assert rows["work_mem"].left == "16384" and rows["work_mem"].right == "8192" and rows["work_mem"].differs
    assert rows["log_min_duration_statement"].right is None and rows["log_min_duration_statement"].differs
    assert comparison.right_name == "other"
    assert not rows["shared_buffers"].differs  # both sides are at the family default


def test_compare_against_defaults_lists_every_family_parameter_and_marks_only_overrides_as_different(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    tuned = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    with session_factory() as session:
        comparison = compare_parameter_groups(session, console_config, tuned, None, against_defaults=True)
    rows = {r.name: r for r in comparison.rows}
    assert "random_page_cost" in rows and rows["random_page_cost"].left is None
    assert rows["random_page_cost"].right == rows["random_page_cost"].default == "4"
    assert not rows["random_page_cost"].differs
    assert {name for name, row in rows.items() if row.differs} == {
        "work_mem",
        "log_min_duration_statement",
        "rds.logical_replication",
    }


def test_compare_refuses_a_resource_that_is_not_a_parameter_group(
    session_factory, console_config, sandbox_snapshot, moto_aws
) -> None:
    orders = arn_for(session_factory, sandbox_snapshot, "demo-orders")
    with session_factory() as session, pytest.raises(ValueError, match="not a parameter group"):
        compare_parameter_groups(session, console_config, orders, None, against_defaults=True)


def test_compare_names_an_unknown_group(session_factory, console_config, sandbox_snapshot, moto_aws) -> None:
    tuned = arn_for(session_factory, sandbox_snapshot, "demo-postgres16-tuned")
    missing = tuned.replace("demo-postgres16-tuned", "no-such-group")
    with session_factory() as session:
        with pytest.raises(KeyError, match="no-such-group"):
            compare_parameter_groups(session, console_config, missing, None, against_defaults=True)
        with pytest.raises(KeyError, match="no-such-group"):
            compare_parameter_groups(session, console_config, tuned, missing, against_defaults=False)
