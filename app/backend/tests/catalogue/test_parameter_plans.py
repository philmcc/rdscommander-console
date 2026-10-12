"""Request-time checks of the parameter group operations, against simulated and real targets."""

import pytest

from rdscommander.catalogue.catalogue import prepare_parameter_create_plan, prepare_parameter_edit_plan
from rdscommander.catalogue.definitions import PlanPreparationError
from rdscommander.catalogue.options import ParameterGroupCreateOptions, ParameterGroupEditOptions
from rdscommander.control_db.models import EngineParameterFamily
from rdscommander.resources.identity import encode_resource_id
from rdscommander.resources.model import ResourceKind, ResourceRecord

REAL_TARGET_KEY = "production/123456789012/eu-west-2"
SIMULATED_TARGET_KEY = "sandbox/000000000000/eu-west-2"
AWS_ONLY_PARAMETER = "rds.parameter_the_bundle_lacks"


def _create_options(target_key: str, **overrides) -> ParameterGroupCreateOptions:
    values = {
        "target_key": target_key,
        "name": "orders-pg16-v2",
        "scope": "instance",
        "family": "postgres16",
        "description": "orders v2",
        "parameters": {},
    }
    return ParameterGroupCreateOptions(**{**values, **overrides})


def _group_record(target_key: str) -> ResourceRecord:
    return ResourceRecord(
        kind=ResourceKind.PARAMETER_GROUP,
        arn="arn:aws:rds:eu-west-2:123456789012:pg:prod-pg16",
        resource_id=encode_resource_id("arn:aws:rds:eu-west-2:123456789012:pg:prod-pg16"),
        identifier="prod-pg16",
        environment_slug=target_key.split("/")[0],
        target_key=target_key,
        ownership="adopted",
        status="available",
        snapshot_id=1,
        facts={"parameter_group_family": "postgres16", "group_scope": "instance", "is_default": False},
    )


def _problems(raised: pytest.ExceptionInfo[PlanPreparationError]) -> set[str]:
    return set(raised.value.problems)


def test_a_real_account_is_not_refused_a_parameter_only_aws_knows_but_its_values_are_checked(
    session_factory, console_config
) -> None:
    with session_factory() as session:
        accepted = prepare_parameter_create_plan(
            session,
            console_config,
            _create_options(REAL_TARGET_KEY, parameters={AWS_ONLY_PARAMETER: "1", "work_mem": "65536"}),
            None,
        )
        assert accepted["parameters"] == {AWS_ONLY_PARAMETER: "1", "work_mem": "65536"}
        with pytest.raises(PlanPreparationError) as raised:
            prepare_parameter_create_plan(
                session,
                console_config,
                _create_options(REAL_TARGET_KEY, parameters={AWS_ONLY_PARAMETER: "1", "work_mem": "abc"}),
                None,
            )
    assert _problems(raised) == {"work_mem must be an integer"}


def test_an_edit_on_a_real_account_leaves_unknown_names_and_resets_to_the_executor(
    session_factory, console_config
) -> None:
    with session_factory() as session:
        accepted = prepare_parameter_edit_plan(
            session,
            console_config,
            ParameterGroupEditOptions(parameters={AWS_ONLY_PARAMETER: "1"}, reset=["another.aws.parameter"]),
            _group_record(REAL_TARGET_KEY),
        )
        assert accepted["parameters"] == {AWS_ONLY_PARAMETER: "1"}
        with pytest.raises(PlanPreparationError) as raised:
            prepare_parameter_edit_plan(
                session,
                console_config,
                ParameterGroupEditOptions(
                    parameters={AWS_ONLY_PARAMETER: "1", "work_mem": "abc"},
                    reset=["rds.rds_superuser_reserved_connections"],
                ),
                _group_record(REAL_TARGET_KEY),
            )
    assert _problems(raised) == {
        "work_mem must be an integer",
        "rds.rds_superuser_reserved_connections cannot be modified",
    }


