import uuid

import boto3
import pytest

from rdscommander.config.models import ConsoleConfig
from rdscommander.config.store import StaticConfigStore
from rdscommander.control_db.config_sync import sync_configuration_to_control_db
from rdscommander.control_db.models import (
    InventoryDbInstance,
    InventoryDbSnapshot,
    InventoryParameterGroup,
    InventoryParameterValue,
    InventorySnapshot,
    Operation,
)
from rdscommander.inventory.queries import latest_snapshot_for_target
from rdscommander.inventory.refresh_executor import InventoryRefreshExecutor
from rdscommander.main import build_executors, build_services
from rdscommander.operations.enums import OperationState
from rdscommander.operations.runner import OperationRunner
from rdscommander.operations.service import OperationService


def _seed(region: str) -> None:
    rds = boto3.client("rds", region_name=region)
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
    rds.create_db_instance(
        DBInstanceIdentifier="pg-one",
        Engine="postgres",
        EngineVersion="16.3",
        DBInstanceClass="db.t4g.micro",
        MasterUsername="u",
        MasterUserPassword="password1",
        AllocatedStorage=20,
        DBParameterGroupName="custom-pg16",
        Tags=[{"Key": "terraform", "Value": "true"}],
    )
    rds.create_db_instance(
        DBInstanceIdentifier="pg-two",
        Engine="postgres",
        EngineVersion="16.3",
        DBInstanceClass="db.t4g.micro",
        MasterUsername="u",
        MasterUserPassword="password1",
        AllocatedStorage=20,
    )


def test_refresh_operation_writes_a_snapshot_per_target(
    session_factory, operator_settings, console_config, config_store, moto_aws
) -> None:
    _seed("eu-west-2")
    sync_configuration_to_control_db(session_factory, console_config)
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "runner-test",
        operator_settings,
        config_store,
        services,
    )
    operation = OperationService(session_factory).create(
        "inventory_refresh", "sandbox", None, {}, "refresh-1", "phil"
    )
    runner.run_once()
    assert OperationService(session_factory).get(operation.id).state == OperationState.SUCCEEDED
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, "sandbox/000000000000/eu-west-2")
        assert snapshot is not None and snapshot.instance_count == 2 and snapshot.parameter_group_count >= 1
        instances = {
            i.db_instance_identifier: i
            for i in session.query(InventoryDbInstance).filter_by(snapshot_id=snapshot.id)
        }
        assert instances["pg-one"].ownership == "foreign_terraform"
        assert instances["pg-two"].ownership == "unmanaged"
        [group] = session.query(InventoryParameterGroup).filter_by(
            snapshot_id=snapshot.id, parameter_group_name="custom-pg16"
        )
        assert group.user_set_parameter_count == 1 and group.is_default is False
        [value] = session.query(InventoryParameterValue).filter_by(
            parameter_group_id=group.id, parameter_name="log_min_duration_statement"
        )
        assert value.parameter_value == "500" and value.source == "user"


def test_refresh_is_idempotent_on_resume(
    session_factory, operator_settings, console_config, config_store, moto_aws
) -> None:
    _seed("eu-west-2")
    sync_configuration_to_control_db(session_factory, console_config)
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "runner-test",
        operator_settings,
        config_store,
        services,
    )
    operation = OperationService(session_factory).create(
        "inventory_refresh", "sandbox", None, {}, "refresh-2", "phil"
    )
    runner.run_once()
    with session_factory() as session:
        row = session.get(type(operation), operation.id)
        row.state = OperationState.QUEUED.value
        session.commit()
    runner.run_once()
    with session_factory() as session:
        assert session.query(InventorySnapshot).filter_by(operation_id=operation.id).count() == 1


def test_refresh_captures_snapshots_and_apply_status(
    session_factory, operator_settings, config_store, console_config, moto_aws
) -> None:
    _seed("eu-west-2")
    sync_configuration_to_control_db(session_factory, console_config)
    rds = boto3.client("rds", region_name="eu-west-2")
    rds.create_db_snapshot(
        DBSnapshotIdentifier="pg-two-manual",
        DBInstanceIdentifier="pg-two",
        Tags=[{"Key": "keep", "Value": "yes"}],
    )
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, config_store),
        "runner-test",
        operator_settings,
        config_store,
        services,
    )
    OperationService(session_factory).create("inventory_refresh", "sandbox", None, {}, "refresh-3", "phil")
    runner.run_once()
    with session_factory() as session:
        snapshot = latest_snapshot_for_target(session, "sandbox/000000000000/eu-west-2")
        db_snapshots = session.query(InventoryDbSnapshot).filter_by(snapshot_id=snapshot.id).all()
        # the simulator takes an automated snapshot of every new instance; the manual one sits beside them
        assert {s.snapshot_type for s in db_snapshots} == {"automated", "manual"}
        assert {s.snapshot_scope for s in db_snapshots} == {"instance"}
        [db_snapshot] = [s for s in db_snapshots if s.snapshot_type == "manual"]
        assert (
            db_snapshot.db_snapshot_identifier == "pg-two-manual"
            and db_snapshot.source_identifier == "pg-two"
        )
        assert db_snapshot.engine_version and db_snapshot.status and db_snapshot.allocated_storage_gb == 20
        assert db_snapshot.created_at_source_utc is not None and db_snapshot.tags == {"keep": "yes"}
        instance = (
            session.query(InventoryDbInstance)
            .filter_by(snapshot_id=snapshot.id, db_instance_identifier="pg-one")
            .one()
        )
        assert instance.parameter_apply_status in ("in-sync", "pending-reboot", "applying", None)