def test_the_simulated_account_still_refuses_an_unknown_name_because_the_bundle_is_its_authority(
    session_factory, console_config
) -> None:
    with session_factory() as session, pytest.raises(PlanPreparationError) as raised:
        prepare_parameter_create_plan(
            session,
            console_config,
            _create_options(SIMULATED_TARGET_KEY, parameters={AWS_ONLY_PARAMETER: "1"}),
            None,
        )
    assert _problems(raised) == {f"{AWS_ONLY_PARAMETER} is not a parameter of postgres16"}


def test_a_real_account_with_fresh_aws_metadata_refuses_an_unknown_name(
    session_factory, console_config
) -> None:
    with session_factory() as session:
        session.add(
            EngineParameterFamily(
                family="postgres16",
                scope="instance",
                source="aws",
                parameters=[
                    {
                        "ParameterName": "work_mem",
                        "ParameterValue": "4096",
                        "DataType": "integer",
                        "AllowedValues": "64-2147483647",
                        "IsModifiable": True,
                        "ApplyType": "dynamic",
                    }
                ],
            )
        )
        session.commit()
        with pytest.raises(PlanPreparationError) as raised:
            prepare_parameter_create_plan(
                session,
                console_config,
                _create_options(REAL_TARGET_KEY, parameters={AWS_ONLY_PARAMETER: "1"}),
                None,
            )
    assert _problems(raised) == {f"{AWS_ONLY_PARAMETER} is not a parameter of postgres16"}


def test_a_real_account_with_a_family_nobody_can_check_offline_is_let_through(
    session_factory, console_config
) -> None:
    with session_factory() as session:
        accepted = prepare_parameter_create_plan(
            session,
            console_config,
            _create_options(REAL_TARGET_KEY, family="postgres14", parameters={"work_mem": "abc"}),
            None,
        )
    assert accepted["family"] == "postgres14"  # the executor validates it against AWS


def _aurora_group_record(scope: str) -> ResourceRecord:
    arn_type = "cluster-pg" if scope == "cluster" else "pg"
    arn = f"arn:aws:rds:eu-west-2:000000000000:{arn_type}:aurora-orders-pg16"
    return ResourceRecord(
        kind=ResourceKind.PARAMETER_GROUP,
        arn=arn,
        resource_id=encode_resource_id(arn),
        identifier="aurora-orders-pg16",
        environment_slug="sandbox",
        target_key=SIMULATED_TARGET_KEY,
        ownership="unmanaged",
        status="available",
        snapshot_id=1,
        facts={"parameter_group_family": "aurora-postgresql16", "group_scope": scope, "is_default": False},
    )


def test_an_aurora_instance_group_edit_is_checked_against_the_instance_parameters_of_its_family(
    session_factory, console_config
) -> None:
    with session_factory() as session:
        accepted = prepare_parameter_edit_plan(
            session,
            console_config,
            ParameterGroupEditOptions(parameters={"max_connections": "500"}),
            _aurora_group_record("instance"),
        )
        assert accepted["parameters"] == {"max_connections": "500"}
        with pytest.raises(PlanPreparationError) as raised:
            prepare_parameter_edit_plan(
                session,
                console_config,
                ParameterGroupEditOptions(parameters={"rds.logical_replication": "1"}),
                _aurora_group_record("instance"),
            )
    assert _problems(raised) == {
        "rds.logical_replication is not a parameter of aurora-postgresql16 instance groups"
    }


def test_an_aurora_cluster_group_refuses_an_instance_only_parameter_at_request_time(
    session_factory, console_config
) -> None:
    with session_factory() as session:
        with pytest.raises(PlanPreparationError) as raised_edit:
            prepare_parameter_edit_plan(
                session,
                console_config,
                ParameterGroupEditOptions(parameters={"max_connections": "500"}),
                _aurora_group_record("cluster"),
            )
        with pytest.raises(PlanPreparationError) as raised_create:
            prepare_parameter_create_plan(
                session,
                console_config,
                _create_options(
                    SIMULATED_TARGET_KEY,
                    scope="instance",
                    family="aurora-postgresql16",
                    parameters={"rds.logical_replication": "1"},
                ),
                None,
            )
    assert _problems(raised_edit) == {
        "max_connections is not a parameter of aurora-postgresql16 cluster groups"
    }
    assert _problems(raised_create) == {
        "rds.logical_replication is not a parameter of aurora-postgresql16 instance groups"
    }