TWO_REGION_TARGET_KEYS = ["sandbox/000000000000/eu-west-2", "sandbox/000000000000/eu-west-1"]


@pytest.fixture
def two_region_config() -> ConsoleConfig:
    return ConsoleConfig.model_validate(
        {
            "company": {"company_display_name": "Test Co", "company_slug": "testco"},
            "terraform": {},
            "environments": [
                {
                    "slug": "sandbox",
                    "display_name": "Sandbox",
                    "targets": [
                        {"aws_account_id": "000000000000", "aws_region": "eu-west-2", "simulated": True},
                        {"aws_account_id": "000000000000", "aws_region": "eu-west-1", "simulated": True},
                    ],
                },
                {
                    "slug": "production",
                    "display_name": "Production",
                    "targets": [
                        {
                            "aws_account_id": "123456789012",
                            "aws_region": "eu-west-2",
                            "aws_profile_name": "testco-prod",
                        }
                    ],
                },
            ],
        }
    )


@pytest.fixture
def two_region_store(two_region_config, session_factory) -> StaticConfigStore:
    return StaticConfigStore(two_region_config, session_factory)


@pytest.fixture
def two_region_executor(two_region_store) -> InventoryRefreshExecutor:
    return InventoryRefreshExecutor(two_region_store)


def _refresh_operation(environment_slug: str | None, plan: dict) -> Operation:
    return Operation(
        id=uuid.uuid4(),
        operation_kind="inventory_refresh",
        environment_slug=environment_slug,
        plan=plan,
    )


def test_step_names_cover_every_target_when_no_target_keys_are_given(two_region_executor) -> None:
    expected_steps = [f"refresh:{key}" for key in TWO_REGION_TARGET_KEYS]
    assert two_region_executor.step_names(_refresh_operation("sandbox", {})) == expected_steps
    assert two_region_executor.step_names(_refresh_operation("sandbox", {"options": {}})) == expected_steps
    assert (
        two_region_executor.step_names(_refresh_operation("sandbox", {"options": {"target_keys": []}}))
        == expected_steps
    )


def test_step_names_narrow_to_the_requested_targets_of_the_environment(two_region_executor) -> None:
    operation = _refresh_operation(
        "sandbox",
        {
            "options": {
                "target_keys": [
                    "sandbox/000000000000/eu-west-1",
                    "production/123456789012/eu-west-2",
                    "sandbox/000000000000/ap-south-1",
                ]
            }
        },
    )
    assert two_region_executor.step_names(operation) == ["refresh:sandbox/000000000000/eu-west-1"]


def test_step_names_refuse_an_operation_without_an_environment(two_region_executor) -> None:
    operation = _refresh_operation(None, {})
    with pytest.raises(ValueError, match=str(operation.id)):
        two_region_executor.step_names(operation)


def test_narrowed_refresh_only_writes_snapshots_for_the_requested_targets(
    session_factory, operator_settings, two_region_config, two_region_store, moto_aws
) -> None:
    _seed("eu-west-2")
    sync_configuration_to_control_db(session_factory, two_region_config)
    services = build_services(operator_settings, session_factory)
    runner = OperationRunner(
        session_factory,
        build_executors(services, two_region_store),
        "runner-test",
        operator_settings,
        two_region_store,
        services,
    )
    operation = OperationService(session_factory).create(
        "inventory_refresh",
        "sandbox",
        None,
        {"options": {"target_keys": ["sandbox/000000000000/eu-west-2", "production/123456789012/eu-west-2"]}},
        "refresh-narrowed",
        "phil",
    )
    runner.run_once()
    assert OperationService(session_factory).get(operation.id).state == OperationState.SUCCEEDED
    with session_factory() as session:
        written_target_keys = [
            row.target_key for row in session.query(InventorySnapshot).filter_by(operation_id=operation.id)
        ]
    assert written_target_keys == ["sandbox/000000000000/eu-west-2"]
